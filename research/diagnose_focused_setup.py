#!/usr/bin/env python3
"""Focused diagnostics for setup-based short swing shadow strategies.

Reads existing setup shadow backtest outputs and expanded daily bars. It does
not change strategy configuration, thresholds, ledgers, or live trading state.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "output" / "setup_based_short_swing"
DEFAULT_KLINE_DIR = ROOT / "data" / "expanded" / "daily_kline"
FOCUS_SETUPS = ["volatility_contraction_breakout_v1", "pullback_reclaim_v1"]
THRESHOLDS = [0.01, 0.02, 0.03, 0.04, 0.05]


def _safe_float(value: Any) -> Optional[float]:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _markdown_table(df: pd.DataFrame, columns: list[tuple[str, str]], max_rows: Optional[int] = None) -> list[str]:
    if df.empty:
        return ["No rows."]
    work = df.head(max_rows) if max_rows else df
    lines = ["| " + " | ".join(title for title, _col in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    pct_cols = {
        "net_return",
        "total_return",
        "max_drawdown",
        "win_rate",
        "average_trade_return",
        "median_trade_return",
        "average_return",
        "median_return",
        "mfe_5d",
        "mae_5d",
        "avg_mfe_5d",
        "median_mfe_5d",
        "avg_mae_5d",
        "median_mae_5d",
        "mfe_until_exit",
        "mae_until_exit",
        "day1_return",
        "day2_return",
        "day3_return",
        "day5_return",
        "cumulative_contribution",
        "trade_count_share",
        "setup_failure_rate",
        "limit_down_blocked_exit_rate",
        "environment_exit_rate",
        "pre_5d_return",
        "pre_10d_return",
        "pre_20d_return",
        "entry_gap_pct",
        "entry_fade_pct",
        "entry_chase_pct",
        "ma20_distance_signal",
        "turnover_contraction",
        "turnover_ratio_tag",
        "vol10_to_vol20_tag",
        "close_to_20d_high",
        "signal_upper_shadow_ratio",
        "hit_up_2pct_rate",
        "hit_up_3pct_rate",
        "hit_down_2pct_rate",
        "hit_down_3pct_rate",
    }
    for row in work.to_dict("records"):
        cells: list[str] = []
        for _title, col in columns:
            value = row.get(col)
            if col in pct_cols or col.endswith("_return") or "drawdown" in col:
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


def _symbol_to_path(kline_dir: Path, symbol: str) -> Path:
    return kline_dir / f"{symbol.replace('.', '_')}.csv"


def _load_bars(kline_dir: Path, symbols: list[str]) -> dict[str, pd.DataFrame]:
    histories: dict[str, pd.DataFrame] = {}
    for symbol in sorted(set(symbols)):
        path = _symbol_to_path(kline_dir, symbol)
        if not path.exists():
            continue
        frame = pd.read_csv(path, encoding="utf-8-sig").sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
        histories[symbol] = frame
    return histories


def _date_index(frame: pd.DataFrame, date: str) -> Optional[int]:
    matches = frame.index[frame["date"].astype(str) == str(date)].tolist()
    return int(matches[0]) if matches else None


def _ret(frame: pd.DataFrame, idx: int, window: int) -> Optional[float]:
    if idx - window < 0 or idx >= len(frame):
        return None
    close = _safe_float(frame.loc[idx, "close"])
    prev = _safe_float(frame.loc[idx - window, "close"])
    if close is None or prev is None or prev <= 0:
        return None
    return close / prev - 1


def _ma(frame: pd.DataFrame, idx: int, window: int) -> Optional[float]:
    if idx - window + 1 < 0:
        return None
    values = pd.to_numeric(frame.loc[idx - window + 1 : idx, "close"], errors="coerce").dropna()
    if len(values) < window:
        return None
    return float(values.mean())


def _shadow_ratio(row: pd.Series, side: str) -> Optional[float]:
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    open_price = _safe_float(row.get("open"))
    close = _safe_float(row.get("close"))
    if high is None or low is None or open_price is None or close is None or high <= low:
        return None
    body_high = max(open_price, close)
    body_low = min(open_price, close)
    if side == "upper":
        return max(0.0, (high - body_high) / (high - low))
    return max(0.0, (body_low - low) / (high - low))


def _load_focus_trades(output_dir: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for setup in FOCUS_SETUPS:
        path = output_dir / f"{setup}_accepted_trades.csv"
        if not path.exists():
            raise FileNotFoundError(path)
        frame = pd.read_csv(path, encoding="utf-8-sig")
        frame["strategy_key"] = setup
        frames.append(frame)
    trades = pd.concat(frames, ignore_index=True)
    for col in [
        "entry_price_raw",
        "exit_price_raw",
        "net_return",
        "position_pct",
        "portfolio_position_pct",
        "rank_score",
        "setup_score",
        "close_to_20d_high",
        "volatility_20d",
        "amplitude",
        "upper_shadow_ratio",
        "change_rate_5d",
        "volume_ratio",
        "industry_ret3_rank_pct",
        "industry_ret5_rank_pct",
        "industry_up_ratio",
        "industry_above_ma20_ratio",
        "industry_strong_count",
    ]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    return trades


def _parse_tags(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def _first_thresholds(horizon: pd.DataFrame, entry: float) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for threshold in THRESHOLDS:
        label = f"{int(threshold * 100)}pct"
        first_up = None
        first_down = None
        for offset, (_idx, row) in enumerate(horizon.iterrows(), start=1):
            high = _safe_float(row.get("high"))
            low = _safe_float(row.get("low"))
            if first_up is None and high is not None and high / entry - 1 >= threshold:
                first_up = offset
            if first_down is None and low is not None and low / entry - 1 <= -threshold:
                first_down = offset
        result[f"hit_up_{label}"] = first_up is not None
        result[f"first_up_{label}_day"] = first_up
        result[f"hit_down_{label}"] = first_down is not None
        result[f"first_down_{label}_day"] = first_down
        if first_up is None and first_down is None:
            first_touch = "none"
        elif first_up is None:
            first_touch = "down"
        elif first_down is None:
            first_touch = "up"
        elif first_up < first_down:
            first_touch = "up"
        elif first_down < first_up:
            first_touch = "down"
        else:
            first_touch = "same_day_both"
        result[f"first_touch_{label}"] = first_touch
    return result


def _sector_lookup(output_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    path = output_dir / "setup_sector_stats.csv"
    if not path.exists():
        return {}
    frame = pd.read_csv(path, encoding="utf-8-sig")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        result[(str(row.get("date")), str(row.get("industry")))] = row
    return result


def _enrich_mfe_mae(trades: pd.DataFrame, histories: dict[str, pd.DataFrame], sector_stats: dict[tuple[str, str], dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for trade in trades.to_dict("records"):
        symbol = str(trade.get("symbol"))
        frame = histories.get(symbol)
        if frame is None or frame.empty:
            continue
        entry_idx = _date_index(frame, str(trade.get("entry_date")))
        exit_idx = _date_index(frame, str(trade.get("exit_date")))
        signal_idx = _date_index(frame, str(trade.get("signal_date")))
        entry_price = _safe_float(trade.get("entry_price_raw"))
        if entry_idx is None or signal_idx is None or entry_price is None or entry_price <= 0:
            continue
        if exit_idx is None:
            exit_idx = min(entry_idx + int(trade.get("holding_days") or 5) - 1, len(frame) - 1)
        horizon_end = min(entry_idx + 4, len(frame) - 1)
        exit_end = min(max(exit_idx, entry_idx), len(frame) - 1)
        horizon = frame.loc[entry_idx:horizon_end].copy()
        held = frame.loc[entry_idx:exit_end].copy()
        max_high_5d = pd.to_numeric(horizon["high"], errors="coerce").max()
        min_low_5d = pd.to_numeric(horizon["low"], errors="coerce").min()
        max_high_exit = pd.to_numeric(held["high"], errors="coerce").max()
        min_low_exit = pd.to_numeric(held["low"], errors="coerce").min()
        tags = _parse_tags(trade.get("setup_tags_json"))
        enriched = dict(trade)
        enriched.update(
            {
                "mfe_5d": float(max_high_5d / entry_price - 1) if pd.notna(max_high_5d) else None,
                "mae_5d": float(min_low_5d / entry_price - 1) if pd.notna(min_low_5d) else None,
                "mfe_until_exit": float(max_high_exit / entry_price - 1) if pd.notna(max_high_exit) else None,
                "mae_until_exit": float(min_low_exit / entry_price - 1) if pd.notna(min_low_exit) else None,
                "pre_5d_return": _ret(frame, signal_idx, 5),
                "pre_10d_return": _ret(frame, signal_idx, 10),
                "pre_20d_return": _ret(frame, signal_idx, 20),
                "ma20_distance_signal": None,
                "entry_gap_pct": None,
                "entry_fade_pct": None,
                "entry_chase_pct": None,
                "signal_upper_shadow_ratio": tags.get("upper_shadow_ratio", trade.get("upper_shadow_ratio")),
                "turnover_contraction": tags.get("turnover_contraction"),
                "turnover_ratio_tag": tags.get("turnover_ratio"),
                "vol10_to_vol20_tag": (tags.get("vol10") / tags.get("vol20")) if tags.get("vol10") is not None and tags.get("vol20") else None,
                "platform_range20_tag": tags.get("range20"),
                "panic_drawdown10_tag": tags.get("drawdown10"),
                "confirm_above_prev_high_tag": tags.get("confirm_above_prev_high"),
            }
        )
        ma20 = _ma(frame, signal_idx, 20)
        signal_close = _safe_float(frame.loc[signal_idx, "close"])
        if ma20 and signal_close:
            enriched["ma20_distance_signal"] = signal_close / ma20 - 1
        if entry_idx > 0:
            prev_close = _safe_float(frame.loc[entry_idx - 1, "close"])
            entry_open = _safe_float(frame.loc[entry_idx, "open"])
            entry_close = _safe_float(frame.loc[entry_idx, "close"])
            if prev_close and entry_open:
                enriched["entry_gap_pct"] = entry_open / prev_close - 1
            if entry_open and entry_close:
                enriched["entry_fade_pct"] = entry_close / entry_open - 1
            if signal_close:
                enriched["entry_chase_pct"] = entry_price / signal_close - 1
        for day in [1, 2, 3, 5]:
            idx = entry_idx + day - 1
            if idx < len(frame):
                close = _safe_float(frame.loc[idx, "close"])
                enriched[f"day{day}_return"] = close / entry_price - 1 if close is not None else None
            else:
                enriched[f"day{day}_return"] = None
        enriched.update(_first_thresholds(horizon, entry_price))
        if str(trade.get("setup_key")) == "volatility_contraction_breakout_v1":
            enriched.update(_breakout_diagnostics(frame, signal_idx, entry_idx, trade, tags))
        elif str(trade.get("setup_key")) == "pullback_reclaim_v1":
            enriched.update(_pullback_diagnostics(frame, signal_idx, entry_idx, trade, tags))
        industry = str(trade.get("industry") or "行业缺失")
        entry_sector = sector_stats.get((str(trade.get("entry_date")), industry), {})
        signal_sector = sector_stats.get((str(trade.get("signal_date")), industry), {})
        enriched["entry_industry_ret3_rank_pct"] = _safe_float(entry_sector.get("industry_ret3_rank_pct"))
        enriched["entry_industry_ret5_rank_pct"] = _safe_float(entry_sector.get("industry_ret5_rank_pct"))
        signal_rank = _safe_float(signal_sector.get("industry_ret5_rank_pct")) or _safe_float(trade.get("industry_ret5_rank_pct"))
        entry_rank = enriched["entry_industry_ret5_rank_pct"]
        enriched["sector_weakened_after_signal"] = bool(entry_rank is not None and signal_rank is not None and entry_rank < signal_rank - 0.20)
        enriched["profit_trade"] = (_safe_float(trade.get("net_return")) or 0) > 0
        enriched["setup_failure_had_float_profit"] = str(trade.get("exit_reason")) == "setup_failure_exit" and (enriched.get("mfe_until_exit") or 0) > 0
        rows.append(enriched)
    return pd.DataFrame(rows)


def _breakout_diagnostics(frame: pd.DataFrame, signal_idx: int, entry_idx: int, trade: dict[str, Any], tags: dict[str, Any]) -> dict[str, Any]:
    signal = frame.loc[signal_idx]
    entry = frame.loc[entry_idx]
    prior_start = max(0, signal_idx - 20)
    prior = frame.loc[prior_start:signal_idx - 1].copy()
    closes = pd.to_numeric(prior["close"], errors="coerce").dropna()
    highs = pd.to_numeric(prior["high"], errors="coerce").dropna()
    lows = pd.to_numeric(prior["low"], errors="coerce").dropna()
    entry_open = _safe_float(entry.get("open"))
    entry_close = _safe_float(entry.get("close"))
    prev_close = _safe_float(frame.loc[entry_idx - 1, "close"]) if entry_idx > 0 else None
    close_to_high = _safe_float(trade.get("close_to_20d_high"))
    platform_days_upper_half = None
    if len(highs) and len(lows) and len(closes):
        high20 = highs.max()
        low20 = lows.min()
        midpoint = (high20 + low20) / 2
        platform_days_upper_half = int((closes >= midpoint).sum())
    vol_ratio = (tags.get("vol10") / tags.get("vol20")) if tags.get("vol10") is not None and tags.get("vol20") else None
    turnover_ratio = tags.get("turnover_ratio")
    return {
        "breakout_signal_upper_shadow_long": (_safe_float(tags.get("upper_shadow_ratio")) or 0) >= 0.45,
        "entry_gap_and_fade": bool(entry_open is not None and entry_close is not None and prev_close is not None and entry_open > prev_close * 1.01 and entry_close < entry_open),
        "platform_days_upper_half": platform_days_upper_half,
        "platform_maybe_short": bool(platform_days_upper_half is not None and platform_days_upper_half < 6),
        "volatility_not_really_contracted": bool(vol_ratio is not None and vol_ratio > 0.85),
        "turnover_too_hot": bool(turnover_ratio is not None and turnover_ratio > 2.0),
        "distance_to_high_too_far": bool(close_to_high is not None and close_to_high < -0.025),
        "distance_to_high_too_close": bool(close_to_high is not None and close_to_high > -0.002),
        "false_breakout_proxy": bool(_safe_float(trade.get("net_return")) is not None and _safe_float(trade.get("net_return")) < 0 and _safe_float(tags.get("upper_shadow_ratio")) is not None and _safe_float(tags.get("upper_shadow_ratio")) >= 0.35),
        "signal_day_return": _ret(frame, signal_idx, 1),
        "entry_day_upper_shadow": _shadow_ratio(entry, "upper"),
    }


def _pullback_diagnostics(frame: pd.DataFrame, signal_idx: int, entry_idx: int, trade: dict[str, Any], tags: dict[str, Any]) -> dict[str, Any]:
    closes = pd.to_numeric(frame["close"], errors="coerce")
    lows = pd.to_numeric(frame["low"], errors="coerce")
    recent = frame.loc[max(0, signal_idx - 5):signal_idx].copy()
    ma20_values = [_ma(frame, idx, 20) for idx in range(max(0, signal_idx - 5), signal_idx + 1)]
    low_values = pd.to_numeric(recent["low"], errors="coerce").tolist()
    min_low_vs_ma20 = None
    ratios = []
    for low, ma20 in zip(low_values, ma20_values):
        if ma20 and pd.notna(low):
            ratios.append(low / ma20 - 1)
    if ratios:
        min_low_vs_ma20 = min(ratios)
    high10_idx = int(pd.to_numeric(frame.loc[max(0, signal_idx - 10):signal_idx, "high"], errors="coerce").idxmax())
    pullback_days = signal_idx - high10_idx
    entry_close = _safe_float(frame.loc[entry_idx, "close"])
    signal_close = _safe_float(frame.loc[signal_idx, "close"])
    entry_price = _safe_float(trade.get("entry_price_raw"))
    ma20 = _ma(frame, signal_idx, 20)
    ma20_distance = signal_close / ma20 - 1 if signal_close is not None and ma20 else None
    turnover_contraction = _safe_float(tags.get("turnover_contraction"))
    return {
        "pullback_days_from_10d_high": pullback_days,
        "pullback_too_short": bool(pullback_days <= 1),
        "pullback_too_long": bool(pullback_days >= 7),
        "trend_damage_last5_low_vs_ma20": min_low_vs_ma20,
        "trend_maybe_damaged": bool(min_low_vs_ma20 is not None and min_low_vs_ma20 < -0.01),
        "contraction_not_obvious": bool(turnover_contraction is not None and turnover_contraction > 0.85),
        "entry_next_day_no_followthrough": bool(entry_close is not None and signal_close is not None and entry_close <= signal_close),
        "ma20_distance_too_far": bool(ma20_distance is not None and ma20_distance > 0.12),
        "entry_chase_too_high": bool(entry_price is not None and signal_close is not None and entry_price / signal_close - 1 > 0.025),
        "ma20_distance_signal_diagnostic": ma20_distance,
        "signal_day_return": _ret(frame, signal_idx, 1),
    }


def _summarize_mfe_mae(mfe: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (setup, group_name), group in mfe.assign(result_group=lambda df: df["net_return"].apply(lambda x: "winner" if x > 0 else "loser")).groupby(["setup_key", "result_group"]):
        rows.append(
            {
                "setup_key": setup,
                "result_group": group_name,
                "trade_count": int(len(group)),
                "avg_mfe_5d": group["mfe_5d"].mean(),
                "median_mfe_5d": group["mfe_5d"].median(),
                "avg_mae_5d": group["mae_5d"].mean(),
                "median_mae_5d": group["mae_5d"].median(),
                "hit_up_2pct_rate": group["hit_up_2pct"].mean(),
                "hit_up_3pct_rate": group["hit_up_3pct"].mean(),
                "hit_down_2pct_rate": group["hit_down_2pct"].mean(),
                "hit_down_3pct_rate": group["hit_down_3pct"].mean(),
            }
        )
    return pd.DataFrame(rows)


def _yearly_diagnosis(mfe: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    yearly = pd.read_csv(output_dir / "setup_yearly_performance.csv", encoding="utf-8-sig")
    focus = yearly[yearly["strategy_key"].isin(FOCUS_SETUPS)].copy()
    mfe = mfe.copy()
    mfe["entry_year"] = pd.to_datetime(mfe["entry_date"], errors="coerce").dt.year
    rows: list[dict[str, Any]] = []
    for record in focus.to_dict("records"):
        setup = record["strategy_key"]
        year_label = str(record["year"])
        year_int = 2026 if year_label == "2026 YTD" else int(year_label)
        group = mfe[(mfe["setup_key"] == setup) & (mfe["entry_year"] == year_int)]
        rows.append(
            {
                "setup_key": setup,
                "year": year_label,
                "accepted_trade_count": int(record.get("accepted_trade_count") or 0),
                "total_return": record.get("return"),
                "max_drawdown": record.get("max_drawdown"),
                "win_rate": record.get("win_rate"),
                "average_trade_return": record.get("average_trade_return"),
                "median_trade_return": record.get("median_trade_return"),
                "setup_failure_exit_count": int((group["exit_reason"] == "setup_failure_exit").sum()) if not group.empty else 0,
                "limit_down_blocked_exit_count": int((group["exit_reason"] == "limit_down_blocked_exit").sum()) if not group.empty else 0,
                "environment_exit_count": int((group["exit_reason"] == "environment_exit").sum()) if not group.empty else 0,
                "avg_mfe_5d": group["mfe_5d"].mean() if not group.empty else None,
                "avg_mae_5d": group["mae_5d"].mean() if not group.empty else None,
            }
        )
    return pd.DataFrame(rows)


def _industry_diagnosis(mfe: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (setup, industry), group in mfe.groupby(["setup_key", "industry"], dropna=False):
        returns = pd.to_numeric(group["net_return"], errors="coerce").dropna()
        contribution = (
            pd.to_numeric(group.get("net_return"), errors="coerce").fillna(0)
            * pd.to_numeric(group.get("portfolio_position_pct"), errors="coerce").fillna(0)
        ).sum()
        rows.append(
            {
                "setup_key": setup,
                "industry": industry,
                "trade_count": int(len(group)),
                "win_rate": float((returns > 0).mean()) if len(returns) else None,
                "average_trade_return": float(returns.mean()) if len(returns) else None,
                "median_trade_return": float(returns.median()) if len(returns) else None,
                "cumulative_contribution": float(contribution),
                "setup_failure_exit_count": int((group["exit_reason"] == "setup_failure_exit").sum()),
                "limit_down_blocked_exit_count": int((group["exit_reason"] == "limit_down_blocked_exit").sum()),
                "take_profit_count": int((group["exit_reason"] == "take_profit").sum()),
                "environment_exit_count": int((group["exit_reason"] == "environment_exit").sum()),
            }
        )
    return pd.DataFrame(rows).sort_values(["setup_key", "cumulative_contribution"], ascending=[True, False])


def _failure_samples(mfe: pd.DataFrame) -> pd.DataFrame:
    failure = mfe[pd.to_numeric(mfe["net_return"], errors="coerce") < 0].copy()
    keep_cols = [
        "setup_key",
        "symbol",
        "name",
        "industry",
        "signal_date",
        "entry_date",
        "exit_date",
        "market_state",
        "entry_price_raw",
        "exit_price_raw",
        "net_return",
        "mfe_5d",
        "mae_5d",
        "mfe_until_exit",
        "mae_until_exit",
        "exit_reason",
        "signal_upper_shadow_ratio",
        "entry_gap_pct",
        "entry_fade_pct",
        "entry_chase_pct",
        "close_to_20d_high",
        "turnover_ratio_tag",
        "turnover_contraction",
        "vol10_to_vol20_tag",
        "platform_range20_tag",
        "platform_maybe_short",
        "volatility_not_really_contracted",
        "turnover_too_hot",
        "false_breakout_proxy",
        "pullback_days_from_10d_high",
        "trend_maybe_damaged",
        "contraction_not_obvious",
        "entry_next_day_no_followthrough",
        "sector_weakened_after_signal",
        "setup_tags_json",
    ]
    return failure[[col for col in keep_cols if col in failure.columns]].sort_values(["setup_key", "net_return"])


def _manual_review_watchlist(mfe: pd.DataFrame, histories: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for setup in FOCUS_SETUPS:
        group = mfe[mfe["setup_key"] == setup].copy()
        buckets = {
            "best_20": group.sort_values("net_return", ascending=False).head(20),
            "worst_20": group.sort_values("net_return").head(20),
            "typical_setup_failure_20": group[group["exit_reason"] == "setup_failure_exit"].sort_values(["mfe_until_exit", "net_return"], ascending=[False, True]).head(20),
            "typical_take_profit_20": group[group["exit_reason"] == "take_profit"].sort_values(["mae_until_exit", "mfe_until_exit"], ascending=[False, False]).head(20),
            "boundary_ambiguous_20": group[(group["mfe_5d"] >= 0.02) & (group["net_return"] <= 0)].sort_values(["mfe_5d", "mae_5d"], ascending=[False, True]).head(20),
        }
        for bucket, frame in buckets.items():
            for rec in frame.to_dict("records"):
                symbol = str(rec.get("symbol"))
                bars = histories.get(symbol)
                window = ""
                if bars is not None:
                    signal_idx = _date_index(bars, str(rec.get("signal_date")))
                    exit_idx = _date_index(bars, str(rec.get("exit_date")))
                    if signal_idx is not None:
                        start = max(0, signal_idx - 10)
                        end = min(len(bars) - 1, (exit_idx if exit_idx is not None else signal_idx) + 5)
                        window = f"{bars.loc[start, 'date']}~{bars.loc[end, 'date']}"
                rows.append(
                    {
                        "review_bucket": bucket,
                        "symbol": rec.get("symbol"),
                        "name": rec.get("name"),
                        "signal_date": rec.get("signal_date"),
                        "entry_date": rec.get("entry_date"),
                        "exit_date": rec.get("exit_date"),
                        "setup_key": rec.get("setup_key"),
                        "industry": rec.get("industry"),
                        "market_state": rec.get("market_state"),
                        "sector_strength": rec.get("industry_ret5_rank_pct"),
                        "entry_price_raw": rec.get("entry_price_raw"),
                        "exit_price_raw": rec.get("exit_price_raw"),
                        "net_return": rec.get("net_return"),
                        "mfe_5d": rec.get("mfe_5d"),
                        "mae_5d": rec.get("mae_5d"),
                        "exit_reason": rec.get("exit_reason"),
                        "key_shape_tags": rec.get("setup_tags_json"),
                        "kline_review_window": window,
                    }
                )
    return pd.DataFrame(rows)


def _distribution_text(frame: pd.DataFrame, group_col: str, value_col: str, top_n: int = 10) -> list[str]:
    if frame.empty or group_col not in frame.columns:
        return ["- No data."]
    rows = frame.groupby(group_col)[value_col].count().sort_values(ascending=False).head(top_n)
    return [f"- {idx}: {int(value)}" for idx, value in rows.items()]


def _bool_rate(series: pd.Series) -> float:
    if series.empty:
        return float("nan")
    return series.map(lambda value: bool(value) if pd.notna(value) else False).mean()


def _render_mfe_mae_report(output_dir: Path, mfe: pd.DataFrame, summary: pd.DataFrame) -> None:
    lines = [
        "# MFE / MAE Diagnosis",
        "",
        "本报告只分析 `volatility_contraction_breakout_v1` 与 `pullback_reclaim_v1` 的已接受交易，不修改任何规则。",
        "",
        "## Summary By Result Group",
        "",
    ]
    lines.extend(
        _markdown_table(
            summary,
            [
                ("setup", "setup_key"),
                ("分组", "result_group"),
                ("笔数", "trade_count"),
                ("平均MFE", "avg_mfe_5d"),
                ("中位MFE", "median_mfe_5d"),
                ("平均MAE", "avg_mae_5d"),
                ("中位MAE", "median_mae_5d"),
                ("触及+2%", "hit_up_2pct_rate"),
                ("触及+3%", "hit_up_3pct_rate"),
                ("触及-2%", "hit_down_2pct_rate"),
                ("触及-3%", "hit_down_3pct_rate"),
            ],
        )
    )
    lines.extend(["", "## Threshold Path", ""])
    for setup in FOCUS_SETUPS:
        group = mfe[mfe["setup_key"] == setup]
        lines.append(f"### {setup}")
        if group.empty:
            lines.append("- No trades.")
            continue
        setup_failure = group[group["exit_reason"] == "setup_failure_exit"]
        take_profit = group[group["exit_reason"] == "take_profit"]
        float_profit_before_failure = float((setup_failure["mfe_until_exit"] > 0).mean()) if len(setup_failure) else None
        hit_2_before_failure = float((setup_failure["mfe_until_exit"] >= 0.02).mean()) if len(setup_failure) else None
        take_profit_hit_2_early = float((take_profit["first_up_2pct_day"].fillna(99) < take_profit["holding_days"]).mean()) if len(take_profit) else None
        take_profit_hit_3_early = float((take_profit["first_up_3pct_day"].fillna(99) < take_profit["holding_days"]).mean()) if len(take_profit) else None
        lines.append(f"- setup failure 交易失败前曾出现浮盈比例：{_fmt_pct(float_profit_before_failure)}。")
        lines.append(f"- setup failure 交易失败前曾触及 +2% 比例：{_fmt_pct(hit_2_before_failure)}。")
        lines.append(f"- take_profit 交易更早触及 +2% 比例：{_fmt_pct(take_profit_hit_2_early)}。")
        lines.append(f"- take_profit 交易更早触及 +3% 比例：{_fmt_pct(take_profit_hit_3_early)}。")
        rr = abs((group["first_take_profit"] / group["entry_price_raw"] - 1).median() / (group["stop_loss"] / group["entry_price_raw"] - 1).median()) if "first_take_profit" in group else None
        lines.append(f"- 当前固定止盈/失败点的中位盈亏比约：{_fmt_num(rr)}。")
        lines.append("")
    lines.extend(
        [
            "## Interpretation",
            "",
            "- 如果 setup failure 前大量出现过浮盈，只能作为 shadow exit experiment 候选，不能直接改主策略。",
            "- 如果 MAE 很快发生，说明入场路径需要分钟数据验证，日线无法确认先后顺序。",
            "- 当前固定止盈约 3.94%，setup failure 亏损常接近 -5.29%，盈亏比本身偏吃紧。",
        ]
    )
    (output_dir / "mfe_mae_diagnosis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_failure_profile(output_dir: Path, mfe: pd.DataFrame, failure_samples: pd.DataFrame) -> None:
    lines = ["# Failure Case Profile", "", "本报告只画像失败原因，不修改阈值、不并入主策略。", ""]
    for setup in FOCUS_SETUPS:
        group = mfe[(mfe["setup_key"] == setup) & (mfe["net_return"] < 0)].copy()
        lines.append(f"## {setup}")
        if group.empty:
            lines.append("- No losing trades.")
            continue
        lines.append(f"- 亏损交易数：{len(group)}，平均收益 {_fmt_pct(group['net_return'].mean())}，中位数 {_fmt_pct(group['net_return'].median())}。")
        lines.append(f"- 平均 MFE：{_fmt_pct(group['mfe_5d'].mean())}，平均 MAE：{_fmt_pct(group['mae_5d'].mean())}。")
        if setup == "volatility_contraction_breakout_v1":
            checks = [
                ("突破日上影线过长", "breakout_signal_upper_shadow_long"),
                ("次日高开低走", "entry_gap_and_fade"),
                ("平台可能偏短", "platform_maybe_short"),
                ("波动未真正收缩", "volatility_not_really_contracted"),
                ("成交额放量过猛", "turnover_too_hot"),
                ("行业转弱", "sector_weakened_after_signal"),
                ("距离20日高点过远", "distance_to_high_too_far"),
                ("距离20日高点过近", "distance_to_high_too_close"),
                ("假突破代理", "false_breakout_proxy"),
            ]
        else:
            checks = [
                ("趋势可能已破坏", "trend_maybe_damaged"),
                ("回踩过短", "pullback_too_short"),
                ("回踩过长", "pullback_too_long"),
                ("缩量不明显", "contraction_not_obvious"),
                ("次日无法延续", "entry_next_day_no_followthrough"),
                ("距离MA20过远", "ma20_distance_too_far"),
                ("行业转弱", "sector_weakened_after_signal"),
                ("入场偏追高", "entry_chase_too_high"),
            ]
        for label, col in checks:
            if col in group.columns:
                lines.append(f"- {label}：{_fmt_pct(_bool_rate(group[col]))}。")
        lines.append("")
    lines.extend(["## Worst Samples", ""])
    lines.extend(
        _markdown_table(
            failure_samples.head(40),
            [
                ("setup", "setup_key"),
                ("标的", "symbol"),
                ("名称", "name"),
                ("行业", "industry"),
                ("信号", "signal_date"),
                ("买入", "entry_date"),
                ("退出", "exit_date"),
                ("收益", "net_return"),
                ("MFE", "mfe_5d"),
                ("MAE", "mae_5d"),
                ("退出", "exit_reason"),
            ],
        )
    )
    (output_dir / "failure_case_profile.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_winning_profile(output_dir: Path, mfe: pd.DataFrame) -> None:
    lines = ["# Winning Case Profile", "", "本报告画像盈利样本的共同特征，只用于人工复盘。", ""]
    for setup in FOCUS_SETUPS:
        group = mfe[(mfe["setup_key"] == setup) & (mfe["net_return"] > 0)].copy()
        lines.append(f"## {setup}")
        if group.empty:
            lines.append("- No winning trades.")
            continue
        lines.append(f"- 盈利交易数：{len(group)}，平均收益 {_fmt_pct(group['net_return'].mean())}，中位数 {_fmt_pct(group['net_return'].median())}。")
        lines.append(f"- 平均 MFE：{_fmt_pct(group['mfe_5d'].mean())}，平均 MAE：{_fmt_pct(group['mae_5d'].mean())}。")
        lines.append(f"- 买入前 5 日平均表现：{_fmt_pct(group['pre_5d_return'].mean())}；10 日：{_fmt_pct(group['pre_10d_return'].mean())}；20 日：{_fmt_pct(group['pre_20d_return'].mean())}。")
        lines.append(f"- 市场状态分布：{dict(Counter(group['market_state']).most_common())}。")
        lines.append("- 成功样本行业 Top 10：")
        lines.extend(_distribution_text(group, "industry", "net_return"))
        if setup == "volatility_contraction_breakout_v1":
            lines.append(f"- 突破成功样本的中位成交额放大倍数：{_fmt_num(group['turnover_ratio_tag'].median())}。")
            lines.append(f"- 突破成功样本的中位 vol10/vol20：{_fmt_num(group['vol10_to_vol20_tag'].median())}。")
        else:
            lines.append(f"- 回踩成功样本的中位缩量比例：{_fmt_pct(group['turnover_contraction'].median())}。")
            lines.append(f"- 回踩成功样本的中位 MA20 距离：{_fmt_pct(group['ma20_distance_signal'].median())}。")
        lines.append("")
    lines.extend(
        [
            "## Interpretation",
            "",
            "- 盈利样本多来自 active 市场与板块共振，但这种结构目前只改善风险分布，没有把长样本收益转正。",
            "- 后续人工复盘应优先看盈利样本是否具有稳定、肉眼可识别的 K 线结构，而不是直接调阈值。",
        ]
    )
    (output_dir / "winning_case_profile.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_final_report(
    output_dir: Path,
    mfe: pd.DataFrame,
    mfe_summary: pd.DataFrame,
    yearly: pd.DataFrame,
    industry: pd.DataFrame,
    watchlist: pd.DataFrame,
) -> None:
    lines = [
        "# Focused Setup Diagnosis Report",
        "",
        "本报告基于 `setup_full_trade_performance_report.md` 继续诊断，只研究 `volatility_contraction_breakout_v1` 与 `pullback_reclaim_v1`。不新增策略，不调参，不并入主策略。",
        "",
        "## A. 已确认事实",
        "",
        "- 所有 setup-based 版本长样本累计收益仍为负，不能并入主策略。",
        "- `volatility_contraction_breakout_v1` 当前累计收益最好，跌停无法卖出为 0。",
        "- `pullback_reclaim_v1` 当前最大回撤和中位数更好。",
        "- `panic_repair_v1` 和 `setup_combined_v1` 本轮暂停优化，只保留诊断结论。",
        "",
        "## B. volatility contraction breakout 是否值得继续",
        "",
    ]
    vcb = mfe[mfe["setup_key"] == "volatility_contraction_breakout_v1"]
    pr = mfe[mfe["setup_key"] == "pullback_reclaim_v1"]
    lines.extend(_setup_brief(vcb))
    lines.extend(
        [
            "",
            "## C. pullback reclaim 是否值得继续",
            "",
        ]
    )
    lines.extend(_setup_brief(pr))
    lines.extend(
        [
            "",
            "## D. panic repair 为什么暂停",
            "",
            "- 前一轮已确认 panic repair 接受交易最多、setup failure 多、累计收益和回撤最差。",
            "- 这类 setup 容易把“仍在下跌过程中的反抽”误判为修复确认。",
            "- 当前应先做失败样本人工复盘，而不是调阈值继续优化。",
            "",
            "## E. combined setup 为什么不继续",
            "",
            "- combined 没有明显优于 V3，并且可能被 panic repair 和弱 setup 拖累。",
            "- 在单 setup 仍未验证正期望之前，组合只会混合噪音，降低诊断清晰度。",
            "",
            "## F. 当前退出规则是否合理",
            "",
        ]
    )
    tp = mfe["first_take_profit"] / mfe["entry_price_raw"] - 1
    stop = mfe["stop_loss"] / mfe["entry_price_raw"] - 1
    rr = abs(tp.median() / stop.median()) if stop.median() else None
    lines.append(f"- 当前止盈中位约 {_fmt_pct(tp.median())}，失败/止损中位约 {_fmt_pct(stop.median())}，中位盈亏比约 {_fmt_num(rr)}。")
    lines.append("- 这个盈亏比偏吃紧，要求胜率和入场质量更高；但这只能形成 shadow exit experiment 候选，不能直接修改主策略。")
    lines.extend(
        [
            "",
            "## G. MFE/MAE 给出的启示",
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            mfe_summary,
            [
                ("setup", "setup_key"),
                ("分组", "result_group"),
                ("笔数", "trade_count"),
                ("平均MFE", "avg_mfe_5d"),
                ("中位MFE", "median_mfe_5d"),
                ("平均MAE", "avg_mae_5d"),
                ("中位MAE", "median_mae_5d"),
                ("触及+2%", "hit_up_2pct_rate"),
                ("触及-2%", "hit_down_2pct_rate"),
            ],
        )
    )
    fail_float = mfe[(mfe["exit_reason"] == "setup_failure_exit") & (mfe["mfe_until_exit"] > 0)]
    lines.append("")
    lines.append(f"- setup failure 前曾经出现浮盈的交易：{len(fail_float)} 笔，占 setup failure 的 {_fmt_pct(len(fail_float) / max(1, int((mfe['exit_reason'] == 'setup_failure_exit').sum())))}。")
    lines.append("- 如果 MFE 显示很多交易先浮盈后失败，下一步只能设计 shadow exit experiment，不能直接改主策略。")
    lines.append("- 如果 MAE 发生很快，应优先验证入场路径和盘中先后顺序，这需要分钟数据。")
    lines.extend(
        [
            "",
            "## H. 需要人工复盘的样本",
            "",
            f"- 已输出 `manual_review_watchlist.csv`，共 {len(watchlist)} 条复盘样本。",
            "- 每个 setup 包含最好 20、最差 20、典型 setup failure 20、典型 take_profit 20、边界误判样本 20。",
            "",
            "## I. 是否需要分钟数据",
            "",
            "- 需要，但目的不是继续堆因子，而是验证触发区间、突破后回落、回踩后延续、同日止盈止损先后顺序。",
            "- 当前日线只能做保守代理，不能判断盘中真实成交路径。",
            "",
            "## J. 下一步建议",
            "",
            "- 当前 setup-based 方向改善了风险结构，但尚未验证收益端优势。",
            "- 暂停 panic repair 与 combined setup 优化。",
            "- 对 `volatility_contraction_breakout_v1` 和 `pullback_reclaim_v1` 先做人工 K 线复盘，验证成功/失败样本是否有稳定可解释结构。",
            "- 仅在人工复盘确认“先浮盈后失败”大量存在时，设计 shadow exit experiment；仍不得并入主策略。",
        ]
    )
    lines.extend(["", "## Yearly Snapshot", ""])
    lines.extend(
        _markdown_table(
            yearly,
            [
                ("setup", "setup_key"),
                ("年份", "year"),
                ("交易", "accepted_trade_count"),
                ("收益", "total_return"),
                ("最大回撤", "max_drawdown"),
                ("胜率", "win_rate"),
                ("平均", "average_trade_return"),
                ("中位", "median_trade_return"),
                ("setup失败", "setup_failure_exit_count"),
                ("跌停无法卖出", "limit_down_blocked_exit_count"),
                ("环境退出", "environment_exit_count"),
            ],
            max_rows=20,
        )
    )
    lines.extend(["", "## Industry Top / Bottom", ""])
    for setup in FOCUS_SETUPS:
        group = industry[industry["setup_key"] == setup].copy()
        top = group.sort_values("cumulative_contribution", ascending=False).head(5)
        bottom = group.sort_values("cumulative_contribution").head(5)
        lines.append(f"### {setup} Top")
        lines.extend(_markdown_table(top, [("行业", "industry"), ("交易", "trade_count"), ("胜率", "win_rate"), ("平均", "average_trade_return"), ("中位", "median_trade_return"), ("贡献", "cumulative_contribution"), ("setup失败", "setup_failure_exit_count"), ("跌停", "limit_down_blocked_exit_count")]))
        lines.append(f"### {setup} Bottom")
        lines.extend(_markdown_table(bottom, [("行业", "industry"), ("交易", "trade_count"), ("胜率", "win_rate"), ("平均", "average_trade_return"), ("中位", "median_trade_return"), ("贡献", "cumulative_contribution"), ("setup失败", "setup_failure_exit_count"), ("跌停", "limit_down_blocked_exit_count")]))
        lines.append("")
    (output_dir / "focused_setup_diagnosis_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setup_brief(group: pd.DataFrame) -> list[str]:
    if group.empty:
        return ["- No data."]
    winners = group[group["net_return"] > 0]
    losers = group[group["net_return"] <= 0]
    setup = str(group["setup_key"].iloc[0])
    lines = [
        f"- 交易数：{len(group)}；胜率：{_fmt_pct((group['net_return'] > 0).mean())}；平均收益：{_fmt_pct(group['net_return'].mean())}；中位数：{_fmt_pct(group['net_return'].median())}。",
        f"- 平均 MFE：{_fmt_pct(group['mfe_5d'].mean())}；平均 MAE：{_fmt_pct(group['mae_5d'].mean())}。",
        f"- 盈利交易平均 MFE：{_fmt_pct(winners['mfe_5d'].mean()) if len(winners) else '-'}；亏损交易平均 MFE：{_fmt_pct(losers['mfe_5d'].mean()) if len(losers) else '-'}。",
    ]
    if setup == "volatility_contraction_breakout_v1":
        false_break = _bool_rate(group[group["net_return"] < 0]["false_breakout_proxy"])
        lines.append(f"- 亏损样本中的假突破代理比例：{_fmt_pct(false_break)}。")
        lines.append("- 继续观察价值：有，主要因为跌停无法卖出为 0、累计收益最接近零；但仍未验证正期望。")
    else:
        no_follow = _bool_rate(group[group["net_return"] < 0]["entry_next_day_no_followthrough"])
        lines.append(f"- 亏损样本中次日无法延续比例：{_fmt_pct(no_follow)}。")
        lines.append("- 继续观察价值：有，主要因为回撤和中位数较好；但仍未验证正期望。")
    return lines


def run(output_dir: Path = DEFAULT_OUTPUT_DIR, kline_dir: Path = DEFAULT_KLINE_DIR) -> dict[str, Any]:
    trades = _load_focus_trades(output_dir)
    histories = _load_bars(kline_dir, trades["symbol"].astype(str).tolist())
    sector_stats = _sector_lookup(output_dir)
    mfe = _enrich_mfe_mae(trades, histories, sector_stats)
    mfe_summary = _summarize_mfe_mae(mfe)
    failure_samples = _failure_samples(mfe)
    yearly = _yearly_diagnosis(mfe, output_dir)
    industry = _industry_diagnosis(mfe)
    watchlist = _manual_review_watchlist(mfe, histories)

    _write_csv(output_dir / "mfe_mae_trades.csv", mfe)
    _write_csv(output_dir / "failure_case_samples.csv", failure_samples)
    _write_csv(output_dir / "focused_setup_yearly_diagnosis.csv", yearly)
    _write_csv(output_dir / "focused_setup_industry_diagnosis.csv", industry)
    _write_csv(output_dir / "manual_review_watchlist.csv", watchlist)

    _render_mfe_mae_report(output_dir, mfe, mfe_summary)
    _render_failure_profile(output_dir, mfe, failure_samples)
    _render_winning_profile(output_dir, mfe)
    _render_final_report(output_dir, mfe, mfe_summary, yearly, industry, watchlist)
    return {
        "output_dir": str(output_dir),
        "focused_trade_count": int(len(mfe)),
        "manual_review_count": int(len(watchlist)),
        "mfe_mae_trades": str(output_dir / "mfe_mae_trades.csv"),
        "report": str(output_dir / "focused_setup_diagnosis_report.md"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Focused setup diagnostics without modifying strategy.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--kline-dir", default=str(DEFAULT_KLINE_DIR))
    args = parser.parse_args()
    payload = run(Path(args.output), Path(args.kline_dir))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
