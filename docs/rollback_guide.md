# Rollback Guide

## Current Default

- Active strategy: `alpha040_v3_risk_controlled`
- Rollback target: `legacy_momentum_v1`

## Roll Back By Config

1. Open `strategy.json`.
2. Change:

```json
"active_strategy": "legacy_momentum_v1"
```

3. Under `market_scanner`, also set:

```json
"active_strategy": "legacy_momentum_v1"
```

4. Run:

```bash
python3 src/market_scanner.py --strategy strategy.json
```

## Roll Back By Backup Files

Use this only if the upgraded scanner path itself is suspected to be broken.

```bash
cp strategy_legacy_v1.json strategy.json
cp archive/market_scanner_legacy_v1.py src/market_scanner.py
```

Then run validation:

```bash
python3 src/market_scanner.py --strategy strategy.json
```

## Audit Files

- Legacy config backup: `strategy_legacy_v1.json`
- Legacy scanner backup: `archive/market_scanner_legacy_v1.py`
- Freeze V3 config snapshot: `freeze_v3_strategy.json`
- Upgrade report: `output/main_strategy_upgrade_v3/main_strategy_upgrade_v3_report.md`

Rollback does not delete any Alpha040 V3 research outputs. It only restores which strategy is active.
