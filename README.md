# 黄金价格提醒 · gold-alert

每隔一段时间播报两个价格：**国际金价**（XAU/USD 现货）和 **民生银行积存金**（元/克），
给出与"上次检查"、"昨收"的涨跌对比，并支持**推送到手机**。

## 数据源

| 项目 | 来源 | 说明 |
| --- | --- | --- |
| 国际金价 | 新浪财经 伦敦金 `hq.sinajs.cn/list=hf_XAU` | 含真实成交时间、昨收、日内高低；失败时自动切 `api.gold-api.com/price/XAU` |
| 民生积存金 | 京东金融 `api.jdjygold.com/.../getGoldPrice?goldCode=CMBC-JCJ` | 返回现价、昨收、开高低、涨跌 |
| 美元人民币 | `open.er-api.com/v6/latest/USD` | 仅用于把美元/盎司折算成人民币/克，失败不影响主流程 |

## 用法

```bat
gold.bat                 :: 只看报价（不推送）
gold.bat --push          :: 报价 + 按策略推送到手机（默认推送精简版）
gold.bat --push-test     :: 发一条测试消息到手机
gold.bat --push-force    :: 忽略策略与额度，立即推一条
gold.bat --push-full     :: 推送完整长报告（默认只推精简版）
gold.bat --push-dry-run  :: 只预览推送标题与正文，不发送
gold.bat --json          :: JSON 输出
gold.bat --reset         :: 清空基线
```

## 手机推送（Server酱 / PushPlus / Bark / Telegram / Webhook）

配置在 `push_config.json`，用下面命令一键写入凭据并启用：

```bat
setup_push.bat serverchan SCTxxxxxxxxxxxx      :: 微信（Server酱）
setup_push.bat pushplus   <token>              :: 微信（PushPlus）
setup_push.bat bark       https://api.day.app/xxxx
setup_push.bat telegram   <bot_token> <chat_id>
setup_push.bat webhook    https://...          :: 钉钉/企业微信等自定义机器人
setup_push.bat email      smtp.qq.com 465 you@qq.com 授权码 [收件地址]   :: 邮件
setup_push.bat ntfy       <topic> [server]     :: ntfy（安卓横幅可显示全文）
```

然后测试：

```bat
gold.bat --push-test
```

输出 `[推送] 已通过 serverchan 发送` 即成功；`[推送] 失败：…` 会说明具体原因（如 SendKey 无效）。

### 推送策略与额度

`push_config.json` 关键项：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | false | 总开关，setup_push.bat 会自动置 true |
| `push_policy` | digest | `every` 每次运行都推 / `digest` 定时+告警 / `alerts_only` 仅告警 |
| `digest_times` | 09:00,11:30,15:00,20:00 | digest 模式下的固定播报时刻 |
| `max_per_day` | 5 | 每日推送上限（Server酱免费版约 5 条/天） |
| `min_gap_minutes` | 20 | 两条推送的最小间隔，避免多个触发源重复发 |
| `quiet_hours` | 23:30–08:00 | 免打扰时段 |

> Server酱免费版每天约 5 条。想每 30 分钟推一条（48 条/天），改用 PushPlus 或开通 Server酱会员后
> 把 `push_policy` 改成 `every`、`max_per_day` 调到 60。

### 两个触发源（可同时启用，靠 min_gap 去重）

1. **DSH 会话提醒**：工作日 08:00–23:30 每 30 分钟，产出聊天播报并调用 `--push`
2. **后台常驻循环**：DSH 关闭也照常运行
   - 双击 `安装开机自启_双击运行.bat` —— 装开机自启 + 立即启动
   - `start_gold_push.bat` / `stop_gold_push.bat` —— 手动启停
   - 日志：`state/push_loop.log`

> 后台循环从 DSH 沙箱内启动会被回收，所以自启/启动脚本需要**双击运行一次**（不在沙箱内）。

## 文件

- `gold_price.py` —— 抓取、对比与排版
- `pusher.py` —— 多通道推送、策略判断、额度控制
- `push_loop.py` —— 后台常驻循环
- `gold.bat` / `setup_push.bat` / `start_gold_push.bat` —— 启动器
- `push_config.json` —— 推送配置
- `state/gold_state.json` —— 上次快照与今日推送计数
- `state/gold_history.csv` —— 历史报价
- `state/push_loop.log` —— 后台循环日志

## 调整

- 波动提醒阈值：`gold_price.py` 顶部 `THRESH_*` 常量
- 播报频率/时段：DSH 定时提醒（cron `*/30 8-23 * * 1-5`）；后台循环见 `push_loop.py` 顶部常量

## 快速设置预警阈值

双击 `设置预警.bat` 打开交互菜单，或命令行直接改：

```bat
设置预警.bat                          交互菜单（推荐）
设置预警.bat list                     查看阈值 + 距现价百分比
设置预警.bat above 4250 4300          重设"涨破"阈值（覆盖原值）
设置预警.bat below 4100 4000          重设"跌破"阈值（覆盖原值）
设置预警.bat add above 4350           新增一档
设置预警.bat rm above 4250            删除一档
设置预警.bat clear                    清空阈值
设置预警.bat cmbc above 900           给民生积存金设阈值
```

改完自动 `git commit + push` 到 GitHub（用本机已缓存的 GitHub 凭据，不需要 token），
云端下一次运行（≤10 分钟）即生效。若本机凭据失效，改动会保留在本地并提示推送失败。

## 手机 App（PWA）

支持：金价走势图、提醒时间段、检查间隔、国际/民生双市场阈值、最近预警记录。
配置写在 `alerts.json` 的 `config` 字段：`window_start` / `window_end` / `interval_minutes`。

地址：**https://yabaozioffcial.github.io/gold-alert/**

- 手机浏览器打开 → 菜单「添加到主屏幕」，即可像 App 一样全屏使用
- 首次打开点右上角齿轮，粘贴 GitHub Token（勾选 `repo`；fine-grained 则给 Contents 读写），
  之后在 App 里加/删阈值、点「保存到云端」即可，云端 ≤10 分钟生效
- 只读不需要 Token：可直接查看当前阈值、当前金价与距现价百分比
- 源码在仓库 `docs/`（`index.html` / `manifest.webmanifest` / `sw.js` / `icon.svg`）