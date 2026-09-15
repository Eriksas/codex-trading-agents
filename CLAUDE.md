# Legacy Agent Compatibility Instructions

This repository is now maintained under the shared project rules in `AGENTS.md`.

Claude-style agents should read `AGENTS.md` before making changes. The current
project boundary is:

- agent-assisted strategy analysis, research, and personal simulated trade planning only;
- no live brokerage or trading API integration;
- no fabricated market data;
- no claims of certain return, win rate, or controlled risk;
- generated trigger ranges, stop loss, take profit, and position fields must come
  from explicit rules and be labeled for personal simulation and review.

The older Claude-specific wording was intentionally removed to avoid conflicting
instructions between agents.

关键分工与证据规则统一见 [AGENTS.md](AGENTS.md)：Agent 辅助提出假设和整理结果，Python 负责关键数值，重要策略变化经过独立实验、历史验证和人工确认。不要在兼容文件另立一套晋级标准。
