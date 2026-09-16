# 扫描器拆分基线

`scanner_baseline.json` 是**合成回归测试输出，不是真实行情、交易记录、收益报告或策略实验**。所有 DEMO、STOP、PROFIT 等标识均为测试构造。

## 基线来源

- 原始提交：`7b1bc3d62e44cec1c71b3dc9a0b69a06fc626ba8`。
- 原始文件：该提交的 `src/market_scanner.py`，拆分前共 3,737 行。
- `source_sha256`：上述源文件以 UTF-8、LF 换行编码后的 SHA256。
- `function_signatures`：原有 95 个函数的调用签名。
- `default_config_sha256`：默认配置按 `json.dumps(sort_keys=True, ensure_ascii=False)` 编码后的 SHA256。
- 输入与采集过程：[scanner_scenarios.py](../scanner_scenarios.py)。

先用原实现生成 expected，再运行重构后的实现进行比较；CI 只读 expected，不会自动重生成来掩盖差异。

## 覆盖内容

`pure` 覆盖两套规则和执行口径，`ledger` 覆盖模拟状态转换，`legacy_pipeline`、`v3_pipeline`、`empty_pipeline` 覆盖实际扫描编排。

完整扫描保存返回摘要和每个 CSV/JSON/Markdown 文件的内容哈希。临时根目录统一替换为 `<RUN>`、路径分隔符统一为 `/`，文本读取统一换行；JSON 对象键排序后比较。时钟固定在 2026-03-31 18:00。数值、字段、数组顺序和报告内容不作“容差修正”。

## 如需复核来源

维护者可用 `git show 7b1bc3d62e44cec1c71b3dc9a0b69a06fc626ba8:src/market_scanner.py` 取得旧源文件，保存到仓库忽略的 `.codex/` 临时目录（不要运行它的 CLI）。将 `src/` 与 `tests/` 加入当前 Python 的模块搜索路径，用 `importlib.util.spec_from_file_location` 加载旧文件后，在临时目录调用 `scanner_scenarios.capture(旧模块, 临时根目录)`。

该 helper 固定数据与时钟，阻止真实 socket 请求和完整扫描中的外部程序调用，所有写入均指向临时目录。它不会读取真实行情或修改生产台账。比较结果应与 expected 一致；若不一致，先调查原因，不直接覆盖基线。
