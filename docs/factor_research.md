# 因子研究与策略消融模块

`research/factor_research.py` 是独立 shadow research 模块，用于评估现有因子和 Alpha191 短周期量价子集。它不修改 `strategy.json` 主策略，不写交易台账，不连接实盘接口。

## 运行

```bash
python3 research/factor_research.py
```

常用参数：

```bash
python3 research/factor_research.py \
  --max-symbols 300 \
  --min-cross-section 30 \
  --output output/factor_research
```

第二轮审计与专项影子实验：

```bash
python3 research/factor_research_round2.py
```

第三轮 Alpha040 Core shadow strategy：

```bash
python3 research/factor_research_round3.py
```

第四轮风险归因与组合增强 shadow strategy：

```bash
python3 research/factor_research_round4.py
```

第五轮风险口径修正与止损归因 shadow strategy：

```bash
python3 research/factor_research_round5.py
```

第六轮 Alpha040 Risk-Controlled shadow strategy：

```bash
python3 research/factor_research_round6.py
```

Freeze Validation：

```bash
python3 research/factor_research_freeze_v3.py
python3 src/forward_paper_trading_v3.py
```

模块默认读取 `data/cache/market_scanner/ohlcv/` 中由收盘扫描沉淀的历史 K 线缓存。若缓存未覆盖全 A，则报告会明确标注样本边界，不补造数据。

## 因子口径

- 现有因子：RPS20/RPS60、基础分、增强分、综合分、动量、成交额、振幅、量比、波动率、距离 20 日高点、均线偏离。
- Alpha191 子集：只纳入 20 个短周期量价因子，覆盖跳空、日内位置、短期动量、量价相关、量能方向和区间强弱。
- 预处理：每个交易日横截面执行 1%/99% 去极值、当日中位数缺失填充、z-score 标准化。
- 标签：未来 5 日超额收益 = 个股未来 5 日收益 - 当日研究横截面未来 5 日收益中位数。
- 验证：按日期时间切分，禁止随机切分。

## 输出

默认输出到 `output/factor_research/`：

- `factor_dataset.csv`：原始因子、标准化因子和未来 5 日超额收益标签。
- `factor_metadata.csv`：因子名称、来源和说明。
- `ic_daily.csv` / `ic_summary.csv`：IC 与 RankIC 明细和汇总。
- `group_returns.csv` / `group_summary.csv`：分组收益明细和汇总。
- `correlation_matrix.csv`：标准化因子相关性矩阵。
- `ablation_trades.csv` / `ablation_summary.csv`：因子消融影子回测。
- `factor_research_report.md`：人类可读研究报告。

## 消融实验

当前实现以下 shadow 对照：

- 当前因子组合影子基线
- 去掉 RPS
- 去掉趋势
- 去掉量能
- 去掉波动
- 去掉强势区
- 加入防守环境不交易
- 加入时间止损
- 加入冲高回落过滤

消融实验只用于诊断和候选实验设计。任何因子或规则升级都必须经过人工确认、二次回测和 Hermes critic 审查后，才能考虑进入主策略。

## 第二轮审计

`research/factor_research_round2.py` 输出到 `output/factor_research_round2/`，重点覆盖：

- `universe_audit.md`：检查当前缓存股票池是否存在后验股票池偏差。
- `top_abs_correlations.csv`：绝对相关最高的前 20 组因子。
- `watched_factor_correlations.csv`：RPS/涨跌幅、波动/振幅、趋势/强势区等指定因子对检查。
- `train_test_factor_comparison.csv`：train/test IC、RankIC、分组收益对比和稳定性分类。
- `round2_shadow_summary.csv`：alpha040 专项、趋势弱过滤、三种时间止损的影子回测。
- `reversal_filter_audit.json`：冲高回落过滤触发、剔除和后续收益审计。
- `factor_research_round2_report.md`：第二轮人类可读报告。

第二轮仍然是 shadow evidence，不替换主策略参数。

## 第三轮 Alpha040 Core

`research/factor_research_round3.py` 输出到 `output/factor_research_round3/`，重点覆盖：

- `daily_universe.csv`：逐日动态可交易股票池，含股票数和剔除原因统计。
- `alpha040_core_selected_signals.csv`：Alpha040 Core 每日入选信号。
- `alpha040_core_trades.csv`：带手续费、滑点、涨跌停和停牌限制的事件级回测明细。
- `train_test_performance.csv`：train/test 分段表现。
- `market_regime_performance.csv`：按市场环境分组表现。
- `industry_performance.csv`：按可用行业/板块字段分组表现。
- `exit_reason_performance.csv`：止盈、止损、时间止损、到期、环境退出分组。
- `single_symbol_loss_summary.csv`：单票最大亏损。
- `monthly_returns.csv`：组合月度收益。
- `daily_positions.csv`：每日持仓数量和敞口。
- `factor_research_round3_report.md`：第三轮人类可读报告。

第三轮使用 Alpha040 为主排序因子，RPS60 和接近 20 日高点为辅助因子；不重复加权 RPS60/change_rate_60d 或 RPS20/change_rate_20d；趋势只做弱过滤；冲高回落只作为标签输出。

## 第四轮风险归因

`research/factor_research_round4.py` 输出到 `output/factor_research_round4/`，基于第三轮 Alpha040 Core 做组合层面的风险归因和风控 shadow 对照，不新增复杂因子。

- `portfolio_metrics.json`：初始资金、期末权益、累计收益、年化收益、最大回撤、夏普、卡玛和最大回撤区间。
- `daily_equity.csv` / `monthly_returns.csv`：当前止损版本的每日权益曲线和月度收益。
- `exit_reason_summary.csv`：止盈、止损、到期、时间止损、跌停无法卖出、同日止盈止损的分组统计。
- `market_regime_summary.csv` / `market_regime_open_policy.csv`：按积极、中性、谨慎、防守环境统计表现，并给出禁止开仓、降仓/收紧风控或继续观察的建议。
- `risk_experiment_summary.csv`：当前止损、ATR 止损、ATR 止损 + 风险预算仓位三组风险控制 shadow 实验。
- `max_losing_streak_detail.json`：最大连续亏损的时间、环境、股票和行业/主题集中度。
- `universe_zero_dates.csv` / `universe_count_distribution.csv`：daily universe 为 0 的日期原因和股票池数量分布。
- `factor_research_round4_report.md`：第四轮人类可读报告。

第四轮结论仍是 shadow evidence。风险控制实验只用于研究止损和仓位管理是否值得进入更长样本验证，不会自动修改 `market_scanner` 主策略。

## 第五轮风险口径修正

`research/factor_research_round5.py` 输出到 `output/factor_research_round5/`，重点解决 Round4 暴露出的 universe 覆盖、市场环境口径混用、止损/跌停归因和 A/C 风控版本取舍问题，不新增复杂因子。

- `universe_zero_reason_summary.csv` / `universe_zero_audit.md`：读取 Round4 的 universe 为 0 日期，按缓存覆盖、过滤过严、数据缺失、涨跌停过滤和其他原因重新归类。
- `market_regime_event_summary.csv`：事件级 trade sequence 表现，只含交易数、胜率、平均单笔、中位数和最大连续亏损。
- `market_regime_portfolio_summary.csv`：组合级 equity curve 表现，只含组合累计收益、组合最大回撤和正收益交易日占比。
- `market_regime_dual_summary.csv`：把事件级和组合级指标并列表达，但不混用最大回撤口径。
- `stop_loss_feature_comparison.csv` / `risk_group_market_distribution.csv` / `risk_feature_lift.csv`：对止损和跌停无法卖出交易做特征差异、市场环境分布与风险 lift 分析。
- `stop_risk_filter_experiment_summary.csv`：高振幅、高波动、开仓近涨停、高上影线、5 日过热五组 shadow 剔除实验。
- `risk_control_a_c_comparison.csv` / `monthly_stability_comparison.csv`：A 当前止损 baseline 与 C ATR 止损 + 风险预算仓位对比。
- `factor_research_round5_report.md`：第五轮人类可读报告。

第五轮的推荐版本只表示下一轮 shadow research 优先级，不会自动并入主策略。

## 第六轮风险控制策略

`research/factor_research_round6.py` 输出到 `output/factor_research_round6/`，基于 Round5 的风险阈值构建 Alpha040 Risk-Controlled Shadow Strategy，重点验证减少亏损交易，不以单纯提高收益为目标。

- `strategy_comparison.csv`：baseline、版本1、版本2、版本3 的事件交易数、组合接受交易数、收益、回撤、夏普、卡玛、胜率、平均单笔、中位数、最大连续亏损、止损和跌停无法卖出次数。
- `monthly_returns_by_version.csv`：四个版本的月度收益宽表。
- `daily_candidate_counts.csv`：baseline、版本1、版本2、版本3 每日候选数和选中数。
- `candidate_count_summary.csv` / `over_filter_audit.json`：在允许开仓且 baseline 有候选的日期内判断版本3是否过滤过度。
- `data_boundary.md` / `data_boundary.json`：继续保留本地缓存覆盖边界说明。
- 每个版本均输出 `*_trades.csv`、`*_portfolio_trades.csv`、`*_accepted_trades.csv`、`*_daily_equity.csv` 和 `*_monthly_returns.csv`。
- `factor_research_round6_report.md`：第六轮人类可读报告。

第六轮仍然只是 shadow evidence，不修改主 `market_scanner` 策略，不连接实盘接口。

## Freeze V3 验证

`research/factor_research_freeze_v3.py` 输出到 `output/factor_research_freeze_v3/`，固定 `v3_atr_risk_budget_hot5_vol_risk_on`，只做复跑和稳定性验证，不继续调参、不新增因子、不修改版本3规则。

- `freeze_v3_strategy.json`：冻结规则配置，记录 alpha040 主排序、rps60/close_to_20d_high 辅助、积极环境开仓、5日涨幅和 volatility_20d 阈值、ATR 止损 + 风险预算仓位、趋势弱过滤、冲高回落标签边界。
- `freeze_rerun_summary.csv` / `reproducibility_check.csv`：复跑 baseline、版本2、版本3，并与 Round6 结果比对。
- `stability_test_summary.csv` / `stability_monthly_returns.csv`：交易成本加倍、滑点加倍、5日涨幅阈值、volatility_20d 阈值、最大持仓数、单票风险预算压力测试。
- `sample_out_validation_preparation.json`：判断是否具备更长历史做按年份切分；当前不足时转入 forward paper trading。
- `forward_paper_trading_plan.md`：forward paper trading 操作边界。
- `freeze_v3_validation_report.md`：Freeze Validation 人类可读报告。

`src/forward_paper_trading_v3.py` 只记录冻结 v3 的信号、模拟触发区间和计划参数；不连接实盘，不自动下单，不根据结果改规则。
