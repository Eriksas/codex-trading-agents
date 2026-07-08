> ⚠️ **本报告已过时**：其中 V3 与扩展回测数字基于后被证实失真的指数数据。
> 现行版本见 `docs/project_overall_report_2026-07-08.md`；翻案依据见 `docs/v3_reaudit_2026-07-03.md`。

# Codex Trading Agents 项目整体报告

生成日期：2026-06-29  
项目路径：`/Users/eriksas/projects/codex-trading-agents`  
当前主策略：`alpha040_v3_risk_controlled`  
旧策略保留：`legacy_momentum_v1`

> 本项目仅用于个人模拟复盘、研究和自动化报告，不构成投资建议，不连接实盘接口，不自动下单。

## 1. 项目定位

Codex Trading Agents 是一个面向 A 股的个人量化辅助项目，核心目标不是自动交易，而是把“数据获取、策略扫描、模拟交易计划、风险复盘、AI 诊断、飞书推送”组织成一套可审计、可回滚、可持续进化的工作流。

当前项目已经从早期动量策略升级到 `alpha040_v3_risk_controlled`，同时保留旧策略 `legacy_momentum_v1` 作为回滚入口。Hermes Agent 被定位为策略维护助理：负责长时间只读审计、报告草稿、反方检查和学习记录建议；Codex 负责工程实现、边界把关、最终验收。

## 2. 安全边界

项目当前明确禁止：

- 不连接券商或实盘交易 API。
- 不自动下单。
- 不把回测结果表述为未来收益确定。
- 不在样本不足时调参。
- 不把 `output/`、`data/cache/`、`data/expanded/`、ledger、API key 或 secret 上传 GitHub。
- 不让 Hermes 直接修改主策略、台账或配置。

所有主策略升级都应满足：影子实验、回测验证、人工确认、可回滚。

## 3. 当前主策略

策略名：`alpha040_v3_risk_controlled`

固定规则：

- 主排序因子：`alpha040`。
- 辅助因子：`rps60`、`close_to_20d_high`。
- 只在“积极”市场环境开仓。
- 5 日涨幅大于 `20.39%` 剔除。
- `volatility_20d` 大于 `4.98%` 剔除。
- 使用 ATR 止损和风险预算仓位。
- 收盘价需站上 MA20，趋势只做弱过滤。
- 上影线只打标签，不硬过滤。
- 中性、谨慎、防守环境禁止新开仓。

旧策略 `legacy_momentum_v1` 已保留在：

- `strategy_legacy_v1.json`
- `src/market_scanner_legacy_v1.py`
- `docs/rollback_guide.md`

## 4. 数据扩展状态

Hermes 长任务补齐后的历史数据：

- 数据区间：`2021-01-04` 至 `2026-06-26`
- 股票数：`5198`
- 日线行数：`6,350,503`
- 交易日数：`1326`
- 数据源：当前最终日线实际为 `efinance` 单一来源扩展
- 行业源：已补充 BaoStock 行业表
- 目标请求日期包含 `2026-06-29`，但该日尚未形成完整收盘日线，因此质量报告标记为缺失开放交易日

质量检查结果：

- OHLC 异常：`0`
- close <= 0：`0`
- amount <= 0：`0`
- 最终重复记录：`0`
- 原始重叠记录：`43,294`，主要来自 efinance 与本地缓存重叠；最终按优先级去重
- 收盘价重叠不一致：`0`
- 成交额重叠不一致：`197`
- 涨跌停类记录：`153,278`，保留为真实交易约束的一部分
- `pre_close` 缺失 `5198` 行，基本对应每只股票首条记录，后续可抽样复核

Universe 情况：

- 前 59 个交易日 universe 为 0，主要是 60 日历史窗口暖机期。
- 暖机后有效信号日：`1267`
- 暖机后已足够做扩展回测。

重要口径：当前扩展数据不应宣传为“多源完整全市场长期回测”。更准确的说法是：“基于 efinance 免费源、覆盖 2021-01-04 至 2026-06-26 的扩展历史数据”。如果未来配置 Fuyao/Tushare token，需要重新跑多源一致性校验。

## 5. 扩展回测结果

本节已更新为 2021 起正式扩展回测结果，样本覆盖 `2021-01-04` 至 `2026-06-26`。旧版 2024-2026 结果仍保留在 `output/expanded_backtest_v3/`，新版完整报告位于 `output/expanded_backtest_v3_2021/expanded_backtest_report.md`。

核心对比：

| 策略 | 接受交易 | 累计收益 | 最大回撤 | Sharpe | Calmar | 止损 | 跌停无法卖出 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `legacy_momentum_v1` | 1045 | -22.50% | -25.46% | -0.84 | -0.19 | 519 | 0 |
| `baseline_current_stop` | 1419 | -111.08% | -113.94% | -0.40 | - | 673 | 167 |
| `v3_atr_risk_budget_hot5_vol_risk_on` | 347 | -9.89% | -11.12% | -1.12 | -0.18 | 52 | 14 |

解释：

- V3 在 2021 起扩展样本中不再呈现正收益，说明旧 2024-2026 小样本收益发生回归。
- V3 仍比 legacy 和当前止损 baseline 更偏风险控制：亏损较小、最大回撤较低、止损和跌停无法卖出次数更少。
- V3 与 legacy 的交易口径不同，不能只按收益大小直接判断优劣。
- 本轮结果不能推出“策略稳定赚钱”，也不能自动触发调参或回滚；只能作为扩大样本后的诊断依据。

## 6. 行业归因修复

行业归因已按 2021 起正式扩展回测重新刷新，口径为 `output/expanded_backtest_v3_2021/v3_atr_risk_budget_hot5_vol_risk_on_accepted_trades.csv` 中的 347 笔 accepted trades。

当前修正后的行业口径：

- V3 接受交易：`347` 笔。
- 行业缺失数量：`0`。
- 行业字段来自 `data/expanded/industry_or_sector.csv` / BaoStock 行业映射，不使用 SH/SZ/BJ 市场后缀冒充行业。
- 累计贡献按 `net_return × portfolio_position_pct` 计算，避免把不同仓位交易当成同等影响。
- 完整结果位于 `output/expanded_backtest_v3_2021/v3_industry_performance_2021.csv`。

当前 V3 行业分布前几项：

| 行业 | 交易数 | 胜率 | 平均单笔 | 中位数 | 止损 | 跌停无法卖出 | 累计贡献 |
|---|---:|---:|---:|---:|---:|---:|---:|
| C39计算机、通信和其他电子设备制造业 | 64 | 45.31% | -0.07% | -0.45% | 7 | 2 | -0.18% |
| C38电气机械和器材制造业 | 29 | 37.93% | -1.11% | -1.21% | 4 | 0 | -0.50% |
| I65软件和信息技术服务业 | 23 | 34.78% | 1.35% | -3.07% | 0 | 1 | 0.70% |
| J67资本市场服务 | 18 | 27.78% | -1.75% | -1.93% | 1 | 0 | -0.32% |
| C27医药制造业 | 15 | 33.33% | -1.46% | -2.39% | 1 | 2 | -0.48% |

后续应重点观察：V3 是否长期集中在电子设备、电气机械、软件信息等方向；单行业拖累只能列为 `shadow_experiment_candidate`，不能直接修改主策略。

## 7. 主要风险

当前最重要的风险不是“策略是否赚钱”，而是以下几个工程和方法论风险：

1. 数据源单一：当前扩展数据主要依赖 efinance，未来应接入 Fuyao/Tushare 后做一致性检查。
2. 2021 起正式扩展回测已完成：结果显示 V3 收益转负，需进入分年份、行业、环境和退出原因诊断阶段。
3. V3 收益转负：2021 起累计收益约 -9.89%，说明更长样本下收益端并不稳。
4. 样本仍有限：V3 接受交易 347 笔，足够做扩展回测诊断，但不够支持频繁细分调参。
5. 环境退出贡献偏负：`environment_exit` 44 笔平均约 -0.69%，后续可作为影子实验观察项。
6. 行业集中需持续监控：行业归因刚修复，应观察后续 forward paper trading 是否持续集中。


## 8. Setup-Based 与 Exit Experiment 收敛结论

2021 起长样本扩展后，项目已完成 ranking-based、setup-based、exit shadow 三轮研究收敛：

- ranking-based 策略未通过长样本收益验证：V3 在 2021-01-04 至 2026-06-26 样本中累计收益约 -9.89%，alpha040 长样本 IC/RankIC 与 Q5-Q1 不支持其作为稳定主因子。
- setup-based 策略改善了风险结构，但仍未转正：`volatility_contraction_breakout_v1` 累计收益约 -1.86%，`pullback_reclaim_v1` 约 -2.94%，两者均好于多数旧 ranking 版本，但未验证正期望。
- exit shadow experiment 未能使 setup 转正：所有退出实验仍为负收益，没有任何组合口径转正。
- 当前最接近可继续观察的是 `volatility_contraction_breakout_v1 / fast_fail_exit_day2`：累计收益约 -1.77%，相对 baseline 仅改善 0.09%，路径依赖交易少，但不能证明有效。
- `pullback_reclaim_v1 / partial_take_profit_3pct` 可列为 shadow candidate：相对 baseline 改善约 0.71%，中位数改善，但路径依赖比例约 62.02%，必须分钟数据验证。
- 当前阶段不应继续调参，不应继续扩大策略数量。
- 后续重点应转向人工样本复盘和分钟数据可行性验证。

保留候选：

| 候选 | 保留理由 | 限制 | 状态 |
|---|---|---|---|
| `volatility_contraction_breakout_v1 / fast_fail_exit_day2` | 收益最接近 0，路径依赖交易少 | 相对 baseline 改善仅 0.09%，不能证明有效 | `shadow_experiment_candidate` |
| `pullback_reclaim_v1 / partial_take_profit_3pct` | 中位数改善、胜率较高 | 路径依赖比例高，必须分钟数据确认 | `shadow_experiment_candidate` |

其他 setup exit 实验统一归档为 `archived_negative_or_path_dependent`。

关键输出：

- `output/setup_based_short_swing_exit_experiment/setup_exit_research_conclusion.md`
- `output/setup_based_short_swing_exit_experiment/manual_review_priority_list.csv`
- `output/setup_based_short_swing_exit_experiment/minute_data_feasibility_plan.md`

最终判断：当前项目工程框架有效，但策略 alpha 尚未找到。后续研究应收敛，不应继续扩大策略数量。只有在人工样本复盘和分钟数据验证后，才能决定是否继续 setup-based 路线。

## 9. Hermes 工作协议

Hermes 的定位：

- 长时间读报告、跑审计、写诊断草稿。
- 每日/每周做策略维护参谋。
- 输出事实核对、反方审查、候选实验草案。
- 不修改主策略、不改台账、不自动下单。

推荐节奏：

- 每日：读取日报、模拟持仓、数据质量、异常退出，生成简短维护摘要。
- 每周：运行 steward daily + critic，检查 5/20/60 日窗口是否有真实差异。
- 每 30-50 笔 closed paper trades：再讨论是否做实验草案。
- 小样本阶段：优先输出“不调参”。

Codex 的职责：

- 分派任务。
- 检查 Hermes 是否越界。
- 审计结论是否绑定数据和样本量。
- 决定是否进入代码修改、回测或策略升级。

## 10. GitHub 上传建议

建议上传：

- `src/`
- `scripts/`
- `prompts/`
- `docs/`
- `README.md`
- `HERMES.md`
- `strategy.json`
- `strategy_legacy_v1.json`
- `freeze_v3_strategy.json`
- `.env.example`
- GitHub Actions workflow

不要上传：

- `.env`
- API key 或 secret
- `output/`
- `data/cache/`
- `data/expanded/`
- `data/ledger/`
- `logs/`
- 任何飞书 secret、receive id、pairing code

上传前建议：

- 清理 `.DS_Store`。
- 确认 `.gitignore` 覆盖 output/cache/ledger/logs。
- 在 README 中明确：当前是个人研究与模拟复盘项目，不是实盘交易系统。
- 在 README 中明确：扩展回测当前主要基于 efinance 免费源。

## 11. 下一步优先级

P0：

- 人工复核 `output/expanded_backtest_v3_2021/expanded_backtest_report.md` 与 `output/expanded_backtest_v3_2021/v3_long_sample_diagnosis.md`，尤其是收益回归、行业集中、退出原因和风险预算敞口。
- 在 README 或数据报告里明确 efinance 单源扩展口径。
- 抽样校验 `pre_close` 首条缺失是否符合预期。

P1：

- 启动或检查 `forward_paper_trading_v3.py` 的每日记录。
- 至少累计 30-50 笔模拟 closed trades 后再讨论策略调参。
- 让 Hermes 每周做 critic，专门反驳过拟合和小样本结论。

P2：

- 配置 Fuyao/Tushare 后重跑多源扩展数据。
- 对 `environment_exit` 做 shadow 实验，不并入主策略。
- 增加行业集中度风控 shadow 报告。
- 做 GitHub 首次正式提交前的 secret 与 ignore 审计。

## 12. 当前结论

项目已经从“策略脚本”进化为一个有边界、有回滚、有影子实验、有 AI 维护协议的个人量化研究系统。当前 V3 主策略不是高收益模型，但它在扩展样本中体现出更好的回撤控制和执行纪律。下一阶段的重点不是继续调参，而是稳定运行 forward paper trading、积累真实模拟样本、修复数据源和行业口径，并让 Hermes 作为长期维护助理持续输出可审计的诊断。

Setup/exit 研究收敛后的最终判断：当前项目工程框架有效，但策略 alpha 尚未找到。后续研究应收敛，不应继续扩大策略数量。只有在人工样本复盘和分钟数据验证后，才能决定是否继续 setup-based 路线。
