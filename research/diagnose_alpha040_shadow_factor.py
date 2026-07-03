#!/usr/bin/env python3
"""Shadow factor diagnosis for alpha040_v3_risk_controlled.

This script does not modify strategy.json, ledgers, or scanner defaults. It
keeps the Freeze V3 risk-control framework fixed and only changes the ranking
recipe to diagnose whether alpha040 direction is hurting the long sample.
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
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import factor_research as fr
import factor_research_round3 as r3
import factor_research_round4 as r4
import factor_research_round5 as r5
import market_scanner as scanner

DATA_DIR = ROOT / "data" / "expanded"
DEFAULT_OUTPUT_DIR = ROOT / "output" / "alpha040_factor_diagnosis"
DEFAULT_CACHE_DIR = DATA_DIR / "daily_kline"
REFERENCE_DIR = ROOT / "output" / "expanded_backtest_v3_2021"
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
class FactorShadowStrategy:
    """A fixed-risk shadow strategy with alternate ranking only."""

    key: str
    label: str
    rank_mode: str


STRATEGIES = [
    FactorShadowStrategy(
        key="v3_original_alpha040",
        label="A：V3 原版 alpha040 主排序",
        rank_mode="original_alpha040",
    ),
    FactorShadowStrategy(
        key="v3_no_alpha040",
        label="B：移除 alpha040，仅 rps60 + close_to_20d_high",
        rank_mode="no_alpha040",
    ),
    FactorShadowStrategy(
        key="v3_reversed_alpha040",
        label="C：-alpha040 主排序",
        rank_mode="reversed_alpha040",
    ),
    FactorShadowStrategy(
        key="v3_rps60_primary",
        label="D：rps60 主排序，alpha040 仅观察",
        rank_mode="rps60_primary",
    ),
    FactorShadowStrategy(
        key="v3_equal_weight_core",
        label="E：alpha040/rps60/high 等权",
        rank_mode="equal_weight_core",
    ),
    FactorShadowStrategy(
        key="v3_filter_only_no_rank",
        label="F：仅过滤，不使用排序因子",
        rank_mode="filter_only",
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


def _load_config(strategy_path: Path) -> dict[str, Any]:
    with open(strategy_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _active_strategy_cfg(config: dict[str, Any]) -> dict[str, Any]:
    active = config.get("active_strategy") or config.get("market_scanner", {}).get("active_strategy")
    strategies = config.get("strategies") or {}
    cfg = strategies.get(active) or strategies.get("alpha040_v3_risk_controlled") or {}
    if not cfg:
        raise ValueError("strategy.json 缺少 alpha040_v3_risk_controlled 配置")
    return cfg


def _load_v3_thresholds(strategy_path: Path) -> dict[str, float]:
    cfg = _active_strategy_cfg(_load_config(strategy_path))
    filters = cfg.get("filters") or {}
    thresholds = {
        "change_rate_5d_q75": _safe_float(filters.get("max_change_rate_5d")),
        "volatility_20d_q75": _safe_float(filters.get("max_volatility_20d")),
    }
    missing = [key for key, value in thresholds.items() if value is None]
    if missing:
        raise ValueError(f"V3 配置缺少固定阈值：{missing}")
    return {key: float(value) for key, value in thresholds.items() if value is not None}


def _load_expanded_metadata() -> dict[str, dict[str, str]]:
    """Load name and industry mapping without inventing sectors."""
    metadata: dict[str, dict[str, str]] = {}
    stock_path = DATA_DIR / "stock_basic.csv"
    if stock_path.exists():
        stock = pd.read_csv(stock_path, encoding="utf-8-sig")
        for row in stock.to_dict("records"):
            symbol = str(row.get("ts_code") or "").strip()
            if symbol:
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
            industry_name = str(row.get("industry") or row.get("sector") or "").strip()
            if not industry_name:
                industry_name = "行业缺失"
            item = metadata.setdefault(symbol, {"name": symbol, "sector": "行业缺失"})
            item["sector"] = industry_name
    return metadata


def _build_market_timeline_from_expanded() -> dict[str, dict]:
    """Build 2021+ market regime timeline from expanded index cache."""
    scanner._set_active_config(scanner._load_scanner_config("strategy.json"))
    index_dir = DATA_DIR / "index"
    if index_dir.exists():
        return fr._market_timeline_from_cache(index_dir)
    index_daily = DATA_DIR / "index_daily.csv"
    if not index_daily.exists():
        return {}
    raw = pd.read_csv(index_daily, encoding="utf-8-sig")
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce")
    label_by_code = {
        "000001.SH": "上证",
        "399001.SZ": "深成指",
        "399006.SZ": "创业板",
        "000300.SH": "沪深300",
    }
    indexes: list[dict[str, Any]] = []
    for code, label in label_by_code.items():
        group = raw[raw["ts_code"] == code].dropna(subset=["trade_date", "close"]).sort_values("trade_date")
        if len(group) < 80:
            continue
        bars = []
        for row in group.itertuples(index=False):
            bars.append(
                {
                    "date": row.trade_date.strftime("%Y-%m-%d"),
                    "open": _safe_float(getattr(row, "open", None)),
                    "high": _safe_float(getattr(row, "high", None)),
                    "low": _safe_float(getattr(row, "low", None)),
                    "close": _safe_float(getattr(row, "close", None)),
                    "volume": _safe_float(getattr(row, "vol", None)),
                    "turnover": _safe_float(getattr(row, "amount", None)),
                }
            )
        indexes.append({"label": label, "_history_bars": bars})
    return scanner._build_market_timeline(indexes) if indexes else {}


def _trend_adjustment(item: dict) -> Optional[float]:
    latest = _safe_float(item.get("latest"))
    ma5 = _safe_float(item.get("ma5"))
    ma10 = _safe_float(item.get("ma10"))
    ma20 = _safe_float(item.get("ma20"))
    if latest is None or ma20 is None or latest < ma20:
        return None
    adjustment = 0.0
    if ma5 is not None and ma10 is not None and ma5 >= ma10 >= ma20:
        adjustment += 0.08
    elif ma5 is not None and ma5 >= ma20:
        adjustment += 0.04
    distance = latest / ma20 - 1 if ma20 else 0.0
    if distance > 0.12:
        adjustment -= min(0.35, (distance - 0.12) * 2.0)
    return adjustment


def _base_filters(
    item: dict,
    market_profile: dict[str, Any],
    thresholds: dict[str, float],
    strategy: FactorShadowStrategy,
) -> tuple[bool, str]:
    """Apply fixed V3 filters; ranking requirements depend on shadow mode."""
    if market_profile.get("regime_label") != RISK_ON_LABEL:
        return False, "market_not_risk_on"
    if _trend_adjustment(item) is None:
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

    if strategy.rank_mode != "filter_only":
        required = ["rps60_z", "close_to_20d_high_z"]
        if strategy.rank_mode in {"original_alpha040", "reversed_alpha040", "equal_weight_core"}:
            required.append("alpha040_z")
        for col in required:
            if _safe_float(item.get(col)) is None:
                return False, f"missing_{col}"
    return True, "passed"


def _rank_score(item: dict, strategy: FactorShadowStrategy) -> Optional[float]:
    trend = _trend_adjustment(item)
    if trend is None:
        return None
    alpha = _safe_float(item.get("alpha040_z"))
    rps60 = _safe_float(item.get("rps60_z"))
    high = _safe_float(item.get("close_to_20d_high_z"))
    if strategy.rank_mode == "filter_only":
        return 0.0
    if rps60 is None or high is None:
        return None
    if strategy.rank_mode == "no_alpha040":
        score = (2 / 3) * rps60 + (1 / 3) * high + trend
    elif strategy.rank_mode == "reversed_alpha040":
        if alpha is None:
            return None
        score = -0.55 * alpha + 0.30 * rps60 + 0.15 * high + trend
    elif strategy.rank_mode == "rps60_primary":
        score = 0.80 * rps60 + 0.20 * high + trend
    elif strategy.rank_mode == "equal_weight_core":
        if alpha is None:
            return None
        score = (alpha + rps60 + high) / 3 + trend
    else:
        if alpha is None:
            return None
        score = 0.55 * alpha + 0.30 * rps60 + 0.15 * high + trend
    return round(float(score), 6)


def _eligible_for_strategy(
    rows: list[dict],
    market_profile: dict[str, Any],
    thresholds: dict[str, float],
    strategy: FactorShadowStrategy,
) -> tuple[list[dict], dict[str, int]]:
    eligible: list[dict] = []
    reason_counts: Counter[str] = Counter()
    for item in rows:
        passed, reason = _base_filters(item, market_profile, thresholds, strategy)
        reason_counts[reason] += 1
        if not passed:
            continue
        score = _rank_score(item, strategy)
        if score is None:
            reason_counts["score_missing"] += 1
            continue
        enriched = {**item, "alpha040_core_score": score, "rank_score": score}
        eligible.append(enriched)
    if strategy.rank_mode == "filter_only":
        eligible.sort(key=lambda row: (str(row.get("_symbol_key") or row.get("symbol") or ""), str(row.get("name") or "")))
    else:
        eligible.sort(
            key=lambda row: (
                _safe_float(row.get("alpha040_core_score")) if _safe_float(row.get("alpha040_core_score")) is not None else -999.0,
                str(row.get("_symbol_key") or row.get("symbol") or ""),
            ),
            reverse=True,
        )
    return eligible, dict(reason_counts)


def _build_candidates(
    universe_by_date: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    thresholds: dict[str, float],
) -> tuple[pd.DataFrame, dict[str, dict[str, list[dict]]]]:
    rows: list[dict[str, Any]] = []
    candidates_by_strategy: dict[str, dict[str, list[dict]]] = {strategy.key: {} for strategy in STRATEGIES}
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
        for strategy in STRATEGIES:
            eligible, reason_counts = _eligible_for_strategy(universe_by_date[date], market_profile, thresholds, strategy)
            candidates_by_strategy[strategy.key][date] = eligible
            row[f"{strategy.key}_candidate_count"] = len(eligible)
            row[f"{strategy.key}_selected_count"] = min(len(eligible), candidate_limit)
            row[f"{strategy.key}_filter_reasons_json"] = json.dumps(reason_counts, ensure_ascii=False)
        rows.append(row)
    return pd.DataFrame(rows), candidates_by_strategy


def _run_strategy_events(
    strategy: FactorShadowStrategy,
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
            trade["rank_mode"] = strategy.rank_mode
            trade["shadow_rank_score"] = item.get("alpha040_core_score")
            trade["split"] = "train" if pd.to_datetime(trade["signal_date"]) <= train_end else "test"
            trades.append(trade)
            if trade.get("exit_date"):
                next_available_by_symbol[symbol] = str(trade["exit_date"])
    trades_df = pd.DataFrame(trades)
    return r5._enrich_trades(trades_df, {date: candidates_by_date[date] for date in candidates_by_date}, histories, limit_threshold)


def _accepted_trades(portfolio: pd.DataFrame) -> pd.DataFrame:
    if portfolio.empty or "portfolio_action" not in portfolio.columns:
        return pd.DataFrame()
    return portfolio[portfolio["portfolio_action"] == "accepted"].copy()


def _run_portfolio(
    strategy: FactorShadowStrategy,
    trades: pd.DataFrame,
    histories: dict[str, list[dict]],
    active_dates: list[str],
    initial_cash: float,
    output_dir: Path,
) -> dict[str, Any]:
    portfolio = r4._accept_portfolio_trades(trades, initial_cash)
    equity, monthly, metrics = r4._portfolio_equity_curve(portfolio, histories, active_dates, initial_cash)
    accepted = _accepted_trades(portfolio)
    prefix = f"{strategy.key}_"
    _write_csv(output_dir / f"{prefix}trades.csv", trades)
    _write_csv(output_dir / f"{prefix}portfolio_trades.csv", portfolio)
    _write_csv(output_dir / f"{prefix}accepted_trades.csv", accepted)
    _write_csv(output_dir / f"{prefix}daily_equity.csv", equity)
    _write_csv(output_dir / f"{prefix}monthly_returns.csv", monthly)
    return {
        "strategy": strategy,
        "trades": trades,
        "portfolio": portfolio,
        "accepted": accepted,
        "equity": equity,
        "monthly": monthly,
        "metrics": metrics,
    }


def _max_consecutive_losses(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    ordered = frame.copy()
    sort_cols = [col for col in ["exit_date", "entry_date", "signal_date"] if col in ordered.columns]
    if sort_cols:
        ordered = ordered.sort_values(sort_cols)
    max_run = 0
    current = 0
    returns = pd.to_numeric(ordered.get("net_return"), errors="coerce").fillna(0)
    for value in returns:
        if value < 0:
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    return int(max_run)


def _trade_stats(frame: pd.DataFrame) -> dict[str, Any]:
    returns = pd.to_numeric(frame.get("net_return"), errors="coerce").dropna() if not frame.empty else pd.Series(dtype=float)
    return {
        "win_rate": float((returns > 0).mean()) if len(returns) else None,
        "average_trade_return": float(returns.mean()) if len(returns) else None,
        "median_trade_return": float(returns.median()) if len(returns) else None,
        "max_consecutive_losses": _max_consecutive_losses(frame),
        "stop_loss_count": int((frame.get("exit_reason") == "stop_loss").sum()) if not frame.empty else 0,
        "limit_down_blocked_exit_count": int((frame.get("exit_reason") == "limit_down_blocked_exit").sum()) if not frame.empty else 0,
        "timeout_count": int((frame.get("exit_reason") == "timeout").sum()) if not frame.empty else 0,
        "environment_exit_count": int((frame.get("exit_reason") == "environment_exit").sum()) if not frame.empty else 0,
    }


def _monthly_stats(monthly: pd.DataFrame) -> dict[str, Any]:
    if monthly.empty or "monthly_return" not in monthly.columns:
        return {
            "positive_month_rate": None,
            "worst_month": None,
            "worst_month_return": None,
            "best_month": None,
            "best_month_return": None,
        }
    work = monthly.copy()
    values = pd.to_numeric(work["monthly_return"], errors="coerce")
    valid = work[values.notna()].copy()
    valid["monthly_return"] = pd.to_numeric(valid["monthly_return"], errors="coerce")
    if valid.empty:
        return {
            "positive_month_rate": None,
            "worst_month": None,
            "worst_month_return": None,
            "best_month": None,
            "best_month_return": None,
        }
    worst = valid.sort_values("monthly_return").iloc[0].to_dict()
    best = valid.sort_values("monthly_return", ascending=False).iloc[0].to_dict()
    return {
        "positive_month_rate": float((valid["monthly_return"] > 0).mean()),
        "worst_month": str(worst.get("month")),
        "worst_month_return": float(worst.get("monthly_return")),
        "best_month": str(best.get("month")),
        "best_month_return": float(best.get("monthly_return")),
    }


def _yearly_return_rows(
    results: dict[str, dict[str, Any]],
    years: list[int],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        result = results[strategy.key]
        accepted = result["accepted"].copy()
        equity = result["equity"].copy()
        if not accepted.empty:
            accepted["entry_date_dt"] = pd.to_datetime(accepted["entry_date"], errors="coerce")
            accepted["entry_year"] = accepted["entry_date_dt"].dt.year
        equity["date_dt"] = pd.to_datetime(equity["date"], errors="coerce") if not equity.empty else pd.Series(dtype="datetime64[ns]")
        for year in years:
            eq = equity[equity["date_dt"].dt.year == year].copy() if not equity.empty else pd.DataFrame()
            trades = accepted[accepted["entry_year"] == year].copy() if not accepted.empty else pd.DataFrame()
            if eq.empty:
                continue
            start_equity = float(eq["equity"].iloc[0])
            end_equity = float(eq["equity"].iloc[-1])
            period_return = end_equity / start_equity - 1 if start_equity else None
            peak = eq["equity"].cummax()
            drawdown = eq["equity"] / peak - 1
            returns = pd.to_numeric(trades.get("net_return"), errors="coerce").dropna() if not trades.empty else pd.Series(dtype=float)
            rows.append(
                {
                    "strategy_key": strategy.key,
                    "strategy_label": strategy.label,
                    "year": "2026 YTD" if year == 2026 else str(year),
                    "return": period_return,
                    "max_drawdown": float(drawdown.min()) if len(drawdown) else None,
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
    for strategy in STRATEGIES:
        result = results[strategy.key]
        metrics = result["metrics"]
        accepted = result["accepted"]
        stats = _trade_stats(accepted)
        monthly = _monthly_stats(result["monthly"])
        yr = yearly[yearly["strategy_key"] == strategy.key].copy()
        yr_valid = yr[pd.to_numeric(yr["return"], errors="coerce").notna()].copy()
        yr_valid["return"] = pd.to_numeric(yr_valid["return"], errors="coerce")
        worst_year = yr_valid.sort_values("return").head(1).to_dict("records")
        positive_year_rate = float((yr_valid["return"] > 0).mean()) if not yr_valid.empty else None
        rows.append(
            {
                "strategy_key": strategy.key,
                "strategy_label": strategy.label,
                "rank_mode": strategy.rank_mode,
                "event_trade_count": int(len(result["trades"])),
                "accepted_trade_count": int(metrics.get("accepted_trades") or 0),
                "ending_equity": metrics.get("ending_equity"),
                "total_return": metrics.get("total_return"),
                "annualized_return": metrics.get("annualized_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe": metrics.get("sharpe"),
                "calmar": metrics.get("calmar"),
                **stats,
                "positive_year_rate": positive_year_rate,
                "positive_month_rate": monthly.get("positive_month_rate"),
                "worst_year": worst_year[0]["year"] if worst_year else None,
                "worst_year_return": worst_year[0]["return"] if worst_year else None,
                "worst_month": monthly.get("worst_month"),
                "worst_month_return": monthly.get("worst_month_return"),
                "best_month": monthly.get("best_month"),
                "best_month_return": monthly.get("best_month_return"),
            }
        )
    return pd.DataFrame(rows)


def _monthly_return_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        monthly = results[strategy.key]["monthly"].copy()
        if monthly.empty:
            continue
        for row in monthly.to_dict("records"):
            rows.append(
                {
                    "strategy_key": strategy.key,
                    "strategy_label": strategy.label,
                    "month": row.get("month"),
                    "start_equity": row.get("start_equity"),
                    "end_equity": row.get("end_equity"),
                    "monthly_return": row.get("monthly_return"),
                }
            )
    return pd.DataFrame(rows)


def _industry_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        accepted = results[strategy.key]["accepted"].copy()
        if accepted.empty:
            rows.append(
                {
                    "strategy_key": strategy.key,
                    "strategy_label": strategy.label,
                    "industry": "无交易",
                    "trade_count": 0,
                    "trade_count_share": None,
                    "win_rate": None,
                    "average_trade_return": None,
                    "median_trade_return": None,
                    "stop_loss_count": 0,
                    "limit_down_blocked_exit_count": 0,
                    "cumulative_contribution": 0.0,
                    "industry_missing_count": 0,
                    "contribution_rank": None,
                    "trade_count_rank": None,
                }
            )
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
                    "contribution_rank": None,
                    "trade_count_rank": None,
                }
            )
    result = pd.DataFrame(rows)
    if not result.empty:
        result["contribution_rank"] = result.groupby("strategy_key")["cumulative_contribution"].rank(method="first", ascending=False)
        result["trade_count_rank"] = result.groupby("strategy_key")["trade_count"].rank(method="first", ascending=False)
    return result.sort_values(["strategy_key", "trade_count", "cumulative_contribution"], ascending=[True, False, False])


def _exit_reason_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
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


def _industry_concentration(industry: pd.DataFrame, strategy_key: str) -> dict[str, Any]:
    rows = industry[industry["strategy_key"] == strategy_key].copy()
    rows = rows[rows["trade_count"] > 0]
    if rows.empty:
        return {"top_industry": None, "top_industry_share": None, "top3_share": None, "industry_count": 0, "missing_count": 0}
    ordered = rows.sort_values("trade_count", ascending=False)
    return {
        "top_industry": ordered.iloc[0]["industry"],
        "top_industry_share": float(ordered.iloc[0]["trade_count_share"]),
        "top3_share": float(ordered.head(3)["trade_count_share"].sum()),
        "industry_count": int(len(rows)),
        "missing_count": int(rows["industry_missing_count"].sum()),
    }


def _reference_baseline() -> dict[str, Any]:
    path = REFERENCE_DIR / "expanded_strategy_comparison.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path, encoding="utf-8-sig")
    ref = df[df["strategy_key"] == "v3_atr_risk_budget_hot5_vol_risk_on"]
    if ref.empty:
        return {}
    return {key: (None if pd.isna(value) else value) for key, value in ref.iloc[0].to_dict().items()}


def _markdown_table(df: pd.DataFrame, cols: list[tuple[str, str]], max_rows: Optional[int] = None) -> list[str]:
    work = df.head(max_rows) if max_rows else df
    lines = [
        "| " + " | ".join(title for title, _ in cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in work.to_dict("records"):
        cells = []
        for _title, col in cols:
            value = row.get(col)
            if col.endswith("return") or col in {
                "total_return",
                "annualized_return",
                "max_drawdown",
                "win_rate",
                "average_trade_return",
                "median_trade_return",
                "positive_year_rate",
                "positive_month_rate",
                "worst_year_return",
                "worst_month_return",
                "cumulative_contribution",
                "trade_count_share",
                "top_industry_share",
                "top3_share",
                "max_loss",
            }:
                cells.append(_fmt_pct(value))
            elif isinstance(value, float):
                cells.append(_fmt_num(value))
            elif pd.isna(value):
                cells.append("-")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _judgement(summary: pd.DataFrame) -> dict[str, Any]:
    by_key = summary.set_index("strategy_key").to_dict("index")
    original = by_key.get("v3_original_alpha040", {})
    no_alpha = by_key.get("v3_no_alpha040", {})
    reversed_alpha = by_key.get("v3_reversed_alpha040", {})
    rps_primary = by_key.get("v3_rps60_primary", {})
    filter_only = by_key.get("v3_filter_only_no_rank", {})

    def total(key: dict[str, Any]) -> float:
        value = _safe_float(key.get("total_return"))
        return value if value is not None else float("-inf")

    original_ret = total(original)
    material_improvement = 0.01
    best_key = summary.sort_values("total_return", ascending=False).iloc[0]["strategy_key"] if not summary.empty else None
    all_negative = bool((pd.to_numeric(summary["total_return"], errors="coerce") < 0).all()) if not summary.empty else False
    reversed_delta = total(reversed_alpha) - original_ret
    no_alpha_delta = total(no_alpha) - original_ret
    rps_primary_delta = total(rps_primary) - original_ret
    ranked_best = max(
        total(original),
        total(no_alpha),
        total(reversed_alpha),
        total(rps_primary),
        total(by_key.get("v3_equal_weight_core", {})),
    )
    return {
        "original_return": original_ret,
        "best_strategy_key": best_key,
        "all_negative": all_negative,
        "material_improvement_threshold": material_improvement,
        "alpha040_dragged": no_alpha_delta > material_improvement and reversed_delta > material_improvement,
        "reversed_better": reversed_delta > material_improvement,
        "reversed_slightly_better": reversed_delta > 0,
        "no_alpha_better": no_alpha_delta > material_improvement,
        "no_alpha_slightly_better": no_alpha_delta > 0,
        "rps_primary_better": rps_primary_delta > material_improvement,
        "rps_primary_slightly_better": rps_primary_delta > 0,
        "filter_only_better_than_ranked": total(filter_only) >= ranked_best + material_improvement,
        "filter_only_slightly_better_than_ranked": total(filter_only) >= ranked_best,
        "filter_only_return": total(filter_only),
        "no_alpha_return": total(no_alpha),
        "reversed_return": total(reversed_alpha),
        "rps_primary_return": total(rps_primary),
        "reversed_delta_vs_original": reversed_delta,
        "no_alpha_delta_vs_original": no_alpha_delta,
        "rps_primary_delta_vs_original": rps_primary_delta,
        "filter_only_delta_vs_best_ranked": total(filter_only) - ranked_best,
    }


def _write_reports(
    output_dir: Path,
    summary: pd.DataFrame,
    yearly: pd.DataFrame,
    monthly: pd.DataFrame,
    industry: pd.DataFrame,
    exit_reason: pd.DataFrame,
    daily_counts: pd.DataFrame,
    thresholds: dict[str, float],
    reference: dict[str, Any],
) -> None:
    judgement = _judgement(summary)
    concentration_rows = []
    for strategy in STRATEGIES:
        concentration_rows.append({"strategy_key": strategy.key, **_industry_concentration(industry, strategy.key)})
    concentration = pd.DataFrame(concentration_rows)
    summary_with_conc = summary.merge(concentration, on="strategy_key", how="left")
    _write_csv(output_dir / "shadow_factor_strategy_summary.csv", summary_with_conc)
    _write_csv(output_dir / "shadow_factor_yearly_performance.csv", yearly)
    _write_csv(output_dir / "shadow_factor_monthly_returns.csv", monthly)
    _write_csv(output_dir / "shadow_factor_industry_performance.csv", industry)
    _write_csv(output_dir / "shadow_factor_exit_reason.csv", exit_reason)

    original = summary[summary["strategy_key"] == "v3_original_alpha040"].iloc[0].to_dict()
    ref_note = "未找到上一轮 V3 汇总，无法做数值校验。"
    if reference:
        ref_note = (
            f"上一轮 V3 原始扩展回测：接受交易 {int(reference.get('accepted_trade_count') or 0)}，"
            f"累计收益 {_fmt_pct(reference.get('total_return'))}，最大回撤 {_fmt_pct(reference.get('max_drawdown'))}；"
            f"本轮 shadow runner 原版：接受交易 {int(original.get('accepted_trade_count') or 0)}，"
            f"累计收益 {_fmt_pct(original.get('total_return'))}，最大回撤 {_fmt_pct(original.get('max_drawdown'))}。"
        )

    lines = [
        "# Alpha040 Shadow Factor Strategy Comparison",
        "",
        "本报告只做 shadow 对照实验：固定 V3 风控框架、固定阈值、固定 ATR 止损与风险预算仓位，只替换排序口径。未修改主策略、未连接实盘、未自动下单。",
        "",
        "## 数据与固定规则",
        "",
        "- 数据：2021-01-04 至 2026-06-26 扩展日线，当前为 efinance 单源扩展数据，行业映射来自 BaoStock/expanded metadata。",
        "- 风控：只在积极市场环境开仓；中性、谨慎、防守禁止新开仓。",
        f"- 过滤：5 日涨幅 > {_fmt_pct(thresholds['change_rate_5d_q75'])} 剔除；volatility_20d > {_fmt_pct(thresholds['volatility_20d_q75'])} 剔除；收盘价需站上 MA20。",
        "- 交易：ATR 止损 + 风险预算仓位；冲高回落仅作标签，不硬过滤。",
        f"- Baseline 校验：{ref_note}",
        "",
        "## 策略总览",
        "",
    ]
    lines.extend(
        _markdown_table(
            summary_with_conc,
            [
                ("版本", "strategy_key"),
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
                ("timeout", "timeout_count"),
                ("environment_exit", "environment_exit_count"),
                ("正收益年份", "positive_year_rate"),
                ("正收益月份", "positive_month_rate"),
                ("最差年份", "worst_year"),
                ("最差月份", "worst_month"),
            ],
        )
    )
    lines.extend(["", "## 分年份表现", ""])
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
    lines.extend(["", "## 行业 Top/Bottom 摘要", ""])
    for strategy in STRATEGIES:
        rows = industry[industry["strategy_key"] == strategy.key].copy()
        lines.extend([f"### {strategy.key}", ""])
        conc = concentration[concentration["strategy_key"] == strategy.key].iloc[0].to_dict()
        lines.append(
            f"- 行业数：{int(conc.get('industry_count') or 0)}；第一大行业：{conc.get('top_industry') or '-'} "
            f"({_fmt_pct(conc.get('top_industry_share'))})；Top3 集中度：{_fmt_pct(conc.get('top3_share'))}；"
            f"行业缺失交易：{int(conc.get('missing_count') or 0)}。"
        )
        top = rows.sort_values("cumulative_contribution", ascending=False).head(10)
        bottom = rows.sort_values("cumulative_contribution").head(10)
        lines.extend(["", "Top 10:", ""])
        lines.extend(
            _markdown_table(
                top,
                [
                    ("行业", "industry"),
                    ("交易", "trade_count"),
                    ("胜率", "win_rate"),
                    ("平均单笔", "average_trade_return"),
                    ("贡献", "cumulative_contribution"),
                    ("止损", "stop_loss_count"),
                    ("跌停", "limit_down_blocked_exit_count"),
                ],
            )
        )
        lines.extend(["", "Bottom 10:", ""])
        lines.extend(
            _markdown_table(
                bottom,
                [
                    ("行业", "industry"),
                    ("交易", "trade_count"),
                    ("胜率", "win_rate"),
                    ("平均单笔", "average_trade_return"),
                    ("贡献", "cumulative_contribution"),
                    ("止损", "stop_loss_count"),
                    ("跌停", "limit_down_blocked_exit_count"),
                ],
            )
        )
        lines.append("")
    lines.extend(["## 退出原因表现", ""])
    lines.extend(
        _markdown_table(
            exit_reason,
            [
                ("版本", "strategy_key"),
                ("退出原因", "exit_reason"),
                ("笔数", "trade_count"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_trade_return"),
                ("中位数", "median_trade_return"),
                ("最大亏损", "max_loss"),
                ("贡献", "cumulative_contribution"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## A. 已确认事实",
            "",
            f"- alpha040 原版 V3 本轮累计收益为 {_fmt_pct(original.get('total_return'))}，长样本收益端仍未通过验证。",
            f"- 本轮表现最好的 shadow 版本是 `{judgement['best_strategy_key']}`，但它只是 shadow 对照，不改变主策略。",
            f"- 所有版本是否均为负：{'是' if judgement['all_negative'] else '否'}。",
            "",
            "## B. 对 alpha040 的判断",
            "",
        ]
    )
    if judgement["reversed_better"]:
        lines.append("- `-alpha040` 版本较 alpha040 原方向有超过 1 个百分点的收益改善，alpha040 原方向可能存在方向性拖累。")
    elif judgement["reversed_slightly_better"]:
        lines.append("- `-alpha040` 版本数值上略高于 alpha040 原方向，但改善不足 1 个百分点，不能称为明显优于。")
    else:
        lines.append("- `-alpha040` 未明显优于 alpha040 原方向，不能简单判断为“反向因子更好”。")
    if judgement["no_alpha_better"]:
        lines.append("- 去掉 alpha040 后表现改善，说明 alpha040 在当前排序组合里可能不是稳定贡献项。")
    else:
        lines.append("- 去掉 alpha040 后没有改善，说明亏损不只来自 alpha040 单一主排序。")
    if judgement["rps_primary_better"]:
        lines.append("- rps60 主排序优于 alpha040 原版，但不能直接替换主策略，只能列入 shadow 观察。")
    else:
        lines.append("- rps60 主排序没有优于 alpha040 原版，当前证据不足以支持把 rps60 提为主因子。")
    lines.extend(["", "## C. 对 V3 风控框架的判断", ""])
    if judgement["filter_only_better_than_ranked"]:
        lines.append("- filter-only 版本不弱于排序版本，说明当前排序因子可能没有提供有效增益，甚至可能带来负贡献。")
    else:
        lines.append("- 至少有排序版本优于 filter-only，说明风控框架之外的排序仍可能有贡献，但贡献稳定性需要继续观察。")
    if judgement["all_negative"]:
        lines.append("- 所有 shadow 版本均为负，说明当前量价短线框架没有通过 2021 起长样本收益验证。")
    lines.extend(
        [
            "",
            "## D. 不能立即做的事",
            "",
            "- 不能因为某个 shadow 版本更好就自动替换 `alpha040_v3_risk_controlled`。",
            "- 不能根据本轮结果调 20.39% 或 4.98% 阈值。",
            "- 不能新增因子、训练模型、连接实盘或自动下单。",
            "- 不能把单源 efinance 扩展数据宣传为多源完整全市场长期验证。",
            "",
            "## E. 下一步 shadow experiment candidate",
            "",
            "- 若 `v3_no_alpha040` 或 `v3_rps60_primary` 明显更稳，可进入 freeze 前的稳定性观察，但仍需样本外 forward paper trading。",
            "- 若 `v3_filter_only_no_rank` 最好，应优先审计排序因子贡献，而不是继续堆叠因子。",
            "- 若 `v3_reversed_alpha040` 最好，只能先做 alpha040 方向性稳定性诊断，不得反向并入主策略。",
            "",
            "## 输出文件",
            "",
            "- `shadow_factor_strategy_summary.csv`",
            "- `shadow_factor_yearly_performance.csv`",
            "- `shadow_factor_monthly_returns.csv`",
            "- `shadow_factor_industry_performance.csv`",
            "- `shadow_factor_exit_reason.csv`",
            "- `alpha040_direction_diagnosis.md`",
        ]
    )
    (output_dir / "shadow_factor_strategy_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    direction_lines = [
        "# Alpha040 Direction Diagnosis",
        "",
        "本文件只回答主因子方向与排序贡献问题，不提出直接调参或主策略替换。",
        "",
        "## 对照结果",
        "",
    ]
    direction_lines.extend(
        _markdown_table(
            summary,
            [
                ("版本", "strategy_key"),
                ("排序口径", "rank_mode"),
                ("累计收益", "total_return"),
                ("最大回撤", "max_drawdown"),
                ("Sharpe", "sharpe"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_trade_return"),
                ("正收益年份", "positive_year_rate"),
                ("正收益月份", "positive_month_rate"),
            ],
        )
    )
    direction_lines.extend(
        [
            "",
            "## A. 已确认事实",
            "",
            f"- 原版 alpha040 V3 收益：{_fmt_pct(judgement['original_return'])}。",
            f"- 去掉 alpha040 收益：{_fmt_pct(judgement['no_alpha_return'])}。",
            f"- 反向 alpha040 收益：{_fmt_pct(judgement['reversed_return'])}。",
            f"- rps60 主排序收益：{_fmt_pct(judgement['rps_primary_return'])}。",
            f"- filter-only 收益：{_fmt_pct(judgement['filter_only_return'])}。",
            "",
            "## B. 对 alpha040 的判断",
            "",
            f"- alpha040 原方向是否拖累 V3：{'倾向是' if judgement['alpha040_dragged'] else '证据不足或不单一'}。",
            f"- -alpha040 是否明显优于 alpha040：{'是' if judgement['reversed_better'] else '否'}（差值 {_fmt_pct(judgement['reversed_delta_vs_original'])}）。",
            f"- 去掉 alpha040 后是否改善：{'是' if judgement['no_alpha_better'] else '否'}（差值 {_fmt_pct(judgement['no_alpha_delta_vs_original'])}）。",
            f"- rps60 主排序是否更稳定：{'倾向是' if judgement['rps_primary_better'] else '暂未证明'}。",
            "",
            "## C. 对 V3 风控框架的判断",
            "",
            f"- filter-only 是否不弱于排序版本：{'是' if judgement['filter_only_better_than_ranked'] else '否'}。",
            "- 风控框架降低了止损和跌停无法卖出暴露，但收益端是否足够，仍需 forward paper trading 和独立样本观察。",
            "",
            "## D. 不能立即做的事",
            "",
            "- 不替换主策略，不反向使用 alpha040，不把 rps60 提为主因子，不调整冻结阈值。",
            "",
            "## E. 下一步 shadow experiment candidate",
            "",
            "- 建议把表现较好的非 alpha040 排序版本纳入下一轮只读 shadow 观察；若所有版本仍为负，则优先暂停因子扩张，审计框架本身。",
        ]
    )
    (output_dir / "alpha040_direction_diagnosis.md").write_text("\n".join(direction_lines) + "\n", encoding="utf-8")
    _write_csv(output_dir / "daily_candidate_counts.csv", daily_counts)
    _write_json(output_dir / "summary.json", {"judgement": judgement, "thresholds": thresholds, "reference_baseline": reference})


def run_shadow_factor_diagnosis(
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
    thresholds = _load_v3_thresholds(strategy_path)
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
    active_dates = [date for date, rows in universe_by_date.items() if rows]
    train_end, _test_start = fr._split_dates(pd.Series(pd.to_datetime(active_dates)), train_ratio=train_ratio)
    market_timeline = _build_market_timeline_from_expanded()
    max_hold_days = int(scanner._cfg("backtest", "max_hold_days", 5))
    entry_window_days = int(scanner._cfg("backtest", "entry_window_days", 2))
    initial_cash = float(scanner._cfg("backtest", "portfolio_initial_cash", 1_000_000))
    daily_counts, candidates_by_strategy = _build_candidates(universe_by_date, market_timeline, thresholds)

    results: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        trades = _run_strategy_events(
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

    years = [2021, 2022, 2023, 2024, 2025, 2026]
    yearly = _yearly_return_rows(results, years)
    summary = _summary_rows(results, yearly)
    monthly = _monthly_return_rows(results)
    industry = _industry_rows(results)
    exit_reason = _exit_reason_rows(results)
    reference = _reference_baseline()
    _write_reports(output_dir, summary, yearly, monthly, industry, exit_reason, daily_counts, thresholds, reference)
    return {
        "history_symbol_count": len(histories),
        "active_date_count": len(active_dates),
        "market_timeline_dates": len(market_timeline),
        "output_dir": str(output_dir),
        "summary": summary.to_dict("records"),
        "daily_universe_rows": int(len(daily_universe)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run alpha040 shadow factor diagnosis without changing main strategy.")
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
    summary = run_shadow_factor_diagnosis(
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
