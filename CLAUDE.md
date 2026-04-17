# Claude Trading Agents - 项目配置

## 项目定位

本项目是**AI工具落地探索性 Demo**，用于验证 Claude Code Sub-agents 在金融数据协作工作流中的适用边界。

**不是**：自动化交易系统、投资顾问工具、生产级金融产品
**是**：个人学习项目，探索多 agent 任务分工、上下文传递、产出聚合的工程模式

## 核心原则

1. **产出定位为"研究性数据分析"，非"投资建议"**
   所有报告必须在开头和结尾标注免责声明（见下方 Disclaimer 模板）。
   Agent 描述市场现象和技术指标读数，但**不使用**"建议买入/卖出/持有"等操作性语言。

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
- **Agent 框架**: Claude Code 原生 Task 工具（Sub-agents）

## 项目结构

```
claude-trading-agents/
├── CLAUDE.md              # 本文件（项目总配置）
├── watchlist.json         # 自选股列表（修改标的只动这里）
├── requirements.txt       # Python 依赖
├── src/
│   ├── data_fetcher.py   # 数据抓取 agent 的工具函数
│   ├── analyzer.py        # 分析 agent 的工具函数
│   └── reporter.py        # 报告 agent 的工具函数
├── prompts/
│   ├── data_agent.md     # 数据抓取 sub-agent 的指令
│   ├── analysis_agent.md # 分析 sub-agent 的指令
│   └── report_agent.md   # 报告 sub-agent 的指令
├── output/
│   └── YYYY-MM-DD/       # 每日产出
├── logs/                  # 运行日志
└── docs/
    └── sop.md            # AI 工作流 SOP（项目复盘，面试可展示）
```

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
- ❌ "建议逢低买入"
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
本报告由 AI 工作流自动生成，仅用于个人学习与技术探索，不构成任何投资建议。
所有分析基于公开历史数据，不保证准确性或完整性。
报告中的观察仅为数据描述，不应作为投资决策依据。
投资有风险，入市需谨慎。
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

- ❌ 输出具体买卖点位或价格目标
- ❌ 使用"建议""推荐""应该"等操作性词汇
- ❌ 编造未成功获取的数据
- ❌ 将本工作流产出对外传播为投资参考
- ❌ 连接实盘交易接口
