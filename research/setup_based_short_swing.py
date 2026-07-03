#!/usr/bin/env python3
"""Setup-based short swing shadow research.

This module intentionally does not modify the active market scanner strategy.
It tests event-style short swing setups with explicit no-trade states:

- pullback_reclaim_v1
- volatility_contraction_breakout_v1
- panic_repair_v1

The implementation uses daily bars only. Intraday trigger paths are therefore
conservative daily proxies and must not be treated as live-trading logic.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in [ROOT / "src", ROOT / "scripts"]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import factor_research as fr
import factor_research_round3 as r3
import factor_research_round4 as r4
import market_scanner as scanner

DATA_DIR = ROOT / "data" / "expanded"
DEFAULT_CACHE_DIR = DATA_DIR / "daily_kline"
DEFAULT_INDEX_DIR = DATA_DIR / "index"
DEFAULT_OUTPUT_DIR = ROOT / "output" / "setup_based_short_swing"
DEFAULT_STRATEGY_PATH = ROOT / "strategy.json"
REFERENCE_DIR = ROOT / "output" / "expanded_backtest_v3_2021"

SETUP_STRATEGY_NAME = "setup_based_short_swing_v1"
INITIAL_CASH = 1_000_000.0
LIMIT_THRESHOLD = r3.DEFAULT_LIMIT_THRESHOLD
FEE_BPS = 5.0
SLIPPAGE_BPS = 10.0
ENTRY_WINDOW_DAYS = 2
DEFAULT_OBSERVE_DAYS = 3
MAX_HOLD_DAYS = 5
MAX_POSITIONS = 2
MAX_SAME_DAY_NEW = 2
MAX_TOTAL_EXPOSURE_PCT = 0.12
MAX_POSITION_PCT = 6.0
ACTIVE_STATES = {"strong_active", "weak_active"}
EXPECTED_EXIT_REASONS = [
    "take_profit",
    "stop_loss",
    "timeout",
    "time_stop",
    "environment_exit",
    "limit_down_blocked_exit",
    "same_day_stop_take_conservative",
    "setup_failure_exit",
]


@dataclass(frozen=True)
class SetupBacktestVariant:
    key: str
    label: str
    setup_keys: tuple[str, ...]
    use_market_filter: bool = True
    use_sector_filter: bool = True


SETUP_VARIANTS = [
    SetupBacktestVariant(
        key="pullback_reclaim_v1",
        label="强势回踩再转强",
        setup_keys=("pullback_reclaim_v1",),
    ),
    SetupBacktestVariant(
        key="volatility_contraction_breakout_v1",
        label="低波动平台突破",
        setup_keys=("volatility_contraction_breakout_v1",),
    ),
    SetupBacktestVariant(
        key="panic_repair_v1",
        label="恐慌后修复确认",
        setup_keys=("panic_repair_v1",),
    ),
    SetupBacktestVariant(
        key="setup_combined_v1",
        label="三类 setup 组合",
        setup_keys=("pullback_reclaim_v1", "volatility_contraction_breakout_v1", "panic_repair_v1"),
    ),
    SetupBacktestVariant(
        key="setup_combined_no_sector_filter",
        label="三类 setup 组合 / 不启用板块过滤",
        setup_keys=("pullback_reclaim_v1", "volatility_contraction_breakout_v1", "panic_repair_v1"),
        use_sector_filter=False,
    ),
    SetupBacktestVariant(
        key="setup_combined_no_market_filter",
        label="三类 setup 组合 / 不启用市场过滤",
        setup_keys=("pullback_reclaim_v1", "volatility_contraction_breakout_v1", "panic_repair_v1"),
        use_market_filter=False,
    ),
]


def _safe_float(value: Any) -> Optional[float]:
    return fr._safe_float(value)


def _fmt_pct(value: Any, digits: int = 2) -> str:
    value_float = _safe_float(value)
    if value_float is None:
        return "-"
    return f"{value_float * 100:.{digits}f}%"


def _fmt_num(value: Any, digits: int = 4) -> str:
    value_float = _safe_float(value)
    if value_float is None:
        return "-"
    return f"{value_float:.{digits}f}"


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=fr._json_default)


def _markdown_table(df: pd.DataFrame, columns: list[tuple[str, str]], max_rows: Optional[int] = None) -> list[str]:
    if df.empty:
        return ["No rows."]
    work = df.head(max_rows) if max_rows else df
    lines = ["| " + " | ".join(title for title, _col in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in work.to_dict("records"):
        cells: list[str] = []
        for _title, col in columns:
            value = row.get(col)
            if col.endswith("_return") or col in {
                "total_return",
                "annualized_return",
                "max_drawdown",
                "win_rate",
                "average_trade_return",
                "median_trade_return",
                "positive_year_rate",
                "positive_month_rate",
                "return",
                "trade_count_share",
            }:
                cells.append(_fmt_pct(value))
            elif isinstance(value, float):
                cells.append(_fmt_num(value))
            elif pd.isna(value):
                cells.append("-")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    if max_rows and len(df) > max_rows:
        lines.append(f"\n仅展示前 {max_rows} 行，共 {len(df)} 行。")
    return lines


def _ma(values: list[Optional[float]], end_idx: int, window: int) -> Optional[float]:
    if end_idx - window + 1 < 0:
        return None
    sample = [value for value in values[end_idx - window + 1 : end_idx + 1] if value is not None]
    if len(sample) < window:
        return None
    return float(sum(sample) / len(sample))


def _avg(values: list[Optional[float]]) -> Optional[float]:
    sample = [value for value in values if value is not None]
    if not sample:
        return None
    return float(sum(sample) / len(sample))


def _ret(bars: list[dict], idx: int, window: int) -> Optional[float]:
    if idx - window < 0:
        return None
    close = _safe_float(bars[idx].get("close"))
    prev = _safe_float(bars[idx - window].get("close"))
    if close is None or prev is None or prev <= 0:
        return None
    return close / prev - 1


def _daily_ret(bars: list[dict], idx: int) -> Optional[float]:
    if idx <= 0:
        return None
    close = _safe_float(bars[idx].get("close"))
    prev = _safe_float(bars[idx - 1].get("close"))
    if close is None or prev is None or prev <= 0:
        return None
    return close / prev - 1


def _shadow_ratio(bar: dict, side: str) -> Optional[float]:
    high = _safe_float(bar.get("high"))
    low = _safe_float(bar.get("low"))
    close = _safe_float(bar.get("close"))
    open_price = _safe_float(bar.get("open"))
    if high is None or low is None or close is None or open_price is None or high <= low:
        return None
    body_high = max(open_price, close)
    body_low = min(open_price, close)
    if side == "upper":
        return max(0.0, (high - body_high) / (high - low))
    return max(0.0, (body_low - low) / (high - low))


def _rolling_high(bars: list[dict], idx: int, window: int, include_today: bool = True) -> Optional[float]:
    end = idx + 1 if include_today else idx
    start = max(0, end - window)
    highs = [_safe_float(bar.get("high")) for bar in bars[start:end]]
    highs = [value for value in highs if value is not None]
    return max(highs) if highs else None


def _rolling_low(bars: list[dict], idx: int, window: int, include_today: bool = True) -> Optional[float]:
    end = idx + 1 if include_today else idx
    start = max(0, end - window)
    lows = [_safe_float(bar.get("low")) for bar in bars[start:end]]
    lows = [value for value in lows if value is not None]
    return min(lows) if lows else None


def _load_expanded_metadata() -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    stock_path = DATA_DIR / "stock_basic.csv"
    if stock_path.exists():
        stock = pd.read_csv(stock_path, encoding="utf-8-sig")
        for row in stock.to_dict("records"):
            symbol = str(row.get("ts_code") or "").strip()
            if not symbol:
                continue
            metadata[symbol] = {
                "name": str(row.get("name") or symbol),
                "sector": str(row.get("industry") or "").strip() or "行业缺失",
            }
    industry_path = DATA_DIR / "industry_or_sector.csv"
    if industry_path.exists():
        industry = pd.read_csv(industry_path, encoding="utf-8-sig")
        for row in industry.to_dict("records"):
            symbol = str(row.get("ts_code") or "").strip()
            if not symbol:
                continue
            industry_name = str(row.get("industry") or row.get("sector") or "").strip() or "行业缺失"
            item = metadata.setdefault(symbol, {"name": symbol, "sector": "行业缺失"})
            item["sector"] = industry_name
    return metadata


def _load_index_bars(index_dir: Path) -> dict[str, list[dict]]:
    histories: dict[str, list[dict]] = {}
    if index_dir.exists():
        for path in sorted(index_dir.glob("*.csv")):
            symbol = fr._symbol_from_cache_path(path)
            rows = pd.read_csv(path, encoding="utf-8-sig")
            if "date" not in rows.columns or "close" not in rows.columns:
                continue
            bars = []
            for row in rows.sort_values("date").to_dict("records"):
                bars.append(
                    {
                        "date": str(row.get("date")),
                        "open": _safe_float(row.get("open")),
                        "high": _safe_float(row.get("high")),
                        "low": _safe_float(row.get("low")),
                        "close": _safe_float(row.get("close")),
                        "volume": _safe_float(row.get("volume")),
                        "turnover": _safe_float(row.get("turnover")),
                    }
                )
            if len(bars) >= 80:
                histories[symbol] = bars
    elif (DATA_DIR / "index_daily.csv").exists():
        raw = pd.read_csv(DATA_DIR / "index_daily.csv", encoding="utf-8-sig")
        raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce")
        for code, group in raw.dropna(subset=["trade_date", "close"]).groupby("ts_code"):
            bars = []
            for row in group.sort_values("trade_date").to_dict("records"):
                bars.append(
                    {
                        "date": row["trade_date"].strftime("%Y-%m-%d"),
                        "open": _safe_float(row.get("open")),
                        "high": _safe_float(row.get("high")),
                        "low": _safe_float(row.get("low")),
                        "close": _safe_float(row.get("close")),
                        "volume": _safe_float(row.get("vol")),
                        "turnover": _safe_float(row.get("amount")),
                    }
                )
            if len(bars) >= 80:
                histories[str(code)] = bars
    return histories


def _build_market_states(histories: dict[str, list[dict]], index_dir: Path) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    """Build setup-specific market states from index and breadth data."""
    breadth: dict[str, dict[str, Any]] = defaultdict(lambda: {"returns": [], "total": 0, "up": 0, "above_ma20": 0, "near_high": 0, "big_down": 0, "limit_down": 0})
    for _symbol, bars in histories.items():
        closes = [_safe_float(bar.get("close")) for bar in bars]
        for idx in range(20, len(bars)):
            close = closes[idx]
            prev_close = closes[idx - 1]
            if close is None or prev_close is None or prev_close <= 0:
                continue
            ma20 = _ma(closes, idx, 20)
            high20 = _rolling_high(bars, idx, 20)
            daily = close / prev_close - 1
            date = str(bars[idx].get("date") or "")
            if not date:
                continue
            item = breadth[date]
            item["total"] += 1
            item["returns"].append(daily)
            if daily > 0:
                item["up"] += 1
            if ma20 is not None and close >= ma20:
                item["above_ma20"] += 1
            if high20 and close / high20 - 1 >= -0.03:
                item["near_high"] += 1
            if daily <= -0.07:
                item["big_down"] += 1
            if r3._is_limit_down(bars[idx], prev_close, threshold=LIMIT_THRESHOLD):
                item["limit_down"] += 1

    index_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"index_count": 0, "index_above_ma20_count": 0, "index_positive_slope_count": 0})
    for _symbol, bars in _load_index_bars(index_dir).items():
        closes = [_safe_float(bar.get("close")) for bar in bars]
        for idx in range(25, len(bars)):
            close = closes[idx]
            ma20 = _ma(closes, idx, 20)
            ma20_prev = _ma(closes, idx - 5, 20)
            date = str(bars[idx].get("date") or "")
            if not date or close is None or ma20 is None:
                continue
            item = index_counts[date]
            item["index_count"] += 1
            if close >= ma20:
                item["index_above_ma20_count"] += 1
            if ma20_prev is not None and ma20 > ma20_prev:
                item["index_positive_slope_count"] += 1

    states: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for date in sorted(breadth):
        item = breadth[date]
        total = int(item["total"])
        returns = pd.Series(item["returns"], dtype="float64").dropna()
        up_ratio = item["up"] / total if total else None
        above_ratio = item["above_ma20"] / total if total else None
        near_ratio = item["near_high"] / total if total else None
        median_change = float(returns.median()) if len(returns) else None
        idx_item = index_counts.get(date, {})
        index_above = int(idx_item.get("index_above_ma20_count") or 0)
        index_slope = int(idx_item.get("index_positive_slope_count") or 0)
        index_count = int(idx_item.get("index_count") or 0)
        state = "neutral"
        if total < 100 or up_ratio is None or above_ratio is None or median_change is None:
            state = "neutral"
        elif (
            index_above >= max(2, min(3, index_count))
            and index_slope >= max(1, min(2, index_count))
            and up_ratio >= 0.55
            and above_ratio >= 0.50
            and (near_ratio or 0) >= 0.16
            and median_change > 0
            and int(item["limit_down"]) <= 80
            and int(item["big_down"]) <= 350
        ):
            state = "strong_active"
        elif (
            (index_above >= max(1, min(2, index_count)) or above_ratio >= 0.43)
            and up_ratio >= 0.47
            and above_ratio >= 0.36
            and median_change >= -0.006
            and int(item["limit_down"]) <= 140
            and int(item["big_down"]) <= 520
        ):
            state = "weak_active"
        elif (
            up_ratio < 0.38
            or above_ratio < 0.28
            or median_change < -0.015
            or int(item["limit_down"]) >= 140
            or int(item["big_down"]) >= 520
        ):
            state = "defensive"

        profile = {
            "date": date,
            "market_state": state,
            "open_allowed": state in ACTIVE_STATES,
            "stock_count": total,
            "up_ratio": round(float(up_ratio), 6) if up_ratio is not None else None,
            "above_ma20_ratio": round(float(above_ratio), 6) if above_ratio is not None else None,
            "near_20d_high_ratio": round(float(near_ratio), 6) if near_ratio is not None else None,
            "median_change": round(float(median_change), 6) if median_change is not None else None,
            "big_down_count": int(item["big_down"]),
            "limit_down_count": int(item["limit_down"]),
            "index_count": index_count,
            "index_above_ma20_count": index_above,
            "index_positive_slope_count": index_slope,
        }
        states[date] = profile
        rows.append(profile)
    return states, pd.DataFrame(rows)


def _build_sector_stats(
    universe_by_date: dict[str, list[dict]],
    histories: dict[str, list[dict]],
    market_states: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], pd.DataFrame]:
    sector_by_date: dict[str, dict[str, dict[str, Any]]] = {}
    rows: list[dict[str, Any]] = []
    for date, items in sorted(universe_by_date.items()):
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            symbol = str(item.get("_symbol_key") or item.get("symbol") or "")
            bars = histories.get(symbol) or []
            idx = int(item.get("_signal_idx") or -1)
            if idx < 5 or idx >= len(bars):
                continue
            close = _safe_float(bars[idx].get("close"))
            prev = _safe_float(bars[idx - 1].get("close"))
            ma20 = _safe_float(item.get("ma20"))
            industry = str(item.get("industry") or item.get("sector") or "行业缺失")
            ret3 = _ret(bars, idx, 3)
            ret5 = _ret(bars, idx, 5)
            daily = close / prev - 1 if close is not None and prev is not None and prev > 0 else None
            close_to_high = _safe_float(item.get("close_to_20d_high"))
            change5 = _safe_float(item.get("change_rate_5d"))
            buckets[industry].append(
                {
                    "ret3": ret3,
                    "ret5": ret5,
                    "up": daily is not None and daily > 0,
                    "above_ma20": close is not None and ma20 is not None and close >= ma20,
                    "strong": close_to_high is not None
                    and close_to_high >= -0.08
                    and change5 is not None
                    and change5 > -0.02
                    and close is not None
                    and ma20 is not None
                    and close >= ma20,
                }
            )
        daily_rows: list[dict[str, Any]] = []
        market_profile = market_states.get(date) or {}
        for industry, values in buckets.items():
            ret3 = pd.Series([row["ret3"] for row in values], dtype="float64").dropna()
            ret5 = pd.Series([row["ret5"] for row in values], dtype="float64").dropna()
            row = {
                "date": date,
                "industry": industry,
                "stock_count": len(values),
                "industry_ret3": float(ret3.mean()) if len(ret3) else None,
                "industry_ret5": float(ret5.mean()) if len(ret5) else None,
                "industry_up_ratio": sum(1 for row in values if row["up"]) / len(values) if values else None,
                "industry_above_ma20_ratio": sum(1 for row in values if row["above_ma20"]) / len(values) if values else None,
                "industry_strong_count": sum(1 for row in values if row["strong"]),
                "market_up_ratio": market_profile.get("up_ratio"),
                "market_above_ma20_ratio": market_profile.get("above_ma20_ratio"),
            }
            daily_rows.append(row)
        if daily_rows:
            frame = pd.DataFrame(daily_rows)
            frame["industry_ret3_rank_pct"] = frame["industry_ret3"].rank(pct=True)
            frame["industry_ret5_rank_pct"] = frame["industry_ret5"].rank(pct=True)
            for row in frame.to_dict("records"):
                sector_by_date.setdefault(date, {})[str(row["industry"])] = row
                rows.append(row)
    return sector_by_date, pd.DataFrame(rows)


def _sector_pass(item: dict, sector_stats: dict[str, dict[str, Any]], market_profile: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    industry = str(item.get("industry") or item.get("sector") or "行业缺失")
    if industry in {"", "行业缺失", "未分类"}:
        return False, "missing_industry", {}
    stats = sector_stats.get(industry) or {}
    if not stats:
        return False, "missing_sector_stats", {}
    ret_rank_ok = (_safe_float(stats.get("industry_ret3_rank_pct")) or 0) >= 0.70 or (_safe_float(stats.get("industry_ret5_rank_pct")) or 0) >= 0.70
    market_up = _safe_float(market_profile.get("up_ratio")) or 0
    market_above = _safe_float(market_profile.get("above_ma20_ratio")) or 0
    up_ok = (_safe_float(stats.get("industry_up_ratio")) or 0) > market_up
    above_ok = (_safe_float(stats.get("industry_above_ma20_ratio")) or 0) > market_above
    strong_count = int(stats.get("industry_strong_count") or 0)
    stock_count = int(stats.get("stock_count") or 0)
    strong_ok = strong_count >= max(2, min(3, stock_count // 8 if stock_count >= 24 else 2))
    if ret_rank_ok and strong_ok and (up_ok or above_ok):
        return True, "passed", stats
    failed = []
    if not ret_rank_ok:
        failed.append("sector_return_not_top30")
    if not strong_ok:
        failed.append("sector_not_enough_strong_stocks")
    if not (up_ok or above_ok):
        failed.append("sector_breadth_not_resonant")
    return False, "+".join(failed), stats


def _base_signal(item: dict, setup_key: str, setup_label: str, score: float, trigger_low: float, trigger_high: float, failure_level: float, tags: dict[str, Any]) -> dict[str, Any]:
    signal = dict(item)
    signal.update(
        {
            "setup_strategy_name": SETUP_STRATEGY_NAME,
            "setup_key": setup_key,
            "setup_label": setup_label,
            "setup_score": round(float(score), 6),
            "rank_score": round(float(score), 6),
            "trigger_low": round(float(trigger_low), 3),
            "trigger_high": round(float(trigger_high), 3),
            "failure_level": round(float(failure_level), 3),
            "setup_tags_json": json.dumps(tags, ensure_ascii=False, default=fr._json_default),
        }
    )
    return signal


def _evaluate_pullback_reclaim(item: dict, bars: list[dict], idx: int) -> Optional[dict[str, Any]]:
    if idx < 25:
        return None
    close = _safe_float(bars[idx].get("close"))
    prev_close = _safe_float(bars[idx - 1].get("close"))
    ma5 = _safe_float(item.get("ma5"))
    ma10 = _safe_float(item.get("ma10"))
    ma20 = _safe_float(item.get("ma20"))
    if close is None or prev_close is None or ma5 is None or ma10 is None or ma20 is None or close < ma20:
        return None
    rps20 = _safe_float(item.get("rps20")) or 0
    rps60 = _safe_float(item.get("rps60")) or 0
    if max(rps20, rps60) < 0.55:
        return None
    change5 = _safe_float(item.get("change_rate_5d"))
    if change5 is None or not (-0.11 <= change5 <= 0.035):
        return None
    closes = [_safe_float(bar.get("close")) for bar in bars]
    ma20_recent = [_ma(closes, day, 20) for day in range(idx - 4, idx + 1)]
    lows_recent = [_safe_float(bars[day].get("low")) for day in range(idx - 4, idx + 1)]
    if any(low is None or ma is None or low < ma * 0.985 for low, ma in zip(lows_recent, ma20_recent)):
        return None
    turnovers = [_safe_float(bar.get("turnover")) for bar in bars]
    recent_turnover = _avg(turnovers[idx - 2 : idx + 1])
    prior_turnover = _avg(turnovers[idx - 13 : idx - 3])
    if recent_turnover is None or prior_turnover is None or recent_turnover >= prior_turnover * 0.98:
        return None
    prev_ma5 = _ma(closes, idx - 1, 5)
    prev_ma10 = _ma(closes, idx - 1, 10)
    daily = _daily_ret(bars, idx)
    reclaim = (
        (prev_ma5 is not None and prev_close < prev_ma5 and close >= ma5)
        or (prev_ma10 is not None and prev_close < prev_ma10 and close >= ma10)
        or (close >= ma5 and daily is not None and daily > 0.01)
    )
    if not reclaim:
        return None
    if daily is None or daily > 0.065:
        return None
    if change5 > 0.12:
        return None
    close_to_high = _safe_float(item.get("close_to_20d_high"))
    if close_to_high is None or close_to_high < -0.12:
        return None
    pullback_low = _rolling_low(bars, idx, 5)
    if pullback_low is None:
        return None
    contraction_score = max(0.0, min(1.0, 1 - recent_turnover / prior_turnover))
    score = 0.35 * rps60 + 0.20 * rps20 + 0.25 * (1 + close_to_high) + 0.20 * contraction_score
    failure_level = max(pullback_low, ma20 * 0.995)
    tags = {
        "upper_shadow_ratio": _shadow_ratio(bars[idx], "upper"),
        "turnover_contraction": recent_turnover / prior_turnover if prior_turnover else None,
        "change_rate_5d": change5,
        "close_to_20d_high": close_to_high,
    }
    return _base_signal(
        item,
        "pullback_reclaim_v1",
        "强势回踩再转强",
        score,
        close * 0.995,
        close * 1.030,
        failure_level,
        tags,
    )


def _evaluate_volatility_contraction_breakout(item: dict, bars: list[dict], idx: int) -> Optional[dict[str, Any]]:
    if idx < 30:
        return None
    close = _safe_float(bars[idx].get("close"))
    if close is None:
        return None
    daily = _daily_ret(bars, idx)
    if daily is None or daily < 0.003 or daily > 0.075:
        return None
    prior10_high = _rolling_high(bars, idx, 10, include_today=False)
    high20 = _rolling_high(bars, idx, 20)
    low20 = _rolling_low(bars, idx, 20)
    if prior10_high is None or high20 is None or low20 is None or low20 <= 0:
        return None
    if high20 / low20 - 1 > 0.22:
        return None
    close_to_high = _safe_float(item.get("close_to_20d_high"))
    if close_to_high is None or close_to_high < -0.035:
        return None
    if close < prior10_high * 0.998:
        return None
    returns = [_daily_ret(bars, day) for day in range(idx - 19, idx + 1)]
    vol10 = pd.Series(returns[-10:], dtype="float64").std(ddof=0)
    vol20 = pd.Series(returns, dtype="float64").std(ddof=0)
    if pd.isna(vol10) or pd.isna(vol20) or vol20 <= 0 or vol10 > vol20 * 0.95:
        return None
    turnovers = [_safe_float(bar.get("turnover")) for bar in bars]
    today_turnover = turnovers[idx]
    avg_turnover10 = _avg(turnovers[idx - 10 : idx])
    if today_turnover is None or avg_turnover10 is None or avg_turnover10 <= 0:
        return None
    turnover_ratio = today_turnover / avg_turnover10
    if not (1.05 <= turnover_ratio <= 2.50):
        return None
    if bool(item.get("signal_day_limit_up")):
        return None
    upper = _shadow_ratio(bars[idx], "upper")
    contraction_score = max(0.0, min(1.0, 1 - (vol10 / vol20 if vol20 else 1)))
    volume_score = max(0.0, 1 - abs(turnover_ratio - 1.45) / 1.45)
    score = 0.35 * (1 + close_to_high) + 0.30 * contraction_score + 0.25 * volume_score + 0.10 * (_safe_float(item.get("rps20")) or 0)
    signal_low = _safe_float(bars[idx].get("low")) or close * 0.97
    failure_level = max(prior10_high * 0.985, signal_low)
    tags = {
        "upper_shadow_ratio": upper,
        "vol10": float(vol10),
        "vol20": float(vol20),
        "turnover_ratio": turnover_ratio,
        "range20": high20 / low20 - 1,
    }
    return _base_signal(
        item,
        "volatility_contraction_breakout_v1",
        "低波动平台突破",
        score,
        close * 0.995,
        close * 1.025,
        failure_level,
        tags,
    )


def _panic_day(bars: list[dict], idx: int) -> bool:
    if idx < 6:
        return False
    ret5 = _ret(bars, idx, 5)
    daily = _daily_ret(bars, idx)
    lower = _shadow_ratio(bars[idx], "lower")
    turnovers = [_safe_float(bar.get("turnover")) for bar in bars]
    turnover = turnovers[idx]
    avg5 = _avg(turnovers[idx - 5 : idx])
    close = _safe_float(bars[idx].get("close"))
    low = _safe_float(bars[idx].get("low"))
    volume_expand = turnover is not None and avg5 is not None and avg5 > 0 and turnover > avg5 * 1.15
    lower_repair = lower is not None and lower >= 0.32
    close_off_low = close is not None and low is not None and low > 0 and close / low - 1 >= 0.025
    return ((ret5 is not None and ret5 <= -0.06) or (daily is not None and daily <= -0.035)) and (volume_expand or lower_repair or close_off_low)


def _evaluate_panic_repair(item: dict, bars: list[dict], idx: int) -> Optional[dict[str, Any]]:
    if idx < 20:
        return None
    close = _safe_float(bars[idx].get("close"))
    ma5 = _safe_float(item.get("ma5"))
    ma20 = _safe_float(item.get("ma20"))
    if close is None or ma5 is None or ma20 is None:
        return None
    high10 = _rolling_high(bars, idx, 10)
    if high10 is None or close / high10 - 1 > -0.02:
        return None
    drawdown10 = close / high10 - 1
    ret5 = _ret(bars, idx, 5)
    if drawdown10 > -0.08 and (ret5 is None or ret5 > -0.055):
        return None
    panic_idx: Optional[int] = None
    for day in range(idx - 3, idx):
        if _panic_day(bars, day):
            panic_idx = day
            break
    if panic_idx is None:
        return None
    prev_high = _safe_float(bars[idx - 1].get("high"))
    daily = _daily_ret(bars, idx)
    confirm = prev_high is not None and close > prev_high
    if not confirm and not (close >= ma5 and daily is not None and daily > 0.0):
        return None
    prev_close = r3._prev_close_from_bars(bars, idx)
    if r3._is_limit_down(bars[idx], prev_close, threshold=LIMIT_THRESHOLD):
        return None
    panic_low = _rolling_low(bars, panic_idx, 2)
    today_low = _safe_float(bars[idx].get("low"))
    if panic_low is None or today_low is None:
        return None
    lower = _shadow_ratio(bars[panic_idx], "lower")
    score = 0.35 * min(1.0, abs(drawdown10) / 0.18) + 0.25 * ((_safe_float(item.get("rps60")) or 0.5)) + 0.25 * (1 if confirm else 0.5) + 0.15 * (lower or 0)
    failure_level = min(panic_low, today_low)
    tags = {
        "panic_idx": panic_idx,
        "panic_date": bars[panic_idx].get("date"),
        "drawdown10": drawdown10,
        "ret5": ret5,
        "panic_lower_shadow": lower,
        "confirm_above_prev_high": confirm,
    }
    return _base_signal(
        item,
        "panic_repair_v1",
        "恐慌后修复确认",
        score,
        close * 0.995,
        close * 1.025,
        failure_level,
        tags,
    )


def _evaluate_setup(setup_key: str, item: dict, histories: dict[str, list[dict]]) -> Optional[dict[str, Any]]:
    symbol = str(item.get("_symbol_key") or item.get("symbol") or "")
    idx = int(item.get("_signal_idx") or -1)
    bars = histories.get(symbol) or []
    if idx <= 0 or idx >= len(bars):
        return None
    if setup_key == "pullback_reclaim_v1":
        return _evaluate_pullback_reclaim(item, bars, idx)
    if setup_key == "volatility_contraction_breakout_v1":
        return _evaluate_volatility_contraction_breakout(item, bars, idx)
    if setup_key == "panic_repair_v1":
        return _evaluate_panic_repair(item, bars, idx)
    raise KeyError(setup_key)


def _build_setup_candidates(
    universe_by_date: dict[str, list[dict]],
    histories: dict[str, list[dict]],
    market_states: dict[str, dict[str, Any]],
    sector_by_date: dict[str, dict[str, dict[str, Any]]],
) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], pd.DataFrame, pd.DataFrame]:
    candidates_by_variant: dict[str, dict[str, list[dict[str, Any]]]] = {variant.key: {} for variant in SETUP_VARIANTS}
    daily_diag_rows: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    for date, universe_rows in sorted(universe_by_date.items()):
        market_profile = market_states.get(date) or {"market_state": "neutral", "open_allowed": False}
        sector_stats = sector_by_date.get(date) or {}
        setup_signals_by_key: dict[str, list[dict[str, Any]]] = {key: [] for key in {"pullback_reclaim_v1", "volatility_contraction_breakout_v1", "panic_repair_v1"}}
        for item in universe_rows:
            for setup_key in setup_signals_by_key:
                signal = _evaluate_setup(setup_key, item, histories)
                if signal:
                    industry = str(signal.get("industry") or signal.get("sector") or "行业缺失")
                    passed, reason, stats = _sector_pass(signal, sector_stats, market_profile)
                    signal.update(
                        {
                            "signal_date": date,
                            "sector_filter_pass": passed,
                            "sector_filter_reason": reason,
                            "industry_ret3_rank_pct": stats.get("industry_ret3_rank_pct"),
                            "industry_ret5_rank_pct": stats.get("industry_ret5_rank_pct"),
                            "industry_up_ratio": stats.get("industry_up_ratio"),
                            "industry_above_ma20_ratio": stats.get("industry_above_ma20_ratio"),
                            "industry_strong_count": stats.get("industry_strong_count"),
                            "market_state": market_profile.get("market_state"),
                        }
                    )
                    setup_signals_by_key[setup_key].append(signal)
                    signal_rows.append(
                        {
                            "date": date,
                            "setup_key": setup_key,
                            "symbol": signal.get("symbol"),
                            "name": signal.get("name"),
                            "industry": industry,
                            "setup_score": signal.get("setup_score"),
                            "sector_filter_pass": passed,
                            "sector_filter_reason": reason,
                            "market_state": market_profile.get("market_state"),
                        }
                    )
        for variant in SETUP_VARIANTS:
            market_allowed = (market_profile.get("market_state") in ACTIVE_STATES) or not variant.use_market_filter
            before_sector: list[dict[str, Any]] = []
            for setup_key in variant.setup_keys:
                before_sector.extend(setup_signals_by_key.get(setup_key) or [])
            if market_allowed and variant.use_sector_filter:
                after_sector = [signal for signal in before_sector if bool(signal.get("sector_filter_pass"))]
            elif market_allowed:
                after_sector = list(before_sector)
            else:
                after_sector = []
            after_sector.sort(
                key=lambda signal: (
                    _safe_float(signal.get("setup_score")) if _safe_float(signal.get("setup_score")) is not None else -999,
                    str(signal.get("_symbol_key") or signal.get("symbol") or ""),
                ),
                reverse=True,
            )
            selected = after_sector[:8]
            candidates_by_variant[variant.key][date] = selected
            no_trade_reason = "trade_candidates_available"
            if variant.use_market_filter and market_profile.get("market_state") not in ACTIVE_STATES:
                no_trade_reason = "no_trade_market_filter"
            elif not before_sector:
                no_trade_reason = "no_trade_no_setup"
            elif variant.use_sector_filter and not after_sector:
                no_trade_reason = "no_trade_sector_filter"
            elif not selected:
                no_trade_reason = "no_trade_no_selected_candidate"
            daily_diag_rows.append(
                {
                    "date": date,
                    "strategy_key": variant.key,
                    "market_state": market_profile.get("market_state"),
                    "market_allowed": market_allowed,
                    "use_market_filter": variant.use_market_filter,
                    "use_sector_filter": variant.use_sector_filter,
                    "universe_count": len(universe_rows),
                    "setup_signal_count_before_sector": len(before_sector),
                    "setup_signal_count_after_sector": len(after_sector),
                    "selected_signal_count": len(selected),
                    "no_trade_reason": no_trade_reason,
                    "no_trade": int(len(selected) == 0),
                }
            )
    return candidates_by_variant, pd.DataFrame(daily_diag_rows), pd.DataFrame(signal_rows)


def _entry_price_for_zone(bar: dict, trigger_low: float, trigger_high: float) -> Optional[float]:
    open_price = _safe_float(bar.get("open"))
    high = _safe_float(bar.get("high"))
    low = _safe_float(bar.get("low"))
    if open_price is None or high is None or low is None:
        return None
    if high < trigger_low or low > trigger_high:
        return None
    return round(min(max(open_price, trigger_low), trigger_high), 3)


def _weighted_exit_price(entry_price_raw: float, remaining_exit_raw: float, first_take_profit: float, partial_taken: bool) -> float:
    if not partial_taken:
        return remaining_exit_raw
    realized_ret = first_take_profit / entry_price_raw - 1
    remaining_ret = remaining_exit_raw / entry_price_raw - 1
    return entry_price_raw * (1 + 0.5 * realized_ret + 0.5 * remaining_ret)


def _simulate_setup_trade(
    signal: dict,
    bars: list[dict],
    signal_idx: int,
    market_states: dict[str, dict[str, Any]],
    max_hold_days: int = MAX_HOLD_DAYS,
    entry_window_days: int = ENTRY_WINDOW_DAYS,
    fee_bps: float = FEE_BPS,
    slippage_bps: float = SLIPPAGE_BPS,
    limit_threshold: float = LIMIT_THRESHOLD,
) -> Optional[dict[str, Any]]:
    signal_close = _safe_float(bars[signal_idx].get("close"))
    trigger_low = _safe_float(signal.get("trigger_low"))
    trigger_high = _safe_float(signal.get("trigger_high"))
    failure_level = _safe_float(signal.get("failure_level"))
    if signal_close is None or trigger_low is None or trigger_high is None or failure_level is None:
        return None

    entry_idx: Optional[int] = None
    entry_price_raw: Optional[float] = None
    entry_block_reasons: list[str] = []
    for idx in range(signal_idx + 1, min(signal_idx + 1 + entry_window_days, len(bars))):
        bar = bars[idx]
        prev_close = r3._prev_close_from_bars(bars, idx)
        open_price = _safe_float(bar.get("open"))
        low = _safe_float(bar.get("low"))
        if r3._is_suspended_or_untradeable(bar):
            entry_block_reasons.append("suspended_no_entry")
            continue
        if r3._is_limit_up(bar, prev_close, threshold=limit_threshold):
            entry_block_reasons.append("limit_up_cannot_buy")
            continue
        if open_price is not None and open_price > signal_close * 1.055:
            entry_block_reasons.append("gap_up_too_large")
            break
        if prev_close is not None and open_price is not None and open_price >= prev_close * (1 + limit_threshold * 0.82):
            entry_block_reasons.append("near_limit_up_abandoned")
            break
        if low is not None and low < failure_level * 0.995:
            entry_block_reasons.append("gap_down_or_breakdown")
            break
        price = _entry_price_for_zone(bar, trigger_low, trigger_high)
        if price is not None:
            entry_idx = idx
            entry_price_raw = price
            break
        entry_block_reasons.append("trigger_zone_not_touched")
    if entry_idx is None or entry_price_raw is None:
        return None

    hard_stop = entry_price_raw * 0.95
    setup_stop = max(failure_level, hard_stop)
    if setup_stop >= entry_price_raw * 0.995:
        setup_stop = entry_price_raw * 0.965
    first_take_profit = entry_price_raw * 1.035
    second_take_profit = entry_price_raw * 1.050
    position_pct = r4._risk_budget_position_pct(entry_price_raw, setup_stop, max_position_pct=MAX_POSITION_PCT)
    entry_price = entry_price_raw * (1 + slippage_bps / 10000)
    max_gain = -1.0
    blocked_exit_days = 0
    blocked_exit_original_reason = ""
    same_day_stop_take = False
    partial_taken = False
    exit_reason = "timeout"
    exit_idx = min(entry_idx + max_hold_days - 1, len(bars) - 1)
    exit_price_raw = _safe_float(bars[exit_idx].get("close")) or entry_price_raw

    for idx in range(entry_idx, min(entry_idx + max_hold_days, len(bars))):
        bar = bars[idx]
        prev_close = r3._prev_close_from_bars(bars, idx)
        date = str(bar.get("date") or "")
        high = _safe_float(bar.get("high")) or entry_price_raw
        low = _safe_float(bar.get("low")) or entry_price_raw
        close = _safe_float(bar.get("close")) or entry_price_raw
        holding_days = idx - entry_idx + 1
        max_gain = max(max_gain, high / entry_price_raw - 1)
        can_sell = r3._can_sell(bar, prev_close, limit_threshold)
        hit_setup_failure = low <= setup_stop
        hit_hard_stop = low <= hard_stop
        hit_first_take = high >= first_take_profit
        hit_second_take = high >= second_take_profit
        desired_exit: Optional[tuple[str, float]] = None

        if (hit_setup_failure or hit_hard_stop) and hit_first_take:
            same_day_stop_take = True
            desired_exit = ("same_day_stop_take_conservative", setup_stop if hit_setup_failure else hard_stop)
        elif hit_setup_failure:
            desired_exit = ("setup_failure_exit", setup_stop)
        elif hit_hard_stop:
            desired_exit = ("stop_loss", hard_stop)
        elif hit_second_take:
            partial_taken = True
            desired_exit = ("take_profit", _weighted_exit_price(entry_price_raw, second_take_profit, first_take_profit, partial_taken))
        elif hit_first_take:
            partial_taken = True

        market_state = (market_states.get(date) or {}).get("market_state")
        closes = [_safe_float(item.get("close")) for item in bars]
        ma5 = _ma(closes, idx, 5)
        if desired_exit is None and market_state in {"neutral", "defensive"} and holding_days > 1:
            desired_exit = ("environment_exit", _weighted_exit_price(entry_price_raw, close, first_take_profit, partial_taken))
        elif desired_exit is None and holding_days >= DEFAULT_OBSERVE_DAYS and max_gain < 0.015 and ma5 is not None and close < ma5:
            desired_exit = ("time_stop", _weighted_exit_price(entry_price_raw, close, first_take_profit, partial_taken))
        elif desired_exit is None and holding_days >= max_hold_days:
            desired_exit = ("timeout", _weighted_exit_price(entry_price_raw, close, first_take_profit, partial_taken))

        if desired_exit is None:
            continue
        if not can_sell:
            blocked_exit_days += 1
            blocked_exit_original_reason = desired_exit[0]
            if idx < len(bars) - 1:
                continue
        exit_reason, exit_price_raw = desired_exit
        if blocked_exit_days > 0:
            exit_reason = "limit_down_blocked_exit"
        exit_idx = idx
        break

    exit_price = exit_price_raw * (1 - slippage_bps / 10000)
    gross_return = exit_price / entry_price - 1
    net_return = gross_return - fee_bps * 2 / 10000
    signal_date = str(bars[signal_idx].get("date") or "")
    entry_date = str(bars[entry_idx].get("date") or "")
    exit_date = str(bars[exit_idx].get("date") or "")
    return {
        "strategy_name": SETUP_STRATEGY_NAME,
        "setup_key": signal.get("setup_key"),
        "setup_label": signal.get("setup_label"),
        "symbol": signal.get("symbol"),
        "name": signal.get("name"),
        "industry": signal.get("industry") or signal.get("sector") or "行业缺失",
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "signal_price": round(signal_close, 3),
        "entry_price_raw": round(entry_price_raw, 3),
        "entry_price": round(entry_price, 3),
        "exit_price_raw": round(exit_price_raw, 3),
        "exit_price": round(exit_price, 3),
        "failure_level": round(failure_level, 3),
        "stop_loss": round(setup_stop, 3),
        "hard_stop": round(hard_stop, 3),
        "first_take_profit": round(first_take_profit, 3),
        "second_take_profit": round(second_take_profit, 3),
        "partial_take_profit": partial_taken,
        "holding_days": exit_idx - entry_idx + 1,
        "exit_reason": exit_reason,
        "gross_return": round(gross_return, 6),
        "net_return": round(net_return, 6),
        "fee_bps_per_side": fee_bps,
        "slippage_bps_per_side": slippage_bps,
        "blocked_exit_days": blocked_exit_days,
        "blocked_exit_original_reason": blocked_exit_original_reason,
        "same_day_stop_take": same_day_stop_take,
        "entry_block_reasons": "；".join(entry_block_reasons),
        "position_pct": position_pct,
        "rank_score": signal.get("setup_score"),
        "setup_score": signal.get("setup_score"),
        "trigger_low": signal.get("trigger_low"),
        "trigger_high": signal.get("trigger_high"),
        "setup_tags_json": signal.get("setup_tags_json"),
        "market_state": (market_states.get(signal_date) or {}).get("market_state"),
        "rps20": signal.get("rps20"),
        "rps60": signal.get("rps60"),
        "close_to_20d_high": signal.get("close_to_20d_high"),
        "volatility_20d": signal.get("volatility_20d"),
        "amplitude": signal.get("amplitude"),
        "upper_shadow_ratio": signal.get("upper_shadow_ratio"),
        "change_rate_5d": signal.get("change_rate_5d"),
        "volume_ratio": signal.get("volume_ratio"),
        "sector_filter_pass": signal.get("sector_filter_pass"),
        "sector_filter_reason": signal.get("sector_filter_reason"),
        "industry_ret3_rank_pct": signal.get("industry_ret3_rank_pct"),
        "industry_ret5_rank_pct": signal.get("industry_ret5_rank_pct"),
        "industry_up_ratio": signal.get("industry_up_ratio"),
        "industry_above_ma20_ratio": signal.get("industry_above_ma20_ratio"),
        "industry_strong_count": signal.get("industry_strong_count"),
    }


def _run_setup_events(
    variant: SetupBacktestVariant,
    candidates_by_date: dict[str, list[dict[str, Any]]],
    histories: dict[str, list[dict]],
    market_states: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    trades: list[dict[str, Any]] = []
    next_available_by_symbol: dict[str, str] = {}
    for date in sorted(candidates_by_date):
        daily_opened = 0
        for signal in candidates_by_date[date]:
            if daily_opened >= MAX_SAME_DAY_NEW:
                break
            symbol = str(signal.get("_symbol_key") or signal.get("symbol") or "")
            if not symbol or next_available_by_symbol.get(symbol, "") >= date:
                continue
            bars = histories.get(symbol) or []
            trade = _simulate_setup_trade(signal, bars, int(signal["_signal_idx"]), market_states)
            if not trade:
                continue
            trade["strategy_key"] = variant.key
            trade["strategy_label"] = variant.label
            trades.append(trade)
            daily_opened += 1
            if trade.get("exit_date"):
                next_available_by_symbol[symbol] = str(trade["exit_date"])
    return pd.DataFrame(trades)


def _accept_setup_portfolio(trades: pd.DataFrame, initial_cash: float = INITIAL_CASH) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    active: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    daily_new: Counter[str] = Counter()
    for _, row in trades.sort_values(["entry_date", "rank_score"], ascending=[True, False]).iterrows():
        entry_date = str(row.get("entry_date"))
        industry = str(row.get("industry") or "行业缺失")
        active = [item for item in active if item["exit_date"] >= entry_date]
        current_exposure = sum(float(item["portfolio_position_pct"]) for item in active)
        industry_in_use = any(item["industry"] == industry for item in active)
        requested = (_safe_float(row.get("position_pct")) or MAX_POSITION_PCT) / 100
        actual = min(requested, MAX_TOTAL_EXPOSURE_PCT - current_exposure)
        skip_reason = ""
        if len(active) >= MAX_POSITIONS:
            skip_reason = "skipped_capacity"
        elif industry_in_use:
            skip_reason = "skipped_same_industry"
        elif daily_new[entry_date] >= MAX_SAME_DAY_NEW:
            skip_reason = "skipped_same_day_limit"
        elif actual <= 0:
            skip_reason = "skipped_exposure_limit"
        if skip_reason:
            rows.append({**row.to_dict(), "portfolio_action": skip_reason, "portfolio_position_pct": 0.0, "position_value": 0.0})
            continue
        accepted = {
            **row.to_dict(),
            "portfolio_action": "accepted",
            "portfolio_position_pct": round(actual, 6),
            "position_value": round(initial_cash * actual, 2),
        }
        rows.append(accepted)
        active.append({"exit_date": str(row["exit_date"]), "portfolio_position_pct": actual, "industry": industry})
        daily_new[entry_date] += 1
    return pd.DataFrame(rows)


def _max_consecutive_losses(trades: pd.DataFrame) -> int:
    if trades.empty or "net_return" not in trades.columns:
        return 0
    work = trades.copy()
    sort_cols = [col for col in ["exit_date", "entry_date", "signal_date"] if col in work.columns]
    if sort_cols:
        work = work.sort_values(sort_cols)
    current = 0
    best = 0
    for value in pd.to_numeric(work["net_return"], errors="coerce").fillna(0):
        if value < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return int(best)


def _monthly_stats(monthly: pd.DataFrame) -> dict[str, Any]:
    if monthly.empty:
        return {"positive_month_rate": None, "worst_month": None, "worst_month_return": None, "best_month": None, "best_month_return": None}
    work = monthly.copy()
    value_col = "monthly_return" if "monthly_return" in work.columns else "return"
    period_col = "month" if "month" in work.columns else "period"
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    work = work.dropna(subset=[value_col])
    if work.empty:
        return {"positive_month_rate": None, "worst_month": None, "worst_month_return": None, "best_month": None, "best_month_return": None}
    worst = work.sort_values(value_col).iloc[0].to_dict()
    best = work.sort_values(value_col, ascending=False).iloc[0].to_dict()
    return {
        "positive_month_rate": float((work[value_col] > 0).mean()),
        "worst_month": str(worst.get(period_col)),
        "worst_month_return": float(worst.get(value_col)),
        "best_month": str(best.get(period_col)),
        "best_month_return": float(best.get(value_col)),
    }


def _run_portfolio_for_variant(
    variant: SetupBacktestVariant,
    trades: pd.DataFrame,
    histories: dict[str, list[dict]],
    active_dates: list[str],
    output_dir: Path,
) -> dict[str, Any]:
    portfolio = _accept_setup_portfolio(trades, INITIAL_CASH)
    equity, monthly, metrics = r4._portfolio_equity_curve(portfolio, histories, active_dates, INITIAL_CASH)
    accepted = portfolio[portfolio["portfolio_action"] == "accepted"].copy() if not portfolio.empty and "portfolio_action" in portfolio else pd.DataFrame()
    prefix = f"{variant.key}_"
    _write_csv(output_dir / f"{prefix}trades.csv", trades)
    _write_csv(output_dir / f"{prefix}portfolio_trades.csv", portfolio)
    _write_csv(output_dir / f"{prefix}accepted_trades.csv", accepted)
    _write_csv(output_dir / f"{prefix}daily_equity.csv", equity)
    _write_csv(output_dir / f"{prefix}monthly_returns.csv", monthly)
    return {"variant": variant, "trades": trades, "portfolio": portfolio, "accepted": accepted, "equity": equity, "monthly": monthly, "metrics": metrics}


def _load_alpha040_reference() -> Optional[dict[str, Any]]:
    accepted_path = REFERENCE_DIR / "v3_atr_risk_budget_hot5_vol_risk_on_accepted_trades.csv"
    equity_path = REFERENCE_DIR / "v3_atr_risk_budget_hot5_vol_risk_on_daily_equity.csv"
    monthly_path = REFERENCE_DIR / "v3_atr_risk_budget_hot5_vol_risk_on_monthly_returns.csv"
    trades_path = REFERENCE_DIR / "v3_atr_risk_budget_hot5_vol_risk_on_trades.csv"
    if not accepted_path.exists() or not equity_path.exists() or not monthly_path.exists():
        return None
    accepted = pd.read_csv(accepted_path, encoding="utf-8-sig")
    equity = pd.read_csv(equity_path, encoding="utf-8-sig")
    monthly = pd.read_csv(monthly_path, encoding="utf-8-sig")
    trades = pd.read_csv(trades_path, encoding="utf-8-sig") if trades_path.exists() else accepted.copy()
    metrics = r4._portfolio_equity_curve(accepted.assign(portfolio_action="accepted") if "portfolio_action" not in accepted.columns else accepted, {}, list(equity["date"].astype(str)), INITIAL_CASH)[2]
    if not equity.empty:
        ending = float(pd.to_numeric(equity["equity"], errors="coerce").dropna().iloc[-1])
        daily_returns = pd.to_numeric(equity.get("daily_return"), errors="coerce").dropna() if "daily_return" in equity.columns else pd.to_numeric(equity["equity"], errors="coerce").pct_change().dropna()
        total_return = ending / INITIAL_CASH - 1
        annualized = (ending / INITIAL_CASH) ** (252 / max(1, len(equity))) - 1 if ending > 0 else None
        drawdown = pd.to_numeric(equity.get("drawdown"), errors="coerce") if "drawdown" in equity.columns else pd.to_numeric(equity["equity"], errors="coerce") / pd.to_numeric(equity["equity"], errors="coerce").cummax() - 1
        sharpe = None
        if len(daily_returns) > 1 and daily_returns.std(ddof=1) != 0:
            sharpe = float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252))
        max_dd = float(drawdown.min()) if len(drawdown.dropna()) else None
        metrics = {
            "accepted_trades": int(len(accepted)),
            "total_return": round(total_return, 6),
            "annualized_return": round(float(annualized), 6) if annualized is not None else None,
            "max_drawdown": round(float(max_dd), 6) if max_dd is not None else None,
            "sharpe": round(sharpe, 6) if sharpe is not None else None,
            "calmar": round(float(annualized / abs(max_dd)), 6) if annualized is not None and max_dd is not None and max_dd < 0 else None,
        }
    variant = SetupBacktestVariant(
        key="alpha040_v3_risk_controlled",
        label="对照：ranking-based V3",
        setup_keys=(),
    )
    return {"variant": variant, "trades": trades, "portfolio": accepted, "accepted": accepted, "equity": equity, "monthly": monthly, "metrics": metrics}


def _yearly_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in results.items():
        variant = result["variant"]
        equity = result["equity"].copy()
        accepted = result["accepted"].copy()
        if equity.empty:
            continue
        equity["date_dt"] = pd.to_datetime(equity["date"], errors="coerce")
        if not accepted.empty:
            accepted["entry_year"] = pd.to_datetime(accepted["entry_date"], errors="coerce").dt.year
        for year in [2021, 2022, 2023, 2024, 2025, 2026]:
            eq = equity[equity["date_dt"].dt.year == year].copy()
            trades = accepted[accepted["entry_year"] == year].copy() if not accepted.empty and "entry_year" in accepted.columns else pd.DataFrame()
            if eq.empty:
                continue
            start = float(eq["equity"].iloc[0])
            end = float(eq["equity"].iloc[-1])
            peak = eq["equity"].cummax()
            dd = eq["equity"] / peak - 1
            returns = pd.to_numeric(trades.get("net_return"), errors="coerce").dropna() if not trades.empty else pd.Series(dtype=float)
            rows.append(
                {
                    "strategy_key": key,
                    "strategy_label": variant.label,
                    "year": "2026 YTD" if year == 2026 else str(year),
                    "return": end / start - 1 if start else None,
                    "max_drawdown": float(dd.min()) if len(dd) else None,
                    "accepted_trade_count": int(len(trades)),
                    "win_rate": float((returns > 0).mean()) if len(returns) else None,
                    "average_trade_return": float(returns.mean()) if len(returns) else None,
                    "median_trade_return": float(returns.median()) if len(returns) else None,
                    "stop_loss_count": int((trades.get("exit_reason") == "stop_loss").sum()) if not trades.empty else 0,
                    "limit_down_blocked_exit_count": int((trades.get("exit_reason") == "limit_down_blocked_exit").sum()) if not trades.empty else 0,
                }
            )
    return pd.DataFrame(rows)


def _summary_rows(results: dict[str, dict[str, Any]], yearly: pd.DataFrame, daily_diag: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in results.items():
        variant = result["variant"]
        accepted = result["accepted"].copy()
        metrics = result["metrics"]
        monthly_stats = _monthly_stats(result["monthly"])
        returns = pd.to_numeric(accepted.get("net_return"), errors="coerce").dropna() if not accepted.empty else pd.Series(dtype=float)
        yr = yearly[yearly["strategy_key"] == key].copy() if not yearly.empty else pd.DataFrame()
        if not yr.empty:
            yr["return"] = pd.to_numeric(yr["return"], errors="coerce")
        worst_year = yr.dropna(subset=["return"]).sort_values("return").head(1).to_dict("records") if not yr.empty else []
        no_trade_count = None
        if key in set(daily_diag.get("strategy_key", pd.Series(dtype=str)).astype(str)):
            no_trade_count = int((daily_diag[daily_diag["strategy_key"] == key]["selected_signal_count"] == 0).sum())
        rows.append(
            {
                "strategy_key": key,
                "strategy_label": variant.label,
                "event_trade_count": int(len(result.get("trades", pd.DataFrame()))),
                "accepted_trade_count": int(metrics.get("accepted_trades") or len(accepted)),
                "no_trade_date_count": no_trade_count,
                "total_return": metrics.get("total_return"),
                "annualized_return": metrics.get("annualized_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe": metrics.get("sharpe"),
                "calmar": metrics.get("calmar"),
                "win_rate": float((returns > 0).mean()) if len(returns) else None,
                "average_trade_return": float(returns.mean()) if len(returns) else None,
                "median_trade_return": float(returns.median()) if len(returns) else None,
                "max_consecutive_losses": _max_consecutive_losses(accepted),
                "stop_loss_count": int((accepted.get("exit_reason") == "stop_loss").sum()) if not accepted.empty else 0,
                "limit_down_blocked_exit_count": int((accepted.get("exit_reason") == "limit_down_blocked_exit").sum()) if not accepted.empty else 0,
                "timeout_count": int((accepted.get("exit_reason") == "timeout").sum()) if not accepted.empty else 0,
                "setup_failure_exit_count": int((accepted.get("exit_reason") == "setup_failure_exit").sum()) if not accepted.empty else 0,
                "positive_year_rate": float((yr["return"] > 0).mean()) if not yr.empty else None,
                "positive_month_rate": monthly_stats.get("positive_month_rate"),
                "worst_year": worst_year[0]["year"] if worst_year else None,
                "worst_year_return": worst_year[0]["return"] if worst_year else None,
                "worst_month": monthly_stats.get("worst_month"),
                "worst_month_return": monthly_stats.get("worst_month_return"),
                "best_month": monthly_stats.get("best_month"),
                "best_month_return": monthly_stats.get("best_month_return"),
            }
        )
    return pd.DataFrame(rows)


def _monthly_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in results.items():
        monthly = result["monthly"].copy()
        for row in monthly.to_dict("records"):
            rows.append({"strategy_key": key, "strategy_label": result["variant"].label, **row})
    return pd.DataFrame(rows)


def _exit_reason_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in results.items():
        accepted = result["accepted"].copy()
        if not accepted.empty:
            accepted["industry"] = accepted.get("industry", "行业缺失").fillna("行业缺失").replace("", "行业缺失")
        for reason in EXPECTED_EXIT_REASONS:
            group = accepted[accepted["exit_reason"] == reason].copy() if not accepted.empty and "exit_reason" in accepted.columns else pd.DataFrame()
            returns = pd.to_numeric(group.get("net_return"), errors="coerce").dropna() if not group.empty else pd.Series(dtype=float)
            rows.append(
                {
                    "strategy_key": key,
                    "strategy_label": result["variant"].label,
                    "exit_reason": reason,
                    "trade_count": int(len(group)),
                    "win_rate": float((returns > 0).mean()) if len(returns) else None,
                    "average_trade_return": float(returns.mean()) if len(returns) else None,
                    "median_trade_return": float(returns.median()) if len(returns) else None,
                    "max_loss": float(returns.min()) if len(returns) else None,
                }
            )
    return pd.DataFrame(rows)


def _industry_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in results.items():
        accepted = result["accepted"].copy()
        if accepted.empty:
            continue
        accepted["industry"] = accepted.get("industry", "行业缺失").fillna("行业缺失").replace("", "行业缺失")
        accepted["return_contribution"] = (
            pd.to_numeric(accepted.get("net_return"), errors="coerce").fillna(0)
            * pd.to_numeric(accepted.get("portfolio_position_pct"), errors="coerce").fillna(0)
        )
        total = len(accepted)
        for industry, group in accepted.groupby("industry", dropna=False):
            returns = pd.to_numeric(group.get("net_return"), errors="coerce").dropna()
            rows.append(
                {
                    "strategy_key": key,
                    "strategy_label": result["variant"].label,
                    "industry": industry,
                    "trade_count": int(len(group)),
                    "trade_count_share": float(len(group) / total) if total else None,
                    "win_rate": float((returns > 0).mean()) if len(returns) else None,
                    "average_trade_return": float(returns.mean()) if len(returns) else None,
                    "median_trade_return": float(returns.median()) if len(returns) else None,
                    "stop_loss_count": int((group.get("exit_reason") == "stop_loss").sum()),
                    "setup_failure_exit_count": int((group.get("exit_reason") == "setup_failure_exit").sum()),
                    "limit_down_blocked_exit_count": int((group.get("exit_reason") == "limit_down_blocked_exit").sum()),
                    "cumulative_contribution": float(group["return_contribution"].sum()),
                    "industry_missing_count": int((group["industry"] == "行业缺失").sum()),
                }
            )
    return pd.DataFrame(rows)


def _market_filter_diagnosis(daily_diag: pd.DataFrame) -> pd.DataFrame:
    if daily_diag.empty:
        return pd.DataFrame()
    rows = []
    for (strategy_key, market_state), group in daily_diag.groupby(["strategy_key", "market_state"], dropna=False):
        rows.append(
            {
                "strategy_key": strategy_key,
                "market_state": market_state,
                "date_count": int(len(group)),
                "no_trade_market_filter_count": int((group["no_trade_reason"] == "no_trade_market_filter").sum()),
                "no_trade_date_count": int((group["selected_signal_count"] == 0).sum()),
                "average_universe_count": float(pd.to_numeric(group["universe_count"], errors="coerce").mean()),
                "average_setup_before_sector": float(pd.to_numeric(group["setup_signal_count_before_sector"], errors="coerce").mean()),
                "average_selected_signal_count": float(pd.to_numeric(group["selected_signal_count"], errors="coerce").mean()),
            }
        )
    return pd.DataFrame(rows)


def _sector_filter_diagnosis(daily_diag: pd.DataFrame, signal_log: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not daily_diag.empty:
        for strategy_key, group in daily_diag.groupby("strategy_key"):
            rows.append(
                {
                    "strategy_key": strategy_key,
                    "diagnosis_level": "daily",
                    "industry": "ALL",
                    "date_count": int(len(group)),
                    "setup_before_sector": int(pd.to_numeric(group["setup_signal_count_before_sector"], errors="coerce").sum()),
                    "setup_after_sector": int(pd.to_numeric(group["setup_signal_count_after_sector"], errors="coerce").sum()),
                    "no_trade_sector_filter_count": int((group["no_trade_reason"] == "no_trade_sector_filter").sum()),
                }
            )
    if not signal_log.empty:
        for (setup_key, industry), group in signal_log.groupby(["setup_key", "industry"], dropna=False):
            rows.append(
                {
                    "strategy_key": setup_key,
                    "diagnosis_level": "signal_industry",
                    "industry": industry,
                    "date_count": int(group["date"].nunique()),
                    "setup_before_sector": int(len(group)),
                    "setup_after_sector": int((group["sector_filter_pass"] == True).sum()),
                    "no_trade_sector_filter_count": int((group["sector_filter_pass"] != True).sum()),
                }
            )
    return pd.DataFrame(rows)


def _render_trade_examples(output_dir: Path, results: dict[str, dict[str, Any]], daily_diag: pd.DataFrame) -> None:
    lines = ["# Setup Trade Examples", "", "仅展示 shadow 回测样例，用于检查形态路径和退出原因，不构成买卖建议。", ""]
    for key, result in results.items():
        if key == "alpha040_v3_risk_controlled":
            continue
        accepted = result["accepted"].copy()
        if accepted.empty:
            lines.extend([f"## {key}", "", "No accepted trades.", ""])
            continue
        accepted["_return"] = pd.to_numeric(accepted["net_return"], errors="coerce")
        samples = pd.concat([accepted.sort_values("_return", ascending=False).head(3), accepted.sort_values("_return").head(3)])
        lines.extend([f"## {key}", ""])
        lines.extend(
            _markdown_table(
                samples,
                [
                    ("setup", "setup_key"),
                    ("标的", "symbol"),
                    ("名称", "name"),
                    ("行业", "industry"),
                    ("信号日", "signal_date"),
                    ("买入日", "entry_date"),
                    ("退出日", "exit_date"),
                    ("收益", "net_return"),
                    ("退出原因", "exit_reason"),
                    ("市场", "market_state"),
                    ("分数", "setup_score"),
                ],
            )
        )
        lines.append("")
    no_trade = daily_diag[daily_diag["selected_signal_count"] == 0].copy() if not daily_diag.empty else pd.DataFrame()
    lines.extend(["## No-Trade Samples", ""])
    if no_trade.empty:
        lines.append("No no-trade dates in diagnostics.")
    else:
        lines.extend(
            _markdown_table(
                no_trade.head(20),
                [
                    ("日期", "date"),
                    ("版本", "strategy_key"),
                    ("市场", "market_state"),
                    ("原因", "no_trade_reason"),
                    ("setup 前", "setup_signal_count_before_sector"),
                    ("setup 后", "setup_signal_count_after_sector"),
                ],
            )
        )
    (output_dir / "setup_trade_examples.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_no_trade_report(output_dir: Path, daily_diag: pd.DataFrame) -> None:
    lines = ["# Setup No-Trade Report", "", "no-trade 是 setup-based 策略的正常输出：没有市场、板块或形态共振时不应强行交易。", ""]
    if daily_diag.empty:
        lines.append("No diagnostics available.")
    else:
        reason = daily_diag.groupby(["strategy_key", "no_trade_reason"]).size().reset_index(name="date_count")
        lines.extend(["## Reason Counts", ""])
        lines.extend(_markdown_table(reason.sort_values(["strategy_key", "date_count"], ascending=[True, False]), [("版本", "strategy_key"), ("原因", "no_trade_reason"), ("日期数", "date_count")]))
        lines.extend(["", "## Market State Counts", ""])
        state = daily_diag.groupby(["strategy_key", "market_state"]).size().reset_index(name="date_count")
        lines.extend(_markdown_table(state, [("版本", "strategy_key"), ("市场状态", "market_state"), ("日期数", "date_count")]))
    (output_dir / "setup_no_trade_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_main_report(
    output_dir: Path,
    summary: pd.DataFrame,
    yearly: pd.DataFrame,
    market_diag: pd.DataFrame,
    sector_diag: pd.DataFrame,
    exit_reason: pd.DataFrame,
) -> None:
    ranked = summary.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).copy()
    setup_only = ranked[ranked["strategy_key"] != "alpha040_v3_risk_controlled"].copy()
    best_setup = setup_only.iloc[0].to_dict() if not setup_only.empty else {}
    v3 = summary[summary["strategy_key"] == "alpha040_v3_risk_controlled"].head(1).to_dict("records")
    all_setups_negative = bool((pd.to_numeric(setup_only["total_return"], errors="coerce") < 0).all()) if not setup_only.empty else True
    median_improved = None
    if v3 and not setup_only.empty:
        v3_median = _safe_float(v3[0].get("median_trade_return"))
        best_median = _safe_float(best_setup.get("median_trade_return"))
        if v3_median is not None and best_median is not None:
            median_improved = best_median > v3_median
    lines = [
        "# Setup-Based Short Swing Shadow Report",
        "",
        "本报告新增 `setup_based_short_swing_v1` 影子研究模块。它不修改当前主策略 `alpha040_v3_risk_controlled`，不连接实盘，不自动下单。",
        "",
        "## Backtest Boundary",
        "",
        "- 数据：2021-01-04 至 2026-06-26 扩展日线，当前为 efinance 单源扩展；行业映射使用 BaoStock/expanded metadata。",
        "- 执行：信号日只生成计划，次日最多观察 2 天触发；默认观察 3 天，最长持有 5 天。",
        "- 仓位：最大同时持仓 2 只，同行业最多 1 只，单票风险预算，单票名义仓位上限保守。",
        "- 允许 no-trade：市场层、板块层、setup 层任一不通过都可以不交易。",
        "",
        "## Strategy Summary",
        "",
    ]
    lines.extend(
        _markdown_table(
            ranked,
            [
                ("版本", "strategy_key"),
                ("事件交易", "event_trade_count"),
                ("接受交易", "accepted_trade_count"),
                ("no-trade 日期", "no_trade_date_count"),
                ("累计收益", "total_return"),
                ("年化", "annualized_return"),
                ("最大回撤", "max_drawdown"),
                ("Sharpe", "sharpe"),
                ("Calmar", "calmar"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_trade_return"),
                ("中位数", "median_trade_return"),
                ("最大连亏", "max_consecutive_losses"),
                ("止损", "stop_loss_count"),
                ("跌停无法卖出", "limit_down_blocked_exit_count"),
                ("timeout", "timeout_count"),
                ("setup失败", "setup_failure_exit_count"),
                ("正收益年份", "positive_year_rate"),
                ("正收益月份", "positive_month_rate"),
                ("最差年份", "worst_year"),
                ("最差月份", "worst_month"),
            ],
        )
    )
    lines.extend(["", "## Yearly Performance", ""])
    lines.extend(
        _markdown_table(
            yearly,
            [
                ("版本", "strategy_key"),
                ("年份", "year"),
                ("收益", "return"),
                ("最大回撤", "max_drawdown"),
                ("接受交易", "accepted_trade_count"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_trade_return"),
                ("中位数", "median_trade_return"),
                ("止损", "stop_loss_count"),
                ("跌停无法卖出", "limit_down_blocked_exit_count"),
            ],
            max_rows=80,
        )
    )
    lines.extend(["", "## Exit Reason Snapshot", ""])
    risk_exits = exit_reason[exit_reason["exit_reason"].isin(["stop_loss", "setup_failure_exit", "limit_down_blocked_exit", "timeout"])].copy()
    lines.extend(
        _markdown_table(
            risk_exits,
            [
                ("版本", "strategy_key"),
                ("退出", "exit_reason"),
                ("笔数", "trade_count"),
                ("胜率", "win_rate"),
                ("平均", "average_trade_return"),
                ("中位", "median_trade_return"),
                ("最大亏损", "max_loss"),
            ],
            max_rows=80,
        )
    )
    market_contribution = "待观察"
    sector_contribution = "待观察"
    if not summary.empty:
        combined = summary[summary["strategy_key"] == "setup_combined_v1"].head(1)
        no_market = summary[summary["strategy_key"] == "setup_combined_no_market_filter"].head(1)
        no_sector = summary[summary["strategy_key"] == "setup_combined_no_sector_filter"].head(1)
        if not combined.empty and not no_market.empty:
            market_contribution = "有正贡献" if float(combined.iloc[0]["total_return"]) > float(no_market.iloc[0]["total_return"]) else "未显示正贡献"
        if not combined.empty and not no_sector.empty:
            sector_contribution = "有正贡献" if float(combined.iloc[0]["total_return"]) > float(no_sector.iloc[0]["total_return"]) else "未显示正贡献"
    lines.extend(
        [
            "",
            "## A. 已确认事实",
            "",
            f"- 当前对照组是 `alpha040_v3_risk_controlled`；本轮只新增 setup-based shadow research。",
            f"- 最好 setup 版本：`{best_setup.get('strategy_key', '-')}`，累计收益 {_fmt_pct(best_setup.get('total_return'))}，最大回撤 {_fmt_pct(best_setup.get('max_drawdown'))}，接受交易 {best_setup.get('accepted_trade_count', '-')} 笔。",
            f"- setup 版本是否全部为负：{'是' if all_setups_negative else '否'}。",
            "- no-trade 日期被保留为有效研究结果，不会被填充成强制交易。",
            "",
            "## B. setup-based 是否优于 ranking-based",
            "",
        ]
    )
    if v3 and best_setup:
        lines.append(
            f"- V3 对照累计收益 {_fmt_pct(v3[0].get('total_return'))}，最好 setup 累计收益 {_fmt_pct(best_setup.get('total_return'))}。"
        )
        lines.append(
            f"- 中位数收益是否改善：{'是' if median_improved else '否或不明显'}。"
        )
    if all_setups_negative:
        lines.append("- setup 方向暂未通过长样本收益验证，但可作为进一步形态研究方向。")
    else:
        lines.append("- 至少一个 setup 版本转正，只能列为下一阶段 shadow candidate，不能并入主策略。")
    lines.extend(
        [
            "",
            "## C. 三类 setup 的表现",
            "",
        ]
    )
    setup_rows = summary[summary["strategy_key"].isin(["pullback_reclaim_v1", "volatility_contraction_breakout_v1", "panic_repair_v1"])].copy()
    lines.extend(
        _markdown_table(
            setup_rows,
            [
                ("setup", "strategy_key"),
                ("接受交易", "accepted_trade_count"),
                ("累计收益", "total_return"),
                ("最大回撤", "max_drawdown"),
                ("胜率", "win_rate"),
                ("平均", "average_trade_return"),
                ("中位", "median_trade_return"),
                ("setup失败", "setup_failure_exit_count"),
                ("跌停无法卖出", "limit_down_blocked_exit_count"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## D. 市场层和板块层贡献",
            "",
            f"- 市场层过滤贡献判断：{market_contribution}。",
            f"- 板块层过滤贡献判断：{sector_contribution}。",
            "- 详细每日过滤数量见 `setup_market_filter_diagnosis.csv` 和 `setup_sector_filter_diagnosis.csv`。",
            "",
            "## E. 仍然失败的地方",
            "",
            "- 当前只使用日线数据，无法确认次日触发区间内的真实成交路径。",
            "- 板块只使用行业映射，不包含真实概念题材和盘中资金共振。",
            "- 如果 setup 仍为负，说明问题不是单纯 alpha040，而是日线短线事件定义仍不足以覆盖真实优势。",
            "",
            "## F. 是否需要分钟数据",
            "",
            "- 需要，但不是为了继续堆因子；主要用于验证触发区间、冲高回落、跳空、接近涨停和失败点的真实先后顺序。",
            "- 在 setup 规则没有出现可观察优势前，分钟数据应作为执行路径验证，而不是新一轮调参入口。",
            "",
            "## G. 下一步建议",
            "",
            "- 先观察三类 setup 中交易更少、止损更少、中位数更好的候选，而不是追求最高累计收益。",
            "- 若某个 setup 转正，进入 freeze-style 复跑和 forward paper trading，仍不并入主策略。",
            "- 若所有 setup 为负，暂停从全市场日线选股里寻找默认交易日信号，改做更严格的事件样本库和人工复盘标签。",
        ]
    )
    (output_dir / "setup_based_short_swing_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_design_doc(path: Path) -> None:
    lines = [
        "# Setup-Based Strategy Design",
        "",
        "## 1. 为什么从 ranking-based 转向 setup-based",
        "",
        "2021 起长样本显示，`alpha040_v3_risk_controlled` 和多组短线 ranking shadow 版本收益端均未通过验证。继续在同一横截面排序框架里调 alpha040、RPS 或持有期，容易把问题误判为参数问题。新的研究方向改为事件型 setup：没有明确形态、市场和板块共振时允许不交易。",
        "",
        "## 2. 三类 setup 的定义",
        "",
        "- `pullback_reclaim_v1`：过去 20 日相对强，3-5 日回踩不破 MA20，量能收缩，当日重新站回 MA5/MA10。",
        "- `volatility_contraction_breakout_v1`：10-20 日低波动平台，靠近 20 日高点，温和放量突破平台上沿。",
        "- `panic_repair_v1`：3-10 日恐慌下跌后，不抢第一根修复，等待重新站上前高或 MA5 的确认。",
        "",
        "## 3. 市场层和板块层的作用",
        "",
        "市场层把行情分为 `strong_active`、`weak_active`、`neutral`、`defensive`。只有 strong/weak active 允许开仓。板块层使用 BaoStock 行业映射，要求行业短期收益、上涨比例、MA20 宽度和同步转强数量相对全市场占优，避免个股孤立强势。",
        "",
        "## 4. 为什么 no-trade 是正确结果",
        "",
        "短线 setup 的目标不是每天选出几只股票，而是在市场、板块和个股形态同时满足时才生成计划。没有 setup 时不交易，比强行交易更符合风险控制。",
        "",
        "## 5. 为什么当前仍不能实盘",
        "",
        "本模块只使用日线数据，且当前扩展数据为 efinance 单源口径。它无法验证盘中触发顺序、买卖盘口、涨停附近成交概率，也没有经过 forward paper trading 积累。",
        "",
        "## 6. 为什么短线 setup 后续需要分钟数据验证",
        "",
        "短线胜负往往取决于次日触发区间、冲高回落、跳空低开破位、接近涨停和失败点的先后顺序。日线只能做保守代理，分钟数据用于验证执行路径，不应被用来无限调参。",
        "",
        "## 7. 为什么当前只做 shadow research",
        "",
        "当前主策略和 legacy 回滚机制保持不变。setup-based 方向必须先通过长样本、压力测试和 forward paper trading，至少累计 30-50 笔后再讨论是否升级。",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run_setup_based_short_swing(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    index_dir: Path = DEFAULT_INDEX_DIR,
    strategy_path: Path = DEFAULT_STRATEGY_PATH,
    max_symbols: Optional[int] = None,
    min_bars: int = 80,
    min_history: int = 60,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    scanner._set_active_config(scanner._load_scanner_config(str(strategy_path)))
    histories = fr.load_cached_histories(cache_dir=cache_dir, max_symbols=max_symbols, min_bars=min_bars)
    if not histories:
        raise RuntimeError(f"未找到可用历史 K 线缓存：{cache_dir}")
    metadata = _load_expanded_metadata()
    alpha040_map = r3._build_alpha040_map(histories)
    universe_by_date, daily_universe = r3._build_dynamic_universe(
        histories,
        metadata,
        alpha040_map,
        min_history,
        output_dir / "universe_audit",
    )
    active_dates = [date for date, rows in sorted(universe_by_date.items()) if rows]
    market_states, market_state_rows = _build_market_states(histories, index_dir)
    sector_by_date, sector_stats_rows = _build_sector_stats(universe_by_date, histories, market_states)
    candidates_by_variant, daily_diag, signal_log = _build_setup_candidates(universe_by_date, histories, market_states, sector_by_date)

    _write_csv(output_dir / "daily_universe.csv", daily_universe)
    _write_csv(output_dir / "setup_market_states.csv", market_state_rows)
    _write_csv(output_dir / "setup_sector_stats.csv", sector_stats_rows)
    _write_csv(output_dir / "setup_daily_diagnostics.csv", daily_diag)
    _write_csv(output_dir / "setup_signal_log.csv", signal_log)

    results: dict[str, dict[str, Any]] = {}
    reference = _load_alpha040_reference()
    if reference:
        results["alpha040_v3_risk_controlled"] = reference
    for variant in SETUP_VARIANTS:
        trades = _run_setup_events(variant, candidates_by_variant[variant.key], histories, market_states)
        results[variant.key] = _run_portfolio_for_variant(variant, trades, histories, active_dates, output_dir)

    yearly = _yearly_rows(results)
    summary = _summary_rows(results, yearly, daily_diag)
    monthly = _monthly_rows(results)
    market_diag = _market_filter_diagnosis(daily_diag)
    sector_diag = _sector_filter_diagnosis(daily_diag, signal_log)
    exit_reason = _exit_reason_rows(results)
    industry = _industry_rows(results)

    _write_csv(output_dir / "setup_strategy_summary.csv", summary)
    _write_csv(output_dir / "setup_yearly_performance.csv", yearly)
    _write_csv(output_dir / "setup_monthly_returns.csv", monthly)
    _write_csv(output_dir / "setup_market_filter_diagnosis.csv", market_diag)
    _write_csv(output_dir / "setup_sector_filter_diagnosis.csv", sector_diag)
    _write_csv(output_dir / "setup_exit_reason_summary.csv", exit_reason)
    _write_csv(output_dir / "setup_industry_performance.csv", industry)
    _render_trade_examples(output_dir, results, daily_diag)
    _render_no_trade_report(output_dir, daily_diag)
    _render_main_report(output_dir, summary, yearly, market_diag, sector_diag, exit_reason)
    _write_design_doc(ROOT / "docs" / "setup_based_strategy_design.md")

    ranked = summary[summary["strategy_key"] != "alpha040_v3_risk_controlled"].sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    best = ranked.head(1).to_dict("records")
    payload = {
        "strategy_name": SETUP_STRATEGY_NAME,
        "history_symbol_count": len(histories),
        "active_date_count": len(active_dates),
        "market_state_count": len(market_state_rows),
        "daily_universe_rows": int(len(daily_universe)),
        "output_dir": str(output_dir),
        "best_setup_candidate": best[0] if best else None,
        "main_strategy_changed": False,
    }
    _write_json(output_dir / "summary.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run setup-based short swing shadow research.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="扩展日线 per-symbol 缓存目录")
    parser.add_argument("--index-dir", default=str(DEFAULT_INDEX_DIR), help="扩展指数缓存目录")
    parser.add_argument("--strategy", default=str(DEFAULT_STRATEGY_PATH), help="策略配置，只读")
    parser.add_argument("--max-symbols", type=int, help="调试用：最多读取多少只股票")
    parser.add_argument("--min-bars", type=int, default=80)
    parser.add_argument("--min-history", type=int, default=60)
    args = parser.parse_args()
    payload = run_setup_based_short_swing(
        output_dir=Path(args.output),
        cache_dir=Path(args.cache_dir),
        index_dir=Path(args.index_dir),
        strategy_path=Path(args.strategy),
        max_symbols=args.max_symbols,
        min_bars=args.min_bars,
        min_history=args.min_history,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=fr._json_default))


if __name__ == "__main__":
    main()
