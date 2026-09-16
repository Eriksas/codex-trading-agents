# Codex Trading Agents - 项目配置

## 项目定位

本项目是 **AI Agent 辅助策略研究与分析框架**。通过数据检查、指标诊断、候选假设、Python 定量验证、反方检查和人工确认，组织可验证、可复盘的研究。

**不是**：自动化实盘交易系统、面向公众的投资顾问工具、生产级金融产品
**是**：个人研究与求职展示项目；保留数据抓取、收盘扫描、模拟计划、因子研究、回测和前向观察，展示分析方法、工具分工和证据边界。

**AI 提出假设，程序验证结论。** 方法见 [docs/analysis_methodology.md](docs/analysis_methodology.md)，运行机制与未实施的权限控制见 [docs/agent_harness.md](docs/agent_harness.md)。

## 核心原则

1. **产出定位为个人模拟交易计划**
   报告和扫描产物可包含触发区间、止损、第一止盈、仓位、计划等字段，但这些字段必须由明确规则生成，并标注为个人模拟与复盘用途。
   Agent 可以描述规则化执行条件，但不得声称收益确定、胜率确定或替用户连接实盘交易。

2. **宁可失败，不要编造**
   - 数据抓取失败时，必须明确报错，禁止用随机数或历史均值"填充"
   - 技术指标计算异常时，必须在报告中标明"计算异常，已跳过"
   - 不确定的结论用"数据显示…"而非"预测…"

3. **每次运行留痕**
   日报产出存入 `output/YYYY-MM-DD/`，运行日志在 `logs/`；研究结果各有独立 `output/` 子目录，Hermes 草案在 `strategy_experiments/`，主模拟台账与经验记录在 `data/`。已有独立前向观察使用 `forward_state/` 跟踪记录，不回写历史。

## 分析与证据的共同规则

- Agent 可以读取数据、总结信息、拆解问题、提出候选假设和实验建议、寻找反例、整理报告。
- Agent 不得编造缺失数据、把自然语言推测当数值结论、把相关性直接写成因果、因为短期收益好就建议直接晋级。
- 关键数值必须调用 Python / 现有研究脚本计算。文字与程序冲突时核对日期、样本、单位和口径，再修正文字；若程序或数据可疑，暂停结论并登记待重算问题，不能挑选有利数字。
- 每个重要判断注明来源文件、日期范围、样本量和局限。缺来源或样本量时明确不足，不给高置信度策略结论。
- 候选假设需列支持证据、反对证据、还需进行的分析及当前置信程度。反方意见也要接受证据核查。
- 重要策略变化必须经过独立实验、历史验证、反方检查与明确人工确认；实验草案、短期表现和观察样本达标均不构成自动晋级。
- 样本门槛沿用各研究协议，不把健康监控的记录数、bounce 事件数和 gate 观察天数混为一谈。
- 新分析可用 Rejected（证据不支持）、Inconclusive（证据不足）、Promising（初步支持）、Validated（通过预设主要验证）分级；不回填历史状态，不把 Validated 当收益承诺。
- 既有因子代码的确定性缺失处理需披露口径，不得包装成真实观测；缺失行情不能由 Agent 估算补造。

## 技术栈

新读者的主展示入口为 `python main.py demo`；已有数据诊断为 `python main.py diagnose --date YYYY-MM-DD`，见 [docs/quickstart.md](docs/quickstart.md)。它复用健康统计、只读输入，产物写入独立的 `output/diagnosis/`；不调用模型，可导入待人工评审的解释。原每日入口继续保持原行为。

- **语言**: 日报环境建议 Python 3.12；独立 Eval 仅需 Python 3.11+ 标准库
- **数据源**: akshare（A股日频数据）
- **数据处理**: pandas, numpy
- **技术指标**: `src/analyzer.py` 使用 pandas-ta
- **运行方式**: 默认 Python 直接调用与模板；可选 Claude CLI 综合观察、Hermes CLI 诊断。历史 Task 角色设计不代表当前默认入口在启动多个 Agent。

## 项目结构

```
Codex-trading-agents/
├── AGENTS.md              # 本文件（项目总配置）
├── main.py                # 统一健康诊断与离线展示入口
├── main_v2.py             # watchlist 每日报告入口
├── watchlist.json         # 自选股列表（修改标的只动这里）
├── strategy.json          # 主策略与扫描配置
├── freeze_v3_strategy.json# 冻结 V3 规则
├── requirements.txt       # Python 依赖
├── src/                   # 主日报与扫描运行核心
│   ├── market_scanner.py          # 收盘扫描编排、CLI 与历史接口兼容
│   ├── scanner_config/data/rules.py # 扫描配置、数据缓存、筛选与计划
│   ├── scanner_backtest/ledger/report.py # 回测、台账、报告
│   ├── scanner_utils.py          # 扫描器内部的格式化与 CSV 辅助
│   ├── scheduled_v3_reporter.py   # V3 日报
│   ├── forward_paper_trading_v3.py# 冻结 V3 forward paper 记录
│   ├── quant_core.py              # 共享量化库（缓存/可交易性/股票池/V3 过滤）
│   ├── data_fetcher.py / analyzer.py / reporter.py / notifier.py
│   └── reviewer.py / selector.py / synthesizer.py / strategy_health.py / strategy_learning.py
├── research/              # 研究及独立前向观察代码；不得被 src/ 导入
├── archive/               # 弃用入口与 legacy 策略（仅回滚/对照用）
├── prompts/               # sub-agent 与 Hermes 指令
├── scripts/               # shell 入口（推送、Hermes steward）
├── output/YYYY-MM-DD/     # 每日产出
├── logs/                  # 运行日志
├── eval/                  # 独立规则评测，不进入每日运行路径
├── examples/diagnosis/    # 明确标记的合成教学样例
├── tests/                 # 主诊断流程的离线测试
└── docs/                  # 方法、案例、历史研究与操作说明
```

**分层约束（2026-07-03 重构确立）**：

- `src/` 和 `main_v2.py` 是主日报与扫描层。后续已有的 `.github/workflows/daily-forward-observation.yml` 独立调用 `research/forward_*.py` 记录前向样本；这是现状例外，不把研究搜索或调参加入主日报。
- `research/` 内的模块可以导入 `src/`，但 `src/` 不得反向导入 `research/`。
- 运行路径需要的共享函数统一放 `src/quant_core.py`，不要再从研究轮次文件里 import。
- 扫描器内部职责已拆到 `src/scanner_*.py`，见 [第二阶段说明](docs/scanner_refactor.md)。这些组件不得反向导入 `market_scanner.py` 或研究轮次；私有格式化/CSV 辅助放 `scanner_utils.py` 以避免 quant_core 既有依赖造成循环，不扩展为新的研究公共库。旧调用经 `market_scanner` 兼容导入，配置仍通过 `_set_active_config` 切换。
- 新研究实验放 `research/`，一次性诊断脚本也放 `research/`，不要放进 `src/`。

## Sub-agent 职责分工

以下是协作时的角色约定，保留历史设计以便复盘；当前 `main_v2.py` 的抓取、数值分析和报告由 Python 函数直接完成，不必启动对应 Agent。只有需要独立文本解释且用户启用时才使用可选综合观察，Hermes 模式另见 `HERMES.md`。

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
- **技术面**: 调用 `src/analyzer.py` 计算 MA5/MA10/MA20、MACD、RSI(14)、布林带，不由模型心算
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

当用户请求"跑今日分析"或类似指令时，沿用 `main_v2.py` 的当前流程：

1. 读取 `watchlist.json` 确认标的列表
2. 创建当日输出目录 `output/YYYY-MM-DD/`
3. 调用 Python 抓取函数，再调用分析函数计算指标
4. 默认用 `src/synthesizer.py` 模板生成综合观察；明确启用 `--llm` 才调用文本 Agent，失败由模板兜底
5. 调用 `src/reporter.py` 整合报告；消息推送只按用户请求和既有配置执行
6. 打印报告路径，结束会话

**关键约束**:
- 数据和解释通过 JSON/CSV 文件交接；主入口负责聚合状态与错误兜底。
- 数据缺失、计算失败要在状态和报告中保留，不将失败改写为完整成功。
- 不为了“Multi-Agent”名称把纯计算重新拆成 Agent。

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

当前默认直调、可选综合观察与 Hermes 模式分别按现有入口维护。早期分阶段试验经过保留在 `docs/sop.md` 与 `archive/`，不把曾经的目标写成已经实现的能力。

修改前检查 `git status` 并保护用户未提交内容；独立实验不修改主策略参数、历史回测结果或前向记录。新增 Eval 验证用 `python eval/run_eval.py` 和 `python -m unittest discover -s eval -p "test_*.py"`，这不代表真实模型测试通过。

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
