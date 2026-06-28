# Legacy Strategy Notes

## Legacy Version

- Strategy key: `legacy_momentum_v1`
- Config backup: `strategy_legacy_v1.json`
- Scanner backup: `src/market_scanner_legacy_v1.py`
- Status: retained for audit and rollback

## Summary

`legacy_momentum_v1` is the pre-upgrade market scanner strategy. It ranks candidates using a transparent blend of liquidity, daily/short-term momentum, MA trend, volume ratio, volatility control, and proximity to the 20-day high.

The legacy path is kept because Freeze V3 is now promoted as the default strategy, but the old strategy must remain reproducible for comparison and rollback.

## Boundary

The legacy strategy is still a personal simulation and research workflow. It does not connect to live trading and does not provide investment advice.
