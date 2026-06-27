# Local Data Directory

This directory is intentionally kept out of Git except for this README.

Runtime data may include:

- `cache/market_scanner/`: local market data cache.
- `ledger/`: pending/open/archive simulated trade ledger.
- `strategy_learning/`: deterministic Hermes learning memory.

Do not commit local ledgers, cached market data, or generated learning memory unless you have reviewed the content and intentionally want it in the repository.
