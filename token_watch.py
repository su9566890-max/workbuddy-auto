#!/usr/bin/env python3
"""WB_TOKEN 到期哨兵（GitHub Actions 侧兜底）。

背景：本机有一个「每 14 天」的 WorkBuddy 自动化负责自动刷新仓库 Secret
（见 skill `workbuddy-secret-refresh`）。但它依赖「到点那台电脑开着、客户端在跑」——
电脑关着的那一天就白白错过一次。

本脚本跑在 GitHub Actions，**不依赖任何本机资源**，只做一件事：
读出 Secret 里那把令牌的 `exp`，快到期就用微信喊你手动跑一次刷新。
它不写任何东西（permissions 只读），也不碰仓库文件。

判定分三级：
  days_left <= 0      -> expired  （签到/猫猫旅行必然已经在报 401）
  days_left <  阈值    -> soon     （即将到期）
  其余                 -> ok       （静默，除非 FORCE_NOTIFY）

退出码：expired -> 1（run 变红，形成可见信号）；其余 -> 0。

环境变量
  WB_TOKEN          仓库 Secret，必填
  TOKEN_WARN_DAYS   提前几天提醒，默认 7
  NOTIFY_CHANNEL    serverchan（默认）/ bark / off
  SERVERCHAN_KEY / BARK_KEY / BARK_*  与 checkin.py 同义
  PUSH_LEVEL        action（默认）/ all / off
  FORCE_NOTIFY      1/true/yes 时无条件推一条（验证推送配置用）
  GITHUB_ACTIONS    true 时输出 GitHub 注解
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
DEFAULT_WARN_DAYS = 7


def now_cn():
    return datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S +08")


def fmt_dt(ts):
    try:
        return datetime.fromtimestamp(int(ts), CN).strftime("%Y-%m-%d %H:%M +08")
    except Exception:
        return None


# ---------------------------------------------------------------- JWT

def decode_jwt_payload(tok):
    """解出 JWT 的 payload。**不校验签名** —— 只需要读 exp，验签没有意义。
    明文令牌只在本函数内出现，绝不打印。
    """
    parts = tok.split(".")
    if len(parts) < 2:
        raise ValueError("不是 JWT 形态（点号分段数 %d）" % len(parts))
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


# ---------------------------------------------------------------- 推送
# 与 checkin.py / travel.py 保持同一套环境变量与返回格式，便于共用 Secret。

def sendkey_problem(key):
    if not key:
        return "未配置 SERVERCHAN_KEY（Secret 缺失或为空）"
    if key.startswith("eyJ"):
        return ("SERVERCHAN_KEY 疑似被填成了 accessToken（JWT）—— "
                "WB_TOKEN 才填 eyJ 开头的令牌，SERVERCHAN_KEY 应为 SCT 开头的 SendKey")
    if not key.upper().startswith("SCT"):
        return "SERVERCHAN_KEY 不像 Server 酱 SendKey（应以 SCT 开头，实际前缀 %s...）" % key[:4]
    return None


def _send_serverchan(title, content):
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
    if body.get("code") == 0:
        return "ok: pushid=%s" % ((body.get("data") or {}).get("pushid"))
    return "failed: code=%s msg=%s" % (body.get("code"), body.get("message"))


def _bark_target():
    raw = os.environ.get("BARK_KEY", "").strip()
    if not raw:
        return None, None, "未配置 BARK_KEY（Secret 缺失或为空）"
    server = os.environ.get("BARK_SERVER", "").strip() or "https://api.day.app"
    key = raw
    if raw[:7] == "http://" or raw[:8] == "https://":
        parsed = urllib.parse.urlparse(raw)
        segs = [s for s in parsed.path.split("/") if s]
        if not segs:
            return None, None, "BARK_KEY 是 URL 但解析不出 key"
        server, key = "%s://%s" % (parsed.scheme, parsed.netloc), segs[0]
    if len(key) < 12 or not re.fullmatch(r"[A-Za-z0-9_\-]+", key):
        return None, None, "BARK_KEY 形态不对（长度 %d）" % len(key)
    return server, key, None


def _send_bark(title, content):
    server, key, problem = _bark_target()
    if problem:
        return "skipped: " + problem
    payload = {
        "title": title,
        "body": content,
        "group": os.environ.get("BARK_GROUP", "workbuddy").strip() or "workbuddy",
        "level": os.environ.get("BARK_LEVEL", "active").strip() or "active",
    }
    req = urllib.request.Request(
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


CHANNELS = {"serverchan": _send_serverchan, "bark": _send_bark}
NOTIFY_CHANNEL = os.environ.get("NOTIFY_CHANNEL", "serverchan").strip().lower()


def notify(title, content):
    if NOTIFY_CHANNEL == "off":
        return "skipped: [off] 已关闭推送"
    sender = CHANNELS.get(NOTIFY_CHANNEL)
    if sender is None:
        return "skipped: [%s] 未知渠道（可用：%s）" % (
            NOTIFY_CHANNEL, " / ".join(sorted(CHANNELS)))
    try:
        status = sender(title, content)
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "ignore")
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
    print("[push] %s" % status)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        level = "notice" if status.startswith("ok") else "warning"
        print("::%s::notify(%s) -> %s" % (level, NOTIFY_CHANNEL, status.replace("\n", " ")))


# ---------------------------------------------------------------- 结果

def emit(result):
    print(json.dumps(result, ensure_ascii=False))


def annotate(result):
    """把结论打进 GitHub 注解，让 run 摘要页不点日志也能看懂。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    level = result.get("level")
    msg = result.get("msg", "")
    if level == "expired":
        print("::error::WB_TOKEN 已过期 —— %s" % msg)
    elif level == "soon":
        print("::warning::WB_TOKEN 即将到期 —— %s" % msg)
    elif level == "ok" and result.get("action") == "notify":
        print("::notice::%s" % msg)
    elif level == "ok":
        print("::notice::WB_TOKEN 正常，剩余 %.1f 天（阈值 %s 天）" % (
            result.get("days_left", 0), result.get("threshold_days")))


def build_push(result):
    """返回 (title, content)。"""
    forced = result.get("forced")
    label = os.environ.get("WB_LABEL", "").strip()
    prefix = ("【%s】" % label if label else "") + ("（测试）" if forced else "")
    days = result.get("days_left")
    exp = result.get("token_exp", "未知")
    if result.get("level") == "expired":
        title = prefix + "WB_TOKEN 已过期，签到与猫猫旅行正在失败"
        body = ("> 令牌已于 **%s** 过期（%s）。\n\n"
                "在刷新之前，`checkin` 与 `travel` 两个工作流都会报 HTTP 401。\n\n"
                "**处置**：在本机跑一次刷新（电脑开着即可，无需开 WorkBuddy 主窗口）：\n\n"
                "```\nC:\\Users\\Liuxin\\.workbuddy\\binaries\\python\\envs\\wbsecret\\Scripts\\python.exe "
                "C:\\Users\\Liuxin\\.workbuddy\\skills\\workbuddy-secret-refresh\\scripts\\set_wb_secret.py\n```\n\n"
                "或在 WorkBuddy 里说一句「刷新 WB_TOKEN」。") % (
            exp, result.get("overdue", "已过期"))
    elif result.get("level") == "soon":
        title = prefix + "WB_TOKEN 还有 %s 天到期，记得刷新" % (
            ("%.0f" % days) if days is not None else "?")
        body = ("> 到期时间：**%s**，剩余 **%.1f 天**（阈值 %s 天）。\n\n"
                "本机有定期自动刷新 Secret 的自动化，正常情况你不该收到这条 —— 收到说明它没生效\n"
                "（多半是到点电脑没开，或客户端没运行）。\n\n"
                "**处置**：在本机跑一次刷新，或直接等下一个自动刷新窗口。\n\n"
                "```\nC:\\Users\\Liuxin\\.workbuddy\\binaries\\python\\envs\\wbsecret\\Scripts\\python.exe "
                "C:\\Users\\Liuxin\\.workbuddy\\skills\\workbuddy-secret-refresh\\scripts\\set_wb_secret.py\n```") % (
            exp, days, result.get("threshold_days"))
    else:
        title = prefix + "WB_TOKEN 到期哨兵（连通性测试）"
        body = ("令牌正常：到期 **%s**，剩余 **%.1f 天**（阈值 %s 天）。\n\n"
                "收到这条是因为手动触发了 `force_notify`，用于验证推送链路是否通畅。") % (
            exp, days if days is not None else -1, result.get("threshold_days"))
    return title, body


def should_push(result):
    if os.environ.get("FORCE_NOTIFY", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("PUSH_LEVEL", "action").strip().lower() == "off":
        return False
    return result.get("level") in ("expired", "soon")


# ---------------------------------------------------------------- main

def main():
    raw_token = os.environ.get("WB_TOKEN", "").strip()
    try:
        warn_days = float(os.environ.get("TOKEN_WARN_DAYS", "") or DEFAULT_WARN_DAYS)
    except ValueError:
        warn_days = float(DEFAULT_WARN_DAYS)

    result = {"time": now_cn(), "threshold_days": warn_days}

    if not raw_token:
        result.update(status="error", level="expired", days_left=None, action="notify",
                      msg="未取到 WB_TOKEN（Secret 缺失或为空）—— checkin / travel 必然失败")
        finish(result)
        return 1

    try:
        payload = decode_jwt_payload(raw_token)
    except Exception as err:
        result.update(status="error", level="expired", days_left=None, action="notify",
                      msg="WB_TOKEN 无法解析为 JWT（%s: %s）—— Secret 可能被填成了别的值"
                          % (type(err).__name__, err))
        finish(result)
        return 1

    exp = payload.get("exp")
    if not exp:
        result.update(status="error", level="expired", days_left=None, action="notify",
                      msg="JWT 里没有 exp 字段，无法判断到期时间")
        finish(result)
        return 1

    exp_dt = datetime.fromtimestamp(int(exp), CN)
    days_left = (exp_dt - datetime.now(CN)).total_seconds() / 86400.0
    result["token_iat"] = fmt_dt(payload.get("iat"))
    result["token_exp"] = exp_dt.strftime("%Y-%m-%d %H:%M +08")
    result["days_left"] = round(days_left, 2)

    if days_left <= 0:
        result["level"] = "expired"
        result["status"] = "error"
        result["overdue"] = "已过期 %s" % fmt_delta(-days_left)
        result["msg"] = "令牌已于 %s 过期" % result["token_exp"]
    elif days_left < warn_days:
        result["level"] = "soon"
        result["status"] = "warn"
        result["msg"] = "令牌将于 %s 到期，剩余 %.1f 天" % (result["token_exp"], days_left)
    else:
        result["level"] = "ok"
        result["status"] = "ok"
        result["msg"] = "令牌正常，剩余 %.1f 天" % days_left

    finish(result)
    return 1 if result["level"] == "expired" else 0


def fmt_delta(days):
    total_min = int(days * 24 * 60)
    h, m = divmod(total_min, 60)
    if h >= 24:
        return "%d 天 %d 小时" % (h // 24, h % 24)
    return "%d 小时 %d 分" % (h, m)


def finish(result):
    forced = os.environ.get("FORCE_NOTIFY", "").strip().lower() in ("1", "true", "yes")
    result["forced"] = forced
    push = should_push(result)
    result["action"] = "notify" if push else "none"
    if push:
        title, body = build_push(result)
        status = notify(title, body)
        result["push"] = status
        report_push(status)
    emit(result)
    annotate(result)


if __name__ == "__main__":
    sys.exit(main())
