"""扫描配置与当前策略读取；默认参数从原扫描器原样迁移。"""

from pathlib import Path
from typing import Any, Optional
import json

DEFAULT_SCANNER_CONFIG: dict[str, Any] = {
    "active_strategy": "legacy_momentum_v1",
    "strategies": {
        "legacy_momentum_v1": {
            "name": "legacy_momentum_v1",
            "description": "Legacy momentum/volume scanner retained for rollback.",
            "engine": "legacy_momentum",
        },
        "alpha040_v3_risk_controlled": {
            "name": "alpha040_v3_risk_controlled",
            "description": "Frozen Alpha040 V3 risk-controlled strategy.",
            "engine": "alpha040_v3",
            "ranking": {
                "primary_factor": "alpha040",
                "auxiliary_factors": ["rps60", "close_to_20d_high"],
                "weights": {
                    "alpha040_z": 0.55,
                    "rps60_z": 0.30,
                    "close_to_20d_high_z": 0.15,
                },
                "ma_alignment_bonus": 0.08,
                "ma5_above_ma20_bonus": 0.04,
                "ma20_distance_penalty_start": 0.12,
                "ma20_distance_penalty_multiplier": 2.0,
                "ma20_distance_penalty_cap": 0.35,
            },
            "filters": {
                "open_only_market_regime_label": "积极",
                "forbid_market_regime_labels": ["中性", "谨慎", "防守"],
                "max_change_rate_5d": 0.2039,
                "max_volatility_20d": 0.049826,
                "require_close_above_ma20": True,
            },
            "risk_control": {
                "stop_type": "atr",
                "atr_window": 14,
                "atr_multiplier": 2.0,
                "reward_risk": 1.6,
                "risk_budget_account_pct": 0.002,
                "risk_budget_max_position_pct": 8,
                "risk_budget_min_stop_pct": 0.005,
            },
            "upper_shadow": {
                "mode": "label_only",
            },
        },
    },
    "data": {
        "enrich_limit": 120,
        "stock_lookback_calendar_days": 260,
        "index_lookback_calendar_days": 260,
        "cache_enabled": True,
        "cache_dir": "data/cache/market_scanner",
        "refresh_overlap_days": 7,
        "stale_cache_allowed": True,
    },
    "filters": {
        "min_price": 3,
        "min_turnover": 1e9,
        "min_market_cap": 5e10,
        "max_change_rate": 0.095,
        "max_amplitude": 0.12,
        "max_5d_return": 0.28,
        "max_60d_return": 1.5,
        "max_volatility_20d": 0.09,
        "max_volume_ratio": 5,
        "min_close_to_20d_high": -0.12,
    },
    "trade_plan": {
        "base_position_pct": 8,
        "high_amplitude_position_pct": 5,
        "hot_5d_position_pct": 5,
        "hot_day_position_pct": 4,
        "pullback_factor": 0.35,
        "pullback_min": 0.008,
        "pullback_max": 0.025,
        "chase_factor": 0.12,
        "chase_min": 0.003,
        "chase_max": 0.012,
        "stop_factor": 0.75,
        "stop_min": 0.035,
        "stop_max": 0.08,
        "reward_risk": 1.6,
        "high_amplitude_threshold": 0.09,
        "hot_5d_threshold": 0.14,
        "hot_day_threshold": 0.07,
    },
    "market_regimes": {
        "risk_on": {
            "min_score": 70,
            "candidate_limit": 5,
            "position_multiplier": 1.0,
            "max_position_pct": 8,
            "min_final_score": 88,
        },
        "neutral": {
            "min_score": 50,
            "candidate_limit": 4,
            "position_multiplier": 0.8,
            "max_position_pct": 6,
            "min_final_score": 90,
        },
        "cautious": {
            "min_score": 35,
            "candidate_limit": 3,
            "position_multiplier": 0.6,
            "max_position_pct": 5,
            "min_final_score": 92,
        },
        "defensive": {
            "candidate_limit": 1,
            "position_multiplier": 0.35,
            "max_position_pct": 3,
            "min_final_score": 95,
        },
    },
    "backtest": {
        "min_score": 70,
        "max_hold_days": 5,
        "entry_window_days": 2,
        "cost_bps": 15,
        "stop_slippage_bps": 30,
        "portfolio_initial_cash": 1_000_000,
        "portfolio_max_positions": 4,
        "portfolio_max_total_exposure_pct": 0.32,
        "portfolio_default_position_pct": 0.08,
    },
    "ledger": {
        "dir": "data/ledger",
        "pending_file": "pending_trades.csv",
        "archive_file": "trade_archive.csv",
        "same_symbol_active_policy": "skip",
    },
    "review": {
        "enabled": True,
        "max_report_items": 8,
    },
    "strategy_health": {
        "enabled": True,
        "windows": [5, 20, 60],
        "history_days": 120,
        "min_sample": 10,
        "max_tag_rows": 12,
    },
    "strategy_steward": {
        "enabled": False,
        "mode": "daily",
        "script": "scripts/run_strategy_steward.sh",
        "fail_on_error": False,
        "timeout_seconds": 900,
    },
    "strategy_learning": {
        "enabled": True,
        "memory_dir": "data/strategy_learning",
    },
    "shadow_experiments": {
        "enabled": True,
        "risk_budget_account_pct": 0.002,
        "risk_budget_max_position_pct": 8,
        "risk_budget_min_stop_pct": 0.005,
    },
}

ACTIVE_SCANNER_CONFIG: dict[str, Any] = DEFAULT_SCANNER_CONFIG.copy()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并配置。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_scanner_config(config_path: str = "strategy.json") -> dict[str, Any]:
    """读取 market_scanner 配置，缺失字段使用默认值。"""
    path = Path(config_path)
    config = DEFAULT_SCANNER_CONFIG
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        scanner_payload = payload.get("market_scanner") or {}
        if isinstance(scanner_payload, dict):
            config = _deep_merge(DEFAULT_SCANNER_CONFIG, scanner_payload)
        if payload.get("active_strategy"):
            config["active_strategy"] = payload.get("active_strategy")
        if isinstance(payload.get("strategies"), dict):
            config["strategies"] = _deep_merge(config.get("strategies") or {}, payload["strategies"])
    return config


def _set_active_config(config: dict[str, Any]) -> None:
    """设置本次运行配置。"""
    global ACTIVE_SCANNER_CONFIG
    ACTIVE_SCANNER_CONFIG = config


def _cfg(section: str, key: str, default: Any = None) -> Any:
    """读取本次运行配置。"""
    return (ACTIVE_SCANNER_CONFIG.get(section) or {}).get(key, default)


def _active_strategy_key() -> str:
    """返回当前启用策略版本。"""
    return str(ACTIVE_SCANNER_CONFIG.get("active_strategy") or "legacy_momentum_v1")


def _strategy_config(strategy_key: Optional[str] = None) -> dict[str, Any]:
    """读取指定策略配置。"""
    key = strategy_key or _active_strategy_key()
    strategies = ACTIVE_SCANNER_CONFIG.get("strategies") or {}
    return strategies.get(key) or {}


def _strategy_cfg(section: str, key: str, default: Any = None, strategy_key: Optional[str] = None) -> Any:
    """读取当前策略内的配置项。"""
    return (_strategy_config(strategy_key).get(section) or {}).get(key, default)


def _strategy_engine() -> str:
    """当前策略引擎。"""
    return str(_strategy_config().get("engine") or "legacy_momentum")


def _is_alpha040_v3_active() -> bool:
    """判断当前是否启用 Alpha040 V3 引擎。"""
    return _strategy_engine() == "alpha040_v3"


def _strategy_display_name() -> str:
    """策略显示名。"""
    config = _strategy_config()
    return str(config.get("name") or _active_strategy_key())
