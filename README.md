# Codex Trading Agents

个人量化辅助与 AI 工具落地探索项目。项目围绕 A 股收盘扫描、候选筛选、规则化模拟交易计划、复盘健康监控、Hermes 策略管家和飞书推送搭建。

> 本项目不连接实盘交易接口，不提供确定收益或胜率承诺。所有输出仅用于个人模拟交易、复盘和工程研究。

## 核心能力

- watchlist 每日分析：抓取自选股行情与基本面快照，生成中文 Markdown 报告。
- 收盘扫描：基于全市场快照、历史 K 线、RPS、均线趋势、量能、波动率和市场环境筛选候选。
- 规则化计划：自动生成触发区间、止损、第一止盈、仓位和退出条件，用于模拟复盘。
- 台账与回测：维护 pending/open/archive 台账，输出事件级和组合级回测摘要。
- 因子研究：独立 shadow 模块评估现有因子、Alpha191 短周期子集、IC/RankIC、分组收益、相关性和消融实验。
- 策略健康监控：按 5/20/60 日窗口复核触发率、止损率、止盈率、标签表现和市场环境差异。
- Hermes 策略管家：只读诊断、反方审查、影子实验草案和学习记忆，不直接修改主策略。
- 多通道推送：支持飞书应用机器人、飞书 webhook、企业微信、Telegram、Slack、Discord、邮件等。

## 项目结构

2026-07-03 重构后，代码按「每日运行 / 研究 / 归档」三层隔离（详见 `docs/refactor_2026-07-03.md`）：

```text
├── main_v2.py                 # watchlist 每日报告入口
├── src/                       # 每日运行核心（唯一被定时任务依赖的代码）
│   ├── market_scanner.py      # 收盘扫描、模拟计划、台账、回测摘要
│   ├── scheduled_v3_reporter.py   # V3 日报（飞书推送用）
│   ├── forward_paper_trading_v3.py# 冻结 V3 forward paper 记录
│   ├── quant_core.py          # 共享量化库（缓存读取/可交易性/股票池/V3 过滤）
│   ├── data_fetcher / analyzer / reporter / notifier / synthesizer / selector
│   └── reviewer / strategy_health / strategy_learning
├── research/                  # 研究代码（已收敛，不在每日运行路径上）
│   ├── factor_research*.py    # 因子研究 round1-6 与冻结验证（历史轮次，互相依赖）
│   ├── backtest_v3_expanded.py / data_expansion_pipeline.py
│   ├── setup_based_short_swing.py / automated_setup_review.py
│   └── diagnose_* / short_horizon_* / strategy_discovery_shadow.py
├── archive/                   # 弃用入口与旧策略实现（仅回滚/对照用）
│   ├── market_scanner_legacy_v1.py
│   └── main.py / main_native_task.py
└── scripts/                   # shell 入口（推送、Hermes steward）
```

## 策略研究现状（重要）

截至 2026-07-03 的诚实结论：**当前所有已验证策略在 2021-2026 长样本中均为负收益，项目尚未找到可用 alpha。**

- 主策略 `alpha040_v3_risk_controlled` 长样本累计约 -9.89%（其价值在回撤控制，非收益）。
- setup-based 三类事件策略与全部退出实验均未转正，已收敛归档。
- 回测方法论经复核偏保守（次日触发进场、涨停禁买、跌停禁卖、双边费用滑点），负收益是真实研究结论，不是计算错误。
- 因此本项目当前定位是**研究与模拟复盘框架**：继续 forward paper trading 积累样本，不应向策略输出投入真实资金。

## 快速开始

建议使用 Python 3.12。当前 `pandas-ta` 0.4.x 在 PyPI 上要求 Python >= 3.12。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

把真实密钥写入本地 `.env` 或 shell 环境变量，不要提交到仓库。

```bash
export FUYAO_API_KEY="your-api-key"
export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="your-secret"
export FEISHU_RECEIVE_ID="your-chat-or-open-id"
```

## 常用命令

watchlist 每日报告：

```bash
python3 main_v2.py
python3 main_v2.py --push --push-dry-run
python3 main_v2.py --push
```

收盘扫描、模拟计划、回测和台账：

```bash
python3 src/market_scanner.py
python3 src/market_scanner.py --push --push-dry-run
python3 src/market_scanner.py --push
```

V3 日报与 forward paper trading（每日运行路径）：

```bash
python3 src/scheduled_v3_reporter.py
python3 src/forward_paper_trading_v3.py
```

研究模块（已收敛，仅在有新数据或新假设时重跑，不在每日路径上）：

```bash
python3 research/factor_research.py            # 因子 IC/分组/消融
python3 research/factor_research_round6.py     # V3 shadow 策略对比
python3 research/backtest_v3_expanded.py       # 2021 起扩展回测
python3 research/data_expansion_pipeline.py    # 历史数据扩展
python3 research/main_strategy_upgrade_v3.py   # 主策略升级审计
```

Hermes 策略管家：

```bash
scripts/run_strategy_steward.sh --date 2026-06-26 --dry-run
scripts/run_strategy_steward.sh --date 2026-06-26 --mode daily
scripts/run_strategy_steward.sh --date 2026-06-26 --mode critic
scripts/run_strategy_steward.sh --date 2026-06-26 --mode experiment
python3 src/strategy_learning.py --date 2026-06-26 --output output
```

## 输出目录

运行产物默认写入：

```text
output/YYYY-MM-DD/
data/ledger/
data/cache/
data/strategy_learning/
output/factor_research/
output/factor_research_round2/
output/factor_research_round3/
output/factor_research_round4/
output/factor_research_round5/
output/factor_research_round6/
output/factor_research_freeze_v3/
strategy_experiments/YYYY-MM-DD/
logs/
```

这些目录包含本地复盘、缓存、台账或生成报告，默认被 `.gitignore` 排除。上传 GitHub 前只提交源码、配置模板、文档和工作流。

## 配置入口

- `watchlist.json`：自选股列表。
- `strategy.json`：市场扫描、交易计划、回测、台账、健康监控和 Hermes 配置。
- `.env.example`：本地密钥和推送通道示例。
- `.github/workflows/`：GitHub Actions 定时扫描和基础 CI。
- `docs/factor_research.md`：因子研究与策略消融 shadow experiment 说明。
- `docs/project_status_report.md`：当前项目状态、策略升级、验证结果和后续计划。
- `docs/rollback_guide.md`：从 `alpha040_v3_risk_controlled` 回滚到 `legacy_momentum_v1` 的步骤。

## GitHub Actions

仓库默认使用一个定时推送工作流：

- `.github/workflows/daily-v3-feishu-push.yml`：北京时间工作日 19:10 自动生成 V3 日报并推送飞书。

另有两个手动备用工作流：

- `.github/workflows/daily-market-scan.yml`
- `.github/workflows/daily-analysis.yml`

在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions` 中配置需要的 Secrets，例如：

```text
FUYAO_API_KEY
FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_RECEIVE_ID
```

可用脚本把本机环境变量同步到 GitHub Secrets/Variables：

```bash
scripts/setup_github_secrets.sh
```

定时任务不会把 `output/`、`data/cache/`、`data/ledger/` 或 `logs/` 上传为 GitHub artifact，只在 runner 临时生成报告并推送摘要。

## 安全边界

- 数据失败必须明确标记 `partial` 或 `failed`，禁止补造数据。
- Hermes 只读诊断，不直接修改 `strategy.json` 或台账。
- 小样本优先不调参，实验必须先进入影子草案和回测验证。
- 报告中的触发区间、止损、止盈和仓位来自固定规则，仅用于个人模拟复盘。
