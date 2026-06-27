# 每日推送配置

本项目的每日推送采用环境变量配置，不在仓库中保存 token、webhook 或邮箱授权码。

## 本地运行

### 推送收盘扫描报告

```bash
python3 src/market_scanner.py --push --push-dry-run
python3 src/market_scanner.py --push
```

默认推送摘要版 `output/YYYY-MM-DD/scan/scan_report.md`。若希望推送完整 Markdown：

```bash
python3 src/market_scanner.py --push --push-mode full
```

也可以只推送已有扫描报告：

```bash
PYTHONPATH=src python3 -m notifier --report output/2026-06-25/scan/scan_report.md --dry-run
PYTHONPATH=src python3 -m notifier --report output/2026-06-25/scan/scan_report.md
```

每次 `market_scanner.py --push` 后会写入：

```text
output/YYYY-MM-DD/scan/push_summary.json
```

### 推送 watchlist 每日报告

```bash
python3 main_v2.py --push --push-dry-run
python3 main_v2.py --push
```

默认推送摘要版报告。若希望推送完整 Markdown：

```bash
python3 main_v2.py --push --push-mode full
```

也可以只推送已有报告：

```bash
PYTHONPATH=src python3 -m notifier --report output/2026-06-25/report.md --dry-run
PYTHONPATH=src python3 -m notifier --report output/2026-06-25/report.md
```

## 支持的通道

至少配置一个通道即可，多通道会全部发送。

| 通道 | 环境变量 |
|------|----------|
| 企业微信机器人 | `WECHAT_WEBHOOK_URL` |
| 飞书群机器人 Webhook | `FEISHU_WEBHOOK_URL`，可选 `FEISHU_WEBHOOK_SECRET` |
| 飞书应用机器人 API | `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_RECEIVE_ID`，可选 `FEISHU_RECEIVE_ID_TYPE`、`FEISHU_APP_MESSAGE_FORMAT` |
| Telegram | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，可选 `TELEGRAM_MESSAGE_THREAD_ID` |
| Slack Incoming Webhook | `SLACK_WEBHOOK_URL` |
| Discord Webhook | `DISCORD_WEBHOOK_URL` |
| 自定义 Webhook | `CUSTOM_WEBHOOK_URL`，可选 `CUSTOM_WEBHOOK_BEARER_TOKEN`、`CUSTOM_WEBHOOK_BODY_TEMPLATE` |
| ntfy | `NTFY_URL`，可选 `NTFY_TOKEN` |
| Gotify | `GOTIFY_URL`、`GOTIFY_TOKEN`，可选 `GOTIFY_PRIORITY` |
| PushPlus | `PUSHPLUS_TOKEN`，可选 `PUSHPLUS_TOPIC` |
| ServerChan | `SERVERCHAN3_SENDKEY` 或 `SERVERCHAN_SENDKEY` |
| SMTP 邮件 | `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`EMAIL_FROM`、`EMAIL_TO` |

`CUSTOM_WEBHOOK_BODY_TEMPLATE` 必须是合法 JSON 字符串，可使用 `{title}`、`{content}`、`{report_path}` 占位符。

## 飞书两种方式

飞书默认使用 `post` 富文本格式推送摘要，会把 Markdown 表格转换成更适合聊天窗口阅读的分段文本；如果某个机器人环境不兼容，可设置：

```bash
FEISHU_MESSAGE_FORMAT=text
```

也可以分别控制两种飞书通道：

```bash
FEISHU_APP_MESSAGE_FORMAT=post
FEISHU_WEBHOOK_MESSAGE_FORMAT=post
```

### 方式 A：群聊自定义机器人 Webhook

这是最简单的方式：在飞书客户端目标群里添加自定义机器人，复制 Webhook 到 `FEISHU_WEBHOOK_URL`。如果开启签名校验，再配置 `FEISHU_WEBHOOK_SECRET`。

### 方式 B：开放平台应用机器人 API

适合已经创建了飞书开放平台应用的情况。当前 `claw` 应用已启用机器人能力，并且 `im:message` 权限处于已开通状态。需要配置：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=你的应用密钥
FEISHU_RECEIVE_ID=目标用户或群聊 ID
FEISHU_RECEIVE_ID_TYPE=chat_id   # 默认 chat_id，也可用 open_id、union_id、user_id、email
```

运行：

```bash
FEISHU_APP_ID="cli_xxx" \
FEISHU_APP_SECRET="..." \
FEISHU_RECEIVE_ID="..." \
python3 main_v2.py --push --push-dry-run
```

真实发送时去掉 `--push-dry-run`。不要把 `FEISHU_APP_SECRET` 写入代码或提交到仓库；放到本机 shell 环境变量或 GitHub Actions Secrets。

如果本机 Python 报 `CERTIFICATE_VERIFY_FAILED`，请确认已安装依赖中的 `certifi`。`notifier.py` 会优先使用 `certifi` CA 包进行 HTTPS 校验。

## GitHub Actions

项目内有两个工作流模板：

- `.github/workflows/daily-market-scan.yml`：收盘扫描、候选、回测、台账与推送
- `.github/workflows/daily-analysis.yml`：watchlist 每日分析报告与推送

- `schedule`：北京时间交易日 18:30 运行
- `workflow_dispatch`：支持手动触发
- 使用仓库 Secrets/Variables 注入推送配置

在 GitHub 仓库中进入 `Settings -> Secrets and variables -> Actions`，按需添加上述环境变量即可。

收盘扫描工作流还需要：

| 变量 | 用途 |
|------|------|
| `FUYAO_API_KEY` | 扶摇行情 API Key，放在 Actions Secrets |

每次执行 `--push` 后会写入 `push_summary.json`，记录通道、状态和响应摘要。GitHub Actions 默认使用 `--push-fail-on-error`，如果某个已配置通道真实发送失败，工作流会失败；`dry-run` 不会触发失败。

## 安全边界

- 推送内容仍包含免责声明，定位为研究性数据分析，不构成投资建议。
- 不连接实盘交易接口。
- 数据源失败时会在报告中显示 `partial` 或 `failed`，不会补造数据。
