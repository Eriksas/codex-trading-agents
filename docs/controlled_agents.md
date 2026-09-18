# 第三阶段：受控 AI 文字调用

## 如何使用

现有 GitHub 行情获取与飞书推送已配置 Secrets，近期任务运行记录已核对。下文的客户端可用性只针对新增模型调用，不是对现有云端日报能否运行的判断。飞书报告改版继续使用原 Python 路径，见[日报说明](feishu_reports.md)。

默认流程继续离线：`python main.py demo` 只计算并生成报告，不调用模型。人工导入 `--interpretation` 保留。

显式启用前，先检查本机客户端：

```bash
python main.py agent-check
```

该命令只检查 Claude 可执行文件，不读取凭据、验证认证或发送模型请求。当前支持原生 Claude CLI；Windows 的 `.cmd/.bat/.ps1` 启动脚本不进入受控路径。需要时用环境变量 `TRADING_AGENT_CLAUDE_BIN` 指定本机原生可执行文件路径。模型认证由用户按客户端说明自行完成，项目不读取或复制认证文件。

随后显式选择模型：

```bash
python main.py demo --agent claude --model haiku
python main.py diagnose --date 2026-09-16 --agent claude --model haiku
```

`haiku` 是命令示例，需替换为账户实际可用的模型标识。一次诊断最多调用两次，可能使用账户额度或产生模型费用；单次默认超时 120 秒，可用 `--agent-timeout` 调整，最多 600 秒。没有有效统计或输入结构错误时不启动调用。自动调用与人工导入互斥。

## 两步调用，各自留痕

1. Python 生成与原来相同的事实包和数值。
2. 诊断任务提出候选假设，返回证据 ID、反证、待验证方法和置信程度。
3. 程序检查字段、事实包哈希、来源与小样本约束。失败就停止，不补造答案。
4. 第二次调用独立上下文，读取事实包和前轮假设，返回反方意见。它是单独一次调用，仍可能使用同一模型，不能当成独立研究证明。
5. 程序合并通过格式检查的文字，报告仍标记“待人工评审”，策略有效性和人工决定不会由模型改写。

实际调用材料由现有 Steward 规则与本次返回契约组成。自动路径只传指标、质量说明、问题、来源 ID/哈希，不传原始 CSV 或来源的绝对路径；问题和质量说明本身仍可能含私人文字，启用前需确认数据适合交给所选服务。

新增代码职责：

| 文件 | 职责 |
|---|---|
| [agent_client.py](../src/agent_client.py) | 固定参数启动文本调用、超时和结果信封检查、保存原始记录 |
| [agent_workflow.py](../src/agent_workflow.py) | 串行组织诊断与反方任务 |
| [analysis_contract.py](../src/analysis_contract.py) | 人工导入和自动结果共用同一契约 |

## 权限边界

受控客户端使用 `--safe-mode`、空的 `--tools`、`--disallowed-tools "*"`、空 MCP 配置、`--strict-mcp-config`、`--no-session-persistence` 和单轮限制。任务通过 stdin 传入，`shell=False`，工作目录是临时目录。客户端必须支持这些参数；不支持就报错，不删除限制重试。

这些参数的含义已对照 [Claude 官方 CLI 文档](https://code.claude.com/docs/en/cli-reference)核对（2026-09-16）。其中禁用内置工具与禁用 MCP 是不同的控制，不能仅靠 `--allowedTools` 宣称无工具。

**这是客户端工具配置约束，不是操作系统沙箱。** CLI 进程仍使用当前用户身份，并可能读取自己的认证信息；项目没有为其设置文件系统 ACL、网络隔离或恶意可执行文件防护。本轮本机未安装 Claude CLI，未完成真实账户调用验收。

Hermes 暂不接入受控自动后端。核对其[官方 CLI 实现](https://github.com/NousResearch/hermes-agent/blob/main/cli.py)发现，空的 toolsets 参数会走默认工具选择；不能把空值当成禁用工具的证明。原 Hermes 脚本继续按原方式使用、保留原权限限制说明；其生成结果也可按诊断契约人工导入。本轮没有默默改动原每日 Hermes 工作流。

## 原日报的可选 LLM 路径

`python main_v2.py --llm` 继续使用既有 haiku 选择，但通过同一个受控客户端取得 `{code, synthesis}` JSON。Prompt 不再含让模型回写文件的 shell 命令。

Python 检查代码、字段、长度、用语及输入文件是否在调用期间变化，然后仅更新 `synthesis`。普通调用失败仍交原日报的模板兜底；输入已变化时连模板回写也跳过，保留新输入。`data_quality=failed` 直接用不足模板，无需模型。关键数值没有交给模型写入。全文是否夸大或歪曲数值仍需人工核对，字段正确不等于语义正确。

## 日志、失败与模型调用状态

诊断调用存于各次 `output/diagnosis/.../agent/diagnosis/` 和 `critic/`；日报综合观察存于忽略的 `logs/agent_calls/`。每次调用保存：

- `prompt.txt`：实际发送材料与对应哈希。
- `stdout.txt` / `stderr.txt`：CLI 原始输出，可能包含私人内容，仅留本地，不回显到错误摘要。
- `response.txt`：明确成功信封中的模型文字。
- `call.json`：请求模型、时间、耗时、状态和 CLI 声明的使用量。

`model_called=false` 表示未启动模型请求，例如客户端缺失；`null` 表示 CLI 启动后超时/失败，不能确认是否已到达模型；`true` 表示收到 CLI 明确成功的结果信封。它不是模型身份的独立认证，也不代表结果通过研究验证。

输入超过 128 KiB 时不启动 CLI，读取完成的 stdout 超过 2 MiB 时拒绝处理。后者是处理上限，不是模型 token 预算。没有自动重试、自动换模型或因失败放宽权限。诊断失败仍保留 Python 结果和错误状态。

## 接入真实回答的 Eval

```bash
python eval/collect_eval.py --agent claude --model haiku
```

该命令最多逐个调用六个固定案例。只发送 `id/title/input` 和通用输出契约，不发送 `expected` 或人工评审问题。原始回答与每次调用记录保存在独立的 `output/agent_eval/capture-.../`。

采集后调用原 [run_eval.py](../eval/run_eval.py) 的结构与规则检查。错误数值不自动纠正；缺失/错误回答不补造，覆盖不足明确标记。全局客户端/传输失败时停止后续调用；正常返回但格式不合格的案例会记录失败并继续其他案例。

人工仍需核对全文、真实来源与实际工具权限；自动通过状态仍为 `auto_pass_manual_pending`。默认 `python eval/run_eval.py` 仍只检查案例，CI 只执行合成测试，不运行本段真实采集命令。

## 本轮验证边界

已验证固定参数、stdin 和临时工作目录、超时/缺失/错误信封、两阶段依赖、原始数值保护、输入变化拒绝覆盖、Eval 答案不泄露及失败不补造。测试使用 mock 和本地假 CLI，均不是实际模型回答或模型通过率。

真实 Claude 认证、模型额度与运行时行为，以及 Hermes 的无工具自动适配器，仍需后续验证；没有宣称所有 CLI 已受强制沙箱保护。
