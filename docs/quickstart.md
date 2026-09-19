# 上手指南

## 运行诊断

在仓库根目录，使用 Python 3.11+：

```bash
python main.py demo
python main.py diagnose --date 2026-09-15
```

第一条使用[合成样例](../examples/diagnosis/README.md)，无需密钥或依赖；第二条的日期替换为实际统计截止日，读取已有复盘，不联网补数或推送。

每次在 `output/diagnosis/` 创建独立目录。**先读 `report.md`**；核对细节时再看：

| 文件 | 用途 |
|---|---|
| `facts.json` | 全部窗口指标、数据问题、来源路径和哈希 |
| `result.json` | 运行状态、模型调用和人工待审状态 |
| `agent_prompt.md` | 人工获取 AI 解释时使用的任务材料 |
| `run.log`、`python/` | 日志与原健康统计产物；无有效输入时可能没有 `python/` |

缺数据会明确提示不足，不替换成零收益。退出码 `0` 表示流程完成，不代表研究结论成立；`2` 表示输入或 AI 处理错误，具体原因见运行状态。

## 输入数据

默认读取下列位置（相对仓库根目录）：

```text
output/YYYY-MM-DD/scan/review_details.csv
output/YYYY-MM-DD/scan/simulated_trades.csv  # 可选候选信息
data/ledger/trade_archive.csv
```

读取截止日前 120 自然日内的复盘，计算 5/20/60 自然日窗口。同一交易取窗口内最新复盘；归档按退出日期筛选。

<details>
<summary>自定义路径与字段要求</summary>

```bash
python main.py diagnose --date 2026-09-15 --input-root "D:/my-reviews" --archive "D:/my-records/trade_archive.csv" --question "最近触发率和止损率有什么变化？"
```

路径需换成实际目录。CSV 必需列：

- 复盘：`trade_id, review_date, status, data_quality`，日期须与父目录一致。
- 归档：`trade_id, exit_date, net_return`，收益用小数，空值不进入收益统计。
- 候选：`symbol`，其他字段沿用原扫描输出。

状态枚举见 [reviewer.py](../src/reviewer.py)。重复标识、无效日期或非有限数值会阻断计算；原输入不被修改。

</details>

**口径：** 触发/止损/止盈率按复盘数计算，收益均值与胜率按有收益的归档计算，两者未强制逐条连接。请提供同一策略、相同费用口径的记录；交易日覆盖和样本可比性需人工核对。

## 加入 AI 解释

默认离线。自动调用见[受控 AI 指南](controlled_agents.md)。人工导入时，将生成的 `agent_prompt.md` 交给模型，保存其原始 JSON，然后使用相同输入和问题运行：

```bash
python main.py diagnose --date 2026-09-15 --interpretation output/diagnosis/answer.json
```

示例任务用 `python main.py demo --interpretation ...`。回答的 `facts_sha256` 必须匹配；手写回答标记 `model: handwritten`。格式检查通过后，文字仍待人工评审。分享任务材料前检查其中的私人信息。

## 原有每日入口

| 任务 | 命令或说明 |
|---|---|
| 自选股日报 | `python main_v2.py` |
| 扫描与模拟复盘 | `python src/market_scanner.py` |
| V3 日报 | `python src/scheduled_v3_reporter.py` |
| V3 前向记录 | `python src/forward_paper_trading_v3.py` |
| 独立前向观察、Hermes、飞书 | [研究导航](../research/README.md)、[Hermes](hermes.md)、[推送配置](push.md) |

行情环境建议 Python 3.12，安装 [requirements.txt](../requirements.txt)。已有云端流程继续使用 Actions Secrets；本地配置参照 [.env.example](../.env.example)，已有 `.env` 不要覆盖。`--push-dry-run` 只模拟推送，不代表行情分析离线。

## 开发验证

```bash
python -m pip install pandas numpy requests certifi
python -m unittest discover -s tests -p "test_*.py"
python eval/run_eval.py
python -m unittest discover -s eval -p "test_*.py"
python research/test_panel_backtester.py
```

这些是离线代码/规则检查，不是模型通过率或策略成绩。模块位置见[扫描器说明](scanner_refactor.md)。
