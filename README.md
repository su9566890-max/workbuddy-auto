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
| 成长中心全套 | 每 4 小时巡检（00:30 / 04:30 / 08:30 / 12:30 / 16:30 / 20:30） | 领旅行礼物、接单/领任务奖、补登、连登兑换、开盲盒、能量开 Buddy、派出旅行 |
| 每天对话一次 | 每天 09:05 | 免费模型 quick-answer 问一个简单问题，0 积分，保活跃登录 |
| 令牌到期哨兵 | 每周一 11:00 | 令牌剩不到 7 天就微信提醒 |

都是幂等：先查状态，未领才领，已领就跳过，不会重复领。写操作一律不重试。

## 必须保持 public

免费个人账号下，**私有仓库的 schedule 定时不会触发**。所以这个仓库必须是 public。
令牌不要放代码里，只放 GitHub Secret，代码里看不到。

## 部署（2 步，你在手机 GitHub 网页上就能做）

### 第 1 步：拿到 WB_TOKEN（最关键，也最卡人）

token 有效期 28 天，到期要重取一次。两种取法：

- **有电脑**：登录 WorkBuddy 桌面端后，用本仓库里的 `get-token.ps1`（在那台电脑上跑）：
```powershell
powershell -ExecutionPolicy Bypass -File .\get-token.ps1
```
它会把 `accessToken`（eyJ 开头，1000+ 字符）复制到剪贴板，屏幕上只显示前 6 位 + 到期时间，不会打出完整 token。
没有自己的电脑时：借一台电脑 5 分钟，装 WorkBuddy、登录、跑上面那条、复制 token、卸载就行。

- **没电脑**：让 Muse 在云端生成一个登录链接，手机上点开登录，Muse 再去取 token（2026-11-12 已验证走通过，用的就是这个办法）。

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

## 成长中心全套（已上线，2026-11-13）

`growth.py` + `growth.yml`，按 88lin/workbuddy-auto-signin 的 `run_growth` 逻辑改写（云端版，只用 `WB_TOKEN`，
不用本机登录文件、不用调本机客户端解密）。顺序：领旅行礼物 → 接单/领任务奖 → 补登（每轮最多 1 张）
→ 连登兑换（7/14/28 天三档）→ 开盲盒（一轮只开 1 次）→ 能量开 Buddy → 派出旅行。
首次手动运行 2026-11-13 08:21 成功：接了任务「分享一个技能给好友」（进度开始计）、Buddy 派去咖啡馆，
能量 8、成长中心连签 3 天，本次 0 积分、无失败、无推送（空跑不推是对的）。

## 每天对话一次（已上线，2026-11-24）

`chat.py` + `chat.yml`，每天 09:05（北京）调一次中国版聊天接口问一个简单问题
（每天换一个，1+1、星期几这种），只为保住成长中心的「活跃登录」——光签到 +100
不一定能点亮当天。模型固定用 `quick-answer`：它在配置里的 credits 是 `x0.00`，
2026-11-24 实测问 1+1 返回“等于 2”，usage 里 credit=0，一分钱不花。
不要换别的模型：deepseek-v4.1-flash 是 x0.11、glm-5.3-flash 是 x0.08，都会扣额度。
接口是 `POST https://www.codebuddy.cn/v2/chat/completions`，stream=true，
第一条消息必须是 system，另需三个 fresh-UUID 头
（X-Conversation-ID / X-Conversation-Request-ID / X-Conversation-Message-ID）。
成功不推送，只在出错时推。手动运行 #1 于 2026-11-24 11:48 成功：问“太阳从哪边升起？”、credit=0。
待验证：中国版是否认接口聊天来算活跃登录，看 2026-11-25 的活跃日历再定论。

## 国际版（workbuddy.ai）—— 已上线每日活跃，2026-11-24

中国版和国际版账号不互通，token 也不通用。本仓库中国版脚本只针对 `www.codebuddy.cn`；
国际版用单独的 `workbuddy_intl_daily.py` + `intl.yml`（每天 09:15 北京时间），
Secret 是 `WORKBUDDY_INTL_TOKENS`，格式必须是四段 `备注|token|uid|refresh_token`
（token 约 365 天有效，带 refresh_token 可自动续期；三段会把 uid 当 token 用，直接 401）。

脚本来源：L0NE-6/WorkBuddy-Daily（MIT）的 `workbuddy_intl_daily.py`，
本地改了一处：网页对话前补一条 system 消息（上游接口要求首条必须是 system，
否则 HTTP 400 code=11128）。

2026-11-24 手动运行 #3（12:11–12:13）结果：

- **每日活跃已通**：活跃上报成功，连登 1 天；保活对话（deepseek-v4.1-flash）成功；
  网页会话已创建。国际版没有每日 +100 签到活动（探测显示“活动已关闭”），
  连登靠活跃上报 + 网页会话点亮。
- **5 个新手任务**：4 个的动作都真实执行并上报了事件（对话 6 条、装技能 minimax-pdf 7 条、
  召唤专家“品牌短片”9 条、用模板 Slides Creation 4 条），但服务端状态回读仍是 available，
  脚本不冒领、不 claim。国际版分数是隔夜结算的（11-14 的 4 分就是第二天才出现的）。
- **链接微信**：脚本不伪造，需在客户端手动绑定。

2026-11-28 22:30 运行 #8 已确认：4 个任务**已领取**，连登 5 天——隔夜结算后自动领到了。
微信提醒也已接上（2026-11-28）：脚本补了 Server 酱发送（与国内版同一个微信），
工作流去掉了 `NO_NOTIFY=1`、接上了仓库已有的 `SERVERCHAN_KEY`；每天跑完推一条。
注意：运行日志里不带积分/credit/能量的具体数字（活动未开时不发新积分，日志只写
“积分由上游异步发放”），要准确余额得在 workbuddy.ai 成长中心看，或重新探账号。

更早的探查结论（2026-11-13 / 11-14，账号昵称 su su）供参考：

- **签到没有**：国际版签到接口两个地址都是 404，没有每日 +100 那个活动。
- **成长中心接口是通的**（tasks / streak / redeem / lottery / quota / energy / travel 全是 200），
  但账号还是新的：连签 1 天、兑换全锁着、能量 0、Buddy 名额 0、抽奖 0 次、旅行是空的（还没 Buddy）。
- **光登录不送积分**：2026-11-14 当天重新登录 + 调接口，热力墙当天还是 0 分、未点亮
  （13 号那天 16 分是实际使用攒的，不能重复刷）。
- **API 问 1+1 也不算使用**：2026-11-14 实测用国际版 token 调聊天接口问 `1+1=?`，成功返回 `1+1=2`、
  用量里 credit 0（没扣额度），但 2 分钟后再查：当天热力墙还是 0 分、`完成一次对话` 任务还是 available、
  连签还是 1 天、能量还是 0。也就是说接口聊天不计入成长中心的“使用”，点亮热力墙得在 App/网页里真的用。
- **分数是隔夜结算的（2026-11-15 复查确认）**：第二天再查，2026-11-14 那天变成了 4 分，连签变成 2 天。
  所以当天查 0 分不代表没算，分数是隔夜才结算的。但 `完成一次对话` 任务第二天还是 available，
  那 4 分到底是登录、CLI 问答还是网页对话给的，分不出来。
- **网页版对话接口也试过**：网页版自己的代码显示它走 `POST /console/as/conversations/`（创建任务对话）
  加沙箱会话，不是 CLI 那条 `/console/chat/completions`。2026-11-14 用这条接口建了个对话“1+1等于几？”，
  它卡在 working 状态十几分钟没动，最后自己在 13:54 跑完了，前后花了快 2 小时——网页版 agent 就是这么慢，
  不是卡死。但跑完之后 `完成一次对话` 还是没变。
- **5 个新手任务要真做事**：完成一次对话、完成技能安装、链接微信、召唤一次专家、使用一次模板，
  接口上代做不了；而且任务里没写奖励数额。
- 连签奖励本身是有的（7 天档 +2 能量 +1 补登卡 +1 抽奖，14 天 +50 积分，28 天 +150 积分），
  但前提是连续 7 天每天都真的用，不是登一下就行。

## 风险

非官方工具，接口是桌面端逆向来的，腾讯改版就可能失效。用自己的账号、自己的接口，遵守 WorkBuddy 服务条款。
