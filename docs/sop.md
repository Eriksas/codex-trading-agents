# Claude Trading Agents — AI 工作流 SOP

## 概览

本文档记录在构建"Claude Code Sub-agents 金融数据分析工作流"过程中的实际观察、踩坑经验和关键决策。内容面向技术复盘和面试展示，不包含任何投资建议。

---

## 当前收盘扫描闭环：扫描 → 台账 → 复盘 → 推送

新增复盘模块后，每日流程拆为两个清晰层次：

| 层次 | 实现 | 职责 |
|------|------|------|
| 确定性事实层 | `src/reviewer.py` | 读取 `ledger_updates.csv`、行情快照和历史 K，判断触发、过期、止损、第一止盈、持有期结束、最大顺向/逆向波动 |
| 文字解读层 | 未来 `review_agent` | 只读取 `review_summary.json` 与 `review_details.csv`，做中文归因和复盘摘要，不重新计算状态 |

这个边界的目的：

- 复盘状态由代码生成，避免 LLM 改写交易事实
- 数据缺失时明确标记 `missing_data`，不做填补
- agent 未来只负责“把结构化事实讲清楚”，不负责生成止损、止盈、仓位或台账动作

每日扫描新增产物：

| 文件 | 用途 |
|------|------|
| `review_details.csv` | 逐笔复盘明细，适合后续统计策略信号质量 |
| `review_summary.json` | 给日报、飞书推送和未来 agent 使用的结构化汇总 |
| `review_report.md` | 独立复盘 Markdown 报告 |

---

## 策略进化机制：健康监控 → 管家诊断 → 影子实验

固定策略参数只能保证可复现，不能保证长期有效。因此本项目采用“先监控、再诊断、后实验”的迭代方式。

| 层次 | 实现 | 权限 |
|------|------|------|
| 策略健康监控 | `src/strategy_health.py` | 聚合滚动窗口指标，生成健康提示和候选实验假设 |
| 策略管家 Agent | Hermes + `prompts/strategy_steward_agent.md` | 读取健康报告，解释漂移与失效模式，只给实验建议 |
| 策略学习记忆 | `src/strategy_learning.py` + `prompts/hermes_market_learning_skill.md` | 把反复出现的证据沉淀为可审计经验 |
| 影子实验 | `strategy_experiments/YYYY-MM-DD/*.json` | 新参数先回测和模拟复盘，不替换主策略 |
| 人工升级 | 主会话 + 用户确认 | 只有人工确认后才合并到 `strategy.json` |

健康监控默认输出：

| 文件 | 用途 |
|------|------|
| `strategy_health_summary.json` | 给 agent 和日报使用的结构化体检数据 |
| `strategy_health_report.md` | 人类可读策略健康报告 |
| `strategy_health_tags.csv` | 按策略标签拆分的表现摘要 |
| `data/strategy_learning/learning_memory.md` | Hermes 每日读取的长期经验记忆 |

关键边界：

- `strategy_health.py` 不修改参数，只统计事实
- `strategy_steward_agent` 不改主策略，只提出候选实验
- `strategy_learning.py` 只更新学习记忆，不改策略和台账
- 样本不足时必须明确提示“不应基于该窗口单独调参”
- 所有实验必须通过回测、模拟复盘和人工确认后才能升级

当前地基规则：

- 复盘日报里的 `triggered_count` 表示“今日新触发”，策略健康里的
  `triggered_count/trigger_rate` 表示滚动窗口内“已确认入场”的历史口径。
- `market_position_multiplier` 是市场环境目标系数；
  `effective_position_multiplier` 是四舍五入与仓位上限后的实际生效系数。
- 同标的已有 active/pending 时，默认 `same_symbol_active_policy=skip`，
  新候选只记录跳过原因，不新增 pending。
- 止损回测保留理想止损价收益，同时输出滑点版收益和日内最低价压力测试；
  Hermes 判断止损问题时必须区分“参数宽度”与“成交假设/跳空风险”。

Hermes 手动运行入口：

```bash
scripts/run_strategy_steward.sh --date YYYY-MM-DD --mode daily
scripts/run_strategy_steward.sh --date YYYY-MM-DD --mode deep
scripts/run_strategy_steward.sh --date YYYY-MM-DD --mode critic
scripts/run_strategy_steward.sh --date YYYY-MM-DD --mode experiment
```

自动扫描可选接入：

```bash
python3 src/market_scanner.py --date YYYY-MM-DD --steward --steward-mode daily
```

飞书摘要只推送 Hermes 模式、状态、报告路径和最多 3 条核心诊断，不推送完整长文。

---

## 阶段 1：串行基线（直接函数调用）

### 技术选型

| 模块 | 工具 | 备注 |
|------|------|------|
| OHLCV 数据 | akshare `stock_zh_a_hist`（东方财富） | 主力源，连续请求时限流 |
| PE/PB | akshare `stock_zh_valuation_baidu` | 百度股市通，稳定 |
| 财务指标 | akshare `stock_financial_abstract_ths` | 同花顺，THS 数据旧→新排列需取 `iloc[-1]` |
| 技术指标 | pandas-ta 0.4.71b0 | Python 3.14 需配套 numba 0.65 |

### 踩坑记录

**坑 1：东方财富反爬**
- 现象：单次调用正常，连续 5 只股票顺序请求时全部 `RemoteDisconnected`
- 原因：东方财富对高频 IP 请求实施连接层封禁，不返回 HTTP 错误码，直接断连
- 解法：实现三源 fallback（东方财富 → 新浪财经 → 腾讯财经）+ 指数退避重试
- 教训：不要假设同一接口在批量和单次场景下行为一致

**坑 2：THS 财务数据排序方向**
- 现象：`stock_financial_abstract_ths(indicator="按年度")` 返回 `report_date: 1989`（最旧数据）
- 原因：THS 接口数据从旧到新排列，`iloc[0]` 取到了历史第一条而不是最新
- 解法：改用 `iloc[-1]`；同时发现 `按年度` 和 `按报告期` 均为旧→新
- 教训：对外部数据接口的排序方向不要有预设

**坑 3：`python3 -m json.tool` 导致的误判**
- 现象：fetch_summary.json 中中文显示为 `\u5e73\u5b89\u94f6\u884c`，误以为乱码
- 原因：`python3 -m json.tool` 默认 `ensure_ascii=True`，重新序列化时转义了所有非 ASCII
- 实际：文件本身 UTF-8 正常，`cat` 直接看是正确的
- 教训：诊断乱码要看文件本身（`cat`），不要通过会二次处理的工具来判断

**坑 4：本地缓存写入编码**
- `pd.read_csv()` 没有显式指定 `encoding` 时，在某些系统上会用系统默认编码读取 `utf-8-sig` 文件导致 BOM 字符混入列名
- 解法：全项目统一规范，所有 I/O 显式指定 `encoding`（CSV 用 `utf-8-sig`，JSON 用 `utf-8`）

**坑 5：pandas-ta 对列名大小写敏感**
- pandas-ta 要求输入 DataFrame 列名首字母大写（`Open/High/Low/Close/Volume`）
- akshare 返回的原始列名不统一（中文列名 vs 英文小写），必须在调用前显式重命名

---

## 阶段 2.1：引入 Sub-agent（analysis 模块）

### 架构决策

Sub-agent 通过 `claude --print` CLI 子进程调用，而不是用 Claude Code 的 Task 工具 API。

**为什么用 subprocess 而不是 Task API？**
- `TaskCreate` 等工具是 Claude Code 会话内的工具，不能从 Python 脚本里直接调用
- subprocess 调用 `claude -p` 使得 `main.py` 成为可独立运行的 Python 脚本，不依赖主 Claude Code 会话的上下文
- 代价是每次 sub-agent 启动需要初始化完整的 Claude 运行时（~20s 冷启动），相比直接函数调用慢 15 倍以上

### Prompt 工程：自包含原则

**核心约束**：sub-agent 看不到主会话的任何上下文（CLAUDE.md、对话历史、变量状态），它只知道你在 prompt 里写了什么。

因此 analysis_agent.md 必须包含：
1. **角色定位**：我是谁，我做什么，我不做什么
2. **任务参数**：所有路径的具体值（不是变量名，是实际路径字符串）
3. **执行步骤**：具体的 bash 命令，不是"分析一下"，而是"执行这段 Python"
4. **输出格式**：最后一行必须是且仅是什么格式的 JSON
5. **禁止事项**：明确写出来，不能假设 sub-agent 继承了主会话的约束

**Prompt 太短会怎样（推断）**：
- 如果只写"分析 600519 的 OHLCV 数据"，sub-agent 不知道文件在哪里、用什么函数、输出到哪里
- 极可能自己决定实现方案（可能手写 MA 公式而不是用 pandas-ta）
- 输出格式会随机（可能是 Markdown 报告而不是 JSON）

**Prompt 太长的代价**：
- 本次 prompt 约 2778 字符，sub-agent 运行 20-24s，包含约 18s 冷启动 + 2-4s 实际计算
- Prompt 再长 10 倍，冷启动时间基本不变，但 token 消耗和成本线性增加
- 对于纯确定性计算任务，prompt 应精确描述执行命令而非用自然语言描述目标

### 对比实验结果

| 指标 | 阶段 1（直接调用） | 阶段 2.1（sub-agent） |
|------|------------------|----------------------|
| 5 只股票分析耗时 | ~1.4s | ~100s |
| 每只 sub-agent 耗时 | - | ~20s（其中 ~18s 冷启动） |
| 输出 JSON 差异 | 基准 | **完全 IDENTICAL**（diff 为空） |
| schema 验证 | 5/5 通过 | 5/5 通过 |
| sub-agent 自作主张 | N/A | 无，严格遵循输出格式 |

**关键观察**：5 只股票的 analysis JSON 与阶段 1 直接函数调用的结果完全一致（排除时间戳后 diff 为空）。这验证了 sub-agent 调用的是相同的 `analyzer.analyze_stock()` 函数，没有产生任何"创意性"偏差。

### Sub-agent 自作主张的风险

本次未出现，但有两处潜在风险点：

1. **验证步骤**：sub-agent prompt 中要求执行三步（确认文件 → 运行分析 → 验证输出），测试发现 sub-agent 会忠实执行所有步骤，并在最终输出前加了一行 "✓ 输出文件验证成功，所有必需字段齐全。" 的说明文字——这不影响 JSON 解析（`_parse_agent_status` 从末尾逆序找第一个合法 JSON），但说明 sub-agent 有时会在"最后一行 JSON"前加说明。
2. **输出格式漂移**：如果 prompt 中的"输出要求"不够严格（例如只说"返回状态"），sub-agent 可能返回 Markdown 代码块包裹的 JSON，导致 `json.loads()` 失败。本次通过明确写"不加代码块、不加说明文字"来规避。

**因此 `_parse_agent_status` 的逆序扫描策略是必要的**：不能假设最后一行一定是纯 JSON。

### 性能 vs 可并行性权衡

当前实现是串行的（每只股票等前一只完成）。如果改为并行：
- 5 只同时跑：理论上总时间从 100s 降至 ~25s（单只时间）
- 但 5 个并发 claude 进程对资源和 API 并发有压力
- 阶段 2.2 应实现并行版本并测量实际加速比

### 上下文隔离的影响

**优点**：
- 每个 sub-agent 独立运行，一只股票失败不影响其他
- 天然隔离可以并行化
- sub-agent 的 token 消耗不积累到主会话

**代价**：
- CLAUDE.md 的约定对 sub-agent 不自动生效，必须在 prompt 中重新声明禁止事项
- 主会话积累的上下文（如"东方财富今天在限流"）sub-agent 不知道——但这里影响不大，因为 sub-agent 只读文件，不调用网络
- 如果 analyzer.py 有 bug，5 个 sub-agent 会各自发现并报告，主 agent 需要聚合处理

---

## 架构选择依据

本项目采用"Sonnet 主调度 + Haiku 执行 analyzer sub-agent"的层级架构，有明确的官方文档支撑，不是自行拍脑袋的设计。

Anthropic 官方文档（Claude Code sub-agents 章节）将以下模式描述为典型用法：

> 使用能力更强的模型（如 Sonnet）作为 orchestrator，编排多个较小模型（如 Haiku）并行完成子任务。orchestrator 负责任务分解、结果聚合和错误兜底；sub-agent 只负责执行单一、明确的计算任务。

本项目对此模式的映射关系：

| 官方模式角色 | 本项目实现 | 职责 |
|------------|-----------|------|
| Orchestrator（Sonnet） | `main.py` 主进程 + 当前 Claude Code 会话 | 读取 watchlist、调度 sub-agent、验证产出、生成报告 |
| Sub-agent（Haiku） | `claude --model haiku -p <prompt>` 子进程 | 对单只股票执行 `analyzer.analyze_stock()`，返回状态 JSON |
| 任务分解边界 | 按股票代码分片 | 每只股票一个独立 sub-agent，互不依赖 |
| 结果聚合 | `reporter.generate_report()` | 读取所有 `analysis/*.json`，合并为 Markdown 报告 |

**选择 Haiku 执行 analyzer 的理由**：analyzer sub-agent 的任务是纯确定性计算（运行 `python3 -c "from analyzer import analyze_stock; ..."`），不需要复杂推理，Haiku 足够胜任，同时显著降低 token 成本。Sonnet 的推理能力保留给需要综合判断的 orchestrator 层和后续的 synthesis sub-agent。

---

---

## 阶段 2.2：并行化与两种 Sub-agent 实现路径对比

### V1–V4 实测对比表

| 版本 | 实现方式 | 5 只股票总耗时 | 单只分析耗时 | 并发度 | 输出一致性 |
|------|---------|--------------|------------|--------|----------|
| V1 | 直接函数调用（`analyzer.analyze_stock()`） | **1.4s** | ~0.3s | 1（串行） | 基准 |
| V2 | 串行 subprocess（`claude -p`） | **~109s** | ~20s | 1 | 与 V1 完全 IDENTICAL |
| V3 | 并行 subprocess（ThreadPoolExecutor, max_workers=5） | **40.6s**（其中分析阶段 25s，数据抓取 16s） | ~20s | 5 | 与 V1 完全 IDENTICAL |
| V4 | 原生 Agent 工具（Claude Code `Agent` tool） | **失败** | N/A | 2（尝试并行） | N/A |

**V3 实测细节**（`main.py` 并行版）：
- 5 个 subprocess 同时启动，各自耗时 19.7s / 18.8s / 22.0s / 24.0s / 25.1s
- 总 wall time 25.1s（最慢那只决定），相比串行 ~100s 加速约 4×
- 无速率限制、无日志写冲突（日志文件按 `agent_{code}_{date}.log` 各自独立）

### Sub-agent 的两种实现路径

#### 路径 A：subprocess CLI（`main.py`）

```
Python 主进程
└── subprocess.Popen("claude --print --model haiku ...")  × N
    └── 独立 claude 进程（独立 API session，独立资源）
```

- **优点**：完全隔离，一只失败不影响其他；可用 `ThreadPoolExecutor` 并行；可在任何 Python 环境中运行，不依赖 Claude Code 会话
- **缺点**：每次启动约 18s 冷启动（新进程初始化 + LLM 上下文加载）；计费为独立 API 调用；5 只并发 = 5 个独立 claude 进程同时跑

#### 路径 B：原生 Agent 工具（`main_native_task.py`）

```
Claude Code 主会话（Sonnet）
└── Agent tool call × N（同会话内派发）
    └── sub-agent（无冷启动开销，共享运行时）
```

- **理论优点**：无 18s 冷启动，sub-agent 在同一 Claude Code 运行时内启动，速度应快得多；可在同一轮次发出多个 Agent 调用实现并行
- **实际限制（本次实验）**：触发了 Claude Code 的会话级用量上限（"You've hit your limit · resets 4pm"），2 个并行 Agent 调用均立即失败，耗时仅 343ms 和 4945ms（连工具都没来得及调用）

**V4 失败的关键原因**：Agent tool 的用量计入主会话配额，而 subprocess 方式的 `claude --print` 调用是独立的 API session，受独立配额限制。在交互式 Claude Code 会话中密集调用 Agent tool 会更快消耗会话配额。

### 并行化踩到的坑

**坑 1：并行时日志没有冲突（预期中的坑没出现）**
- 预判：多个线程并发写日志可能冲突
- 实际：每个 sub-agent 的日志文件按 `agent_{code}_{date}.log` 命名，天然隔离，无需额外锁

**坑 2：`as_completed` 返回顺序不确定**
- 5 只股票提交后，`as_completed` 按完成顺序返回，不是提交顺序
- 本项目无影响（每只独立写文件），但若依赖顺序汇总结果需注意

**坑 3：V4 的配额墙（本次最重要的发现）**
- 在同一 Claude Code 会话中连续或并发调用 Agent tool 会迅速触发会话级别的速率/用量上限
- 这与 subprocess 方式根本不同：subprocess 绕开了主会话的配额，每个 claude 进程用自己的 session
- 结论：原生 Agent 工具更适合"偶发性"子任务派发（如复杂代码审查、一次性文档生成），不适合批量 × N 的高频派发场景

### 何时该用 Sub-agent，何时直接函数调用

基于本次实验数据：

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| **纯确定性计算**（技术指标、数据转换） | V1 直接函数调用 | 无冷启动，1.4s vs 40s，且输出完全一致 |
| **需要 LLM 推理的子任务**（synthesis 解读、异常诊断） | V3 并行 subprocess | 计算型任务不需要 LLM，但若必须用 LLM，subprocess 配额独立，不影响主会话 |
| **单次复杂子任务**（代码审查、一次性分析） | V4 原生 Agent | 无冷启动优势，但不适合 ×5 批量调用 |
| **大规模批量**（>10 只股票、需稳定并发） | V3 并行 subprocess | Agent tool 配额不够，subprocess 可控且隔离 |

**核心结论**：Sub-agent 的额外开销（subprocess 模式约 18s 冷启动/只）在这个项目中完全不值得——analyzer 的实际计算只需 0.3s，18s 的冷启动是 60 倍开销。Sub-agent 的价值在于"需要 LLM 推理"的任务（synthesis 字段填写、自然语言报告润色），而不是确定性计算。

这正是阶段 1 → 阶段 2 的最重要发现：**把确定性计算也包进 sub-agent 是过度设计**。正确的架构应该是：确定性计算直接调用 → LLM 解读才用 sub-agent。

---

## 阶段 2.3：架构修正——从错误中学到的正确分工

### 问题来源

阶段 2.1/2.2 将纯确定性计算（技术指标、数据聚合）包进 sub-agent，导致 60× overhead。
阶段 2.3 做了架构修正：明确区分"该用 sub-agent 的任务"与"不该用的任务"。

### 正确架构（main_v2.py）

```
步骤 1：data_fetcher.run_data_fetch()   → 直接函数调用（数据抓取，0.3s/只）
步骤 2：analyzer.run_analysis()         → 直接函数调用（技术指标 + 基本面，0.3s/只）
步骤 3：synthesis sub-agent × N 并行    → subprocess claude haiku（LLM 推理，~30s/只，并行后总 34s）
步骤 4：reporter.generate_report()      → 直接函数调用（报告模板渲染，<0.1s）
```

**v2 运行时间（2026-04-17，5 只股票）：**

| 步骤 | 耗时 | 方式 |
|------|------|------|
| 步骤 1 数据抓取 | ~12s（含网络） | 直接调用 |
| 步骤 2 计算 | **1.5s** | 直接调用 |
| 步骤 3 synthesis | **34.2s**（5 只并行） | subprocess haiku |
| 步骤 4 报告 | <0.1s | 直接调用 |
| **总计** | **47.3s** | |

对比：V3 版（calculator 也走 sub-agent）：40.6s，但 synthesis 字段是占位符 `pending_llm_interpretation`。

v2 花了多 7s，但产出了真实的 LLM synthesis 内容——这个开销完全合理。

### synthesis 字段设计

**prompt 设计要点（`prompts/synthesis_agent.md`）：**
1. **三维度结构**：技术面现状 → 基本面现状 → 一致性描述（防止 agent 只写一个维度）
2. **长度约束**：80～150 字（太短则内容不够，太长则信噪比低）
3. **语言禁区**：明确列举禁止词汇（"建议""推荐""看涨"等），而非只描述允许的
4. **幻觉防护**：禁止编造文件中不存在的数据；data_quality=failed 时直接返回固定文本

**茅台 synthesis 示例（2026-04-17）：**
> 贵州茅台收盘价1462.84元，MA5近期死叉MA10，技术面呈弱势，价格位于布林带内中上部。MACD仍在零轴上方但动量减弱，RSI处于中性区间（55.24）。基本面方面，PE处于近三年30%分位，估值相对偏低；但营收同比下滑1.2%，净利润同比下滑4.53%。ROE维持高位（32.53%），显示盈利能力仍强。技术面弱势与基本面利润下滑方向一致，低估值形成支撑。
>
> —— 182 字，LLM 准确引用了 JSON 中的数值，无幻觉

### Sub-agent 任务分工准则（更新版）

**适合 sub-agent（用 subprocess claude haiku）：**
- 需要 LLM 自然语言理解或生成的任务（synthesis、异常诊断解读、摘要）
- 每只处理时间 > 10s（sub-agent 冷启动 ~18s 可被覆盖）
- 任务间完全独立（无数据依赖）

**不适合 sub-agent（直接函数调用）：**
- 确定性计算（技术指标、分位数、聚合）
- 简单文件 I/O 和格式转换
- 对延迟敏感的串行步骤

**判断标准**（一句话版）：
> **"如果一个函数用 Python 能在 1s 内计算出确定结果，它就不该包进 sub-agent。"**

### 架构弃用决策

| 文件 | 状态 | 原因 |
|------|------|------|
| `main.py` | 已弃用（保留对比） | analyzer 也走 sub-agent，60× overhead |
| `main_native_task.py` | 已弃用（保留对比） | Agent tool 触发主会话配额，×5 并发直接失败 |
| `main_v2.py` | **当前推荐** | 正确分工：计算直调 + LLM 推理走 sub-agent |

---

## 待记录（后续阶段）

- [ ] 阶段 3：结构化数据传递（JSON/文件）vs 自然语言传递的准确性差异

---

## Codex 接手记录（2026-06-25）

### 架构调整

Claude 版本的 `main_v2.py` 将 synthesis 生成绑定到 `claude --print --model haiku`，在换成 Codex 或其他执行环境时会出现可移植性问题。接手后新增 `src/synthesizer.py`，提供确定性模板 synthesis：

- 默认运行：`python3 main_v2.py`
- 可选 LLM 运行：`python3 main_v2.py --llm`
- LLM 失败时：自动使用模板 synthesis 兜底，避免报告残留 `pending_llm_interpretation`

### 数据质量口径

基本面字段采用字段级接口池，不再因单个公开接口失败直接降级。只有所有实时/准实时来源和备用计算均无法产出关键字段时，才在 `fetch_errors` 中记录并将 analysis JSON 降级为 `partial`。备用来源会进入 `fetch_warnings` 或字段来源记录，并在“数据质量与异常提示”中展示。

当前基本面优先级：

1. 最新价：腾讯实时行情 → 新浪实时行情
2. 市值/股本：东方财富个股直连 → 腾讯实时行情 → 东方财富全市场实时行情 → AKShare 全市场实时行情 → `stock_individual_info_em`
3. PE/PB：百度股市通 → 腾讯实时行情 / 东方财富实时行情兜底
4. 最后兜底：历史缓存股本 + 最新价重算市值；财报净利润 / EPS 推导股本后重算市值

### 验证结果

2026-06-25 已完成默认模板流程验证：

- 5 只标的 OHLCV 均成功
- 基本面市值/股本已通过腾讯实时行情兜底，5 只标的数据质量均为 `complete`
- 报告输出：`output/2026-06-25/report.md`
- 报告开头和结尾均包含免责声明

---

## 每日推送方案调研（2026-06-25）

### GitHub 调研结论

本次参考了三类开源项目：

- `daily_stock_analysis` / `daily_stock`：多通道通知配置做得完整，适合借鉴“Secrets/环境变量驱动、多通道同时发送、GitHub Actions 定时运行”的工程模式。
- `market-data-notification`：把每日市场信息组织成 digest 并推送到 Telegram，适合借鉴“报告产物与通知产物分离”的设计。
- `microsoft/qlib`、`rock-mind/autoquant`：更偏完整量化研究/回测/策略迭代平台；对当前 demo 来说过重，且容易把项目引向“交易建议/自动交易”方向，因此暂不引入。

### 落地决策

新增 `src/notifier.py`，推送层只读取已生成的 `report.md`，不重新解释行情、不生成买卖信号。`main_v2.py` 增加 `--push`、`--push-mode`、`--push-dry-run` 参数。

支持通道：

- 企业微信：`WECHAT_WEBHOOK_URL`
- 飞书：`FEISHU_WEBHOOK_URL` / `FEISHU_WEBHOOK_SECRET`
- Slack：`SLACK_WEBHOOK_URL`
- Discord：`DISCORD_WEBHOOK_URL`
- 自定义 Webhook：`CUSTOM_WEBHOOK_URL`
- ntfy：`NTFY_URL`
- ServerChan：`SERVERCHAN3_SENDKEY`
- SMTP 邮件：`SMTP_HOST` 等

新增 `.github/workflows/daily-analysis.yml`，默认北京时间工作日 18:30 运行，并上传 `output/` 和 `logs/` artifact。

---

## 观察池策略筛选（2026-06-25）

### 定位

新增 `strategy.json` 和 `src/selector.py`，用于对当前 `watchlist.json` 观察池做研究性排序。输出为“观察优先级”，不输出买入、卖出、持有、目标价等操作性内容。

### 默认策略

策略名：`balanced_research_watchlist`

因子与权重：

- 质量：25%，主要使用 ROE
- 成长：20%，使用营收同比、净利润同比
- 估值：20%，主要使用 PE 近三年分位，PB 作为轻量惩罚项
- 动量：25%，使用均线结构、MACD、RSI、布林带状态
- 风险：10%，对数据质量、估值高分位、RSI 过热、关键字段缺失扣分

硬过滤：

- `data_quality` 必须为 `complete`
- `market_cap` 必须存在
- `pe_percentile_3y` 必须存在

### 产物

每日管道新增步骤 2.5：

```bash
python3 main_v2.py
```

会生成：

- `output/YYYY-MM-DD/selection/selection.json`
- `output/YYYY-MM-DD/selection/selection.md`
- `output/YYYY-MM-DD/report.md` 中的“策略筛选摘要”

### 后续扩展

当前策略只在 watchlist 内排序。若要扩展到全 A 初筛，可以先用市值、成交额、数据完整性、行业白名单/黑名单缩小候选池，再复用 `selector.py` 的打分逻辑。

---

## FTShare-market-data 接入记录（2026-06-25）

### 安装来源

已从 GitHub/ClawHub 对应仓库安装：

- 仓库：`Shawn92/ftshare-market-data`
- 版本：`v0.0.1`
- 安装目录：`~/.codex/skills/ftshare-market-data`

Codex 识别新 skill 需要重启；当前项目流程已通过 `run.py` 直接调用，无需等待重启。

### 本地调用注意

本机 Python 3.14 的 urllib 默认 CA 链对 `ftai.chat` / `market.ft.tech` 会报证书错误，因此调用时需设置 certifi：

```bash
SSL_CERT_FILE=$(python3 -c 'import certifi; print(certifi.where())') \
python3 ~/.codex/skills/ftshare-market-data/run.py stock-security-info --symbol 600519.SH
```

### 项目接入方式

`src/data_fetcher.py` 新增 FTShare 单票接口 `_fetch_security_info_ftshare()`，默认查找：

1. 环境变量 `FTSHARE_RUN_PY`
2. `~/.codex/skills/ftshare-market-data/run.py`

当前基本面字段优先级调整为：

1. FTShare `stock-security-info`
2. 腾讯实时行情
3. 东方财富 / AKShare / `stock_individual_info_em`
4. 历史缓存或财报推导兜底

验证结果：`python3 main_v2.py` 运行后，5 只观察标的的最新价、市值/股本、PE、PB 均来自 `ftshare_stock_security_info`，数据质量为 `complete`。

---

## 收盘扫描汇报（2026-06-25）

### 定位

新增 `src/market_scanner.py`，用于模仿“收盘扫描 + 候选表 + 排除理由”的简洁日报风格。项目已切换为个人模拟交易计划模式，允许输出规则化触发区间、止损、第一止盈、仓位和计划字段。

当前输出字段：

- 当前/信号价
- 触发区间
- 止损
- 第一止盈
- 仓位
- 计划

所有交易参数由固定规则计算，仍需人工复核，不连接实盘接口。

### 运行方式

```bash
python3 src/market_scanner.py
```

### 产物

输出目录：`output/YYYY-MM-DD/scan/`

- `daily_scans.csv`：扫描样本全量打分
- `research_candidates.csv`：当日研究候选，不超过 5 只
- `simulated_trades.csv`：模拟交易 pending 列表，包含触发区间、止损、第一止盈、仓位、计划
- `excluded_watchlist.csv`：容易误判但未列入的样例
- `backtest_trades.csv`：规则回测逐笔结果
- `backtest_summary.json`：回测汇总指标
- `portfolio_backtest_trades.csv`：组合级回测逐笔结果，含 accepted/skipped_capacity
- `portfolio_backtest_summary.json`：组合级资金、持仓和敞口约束后的汇总
- `market_profile.json`：市场环境过滤结果，含环境评级、候选上限、最低综合分和仓位系数
- `ledger_updates.csv`：本次 pending / open / closed 台账变更
- `memory.md`：当日扫描记忆
- `scan_report.md`：可直接发送的简洁汇报

### 数据源

优先使用同花顺扶摇 API：

- `GET /api/a-share/prices/snapshot`：A 股全市场分页行情快照
- `GET /api/meta/tickers/list`：A 股代码表，用于补充中文名
- `GET /api/a-share-index/prices/snapshot`：上证、深成指、创业板、沪深300 指数概览
- `GET /api/a-share-index/prices/historical`：指数历史 K，用于 MA20/MA60、指数波动率和市场环境过滤
- `GET /api/a-share/prices/historical`：高潜样本历史日 K，用于恢复 5/20/60 日涨幅、MA5/MA10/MA20、20 日均额、20 日波动率和规则回测

API Key 不写入仓库，运行时通过以下任一方式注入：

```bash
export FUYAO_API_KEY="<your-api-key>"
python3 src/market_scanner.py
```

或：

```bash
export FUYAO_API_KEY_FILE="$HOME/.secrets/fuyao_api_key"
python3 src/market_scanner.py
```

若扶摇不可用，脚本会尝试 FTShare fallback：

- `stock-quotes-list`：A 股分页行情
- `index-detail`：上证、深成指、创业板、沪深300 指数概览

### 完整策略恢复

扶摇快照不直接提供多日涨幅、均线、换手率和市值。当前实现采用两阶段方式：

1. 全市场快照初筛：用成交额、当日强度、日内振幅排出高潜样本。
2. 历史 K 补齐：默认对前 120 个样本拉取约 260 个自然日的日 K，补齐 5/20/60 日收益、MA5/MA10/MA20、20 日均额、量能倍率和 20 日波动率。

候选必须完成历史 K 补齐；未补齐的标的只进入 `daily_scans.csv` 留痕，不进入当日候选。

### 市场环境过滤

市场环境过滤基于上证、深成指、创业板、沪深300 的指数历史 K，以及全市场样本宽度生成四档环境：

- 积极：候选上限 5，仓位系数 1.0
- 中性：候选上限 4，仓位系数 0.8
- 谨慎：候选上限 3，仓位系数 0.6
- 防守：候选上限 1，仓位系数 0.35

环境过滤会影响当日候选数量、最低综合分和模拟仓位，并写入 `market_profile.json` 与 `simulated_trades.csv`。

### 策略参数配置化

`strategy.json` 新增 `market_scanner` 分支，覆盖收盘扫描的主要参数：

- `data`：历史 K 补齐数量、缓存目录、是否允许 stale cache。
- `filters`：价格、成交额、涨幅、振幅、波动率、距离 20 日高点等硬过滤。
- `trade_plan`：触发区间、止损、第一止盈、仓位生成规则。
- `market_regimes`：积极 / 中性 / 谨慎 / 防守四档市场环境。
- `backtest`：事件级回测、组合级回测和资金约束。
- `ledger`：pending 台账与归档文件位置。

代码内保留默认配置，配置文件缺字段时自动回退默认值。

### 数据缓存与增量更新

历史 K 默认缓存到：

- `data/cache/market_scanner/ohlcv/`
- `data/cache/market_scanner/index/`

日常运行流程：

1. 先读本地缓存。
2. 缓存已覆盖最近交易日则直接使用。
3. 缓存过期则从最近缓存日期向前重叠若干天增量刷新。
4. 外部接口临时失败时，如缓存存在且配置允许，使用 stale cache 继续运行并记录 warning。

实测：首次写入 124 个缓存文件；第二次运行全流程约 3.26 秒。

### 规则回测口径

回测复用同一套扫描评分和模拟计划规则：

- 信号日：收盘后生成触发区间、止损、第一止盈和仓位。
- 买入触发：信号后 2 个交易日内触及触发区间，按保守价格模拟成交。
- 持有周期：最多 5 个交易日。
- 退出顺序：同一交易日同时触及止损和止盈时，按先止损计。
- 成本：每笔扣除 15bp，用于粗略覆盖交易成本和滑点。

当前回测是事件级规则回放，输出胜率、平均/中位单笔、最差单笔、退出分布和事件序列回撤。它会使用历史指数环境过滤新信号，但仍未构建资金占用、同时持仓上限和组合再平衡曲线，因此不能当作组合收益曲线使用，也不能外推为未来收益或胜率保证。

### 组合级回测与台账

组合级回测在事件级交易基础上加入：

- 初始资金
- 同时持仓上限
- 总敞口上限
- 信号冲突时的容量跳过

输出：

- `portfolio_backtest_trades.csv`
- `portfolio_backtest_summary.json`

pending 台账维护：

- `data/ledger/pending_trades.csv`：当前 pending/open 模拟记录
- `data/ledger/trade_archive.csv`：已过期、止损、止盈或到期的归档记录
- `output/YYYY-MM-DD/scan/ledger_updates.csv`：本次运行变更

同一天重复运行不会重复新增同一笔 pending。
