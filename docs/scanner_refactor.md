# 第二阶段：按职责拆分扫描器

## 从哪里读代码

原扫描器共 3,737 行，数据请求、评分、回测、台账和 Markdown 混在同一文件。拆分后 [market_scanner.py](../src/market_scanner.py) 为 803 行，保留执行顺序、CLI、Hermes 调度以及历史函数名的兼容导入。

| 要修改的内容 | 现在的位置 | 边界 |
|---|---|---|
| 配置读取、当前策略 | [scanner_config.py](../src/scanner_config.py) | 默认值原样迁移；配置文件格式不变 |
| 数据源、缓存、K 线补齐 | [scanner_data.py](../src/scanner_data.py) | 请求参数、缓存合并、旧缓存和备用源行为保留 |
| 评分、候选、市场环境、模拟计划 | [scanner_rules.py](../src/scanner_rules.py) | 规则参数与筛选顺序保留 |
| 事件/组合回放、原有影子对照 | [scanner_backtest.py](../src/scanner_backtest.py) | 成交、费用、止损优先与资金约束口径保留 |
| pending/open 状态及归档 | [scanner_ledger.py](../src/scanner_ledger.py) | 字段、状态迁移、同标的跳过和归档去重保留 |
| 报告文字与表格 | [scanner_report.py](../src/scanner_report.py) | 只渲染已计算结果，不取行情、不执行回测 |
| 扫描器内部的格式化与 CSV | [scanner_utils.py](../src/scanner_utils.py) | 原有八个私有辅助函数，不作为新的研究 API |
| 一次扫描的步骤与开关 | [market_scanner.py](../src/market_scanner.py) | 组织已有模块，维持旧命令 |

共用的研究/日报量化能力仍通过 [quant_core.py](../src/quant_core.py) 维护。扫描器的内部辅助函数单独放置，是为避免 quant_core 原本依赖扫描器而产生循环导入；这次没有把历史研究轮次移入运行层。

## 依赖与兼容方式

配置与基础 I/O 位于依赖底层。数据模块读取配置；规则模块使用数据中的确定性计算；回测使用规则与报告；台账复用成交计算；最外层扫描入口组织这些组件。组件不能反向导入 `market_scanner`，也不能导入 `research/`，测试检查循环依赖。

历史代码仍可使用 `import market_scanner as scanner`，包括已有 `_score_stock`、`_build_trade_plan`、`_run_backtest` 等接口。95 个原函数签名保留；不需要批量修改研究轮次或原每日入口。直接脚本方式与 `src.market_scanner` 包导入均有检查。

配置切换仍使用 `scanner._set_active_config(config)`。内部组件统一从配置模块读取当前值，避免拆分后读取旧配置副本；不支持把多个策略并发运行在同一份全局配置里。本次没有引入并发上下文或新的配置框架。

旧名称是调用兼容接口，不是跨模块 monkeypatch 代理。测试数据源、缓存等内部调用时，应 patch 实际所属模块，例如 `scanner_data._fetch_fuyao_bars`。完整扫描的顶层输入仍可通过 `market_scanner.fetch_market_snapshot` 替换。

## 怎样确认没有改数值

基线固定在拆分前提交 `7b1bc3d62e44cec1c71b3dc9a0b69a06fc626ba8`，在修改实现前运行合成输入并保存输出，见[测试基线说明](../tests/fixtures/README.md)。没有重新执行真实行情研究或改写历史结果。

自动验证包括：

- legacy 与 V3 的评分、过滤、触发区间、ATR/仓位、止损/止盈、未触发、到期、费用和组合容量。
- 台账的进入、退出、缺行情、过期、同标的跳过与同日重跑归档去重。
- legacy、V3、空行情三条完整流程的返回摘要和全部 CSV/JSON/Markdown 产物；仅归一临时目录、路径分隔符，固定时钟，不修改数字或文案。
- 新鲜缓存、旧缓存降级、禁用旧缓存、增量去重、缓存禁写、备用数据源与外部程序错误。
- 配置反复切换、95 个函数签名、默认配置摘要、每日与历史研究导入、模块无环依赖。
- Hermes 超时与推送参数只通过 mock 检查，不发送消息或调用模型。

```bash
python -m pip install pandas numpy requests certifi
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s eval -p "test_*.py"
python research/test_panel_backtester.py
python main.py demo
```

本地验证记录（2026-09-16）：27 项主流程/扫描器测试、14 项 Eval 测试、原引擎 T1–T8 均通过；三条完整场景各 24 个产物与旧基线一致。71 个 Python 文件编译、84 个变更文档链接/锚点、全部 shell 语法及原四个每日入口的帮助检查通过。旧源文件哈希已对照固定 Git 提交核验，95 个原函数的语法结构在归一配置引用位置后保持一致。

## 本轮保持的范围与局限

策略配置、冻结阈值、费用、执行假设、每日工作流、前向记录和历史研究结果均不随拆分修改；原四个每日入口继续使用原命令。运行输出文件名和字段保留。

这些测试证明的是固定场景下重构前后一致，不证明回测模型正确、数据源可靠或策略有效。扫描器里的旧数据源路径和已记录的执行近似原样保留；相关修复应另立变更，不能混入“等价拆分”。

本轮不迁移历史研究链、不新增 Agent 自动调用或权限框架。新读者仍从 `python main.py demo` 开始，只有维护扫描逻辑时才需要阅读这些组件。
