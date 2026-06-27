# Legacy Agent Compatibility Instructions

This repository is now maintained under the shared project rules in `AGENTS.md`.

Claude-style agents should read `AGENTS.md` before making changes. The current
project boundary is:

- personal quantitative research and simulated trade planning only;
- no live brokerage or trading API integration;
- no fabricated market data;
- no claims of certain return, win rate, or controlled risk;
- generated trigger ranges, stop loss, take profit, and position fields must come
  from explicit rules and be labeled for personal simulation and review.

The older Claude-specific wording was intentionally removed to avoid conflicting
instructions between agents.
