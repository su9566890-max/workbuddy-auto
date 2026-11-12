#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy「派猫猫旅行」自动闭环（幂等，纯标准库）。

玩法：把 Buddy 派去一个地点旅行 1~4 小时，回来后领取 5~10 积分；每日限一次。

本脚本每次运行只做「读一次状态 → 决定一个动作」，不做后台轮询：

    state=arrived            → 领取（claim，只领一次）
    state=traveling          → 跳过（绝不重复派出）
    daily_limit_reached=true → 跳过（今日额度已用尽）
    state=idle（且未达上限） → 派出（depart）

幂等由服务端状态保证：重复运行、定时器抖动都不会重复派或重复领。

令牌：复用签到那一个，无需浏览器 Cookie。
    WB_TOKEN   accessToken（必填，与 checkin.py 共用同一个 Secret）
    未设置时自动读取本机 WorkBuddy 登录态文件（便于本地调试）。

可选：设置 NOTIFY_CHANNEL 后，结果会推送到手机。当前支持：
      bark       = Bark（iPhone 原生通知，走 APNs，无条数限制）
                   密钥放 BARK_KEY（App 首页那串 key，或整条测试 URL 均可）
                   可选 BARK_SERVER（自建域名）、BARK_GROUP、BARK_LEVEL
      serverchan = Server 酱（微信），密钥放 SERVERCHAN_KEY
                   ⚠️ 免费额度只有约 5 条/天，不适合 PUSH_LEVEL=all
    未设置 NOTIFY_CHANNEL 时默认 serverchan（向后兼容既有部署）。

    推送级别由 PUSH_LEVEL 控制：
      all    = 每次巡检都推送（默认，含「旅行中」「额度已用尽」等巡检结果）
      action = 只在「派出成功 / 领取成功 / 出错」时推送
      off    = 本仓彻底关闭推送（无推送、无告警噪音）
    推送结果写回结果 JSON 的 push 字段并打印（CI 下额外输出 GitHub 注解），
    故密钥填错/未配置时不再静默。
    调试：设置 FORCE_NOTIFY=1（Actions 手动触发时勾选 force_notify）**强制推一条**，
    即便本次结果是「跳过」也照推，标题带「（测试）」前缀 —— 用于在无派出/无领取的时刻
    也能验证推送配置是否正确。这是旅行侧唯一的推送验证入口。

用法：
    python travel.py                # 巡检一轮
    python travel.py --dry-run      # 只查状态，不做任何写操作
    python travel.py --location 1   # 指定地点（1咖啡馆 2商场 3健身房 4客栈）

退出码：0 = 正常（含跳过）；1 = 失败
安全约定：全程不打印、不落盘任何令牌内容。
"""

import argparse
import json
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# 域名：默认与签到保持同一个域（www.codebuddy.cn），可用 WB_DOMAIN 覆盖。
#
# 2026-10-12 复盘：曾把「旅行连续失败、签到一直正常」误判为 workbuddy.cn 被 GitHub
# 机房 IP 风控。该假设已被证伪 —— 两个域名从 GitHub runner 都能正常访问，真正原因是
# 仓库 Secret WB_TOKEN 里的令牌到期（accessToken 有效期 28 天），签到与旅行在同一时刻
# 一起失效。保留 WB_DOMAIN 可配、并把实际请求域名写进日志与推送，是为了两侧行为一致、
# 排查时不再被域名差异分散注意力。
DEFAULT_DOMAIN = "www.codebuddy.cn"
DOMAIN = (os.environ.get("WB_DOMAIN") or "").strip() or DEFAULT_DOMAIN
BASE = "https://%s/activity/growth/buddy/travel" % DOMAIN
TIMEOUT = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
LOCATIONS = {1: "咖啡馆", 2: "商场店铺", 3: "健身房", 4: "古镇客栈"}

LOCAL_FILES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 r"CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info"),
    os.path.expanduser(
        "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"),
]


# ---------------------------------------------------------------- 基础设施

def now_cn():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S +08")


def read_local_token():
    for path in LOCAL_FILES:
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    auth = (json.load(fh) or {}).get("auth") or {}
            except Exception:
                continue
            tok = (auth.get("accessToken") or "").strip()
            if tok:
                return tok, "local:%s" % path
    return None, None


def call(token, path, method="GET", payload=None):
    """返回 (http_status, parsed_body)。"""
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "authorization": "Bearer " + token,
        "referer": "https://%s/profile/growth-center" % DOMAIN,
        "origin": "https://%s" % DOMAIN,
        "user-agent": UA,
        "x-client-platform": "web",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"code": -1, "msg": raw[:200]}
    except Exception as e:
        return 0, {"code": -1, "msg": "%s: %s" % (type(e).__name__, e)}


def try_payloads(token, path, payloads):
    """写操作按顺序尝试多种字段名（接口未公开文档，抗字段改名）。"""
    last = None
    for payload in payloads:
        st, body = call(token, path, method="POST", payload=payload)
        if st in (401, 403) or body.get("code") in (401, 403):
            return False, body, payload, st
        if st == 200 and body.get("code") == 0:
            return True, body.get("data") or {}, payload, st
        last = (body, payload, st)
    if last:
        return False, last[0], last[1], last[2]
    return False, {"code": -1, "msg": "无可用 payload"}, {}, 0


# ---------------------------------------------------------------- 通知

def sendkey_problem(key):
    """检查 SERVERCHAN_KEY 的形态，返回问题描述；None 表示形态正常。

    血泪教训：曾出现「SERVERCHAN_KEY 被填成 accessToken」的情况，推送失败被静默吞掉，
    旅行一切正常但收不到微信。故改为显式回报推送结果。
    """
    if not key:
        return "未配置 SERVERCHAN_KEY（Secret 缺失或为空）"
    if key.startswith("eyJ"):
        return ("SERVERCHAN_KEY 疑似被填成了 accessToken（JWT）—— "
                "WB_TOKEN 才填 eyJ 开头的令牌，SERVERCHAN_KEY 应为 SCT 开头的 SendKey")
    if not key.upper().startswith("SCT"):
        return "SERVERCHAN_KEY 不像 Server 酱 SendKey（应以 SCT 开头，实际前缀 %s...，共 %d 字符）" % (
            key[:4], len(key))
    return None


DEFAULT_BARK_SERVER = "https://api.day.app"
NOTIFY_CHANNEL = os.environ.get("NOTIFY_CHANNEL", "serverchan").strip().lower()


def _bark_target():
    """从 BARK_KEY 解析出 (server, key, 问题)；兼容纯 key / App 首页整条测试 URL / 自建域名。"""
    raw = os.environ.get("BARK_KEY", "").strip()
    if not raw:
        return None, None, "未配置 BARK_KEY（Secret 缺失或为空）"
    server = os.environ.get("BARK_SERVER", "").strip() or DEFAULT_BARK_SERVER
    key = raw
    if raw[:7] == "http://" or raw[:8] == "https://":
        parsed = urllib.parse.urlparse(raw)
        segs = [s for s in parsed.path.split("/") if s]
        if not segs:
            return None, None, "BARK_KEY 是 URL 但解析不出 key（应为 /<key>/… 形式）"
        server, key = "%s://%s" % (parsed.scheme, parsed.netloc), segs[0]
    if key.startswith("eyJ"):
        return None, None, ("BARK_KEY 疑似被填成了 accessToken（JWT）—— "
                            "WB_TOKEN 才填 eyJ 开头的令牌，BARK_KEY 应是 App 首页那串 key")
    if key.upper().startswith("SCT"):
        return None, None, ("BARK_KEY 疑似填成了 Server 酱 SendKey（SCT 开头）—— "
                            "Bark 用的是 App 首页那串 key（或整条测试 URL）")
    if len(key) < 12 or not re.fullmatch(r"[A-Za-z0-9_\-]+", key):
        return None, None, "BARK_KEY 形态不对（应为 App 首页的 key 或整条测试 URL；当前长度 %d）" % len(key)
    return server, key, None


def _send_serverchan(title, content):
    """Server 酱（sct.ftqq.com）。免费额度只有约 5 条/天，不适合 PUSH_LEVEL=all。"""
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    problem = sendkey_problem(key)
    if problem:
        return "skipped: " + problem
    req = urllib.request.Request(
        "https://sctapi.ftqq.com/%s.send" % key,
        data=urllib.parse.urlencode({"title": title, "desp": content}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8", "ignore"))
    # Server 酱业务失败也可能回 HTTP 200，必须看 body 的 code
    if body.get("code") == 0:
        return "ok: pushid=%s" % ((body.get("data") or {}).get("pushid"))
    return "failed: code=%s msg=%s" % (body.get("code"), body.get("message"))


def _send_bark(title, content):
    """Bark（iOS 原生通知，走 APNs）。无条数限制。"""
    server, key, problem = _bark_target()
    if problem:
        return "skipped: " + problem
    payload = {
        "title": title,
        "body": content,
        "group": os.environ.get("BARK_GROUP", "workbuddy").strip() or "workbuddy",
        # active(默认) / timeSensitive(可突破专注模式) / critical(静音也响) / passive
        "level": os.environ.get("BARK_LEVEL", "active").strip() or "active",
    }
    req = urllib.request.Request(
        # 用 API v1 的 POST /<key>（兼容性最好）；不采用 /push，那是较新服务端才有的形态
        "%s/%s" % (server.rstrip("/"), key),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8", "ignore"))
    if body.get("code") == 200:
        return "ok: code=200"
    return "failed: code=%s msg=%s" % (body.get("code"), body.get("message"))


# 通知后端表：加渠道只需在这里加一行。约定 —— 入参 (title, content)，返回结果字符串。
CHANNELS = {
    "serverchan": _send_serverchan,
    "bark": _send_bark,
}


def notify(title, content):
    """按 NOTIFY_CHANNEL 分发推送，返回结果字符串（始终带渠道标签）。"""
    sender = CHANNELS.get(NOTIFY_CHANNEL)
    if sender is None:
        return "skipped: [%s] 未知渠道（NOTIFY_CHANNEL 可用值：%s）" % (
            NOTIFY_CHANNEL, " / ".join(sorted(CHANNELS)))
    try:
        status = sender(title, content)
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "ignore")
        # 各家错误信息都在 JSON body 里，解开转义更可读
        try:
            j = json.loads(raw)
            status = "failed: HTTP %s code=%s msg=%s" % (
                err.code, j.get("code"), j.get("message") or j.get("info"))
        except Exception:
            status = "failed: HTTP %s %s" % (err.code, raw[:180])
    except Exception as err:
        status = "failed: %s: %s" % (type(err).__name__, err)
    head, _, rest = status.partition(": ")
    return "%s: [%s] %s" % (head, NOTIFY_CHANNEL, rest)


def report_push(status):
    """把推送结果打到日志；CI 下额外发 GitHub 注解，使失败在 run 摘要可见。"""
    print("[push] %s" % status)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        level = "notice" if status.startswith("ok") else "warning"
        print("::%s::notify(%s) -> %s" % (level, NOTIFY_CHANNEL, status.replace("\n", " ")))


# 推送级别（环境变量 PUSH_LEVEL）：
#   all    = 每次巡检都推送（默认；用户要求「巡检结果也推送到 Server 酱」）
#   action = 仅在派出 / 领取 / 出错时推送（安静模式，正常每天最多 2 条）
#   off    = 本仓彻底关闭推送（什么都不推，也不输出告警注解）
PUSH_ACTIONS = ("departed", "claimed")
PUSH_LEVEL = os.environ.get("PUSH_LEVEL", "all").strip().lower()


def is_forced():
    """手动触发时勾选 force_notify，强制推送一条，用于验证推送配置。

    这一层是「可手动验证入口」原则的落实：`action`/`off` 模式下，跳过类结果默认不推送，
    于是「推送到底通不通」在旅行侧无法验证 —— 曾因此让用户误以为推送坏了。
    """
    return os.environ.get("FORCE_NOTIFY", "").strip().lower() in ("1", "true", "yes")


def should_push(result):
    # 手动强制推送优先（即便本次是「跳过」也要推一条，用于验证推送配置）
    if is_forced():
        return True
    if PUSH_LEVEL == "off":
        return False
    if result.get("status") == "error":
        return True
    if PUSH_LEVEL == "all":
        return True
    return result.get("action") in PUSH_ACTIONS


def fmt_remain(minutes):
    minutes = int(minutes)
    if minutes >= 60:
        return "%d 小时 %d 分" % (minutes // 60, minutes % 60)
    return "%d 分钟" % minutes


def build_title(result):
    # 测试推送加前缀，便于和真实通知区分（force_notify 时）
    label = os.environ.get("WB_LABEL", "").strip()
    prefix = ("【%s】" % label if label else "") + ("（测试）" if is_forced() else "")
    a = result.get("action")
    if result.get("status") == "error":
        return prefix + "猫猫旅行异常，需要处理"
    if a == "claimed":
        return prefix + "猫猫旅行归来 +%s 积分" % result.get("reward_credit", "?")
    if a == "departed":
        return prefix + "猫猫已出发 · %s" % result.get("location", "")
    if a == "skip_traveling":
        rm = result.get("remain_minutes")
        if rm and rm > 0:
            return prefix + "猫猫巡检 · 旅行中（还有 %s）" % fmt_remain(rm)
        return prefix + "猫猫巡检 · 旅行中"
    if a == "skip_daily_limit":
        return prefix + "猫猫巡检 · 今日已完成"
    if a == "none":
        return prefix + "猫猫巡检 · 只读查询"
    return prefix + "猫猫巡检"


def build_body(result):
    lines = ["时间：%s" % result.get("time", ""),
             "状态：%s" % result.get("state", "-")]
    # 把本次实际请求的域名写进消息：域名是这轮排查的关键变量，漏掉它下次还得再挖一遍
    if result.get("domain"):
        lines.append("域名：%s" % result["domain"])
    # 出错时 action 也可能是 "none"，此时不列动作，只给原因，避免误显成 dry-run
    if result.get("action") and result.get("status") != "error":
        lines.append("动作：%s" % {
            "departed": "派出旅行", "claimed": "领取奖励",
            "skip_traveling": "旅行中，等待归来",
            "skip_daily_limit": "今日额度已用尽，跳过",
            "none": "只读查询（dry-run）",
        }.get(result["action"], result["action"]))
    if result.get("location"):
        lines.append("地点：%s" % result["location"])
    if result.get("reward_credit"):
        lines.append("本次积分：+%s" % result["reward_credit"])
    if result.get("remain_minutes"):
        lines.append("剩余时间：约 %s" % fmt_remain(result["remain_minutes"]))
    if result.get("arrive_at"):
        lines.append("预计归来：%s" % result["arrive_at"])
    if result.get("status") == "error" and result.get("msg"):
        lines.append("原因：%s" % result["msg"])
    return "\n".join(lines)


def emit(result):
    notify_it = should_push(result)
    if notify_it:
        result["push"] = notify(build_title(result), build_body(result))
    print(json.dumps(result, ensure_ascii=False))
    if notify_it:
        report_push(result["push"])


# ---------------------------------------------------------------- 主流程

def fmt_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S +08")
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只查状态，不做写操作")
    ap.add_argument("--location", type=int, default=0,
                    help="地点 id（1咖啡馆 2商场 3健身房 4客栈），0=随机")
    args = ap.parse_args()

    token = os.environ.get("WB_TOKEN", "").strip()
    source = "env:WB_TOKEN"
    if not token:
        token, source = read_local_token()
    if not token:
        emit({"status": "error", "action": "none", "time": now_cn(), "domain": DOMAIN,
              "msg": "未找到令牌：请设置环境变量 WB_TOKEN，或先在本机登录 WorkBuddy 桌面端"})
        return 1

    st, body = call(token, "/status")
    if st in (401, 403) or body.get("code") in (401, 403):
        # 401 与 403 必须分开说。403 多数是网关/风控按出口 IP 或地域拦截，跟令牌没有关系；
        # 曾把两者合并报成「令牌已失效」，于是每次排查都被引去重取令牌，反而掩盖了真问题。
        code = st if st in (401, 403) else body.get("code")
        if code == 401:
            msg = "令牌已失效（HTTP 401），请重新获取 accessToken 并更新 Secret"
        else:
            msg = ("请求被拒绝（HTTP %s）：域名 %s 疑似被网关/风控拦截（机房 IP 或地域限制），"
                   "不是令牌失效。请先在本机验证该域名是否可访问，或改用可访问的域名（WB_DOMAIN）"
                   % (code, DOMAIN))
        emit({"status": "error", "action": "none", "time": now_cn(), "source": source,
              "domain": DOMAIN, "msg": msg})
        return 1
    if body.get("code") != 0:
        emit({"status": "error", "action": "none", "time": now_cn(), "source": source,
              "domain": DOMAIN, "msg": "状态查询失败：%s" % body.get("msg")})
        return 1

    d = body.get("data") or {}
    state = (d.get("state") or "").lower()
    limit = bool(d.get("daily_limit_reached"))
    record_id = d.get("record_id") or 0
    loc_name = (d.get("location") or {}).get("name")
    arrive_at = fmt_ts(d.get("arrive_at"))
    # 剩余时间（分钟）：用服务端时间算，避免本机时钟偏差
    remain_minutes = None
    if d.get("arrive_at") and d.get("server_now"):
        remain_minutes = max(0, int((int(d["arrive_at"]) - int(d["server_now"])) // 60))
    base = {"time": now_cn(), "state": state, "source": source, "domain": DOMAIN,
            "daily_limit_reached": limit, "record_id": record_id,
            "location": loc_name, "arrive_at": arrive_at}
    if remain_minutes:
        base["remain_minutes"] = remain_minutes

    if args.dry_run:
        emit(dict(base, status="ok", action="none", msg="dry-run：仅查询"))
        return 0

    # 1) 已归来 → 领取
    if state == "arrived":
        ok, data, used, st = try_payloads(token, "/claim", [
            {"record_id": record_id}, {"recordId": record_id}, {},
        ])
        if not ok:
            emit(dict(base, status="error", action="claim",
                      msg="领取失败：HTTP %s / %s" % (st, data.get("msg") or data.get("code"))))
            return 1
        emit(dict(base, status="ok", action="claimed",
                  reward_credit=data.get("reward_credit") or d.get("reward_credit") or 0,
                  msg="领取成功"))
        return 0

    # 2) 旅行中 → 跳过
    if state == "traveling":
        emit(dict(base, status="ok", action="skip_traveling", msg="旅行中，等待归来"))
        return 0

    # 3) 今日额度已用尽 → 跳过
    if limit:
        emit(dict(base, status="ok", action="skip_daily_limit", msg="今日额度已用尽"))
        return 0

    # 4) 空闲 → 派出
    loc_id = args.location if args.location in LOCATIONS else random.choice(list(LOCATIONS))
    code = {1: "coffee", 2: "mall", 3: "gym", 4: "inn"}[loc_id]
    ok, data, used, st = try_payloads(token, "/depart", [
        {"location_id": loc_id}, {"locationId": loc_id},
        {"location": loc_id}, {"location_code": code},
    ])
    if not ok:
        emit(dict(base, status="error", action="depart",
                  msg="派出失败：HTTP %s / %s" % (st, data.get("msg") or data.get("code"))))
        return 1
    emit(dict(base, status="ok", action="departed", location=LOCATIONS[loc_id],
              record_id=data.get("record_id") or record_id,
              arrive_at=fmt_ts(data.get("arrive_at")) or arrive_at,
              duration_hours=data.get("duration_hours"),
              msg="派出成功"))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
