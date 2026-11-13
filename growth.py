#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 成长中心全套（云端版，GitHub Actions 跑）。

来源：88lin/workbuddy-auto-signin 的 run_growth 逻辑（MIT），按云端改写：
  - 不读本机登录文件、不调本机客户端解密：只用环境变量 WB_TOKEN（仓库 Secret）
  - 接口域名用 www.codebuddy.cn（2026-11-13 只读探针 8/8 验证通过）
  - 顺序照原版：领旅行礼物 → 接单/领任务奖 → 补登 → 连登兑换 → 开盲盒
    → 能量开 Buddy → 派出旅行 → 展示能量/连签
  - 写操作（领奖/兑换/开盒/补登/派出）一律不重试；每步先查状态、未满足就跳过；
    只有 claimed/已解锁/有次数/有卡时才发写请求
  - 推送复用 checkin.py 的 Server 酱通道，PUSH_LEVEL=action：只在领到东西或
    出错时推，空跑不推

退出码：0 = 正常（即使无可领项）；1 = 令牌失效或硬失败（5xx/网络/预算耗尽）
安全约定：全程不打印、不落盘任何令牌内容。
"""

import base64
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from datetime import date as _date

import checkin as _checkin  # 复用推送通道（notify / is_forced），不跑它的 main

DOMAIN = os.environ.get("WB_DOMAIN", "www.codebuddy.cn").strip() or "www.codebuddy.cn"
BASE = "https://" + DOMAIN + "/v2/activity/growth"

REQUEST_TIMEOUT = 30
BUDGET_SECONDS = 420.0
MAKEUP_MAX_PER_RUN = 1
CODE_NO_NETWORK = -1
CODE_BUDGET_OUT = -2

PUSH_LEVEL = os.environ.get("PUSH_LEVEL", "action").strip().lower()

_started_at = None


def now_cn():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S +08")


def _budget_left():
    if _started_at is None:
        return BUDGET_SECONDS
    return BUDGET_SECONDS - (time.monotonic() - _started_at)


def jwt_sub(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode()).get("sub", "")
    except Exception:
        return ""


def build_headers(token):
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy",
        "X-Domain": DOMAIN,
    }
    uid = jwt_sub(token)
    if uid:
        headers["X-User-Id"] = uid
    return headers


# ---------- 小工具（照 88lin 的语义抄） ----------

def dig(obj, key):
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if k in obj and isinstance(obj[k], dict):
                r = dig(obj[k], key)
                if r is not None:
                    return r
    return None


def _api_success(code, body):
    business_code = body.get("code") if isinstance(body, dict) else None
    return 200 <= code < 300 and (
        business_code is None or (type(business_code) is int and business_code == 0))


def _is_hard_failure(code):
    return code >= 500 or code in (CODE_NO_NETWORK, CODE_BUDGET_OUT)


def _http_label(code):
    if code == CODE_NO_NETWORK:
        return "网络不可达"
    if code == CODE_BUDGET_OUT:
        return "时间预算耗尽"
    return "HTTP %s" % code


def _is_no_chance(msg):
    m = str(msg or "").lower()
    if not m:
        return False
    if "insufficient" in m or "not enough" in m:
        return "chance" in m or "balance" in m
    return "no chance" in m


def _is_unknown_tier(code, body):
    if code != 400:
        return False
    m = str(dig(body, "msg") or "").lower()
    return "tier" in m and ("unknown" in m or "unsupported" in m or "invalid" in m)


def _is_tier_locked(code, body):
    if code != 403:
        return False
    m = str(dig(body, "msg") or "")
    return "天数不足" in m or "不足" in m


def as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def fmt_credit(v):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return v


def _first_int(body, key, fallback=0):
    if isinstance(body, dict):
        v = dig(body, key)
        if v is not None:
            return as_int(v)
    return as_int(fallback)


def _fmt_eta(arrive_at, server_now):
    try:
        left = float(arrive_at) - float(server_now)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(left):
        return ""
    if left <= 0:
        return "，已到达待领取"
    minutes = int(round(left / 60.0))
    if minutes < 60:
        return "，约 %d 分钟后回" % max(1, minutes)
    return "，约 %.1f 小时后回" % (left / 3600.0)


def _client_token(prefix="u"):
    return "%s-%s" % (prefix, uuid.uuid4())


_REDEEM_REWARDS = {
    "7d": "+2 能量 +1 补登卡 +1 次抽奖",
    "14d": "+50 积分 +3 能量 +1 补登卡 +1 次抽奖",
    "28d": "+150 积分 +5 能量 +1 补登卡 +1 次抽奖",
}

_REDEEM_TIERS = (
    ("7d", "starter", "入门", 7),
    ("14d", "advanced", "进阶", 14),
    ("28d", "legendary", "巅峰", 28),
)


def _redeem_reward_desc(body, tier):
    bits = []
    credit = as_int(dig(body, "credit_granted"), 0)
    energy = as_int(dig(body, "energy_granted"), 0)
    cards = as_int(dig(body, "cards_granted"), 0)
    chances = as_int(dig(body, "chances_granted"), 0)
    if credit:
        bits.append("+%s 积分" % fmt_credit(credit))
    if energy:
        bits.append("+%s 能量" % fmt_credit(energy))
    if cards:
        bits.append("+%s 补登卡" % fmt_credit(cards))
    if chances:
        bits.append("+%s 次抽奖" % fmt_credit(chances))
    if bits:
        return "（%s）" % " ".join(bits)
    return "（%s）" % _REDEEM_REWARDS.get(tier, "奖励已到账")


def _makeup_candidates(streak_body, heatmap_body):
    def parse_date(value):
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError("补登日期格式无效")
        return _date.fromisoformat(value)

    today_obj = dig(heatmap_body, "today")
    if not isinstance(today_obj, dict):
        raise ValueError("活跃日历缺少服务端日期")
    today = parse_date(today_obj.get("date"))
    launch = parse_date(dig(streak_body, "launch_date"))
    start = max(today.replace(day=1), launch, _date(2026, 6, 17))
    streak = dig(streak_body, "streak")
    if not isinstance(streak, dict):
        raise ValueError("连登状态格式无效")
    made_up = streak.get("makeup_dates", [])
    if not isinstance(made_up, list):
        raise ValueError("已补登记录格式无效")
    made_up = {parse_date(value) for value in made_up}
    cells = dig(heatmap_body, "cells")
    if not isinstance(cells, list):
        raise ValueError("活跃日历缺少日期列表")
    scores = {}
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("活跃日历记录格式无效")
        day = parse_date(cell.get("date"))
        score = cell.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or score < 0:
            raise ValueError("活跃日历分数无效")
        if day in scores and scores[day] != score:
            raise ValueError("活跃日历同一日期的记录冲突")
        scores[day] = score
    return sorted((day.isoformat() for day, score in scores.items()
                   if start <= day < today and score == 0 and day not in made_up), reverse=True)


# ---------- 请求层：GET 可重试，POST 写操作不重试 ----------

def _request(url, headers, method="GET", payload=None, retry=False):
    if _budget_left() <= 0:
        return CODE_BUDGET_OUT, {"error": "已达本次运行时间预算，跳过剩余请求"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    attempts = 3 if retry else 1
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(
                    req, timeout=max(1, min(REQUEST_TIMEOUT, _budget_left())), context=ctx) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, {"raw": raw[:500]}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except Exception:
                data = {"raw": raw[:500]}
            # 5xx 才重试，4xx 是业务答案、重试也没用
            if retry and exc.code >= 500 and attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return exc.code, data
        except Exception as exc:
            if retry and attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return CODE_NO_NETWORK, {"error": str(exc)}
    return CODE_NO_NETWORK, {"error": "重试耗尽"}


def get(url, headers):
    return _request(url, headers, method="GET", retry=True)


def post(url, headers, payload=None):
    return _request(url, headers, method="POST", payload=payload, retry=False)


# ---------- 成长中心全套 ----------

def run_growth(headers):
    base = BASE
    parts = []
    credits_gained = 0
    failures = 0
    hard_failures = 0
    successes = 0

    def _check_auth(code):
        return code in (401, 403)

    def _auth_failure(code):
        if code == 401:
            return 1, {"result": "AUTH_REJECTED", "needs_attention": True,
                       "report": "服务端拒绝认证（HTTP 401），令牌可能已失效，请重新登录拿 token"}
        return 1, {"result": "FORBIDDEN", "needs_attention": True,
                   "report": "服务端拒绝此操作（HTTP 403），请检查账号权限或活动条件"}

    def _note_http(code, body, label, required=False):
        nonlocal failures, hard_failures
        if 200 <= code < 300 and (not required or _api_success(code, body)):
            return False
        reason = _http_label(code)
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error") or body.get("msg") or "")
        parts.append("%s失败：%s" % (label, "%s（%s）" % (reason, detail)
                                     if detail and detail != reason else (detail or reason)))
        if required or _is_hard_failure(code):
            failures += 1
            hard_failures += 1
        return True

    # --- 1. Buddy 旅行：先领已有礼物 ---
    travel = None
    daily_limit = False
    try:
        scode, sbody = get(base + "/buddy/travel/status", headers)
        if scode == CODE_BUDGET_OUT:
            return 1, {"result": "TIMEOUT",
                       "report": "时间预算耗尽，成长中心跳过，下次自动重试"}
        if scode == CODE_NO_NETWORK:
            return 1, {"result": "NETWORK",
                       "report": "网络不可达，成长中心跳过（%s）" % (sbody.get("error") or "")}
        if _check_auth(scode):
            return 1, _auth_failure(scode)[1]
        if not _note_http(scode, sbody, "查旅行状态", required=True):
            state = dig(sbody, "state")
            if state in ("idle", "traveling", "arrived"):
                travel = state
                daily_limit = bool(dig(sbody, "daily_limit_reached"))
            else:
                parts.append("旅行状态无法识别，跳过开 Buddy 与派出")
                failures += 1
                hard_failures += 1
        if travel == "arrived":
            record_id = dig(sbody, "record_id")
            ccode, cbody = post(base + "/buddy/travel/claim", headers, {"record_id": record_id})
            if _check_auth(ccode):
                return 1, _auth_failure(ccode)[1]
            if _api_success(ccode, cbody) and dig(cbody, "reward_credit") is not None:
                got = as_int(dig(cbody, "reward_credit"))
                credits_gained += got
                parts.append("领旅行礼物 +%s 积分" % fmt_credit(got))
                successes += 1
                travel = "idle"
            else:
                msg = (dig(cbody, "msg") or "") if isinstance(cbody, dict) else ""
                parts.append("领旅行礼物失败：%s" % (msg or "HTTP %s" % ccode))
                failures += 1
                hard_failures += _is_hard_failure(ccode)
        if travel == "idle" and daily_limit:
            parts.append("今日旅行名额已用完")
        elif travel == "traveling":
            location = dig(sbody, "location")
            loc_name = location.get("name", "?") if isinstance(location, dict) else "?"
            parts.append("Buddy 旅行中（%s%s）" % (
                loc_name, _fmt_eta(dig(sbody, "arrive_at"), dig(sbody, "server_now"))))
    except Exception as e:
        parts.append("旅行模块异常（%s: %s）" % (type(e).__name__, e))
        failures += 1
        hard_failures += 1

    # --- 2. 任务：接单（未接的）+ 领奖（已完成的） ---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，任务领奖跳过")
    else:
        try:
            tcode, tbody = get(base + "/tasks", headers)
            if _check_auth(tcode):
                return 1, _auth_failure(tcode)[1]
            if not _note_http(tcode, tbody, "查任务列表"):
                tasks = dig(tbody, "tasks") or []
                titles = {t.get("task_code"): t.get("title", t.get("task_code")) for t in tasks}
                pending = [t.get("task_code") for t in tasks
                           if t.get("task_code") and not t.get("locked")
                           and t.get("accept_status") == "not_accepted"]
                for i in range(0, len(pending), 20):
                    if _budget_left() <= 0:
                        parts.append("时间预算耗尽，剩余任务下次再接单")
                        break
                    batch = pending[i:i + 20]
                    acode, abody = post(base + "/tasks/accept", headers,
                                        {"task_codes": batch})
                    if _check_auth(acode):
                        return 1, _auth_failure(acode)[1]
                    results = dig(abody, "results")
                    if not isinstance(results, list):
                        results = [{"task_code": c,
                                    "status": "ok" if 200 <= acode < 300 else "error",
                                    "message": dig(abody, "msg")} for c in batch]
                    for r in results:
                        title = titles.get(r.get("task_code"), r.get("task_code"))
                        if r.get("status") == "error":
                            parts.append("领取任务「%s」失败：%s" % (
                                title, r.get("message") or "HTTP %s" % acode))
                            failures += 1
                            hard_failures += _is_hard_failure(acode)
                        else:
                            parts.append("领取任务「%s」（进度开始计）" % title)
                            successes += 1
                for t in tasks:
                    if _budget_left() <= 0:
                        parts.append("时间预算耗尽，剩余任务奖下次再领")
                        break
                    if t.get("locked") or t.get("accept_status") != "completed":
                        continue
                    code = t.get("task_code")
                    title = titles.get(code, code)
                    try:
                        ccode, cbody = post(base + "/tasks/%s/claim" % code, headers, {})
                        if _check_auth(ccode):
                            return 1, _auth_failure(ccode)[1]
                        if 200 <= ccode < 300 and not dig(cbody, "already_claimed"):
                            rc = _first_int(cbody, "credit", t.get("reward_credit"))
                            re_ = _first_int(cbody, "energy", t.get("reward_energy"))
                            credits_gained += rc
                            parts.append("领任务奖「%s」+credit%s+energy%s" % (title, rc, re_))
                            successes += 1
                        elif 200 <= ccode < 300:
                            parts.append("任务奖「%s」已领过" % title)
                        else:
                            parts.append("领任务奖「%s」失败：%s" % (
                                title, dig(cbody, "msg") or "HTTP %s" % ccode))
                            failures += 1
                            hard_failures += _is_hard_failure(ccode)
                    except Exception as e:
                        parts.append("领任务奖「%s」异常（%s: %s）" % (
                            code, type(e).__name__, e))
                        failures += 1
                        hard_failures += 1
        except Exception as e:
            parts.append("任务模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 3. 补登：每轮最多一张，仅当月断登 ---
    streak_body = None
    streak_stale = False
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，补登跳过")
    else:
        try:
            mcode, mbody = get(base + "/streak", headers)
            if _check_auth(mcode):
                return 1, _auth_failure(mcode)[1]
            if not _note_http(mcode, mbody, "查连登状态", required=True):
                streak_body = mbody
                cards_obj = dig(mbody, "makeup_cards")
                cards = as_int(cards_obj.get("balance")) if isinstance(cards_obj, dict) \
                    else as_int(cards_obj)
                dates = []
                if cards > 0:
                    hcode, hbody = get(base + "/heatmap", headers)
                    if _check_auth(hcode):
                        return 1, _auth_failure(hcode)[1]
                    if not _note_http(hcode, hbody, "查补登日历", required=True):
                        dates = _makeup_candidates(mbody, hbody)
                if dates:
                    made_up = 0
                    for d in dates[:min(cards, MAKEUP_MAX_PER_RUN)]:
                        if _budget_left() <= 0:
                            parts.append("时间预算耗尽，剩余补登下次再做")
                            break
                        ucode, ubody = post(base + "/makeup-cards/use", headers,
                                            {"target_date": d})
                        if _check_auth(ucode):
                            return 1, _auth_failure(ucode)[1]
                        if _api_success(ucode, ubody):
                            cards -= 1
                            made_up += 1
                            streak_stale = True
                            left_obj = dig(ubody, "makeup_cards")
                            left_cards = as_int(left_obj.get("balance"), cards) \
                                if isinstance(left_obj, dict) else as_int(left_obj, cards)
                            parts.append("补登 %s（剩 %s 张卡）" % (d, left_cards))
                            cards = max(0, left_cards)
                            successes += 1
                        else:
                            msg = dig(ubody, "msg") or ""
                            if ucode == 400 and str(msg).strip().lower() == \
                                    "date is not broken, no makeup needed":
                                parts.append("%s 已活跃或已补登，无需再次补登" % d)
                                break
                            parts.append("补登 %s 失败：%s" % (d, msg or "HTTP %s" % ucode))
                            failures += 1
                            hard_failures += 1
                            break
                    if made_up and len(dates) > made_up and cards > 0:
                        parts.append("另有 %s 天可补、剩 %s 张卡，下轮继续" % (
                            len(dates) - made_up, cards))
        except Exception as e:
            parts.append("补登模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 4. 连登兑换：入门 7 天 / 进阶 14 天 / 巅峰 28 天 ---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，连登兑换跳过")
    else:
        try:
            rcode, rbody = get(base + "/redeem/summary", headers)
            if _check_auth(rcode):
                return 1, _auth_failure(rcode)[1]
            if not _note_http(rcode, rbody, "查连登兑换"):
                for tier, status_key, label, days in _REDEEM_TIERS:
                    if _budget_left() <= 0:
                        parts.append("时间预算耗尽，剩余连登兑换下次再领")
                        break
                    status = dig(rbody, status_key + "_status")
                    if not status or status in ("claimed", "locked"):
                        continue
                    c2code, c2body = post(base + "/redeem", headers,
                                          {"tier": tier, "client_token": _client_token()})
                    if _is_unknown_tier(c2code, c2body):
                        c2code, c2body = post(base + "/redeem", headers,
                                              {"tier": days, "client_token": _client_token()})
                    if _is_tier_locked(c2code, c2body):
                        parts.append("连登兑换「%s」未解锁（连登天数不足）" % label)
                        continue
                    if _check_auth(c2code):
                        return 1, _auth_failure(c2code)[1]
                    if 200 <= c2code < 300:
                        credits_gained += as_int(dig(c2body, "credit_granted"))
                        parts.append("连登兑换「%s」%s" % (label, _redeem_reward_desc(c2body, tier)))
                        successes += 1
                    else:
                        msg = dig(c2body, "msg") or ""
                        parts.append("连登兑换「%s」失败：%s" % (label, msg or "HTTP %s" % c2code))
                        failures += 1
                        hard_failures += _is_hard_failure(c2code)
        except Exception as e:
            parts.append("连登兑换模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 5. 盲盒：有次数才开，一轮只开一次 ---
    if _budget_left() <= 0:
        parts.append("时间预算耗尽，盲盒跳过")
    else:
        try:
            lcode, lbody = get(base + "/lottery/chances", headers)
            if _check_auth(lcode):
                return 1, _auth_failure(lcode)[1]
            chances = 0 if _note_http(lcode, lbody, "查抽奖机会") else as_int(dig(lbody, "balance"))
            if chances > 0:
                dcode, dbody = post(base + "/lottery/draw", headers,
                                    {"client_token": _client_token()})
                if _check_auth(dcode):
                    return 1, _auth_failure(dcode)[1]
                if 200 <= dcode < 300:
                    prize = dig(dbody, "prize_name") or dig(dbody, "prize") or "未知"
                    if not isinstance(prize, str):
                        prize = str(prize)
                    if dig(dbody, "need_address") or dig(dbody, "require_address"):
                        prize += "（实物奖，需到成长中心填写收件信息）"
                    parts.append("开盲盒获得：%s" % prize)
                    successes += 1
                    if chances > 1:
                        parts.append("还剩 %s 次抽奖机会，下轮继续" % (chances - 1))
                else:
                    msg = dig(dbody, "msg") or ""
                    if _is_no_chance(msg):
                        parts.append("开盲盒：%s" % (msg or "无抽奖机会"))
                    else:
                        parts.append("开盲盒失败：%s" % (msg or "HTTP %s" % dcode))
                        failures += 1
                        hard_failures += _is_hard_failure(dcode)
        except Exception as e:
            parts.append("盲盒模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 6. Buddy 盲盒：旅行空闲、能量够才开 ---
    if travel != "idle":
        parts.append("Buddy 盲盒跳过：%s" % (
            "旅行尚未结束或礼物尚未领到" if travel in ("traveling", "arrived") else "旅行状态未知"))
    elif _budget_left() <= 0:
        parts.append("时间预算耗尽，Buddy 盲盒跳过")
    else:
        try:
            qcode, qbody = get(base + "/buddy/quota", headers)
            if _check_auth(qcode):
                return 1, _auth_failure(qcode)[1]
            if not _note_http(qcode, qbody, "查 Buddy 能量"):
                affordable = as_int(dig(qbody, "affordable"))
                max_open = as_int(dig(qbody, "max_open_count"), 1) or 1
                if affordable > 0:
                    count = min(affordable, max_open)
                    ocode, obody = post(base + "/buddy/open", headers,
                                        {"count": count, "client_token": _client_token()})
                    if _check_auth(ocode):
                        return 1, _auth_failure(ocode)[1]
                    if 200 <= ocode < 300:
                        name = dig(obody, "buddy") or dig(obody, "name") or dig(obody, "buddies")
                        if not isinstance(name, str):
                            name = "新 Buddy"
                        parts.append("开 Buddy 盲盒 ×%s（%s）" % (count, name))
                        successes += 1
                    else:
                        msg = dig(obody, "msg") or ""
                        parts.append("开 Buddy 盲盒失败：%s" % (msg or "HTTP %s" % ocode))
                        failures += 1
                        hard_failures += _is_hard_failure(ocode)
        except Exception as e:
            parts.append("Buddy 盲盒模块异常（%s: %s）" % (type(e).__name__, e))
            failures += 1
            hard_failures += 1

    # --- 6b. 最后派出：空闲且名额未用完才派 ---
    if travel == "idle" and not daily_limit:
        if _budget_left() <= 0:
            parts.append("时间预算耗尽，派 Buddy 跳过")
        else:
            try:
                ccode, cbody = get(base + "/buddy/travel/config", headers)
                if _check_auth(ccode):
                    return 1, _auth_failure(ccode)[1]
                if not _note_http(ccode, cbody, "查旅行配置", required=True):
                    locs = dig(cbody, "locations")
                    if locs and isinstance(locs[0], dict):
                        dcode, dbody = post(base + "/buddy/travel/depart", headers,
                                            {"location_id": locs[0].get("id")})
                        msg = str(dig(dbody, "msg") or "")
                        if dcode == 403 and msg.strip().lower() == "no active buddy":
                            parts.append("派 Buddy 跳过：尚未领养 Buddy")
                        elif _check_auth(dcode):
                            return 1, _auth_failure(dcode)[1]
                        elif _api_success(dcode, dbody):
                            location = dig(dbody, "location")
                            location = location if isinstance(location, dict) else {}
                            loc_name = location.get("name", "?")
                            dur = dig(dbody, "duration_hours") or location.get("duration_hours", "?")
                            parts.append("派 Buddy 去%s（%s 小时后回）" % (loc_name, dur))
                            successes += 1
                        else:
                            parts.append("派 Buddy 失败：%s" % (msg or "HTTP %s" % dcode))
                            failures += 1
                            hard_failures += _is_hard_failure(dcode)
            except Exception as e:
                parts.append("派 Buddy 模块异常（%s: %s）" % (type(e).__name__, e))
                failures += 1
                hard_failures += 1

    # --- 7. 能量 & 连签（纯展示） ---
    energy = None
    streak_days = None
    if _budget_left() > 0:
        try:
            ecode, ebody = get(base + "/energy", headers)
            if not _check_auth(ecode):
                energy = dig(ebody, "balance") if (200 <= ecode < 300) else None
        except Exception:
            pass
    try:
        if streak_body is not None and not streak_stale:
            streak_obj = dig(streak_body, "streak") or {}
            streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
        elif _budget_left() > 0:
            scode2, sbody2 = get(base + "/streak", headers)
            if not _check_auth(scode2):
                streak_obj = dig(sbody2, "streak") or {}
                streak_days = streak_obj.get("days") if isinstance(streak_obj, dict) else None
    except Exception:
        pass

    tail = []
    if energy is not None:
        tail.append("能量 %s" % energy)
    if streak_days is not None:
        tail.append("连签 %s 天" % streak_days)
    if credits_gained:
        tail.append("本次 +共 %s 积分" % credits_gained)

    if parts:
        report = "；".join(parts)
    elif failures:
        report = "成长中心各步骤均失败"
    else:
        report = "成长中心无可领取项"
    if tail:
        report += "（%s）" % "，".join(tail)

    result_code = 1 if hard_failures else 0
    idle = (successes == 0 and failures == 0)
    return result_code, {"result": "GROWTH", "report": report,
                         "credits_gained": credits_gained,
                         "energy": energy, "streak_days": streak_days, "idle": idle,
                         **({"failures": failures} if failures else {})}


# ---------- 入口：汇报 + 推送 ----------

def emit(result):
    forced = _checkin.is_forced()
    if PUSH_LEVEL == "off":
        notify_it = False
    elif forced:
        notify_it = True
    elif result.get("result") in ("AUTH_REJECTED", "FORBIDDEN", "TIMEOUT", "NETWORK"):
        notify_it = True
    elif PUSH_LEVEL == "all":
        notify_it = True
    else:  # action：只在领到东西或硬失败时推
        notify_it = result.get("credits_gained", 0) > 0 or "failures" in result
    if notify_it:
        title = "WorkBuddy 成长中心 · %s" % result.get("result", "")
        content = "%s\n时间：%s" % (result.get("report", ""), now_cn())
        result["push"] = _checkin.notify(title, content)
        _checkin.report_push(result["push"])
    print(json.dumps(result, ensure_ascii=False))


def main():
    global _started_at
    _started_at = time.monotonic()
    token = os.environ.get("WB_TOKEN", "").strip()
    if not token or len(token) < 20:
        emit({"status": "error", "action": "growth", "time": now_cn(),
              "source": "env:WB_TOKEN",
              "msg": "未找到 WB_TOKEN（Secret 缺失或为空）"})
        return 1
    headers = build_headers(token)
    try:
        code, out = run_growth(headers)
    except Exception as exc:
        emit({"status": "error", "action": "growth", "time": now_cn(),
              "source": "env:WB_TOKEN",
              "msg": "成长中心异常：%s: %s" % (type(exc).__name__, exc)})
        return 1
    out = dict(out)
    out["status"] = "error" if code else "ok"
    out["action"] = "growth"
    out["time"] = now_cn()
    out["source"] = "env:WB_TOKEN"
    emit(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
