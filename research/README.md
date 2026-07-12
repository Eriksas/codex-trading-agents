# research/ - 研究代码（已收敛）

本目录存放因子研究、扩展回测、setup-based 实验和一次性诊断脚本。
它们**不在每日运行路径上**，`src/` 与定时任务不得导入本目录。

## 研究结论（截至 2026-07-03）

- ranking-based（alpha040 V3）与 setup-based 两条路线在 2021-2026 长样本中均为负收益。
- 全部退出实验未能使任何组合转正。
- 项目结论：工程框架有效，策略 alpha 尚未找到；**不应继续调参或扩大策略数量**。
- 下一步依据 `docs/project_overall_report_2026-06-29.md`：人工样本复盘、分钟数据可行性验证、forward paper trading 积累 30-50 笔。

## 文件说明

| 文件 | 用途 | 状态 |
|---|---|---|
| `factor_research.py` | 因子 IC/RankIC/分组/消融基础框架 | 保留，round2-6 的依赖底座 |
| `factor_research_round2-6.py` | 历史研究轮次（链式依赖，round6 为 V3 来源） | 冻结，仅复现历史结果用 |
| `factor_research_freeze_v3.py` | V3 冻结验证 | 冻结 |
| `backtest_v3_expanded.py` | 2021 起扩展回测（引擎为 round6） | 数据更新后可重跑 |
| `data_expansion_pipeline.py` | efinance/BaoStock 历史数据扩展 | 数据更新后可重跑 |
| `main_strategy_upgrade_v3.py` | 主策略升级审计（对照 archive/ 中 legacy） | 下次策略升级时用 |
| `setup_based_short_swing.py` | 三类事件型 setup 研究 | 已收敛，负收益归档 |
| `automated_setup_review.py` | setup 样本自动复盘 | 已收敛 |
| `diagnose_*.py`、`short_horizon_*.py`、`strategy_discovery_shadow.py`、`run_setup_exit_experiment.py` | 一次性诊断与 shadow 实验脚本 | 已完成使命，保留可复现性 |
| `panel_backtester.py` | 全市场向量化回测引擎（复权修正/涨跌停/T+1，2026-07 新增） | **活跃**，配套 `test_panel_backtester.py` 手工算例 |
| `strategy_search_202607.py` | 2026-07 策略搜索 runner（三段协议+试验登记） | 本轮已收敛，见 `docs/strategy_search_2026-07_report.md` |
| `fetch_adj_factors_baostock.py` | BaoStock 后复权因子拉取（修复不复权缺陷） | 数据工具，可重跑 |

## forward paper 日常运行（idx1000_bounce_h5 shadow candidate）

每交易日收盘后（15:30 后，本地运行；夜间东财接口不稳）：

GitHub Actions 每交易日自动运行全套（19:40 + 21:30 幂等重试），本地补跑同命令：

```bash
python3 research/forward_paper_bounce.py update && python3 research/forward_paper_bounce.py check
python3 research/forward_paper_bounce.py settle       # 有持仓时
python3 research/forward_gate_recorder.py             # G2/G3/G4 目标暴露
python3 research/forward_thermometer_recorder.py      # 涨停生态快照
python3 research/forward_cb_recorder.py               # 双低名单+主板信用温度计
python3 research/forward_divlv_recorder.py            # 红利名单（季度，季初窗口）
python3 research/forward_health_check.py              # 台账自检（问题→飞书）
```

纪律：bounce 不满 20 次事件、gate 不满 Day250 主决策点（PROTOCOL_V1），
不做统计结论、不调参。全部台账在 `forward_state/`（git 跟踪=防篡改留痕）。

## 2026-07 搜索轮结论摘要

61 个配置三段检验（训练深熊/验证混合/holdout 疯牛）后：无高仓位全天候非负策略；
`idx1000_bounce_h5` 三段全正但统计不显著，列为 shadow candidate；`abturn` 缩量因子
为体制条件性 alpha（熊/震荡强、疯牛失效）。重要数据修复：指数文件失真已重建
（`index_daily_baostock.csv`）、不复权缺陷已加除权中性化。详见完整报告。

## 运行方式

所有模块从仓库根目录运行（输出路径为 cwd 相对路径）：

```bash
python3 research/factor_research_round6.py
python3 research/backtest_v3_expanded.py
```

模块间导入依赖脚本所在目录自动加入 `sys.path`；`market_scanner` 等运行核心通过各文件头部的 `ROOT_DIR / "src"` 注入解析。
