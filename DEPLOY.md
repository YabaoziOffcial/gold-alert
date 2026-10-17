# 迁移部署清单（5 分钟）

## 已完成（我这边的部分）

- 云端脚本 `cloud/gold_alert_cloud.py`（纯标准库，已实测推送成功）
- GitHub Actions 定时任务 `.github/workflows/gold-alert.yml`（北京时间 09:00/11:30/15:00/20:00）
- 本地 git 仓库已初始化并提交（commit 92bb1fc）

## 你需要做的三步

### 1. 在 GitHub 建一个空仓库

- 打开 https://github.com/new
- Repository name 随意，比如 `gold-alert`
- **Private 也可以**
- **不要勾选** Add a README / .gitignore / license（否则推送会冲突）

### 2. 把仓库地址发我，或者自己推送

把仓库 HTTPS 地址（形如 `https://github.com/你的用户名/gold-alert.git`）发给我，我直接从这里推上去。

也可以双击运行：
```bat
push_to_github.bat https://github.com/你的用户名/gold-alert.git
```
推送时若弹出 GitHub 登录窗口，授权即可。

### 3. 配置 ntfy 主题（仓库页面操作）

仓库 → Settings → Secrets and variables → **Actions** → New repository secret：

- Name: `NTFY_TOPIC`
- Secret: `gold-alert-sd-7f3k9x2q`

然后在 **Actions** 标签页 → 左侧 `gold-alert` → 右侧 **Run workflow** 手动跑一次，日志出现 `已推送` 即成功。

## 验证要点

- 日志里 `已推送: 国际 ... / 民生 ...` 表示两个数据源都成功
- 手机 ntfy 订阅同一主题，应收到通知
- GitHub cron 有 0–15 分钟延迟，属正常
