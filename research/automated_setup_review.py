#!/usr/bin/env python3
"""Automated setup review for short-swing shadow research.

This module replaces large manual K-line review queues with deterministic
machine labels, aggregation, and simple cluster diagnostics. It is read-only
with respect to strategy configuration and does not add strategies, factors,
live trading, or order execution.
"""

from __future__ import annotations

import argparse
import itertools
import math
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SETUP_DIR = ROOT / "output" / "setup_based_short_swing"
EXIT_DIR = ROOT / "output" / "setup_based_short_swing_exit_experiment"
DATA_DIR = ROOT / "data" / "expanded"
OUTPUT_DIR = ROOT / "output" / "automated_setup_review"

FOCUS_SETUPS = ["volatility_contraction_breakout_v1", "pullback_reclaim_v1"]
FOCUS_EXIT_CANDIDATES = {
    ("volatility_contraction_breakout_v1", "fast_fail_exit_day2"),
    ("pullback_reclaim_v1", "partial_take_profit_3pct"),
}

LABEL_GROUPS: dict[str, list[str]] = {
    "entry_quality": [
        "gap_up_large",
        "gap_down_weak",
        "close_near_day_low",
        "long_upper_shadow",
        "volume_overheated",
        "weak_close_after_breakout",
        "late_entry_risk",
        "near_limit_up_risk",
    ],
    "setup_quality": [
        "false_breakout_proxy",
        "pullback_not_reclaimed",
        "next_day_no_follow_through",
        "trend_damaged_before_entry",
        "platform_too_short",
        "volatility_not_contracting",
        "pullback_too_deep",
        "too_far_from_ma20",
    ],
    "sector_market": [
        "sector_weakening",
        "sector_not_broad_enough",
        "market_weakening_after_entry",
        "defensive_transition",
        "industry_crowding",
    ],
    "profit_giveback": [
        "mfe_above_2_then_loss",
        "mfe_above_3_then_loss",
        "mfe_above_5_then_loss",
        "fast_profit_then_reversal",
        "profit_not_protected",
        "path_dependent_exit",
    ],
    "loss_path": [
        "mae_below_2_fast",
        "mae_below_3_fast",
        "setup_failure_fast",
        "limit_down_or_liquidity_risk",
        "loss_without_mfe",
        "slow_bleed_loss",
    ],
}

LABELS = [label for labels in LABEL_GROUPS.values() for label in labels]

LABEL_DESCRIPTIONS = {
    "gap_up_large": "次日高开过大，容易变成追高成交。",
    "gap_down_weak": "次日低开且走势偏弱。",
    "close_near_day_low": "触发日收盘接近日内低位。",
    "long_upper_shadow": "信号日或入场日上影线偏长。",
    "volume_overheated": "成交额/量比放大过猛。",
    "weak_close_after_breakout": "突破后收盘不强。",
    "late_entry_risk": "入场离短期低点或 MA20 过远。",
    "near_limit_up_risk": "接近涨停或当日过热追入风险。",
    "false_breakout_proxy": "突破后快速跌回平台的日线代理标签。",
    "pullback_not_reclaimed": "回踩后没有真正重新转强。",
    "next_day_no_follow_through": "次日无法延续。",
    "trend_damaged_before_entry": "入场前趋势已有破坏迹象。",
    "platform_too_short": "平台整理时间不足。",
    "volatility_not_contracting": "波动没有真正收缩。",
    "pullback_too_deep": "回踩过深。",
    "too_far_from_ma20": "距离 MA20 过远。",
    "sector_weakening": "入场时或入场后行业转弱。",
    "sector_not_broad_enough": "行业内部共振不足。",
    "market_weakening_after_entry": "持仓期市场转弱导致退出。",
    "defensive_transition": "持仓阶段进入偏防守状态。",
    "industry_crowding": "交易集中在少数高频行业。",
    "mfe_above_2_then_loss": "曾有 +2% 以上浮盈但最终亏损。",
    "mfe_above_3_then_loss": "曾有 +3% 以上浮盈但最终亏损。",
    "mfe_above_5_then_loss": "曾有 +5% 以上浮盈但最终亏损。",
    "fast_profit_then_reversal": "早期快速浮盈后反转。",
    "profit_not_protected": "浮盈未被保护并转亏。",
    "path_dependent_exit": "依赖日线 high/low 先后顺序的退出路径。",
    "mae_below_2_fast": "前两天快速达到 -2% 浮亏。",
    "mae_below_3_fast": "前两天快速达到 -3% 浮亏。",
    "setup_failure_fast": "很快触发 setup failure。",
    "limit_down_or_liquidity_risk": "跌停或流动性阻塞风险。",
    "loss_without_mfe": "几乎没有浮盈即亏损。",
    "slow_bleed_loss": "缓慢磨损式亏损。",
}


def _safe_float(value: Any, default: float = math.nan) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    if pd.isna(value):
        return False
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y", "t"}


def _not_blank(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if pd.isna(value):
        return False
    return str(value).strip() not in {"", "nan", "None"}


def _ge(value: Any, threshold: float) -> bool:
    value_float = _safe_float(value)
    return not math.isnan(value_float) and value_float >= threshold


def _le(value: Any, threshold: float) -> bool:
    value_float = _safe_float(value)
    return not math.isnan(value_float) and value_float <= threshold


def _fmt_pct(value: Any, digits: int = 2) -> str:
    value_float = _safe_float(value)
    if math.isnan(value_float):
        return "-"
    return f"{value_float * 100:.{digits}f}%"


def _fmt_num(value: Any, digits: int = 4) -> str:
    value_float = _safe_float(value)
    if math.isnan(value_float):
        return "-"
    return f"{value_float:.{digits}f}"


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _read_csv(path: Path, required: bool = False) -> pd.DataFrame:
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def _date_year(value: Any) -> str:
    text = str(value)
    if len(text) >= 4:
        return text[:4]
    return "unknown"


def _trade_key_columns() -> list[str]:
    return ["setup_key", "symbol", "entry_date", "exit_date"]


def _normalise_trade_columns(trades: pd.DataFrame) -> pd.DataFrame:
    trades = trades.copy()
    for col in [
        "net_return",
        "mfe_5d",
        "mae_5d",
        "mfe_until_exit",
        "mae_until_exit",
        "entry_gap_pct",
        "entry_fade_pct",
        "entry_chase_pct",
        "signal_upper_shadow_ratio",
        "entry_day_upper_shadow",
        "upper_shadow_ratio",
        "volume_ratio",
        "turnover_ratio_tag",
        "vol10_to_vol20_tag",
        "platform_range20_tag",
        "change_rate_5d",
        "signal_day_return",
        "close_to_20d_high",
        "ma20_distance_signal",
        "industry_up_ratio",
        "industry_above_ma20_ratio",
        "industry_strong_count",
        "holding_days",
        "day1_return",
        "day2_return",
        "day3_return",
        "day5_return",
        "first_up_1pct_day",
        "first_up_2pct_day",
        "first_up_3pct_day",
        "first_up_4pct_day",
        "first_up_5pct_day",
        "first_down_1pct_day",
        "first_down_2pct_day",
        "first_down_3pct_day",
        "first_down_4pct_day",
        "first_down_5pct_day",
        "blocked_exit_days",
        "position_pct",
        "portfolio_position_pct",
    ]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    for col in ["sector_weakened_after_signal", "same_day_stop_take", "partial_take_profit"]:
        if col in trades.columns:
            trades[col] = trades[col].map(_truthy)
    for col in ["signal_date", "entry_date", "exit_date"]:
        if col in trades.columns:
            trades[col] = trades[col].astype(str)
    trades["year"] = trades["entry_date"].map(_date_year)
    return trades


def load_inputs(setup_dir: Path, exit_dir: Path, data_dir: Path) -> dict[str, pd.DataFrame]:
    trades = _read_csv(setup_dir / "mfe_mae_trades.csv", required=True)
    trades = trades[trades["setup_key"].isin(FOCUS_SETUPS)].copy()
    trades = _normalise_trade_columns(trades)

    manual = _read_csv(exit_dir / "manual_review_priority_list.csv")
    exit_summary = _read_csv(exit_dir / "setup_exit_experiment_summary.csv")
    exit_trades = _read_csv(exit_dir / "setup_exit_experiment_trades.csv")
    market_states = _read_csv(setup_dir / "setup_market_states.csv")
    sector_stats = _read_csv(setup_dir / "setup_sector_stats.csv")
    industry_map = _read_csv(data_dir / "industry_or_sector.csv")

    return {
        "trades": trades,
        "manual": manual,
        "exit_summary": exit_summary,
        "exit_trades": exit_trades,
        "market_states": market_states,
        "sector_stats": sector_stats,
        "industry_map": industry_map,
    }


def attach_exit_and_priority_context(trades: pd.DataFrame, manual: pd.DataFrame, exit_trades: pd.DataFrame) -> pd.DataFrame:
    trades = trades.copy()
    trades["candidate_exit_variants"] = ""
    trades["manual_review_buckets"] = ""
    trades["manual_path_dependency_flags"] = ""

    if not manual.empty:
        for col in _trade_key_columns():
            if col in manual.columns:
                manual[col] = manual[col].astype(str)
        manual = manual[manual["setup_key"].isin(FOCUS_SETUPS)].copy()
        manual_group = (
            manual.groupby(_trade_key_columns())
            .agg(
                candidate_exit_variants=("exit_variant", lambda values: ";".join(sorted(set(map(str, values))))),
                manual_review_buckets=("review_bucket", lambda values: ";".join(sorted(set(map(str, values))))),
                manual_path_dependency_flags=(
                    "path_dependency_flag",
                    lambda values: ";".join(sorted({str(v) for v in values if _not_blank(v)})),
                ),
            )
            .reset_index()
        )
        trades = trades.merge(manual_group, on=_trade_key_columns(), how="left", suffixes=("", "_manual"))
        for col in ["candidate_exit_variants", "manual_review_buckets", "manual_path_dependency_flags"]:
            if f"{col}_manual" in trades.columns:
                trades[col] = trades[f"{col}_manual"].fillna(trades[col]).fillna("")
                trades = trades.drop(columns=[f"{col}_manual"])

    trades["candidate_path_dependency_flags"] = ""
    if not exit_trades.empty:
        candidate_trades = exit_trades[
            exit_trades.apply(lambda r: (r.get("setup_key"), r.get("exit_variant")) in FOCUS_EXIT_CANDIDATES, axis=1)
        ].copy()
        if not candidate_trades.empty:
            for col in _trade_key_columns():
                if col in candidate_trades.columns:
                    candidate_trades[col] = candidate_trades[col].astype(str)
            candidate_group = (
                candidate_trades.groupby(_trade_key_columns())
                .agg(
                    candidate_path_dependency_flags=(
                        "path_dependency_flag",
                        lambda values: ";".join(sorted({str(v) for v in values if _not_blank(v)})),
                    ),
                    candidate_exit_notes=("exit_rule_note", lambda values: ";".join(sorted({str(v) for v in values if _not_blank(v)}))),
                )
                .reset_index()
            )
            trades = trades.merge(candidate_group, on=_trade_key_columns(), how="left", suffixes=("", "_candidate"))
            for col in ["candidate_path_dependency_flags", "candidate_exit_notes"]:
                if f"{col}_candidate" in trades.columns:
                    trades[col] = trades[f"{col}_candidate"].fillna(trades.get(col, "")).fillna("")
                    trades = trades.drop(columns=[f"{col}_candidate"])
    if "candidate_exit_notes" not in trades.columns:
        trades["candidate_exit_notes"] = ""
    return trades


def crowded_industries(trades: pd.DataFrame) -> set[str]:
    counts = trades["industry"].fillna("unknown").value_counts()
    if counts.empty:
        return set()
    threshold = max(8, float(counts.quantile(0.90)))
    return set(counts[counts >= threshold].index)


def label_trade(row: pd.Series, crowded: set[str]) -> dict[str, bool]:
    setup = str(row.get("setup_key", ""))
    exit_reason = str(row.get("exit_reason", ""))
    net_return = _safe_float(row.get("net_return"))
    mfe = _safe_float(row.get("mfe_5d"))
    mae = _safe_float(row.get("mae_5d"))
    holding_days = _safe_float(row.get("holding_days"), 0.0)
    first_up_2 = _safe_float(row.get("first_up_2pct_day"))
    first_up_3 = _safe_float(row.get("first_up_3pct_day"))
    first_down_2 = _safe_float(row.get("first_down_2pct_day"))
    first_down_3 = _safe_float(row.get("first_down_3pct_day"))

    upper_shadow_values = [
        _safe_float(row.get("signal_upper_shadow_ratio")),
        _safe_float(row.get("entry_day_upper_shadow")),
        _safe_float(row.get("upper_shadow_ratio")),
    ]
    max_upper_shadow = max([value for value in upper_shadow_values if not math.isnan(value)] or [math.nan])

    is_loss = not math.isnan(net_return) and net_return < 0
    is_volatility = setup == "volatility_contraction_breakout_v1"
    is_pullback = setup == "pullback_reclaim_v1"

    labels = {
        "gap_up_large": _ge(row.get("entry_gap_pct"), 0.030),
        "gap_down_weak": _le(row.get("entry_gap_pct"), -0.020)
        and (_le(row.get("entry_fade_pct"), -0.015) or _le(row.get("day1_return"), -0.005)),
        "close_near_day_low": _le(row.get("entry_fade_pct"), -0.025),
        "long_upper_shadow": not math.isnan(max_upper_shadow) and max_upper_shadow >= 0.45,
        "volume_overheated": _ge(row.get("volume_ratio"), 1.90)
        or _ge(row.get("turnover_ratio_tag"), 2.10)
        or _truthy(row.get("turnover_too_hot")),
        "weak_close_after_breakout": is_volatility
        and (_le(row.get("entry_fade_pct"), -0.015) or _le(row.get("day1_return"), -0.005) or _ge(row.get("entry_day_upper_shadow"), 0.45)),
        "late_entry_risk": _ge(row.get("entry_chase_pct"), 0.020)
        or _ge(row.get("ma20_distance_signal"), 0.120)
        or _truthy(row.get("entry_chase_too_high")),
        "near_limit_up_risk": _ge(row.get("signal_day_return"), 0.070) or _ge(row.get("entry_gap_pct"), 0.040),
        "false_breakout_proxy": _truthy(row.get("false_breakout_proxy"))
        or (is_volatility and _le(row.get("day2_return"), -0.020) and _le(row.get("entry_fade_pct"), -0.010)),
        "pullback_not_reclaimed": is_pullback
        and (_truthy(row.get("entry_next_day_no_followthrough")) or _le(row.get("day1_return"), -0.005)),
        "next_day_no_follow_through": _truthy(row.get("entry_next_day_no_followthrough")) or _le(row.get("day1_return"), -0.010),
        "trend_damaged_before_entry": _truthy(row.get("trend_maybe_damaged"))
        or _truthy(row.get("trend_damage_last5_low_vs_ma20")),
        "platform_too_short": _truthy(row.get("platform_maybe_short"))
        or (is_volatility and _le(row.get("platform_days_upper_half"), 5)),
        "volatility_not_contracting": _truthy(row.get("volatility_not_really_contracted"))
        or _truthy(row.get("contraction_not_obvious"))
        or _ge(row.get("vol10_to_vol20_tag"), 0.90),
        "pullback_too_deep": is_pullback and (_le(row.get("close_to_20d_high"), -0.100) or _ge(row.get("pullback_days_from_10d_high"), 8)),
        "too_far_from_ma20": _truthy(row.get("ma20_distance_too_far")) or _ge(row.get("ma20_distance_signal"), 0.120),
        "sector_weakening": _truthy(row.get("sector_weakened_after_signal"))
        or _le(row.get("industry_ret3_rank_pct"), 0.25)
        or _le(row.get("industry_ret5_rank_pct"), 0.25),
        "sector_not_broad_enough": _le(row.get("industry_up_ratio"), 0.55)
        or _le(row.get("industry_above_ma20_ratio"), 0.55)
        or _le(row.get("industry_strong_count"), 1),
        "market_weakening_after_entry": exit_reason == "environment_exit",
        "defensive_transition": exit_reason == "environment_exit" and str(row.get("market_state", "")) != "strong_active",
        "industry_crowding": str(row.get("industry", "unknown")) in crowded,
        "mfe_above_2_then_loss": is_loss and _ge(mfe, 0.020),
        "mfe_above_3_then_loss": is_loss and _ge(mfe, 0.030),
        "mfe_above_5_then_loss": is_loss and _ge(mfe, 0.050),
        "fast_profit_then_reversal": is_loss
        and ((not math.isnan(first_up_2) and first_up_2 <= 2) or (not math.isnan(first_up_3) and first_up_3 <= 3)),
        "profit_not_protected": is_loss and _ge(mfe, 0.020) and exit_reason != "take_profit",
        "path_dependent_exit": _not_blank(row.get("manual_path_dependency_flags"))
        or _not_blank(row.get("candidate_path_dependency_flags"))
        or _truthy(row.get("same_day_stop_take"))
        or "limit_down" in exit_reason,
        "mae_below_2_fast": (not math.isnan(first_down_2) and first_down_2 <= 2)
        or (_le(mae, -0.020) and holding_days <= 2),
        "mae_below_3_fast": (not math.isnan(first_down_3) and first_down_3 <= 2)
        or (_le(mae, -0.030) and holding_days <= 2),
        "setup_failure_fast": exit_reason == "setup_failure_exit" and holding_days <= 2,
        "limit_down_or_liquidity_risk": "limit_down" in exit_reason or _ge(row.get("blocked_exit_days"), 1),
        "loss_without_mfe": is_loss and (math.isnan(mfe) or mfe < 0.010),
        "slow_bleed_loss": is_loss and holding_days >= 4 and (math.isnan(mfe) or mfe < 0.020) and _ge(mae, -0.050),
    }
    return labels


def build_auto_labels(trades: pd.DataFrame) -> pd.DataFrame:
    crowded = crowded_industries(trades)
    label_rows = [label_trade(row, crowded) for _idx, row in trades.iterrows()]
    label_df = pd.DataFrame(label_rows).astype(bool)
    base = trades.drop(columns=[label for label in LABELS if label in trades.columns], errors="ignore")
    labeled = pd.concat([base.reset_index(drop=True), label_df], axis=1)
    labeled["active_label_count"] = label_df.sum(axis=1)
    labeled["active_labels"] = label_df.apply(lambda r: ";".join([label for label in LABELS if bool(r[label])]), axis=1)
    labeled["is_winner"] = labeled["net_return"] > 0
    labeled["is_loser"] = labeled["net_return"] < 0
    return labeled


def summarise_labels(df: pd.DataFrame, subset_mask: pd.Series, scope: str) -> pd.DataFrame:
    scope_df = df[subset_mask].copy()
    rows = []
    for label in LABELS:
        tagged = df[df[label]].copy()
        tagged_in_scope = scope_df[scope_df[label]].copy()
        rows.append(
            {
                "scope": scope,
                "label": label,
                "label_group": next(group for group, labels in LABEL_GROUPS.items() if label in labels),
                "description": LABEL_DESCRIPTIONS[label],
                "trade_count": len(tagged_in_scope),
                "scope_share": len(tagged_in_scope) / len(scope_df) if len(scope_df) else math.nan,
                "all_trade_count": len(tagged),
                "all_trade_share": len(tagged) / len(df) if len(df) else math.nan,
                "average_return": tagged["net_return"].mean() if len(tagged) else math.nan,
                "median_return": tagged["net_return"].median() if len(tagged) else math.nan,
                "win_rate": (tagged["net_return"] > 0).mean() if len(tagged) else math.nan,
                "average_mfe_5d": tagged["mfe_5d"].mean() if len(tagged) else math.nan,
                "average_mae_5d": tagged["mae_5d"].mean() if len(tagged) else math.nan,
                "top_setup": tagged["setup_key"].mode().iat[0] if len(tagged) else "",
                "top_industry": tagged["industry"].mode().iat[0] if len(tagged) else "",
                "top_year": tagged["year"].mode().iat[0] if len(tagged) else "",
            }
        )
    result = pd.DataFrame(rows)
    return result.sort_values(["trade_count", "average_return"], ascending=[False, True]).reset_index(drop=True)


def _combo_rows(df: pd.DataFrame, min_count: int = 5) -> pd.DataFrame:
    buckets: dict[tuple[str, ...], list[int]] = {}
    for idx, row in df.iterrows():
        active = [label for label in LABELS if bool(row[label])]
        for size in (2, 3):
            if len(active) < size:
                continue
            for combo in itertools.combinations(active, size):
                buckets.setdefault(combo, []).append(idx)
    rows = []
    for combo, indexes in buckets.items():
        if len(indexes) < min_count:
            continue
        sample = df.loc[indexes]
        rows.append(
            {
                "label_combo": " + ".join(combo),
                "combo_size": len(combo),
                "trade_count": len(sample),
                "average_return": sample["net_return"].mean(),
                "median_return": sample["net_return"].median(),
                "win_rate": (sample["net_return"] > 0).mean(),
                "average_mfe_5d": sample["mfe_5d"].mean(),
                "average_mae_5d": sample["mae_5d"].mean(),
                "setup_mix": ";".join(f"{k}:{v}" for k, v in sample["setup_key"].value_counts().head(3).items()),
                "top_industries": ";".join(f"{k}:{v}" for k, v in sample["industry"].value_counts().head(3).items()),
                "top_years": ";".join(f"{k}:{v}" for k, v in sample["year"].value_counts().head(3).items()),
            }
        )
    return pd.DataFrame(rows)


def label_combinations(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    combos = _combo_rows(df)
    if combos.empty:
        empty = pd.DataFrame()
        return empty, empty
    dangerous = combos.sort_values(
        ["median_return", "average_return", "trade_count"], ascending=[True, True, False]
    ).head(20)
    promising = combos.sort_values(
        ["median_return", "average_return", "win_rate", "trade_count"], ascending=[False, False, False, False]
    ).head(20)
    return dangerous.reset_index(drop=True), promising.reset_index(drop=True)


def label_contribution_by(df: pd.DataFrame, dimension: str) -> pd.DataFrame:
    rows = []
    if dimension not in df.columns:
        return pd.DataFrame()
    for value, group in df.groupby(dimension, dropna=False):
        for label in LABELS:
            tagged = group[group[label]]
            if tagged.empty:
                continue
            rows.append(
                {
                    "dimension": dimension,
                    "dimension_value": value,
                    "label": label,
                    "trade_count": len(tagged),
                    "dimension_trade_count": len(group),
                    "label_share_in_dimension": len(tagged) / len(group) if len(group) else math.nan,
                    "average_return": tagged["net_return"].mean(),
                    "median_return": tagged["net_return"].median(),
                    "win_rate": (tagged["net_return"] > 0).mean(),
                    "average_mfe_5d": tagged["mfe_5d"].mean(),
                    "average_mae_5d": tagged["mae_5d"].mean(),
                }
            )
    return pd.DataFrame(rows).sort_values(["dimension_value", "trade_count"], ascending=[True, False])


def simple_kmeans(matrix: np.ndarray, k: int = 6, max_iter: int = 80, seed: int = 42) -> np.ndarray:
    n = matrix.shape[0]
    if n == 0:
        return np.array([], dtype=int)
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    init = rng.choice(n, size=k, replace=False)
    centers = matrix[init].astype(float)
    labels = np.zeros(n, dtype=int)
    for _ in range(max_iter):
        distances = ((matrix[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        next_labels = distances.argmin(axis=1)
        if np.array_equal(next_labels, labels):
            break
        labels = next_labels
        for cluster_id in range(k):
            members = matrix[labels == cluster_id]
            if len(members):
                centers[cluster_id] = members.mean(axis=0)
            else:
                farthest = distances.min(axis=1).argmax()
                centers[cluster_id] = matrix[farthest]
    return labels


def cluster_profiles(df: pd.DataFrame, k: int = 6) -> tuple[pd.DataFrame, pd.DataFrame]:
    matrix = df[LABELS].astype(float).to_numpy()
    clusters = simple_kmeans(matrix, k=k)
    clustered = df.copy()
    clustered["cluster_id"] = clusters
    rows = []
    overall_avg = df["net_return"].mean()
    for cluster_id, group in clustered.groupby("cluster_id"):
        label_rates = {label: float(group[label].mean()) for label in LABELS}
        main_labels = sorted(label_rates.items(), key=lambda item: item[1], reverse=True)[:8]
        avg_return = group["net_return"].mean()
        median_return = group["net_return"].median()
        win_rate = (group["net_return"] > 0).mean()
        if len(group) >= 20 and avg_return < overall_avg and median_return < 0:
            disposition = "archive_or_avoid"
        elif len(group) >= 10 and (avg_return > 0 or median_return > 0):
            disposition = "continue_shadow_observation"
        else:
            disposition = "diagnostic_only"
        rows.append(
            {
                "cluster_id": int(cluster_id),
                "trade_count": len(group),
                "average_return": avg_return,
                "median_return": median_return,
                "win_rate": win_rate,
                "average_mfe_5d": group["mfe_5d"].mean(),
                "average_mae_5d": group["mae_5d"].mean(),
                "main_labels": ";".join(f"{label}:{rate:.0%}" for label, rate in main_labels if rate > 0),
                "main_setups": ";".join(f"{k}:{v}" for k, v in group["setup_key"].value_counts().head(3).items()),
                "main_industries": ";".join(f"{k}:{v}" for k, v in group["industry"].value_counts().head(5).items()),
                "main_years": ";".join(f"{k}:{v}" for k, v in group["year"].value_counts().head(5).items()),
                "disposition": disposition,
            }
        )
    return clustered, pd.DataFrame(rows).sort_values("cluster_id").reset_index(drop=True)


def build_top_10_review(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    used: set[int] = set()

    def add_from(sample: pd.DataFrame, reason: str, limit: int) -> None:
        nonlocal rows
        for idx, row in sample.iterrows():
            if idx in used:
                continue
            used.add(idx)
            payload = row.to_dict()
            payload["review_reason"] = reason
            rows.append(payload)
            if len([r for r in rows if r.get("review_reason") == reason]) >= limit:
                break

    giveback = df[
        (df["net_return"] < 0)
        & ((df["mfe_above_3_then_loss"]) | (df["path_dependent_exit"]) | (df["profit_not_protected"]))
    ].copy()
    giveback["giveback_score"] = giveback["mfe_5d"].fillna(0) - giveback["net_return"].fillna(0)
    add_from(giveback.sort_values("giveback_score", ascending=False), "high_mfe_or_path_dependent_giveback", 4)

    clean_winners = df[(df["net_return"] > 0) & (df["active_label_count"] <= 4)].copy()
    add_from(clean_winners.sort_values("net_return", ascending=False), "clean_winner_structure", 3)

    fast_losses = df[(df["loss_without_mfe"]) | (df["mae_below_3_fast"]) | (df["setup_failure_fast"])].copy()
    add_from(fast_losses.sort_values("net_return", ascending=True), "fast_loss_or_setup_failure", 3)

    result = pd.DataFrame(rows).head(10)
    keep = [
        "review_reason",
        "symbol",
        "name",
        "signal_date",
        "entry_date",
        "exit_date",
        "setup_key",
        "industry",
        "market_state",
        "entry_price_raw",
        "exit_price_raw",
        "net_return",
        "mfe_5d",
        "mae_5d",
        "exit_reason",
        "active_labels",
        "manual_review_buckets",
        "candidate_exit_variants",
        "manual_path_dependency_flags",
        "candidate_path_dependency_flags",
    ]
    for col in keep:
        if col not in result.columns:
            result[col] = ""
    result["kline_review_window"] = result.apply(
        lambda r: f"{r.get('signal_date')}~{r.get('exit_date')}", axis=1
    )
    return result[keep + ["kline_review_window"]]


def markdown_table(df: pd.DataFrame, columns: list[tuple[str, str]], max_rows: Optional[int] = None) -> list[str]:
    if df.empty:
        return ["No rows."]
    work = df.head(max_rows) if max_rows else df
    lines = ["| " + " | ".join(title for title, _ in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    pct_like = {
        "average_return",
        "median_return",
        "win_rate",
        "average_mfe_5d",
        "average_mae_5d",
        "scope_share",
        "all_trade_share",
        "label_share_in_dimension",
    }
    for row in work.to_dict("records"):
        cells = []
        for _title, col in columns:
            value = row.get(col)
            if (
                col in pct_like
                or col.endswith("_return")
                or col.endswith("_rate")
                or "mfe" in col.lower()
                or "mae" in col.lower()
            ):
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


def write_cluster_summary(path: Path, cluster_summary: pd.DataFrame) -> None:
    lines = [
        "# Cluster Summary",
        "",
        "本文件基于自动标签布尔矩阵进行简单 KMeans 聚类。聚类只用于归因和样本分组，不用于训练模型或生成交易信号。",
        "",
    ]
    lines.extend(
        markdown_table(
            cluster_summary,
            [
                ("cluster", "cluster_id"),
                ("交易数", "trade_count"),
                ("平均收益", "average_return"),
                ("中位数", "median_return"),
                ("胜率", "win_rate"),
                ("平均 MFE", "average_mfe_5d"),
                ("平均 MAE", "average_mae_5d"),
                ("主要标签", "main_labels"),
                ("结论", "disposition"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "解释边界：聚类结果是归因视角，不是策略选择器。任何 `continue_shadow_observation` 都只代表值得继续观察，不代表可以并入主策略。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _top_label_text(label_summary: pd.DataFrame, n: int = 5) -> str:
    if label_summary.empty:
        return "-"
    parts = []
    for row in label_summary.head(n).to_dict("records"):
        parts.append(f"{row['label']}({int(row['trade_count'])} 笔, 中位 {_fmt_pct(row['median_return'])})")
    return "；".join(parts)


def write_report(
    path: Path,
    labeled: pd.DataFrame,
    failure_summary: pd.DataFrame,
    winner_summary: pd.DataFrame,
    dangerous: pd.DataFrame,
    promising: pd.DataFrame,
    cluster_summary: pd.DataFrame,
    top10: pd.DataFrame,
) -> None:
    total = len(labeled)
    winners = int((labeled["net_return"] > 0).sum())
    losers = int((labeled["net_return"] < 0).sum())
    avg_return = labeled["net_return"].mean()
    median_return = labeled["net_return"].median()
    win_rate = winners / total if total else math.nan
    positive_promising = promising[
        (promising.get("trade_count", pd.Series(dtype=float)) >= 10)
        & (promising.get("average_return", pd.Series(dtype=float)) > 0)
        & (promising.get("median_return", pd.Series(dtype=float)) > 0)
    ]
    robust_positive = not positive_promising[
        (positive_promising["trade_count"] >= 20)
        & (~positive_promising["label_combo"].str.contains("path_dependent_exit", na=False))
    ].empty

    vol = labeled[labeled["setup_key"] == "volatility_contraction_breakout_v1"]
    pullback = labeled[labeled["setup_key"] == "pullback_reclaim_v1"]
    vol_fail = failure_summary[failure_summary["top_setup"] == "volatility_contraction_breakout_v1"].head(5)
    pullback_fail = failure_summary[failure_summary["top_setup"] == "pullback_reclaim_v1"].head(5)

    lines = [
        "# Automated Setup Review Report",
        "",
        "本报告暂停人工逐条 K 线复盘，改用机器标签、聚合统计和失败画像。它不修改主策略、不新增 setup、不调参、不连接实盘。",
        "",
        "## A. 自动化复盘确认的事实",
        "",
        f"- 覆盖 focused setup 交易 {total} 笔：`volatility_contraction_breakout_v1` {len(vol)} 笔，`pullback_reclaim_v1` {len(pullback)} 笔。",
        f"- 总体胜率 {_fmt_pct(win_rate)}，平均单笔 {_fmt_pct(avg_return)}，中位数 {_fmt_pct(median_return)}。",
        f"- 盈利交易 {winners} 笔，亏损交易 {losers} 笔。自动标签平均每笔 {labeled['active_label_count'].mean():.2f} 个。",
        "- 输出包含标签明细、失败/成功标签摘要、危险/有希望标签组合、聚类画像和最多 10 条人工复核样本。",
        "",
        "失败标签频率 Top 10：",
        "",
    ]
    lines.extend(
        markdown_table(
            failure_summary.head(10),
            [
                ("标签", "label"),
                ("失败样本数", "trade_count"),
                ("失败内占比", "scope_share"),
                ("全样本数", "all_trade_count"),
                ("平均收益", "average_return"),
                ("中位收益", "median_return"),
                ("胜率", "win_rate"),
                ("平均 MFE", "average_mfe_5d"),
                ("平均 MAE", "average_mae_5d"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## B. 主要失败模式",
            "",
            f"- 主要失败共性：{_top_label_text(failure_summary, 6)}。",
            "- 失败模式集中在三类：浮盈回吐、次日/前两日无法延续、板块或市场转弱。",
            "- 当标签组合同时包含 `profit_not_protected`、`mfe_above_2_then_loss`、`path_dependent_exit` 时，日线级别无法证明退出改善真实可成交。",
            "",
            "最危险标签组合 Top 10：",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            dangerous.head(10),
            [
                ("标签组合", "label_combo"),
                ("交易数", "trade_count"),
                ("平均收益", "average_return"),
                ("中位收益", "median_return"),
                ("胜率", "win_rate"),
                ("主要 setup", "setup_mix"),
                ("主要年份", "top_years"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "volatility breakout 失败画像：",
            "",
            f"- 样本 {len(vol)} 笔，平均 {_fmt_pct(vol['net_return'].mean())}，中位 {_fmt_pct(vol['net_return'].median())}。",
            f"- 高频失败标签：{_top_label_text(vol_fail, 5)}。",
            "- 自动标签显示其主要问题不是跌停风险，而是假突破、突破后收盘弱、浮盈回吐和环境退出。",
            "",
            "pullback reclaim 失败画像：",
            "",
            f"- 样本 {len(pullback)} 笔，平均 {_fmt_pct(pullback['net_return'].mean())}，中位 {_fmt_pct(pullback['net_return'].median())}。",
            f"- 高频失败标签：{_top_label_text(pullback_fail, 5)}。",
            "- 自动标签显示其主要问题是回踩后未真正转强、次日无法延续、回踩过深或离 MA20 过远。",
            "",
            "## C. 仍有观察价值的模式",
            "",
        ]
    )
    if robust_positive:
        lines.append("- 存在少数非路径依赖标签组合在当前样本中平均和中位收益均为正，但仍需检查样本量和年份集中。")
    else:
        lines.append("- 没有发现足够稳健的非路径依赖正期望标签组合。当前 setup 信号仍不足，建议暂停继续策略扩展。")
    lines.extend(
        [
            "",
            "相对有希望的标签组合 Top 10：",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            promising.head(10),
            [
                ("标签组合", "label_combo"),
                ("交易数", "trade_count"),
                ("平均收益", "average_return"),
                ("中位收益", "median_return"),
                ("胜率", "win_rate"),
                ("主要 setup", "setup_mix"),
                ("主要行业", "top_industries"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "成功标签频率 Top 10：",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            winner_summary.head(10),
            [
                ("标签", "label"),
                ("成功样本数", "trade_count"),
                ("成功内占比", "scope_share"),
                ("全样本数", "all_trade_count"),
                ("平均收益", "average_return"),
                ("中位收益", "median_return"),
                ("胜率", "win_rate"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## D. 应归档的模式",
            "",
            "- 继续基于日线 high/low 优化浮盈保护应归档，除非先取得分钟数据验证。",
            "- 带 `loss_without_mfe`、`setup_failure_fast`、`mae_below_3_fast` 的结构应优先归档或只作负样本研究。",
            "- `panic_repair_v1` 和 `setup_combined_v1` 已经暂停，本模块不再扩大样本或新增变体。",
            "",
            "## E. 是否需要人工复盘",
            "",
            "- 不再建议人工逐条查看大量 K 线。",
            f"- 仅保留 {len(top10)} 条最高信息密度样本，位于 `top_10_manual_review_only.csv`。",
            "- 人工复盘的目标不是找新规则，而是验证自动标签是否误判、确认典型路径是否合理。",
            "",
            "人工复核样本：",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            top10,
            [
                ("原因", "review_reason"),
                ("代码", "symbol"),
                ("名称", "name"),
                ("setup", "setup_key"),
                ("入场", "entry_date"),
                ("收益", "net_return"),
                ("MFE", "mfe_5d"),
                ("MAE", "mae_5d"),
                ("退出", "exit_reason"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## F. 是否需要分钟数据",
            "",
            "- 需要。凡是 `partial_take_profit_3pct`、`profit_protect_*`、`no_loss_after_2pct_mfe`、同日止盈/失败点先后顺序，均必须分钟数据验证。",
            "- 如果没有分钟数据，依赖日线 high/low 的 exit 规则必须停止研究。",
            "- `fast_fail_exit_day2` 更接近收盘确认规则，路径依赖较少，但仍需要验证次日退出是否存在跳空和流动性风险。",
            "",
            "## G. 下一步建议",
            "",
            "- 暂停继续策略扩展，不再增加 setup、因子或 exit 规则。",
            "- 把自动标签作为每周复盘固定产物，让 Hermes 读取聚合表而不是人工样本长列表。",
            "- 若能获取分钟数据，只验证两个保留 shadow candidate 的路径真实性；验证失败则终止 setup exit 研究。",
            "- 当前 setup 信号仍不足，建议暂停继续策略扩展。",
            "",
            "## Cluster Snapshot",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            cluster_summary,
            [
                ("cluster", "cluster_id"),
                ("交易数", "trade_count"),
                ("平均收益", "average_return"),
                ("中位数", "median_return"),
                ("胜率", "win_rate"),
                ("主要标签", "main_labels"),
                ("结论", "disposition"),
            ],
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(setup_dir: Path, exit_dir: Path, data_dir: Path, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs = load_inputs(setup_dir, exit_dir, data_dir)
    trades = attach_exit_and_priority_context(inputs["trades"], inputs["manual"], inputs["exit_trades"])
    labeled = build_auto_labels(trades)

    failure_summary = summarise_labels(labeled, labeled["net_return"] < 0, "losers").head(20)
    winner_summary = summarise_labels(labeled, labeled["net_return"] > 0, "winners").head(20)
    dangerous, promising = label_combinations(labeled)

    by_setup = label_contribution_by(labeled, "setup_key")
    by_industry = label_contribution_by(labeled, "industry")
    by_year = label_contribution_by(labeled, "year")
    by_market = label_contribution_by(labeled, "market_state")

    clustered, cluster_summary = cluster_profiles(labeled, k=6)
    labeled["cluster_id"] = clustered["cluster_id"].values
    top10 = build_top_10_review(labeled)

    label_cols = [
        "strategy_name",
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
        "holding_days",
        "candidate_exit_variants",
        "manual_review_buckets",
        "manual_path_dependency_flags",
        "candidate_path_dependency_flags",
        "active_label_count",
        "active_labels",
        "cluster_id",
    ] + LABELS
    for col in label_cols:
        if col not in labeled.columns:
            labeled[col] = ""

    outputs = {
        "report": output_dir / "automated_setup_review_report.md",
        "labels": output_dir / "trade_auto_labels.csv",
        "failure": output_dir / "failure_label_summary.csv",
        "winners": output_dir / "winner_label_summary.csv",
        "dangerous": output_dir / "dangerous_label_combinations.csv",
        "promising": output_dir / "promising_label_combinations.csv",
        "clusters": output_dir / "clustered_trade_profiles.csv",
        "cluster_summary": output_dir / "cluster_summary.md",
        "top10": output_dir / "top_10_manual_review_only.csv",
        "by_setup": output_dir / "label_contribution_by_setup.csv",
        "by_industry": output_dir / "label_contribution_by_industry.csv",
        "by_year": output_dir / "label_contribution_by_year.csv",
        "by_market": output_dir / "label_contribution_by_market_state.csv",
    }

    _write_csv(outputs["labels"], labeled[label_cols])
    _write_csv(outputs["failure"], failure_summary)
    _write_csv(outputs["winners"], winner_summary)
    _write_csv(outputs["dangerous"], dangerous)
    _write_csv(outputs["promising"], promising)
    _write_csv(outputs["clusters"], cluster_summary)
    _write_csv(outputs["top10"], top10)
    _write_csv(outputs["by_setup"], by_setup)
    _write_csv(outputs["by_industry"], by_industry)
    _write_csv(outputs["by_year"], by_year)
    _write_csv(outputs["by_market"], by_market)
    write_cluster_summary(outputs["cluster_summary"], cluster_summary)
    write_report(outputs["report"], labeled, failure_summary, winner_summary, dangerous, promising, cluster_summary, top10)
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run automated setup review diagnostics.")
    parser.add_argument("--setup-dir", type=Path, default=SETUP_DIR)
    parser.add_argument("--exit-dir", type=Path, default=EXIT_DIR)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outputs = run(args.setup_dir, args.exit_dir, args.data_dir, args.output_dir)
    print("Automated setup review complete.")
    for key, path in outputs.items():
        print(f"{key}: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
