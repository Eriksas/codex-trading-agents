#!/usr/bin/env python3
"""Shadow exit experiments for focused setup-based short swing strategies.

The script replays exits for already accepted setup trades. It does not change
entry signals, setup definitions, active strategy configuration, ledgers, or
live trading state.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import factor_research_round3 as r3
import factor_research_round4 as r4

SETUP_OUTPUT_DIR = ROOT / "output" / "setup_based_short_swing"
DEFAULT_OUTPUT_DIR = ROOT / "output" / "setup_based_short_swing_exit_experiment"
DEFAULT_KLINE_DIR = ROOT / "data" / "expanded" / "daily_kline"
FOCUS_SETUPS = ["volatility_contraction_breakout_v1", "pullback_reclaim_v1"]
INITIAL_CASH = 1_000_000.0
FEE_BPS = 5.0
SLIPPAGE_BPS = 10.0
LIMIT_THRESHOLD = r3.DEFAULT_LIMIT_THRESHOLD


@dataclass(frozen=True)
class ExitVariant:
    key: str
    label: str
    kind: str
    threshold: Optional[float] = None


EXIT_VARIANTS = [
    ExitVariant("baseline", "A. 原始退出规则", "baseline"),
    ExitVariant("profit_protect_2pct", "B. 触及 +2% 后保本保护", "profit_protect", 0.02),
    ExitVariant("profit_protect_3pct", "C. 触及 +3% 后锁定 +1%", "profit_protect", 0.03),
    ExitVariant("fast_fail_exit_day1", "D. 第 1 天无浮盈且收盘弱，次日退出", "fast_fail_day1"),
    ExitVariant("fast_fail_exit_day2", "E. 2 天未达到 +1.5%，第 3 天退出", "fast_fail_day2"),
    ExitVariant("partial_take_profit_3pct", "F. 触及 +3% 半仓止盈", "partial_take_profit", 0.03),
    ExitVariant("tight_setup_failure_2_5pct", "G1. setup failure 收紧至 -2.5%", "tight_setup_failure", 0.025),
    ExitVariant("tight_setup_failure_3_0pct", "G2. setup failure 收紧至 -3.0%", "tight_setup_failure", 0.030),
    ExitVariant("tight_setup_failure_3_5pct", "G3. setup failure 收紧至 -3.5%", "tight_setup_failure", 0.035),
    ExitVariant("no_loss_after_2pct_mfe", "H. 触及 +2% 后最终不低于 -0.5%", "no_loss_after_mfe", 0.02),
]


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
        "total_return",
        "annualized_return",
        "max_drawdown",
        "win_rate",
        "average_trade_return",
        "median_trade_return",
        "positive_year_rate",
        "positive_month_rate",
        "return",
        "monthly_return",
        "worst_year_return",
        "worst_month_return",
        "average_return",
        "median_return",
        "max_loss",
        "baseline_total_return_delta",
        "baseline_max_drawdown_delta",
        "protect_activation_rate",
        "breakeven_exit_rate",
        "partial_activation_rate",
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


def _symbol_path(kline_dir: Path, symbol: str) -> Path:
    return kline_dir / f"{symbol.replace('.', '_')}.csv"


def _load_trades(setup_dir: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for setup in FOCUS_SETUPS:
        path = setup_dir / f"{setup}_accepted_trades.csv"
        if not path.exists():
            raise FileNotFoundError(path)
        frame = pd.read_csv(path, encoding="utf-8-sig")
        frame["setup_key"] = setup
        frames.append(frame)
    trades = pd.concat(frames, ignore_index=True)
    for col in [
        "entry_price_raw",
        "entry_price",
        "exit_price_raw",
        "exit_price",
        "stop_loss",
        "hard_stop",
        "first_take_profit",
        "second_take_profit",
        "holding_days",
        "position_pct",
        "portfolio_position_pct",
        "position_value",
        "net_return",
        "rank_score",
        "setup_score",
    ]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    return trades


def _load_histories(kline_dir: Path, symbols: list[str]) -> tuple[dict[str, pd.DataFrame], dict[str, list[dict]]]:
    frames: dict[str, pd.DataFrame] = {}
    lists: dict[str, list[dict]] = {}
    for symbol in sorted(set(symbols)):
        path = _symbol_path(kline_dir, symbol)
        if not path.exists():
            continue
        frame = pd.read_csv(path, encoding="utf-8-sig").sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frames[symbol] = frame
        lists[symbol] = frame[["date", "open", "high", "low", "close", "volume", "turnover"]].to_dict("records")
    return frames, lists


def _date_idx(frame: pd.DataFrame, date: str) -> Optional[int]:
    matches = frame.index[frame["date"].astype(str) == str(date)].tolist()
    return int(matches[0]) if matches else None


def _ma5(frame: pd.DataFrame, idx: int) -> Optional[float]:
    if idx - 4 < 0:
        return None
    values = pd.to_numeric(frame.loc[idx - 4 : idx, "close"], errors="coerce").dropna()
    if len(values) < 5:
        return None
    return float(values.mean())


def _can_sell(row: pd.Series, prev_close: Optional[float]) -> bool:
    bar = {
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": row.get("close"),
        "volume": row.get("volume"),
        "turnover": row.get("turnover"),
    }
    return r3._can_sell(bar, prev_close, LIMIT_THRESHOLD)


def _net_from_raw(entry_raw: float, exit_raw: float, fee_bps: float = FEE_BPS, slippage_bps: float = SLIPPAGE_BPS) -> tuple[float, float, float]:
    entry_price = entry_raw * (1 + slippage_bps / 10000)
    exit_price = exit_raw * (1 - slippage_bps / 10000)
    gross = exit_price / entry_price - 1
    net = gross - fee_bps * 2 / 10000
    return gross, net, exit_price


def _weighted_partial_net(entry_raw: float, partial_raw: float, final_raw: float) -> tuple[float, float, float]:
    gross_half, net_half, _partial_exit = _net_from_raw(entry_raw, partial_raw)
    gross_rest, net_rest, final_exit = _net_from_raw(entry_raw, final_raw)
    gross = 0.5 * gross_half + 0.5 * gross_rest
    net = 0.5 * net_half + 0.5 * net_rest
    weighted_raw = entry_raw * (1 + net + FEE_BPS * 2 / 10000)
    return gross, net, final_exit if final_raw >= partial_raw else weighted_raw


def _baseline_trade(trade: dict[str, Any], variant: ExitVariant) -> dict[str, Any]:
    result = dict(trade)
    result.update(
        {
            "exit_variant": variant.key,
            "exit_variant_label": variant.label,
            "protect_activated": False,
            "partial_activated": bool(trade.get("partial_take_profit")),
            "breakeven_exit": False,
            "path_dependency_flag": "baseline_original_daily_path",
            "exit_rule_note": "baseline copied from original accepted trade",
        }
    )
    return result


def _simulate_trade(trade: dict[str, Any], frame: pd.DataFrame, variant: ExitVariant) -> Optional[dict[str, Any]]:
    if variant.kind == "baseline":
        return _baseline_trade(trade, variant)
    entry_idx = _date_idx(frame, str(trade.get("entry_date")))
    if entry_idx is None:
        return None
    entry_raw = _safe_float(trade.get("entry_price_raw"))
    if entry_raw is None or entry_raw <= 0:
        return None
    original_exit_idx = _date_idx(frame, str(trade.get("exit_date")))
    max_end_idx = min(entry_idx + 4, len(frame) - 1)
    if original_exit_idx is not None:
        max_end_idx = max(max_end_idx, original_exit_idx)
    original_stop = _safe_float(trade.get("stop_loss")) or entry_raw * 0.95
    first_tp = _safe_float(trade.get("first_take_profit")) or entry_raw * 1.035
    second_tp = _safe_float(trade.get("second_take_profit")) or entry_raw * 1.05
    if variant.kind == "tight_setup_failure" and variant.threshold is not None:
        stop_loss = max(original_stop, entry_raw * (1 - variant.threshold))
    else:
        stop_loss = original_stop

    max_gain = -1.0
    protection_active = False
    partial_active = False
    partial_raw = None
    protected_floor_raw: Optional[float] = None
    scheduled_exit_idx: Optional[int] = None
    scheduled_exit_reason = ""
    blocked_exit_days = 0
    blocked_original_reason = ""
    path_flags: set[str] = set()

    def desired_exit(reason: str, raw_price: float) -> tuple[str, float]:
        if partial_active and partial_raw is not None:
            _gross, net, _exit_price = _weighted_partial_net(entry_raw, partial_raw, raw_price)
            weighted_raw = entry_raw * (1 + net + FEE_BPS * 2 / 10000)
            return reason, weighted_raw
        return reason, raw_price

    for idx in range(entry_idx, max_end_idx + 1):
        row = frame.loc[idx]
        prev_close = _safe_float(frame.loc[idx - 1, "close"]) if idx > 0 else None
        high = _safe_float(row.get("high")) or entry_raw
        low = _safe_float(row.get("low")) or entry_raw
        close = _safe_float(row.get("close")) or entry_raw
        open_price = _safe_float(row.get("open")) or entry_raw
        holding_day = idx - entry_idx + 1
        max_gain = max(max_gain, high / entry_raw - 1)
        can_sell = _can_sell(row, prev_close)

        original_hit_stop = low <= stop_loss
        original_hit_first_tp = high >= first_tp
        original_hit_second_tp = high >= second_tp
        desired: Optional[tuple[str, float]] = None

        if scheduled_exit_idx is not None and idx >= scheduled_exit_idx:
            desired = desired_exit(scheduled_exit_reason, open_price)

        # Existing or tightened failure has priority over same-day newly touched
        # favorable thresholds. This is intentionally conservative for daily bars.
        if desired is None and original_hit_stop:
            if original_hit_first_tp or original_hit_second_tp:
                path_flags.add("same_day_profit_and_failure_unfavorable_order")
                desired = desired_exit("same_day_stop_take_conservative", stop_loss)
            else:
                reason = "tight_setup_failure_exit" if variant.kind == "tight_setup_failure" else "setup_failure_exit"
                desired = desired_exit(reason, stop_loss)

        if desired is None and protection_active and protected_floor_raw is not None and low <= protected_floor_raw:
            if high >= protected_floor_raw:
                path_flags.add("daily_floor_touch_order_unknown")
            reason = "profit_protect_exit" if protected_floor_raw > entry_raw else "breakeven_exit"
            desired = desired_exit(reason, protected_floor_raw)

        if desired is None:
            if variant.key == "profit_protect_2pct" and high >= entry_raw * 1.02:
                protection_active = True
                protected_floor_raw = entry_raw
                path_flags.add("profit_protect_2pct_uses_daily_high")
            elif variant.key == "profit_protect_3pct" and high >= entry_raw * 1.03:
                protection_active = True
                protected_floor_raw = entry_raw * 1.01
                path_flags.add("profit_protect_3pct_uses_daily_high")
            elif variant.key == "no_loss_after_2pct_mfe" and high >= entry_raw * 1.02:
                protection_active = True
                protected_floor_raw = entry_raw * 0.995
                path_flags.add("no_loss_after_2pct_uses_daily_high")
            elif variant.key == "partial_take_profit_3pct" and (not partial_active) and high >= entry_raw * 1.03:
                partial_active = True
                partial_raw = entry_raw * 1.03
                protection_active = True
                protected_floor_raw = entry_raw
                path_flags.add("partial_take_profit_uses_daily_high")

        if desired is None and variant.kind == "fast_fail_day1" and holding_day == 1:
            if high <= entry_raw * 1.001 and close <= entry_raw:
                scheduled_exit_idx = min(idx + 1, len(frame) - 1)
                scheduled_exit_reason = "fast_fail_exit"
        if desired is None and variant.kind == "fast_fail_day2" and holding_day == 2:
            if max_gain < 0.015:
                scheduled_exit_idx = min(idx + 1, len(frame) - 1)
                scheduled_exit_reason = "fast_fail_exit"

        if desired is None and original_hit_second_tp:
            if partial_active and partial_raw is not None:
                desired = desired_exit("take_profit", second_tp)
            else:
                # Keep original full exit structure. First/second TP in the
                # source simulation produced a weighted take-profit result.
                desired = desired_exit("take_profit", (first_tp + second_tp) / 2)
        elif desired is None and original_hit_first_tp and variant.kind not in {"partial_take_profit"}:
            # Baseline setup code only exits fully at second TP, but first TP
            # still activates partial accounting in original trades. For
            # non-partial variants we do not create a new optimistic exit.
            pass

        if desired is None:
            market_state = str(trade.get("market_state") or "")
            if holding_day > 1 and market_state in {"neutral", "defensive"}:
                desired = desired_exit("environment_exit", close)
            elif holding_day >= 3 and max_gain < 0.015:
                ma5 = _ma5(frame, idx)
                if ma5 is not None and close < ma5:
                    desired = desired_exit("time_stop", close)
            elif holding_day >= 5 or idx >= max_end_idx:
                desired = desired_exit("timeout", close)

        if desired is None:
            continue
        if not can_sell:
            blocked_exit_days += 1
            blocked_original_reason = desired[0]
            path_flags.add("limit_down_blocked_exit_path")
            if idx < len(frame) - 1:
                continue
        exit_reason, exit_raw = desired
        if blocked_exit_days > 0:
            exit_reason = "limit_down_blocked_exit"
        gross, net, exit_price = _net_from_raw(entry_raw, exit_raw)
        if partial_active and partial_raw is not None:
            gross, net, exit_price = _weighted_partial_net(entry_raw, partial_raw, exit_raw)
        result = dict(trade)
        result.update(
            {
                "exit_variant": variant.key,
                "exit_variant_label": variant.label,
                "exit_date": str(row.get("date")),
                "exit_price_raw": round(float(exit_raw), 3),
                "exit_price": round(float(exit_price), 3),
                "holding_days": holding_day,
                "exit_reason": exit_reason,
                "gross_return": round(float(gross), 6),
                "net_return": round(float(net), 6),
                "blocked_exit_days": blocked_exit_days,
                "blocked_exit_original_reason": blocked_original_reason,
                "protect_activated": protection_active,
                "partial_activated": partial_active,
                "breakeven_exit": exit_reason == "breakeven_exit",
                "max_gain_before_exit": round(max_gain, 6),
                "path_dependency_flag": ";".join(sorted(path_flags)),
                "exit_rule_note": variant.label,
            }
        )
        return result
    return _baseline_trade(trade, variant)


def _simulate_all(trades: pd.DataFrame, frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for variant in EXIT_VARIANTS:
        for trade in trades.to_dict("records"):
            symbol = str(trade.get("symbol"))
            frame = frames.get(symbol)
            if frame is None:
                continue
            simulated = _simulate_trade(trade, frame, variant)
            if simulated:
                rows.append(simulated)
    return pd.DataFrame(rows)


def _portfolio_for_group(trades: pd.DataFrame, histories: dict[str, list[dict]], all_dates: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    portfolio = trades.copy()
    portfolio["portfolio_action"] = "accepted"
    return r4._portfolio_equity_curve(portfolio, histories, all_dates, INITIAL_CASH)


def _max_consecutive_losses(trades: pd.DataFrame) -> int:
    if trades.empty:
        return 0
    work = trades.copy().sort_values(["exit_date", "entry_date", "symbol"])
    current = best = 0
    for value in pd.to_numeric(work["net_return"], errors="coerce").fillna(0):
        if value < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return int(best)


def _monthly_stats(monthly: pd.DataFrame) -> dict[str, Any]:
    if monthly.empty:
        return {"positive_month_rate": None, "worst_month": None, "worst_month_return": None}
    work = monthly.copy()
    work["monthly_return"] = pd.to_numeric(work["monthly_return"], errors="coerce")
    valid = work.dropna(subset=["monthly_return"])
    if valid.empty:
        return {"positive_month_rate": None, "worst_month": None, "worst_month_return": None}
    worst = valid.sort_values("monthly_return").iloc[0]
    return {
        "positive_month_rate": float((valid["monthly_return"] > 0).mean()),
        "worst_month": str(worst["month"]),
        "worst_month_return": float(worst["monthly_return"]),
    }


def _yearly_from_equity(trades: pd.DataFrame, equity: pd.DataFrame, setup_key: str, variant: ExitVariant) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if equity.empty:
        return pd.DataFrame()
    work = equity.copy()
    work["date_dt"] = pd.to_datetime(work["date"], errors="coerce")
    trades = trades.copy()
    trades["entry_year"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.year
    for year in [2021, 2022, 2023, 2024, 2025, 2026]:
        eq = work[work["date_dt"].dt.year == year].copy()
        if eq.empty:
            continue
        group = trades[trades["entry_year"] == year].copy()
        returns = pd.to_numeric(group.get("net_return"), errors="coerce").dropna()
        start = float(eq["equity"].iloc[0])
        end = float(eq["equity"].iloc[-1])
        peak = eq["equity"].cummax()
        dd = eq["equity"] / peak - 1
        rows.append(
            {
                "setup_key": setup_key,
                "exit_variant": variant.key,
                "exit_variant_label": variant.label,
                "year": "2026 YTD" if year == 2026 else str(year),
                "return": end / start - 1 if start else None,
                "max_drawdown": float(dd.min()) if len(dd) else None,
                "accepted_trade_count": int(len(group)),
                "win_rate": float((returns > 0).mean()) if len(returns) else None,
                "average_trade_return": float(returns.mean()) if len(returns) else None,
                "median_trade_return": float(returns.median()) if len(returns) else None,
                "setup_failure_exit_count": int((group["exit_reason"] == "setup_failure_exit").sum()) if not group.empty else 0,
                "environment_exit_count": int((group["exit_reason"] == "environment_exit").sum()) if not group.empty else 0,
                "limit_down_blocked_exit_count": int((group["exit_reason"] == "limit_down_blocked_exit").sum()) if not group.empty else 0,
                "take_profit_count": int((group["exit_reason"] == "take_profit").sum()) if not group.empty else 0,
                "breakeven_exit_count": int((group["exit_reason"] == "breakeven_exit").sum()) if not group.empty else 0,
            }
        )
    return pd.DataFrame(rows)


def _summaries(simulated: pd.DataFrame, histories: dict[str, list[dict]], all_dates: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary_rows: list[dict[str, Any]] = []
    yearly_frames: list[pd.DataFrame] = []
    monthly_frames: list[pd.DataFrame] = []
    exit_rows: list[dict[str, Any]] = []
    trade_detail_frames: list[pd.DataFrame] = []
    baseline_lookup: dict[str, dict[str, float]] = {}

    for setup_key in FOCUS_SETUPS:
        for variant in EXIT_VARIANTS:
            group = simulated[(simulated["setup_key"] == setup_key) & (simulated["exit_variant"] == variant.key)].copy()
            if group.empty:
                continue
            equity, monthly, metrics = _portfolio_for_group(group, histories, all_dates)
            returns = pd.to_numeric(group["net_return"], errors="coerce").dropna()
            month_stats = _monthly_stats(monthly)
            yearly = _yearly_from_equity(group, equity, setup_key, variant)
            positive_year_rate = None
            worst_year = None
            worst_year_return = None
            if not yearly.empty:
                yearly["return"] = pd.to_numeric(yearly["return"], errors="coerce")
                positive_year_rate = float((yearly["return"] > 0).mean())
                worst = yearly.sort_values("return").iloc[0]
                worst_year = str(worst["year"])
                worst_year_return = float(worst["return"])
            protect_activation_rate = float(group["protect_activated"].mean()) if "protect_activated" in group else None
            partial_activation_rate = float(group["partial_activated"].mean()) if "partial_activated" in group else None
            breakeven_exit_count = int((group["exit_reason"] == "breakeven_exit").sum())
            row = {
                "setup_key": setup_key,
                "exit_variant": variant.key,
                "exit_variant_label": variant.label,
                "accepted_trade_count": int(len(group)),
                "total_return": metrics.get("total_return"),
                "annualized_return": metrics.get("annualized_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe": metrics.get("sharpe"),
                "calmar": metrics.get("calmar"),
                "win_rate": float((returns > 0).mean()) if len(returns) else None,
                "average_trade_return": float(returns.mean()) if len(returns) else None,
                "median_trade_return": float(returns.median()) if len(returns) else None,
                "max_consecutive_losses": _max_consecutive_losses(group),
                "setup_failure_exit_count": int((group["exit_reason"] == "setup_failure_exit").sum()),
                "tight_setup_failure_exit_count": int((group["exit_reason"] == "tight_setup_failure_exit").sum()),
                "environment_exit_count": int((group["exit_reason"] == "environment_exit").sum()),
                "limit_down_blocked_exit_count": int((group["exit_reason"] == "limit_down_blocked_exit").sum()),
                "take_profit_count": int((group["exit_reason"] == "take_profit").sum()),
                "breakeven_exit_count": breakeven_exit_count,
                "profit_protect_exit_count": int((group["exit_reason"] == "profit_protect_exit").sum()),
                "fast_fail_exit_count": int((group["exit_reason"] == "fast_fail_exit").sum()),
                "protect_activation_rate": protect_activation_rate,
                "partial_activation_rate": partial_activation_rate,
                "positive_year_rate": positive_year_rate,
                "positive_month_rate": month_stats.get("positive_month_rate"),
                "worst_year": worst_year,
                "worst_year_return": worst_year_return,
                "worst_month": month_stats.get("worst_month"),
                "worst_month_return": month_stats.get("worst_month_return"),
                "path_dependency_trade_count": int(group["path_dependency_flag"].fillna("").astype(str).str.len().gt(0).sum()),
            }
            if variant.key == "baseline":
                baseline_lookup[setup_key] = {
                    "total_return": row["total_return"],
                    "max_drawdown": row["max_drawdown"],
                    "median_trade_return": row["median_trade_return"],
                }
            summary_rows.append(row)
            y = yearly.copy()
            m = monthly.copy()
            y["setup_key"] = setup_key
            y["exit_variant"] = variant.key
            y["exit_variant_label"] = variant.label
            m["setup_key"] = setup_key
            m["exit_variant"] = variant.key
            m["exit_variant_label"] = variant.label
            yearly_frames.append(y)
            monthly_frames.append(m)
            trade_detail_frames.append(group)
            for reason, reason_group in group.groupby("exit_reason", dropna=False):
                r = pd.to_numeric(reason_group["net_return"], errors="coerce").dropna()
                exit_rows.append(
                    {
                        "setup_key": setup_key,
                        "exit_variant": variant.key,
                        "exit_reason": reason,
                        "trade_count": int(len(reason_group)),
                        "win_rate": float((r > 0).mean()) if len(r) else None,
                        "average_return": float(r.mean()) if len(r) else None,
                        "median_return": float(r.median()) if len(r) else None,
                        "max_loss": float(r.min()) if len(r) else None,
                    }
                )
    summary = pd.DataFrame(summary_rows)
    for idx, rec in summary.iterrows():
        base = baseline_lookup.get(str(rec["setup_key"]), {})
        if base:
            summary.loc[idx, "baseline_total_return_delta"] = (_safe_float(rec["total_return"]) or 0) - (_safe_float(base.get("total_return")) or 0)
            summary.loc[idx, "baseline_max_drawdown_delta"] = (_safe_float(rec["max_drawdown"]) or 0) - (_safe_float(base.get("max_drawdown")) or 0)
            summary.loc[idx, "baseline_median_return_delta"] = (_safe_float(rec["median_trade_return"]) or 0) - (_safe_float(base.get("median_trade_return")) or 0)
    yearly_all = pd.concat(yearly_frames, ignore_index=True) if yearly_frames else pd.DataFrame()
    monthly_all = pd.concat(monthly_frames, ignore_index=True) if monthly_frames else pd.DataFrame()
    exit_reason = pd.DataFrame(exit_rows)
    details = pd.concat(trade_detail_frames, ignore_index=True) if trade_detail_frames else pd.DataFrame()
    return summary, yearly_all, monthly_all, exit_reason, details


def _render_path_warning(output_dir: Path, summary: pd.DataFrame, details: pd.DataFrame) -> None:
    lines = [
        "# Setup Exit Path Dependency Warning",
        "",
        "本轮退出实验使用日线 OHLC 重放，无法知道盘中高低点先后顺序。因此所有依赖 high/low 触发的结果都只能作为 shadow 诊断。",
        "",
        "## Conservative Assumptions",
        "",
        "- 同日同时触及止盈和失败点，按不利顺序处理。",
        "- 保护规则在当日触发后，原则上从后续路径生效；不假设卖在最高点。",
        "- 保本退出按触发价附近执行，并扣除滑点和双边成本。",
        "- 跌停无法卖出时延后退出，并标记 `limit_down_blocked_exit`。",
        "",
        "## Path Dependency Counts",
        "",
    ]
    rows = details.copy()
    rows["has_path_flag"] = rows["path_dependency_flag"].fillna("").astype(str).str.len() > 0
    path_counts = rows.groupby(["setup_key", "exit_variant"]).agg(
        trade_count=("symbol", "count"),
        path_dependency_trade_count=("has_path_flag", "sum"),
    ).reset_index()
    path_counts["path_dependency_rate"] = path_counts["path_dependency_trade_count"] / path_counts["trade_count"]
    lines.extend(
        _markdown_table(
            path_counts,
            [
                ("setup", "setup_key"),
                ("实验", "exit_variant"),
                ("交易", "trade_count"),
                ("路径依赖交易", "path_dependency_trade_count"),
                ("路径依赖比例", "path_dependency_rate"),
            ],
            max_rows=80,
        )
    )
    lines.extend(
        [
            "",
            "## Experiments Requiring Minute Data",
            "",
            "- `profit_protect_2pct`、`profit_protect_3pct`、`no_loss_after_2pct_mfe`：依赖触及浮盈后的回落路径，需要分钟数据验证。",
            "- `partial_take_profit_3pct`：依赖 +3% 触发半仓成交，需要分钟数据验证。",
            "- `tight_setup_failure_*`：若当日同时触及反弹和失败点，日线无法确认先后顺序，需要分钟数据确认误伤程度。",
        ]
    )
    (output_dir / "setup_exit_path_dependency_warning.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_candidate_review(output_dir: Path, summary: pd.DataFrame, yearly: pd.DataFrame) -> None:
    ranked = summary[summary["exit_variant"] != "baseline"].sort_values(["total_return", "max_drawdown"], ascending=[False, False]).copy()
    lines = [
        "# Setup Exit Experiment Candidate Review",
        "",
        "本文件只列出 shadow experiment candidate，不代表可以并入主策略。",
        "",
        "## Top Candidates By Total Return",
        "",
    ]
    lines.extend(
        _markdown_table(
            ranked.head(12),
            [
                ("setup", "setup_key"),
                ("实验", "exit_variant"),
                ("累计收益", "total_return"),
                ("相对baseline", "baseline_total_return_delta"),
                ("最大回撤", "max_drawdown"),
                ("Sharpe", "sharpe"),
                ("胜率", "win_rate"),
                ("中位", "median_trade_return"),
                ("正收益年份", "positive_year_rate"),
                ("路径依赖交易", "path_dependency_trade_count"),
            ],
        )
    )
    lines.extend(["", "## Year Concentration Check", ""])
    positive = ranked[ranked["total_return"] > 0].copy()
    if positive.empty:
        lines.append("- 没有退出实验在当前组合口径下转正。")
    else:
        for row in positive.to_dict("records"):
            y = yearly[(yearly["setup_key"] == row["setup_key"]) & (yearly["exit_variant"] == row["exit_variant"])].copy()
            y["return"] = pd.to_numeric(y["return"], errors="coerce")
            best_year = y.sort_values("return", ascending=False).head(1).to_dict("records")
            pos_rate = float((y["return"] > 0).mean()) if not y.empty else None
            lines.append(f"- `{row['setup_key']} / {row['exit_variant']}` 转正，正收益年份比例 {_fmt_pct(pos_rate)}，最好年份 {best_year[0]['year'] if best_year else '-'}。仍只能列为 `shadow_experiment_candidate`。")
    lines.extend(
        [
            "",
            "## Review Notes",
            "",
            "- 若候选改善主要来自 high/low 触发保护，必须进入分钟数据验证。",
            "- 若候选只改善回撤但收益仍为负，说明 exit 只能降噪，setup 信号本身仍不足。",
        ]
    )
    (output_dir / "setup_exit_experiment_candidate_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_report(output_dir: Path, summary: pd.DataFrame, yearly: pd.DataFrame) -> None:
    ranked = summary.sort_values(["setup_key", "total_return"], ascending=[True, False])
    best = summary[summary["exit_variant"] != "baseline"].sort_values(["total_return", "max_drawdown"], ascending=[False, False]).head(1)
    all_negative = bool((summary[summary["exit_variant"] != "baseline"]["total_return"] < 0).all())
    lines = [
        "# Setup Exit Shadow Experiment Report",
        "",
        "本报告只对 `volatility_contraction_breakout_v1` 与 `pullback_reclaim_v1` 的已接受交易重放退出机制。它不新增 setup、不调入场条件、不并入主策略。",
        "",
        "## Experiment Summary",
        "",
    ]
    lines.extend(
        _markdown_table(
            ranked,
            [
                ("setup", "setup_key"),
                ("退出实验", "exit_variant"),
                ("交易", "accepted_trade_count"),
                ("累计收益", "total_return"),
                ("相对baseline", "baseline_total_return_delta"),
                ("最大回撤", "max_drawdown"),
                ("Sharpe", "sharpe"),
                ("Calmar", "calmar"),
                ("胜率", "win_rate"),
                ("平均", "average_trade_return"),
                ("中位", "median_trade_return"),
                ("最大连亏", "max_consecutive_losses"),
                ("setup失败", "setup_failure_exit_count"),
                ("环境退出", "environment_exit_count"),
                ("跌停无法卖出", "limit_down_blocked_exit_count"),
                ("止盈", "take_profit_count"),
                ("保本", "breakeven_exit_count"),
                ("正收益年份", "positive_year_rate"),
                ("正收益月份", "positive_month_rate"),
                ("最差年份", "worst_year"),
                ("最差月份", "worst_month"),
            ],
            max_rows=120,
        )
    )
    lines.extend(["", "## A. 已确认事实", ""])
    lines.append("- 当前 setup 信号长样本仍未验证收益端优势；本轮只检验退出是否能减少浮盈回吐。")
    if not best.empty:
        rec = best.iloc[0].to_dict()
        lines.append(f"- 最好非 baseline 实验是 `{rec['setup_key']} / {rec['exit_variant']}`，累计收益 {_fmt_pct(rec['total_return'])}，相对 baseline 改善 {_fmt_pct(rec['baseline_total_return_delta'])}。")
    lines.append("- 所有依赖日线 high/low 的保护规则都存在路径依赖，需要分钟数据验证。")
    lines.extend(["", "## B. 哪些退出实验改善明显", ""])
    improved = summary[(summary["exit_variant"] != "baseline") & (summary["baseline_total_return_delta"] > 0)].sort_values("baseline_total_return_delta", ascending=False)
    if improved.empty:
        lines.append("- 没有退出实验相对 baseline 明显改善累计收益。")
    else:
        lines.extend(
            _markdown_table(
                improved.head(10),
                [
                    ("setup", "setup_key"),
                    ("实验", "exit_variant"),
                    ("累计收益", "total_return"),
                    ("相对baseline", "baseline_total_return_delta"),
                    ("最大回撤", "max_drawdown"),
                    ("中位", "median_trade_return"),
                    ("保本", "breakeven_exit_count"),
                    ("路径依赖", "path_dependency_trade_count"),
                ],
            )
        )
    lines.extend(["", "## C. 哪些改善可能来自日线路径假设", ""])
    path_sensitive = summary[(summary["exit_variant"].isin(["profit_protect_2pct", "profit_protect_3pct", "partial_take_profit_3pct", "no_loss_after_2pct_mfe"]))].copy()
    lines.extend(
        _markdown_table(
            path_sensitive,
            [
                ("setup", "setup_key"),
                ("实验", "exit_variant"),
                ("累计收益", "total_return"),
                ("路径依赖交易", "path_dependency_trade_count"),
                ("保护激活", "protect_activation_rate"),
                ("半仓激活", "partial_activation_rate"),
            ],
            max_rows=40,
        )
    )
    lines.extend(["", "## D. 是否需要分钟数据确认", ""])
    lines.append("- 需要。尤其是浮盈保护、半仓止盈、同日触及止盈和失败点的交易，日线无法确认先后顺序。")
    lines.append("- 如果某个实验转正，也只能列为 `shadow_experiment_candidate`，不能并入主策略。")
    lines.extend(["", "## E. 是否仍不能并入主策略", ""])
    if all_negative:
        lines.append("- 当前所有退出实验仍为负，说明当前 setup 信号本身仍不足。")
    else:
        lines.append("- 即使有退出实验转正，也仍不能并入主策略；必须先做分钟数据路径确认和人工复盘。")
    lines.extend(["", "## F. 下一步人工复盘重点", ""])
    lines.append("- 优先复盘触及 +2% 后回吐的交易，确认是否存在可执行的保本或半仓退出路径。")
    lines.append("- 复盘 fast fail 样本，确认 pullback 的次日无法延续是否能通过更保守的退出降低损失。")
    lines.append("- 对 tight failure 的赢家误伤做 K 线复盘，避免只看到亏损缩小而忽略盈利被截断。")
    (output_dir / "setup_exit_experiment_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(setup_dir: Path = SETUP_OUTPUT_DIR, output_dir: Path = DEFAULT_OUTPUT_DIR, kline_dir: Path = DEFAULT_KLINE_DIR) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trades = _load_trades(setup_dir)
    frames, histories = _load_histories(kline_dir, trades["symbol"].astype(str).tolist())
    simulated = _simulate_all(trades, frames)
    all_dates = pd.read_csv(setup_dir / "volatility_contraction_breakout_v1_daily_equity.csv", encoding="utf-8-sig")["date"].astype(str).tolist()
    summary, yearly, monthly, exit_reason, details = _summaries(simulated, histories, all_dates)
    _write_csv(output_dir / "setup_exit_experiment_summary.csv", summary)
    _write_csv(output_dir / "setup_exit_experiment_yearly.csv", yearly)
    _write_csv(output_dir / "setup_exit_experiment_monthly.csv", monthly)
    _write_csv(output_dir / "setup_exit_experiment_exit_reason.csv", exit_reason)
    _write_csv(output_dir / "setup_exit_experiment_trades.csv", details)
    _render_path_warning(output_dir, summary, details)
    _render_candidate_review(output_dir, summary, yearly)
    _render_report(output_dir, summary, yearly)
    return {
        "output_dir": str(output_dir),
        "variant_count": len(EXIT_VARIANTS),
        "simulated_trade_rows": int(len(details)),
        "best": summary.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).head(1).to_dict("records")[0],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run setup exit shadow experiments.")
    parser.add_argument("--setup-dir", default=str(SETUP_OUTPUT_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--kline-dir", default=str(DEFAULT_KLINE_DIR))
    args = parser.parse_args()
    payload = run(Path(args.setup_dir), Path(args.output), Path(args.kline_dir))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
