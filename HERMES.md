# Hermes Project Card

Project name: Codex Trading Agents

Primary local path:

```text
/Users/eriksas/projects/codex-trading-agents
```

GitHub repository:

```text
https://github.com/Eriksas/codex-trading-agents
```

When the owner asks about this trading project from Feishu, use the primary local
path above as the workspace root. Read `AGENTS.md` first, then inspect
`strategy.json`, `README.md`, `docs/hermes.md`, and the files under `src/`.

Important boundaries:

- This is a personal simulation and market-research workflow, not a live trading system.
- Do not connect brokerage or live trading APIs.
- Do not fabricate missing market data.
- Do not modify `strategy.json`, ledgers, or generated reports unless explicitly asked.
- Strategy experiments belong under `strategy_experiments/` and require manual review.

Useful commands:

```bash
python3 src/market_scanner.py --push --push-dry-run
scripts/run_strategy_steward.sh --date YYYY-MM-DD --mode daily
scripts/run_strategy_steward.sh --date YYYY-MM-DD --mode critic
scripts/run_strategy_steward.sh --date YYYY-MM-DD --mode experiment
python3 src/strategy_learning.py --date YYYY-MM-DD --output output
```
