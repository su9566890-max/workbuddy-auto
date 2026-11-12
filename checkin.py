#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 「Buddy 加油站」每日签到（幂等）。

两种运行方式：
1) 云端（GitHub Actions / 任意 CI）：通过环境变量注入令牌
     WB_TOKEN   accessToken（必填，存在仓库 Secret 中）
     WB_DOMAIN  可选，默认 www.codebuddy.cn
2) 本机直接运行：未设置 WB_TOKEN 时，自动读取 WorkBuddy 桌面端登录态文件
     Windows: %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
     macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info

可选：设置 NOTIFY_CHANNEL 后，结果会推送到手机。当前支持：
      bark       = Bark（iPhone 原生通知，走 APNs，无条数限制）
                   密钥放 BARK_KEY（App 首页那串 key，或整条测试 URL 均可）
                   可选 BARK_SERVER（自建域名）、BARK_GROUP、BARK_LEVEL
      serverchan = Server 酱（微信），密钥放 SERVERCHAN_KEY
                   ⚠️ 免费额度只有约 5 条/天，不适合 PUSH_LEVEL=all
    未设置 NOTIFY_CHANNEL 时默认 serverchan（向后兼容既有部署）。

    推送级别由 PUSH_LEVEL 控制：
      all    = 每次巡检都推送（默认）
      action = 只在「领取成功（claimed）」或「出错（error）」时推送
      off    = 本仓彻底关闭推送（无推送、无告警噪音）
    推送结果会写回结果 JSON 的 push 字段并打印（CI 下额外输出 GitHub 注解），
    因此密钥填错/未配置时不再静默 —— 排查推送问题先看这一行。
    ⚠️ 易混淆：WB_TOKEN 是 `eyJ...` 开头的 JWT（1000+ 字符），
       各类推送密钥都不是它，填错会被形态检查当场指出。
    调试：设置 FORCE_NOTIFY=1（Actions 手动触发时可勾选 force_notify）强制推一条，
    用于验证推送配置是否正确。

退出码：0 = 签到成功或今日已签到；1 = 失败（令牌失效 / 网络异常 / 未知错误）
安全约定：全程不打印、不落盘任何令牌内容。
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_DOMAIN = "www.codebuddy.cn"
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"
TIMEOUT = 30
ALREADY_CLAIMED_CODE = 10001

LOCAL_FILES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 r"CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info"),
    os.path.expanduser(
        "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"),
]


def now_cn():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S +08")


def read_local_credentials():
    """读取本机登录态文件，返回 (token, domain, path) 或 (None, None, None)。"""
    for path in LOCAL_FILES:
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    auth = (json.load(fh) or {}).get("auth") or {}
            except Exception:
                continue
            token = (auth.get("accessToken") or "").strip()
            if token:
                return token, (auth.get("domain") or DEFAULT_DOMAIN).strip(), path
    return None, None, None


def post(domain, path, token):
    """POST 空 JSON 体，返回 (http_status, parsed_body)。"""
    req = urllib.request.Request(
        "https://" + domain + path,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WorkBuddy-Checkin-Action/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "ignore")
        try:
            return err.code, json.loads(raw)
        except Exception:
            return err.code, {"code": -1, "msg": raw[:200]}
    except Exception as err:  # 网络层异常
        return 0, {"code": -1, "msg": "%s: %s" % (type(err).__name__, err)}


def sendkey_problem(key):
    """检查 SERVERCHAN_KEY 的形态，返回问题描述；None 表示形态正常。

    这一层是血泪教训：曾出现「SERVERCHAN_KEY 被填成 accessToken」的情况，
    因为推送失败被静默吞掉，签到一切正常但收不到任何微信，用户完全无从判断。
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
    """从 BARK_KEY 解析出 (server, key, 问题)；支持纯 key、App 首页整条测试 URL、自建域名。

    Bark App 首页给的是「https://api.day.app/<key>/推送内容」这种整条 URL，用户往往整条复制，
    所以这里兼容 URL 形态，并顺带取出服务器地址（自建时 URL 里就是自己的域名）。
    """
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
    """Server 酱（sct.ftqq.com）。注意免费额度只有约 5 条/天，不适合 PUSH_LEVEL=all。"""
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
    # Server 酱在业务失败时也可能返回 HTTP 200，必须看 body 里的 code
    if body.get("code") == 0:
        return "ok: pushid=%s" % ((body.get("data") or {}).get("pushid"))
    return "failed: code=%s msg=%s" % (body.get("code"), body.get("message"))


def _send_bark(title, content):
    """Bark（iOS 原生通知，走 APNs）。无条数限制，只需 App 首页那串 key。"""
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


# 通知后端表：加渠道只需在这里加一行。约定 —— 入参 (title, content)，返回结果字符串，
# 异常直接抛出交给 notify() 统一兜（这样各后端不必各写一遍 try/except）。
CHANNELS = {
    "serverchan": _send_serverchan,
    "bark": _send_bark,
}


def notify(title, content):
    """按 NOTIFY_CHANNEL 分发推送，**返回结果字符串**（不存在静默吞异常的版本）。

    返回形态（始终带渠道标签）：
      ok: [bark] code=200        推送成功
      skipped: [bark] <原因>      未推送（未配置 / 形态明显不对），原因即诊断结论
      failed: [bark] ...         已发起但失败（HTTP 错误 / 业务 code 非预期）
    """
    sender = CHANNELS.get(NOTIFY_CHANNEL)
    if sender is None:
        return "skipped: [%s] 未知渠道（NOTIFY_CHANNEL 可用值：%s）" % (
            NOTIFY_CHANNEL, " / ".join(sorted(CHANNELS)))
    try:
        status = sender(title, content)
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "ignore")
        # 各家错误信息都在 JSON body 里（如 Server 酱的 [AUTH]错误的Key），解开转义更可读
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


def is_forced():
    """手动触发时勾选 force_notify，强制推送一条，用于验证 SERVERCHAN_KEY 配置。"""
    return os.environ.get("FORCE_NOTIFY", "").strip().lower() in ("1", "true", "yes")


def build_title(result):
    label = os.environ.get("WB_LABEL", "").strip()
    prefix = ("【%s】" % label if label else "") + ("（测试）" if is_forced() else "")
    if result.get("status") == "error":
        return prefix + "签到异常，需要处理"
    if result.get("action") == "claimed":
        credit = result.get("credit")
        return prefix + "签到成功 +%s 积分" % (credit if credit is not None else "?")
    if result.get("action") == "skip_already_signed":
        return prefix + "签到巡检 · 今日已签到"
    return prefix + "签到巡检"


def build_body(result):
    if result.get("status") == "error":
        outcome = "异常"
    else:
        outcome = {
            "claimed": "领取成功",
            "skip_already_signed": "今日已签到，无需重复领取",
        }.get(result.get("action"), result.get("status", ""))
    lines = [
        "时间：%s" % result.get("time", ""),
        "结果：%s" % outcome,
    ]
    if result.get("credit") is not None:
        lines.append("本次积分：+%s" % result["credit"])
    if result.get("streak_days") is not None:
        lines.append("连续签到：%s 天" % result["streak_days"])
    # 仅在出错时重复打印原因，成功路径的 msg 与「结果」重复，没必要刷屏
    if result.get("status") == "error" and result.get("msg"):
        lines.append("原因：%s" % result["msg"])
    return "\n".join(lines)


# 推送级别（环境变量 PUSH_LEVEL）：
#   all    = 每次巡检都推送（默认；用户要求「巡检结果也推送到 Server 酱」）
#           每天 5 个触发时点，其中 4 次是 skip（今日已领），会各推一条巡检结果
#   action = 只在「真正领到积分」或「出错」时推送（安静模式，每天最多 1 条）
#   off    = 本仓彻底关闭推送（什么都不推、也不输出告警注解）。
#           给「不想要微信推送」的仓库用：比删掉 SERVERCHAN_KEY 更干净 ——
#           删密钥会让每次运行都出现 skipped/告警噪音，off 则完全静默。
PUSH_ACTIONS = ("claimed",)
PUSH_LEVEL = os.environ.get("PUSH_LEVEL", "all").strip().lower()


def should_push(result):
    # 手动强制推送优先保留，便于将来重新启用推送时仍能一次验证通断
    if is_forced():
        return True
    if PUSH_LEVEL == "off":
        return False
    if result.get("status") == "error":
        return True
    if PUSH_LEVEL == "all":
        return True
    return result.get("action") in PUSH_ACTIONS


def emit(result, notify_it=None):
    if notify_it is None:
        notify_it = should_push(result)
    if notify_it:
        # 正文只给结论（时间 / 结果 / 本次积分 / 连续天数，出错时附原因）。
        # 完整的原始 JSON 与推送结果留在 stdout 与 [push] 日志行里 ——
        # 手机通知保持清爽，排错能力不损失。
        result["push"] = notify(build_title(result), build_body(result))
    print(json.dumps(result, ensure_ascii=False))
    if notify_it:
        report_push(result["push"])


def main():
    token = os.environ.get("WB_TOKEN", "").strip()
    domain = os.environ.get("WB_DOMAIN", "").strip()
    source = "env:WB_TOKEN"

    if not token:
        token, local_domain, path = read_local_credentials()
        if token:
            source = "local:%s" % path
            domain = domain or local_domain

    if not token:
        emit({"status": "error", "action": "none", "time": now_cn(),
              "msg": "未找到令牌：请设置环境变量 WB_TOKEN，或先在本机登录 WorkBuddy 桌面端"})
        return 1

    domain = domain or DEFAULT_DOMAIN

    status_code, status_body = post(domain, STATUS_PATH, token)
    if status_code in (401, 403) or status_body.get("code") in (401, 403):
        emit({"status": "error", "action": "none", "time": now_cn(), "source": source,
              "msg": "令牌已失效（HTTP %s），请重新获取 accessToken 并更新 Secret" % status_code})
        return 1
    if status_body.get("code") != 0:
        emit({"status": "error", "action": "none", "time": now_cn(), "source": source,
              "msg": "状态查询失败：%s" % status_body.get("msg")})
        return 1

    data = status_body.get("data") or {}
    if data.get("today_checked_in"):
        emit({"status": "ok", "action": "skip_already_signed", "time": now_cn(),
              "source": source, "streak_days": data.get("streak_days"),
              "today_credit": data.get("today_credit"), "msg": "今日已签到，无需重复领取"})
        return 0

    claim_code, claim_body = post(domain, CLAIM_PATH, token)
    body_code = claim_body.get("code")
    if claim_code in (401, 403) or body_code in (401, 403):
        emit({"status": "error", "action": "claim", "time": now_cn(), "source": source,
              "msg": "领取时令牌失效（HTTP %s）" % claim_code})
        return 1
    if body_code == ALREADY_CLAIMED_CODE:
        emit({"status": "ok", "action": "skip_already_signed", "time": now_cn(), "source": source,
              "msg": claim_body.get("msg") or "今日已签到"})
        return 0
    if body_code != 0:
        emit({"status": "error", "action": "claim", "time": now_cn(), "source": source,
              "msg": "领取失败：%s" % (claim_body.get("msg") or claim_code)})
        return 1

    claim_data = claim_body.get("data") or {}
    emit({"status": "ok", "action": "claimed", "time": now_cn(), "source": source,
          "credit": claim_data.get("today_credit") or claim_data.get("credit"),
          "streak_days": claim_data.get("streak_days"),
          "msg": "领取成功：+%s 积分，连续 %s 天" % (
              claim_data.get("today_credit") or claim_data.get("credit"),
              claim_data.get("streak_days"))})
    return 0


if __name__ == "__main__":
    sys.exit(main())
