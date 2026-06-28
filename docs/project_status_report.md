# Codex Trading Agents 项目情况报告

生成日期：2026-06-28

## 1. 项目定位

Codex Trading Agents 是一个个人量化辅助与 AI 工作流探索项目，用于 A 股收盘扫描、候选筛选、模拟交易计划、策略复盘、因子研究和飞书推送。

项目不连接实盘交易接口，不自动下单，不提供确定收益或胜率承诺。所有报告、触发区间、止损、止盈和仓位参数均用于个人模拟交易、复盘和工程研究。

## 2. 当前主策略状态

当前默认主策略已经升级为：

- active strategy: `alpha040_v3_risk_controlled`
- 冻结来源: `v3_atr_risk_budget_hot5_vol_risk_on`
- 旧策略保留: `legacy_momentum_v1`
- 回滚配置: `strategy_legacy_v1.json`
- 回滚入口: `src/market_scanner_legacy_v1.py`
- 回滚说明: `docs/rollback_guide.md`

新主策略固定规则：

- 主排序因子为 `alpha040`
- 辅助因子为 `rps60`、`close_to_20d_high`
- 只在积极市场环境允许新开仓
- `5日涨幅 > 20.39%` 剔除
- `volatility_20d > 4.98%` 剔除
- 使用 ATR 止损和风险预算仓位
- 趋势只做弱过滤
- 上影线 `upper_shadow_ratio` 只做标签，不做硬过滤
- 中性、谨慎、防守环境禁止新开仓
- 不连接实盘，不自动下单

## 3. 升级验证结果

主策略升级审计输出位于：

- `output/main_strategy_upgrade_v3/main_strategy_upgrade_v3_report.md`
- `output/main_strategy_upgrade_v3/upgrade_comparison.csv`
- `output/main_strategy_upgrade_v3/v3_reproducibility_check.csv`

当前验证结论：

- 升级状态：`PASSED`
- Freeze V3 复跑可复现：`True`
- `alpha040_v3_risk_controlled` 与 Freeze V3 原始结果完全一致
- `mismatch_report.md` 未生成

核心对比指标：

| 策略 | 接受交易 | 累计收益 | 最大回撤 | 夏普 | 卡玛 | 止损次数 | 跌停无法卖出 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `legacy_momentum_v1` | 136 | 2.32% | -3.92% | 0.659 | 1.337 | 72 | 0 |
| `alpha040_v3_risk_controlled` | 69 | 4.00% | -0.84% | 3.489 | 10.831 | 3 | 2 |
| Freeze V3 原始结果 | 69 | 4.00% | -0.84% | 3.489 | 10.831 | 3 | 2 |

说明：以上仍基于当前本地缓存样本，不能视为完整全市场长期回测。

## 4. 主要模块

### 主扫描与日报

- `src/market_scanner.py`
- `src/scheduled_v3_reporter.py`

主扫描器读取 `strategy.json`，根据 `active_strategy` 切换当前默认策略。每日 V3 报告输出 `v3_daily_report.md`；如果市场环境不是积极，则输出 no-trade report，不生成买入候选。

### 因子研究与冻结验证

- `src/factor_research.py`
- `src/factor_research_round2.py`
- `src/factor_research_round3.py`
- `src/factor_research_round4.py`
- `src/factor_research_round5.py`
- `src/factor_research_round6.py`
- `src/factor_research_freeze_v3.py`

这些模块均属于 shadow research，不直接修改主策略。它们覆盖 IC、RankIC、分组收益、相关性矩阵、Alpha040 Core、风险归因、组合回测增强、Freeze V3 稳定性测试。

### 数据扩展与扩展回测

- `src/data_expansion_pipeline.py`
- `src/backtest_v3_expanded.py`

数据扩展模块优先尝试 Fuyao，其次 Tushare，失败时降级本地缓存。扩展回测入口会明确记录数据来源，当前 smoke 结果使用 `local_cache_explicit`。

### Forward Paper Trading

- `src/forward_paper_trading_v3.py`

该模块从下一交易日起记录冻结 V3 模拟信号，写入 `paper_trading_ledger.csv`，并生成 `weekly_paper_review.md`。规则要求至少累计 30-50 笔后再讨论是否进入真实交易流程。

### Hermes 策略管家

- `scripts/run_strategy_steward.sh`
- `prompts/strategy_steward_agent.md`
- `src/strategy_learning.py`

Hermes 只读诊断，不修改主策略、不改台账、不输出确定收益判断。它负责事实核对、策略诊断、候选实验草案、反方审查和学习摘要。

## 5. 自动化与推送

当前支持：

- 飞书应用机器人
- 飞书 webhook
- 企业微信
- Telegram
- Slack
- Discord
- 邮件
- GitHub Actions 定时扫描

推送密钥只通过环境变量或 GitHub Actions Secrets 注入，不写入仓库。上传前已检查源码与文档，未发现真实 Feishu/Fuyao 密钥。

## 6. GitHub 上传边界

推荐提交到 GitHub 的内容：

- 源码
- 配置模板
- 策略配置
- 文档
- GitHub Actions 工作流

不提交到 GitHub 的内容：

- `output/`
- `data/cache/`
- `data/ledger/`
- `logs/`
- `.env`
- 本机密钥文件

这些路径已经由 `.gitignore` 排除。

## 7. 已知限制

- 当前历史覆盖仍主要依赖本地缓存，不能等同完整全市场长期回测。
- Fuyao/Tushare 扩展数据链路已经搭好，但完整覆盖质量还需要持续跑 `universe_coverage_report.md` 与 `data_quality_report.md`。
- 策略指标当前体现的是缓存样本内表现，不代表未来收益。
- Forward paper trading 尚未累计足够样本，不应根据短期表现调参。
- 飞书移动端直接与 Hermes bot 对话需要完成 pairing approve。

## 8. 建议下一步

1. 在 GitHub Actions Secrets 配置 `FUYAO_API_KEY`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_RECEIVE_ID`。
2. 每日收盘后运行 `src/scheduled_v3_reporter.py` 和 `src/forward_paper_trading_v3.py`。
3. 每周查看 `weekly_paper_review.md`，但 30-50 笔前不调参。
4. 定期运行 Hermes `daily` 和 `critic` 模式，保持只读诊断。
5. 补齐 2024 或更早历史后，再做年份切分样本外验证。
