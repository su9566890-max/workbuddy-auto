#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 成长中心只读探针（临时验证用，不会写入任何东西）。

只发 GET 请求，绝不调用领奖 / 开盒 / 兑换 / 补登 / 派出这些写接口。
用来验证：云端 Secret 里的 WB_TOKEN 能不能调通 88lin 版本的成长中心接口。

用法（GitHub Actions 内）：
    WB_TOKEN=<secret> python3 growth_probe.py

输出：每行一个 JSON，含 endpoint、http 状态、业务 code、数据摘要。
全程不打印、不落盘任何令牌内容；只从 JWT 里解出 sub 当 X-User-Id（不验签）。
退出码：0 = 至少一个接口通；1 = 全部失败 / 令牌无效。
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

DOMAIN = os.environ.get("WB_DOMAIN", "www.codebuddy.cn").strip() or "www.codebuddy.cn"
BASE = "https://" + DOMAIN + "/v2/activity/growth"

# 只读 GET 端点（照 88lin/signin.py run_growth 里的查询接口抄的）
PROBES = [
    ("tasks", "/tasks"),
    ("streak", "/streak"),
    ("redeem_summary", "/redeem/summary"),
    ("lottery_chances", "/lottery/chances"),
    ("buddy_quota", "/buddy/quota"),
    ("energy", "/energy"),
    ("travel_status", "/buddy/travel/status"),
    ("travel_config", "/buddy/travel/config"),
]

TIMEOUT = 25


def jwt_sub(token):
    """从 JWT payload 里读 sub（只解 base64，不验签），当 X-User-Id 用。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode()).get("sub", "")
    except Exception:
        return ""


def summarize(body, limit=400):
    """只留摘要：code/msg 和关键计数，不把整包数据都打出来。"""
    if not isinstance(body, dict):
        return {"raw": str(body)[:limit]}
    data = body.get("data")
    summary = {"code": body.get("code"), "msg": body.get("msg", "")}
    if isinstance(data, dict):
        # 只挑几个能判断状态的字段
        for key in ("tasks", "days", "streak_days", "tiers", "chances",
                    "balance", "makeup_cards", "state", "daily_limit_reached"):
            if key in data:
                value = data[key]
                if isinstance(value, list):
                    summary[key] = "list[%d]" % len(value)
                elif isinstance(value, dict):
                    summary[key] = {k: v for k, v in list(value.items())[:5]}
                else:
                    summary[key] = value
        if not summary.keys() - {"code", "msg"}:
            summary["data_keys"] = list(data.keys())[:10]
    elif isinstance(data, list):
        summary["data"] = "list[%d]" % len(data)
    return summary


def main():
    token = os.environ.get("WB_TOKEN", "").strip()
    if not token or len(token) < 20:
        print(json.dumps({"probe": "setup", "ok": False,
                          "error": "未找到 WB_TOKEN"}, ensure_ascii=False))
        return 1
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

    ok_count = 0
    for name, path in PROBES:
        url = BASE + path
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = json.loads(resp.read().decode() or "{}")
                http = resp.status
        except urllib.error.HTTPError as exc:
            http = exc.code
            try:
                body = json.loads(exc.read().decode() or "{}")
            except Exception:
                body = {}
        except Exception as exc:
            print(json.dumps({"probe": name, "ok": False,
                              "error": "network: %s" % exc}, ensure_ascii=False))
            continue
        good = 200 <= http < 300 and body.get("code", 0) == 0
        ok_count += 1 if good else 0
        print(json.dumps({"probe": name, "ok": good, "http": http,
                          "summary": summarize(body)}, ensure_ascii=False))
    print(json.dumps({"probe": "done", "ok_endpoints": ok_count,
                      "total": len(PROBES)}, ensure_ascii=False))
    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
