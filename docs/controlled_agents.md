# 受控 AI 调用

默认诊断离线，启用模型是可选步骤；现有云端行情和飞书任务独立于本机 CLI。报告说明见[飞书日报](feishu_reports.md)。

## 启用

```bash
python main.py agent-check
python main.py demo --agent claude --model haiku
python main.py diagnose --date 2026-09-16 --agent claude --model haiku
```

需要原生 Claude CLI 和有效认证，`haiku` 替换为账户可用模型。Windows 不运行 `.cmd/.bat/.ps1` 启动脚本；可用 `TRADING_AGENT_CLAUDE_BIN` 指定原生可执行文件。`agent-check` 只检查文件存在，不认证账户。

一次诊断最多两次调用，可能产生费用。单次默认超时 120 秒，`--agent-timeout` 最大 600 秒。输入有错误或无有效统计时不调用；自动调用与 `--interpretation` 互斥。

## 执行与失败

诊断提出假设 → 程序校验 → 第二次调用反方检查 → 保存待人工审核的文字。两次调用可以使用同一模型，不是独立研究证明；Python 数值与策略状态不会被模型改写。

自动路径传递问题、指标、质量说明和来源 ID/哈希，不传原始 CSV 或来源绝对路径。文字仍可能含私人信息，启用前确认适合分享。

| 场景 | 行为 |
|---|---|
| 客户端缺失、调用失败、错误 JSON | 留痕并停止；不补答案、不重试或放宽限制 |
| `main_v2.py --llm` 普通失败 | 沿用模板兜底；数据整体失败时直接用不足模板 |
| 日报输入在调用期间变化 | 拒绝模型和模板回写，保留新输入 |
| 字段检查通过 | 文字仍待人工核对，不自动晋级 |

实现：[客户端](../src/agent_client.py)、[两步编排](../src/agent_workflow.py)、[共用契约](../src/analysis_contract.py)。日报的模型只返回 `{code, synthesis}`，由 Python 校验后更新文字字段。

## 权限与验收边界

固定使用 `--safe-mode`、空 `--tools`、`--disallowed-tools "*"`、空 MCP 配置及 `--strict-mcp-config`、`--no-session-persistence`、单轮限制；通过 stdin 传入任务，`shell=False`，使用临时工作目录。不支持这些参数时直接报错。参数依据：[Claude CLI 文档](https://code.claude.com/docs/en/cli-reference)。

**这是工具配置约束，不是 OS 沙箱。** CLI 仍有当前用户权限，可能读取自己的认证。Hermes 的空 toolsets 不能当成禁用工具，见[官方实现](https://github.com/NousResearch/hermes-agent/blob/main/cli.py)；暂保留原路径和人工导入。

已完成离线 mock、假 CLI 和失败路径测试；真实账户认证、额度与模型输出尚未验收，没有真实模型通过率。

## 查记录与评测

诊断记录在 `output/diagnosis/.../agent/`；日报在 `logs/agent_calls/`。`prompt.txt` 是实际输入，`stdout.txt/stderr.txt` 保留原始输出，`response.txt` 保存成功文字，`call.json` 记录模型请求、时间、哈希和状态。原始记录可能含私人内容，默认不提交。

`model_called`：`false` 未启动；`null` 启动后失败/超时、调用与否未知；`true` 收到 CLI 成功信封。它不认证模型身份或结论正确性。输入上限 128 KiB，完成的 stdout 处理上限 2 MiB；后者不是 token 预算。

显式采集六个固定案例并检查回答：

```bash
python eval/collect_eval.py --agent claude --model haiku
```

不向模型发送答案表，不修正错误回答。完整字段、退出码和人工评审要求见 [Eval 说明](../eval/README.md)；默认 `run_eval.py` 和 CI 不调用真实模型。
