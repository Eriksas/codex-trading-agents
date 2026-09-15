# 五分钟上手：一份策略健康诊断

## 1. 运行离线示例

在仓库根目录运行，Python 3.11+ 标准库即可：

```bash
python main.py demo
```

终端返回报告绝对路径。打开 `report.md`，依次看数据缺口、Python 指标、数值假设的否决和待人工决定。

[样例](../examples/diagnosis/README.md)完全合成：三条复盘（其中一条缺数据），两条归档 -4%/+2%。两条的平均值 -1% 来自原健康计算函数。被否决的只是“所提供归档的平均净收益为正”，不能推断策略永远无效。

## 2. 认清四个角色

| 部分 | 这一步做什么 |
|---|---|
| 入口 [main.py](../main.py) | 选择 demo 或 diagnose 任务 |
| 编排 [diagnosis.py](../src/diagnosis.py) | 检查并快照输入，保存报告、日志和 AI 任务材料 |
| 计算 [strategy_health.py](../src/strategy_health.py) | 沿用已有 5/20/60 自然日窗口统计与分母 |
| Agent 与人工 | Agent 提出候选解释和反方意见；人工核对，并组织下一项独立验证 |

无需先理解研究轮次或扫描器全部实现。

## 3. 用自己的已有复盘

```bash
python main.py diagnose --date 2026-09-15
```

默认输入（相对仓库根目录）：

```text
output/YYYY-MM-DD/scan/review_details.csv
output/YYYY-MM-DD/scan/simulated_trades.csv   可选的候选元数据
data/ledger/trade_archive.csv                缺失时收益指标为空
```

脚本读取截至指定日期、最近 120 自然日内的复盘文件，并计算 5/20/60 日窗口。同一交易在不同日期的复盘按窗口内最新状态计算；同文件重复交易标识会报错。归档按退出日期筛选，不使用未来退出收益。

使用其他本地目录时：

```bash
python main.py diagnose --date 2026-09-15 --input-root "D:/my-reviews" --archive "D:/my-records/trade_archive.csv" --question "最近触发率和止损率有什么变化？"
```

上方外部目录是路径示意，需要替换为实际数据。这个入口只处理既有 CSV，不会联网补数据，也不负责把任意表格自动转换为项目格式。

### 最小输入契约

| 文件 | 必需列 |
|---|---|
| `review_details.csv` | `trade_id, review_date, status, data_quality`；review_date 必须与父日期目录一致 |
| `trade_archive.csv` | `trade_id, exit_date, net_return`；net_return 使用小数，空值不进入收益均值 |
| `simulated_trades.csv` | `symbol`；标签和环境字段沿用现有扫描产物 |

复盘状态沿用 [reviewer.py](../src/reviewer.py) 的 `STATUS_LABELS`。参与计算的数值须有限；重复标识、字段/日期错误会阻断本次计算并写出错误报告。原文件不会被修正或覆盖。

**统计边界：** 触发率、止损率、止盈率的分母是 `reviewed_count`；收益均值和胜率使用窗口内有净收益的归档。两组分母不一定相同，归档也未强制与复盘逐条连接。使用者须提供同一研究对象、相同费用/收益口径的记录；当前不自动区分多套策略。完整交易日覆盖、行业可比性和经济含义仍需人工核对。

## 4. 读输出，接入 AI 解释

每次新建 `output/diagnosis/日期-运行时间-标识/`，保留旧运行：

| 文件 | 用途 |
|---|---|
| `report.md` | 首先阅读的一份中文报告 |
| `facts.json` | 可核查的指标、数据质量、输入来源与哈希 |
| `agent_prompt.md` | 复用现有 Steward 规则，为当前材料生成的诊断任务 |
| `result.json` | 运行状态、事实包哈希、导入回答与人工待审状态 |
| `run.log` | 本次运行留痕 |
| `python/` | 原健康模块生成的统计产物；格式错误/无复盘时不生成 |

第一阶段不自动调用模型。需要 AI 辅助时：

1. 阅读 `agent_prompt.md`，确认其中本地路径和统计资料适合分享，再交给你使用的模型。无需给模型文件写入工具。
2. 让模型按文件内的 JSON 契约返回候选假设、证据引用、反证、待验证方法和反方意见。保存原始 JSON，例如本地 `output/diagnosis/answer.json`。
3. 使用相同日期、路径、问题重新运行并导入回答：

```bash
python main.py diagnose --date 2026-09-15 --interpretation output/diagnosis/answer.json
```

对 demo 材料则用 `python main.py demo --interpretation output/diagnosis/answer.json`。回答引用的 `facts_sha256` 必须匹配；数据、问题或路径变化后应重新生成解释。相同材料重复运行哈希不变，输出目录各自独立。

导入契约比旧 Hermes experiment 草案更小，专门服务这条诊断路径；不与原脚本混用。字段格式见生成的 Prompt。手写测试回答的 model 必须标记 `handwritten`，不得冒充模型采集结果。

**导入检查不等于验证全文。** 程序拒绝未知来源、数值覆盖字段和小样本高置信度；候选解释和反方文字仍可能出错，需人工核对。策略有效性仍为 Inconclusive；没有自动审批或策略修改。

退出码 `0` 表示流程完成，可能仍是 `insufficient_data`；`2` 表示输入错误或回答导入被拒绝。退出码不是策略或 Agent 的评分。

## 原有每日入口

这些入口维持原行为，用于准备每日数据和继续原有观察；无需为了新展示入口重新运行历史实验。

| 任务 | 原命令 |
|---|---|
| 自选股日报 | `python main_v2.py` |
| 收盘扫描与模拟复盘 | `python src/market_scanner.py` |
| 冻结 V3 日报 | `python src/scheduled_v3_reporter.py` |
| 冻结 V3 前向记录 | `python src/forward_paper_trading_v3.py` |
| 独立前向观察 | 见[研究导航](../research/README.md)与既有工作流 |
| Hermes daily/critic/experiment | 见[Hermes 操作说明](hermes.md) |

原行情环境建议 Python 3.12，依赖见 [requirements.txt](../requirements.txt)：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

PowerShell 激活用 `.venv\Scripts\Activate.ps1`。没有 `.env` 时才复制 [.env.example](../.env.example)；真实密钥仅写本地环境或 Actions Secrets。扫描可能请求行情；`--push-dry-run` 只模拟推送，不代表整个分析离线。

既有回测引擎验证：`python research/test_panel_backtester.py`（需 pandas、numpy）。推送与运行配置见[推送说明](push.md)，本次统一诊断本身不推送。
