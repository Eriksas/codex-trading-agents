# Market Learning Research Notes

本文件记录从开源量化项目中吸收的工程经验。目标不是复制完整框架，而是把
适合本项目的部分转化为可审计的个人模拟研究流程。

## GitHub 参考

- Qlib: https://github.com/microsoft/qlib
  - 启发：把量化研究拆成数据、学习框架、策略、执行和分析等松耦合模块。
  - 对本项目的落地：继续保持 `data -> review -> health -> steward -> experiment`
    的文件化流水线，不让 agent 直接改策略。
- Microsoft RD-Agent: https://github.com/microsoft/RD-Agent
  - 启发：自动化 R&D 的核心不是一次性给答案，而是循环提出想法、实现、验证、
    复盘。
  - 对本项目的落地：Hermes 负责提出和审查实验，确定性脚本负责沉淀学习记忆。
- QuantConnect Lean: https://github.com/QuantConnect/Lean
  - 启发：成熟交易引擎强调事件驱动、可插拔模块、本地回测和优化命令。
  - 对本项目的落地：把候选、组合约束、风控、复盘、学习记忆拆成独立模块。
- Freqtrade: https://github.com/freqtrade/freqtrade
  - 启发：即使有策略优化，也强调 backtesting、dry-run 和理解机制后再行动。
  - 对本项目的落地：实验必须先 shadow/dry-run，不允许自动进入实盘或主策略。
- FinRL: https://github.com/AI4Finance-Foundation/FinRL
  - 启发：AI 金融研究需要明确区分数据层、环境层、智能体层、应用层和风控。
  - 对本项目的落地：暂不引入 RL，但保留未来接入市场环境仿真和组合风控的接口。

## 本项目采用的学习定义

学习 = 事实复核 + 经验记忆 + 影子实验 + 反方审查 + 晋级门槛。

不是：

- 模型权重在线更新
- agent 自动改主策略
- 小样本调参
- 直接连接实盘

是：

- `data/strategy_learning/learning_memory.json`
- `data/strategy_learning/learning_memory.md`
- `output/YYYY-MM-DD/scan/strategy_learning_summary.json`
- `prompts/hermes_market_learning_skill.md`

## 晋级规则

- 10 笔以内：只记观察，不调参。
- 10-29 笔：可以写影子实验草案，但必须被 critic 审查。
- 30 笔以上且覆盖多个市场环境：可以进入回测验证。
- 回测通过后仍需人工确认，才能考虑修改 `strategy.json`。
