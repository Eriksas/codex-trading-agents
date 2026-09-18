# 文档导航

先跑通一份诊断，再按需要查工具和历史。当前使用说明与有日期的历史档案分开阅读。

## 日常使用：先看这几篇

| 文档 | 解决的问题 |
|---|---|
| [五分钟上手](quickstart.md) | 从 demo 到已有数据、导入 AI 回答，只读一份主报告 |
| [分析方法](analysis_methodology.md) | 怎样定义问题、选指标、检验和否决假设 |
| [真实诊断案例](case_study_strategy_diagnosis.md) | 怎样从早期好看的结果回到证据不足 |
| [Harness](agent_harness.md) | 已有运行机制、人工责任和权限缺口 |
| [第一阶段实现](diagnosis_entry.md) | 本轮改动、兼容边界与验证方法 |
| [第二阶段扫描器拆分](scanner_refactor.md) | 数据、规则、回测、台账与报告分别在哪里，以及怎样验证等价 |
| [第三阶段受控 AI 调用](controlled_agents.md) | 两步文字调用、权限约束、失败记录与真实回答采集 |
| [飞书日报说明](feishu_reports.md) | 怎样区分事实、解释和局限，以及现有云端推送路径 |

## 工具与操作：需要时查

- [因子研究](factor_research.md)：IC/RankIC、分组、相关性与消融。
- [研究代码导航](../research/README.md)：可复用工具、前向观察和历史实验。
- [Hermes](hermes.md)：原有多模式 CLI 操作。
- [推送](push.md)、[手动回滚](rollback_guide.md)：原有运维入口。
- [最小 Eval](../eval/README.md)：结构化回答规则检查及人工评审限制。
- [数据接口记录](fuyao_api_capability_2026-07.md)：有日期的数据源能力笔记。

## 历史研究：沿后续修正阅读

这些是过程档案，不能把早期“当前状态”直接当成现在的结论。后续补遗优先于旧摘要；原始实验记录保留。

| 主题 | 阅读顺序 |
|---|---|
| 早期策略与短样本升级 | [状态报告](project_status_report.md) → [6 月总报告](project_overall_report_2026-06-29.md) → [V3 重审](v3_reaudit_2026-07-03.md)及[反方意见](hermes_critic_v3_reaudit_2026-07-03.md) |
| 7 月搜索与长样本复检 | [搜索报告及终审补遗](strategy_search_2026-07_report.md) + [反方审查](hermes_critic_2026-07-03.md) → [加长复检](panel_v2_extended_recheck_2026-07-09.md) |
| 仓位框架 | [框架修复及补遗](framework_repair_2026-07.md)、[反方审查](hermes_critic_framework_2026-07.md) |
| 后续研究 | [7 月总报告](project_overall_report_2026-07-08.md)、[研究提案](strategy_ideation_2026-07-10.md)、[红利判决及 7 月 21 日补遗](dividend_lowvol_verdict_2026-07-10.md)、[红利反方](hermes_critic_dividend_2026-07-10.md)、[可转债判决](cb_double_low_verdict_2026-07-12.md) |
| 早期设计 | [策略研究](strategy_research.md)、[事件型设计](setup_based_strategy_design.md)、[旧策略笔记](legacy_strategy_notes.md) |
| 工程演变 | [SOP](sop.md)、[7 月重构](refactor_2026-07-03.md)、[学习研究笔记](market_learning_research.md)、[定位整理审计](agent_analysis_audit.md) |

[账户架构手册](account_architecture_handbook_2026-07.md)保留为历史讨论材料，不是统一诊断的运行说明或策略批准依据。
