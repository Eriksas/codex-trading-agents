# forward_state/ - 前向观察状态（git 跟踪，勿手工修改）

本目录是项目里**唯一被 git 跟踪的运行状态**，由
`.github/workflows/daily-forward-observation.yml` 每交易日自动更新提交。
git 提交历史即防篡改留痕，满足 `forward_gate_recorder.PROTOCOL_V1` 的
「不可改历史」要求——**任何手工修改都会破坏留痕效力**。

| 路径 | 内容 | 评估纪律 |
|---|---|---|
| `bounce/signals_ledger.csv` | idx1000_bounce 事件信号与模拟成交 | 不满 20 次事件不做结论 |
| `bounce/events_log.csv` | 每日触发判定留痕 | — |
| `bounce/amounts/` | 全市场成交额日快照（gzip，保留 40 日） | ADV20 数据基础，可重建 |
| `gates/gate_states.csv` | G2/G4 每日目标暴露 | Day250 主决策点，见 PROTOCOL_V1 |

例外说明：项目惯例不上传 output/ 与 data/，本目录是经论证的唯一例外
（小体积、无敏感信息、且留痕本身需要公开的不可篡改历史）。
背景见 `docs/framework_repair_2026-07.md` §5。
