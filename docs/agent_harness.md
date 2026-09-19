# Agent Harness：任务外面的运行规则

**Prompt 告诉模型做什么；Harness 决定任务在什么输入、权限和检查机制下执行。** 本项目由 Python 脚本、文件契约、日志和人工规则共同实现。

| 机制 | 实现 | 边界 |
|---|---|---|
| 项目约束 | [AGENTS.md](../AGENTS.md)、[HERMES.md](../HERMES.md) | 不造数据、不自动晋级；属于行为规范 |
| 确定性事实 | [健康统计](../src/strategy_health.py)、[诊断编排](../src/diagnosis.py) | Python 计算并保留来源，不自动证明数据完整 |
| 模型输入与输出 | [受控客户端](../src/agent_client.py)、[回答契约](../src/analysis_contract.py) | 固定工具限制、哈希与字段检查；不是 OS 沙箱或全文事实核验 |
| 错误与留痕 | 独立运行目录、原始回答、日志、模板兜底 | 文件留痕不等于不可篡改审计 |
| 规则评测 | [Eval](../eval/README.md) | 自动检查后仍需人工评审 |
| 策略变化 | 独立实验、历史验证、人工确认；[手动回滚](rollback_guide.md) | 没有自动审批、晋级或故障回滚服务 |

一次诊断由 Python 准备事实，Agent 提出候选解释并反方检查，程序校验后写报告，人工决定下一步。[学习记录](../src/strategy_learning.py)保存经验，不更新模型权重。

Claude 自动路径禁用工具，模型只返回文字；CLI 进程仍使用当前用户权限。Hermes 原路径的只读主要依靠 Prompt，尚无经核实的无工具适配器。参数、失败状态和验收情况统一见[受控 AI 调用](controlled_agents.md)。
