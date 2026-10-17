# gold-alert（云端版）

黄金价格播报：抓取 **国际金价**（XAU/USD）和 **民生银行积存金**（元/克），
按北京时间 09:00 / 11:30 / 15:00 / 20:00 推送到手机（ntfy）。

## 特点

- 纯 Python 标准库，无依赖
- 无需数据库：涨跌用接口自带的"昨收"计算
- 周末自动跳过
- 国际金价主源新浪，失败自动切 gold-api.com
- 密钥走 GitHub Secrets，不进代码

## 部署

1. 上传本仓库到 GitHub（Private 也可以）
2. Settings → Secrets and variables → Actions → New repository secret
   - `NTFY_TOPIC` = 你的 ntfy 主题
   - （可选）变量 `NTFY_SERVER` = `https://ntfy.sh`
3. Actions → gold-alert → Run workflow 手动验证
4. 手机 ntfy 订阅同一主题

## 定时

| 北京时间 | cron (UTC) |
| --- | --- |
| 09:00 | `0 1 * * 1-5` |
| 11:30 | `30 3 * * 1-5` |
| 15:00 | `0 7 * * 1-5` |
| 20:00 | `0 12 * * 1-5` |

GitHub cron 可能延迟 0–15 分钟，手动触发可随时补发。
