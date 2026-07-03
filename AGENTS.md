# Codex Trading Agents - 项目配置

## 项目定位

本项目是**个人量化辅助与 AI 工具落地探索项目**，用于验证 Codex Sub-agents 在金融数据协作工作流、收盘扫描、模拟交易计划生成中的适用边界。

**不是**：自动化实盘交易系统、面向公众的投资顾问工具、生产级金融产品
**是**：个人使用项目，探索数据抓取、候选筛选、风控参数生成、任务分工、上下文传递、产出聚合的工程模式

## 核心原则

1. **产出定位为个人模拟交易计划**
   报告和扫描产物可包含触发区间、止损、第一止盈、仓位、计划等字段，但这些字段必须由明确规则生成，并标注为个人模拟与复盘用途。
   Agent 可以描述规则化执行条件，但不得声称收益确定、胜率确定或替用户连接实盘交易。

2. **宁可失败，不要编造**
   - 数据抓取失败时，必须明确报错，禁止用随机数或历史均值"填充"
   - 技术指标计算异常时，必须在报告中标明"计算异常，已跳过"
   - 不确定的结论用"数据显示…"而非"预测…"

3. **每次运行留痕**
   所有产出存入 `output/YYYY-MM-DD/` 目录，包含：原始数据、中间计算、最终报告、运行日志。

## 技术栈

- **语言**: Python 3.11+
- **数据源**: akshare（A股日频数据）
- **数据处理**: pandas, numpy
- **技术指标**: ta-lib 或 pandas-ta
- **Agent 框架**: Codex 原生 Task 工具（Sub-agents）

## 项目结构

```
Codex-trading-agents/
├── AGENTS.md              # 本文件（项目总配置）
├── main_v2.py             # watchlist 每日报告入口
├── watchlist.json         # 自选股列表（修改标的只动这里）
├── strategy.json          # 主策略与扫描配置
├── freeze_v3_strategy.json# 冻结 V3 规则
├── requirements.txt       # Python 依赖
├── src/                   # 每日运行核心（定时任务只允许依赖这里）
│   ├── market_scanner.py          # 收盘扫描、模拟计划、台账
│   ├── scheduled_v3_reporter.py   # V3 日报
│   ├── forward_paper_trading_v3.py# 冻结 V3 forward paper 记录
│   ├── quant_core.py              # 共享量化库（缓存/可交易性/股票池/V3 过滤）
│   ├── data_fetcher.py / analyzer.py / reporter.py / notifier.py
│   └── reviewer.py / selector.py / synthesizer.py / strategy_health.py / strategy_learning.py
├── research/              # 研究代码（已收敛；不得被 src/ 或定时任务导入）
├── archive/               # 弃用入口与 legacy 策略（仅回滚/对照用）
├── prompts/               # sub-agent 与 Hermes 指令
├── scripts/               # shell 入口（推送、Hermes steward）
├── output/YYYY-MM-DD/     # 每日产出
├── logs/                  # 运行日志
└── docs/                  # 文档（含 refactor_2026-07-03.md 重构说明）
```

**分层约束（2026-07-03 重构确立）**：

- `src/` 是唯一的每日运行层：GitHub Actions 与 shell 脚本只能调用 `src/` 和 `main_v2.py`。
- `research/` 内的模块可以导入 `src/`，但 `src/` 不得反向导入 `research/`。
- 运行路径需要的共享函数统一放 `src/quant_core.py`，不要再从研究轮次文件里 import。
- 新研究实验放 `research/`，一次性诊断脚本也放 `research/`，不要放进 `src/`。

## Sub-agent 职责分工

### 1. 数据抓取 Agent (`data_agent`)

**输入**: `watchlist.json` 中的股票代码列表
**职责**:
- 调用 akshare 抓取每只股票近 60 个交易日的日频数据（OHLCV）
- 抓取基本面快照：最新 PE、PB、市值、ROE、营收同比增速
- 数据校验：检查缺失值、价格跳空（>10% 单日变动需标记）
- 输出标准化 CSV 到 `output/YYYY-MM-DD/raw/`

**失败处理**: 任一标的抓取失败时，记录到日志但不中断整体流程，在最终报告中明确标注"数据缺失"。

**禁止**: 生成、估算、插值任何未获取到的数据。

### 2. 分析 Agent (`analysis_agent`)

**输入**: 数据抓取 agent 产出的 CSV 文件
**职责**:
- **技术面**: 计算 MA5/MA10/MA20、MACD、RSI(14)、布林带
- **基本面**: 对比 PE/PB 与行业中位数（如能获取）、分析 ROE 和营收增速趋势
- **结合视角**: 识别技术面信号与基本面状况是否一致（如"技术面超买但基本面改善" vs "技术面超卖但基本面恶化"）
- 输出结构化 JSON 到 `output/YYYY-MM-DD/analysis/`

**产出格式约定**:
```json
{
  "stock_code": "000001.SZ",
  "technical": { "signals": [...], "indicators": {...} },
  "fundamental": { "metrics": {...}, "observations": [...] },
  "synthesis": "技术面与基本面的综合观察（描述性语言）",
  "data_quality": "complete" | "partial" | "failed"
}
```

**语言风格**:
- ✅ "MACD 金叉形成于 10 日前，目前红柱持续放大"
- ✅ "PE 处于近三年 80% 分位，估值相对偏高"
- ✅ "触发区间由前一日振幅和最新价自动计算，次日需人工复核承接"
- ✅ "跌破规则止损位则按模拟计划退出"
- ❌ "目标价 XX 元"

### 3. 报告 Agent (`report_agent`)

**输入**: 分析 agent 产出的 JSON 文件
**职责**:
- 整合所有标的分析为单份 Markdown 报告
- 报告结构：免责声明 → 市场概览 → 逐股分析 → 异常提示 → 数据质量总结 → 免责声明
- 输出到 `output/YYYY-MM-DD/report.md`

**报告语言**: 中文（便于阅读）
**代码/日志/文件名**: 英文（行业习惯）

## Disclaimer 模板

所有报告开头和结尾必须包含：

```
---
⚠️ **免责声明**
本报告由 AI 工作流自动生成，用于个人模拟交易、复盘与技术探索。
所有分析基于公开历史数据，不保证准确性或完整性。
报告中的交易参数由规则自动生成，执行前需人工复核。
投资有风险，真实交易风险自担。
---
```

## 主 Agent 调度规则

当用户请求"跑今日分析"或类似指令时，主 agent 的标准流程：

1. 读取 `watchlist.json` 确认标的列表
2. 创建当日输出目录 `output/YYYY-MM-DD/`
3. **并行调用** `data_agent` 抓取所有标的数据
4. 数据抓取完成后，**并行调用** `analysis_agent` 对每只标的独立分析
5. 所有分析完成后，**串行调用** `report_agent` 整合为最终报告
6. 打印报告路径，结束会话

**关键约束**:
- Sub-agent 之间**不直接通信**，所有数据通过文件系统（JSON/CSV）传递
- 主 agent 负责聚合和错误兜底
- 任一 sub-agent 失败时，主 agent 决定是否继续（数据抓取失败 → 跳过该标的；分析失败 → 中断流程）

## 代码风格约定

- 所有函数加 type hints
- 所有对外接口写 docstring（中文说明 + 英文参数）
- 关键计算步骤打 log（使用 Python `logging` 模块，不要用 print）
- 错误处理：明确捕获预期异常（网络超时、数据缺失），不要用裸 `except`
- **文件 I/O 编码规范**：所有文件读写必须显式指定 `encoding`，禁止依赖系统默认值
  - CSV：`encoding="utf-8-sig"`（带 BOM，兼容 Excel 直接打开不乱码）
  - JSON：`encoding="utf-8"` + `json.dump(..., ensure_ascii=False)`（保留中文原文，避免 `\uXXXX` 转义）
  - 日志文件：`encoding="utf-8"`
  - 其他文本读取：`encoding="utf-8"`

## 开发工作流

**阶段 1（当前）**: 串行版本跑通
- 先不用 sub-agent，在主会话里依次调用三个工具模块
- 验证数据抓取、分析、报告生成的业务逻辑

**阶段 2**: 拆分为 sub-agent
- 将三个模块的 prompt 独立写到 `prompts/` 目录
- 主 agent 用 Task 工具并行调度

**阶段 3**: 工程化
- 加入运行日志和错误恢复
- 编写 SOP 文档（`docs/sop.md`），记录 sub-agent 协作的最佳实践与踩坑经验

## 面试/复盘可提及的关键收获

（在 `docs/sop.md` 中持续积累）
- Sub-agent 上下文隔离的影响与应对
- 任务拆分粒度的权衡（太细 → 协调成本高；太粗 → 失去并行优势）
- 结构化数据传递 vs 自然语言传递的准确性差异
- AI 工作流在"可复用 SOP"层面的沉淀价值

## 禁止事项（硬性红线）

- ❌ 编造未成功获取的数据
- ❌ 声称收益确定、胜率确定或风险可控
- ❌ 将本工作流产出对外传播为投资顾问服务
- ❌ 连接实盘交易接口
- ❌ ST/*ST 股票一概不碰（2026-07-03 用户明令）：扫描、候选、回测、forward paper
  全链路剔除；回测应使用时变 ST 状态（BaoStock isST 日频序列），静态快照只能作过渡
- ❌ 使用 fuyao 源指数数据（已证实 2022-2026 双向失真达 ±44%）；指数数据一律以
  BaoStock 序列为准（`data/expanded/index_daily_baostock.csv`），失真原件已隔离备份
