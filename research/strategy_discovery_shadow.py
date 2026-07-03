#!/usr/bin/env python3
"""Strategy discovery shadow experiments.

The module translates public/open-source strategy ideas into simple, auditable
shadow variants. It does not change the active market scanner strategy, ledgers,
or live-trading configuration.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in [ROOT / "src", ROOT / "scripts"]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import diagnose_alpha040_shadow_factor as alpha_shadow
import factor_research as fr
import factor_research_round3 as r3
import factor_research_round4 as r4
import factor_research_round5 as r5
import market_scanner as scanner

DATA_DIR = ROOT / "data" / "expanded"
DEFAULT_CACHE_DIR = DATA_DIR / "daily_kline"
DEFAULT_OUTPUT_DIR = ROOT / "output" / "strategy_discovery"
RISK_ON_LABEL = "积极"
EXPECTED_EXIT_REASONS = [
    "take_profit",
    "stop_loss",
    "timeout",
    "time_stop",
    "environment_exit",
    "limit_down_blocked_exit",
    "same_day_stop_take_conservative",
]


@dataclass(frozen=True)
class DiscoveryStrategy:
    key: str
    label: str
    family: str
    rank_mode: str
    extra_filter: str = "none"
    source_note: str = ""


DISCOVERY_STRATEGIES = [
    DiscoveryStrategy(
        key="control_v3_alpha040",
        label="Control：当前 V3 alpha040",
        family="control",
        rank_mode="alpha040_original",
        source_note="当前主策略基准，仅用于比较。",
    ),
    DiscoveryStrategy(
        key="control_filter_only",
        label="Control：V3 风控过滤后不排序",
        family="control",
        rank_mode="filter_only",
        source_note="上一轮 alpha040 诊断中表现最好的对照口径。",
    ),
    DiscoveryStrategy(
        key="sd_low_vol_momentum",
        label="低波动动量",
        family="low_volatility_momentum",
        rank_mode="low_vol_momentum",
        source_note="结合横截面动量、低波动和市场环境过滤。",
    ),
    DiscoveryStrategy(
        key="sd_long_high_anchor",
        label="长周期高点锚定 + 行业强度",
        family="52_week_high_anchor",
        rank_mode="long_high_anchor",
        extra_filter="long_high_anchor",
        source_note="52周高点/行业高点锚定思想的 A 股 shadow 化。",
    ),
    DiscoveryStrategy(
        key="sd_near_high_low_vol",
        label="近 20 日高点 + 低波动",
        family="breakout_low_noise",
        rank_mode="near_high_low_vol",
        extra_filter="near_20d_high",
        source_note="突破/贴近高点，但降低高波动追涨风险。",
    ),
    DiscoveryStrategy(
        key="sd_pullback_strength",
        label="强势股温和回调",
        family="pullback_to_strength",
        rank_mode="pullback_strength",
        extra_filter="moderate_pullback",
        source_note="强势趋势中等待回调，不直接追极端高点。",
    ),
    DiscoveryStrategy(
        key="sd_industry_relative_strength",
        label="行业相对强度",
        family="industry_momentum",
        rank_mode="industry_relative_strength",
        source_note="行业动量与个股相对强度结合。",
    ),
    DiscoveryStrategy(
        key="sd_trend_quality_low_noise",
        label="趋势质量 + 低噪音",
        family="trend_quality",
        rank_mode="trend_quality",
        extra_filter="ma_alignment",
        source_note="均线多头、低波动、低振幅、低上影线。",
    ),
    DiscoveryStrategy(
        key="sd_risk_avoidance_filter",
        label="风险规避过滤",
        family="risk_avoidance",
        rank_mode="filter_only",
        extra_filter="risk_avoidance",
        source_note="先过滤高振幅/长上影线，检验“少犯错”是否优于排序。",
    ),
    DiscoveryStrategy(
        key="sd_volume_confirmed_breakout",
        label="温和放量突破",
        family="volume_confirmed_breakout",
        rank_mode="volume_confirmed_breakout",
        extra_filter="volume_confirmed_breakout",
        source_note="贴近高点、温和放量、上影线受控。",
    ),
]

SOURCE_REFERENCES = [
    {
        "name": "qstock",
        "url": "https://github.com/tkfy920/qstock",
        "use": "A 股开源投研库，启发 RPS、MM 趋势、选股和回测模块化方向。",
    },
    {
        "name": "QuantsPlaybook",
        "url": "https://github.com/hugo2046/QuantsPlaybook",
        "use": "A 股券商金工研报复现库，启发策略族先复现、再审计、再 shadow 的流程。",
    },
    {
        "name": "George and Hwang 52-week high",
        "url": "https://www.bauer.uh.edu/tgeorge/papers/gh4-paper.pdf",
        "use": "52 周高点锚定/动量思想；本轮只做长周期高点距离 shadow 改写。",
    },
    {
        "name": "Quantpedia 52-week high effect",
        "url": "https://quantpedia.com/strategies/52-weeks-high-effect-in-stocks",
        "use": "行业高点信息与 52 周高点效应的关系；启发行业相对强度候选。",
    },
    {
        "name": "Market Volatility, Momentum, and Reversal",
        "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4478316",
        "use": "高波动下动量容易失效；启发低波动与风险规避候选。",
    },
    {
        "name": "Momentum-Investing",
        "url": "https://github.com/tanish35/Momentum-Investing",
        "use": "开源长多动量框架，包含市场环境、横截面/时间序列动量和反波动仓位思想。",
    },
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
        json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False, default=fr._json_default)


def _load_risk_thresholds() -> dict[str, Optional[float]]:
    candidates = [
        ROOT / "output" / "expanded_backtest_v3_2021" / "round5_thresholds.json",
        ROOT / "output" / "factor_research_round6" / "round5_thresholds.json",
        ROOT / "output" / "factor_research_round5" / "stop_risk_filter_thresholds.json",
    ]
    raw: dict[str, Any] = {}
    for path in candidates:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            break
    return {
        "amplitude_q75": _safe_float(raw.get("amplitude_q75")),
        "upper_shadow_ratio_q75": _safe_float(raw.get("upper_shadow_ratio_q75")),
        "volume_ratio_high": 2.5,
        "volume_ratio_low": 0.8,
    }


def _history_window_high(item: dict, histories: dict[str, list[dict]], window: int) -> Optional[float]:
    symbol = str(item.get("_symbol_key") or item.get("symbol") or "")
    idx = int(item.get("_signal_idx") or -1)
    bars = histories.get(symbol) or []
    if idx < 0 or idx >= len(bars):
        return None
    start = max(0, idx - window + 1)
    highs = [_safe_float(bar.get("high")) for bar in bars[start : idx + 1]]
    highs = [value for value in highs if value is not None and value > 0]
    if len(highs) < max(20, min(window, 120) // 2):
        return None
    return max(highs)


def _with_history_context(item: dict, histories: dict[str, list[dict]]) -> dict:
    row = dict(item)
    latest = _safe_float(row.get("latest") or row.get("close"))
    for window in [60, 120, 252]:
        high = _history_window_high(row, histories, window)
        row[f"high_{window}d"] = high
        row[f"close_to_{window}d_high"] = latest / high - 1 if latest is not None and high else None
    row["long_high_anchor"] = (
        row.get("close_to_252d_high")
        if _safe_float(row.get("close_to_252d_high")) is not None
        else row.get("close_to_120d_high")
        if _safe_float(row.get("close_to_120d_high")) is not None
        else row.get("close_to_60d_high")
    )
    ma20 = _safe_float(row.get("ma20"))
    row["ma20_distance"] = latest / ma20 - 1 if latest is not None and ma20 else None
    return row


def _base_passes(item: dict, market_profile: dict[str, Any], thresholds: dict[str, float]) -> tuple[bool, str]:
    if market_profile.get("regime_label") != RISK_ON_LABEL:
        return False, "market_not_risk_on"
    latest = _safe_float(item.get("latest") or item.get("close"))
    ma20 = _safe_float(item.get("ma20"))
    if latest is None or ma20 is None or latest < ma20:
        return False, "below_ma20_or_missing_trend"
    change_5d = _safe_float(item.get("change_rate_5d"))
    if change_5d is None:
        return False, "missing_change_rate_5d"
    if change_5d > thresholds["change_rate_5d_q75"]:
        return False, "hot_5d_filtered"
    volatility = _safe_float(item.get("volatility_20d"))
    if volatility is None:
        return False, "missing_volatility_20d"
    if volatility > thresholds["volatility_20d_q75"]:
        return False, "high_volatility_filtered"
    return True, "passed"


def _extra_passes(item: dict, strategy: DiscoveryStrategy, risk_thresholds: dict[str, Optional[float]]) -> tuple[bool, str]:
    high20 = _safe_float(item.get("close_to_20d_high"))
    long_anchor = _safe_float(item.get("long_high_anchor"))
    ma5 = _safe_float(item.get("ma5"))
    ma10 = _safe_float(item.get("ma10"))
    ma20 = _safe_float(item.get("ma20"))
    ma20_distance = _safe_float(item.get("ma20_distance"))
    amplitude = _safe_float(item.get("amplitude"))
    upper = _safe_float(item.get("upper_shadow_ratio"))
    volume_ratio = _safe_float(item.get("volume_ratio"))

    if strategy.extra_filter == "none":
        return True, "passed"
    if strategy.extra_filter == "long_high_anchor":
        if long_anchor is None:
            return False, "missing_long_high_anchor"
        if long_anchor < -0.10:
            return False, "far_from_long_high"
    elif strategy.extra_filter == "near_20d_high":
        if high20 is None:
            return False, "missing_close_to_20d_high"
        if high20 < -0.035:
            return False, "far_from_20d_high"
    elif strategy.extra_filter == "moderate_pullback":
        if high20 is None:
            return False, "missing_close_to_20d_high"
        if not (-0.10 <= high20 <= -0.005):
            return False, "not_moderate_pullback"
    elif strategy.extra_filter == "ma_alignment":
        if ma5 is None or ma10 is None or ma20 is None or not (ma5 >= ma10 >= ma20):
            return False, "ma_not_aligned"
        if ma20_distance is not None and ma20_distance > 0.12:
            return False, "too_far_above_ma20"
    elif strategy.extra_filter == "risk_avoidance":
        amp_threshold = risk_thresholds.get("amplitude_q75")
        upper_threshold = risk_thresholds.get("upper_shadow_ratio_q75")
        if amp_threshold is not None and amplitude is not None and amplitude > amp_threshold:
            return False, "high_amplitude_filtered"
        if upper_threshold is not None and upper is not None and upper > upper_threshold:
            return False, "high_upper_shadow_filtered"
    elif strategy.extra_filter == "volume_confirmed_breakout":
        upper_threshold = risk_thresholds.get("upper_shadow_ratio_q75")
        if high20 is None or high20 < -0.03:
            return False, "far_from_20d_high"
        if volume_ratio is None or volume_ratio < (risk_thresholds.get("volume_ratio_low") or 0.8):
            return False, "volume_not_confirmed"
        if volume_ratio > (risk_thresholds.get("volume_ratio_high") or 2.5):
            return False, "volume_too_hot"
        if upper_threshold is not None and upper is not None and upper > upper_threshold:
            return False, "high_upper_shadow_filtered"
    return True, "passed"


def _zscore(values: list[Optional[float]]) -> list[Optional[float]]:
    return r3._standardize(values)


def _score_rows(rows: list[dict], strategy: DiscoveryStrategy) -> list[dict]:
    if not rows:
        return []
    if strategy.rank_mode == "filter_only":
        return [{**row, "alpha040_core_score": 0.0, "rank_score": 0.0} for row in rows]

    metric_cols = [
        "rps60",
        "rps20",
        "close_to_20d_high",
        "long_high_anchor",
        "volatility_20d",
        "amplitude",
        "upper_shadow_ratio",
        "volume_ratio",
        "ma20_distance",
        "change_rate_20d",
    ]
    z: dict[str, list[Optional[float]]] = {
        col: _zscore([_safe_float(row.get(col)) for row in rows]) for col in metric_cols
    }
    industry_mean: dict[str, float] = {}
    buckets: dict[str, list[float]] = {}
    for row in rows:
        industry = str(row.get("industry") or row.get("sector") or "行业缺失")
        rps = _safe_float(row.get("rps60"))
        if rps is not None:
            buckets.setdefault(industry, []).append(rps)
    for industry, values in buckets.items():
        if values:
            industry_mean[industry] = sum(values) / len(values)
    industry_values = [industry_mean.get(str(row.get("industry") or row.get("sector") or "行业缺失")) for row in rows]
    industry_z = _zscore(industry_values)
    pullback_target = []
    volume_confirm = []
    for row in rows:
        high20 = _safe_float(row.get("close_to_20d_high"))
        volume_ratio = _safe_float(row.get("volume_ratio"))
        pullback_target.append(-abs((high20 if high20 is not None else -0.04) + 0.04))
        volume_confirm.append(-abs((volume_ratio if volume_ratio is not None else 1.2) - 1.35))
    pullback_z = _zscore(pullback_target)
    volume_confirm_z = _zscore(volume_confirm)

    scored: list[dict] = []
    for idx, row in enumerate(rows):
        rps60_z = z["rps60"][idx]
        rps20_z = z["rps20"][idx]
        high20_z = z["close_to_20d_high"][idx]
        long_high_z = z["long_high_anchor"][idx]
        low_vol_z = -z["volatility_20d"][idx] if z["volatility_20d"][idx] is not None else None
        low_amp_z = -z["amplitude"][idx] if z["amplitude"][idx] is not None else None
        low_upper_z = -z["upper_shadow_ratio"][idx] if z["upper_shadow_ratio"][idx] is not None else None
        ma_distance_z = -abs(z["ma20_distance"][idx] or 0.0)
        score: Optional[float]
        if strategy.rank_mode == "alpha040_original":
            score = r3._alpha040_core_score(row)
        elif strategy.rank_mode == "low_vol_momentum":
            score = _weighted_sum([(0.45, rps60_z), (0.25, high20_z), (0.30, low_vol_z)])
        elif strategy.rank_mode == "long_high_anchor":
            score = _weighted_sum([(0.45, long_high_z), (0.30, industry_z[idx]), (0.25, low_vol_z)])
        elif strategy.rank_mode == "near_high_low_vol":
            score = _weighted_sum([(0.45, high20_z), (0.30, rps60_z), (0.25, low_vol_z)])
        elif strategy.rank_mode == "pullback_strength":
            score = _weighted_sum([(0.40, rps60_z), (0.25, pullback_z[idx]), (0.20, low_vol_z), (0.15, low_amp_z)])
        elif strategy.rank_mode == "industry_relative_strength":
            score = _weighted_sum([(0.45, industry_z[idx]), (0.35, rps60_z), (0.20, high20_z)])
        elif strategy.rank_mode == "trend_quality":
            score = _weighted_sum([(0.30, rps60_z), (0.20, rps20_z), (0.20, low_vol_z), (0.15, low_amp_z), (0.10, low_upper_z), (0.05, ma_distance_z)])
        elif strategy.rank_mode == "volume_confirmed_breakout":
            score = _weighted_sum([(0.35, high20_z), (0.25, rps20_z), (0.20, volume_confirm_z[idx]), (0.20, low_upper_z)])
        else:
            score = None
        if score is None:
            continue
        scored.append(
            {
                **row,
                "industry_rps60_mean": industry_mean.get(str(row.get("industry") or row.get("sector") or "行业缺失")),
                "industry_rps60_z": industry_z[idx],
                "alpha040_core_score": round(score, 6),
                "rank_score": round(score, 6),
            }
        )
    return scored


def _weighted_sum(parts: list[tuple[float, Optional[float]]]) -> Optional[float]:
    values = [(weight, value) for weight, value in parts if value is not None]
    if not values:
        return None
    weight_sum = sum(weight for weight, _value in values)
    if not weight_sum:
        return None
    return float(sum(weight * value for weight, value in values) / weight_sum)


def _build_candidates(
    universe_by_date: dict[str, list[dict]],
    histories: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    thresholds: dict[str, float],
    risk_thresholds: dict[str, Optional[float]],
) -> tuple[pd.DataFrame, dict[str, dict[str, list[dict]]]]:
    candidate_rows: list[dict[str, Any]] = []
    candidates_by_strategy: dict[str, dict[str, list[dict]]] = {strategy.key: {} for strategy in DISCOVERY_STRATEGIES}
    for date in sorted(universe_by_date):
        market_profile = market_timeline.get(date) or {}
        candidate_limit = int(market_profile.get("candidate_limit") or 5)
        row: dict[str, Any] = {
            "date": date,
            "market_regime": market_profile.get("regime"),
            "market_regime_label": market_profile.get("regime_label"),
            "candidate_limit": candidate_limit,
            "universe_count": len(universe_by_date[date]),
        }
        enriched_base = []
        base_reasons: Counter[str] = Counter()
        for item in universe_by_date[date]:
            enriched = _with_history_context(item, histories)
            passed, reason = _base_passes(enriched, market_profile, thresholds)
            base_reasons[reason] += 1
            if passed:
                enriched_base.append(enriched)
        row["base_pass_count"] = len(enriched_base)
        row["base_filter_reasons_json"] = json.dumps(dict(base_reasons), ensure_ascii=False)
        for strategy in DISCOVERY_STRATEGIES:
            extra_reasons: Counter[str] = Counter()
            extra_rows = []
            for item in enriched_base:
                passed, reason = _extra_passes(item, strategy, risk_thresholds)
                extra_reasons[reason] += 1
                if passed:
                    extra_rows.append(item)
            scored = _score_rows(extra_rows, strategy)
            if strategy.rank_mode == "filter_only":
                scored.sort(key=lambda item: (str(item.get("_symbol_key") or item.get("symbol") or ""), str(item.get("name") or "")))
            else:
                scored.sort(
                    key=lambda item: (
                        _safe_float(item.get("rank_score")) if _safe_float(item.get("rank_score")) is not None else -999.0,
                        str(item.get("_symbol_key") or item.get("symbol") or ""),
                    ),
                    reverse=True,
                )
            candidates_by_strategy[strategy.key][date] = scored
            row[f"{strategy.key}_candidate_count"] = len(scored)
            row[f"{strategy.key}_selected_count"] = min(len(scored), candidate_limit)
            row[f"{strategy.key}_filter_reasons_json"] = json.dumps(dict(extra_reasons), ensure_ascii=False)
        candidate_rows.append(row)
    return pd.DataFrame(candidate_rows), candidates_by_strategy


def _run_events(
    strategy: DiscoveryStrategy,
    candidates_by_date: dict[str, list[dict]],
    histories: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    train_end: pd.Timestamp,
    max_hold_days: int,
    entry_window_days: int,
    fee_bps: float,
    slippage_bps: float,
    limit_threshold: float,
) -> pd.DataFrame:
    trades: list[dict[str, Any]] = []
    next_available_by_symbol: dict[str, str] = {}
    for date in sorted(candidates_by_date):
        market_profile = market_timeline.get(date) or {}
        candidate_limit = int(market_profile.get("candidate_limit") or 5)
        for item in candidates_by_date[date][:candidate_limit]:
            symbol = str(item.get("_symbol_key") or item.get("symbol") or "")
            if not symbol or next_available_by_symbol.get(symbol, "") >= date:
                continue
            trade = r4._simulate_trade_variant(
                item,
                histories[symbol],
                int(item["_signal_idx"]),
                market_timeline,
                max_hold_days,
                entry_window_days,
                fee_bps,
                slippage_bps,
                limit_threshold,
                "atr_stop_risk_budget",
            )
            if not trade:
                continue
            trade["strategy_key"] = strategy.key
            trade["strategy_label"] = strategy.label
            trade["strategy_family"] = strategy.family
            trade["rank_mode"] = strategy.rank_mode
            trade["split"] = "train" if pd.to_datetime(trade["signal_date"]) <= train_end else "test"
            trades.append(trade)
            if trade.get("exit_date"):
                next_available_by_symbol[symbol] = str(trade["exit_date"])
    trades_df = pd.DataFrame(trades)
    return r5._enrich_trades(trades_df, {date: candidates_by_date[date] for date in candidates_by_date}, histories, limit_threshold)


def _run_portfolio(
    strategy: DiscoveryStrategy,
    trades: pd.DataFrame,
    histories: dict[str, list[dict]],
    active_dates: list[str],
    initial_cash: float,
    output_dir: Path,
) -> dict[str, Any]:
    portfolio = r4._accept_portfolio_trades(trades, initial_cash)
    equity, monthly, metrics = r4._portfolio_equity_curve(portfolio, histories, active_dates, initial_cash)
    accepted = portfolio[portfolio["portfolio_action"] == "accepted"].copy() if not portfolio.empty and "portfolio_action" in portfolio else pd.DataFrame()
    prefix = f"{strategy.key}_"
    _write_csv(output_dir / f"{prefix}trades.csv", trades)
    _write_csv(output_dir / f"{prefix}portfolio_trades.csv", portfolio)
    _write_csv(output_dir / f"{prefix}accepted_trades.csv", accepted)
    _write_csv(output_dir / f"{prefix}daily_equity.csv", equity)
    _write_csv(output_dir / f"{prefix}monthly_returns.csv", monthly)
    return {"strategy": strategy, "trades": trades, "portfolio": portfolio, "accepted": accepted, "equity": equity, "monthly": monthly, "metrics": metrics}


def _max_consecutive_losses(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    ordered = frame.copy()
    sort_cols = [col for col in ["exit_date", "entry_date", "signal_date"] if col in ordered.columns]
    if sort_cols:
        ordered = ordered.sort_values(sort_cols)
    current = 0
    max_run = 0
    for value in pd.to_numeric(ordered.get("net_return"), errors="coerce").fillna(0):
        if value < 0:
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    return int(max_run)


def _monthly_stats(monthly: pd.DataFrame) -> dict[str, Any]:
    if monthly.empty:
        return {"positive_month_rate": None, "worst_month": None, "worst_month_return": None, "best_month": None, "best_month_return": None}
    work = monthly.copy()
    work["monthly_return"] = pd.to_numeric(work["monthly_return"], errors="coerce")
    work = work.dropna(subset=["monthly_return"])
    if work.empty:
        return {"positive_month_rate": None, "worst_month": None, "worst_month_return": None, "best_month": None, "best_month_return": None}
    worst = work.sort_values("monthly_return").iloc[0].to_dict()
    best = work.sort_values("monthly_return", ascending=False).iloc[0].to_dict()
    return {
        "positive_month_rate": float((work["monthly_return"] > 0).mean()),
        "worst_month": str(worst["month"]),
        "worst_month_return": float(worst["monthly_return"]),
        "best_month": str(best["month"]),
        "best_month_return": float(best["monthly_return"]),
    }


def _yearly_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in DISCOVERY_STRATEGIES:
        result = results[strategy.key]
        accepted = result["accepted"].copy()
        equity = result["equity"].copy()
        if not accepted.empty:
            accepted["entry_year"] = pd.to_datetime(accepted["entry_date"], errors="coerce").dt.year
        if not equity.empty:
            equity["date_dt"] = pd.to_datetime(equity["date"], errors="coerce")
        for year in [2021, 2022, 2023, 2024, 2025, 2026]:
            eq = equity[equity["date_dt"].dt.year == year].copy() if not equity.empty else pd.DataFrame()
            trades = accepted[accepted["entry_year"] == year].copy() if not accepted.empty else pd.DataFrame()
            if eq.empty:
                continue
            start = float(eq["equity"].iloc[0])
            end = float(eq["equity"].iloc[-1])
            peak = eq["equity"].cummax()
            dd = eq["equity"] / peak - 1
            returns = pd.to_numeric(trades.get("net_return"), errors="coerce").dropna() if not trades.empty else pd.Series(dtype=float)
            rows.append(
                {
                    "strategy_key": strategy.key,
                    "strategy_label": strategy.label,
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


def _summary_rows(results: dict[str, dict[str, Any]], yearly: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in DISCOVERY_STRATEGIES:
        result = results[strategy.key]
        accepted = result["accepted"]
        metrics = result["metrics"]
        returns = pd.to_numeric(accepted.get("net_return"), errors="coerce").dropna() if not accepted.empty else pd.Series(dtype=float)
        monthly = _monthly_stats(result["monthly"])
        yr = yearly[yearly["strategy_key"] == strategy.key].copy()
        yr["return"] = pd.to_numeric(yr["return"], errors="coerce")
        worst_year = yr.dropna(subset=["return"]).sort_values("return").head(1).to_dict("records")
        rows.append(
            {
                "strategy_key": strategy.key,
                "strategy_label": strategy.label,
                "family": strategy.family,
                "rank_mode": strategy.rank_mode,
                "event_trade_count": int(len(result["trades"])),
                "accepted_trade_count": int(metrics.get("accepted_trades") or 0),
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
                "environment_exit_count": int((accepted.get("exit_reason") == "environment_exit").sum()) if not accepted.empty else 0,
                "positive_year_rate": float((yr["return"] > 0).mean()) if not yr.empty else None,
                "positive_month_rate": monthly.get("positive_month_rate"),
                "worst_year": worst_year[0]["year"] if worst_year else None,
                "worst_year_return": worst_year[0]["return"] if worst_year else None,
                "worst_month": monthly.get("worst_month"),
                "worst_month_return": monthly.get("worst_month_return"),
                "best_month": monthly.get("best_month"),
                "best_month_return": monthly.get("best_month_return"),
                "source_note": strategy.source_note,
            }
        )
    return pd.DataFrame(rows)


def _monthly_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in DISCOVERY_STRATEGIES:
        for row in results[strategy.key]["monthly"].to_dict("records"):
            rows.append({"strategy_key": strategy.key, "strategy_label": strategy.label, **row})
    return pd.DataFrame(rows)


def _industry_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in DISCOVERY_STRATEGIES:
        accepted = results[strategy.key]["accepted"].copy()
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
                    "strategy_key": strategy.key,
                    "strategy_label": strategy.label,
                    "industry": industry,
                    "trade_count": int(len(group)),
                    "trade_count_share": float(len(group) / total) if total else None,
                    "win_rate": float((returns > 0).mean()) if len(returns) else None,
                    "average_trade_return": float(returns.mean()) if len(returns) else None,
                    "median_trade_return": float(returns.median()) if len(returns) else None,
                    "stop_loss_count": int((group.get("exit_reason") == "stop_loss").sum()),
                    "limit_down_blocked_exit_count": int((group.get("exit_reason") == "limit_down_blocked_exit").sum()),
                    "cumulative_contribution": float(group["return_contribution"].sum()),
                    "industry_missing_count": int((group["industry"] == "行业缺失").sum()),
                }
            )
    return pd.DataFrame(rows)


def _exit_reason_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in DISCOVERY_STRATEGIES:
        accepted = results[strategy.key]["accepted"].copy()
        if not accepted.empty:
            accepted["entry_year"] = pd.to_datetime(accepted["entry_date"], errors="coerce").dt.year
            accepted["industry"] = accepted.get("industry", "行业缺失").fillna("行业缺失").replace("", "行业缺失")
        for reason in EXPECTED_EXIT_REASONS:
            group = accepted[accepted["exit_reason"] == reason].copy() if not accepted.empty else pd.DataFrame()
            returns = pd.to_numeric(group.get("net_return"), errors="coerce").dropna() if not group.empty else pd.Series(dtype=float)
            contribution = (
                pd.to_numeric(group.get("net_return"), errors="coerce").fillna(0)
                * pd.to_numeric(group.get("portfolio_position_pct"), errors="coerce").fillna(0)
            ).sum() if not group.empty else 0.0
            rows.append(
                {
                    "strategy_key": strategy.key,
                    "strategy_label": strategy.label,
                    "exit_reason": reason,
                    "trade_count": int(len(group)),
                    "win_rate": float((returns > 0).mean()) if len(returns) else None,
                    "average_trade_return": float(returns.mean()) if len(returns) else None,
                    "median_trade_return": float(returns.median()) if len(returns) else None,
                    "max_loss": float(returns.min()) if len(returns) else None,
                    "cumulative_contribution": float(contribution),
                    "year_distribution": json.dumps(Counter(str(int(x)) for x in group["entry_year"].dropna()).most_common(), ensure_ascii=False)
                    if not group.empty
                    else "{}",
                    "industry_distribution_top10": json.dumps(
                        dict(Counter(str(x) for x in group["industry"].dropna()).most_common(10)),
                        ensure_ascii=False,
                    )
                    if not group.empty
                    else "{}",
                }
            )
    return pd.DataFrame(rows)


def _markdown_table(df: pd.DataFrame, columns: list[tuple[str, str]], max_rows: Optional[int] = None) -> list[str]:
    work = df.head(max_rows) if max_rows else df
    lines = ["| " + " | ".join(title for title, _ in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in work.to_dict("records"):
        cells: list[str] = []
        for _title, col in columns:
            value = row.get(col)
            if (
                col.endswith("return")
                or "drawdown" in col
                or col
                in {
                    "total_return",
                    "annualized_return",
                    "win_rate",
                    "average_trade_return",
                    "median_trade_return",
                    "positive_year_rate",
                    "positive_month_rate",
                    "trade_count_share",
                    "cumulative_contribution",
                    "max_loss",
                }
            ):
                cells.append(_fmt_pct(value))
            elif isinstance(value, float):
                cells.append(_fmt_num(value))
            elif pd.isna(value):
                cells.append("-")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _render_report(
    output_dir: Path,
    summary: pd.DataFrame,
    yearly: pd.DataFrame,
    industry: pd.DataFrame,
    exit_reason: pd.DataFrame,
    thresholds: dict[str, float],
    risk_thresholds: dict[str, Optional[float]],
) -> None:
    ranked = summary.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).copy()
    best = ranked.iloc[0].to_dict()
    all_negative = bool((pd.to_numeric(summary["total_return"], errors="coerce") < 0).all())
    lines = [
        "# Strategy Discovery Shadow Report",
        "",
        "本报告把公开/开源策略思路翻译为本项目的 shadow strategy。它不修改当前主策略 `alpha040_v3_risk_controlled`，不连接实盘，不自动下单。",
        "",
        "## External Ideas Used",
        "",
    ]
    for ref in SOURCE_REFERENCES:
        lines.append(f"- [{ref['name']}]({ref['url']}): {ref['use']}")
    lines.extend(
        [
            "",
            "## Fixed Backtest Boundary",
            "",
            "- 数据：2021-01-04 至 2026-06-26 扩展日线，当前为 efinance 单源扩展数据；行业映射来自 expanded metadata/BaoStock。",
            "- 所有候选共用 V3 风控：只在积极环境开仓，ATR 止损 + 风险预算仓位。",
            f"- 固定过滤：5 日涨幅 > {_fmt_pct(thresholds['change_rate_5d_q75'])} 剔除；volatility_20d > {_fmt_pct(thresholds['volatility_20d_q75'])} 剔除；收盘价需站上 MA20。",
            f"- 风险规避候选额外参考阈值：amplitude_q75={_fmt_pct(risk_thresholds.get('amplitude_q75'))}，upper_shadow_q75={_fmt_pct(risk_thresholds.get('upper_shadow_ratio_q75'))}。",
            "",
            "## Strategy Summary",
            "",
        ]
    )
    lines.extend(
        _markdown_table(
            ranked,
            [
                ("版本", "strategy_key"),
                ("策略族", "family"),
                ("接受交易", "accepted_trade_count"),
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
                ("止损", "stop_loss_count"),
                ("跌停无法卖出", "limit_down_blocked_exit_count"),
            ],
            max_rows=80,
        )
    )
    lines.extend(["", "## Industry Concentration Snapshot", ""])
    for strategy in DISCOVERY_STRATEGIES:
        rows = industry[industry["strategy_key"] == strategy.key].copy()
        if rows.empty:
            continue
        top_count = rows.sort_values("trade_count", ascending=False).head(1).iloc[0].to_dict()
        missing = int(rows["industry_missing_count"].sum())
        lines.append(
            f"- `{strategy.key}`：第一大交易行业 {top_count['industry']}，占比 {_fmt_pct(top_count['trade_count_share'])}，行业缺失交易 {missing}。"
        )
    lines.extend(["", "## Exit Reason Snapshot", ""])
    risk_exits = exit_reason[exit_reason["exit_reason"].isin(["stop_loss", "limit_down_blocked_exit"])].copy()
    lines.extend(
        _markdown_table(
            risk_exits,
            [
                ("版本", "strategy_key"),
                ("退出原因", "exit_reason"),
                ("笔数", "trade_count"),
                ("平均单笔", "average_trade_return"),
                ("贡献", "cumulative_contribution"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## A. 已确认事实",
            "",
            f"- 本轮最好的 shadow 候选是 `{best['strategy_key']}`，累计收益 {_fmt_pct(best['total_return'])}，最大回撤 {_fmt_pct(best['max_drawdown'])}。",
            f"- 所有候选是否仍为负：{'是' if all_negative else '否'}。",
            "- 当前结果只说明 shadow 研究优先级，不说明可以进入真实交易。",
            "",
            "## B. 对策略方向的判断",
            "",
        ]
    )
    if all_negative:
        lines.append("- 如果所有候选仍为负，应优先承认当前日线短线框架收益端没有通过长样本验证，而不是继续堆参数。")
    else:
        lines.append("- 至少有候选策略在当前样本转正，可作为下一轮 freeze validation 候选，但不得直接替换主策略。")
    if str(best["strategy_key"]) in {"control_filter_only", "sd_risk_avoidance_filter"}:
        lines.append("- 最优结果仍偏向过滤/风险规避，说明“先少犯错”比复杂排序更值得优先研究。")
    lines.extend(
        [
            "",
            "## C. 不能立即做的事",
            "",
            "- 不能把本轮最优 shadow 直接升级为主策略。",
            "- 不能根据本轮结果继续调 V3 固定阈值。",
            "- 不能连接实盘或自动下单。",
            "- 不能把 efinance 单源扩展数据当作多源完整长期验证。",
            "",
            "## D. 下一步建议",
            "",
            "- 只挑前 2 个候选进入下一轮 freeze-style 稳定性测试：复跑、成本/滑点压力、最大持仓敏感性、forward paper trading。",
            "- 如果最优仍是 filter-only/risk-avoidance，应暂停新增因子，优先做失败交易画像和过滤条件稳定性观察。",
            "- 若出现正收益候选，也要先做样本外记录 30-50 笔，不进入真实交易流程。",
            "",
            "## Output Files",
            "",
            "- `strategy_discovery_summary.csv`",
            "- `strategy_discovery_yearly_performance.csv`",
            "- `strategy_discovery_monthly_returns.csv`",
            "- `strategy_discovery_industry_performance.csv`",
            "- `strategy_discovery_exit_reason.csv`",
            "- `strategy_discovery_candidate_counts.csv`",
            "- `strategy_discovery_sources.md`",
        ]
    )
    (output_dir / "strategy_discovery_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    source_lines = ["# Strategy Discovery Sources", ""]
    for ref in SOURCE_REFERENCES:
        source_lines.extend([f"## {ref['name']}", "", f"- URL: {ref['url']}", f"- How used: {ref['use']}", ""])
    (output_dir / "strategy_discovery_sources.md").write_text("\n".join(source_lines), encoding="utf-8")


def run_strategy_discovery(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    strategy_path: Path = ROOT / "strategy.json",
    max_symbols: Optional[int] = None,
    min_bars: int = 80,
    min_history: int = 60,
    train_ratio: float = 0.7,
    fee_bps: float = 5.0,
    slippage_bps: float = 10.0,
    limit_threshold: float = r3.DEFAULT_LIMIT_THRESHOLD,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    scanner._set_active_config(scanner._load_scanner_config(str(strategy_path)))
    thresholds = alpha_shadow._load_v3_thresholds(strategy_path)
    risk_thresholds = _load_risk_thresholds()
    histories = fr.load_cached_histories(cache_dir=cache_dir, max_symbols=max_symbols, min_bars=min_bars)
    if not histories:
        raise RuntimeError(f"未找到可用历史 K 线缓存：{cache_dir}")
    metadata = alpha_shadow._load_expanded_metadata()
    alpha040_map = r3._build_alpha040_map(histories)
    universe_by_date, daily_universe = r3._build_dynamic_universe(
        histories,
        metadata,
        alpha040_map,
        min_history,
        output_dir / "universe_audit",
    )
    active_dates = [date for date, rows in universe_by_date.items() if rows]
    train_end, _test_start = fr._split_dates(pd.Series(pd.to_datetime(active_dates)), train_ratio=train_ratio)
    market_timeline = alpha_shadow._build_market_timeline_from_expanded()
    max_hold_days = int(scanner._cfg("backtest", "max_hold_days", 5))
    entry_window_days = int(scanner._cfg("backtest", "entry_window_days", 2))
    initial_cash = float(scanner._cfg("backtest", "portfolio_initial_cash", 1_000_000))
    candidate_counts, candidates_by_strategy = _build_candidates(
        universe_by_date,
        histories,
        market_timeline,
        thresholds,
        risk_thresholds,
    )
    _write_csv(output_dir / "strategy_discovery_candidate_counts.csv", candidate_counts)

    results: dict[str, dict[str, Any]] = {}
    for strategy in DISCOVERY_STRATEGIES:
        trades = _run_events(
            strategy,
            candidates_by_strategy[strategy.key],
            histories,
            market_timeline,
            train_end,
            max_hold_days,
            entry_window_days,
            fee_bps,
            slippage_bps,
            limit_threshold,
        )
        results[strategy.key] = _run_portfolio(strategy, trades, histories, active_dates, initial_cash, output_dir)

    yearly = _yearly_rows(results)
    summary = _summary_rows(results, yearly)
    monthly = _monthly_rows(results)
    industry = _industry_rows(results)
    exit_reason = _exit_reason_rows(results)
    _write_csv(output_dir / "strategy_discovery_summary.csv", summary)
    _write_csv(output_dir / "strategy_discovery_yearly_performance.csv", yearly)
    _write_csv(output_dir / "strategy_discovery_monthly_returns.csv", monthly)
    _write_csv(output_dir / "strategy_discovery_industry_performance.csv", industry)
    _write_csv(output_dir / "strategy_discovery_exit_reason.csv", exit_reason)
    _render_report(output_dir, summary, yearly, industry, exit_reason, thresholds, risk_thresholds)
    best = summary.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).iloc[0].to_dict()
    payload = {
        "history_symbol_count": len(histories),
        "active_date_count": len(active_dates),
        "market_timeline_dates": len(market_timeline),
        "daily_universe_rows": int(len(daily_universe)),
        "output_dir": str(output_dir),
        "best_shadow_candidate": {
            key: (None if pd.isna(value) else value)
            for key, value in best.items()
            if key in {"strategy_key", "strategy_label", "family", "total_return", "max_drawdown", "sharpe", "accepted_trade_count"}
        },
        "thresholds": thresholds,
        "risk_thresholds": risk_thresholds,
    }
    _write_json(output_dir / "summary.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run shadow strategy discovery without changing the active strategy.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="扩展日线 per-symbol 缓存目录")
    parser.add_argument("--strategy", default=str(ROOT / "strategy.json"), help="策略配置，只读")
    parser.add_argument("--max-symbols", type=int, help="调试用：最多读取多少只股票")
    parser.add_argument("--min-bars", type=int, default=80)
    parser.add_argument("--min-history", type=int, default=60)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--limit-threshold", type=float, default=r3.DEFAULT_LIMIT_THRESHOLD)
    args = parser.parse_args()
    summary = run_strategy_discovery(
        output_dir=Path(args.output),
        cache_dir=Path(args.cache_dir),
        strategy_path=Path(args.strategy),
        max_symbols=args.max_symbols,
        min_bars=args.min_bars,
        min_history=args.min_history,
        train_ratio=args.train_ratio,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        limit_threshold=args.limit_threshold,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=fr._json_default))


if __name__ == "__main__":
    main()
