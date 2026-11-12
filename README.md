# WorkBuddy 自动签到 + 猫猫旅行（GitHub Actions 云端版）

电脑关机也能跑。每天定时调 WorkBuddy 官方接口，不用开电脑、不用一直挂客户端。

来源：基于 liuxin19930310/workbuddy（MIT 思路，实测接口）改的个人版，
接口：`POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status`
和 `POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin`，
猫猫旅行：`GET/POST /activity/growth/buddy/travel/*`。

## 能自动做什么

| 任务 | 时间（北京） | 奖励 |
|---|---|---|
| Buddy 加油站签到 | 每天 00:05、09:00、12:00、15:00、18:00、21:00 巡检 | +100 积分/天，连签有额外奖励 |
| 派猫猫旅行 | 08:15 派出，12:30 / 16:45 / 21:00 巡检领取 | 归来领 5~10 积分，每天限 1 次 |
| 令牌到期哨兵 | 每周一 11:00 | 令牌剩不到 7 天就微信提醒 |

都是幂等：先查状态，未领才领，已领就跳过，不会重复领。

## 必须保持 public

免费个人账号下，**私有仓库的 schedule 定时不会触发**。所以这个仓库必须是 public。
令牌不要放代码里，只放 GitHub Secret，代码里看不到。

## 部署（2 步，你在手机 GitHub 网页上就能做）

### 第 1 步：拿到 WB_TOKEN（最关键，也最卡人）

这个 token 只能从一台登录过 WorkBuddy 桌面端的电脑上取，手机上取不到：

Windows 电脑上，登录 WorkBuddy 后，文件在：
`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`

用本仓库里的 `get-token.ps1`（在那台电脑上跑）：
```powershell
powershell -ExecutionPolicy Bypass -File .\get-token.ps1
```
它会把 `accessToken`（eyJ 开头，1000+ 字符）复制到剪贴板，屏幕上只显示前 6 位 + 到期时间，不会打出完整 token。

没有自己的电脑时：借一台电脑 5 分钟，装 WorkBuddy、登录、跑上面那条、复制 token、卸载就行。token 有效期 28 天，refreshToken 90 天，到期要重取一次。

**不要把完整 token 发聊天里、发群里、贴仓库里。**

### 第 2 步：填 Secret

仓库页面 → Settings → Secrets and variables → Actions → New repository secret

| 名称 | 值 |
|---|---|
| `WB_TOKEN` | 第 1 步复制的 accessToken（必填，名字必须一字不差） |
| `SERVERCHAN_KEY` | 可选，微信推送。去 sct.ftqq.com 微信扫码登录，复制 SCT 开头的 SendKey。不填也能跑，只是不推送 |

### 第 3 步：验证

Actions → WorkBuddy Daily Checkin → Run workflow → 手动跑一次。
日志里 `action=claimed` 是领到了，`skip_already_signed` 是今天已领过，都正常。
如果报 `令牌已失效`，就是 WB_TOKEN 过期或贴错了，重取一次。

## 维护

- **每 28 天换一次 WB_TOKEN**。token-watch 每周一会查，剩不到 7 天就微信提醒你。
- GitHub 原生定时是 best-effort，偶尔会晚 2 分钟到 3 小时，所以设了多个时点兜底，领到一次就行。
- 想更安静：把 workflow 里的 `PUSH_LEVEL` 改成 `off`，并删掉 SERVERCHAN_KEY。
- 仓库里 `.keepalive/last-run-*.txt` 是每天的运行账本，有今天的戳就说明今天跑过。

## 没覆盖的

成长中心的全套任务（领新任务、领任务奖、开盲盒、连登兑换）在 88lin/workbuddy-auto-signin 里有，
但那个版本要靠本机客户端解密登录态，云端只靠 WB_TOKEN 跑不起来，所以这里先只做签到 + 猫猫旅行。
等你拿到 token、签到跑通了，想加再说。

## 风险

非官方工具，接口是桌面端逆向来的，腾讯改版就可能失效。用自己的账号、自己的接口，遵守 WorkBuddy 服务条款。
