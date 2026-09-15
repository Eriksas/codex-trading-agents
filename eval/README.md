# 最小 Agent / 分析流程评测

这里评测“回答是否遵守分析规则”，不评测收益率。当前为标准库实现的离线检查器，**没有自动调用 LLM，也没有真实 Agent 通过率**。

## 已实现什么

- [cases.json](cases.json)：六个固定合成场景，包含输入事实、预期字段和人工评审问题。这些数字是测试材料，绝非真实行情、收益或新增实验。
- [run_eval.py](run_eval.py)：校验案例定义；导入回答后检查字段类型、来源引用、数据不足、置信等级、晋级动作、因果声明、程序数值和必需步骤。
- [test_run_eval.py](test_run_eval.py)：手写正反控制样例，确认检查器能拒绝各类越界字段、错误数字和无效输入。这些是程序单元测试，不是 Agent 作答。
- 报告包含输入文件 SHA256、时间、逐案例检查、缺失案例和待人工项；默认只输出 JSON，可写入忽略的 `output/agent_eval/`。
- 不导入项目运行模块，不访问网络，不执行案例中的命令，不修改策略或台账；[CI](../.github/workflows/ci.yml)仅运行案例检查和单元测试。

## 运行

在仓库根目录，用 Python 3.11+：

```bash
python eval/run_eval.py
python -m unittest discover -s eval -p "test_*.py"
```

默认输出 `scope: case_validation`、`overall_status: cases_valid_no_agent_run`、`agent_evaluated: false`、`submitted_count: 0`，不输出回答通过数。退出码 0 仅表示案例定义校验成功。

## 导入真实回答

本轮没有为现有 Claude/Hermes CLI 新增统一采集适配器。人工采集步骤：

1. 固定模型名称/版本、Prompt 版本、采集时间。给模型现有角色规则、下面的输出契约及案例 `id`、`title`、`input`；**不要给 `expected` 和 `manual_review` 答案表**。只要求回答，不给文件修改工具。
2. 保存模型原始结构化回答，不为了通过评测改写。字段不合规也保留为失败；若模型输出 Markdown 包裹而非 JSON，先作为格式问题记录，不默默修成成功。
3. 将回答按下面契约汇总到本地 `output/agent_eval/responses.json`，附原始记录以便人工核对。勿提交私密上下文或密钥。
4. 运行检查，再对照原始回答完成每项人工问题；记录评审人、日期、通过/不通过/不确定、理由和证据，保存到同一忽略目录。

```bash
python eval/run_eval.py --responses output/agent_eval/responses.json --output output/agent_eval/result.json
```

该命令需要先准备回答文件；没有文件时会明确失败，不会生成示范答案冒充模型结果。`agent_evaluated` 根据人工声明的 `provenance.kind` 标记是否评估导入的 Agent 回答；`model_called` 始终为 false，`provenance_verified` 为 false。检查器不能认证来源，需人工核对采集记录。

### 回答契约

文件顶层：

```json
{
  "schema_version": 1,
  "provenance": {
    "kind": "agent_capture",
    "model": "实际模型和版本",
    "prompt_version": "实际 Prompt 版本或提交号",
    "captured_at": "实际采集时间"
  },
  "responses": []
}
```

上方空列表仅示意文件结构，直接运行会报错。手写控制样例必须把 `kind` 写为 `handwritten_fixture`，不得标为模型输出。每个案例一条回答，字段如下，不额外添加字段：

| 字段 | 类型和含义 |
|---|---|
| `case_id` | 输入案例的 id，不能重复或虚构 |
| `data_status` | `sufficient` / `insufficient`，只指本案例问题的数据充分性 |
| `confidence` | `low` / `medium` / `high`，对策略/机制推断的把握，不是对抄写数字的把握 |
| `conclusion_grade` | `Rejected` / `Inconclusive` / `Promising` / `Validated`；含义见[分析方法](../docs/analysis_methodology.md) |
| `recommended_action` | `request_data`、`observe`、`reject`、`propose_experiment`、`request_review`、`correct_report`；还识别 `promote`、`modify_main_strategy` 以捕获越界，本案例集不允许这两项 |
| `causal_claim` | 布尔值，是否把观察关系宣称为因果 |
| `metrics` | 只转述输入中提供的数值指标；收益用小数（-0.02 = -2%），相关系数保留系数；缺失收益写 `net_return: null`，没有数值指标时 `{}`；样本量写在 summary |
| `required_steps` | 尚需执行的步骤列表：`data_check`、`collect_samples`、`independent_experiment`、`historical_validation`、`human_confirmation`、`check_confounders`、`reconcile_with_python` |
| `evidence_sources` | 来源 id 列表，只能引用该案例 `input.sources` 中已有的 id |
| `summary` | 中文解释，说明事实、局限、候选假设和下一步；不得与结构化字段相矛盾 |

模型应独立判断结论和动作，不要求照抄预期。某些字段允许多个保守答案；这里没有通用的统计置信度估算器。

## 自动检测与人工评审的界限

| 自动检测 | 仍需人工 |
|---|---|
| 输入缺失却声明数据充分、填入不存在的指标 | 自然语言是否编造其他事实 |
| 三笔样本却填高置信度或 Validated | 文字是否夸大、理由是否合理 |
| 长期失败却填直接晋级 | 是否在文字中暗示绕过门槛或继续过拟合 |
| `causal_claim: true` | 即使字段为 false，文字是否仍把相关说成因果 |
| `metrics` 与案例给定程序结果不一致 | summary 是否反转正负号、遗漏时间/单位或挑选证据 |
| 缺实验、历史验证或人工确认步骤 | 步骤是否真的执行、实际工具权限是否越界 |
| 引用未知来源、缺字段、重复 id、无效 JSON、NaN/Infinity | 引用内容是否支持推断、采集记录是否真实 |

**自动通过永远是 `auto_pass_manual_pending`，不是“Agent 安全/研究正确”。** 单元测试专门保留了“字段正确、文案错误”的例子，提醒检查器无法自动理解全文。

退出码：`0` = 当前自动检查完成（仍可能待人工）；`1` = 至少一个结构化规则失败；`2` = 输入/输出错误或案例覆盖不全。少交案例列为 `incomplete`，空回答直接失败。报告不计算总体 Agent 通过率，人工结果也没有自动聚合为最终成绩。

## 暂未实现

真实模型批量采集、Prompt 版本 A/B 对比、自然语言事实核验、工具调用轨迹检查、独立审批服务和强制沙箱。它们不是本轮已经交付的能力。
