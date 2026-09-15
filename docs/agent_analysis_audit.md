# 本次定位整理的仓库审计

审计基线：`main` 提交 `495b7e4893b497483bc3772f88361357395d05db`；整理日期 2026-09-15。工作目录初始为空，克隆后工作树干净。本次没有读取本地秘密、取得行情或运行新的策略实验。

## 已核实的能力

- `main_v2.py` 默认为抓取、指标计算、模板综合观察和报告的 Python 直调；`--llm` 是可选文本生成路径。
- `src/market_scanner.py`、`reviewer.py`、`strategy_health.py` 实现扫描、模拟台账复盘、滚动指标；`strategy_learning.py` 生成确定性经验记录。
- `research/factor_research.py` 有 IC/RankIC、分组、相关矩阵和消融；后续轮次、冻结验证、扩展及面板回测代码保留。
- Hermes 四种模式有脚本、Prompt、文件检查和解析兜底；生成草案与运行实验是两步。
- `strategy_experiments/` 在公开仓库只有说明和占位文件，不能宣称存在大量公开可核验草案。
- `research/forward_*.py` 与独立 Actions 已支持前向观察，已有 `forward_state/` 跟踪记录。
- 原 CI 有 JSON、Python 编译、shell 语法和面板引擎手工算例；本次之前没有独立 Agent 行为 Eval 套件。

## 新首页需要纠正的内容

1. 旧 V3 约 -9.89% 被 [7 月重审](v3_reaudit_2026-07-03.md)纠正，不能继续当成当前结论。
2. “所有策略均负”不能覆盖后续红利和可转债研究；有正结果的档案仍未通过预设标准。
3. AGENTS.md 的早期全 sub-agent 描述与默认直调不符，应区分历史设计和当前入口。
4. “所有定时任务只调用 src”不符合后续前向工作流；保留主日报不反向导入研究层，同时写明已有例外。
5. 只读、人工确认、回滚分别是 Prompt 约束、维护规则、手动指南，不能夸大成沙箱、审批系统或自动恢复。
6. 历史补遗覆盖早期摘要，案例应注明日期与依据，历史原文保持原状。

## 本次实现范围

重组 README，新增方法、案例、Harness 说明；整理现有规则与 Prompt；增加标准库最小 Eval 与测试。结论分级只用于新分析记录和评测字段，不修改策略状态。

策略配置、`src/`、`research/`、主入口、运行脚本、定时推送/观察工作流、历史报告和前向记录保持原文件内容。CI 只追加独立 Eval 检查。

## 复核边界

本次检查源码和仓库研究档案，未重跑历史回测、真实 LLM/Hermes 调用、行情请求或消息推送。完整原始数据与忽略目录中的结果不随公开仓库提供；案例属于历史文档引用，不是本次独立重现实验。

## 本地验证记录

- Python 3.12.14：59 个项目 Python 文件（含新增 Eval）语法检查通过，五份配置/案例 JSON 可解析。
- `python eval/run_eval.py`：六个案例定义校验通过，回答数量为零，`agent_evaluated: false`。
- `python -m unittest discover -s eval -p "test_*.py"`：14 项检查器测试通过，包含六类规则的正反控制；不是模型成绩。
- `python research/test_panel_backtester.py`：原有 T1–T8 手工算例通过。合成测试使用无复权因子时的引擎分支，不能替代真实行情复权验证。
- 四个入口 `main_v2.py`、`src/market_scanner.py`、`src/scheduled_v3_reporter.py`、`src/forward_paper_trading_v3.py` 的 `--help` 均通过。初次检查缺 `requests`，在忽略的本地 `.venv` 补齐后通过；未修改项目依赖声明。
- 全部 shell 脚本通过 `bash -n`；综合观察 Prompt 占位符替换、非法状态返回和缺失数据模板 smoke 通过，未调用模型。
- 检查 README 及本次变更文档的相对链接、路径和代码围栏；运行层没有导入研究层。受保护的策略、运行代码、历史报告与记录对基线无差异。
- 变更文件秘密特征扫描无命中；输出、环境文件和本地虚拟环境未进入提交。该检查范围是本次差异，不是对所有历史提交的秘密审计。
