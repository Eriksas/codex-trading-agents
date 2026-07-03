"""
factor_research_round4.py - Alpha040 Core 风险归因与组合增强回测

输出：output/factor_research_round4/
本模块只读本地历史缓存和策略配置，不修改 market_scanner 主策略、不写台账、不连接实盘。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import factor_research_round3 as r3
import market_scanner as scanner

DEFAULT_OUTPUT_DIR = Path("output/factor_research_round4")
DEFAULT_ATR_WINDOW = 14
DEFAULT_ATR_MULTIPLIER = 2.0
DEFAULT_RISK_BUDGET_PCT = 0.002


def _safe_float(value: Any) -> Optional[float]:
    """安全转换 float。"""
    return fr._safe_float(value)


def _fmt_pct(value: Any, digits: int = 2) -> str:
    """百分比格式化。"""
    return fr._fmt_pct(value, digits=digits)


def _json_default(value: Any) -> Any:
    """JSON 序列化辅助。"""
    return fr._json_default(value)


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    """写 CSV 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)


def _true_ranges(bars: list[dict], end_idx: int, window: int) -> list[float]:
    """计算截至 end_idx 的真实波幅序列。"""
    values: list[float] = []
    start_idx = max(1, end_idx - window + 1)
    for idx in range(start_idx, end_idx + 1):
        high = _safe_float(bars[idx].get("high"))
        low = _safe_float(bars[idx].get("low"))
        prev_close = _safe_float(bars[idx - 1].get("close"))
        if high is None or low is None or prev_close is None:
            continue
        values.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return values


def _atr(bars: list[dict], end_idx: int, window: int = DEFAULT_ATR_WINDOW) -> Optional[float]:
    """计算 ATR。"""
    values = _true_ranges(bars, end_idx, window)
    if len(values) < max(5, window // 2):
        return None
    return float(sum(values) / len(values))


def _risk_budget_position_pct(entry_price: float, stop_loss: float, max_position_pct: float = 8.0) -> float:
    """根据账户风险预算反推仓位百分比。"""
    if entry_price <= 0:
        return max_position_pct
    stop_distance = max((entry_price - stop_loss) / entry_price, 0.005)
    return round(max(0.0, min(max_position_pct, DEFAULT_RISK_BUDGET_PCT / stop_distance * 100)), 2)


def _simulate_trade_variant(
    signal: dict,
    bars: list[dict],
    signal_idx: int,
    market_timeline: dict[str, dict],
    max_hold_days: int,
    entry_window_days: int,
    fee_bps: float,
    slippage_bps: float,
    limit_threshold: float,
    variant: str,
) -> Optional[dict]:
    """按指定风险控制版本回放交易。"""
    if variant == "current_stop" and hasattr(scanner, "_build_legacy_trade_plan"):
        plan = scanner._build_legacy_trade_plan(signal)
    else:
        plan = scanner._build_trade_plan(signal)
    trigger_low, trigger_high = scanner._parse_trigger_zone(plan["trigger_zone"])
    entry_idx: Optional[int] = None
    entry_price_raw: Optional[float] = None
    entry_block_reasons: list[str] = []
    for idx in range(signal_idx + 1, min(signal_idx + 1 + entry_window_days, len(bars))):
        bar = bars[idx]
        prev_close = r3._prev_close_from_bars(bars, idx)
        if r3._is_suspended_or_untradeable(bar):
            entry_block_reasons.append("suspended_no_entry")
            continue
        if r3._is_limit_up(bar, prev_close, threshold=limit_threshold):
            entry_block_reasons.append("limit_up_cannot_buy")
            continue
        price = scanner._entry_price_for_bar(bar, trigger_low, trigger_high)
        if price is not None:
            entry_idx = idx
            entry_price_raw = price
            break
    if entry_idx is None or entry_price_raw is None:
        return None

    entry_price = entry_price_raw * (1 + slippage_bps / 10000)
    rule_stop = float(plan["stop_loss"])
    atr_value = _atr(bars, signal_idx)
    if variant in {"atr_stop", "atr_stop_risk_budget"} and atr_value is not None:
        stop_loss = round(entry_price_raw - DEFAULT_ATR_MULTIPLIER * atr_value, 3)
        stop_loss = min(rule_stop, stop_loss) if stop_loss < entry_price_raw else rule_stop
    else:
        stop_loss = rule_stop
    stop_loss = max(0.01, stop_loss)
    first_take_profit = float(plan["first_take_profit"])
    if variant in {"atr_stop", "atr_stop_risk_budget"} and atr_value is not None:
        risk_per_share = max(entry_price_raw - stop_loss, entry_price_raw * 0.02)
        first_take_profit = round(entry_price_raw + risk_per_share * float(scanner._cfg("trade_plan", "reward_risk", 1.6)), 3)

    position_pct = _safe_float(plan.get("position_pct")) or 8.0
    if variant == "atr_stop_risk_budget":
        position_pct = _risk_budget_position_pct(entry_price_raw, stop_loss, max_position_pct=position_pct)

    max_gain = -1.0
    blocked_exit_days = 0
    exit_idx = min(entry_idx + max_hold_days - 1, len(bars) - 1)
    exit_price_raw = _safe_float(bars[exit_idx].get("close")) or entry_price_raw
    exit_reason = "timeout"
    same_day_stop_take = False
    blocked_exit_reason = ""

    for idx in range(entry_idx, len(bars)):
        bar = bars[idx]
        prev_close = r3._prev_close_from_bars(bars, idx)
        date = str(bar.get("date") or "")
        high = _safe_float(bar.get("high")) or entry_price_raw
        low = _safe_float(bar.get("low")) or entry_price_raw
        close = _safe_float(bar.get("close")) or entry_price_raw
        max_gain = max(max_gain, high / entry_price_raw - 1)
        holding_days = idx - entry_idx + 1
        can_sell = r3._can_sell(bar, prev_close, limit_threshold)
        desired_exit: Optional[tuple[str, float]] = None
        hit_stop = low <= stop_loss
        hit_take = high >= first_take_profit
        if hit_stop and hit_take:
            same_day_stop_take = True
            desired_exit = ("same_day_stop_take_conservative", stop_loss)
        elif hit_stop:
            desired_exit = ("stop_loss", stop_loss)
        elif hit_take:
            desired_exit = ("take_profit", first_take_profit)
        elif (market_timeline.get(date) or {}).get("regime") == "defensive" and holding_days > 1:
            desired_exit = ("environment_exit", close)
        elif holding_days >= 5 and max_gain < 0.02:
            desired_exit = ("time_stop", close)
        elif holding_days >= max_hold_days:
            desired_exit = ("timeout", close)

        if desired_exit is None:
            continue
        if not can_sell:
            blocked_exit_days += 1
            blocked_exit_reason = desired_exit[0]
            if idx < len(bars) - 1:
                continue
        exit_reason, exit_price_raw = desired_exit
        if blocked_exit_days > 0:
            exit_reason = "limit_down_blocked_exit"
        exit_idx = idx
        break

    exit_price = exit_price_raw * (1 - slippage_bps / 10000)
    gross_return = exit_price / entry_price - 1
    net_return = gross_return - (fee_bps * 2 / 10000)
    signal_date = str(bars[signal_idx].get("date") or "")
    entry_date = str(bars[entry_idx].get("date") or "")
    exit_date = str(bars[exit_idx].get("date") or "")
    return {
        "experiment": variant,
        "symbol": signal.get("symbol"),
        "name": signal.get("name"),
        "industry": signal.get("industry") or signal.get("sector") or "未分类",
        "signal_date": signal_date,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "signal_price": plan.get("signal_price"),
        "entry_price_raw": round(entry_price_raw, 3),
        "entry_price": round(entry_price, 3),
        "exit_price_raw": round(exit_price_raw, 3),
        "exit_price": round(exit_price, 3),
        "stop_loss": round(stop_loss, 3),
        "first_take_profit": round(first_take_profit, 3),
        "atr14": round(atr_value, 4) if atr_value is not None else None,
        "holding_days": exit_idx - entry_idx + 1,
        "exit_reason": exit_reason,
        "gross_return": round(gross_return, 5),
        "net_return": round(net_return, 5),
        "fee_bps_per_side": fee_bps,
        "slippage_bps_per_side": slippage_bps,
        "blocked_exit_days": blocked_exit_days,
        "blocked_exit_original_reason": blocked_exit_reason,
        "same_day_stop_take": same_day_stop_take,
        "entry_block_reasons": "；".join(entry_block_reasons),
        "position_pct": position_pct,
        "rank_score": signal.get("alpha040_core_score"),
        "alpha040": signal.get("alpha040"),
        "alpha040_z": signal.get("alpha040_z"),
        "rps60": signal.get("rps60"),
        "rps60_z": signal.get("rps60_z"),
        "close_to_20d_high": signal.get("close_to_20d_high"),
        "close_to_20d_high_z": signal.get("close_to_20d_high_z"),
        "upper_shadow_ratio": signal.get("upper_shadow_ratio"),
        "market_regime": (market_timeline.get(signal_date) or {}).get("regime"),
        "market_regime_label": (market_timeline.get(signal_date) or {}).get("regime_label"),
    }


def _run_variant_events(
    variant: str,
    universe_by_date: dict[str, list[dict]],
    histories: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    train_end: pd.Timestamp,
    max_hold_days: int,
    entry_window_days: int,
    fee_bps: float,
    slippage_bps: float,
    limit_threshold: float,
) -> pd.DataFrame:
    """运行单个风险控制版本的事件回测。"""
    trades: list[dict] = []
    next_available_by_symbol: dict[str, str] = {}
    for date in sorted(universe_by_date):
        market_profile = market_timeline.get(date) or {}
        candidate_limit = int(market_profile.get("candidate_limit") or 5)
        eligible = []
        for item in universe_by_date[date]:
            score = r3._alpha040_core_score(item)
            if score is None:
                continue
            eligible.append({**item, "alpha040_core_score": score})
        eligible.sort(key=lambda row: row.get("alpha040_core_score") or -999, reverse=True)
        for item in eligible[:candidate_limit]:
            symbol = str(item.get("_symbol_key") or item.get("symbol") or "")
            if not symbol or next_available_by_symbol.get(symbol, "") >= date:
                continue
            trade = _simulate_trade_variant(
                item,
                histories[symbol],
                int(item["_signal_idx"]),
                market_timeline,
                max_hold_days,
                entry_window_days,
                fee_bps,
                slippage_bps,
                limit_threshold,
                variant,
            )
            if not trade:
                continue
            trade["split"] = "train" if pd.to_datetime(trade["signal_date"]) <= train_end else "test"
            trades.append(trade)
            if trade.get("exit_date"):
                next_available_by_symbol[symbol] = str(trade["exit_date"])
    return pd.DataFrame(trades)


def _price_lookup(histories: dict[str, list[dict]]) -> dict[str, dict[str, float]]:
    """生成 symbol/date -> close 映射。"""
    result: dict[str, dict[str, float]] = {}
    for symbol, bars in histories.items():
        result[symbol] = {
            str(bar.get("date")): float(bar["close"])
            for bar in bars
            if bar.get("date") and _safe_float(bar.get("close")) is not None
        }
    return result


def _latest_close_on_or_before(price_map: dict[str, float], date: str) -> Optional[float]:
    """取某日期或之前最近收盘价。"""
    if date in price_map:
        return price_map[date]
    candidates = [key for key in price_map if key <= date]
    if not candidates:
        return None
    return price_map[max(candidates)]


def _accept_portfolio_trades(trades: pd.DataFrame, initial_cash: float) -> pd.DataFrame:
    """按持仓数和总敞口接受事件交易。"""
    if trades.empty:
        return trades.copy()
    max_positions = int(scanner._cfg("backtest", "portfolio_max_positions", 4))
    max_total_exposure = float(scanner._cfg("backtest", "portfolio_max_total_exposure_pct", 0.32))
    active: list[dict] = []
    rows: list[dict] = []
    for _, row in trades.sort_values(["entry_date", "rank_score"], ascending=[True, False]).iterrows():
        entry_date = str(row["entry_date"])
        active = [item for item in active if item["exit_date"] >= entry_date]
        current_exposure = sum(item["portfolio_position_pct"] for item in active)
        requested = (_safe_float(row.get("position_pct")) or 8.0) / 100
        actual = min(requested, max_total_exposure - current_exposure)
        if len(active) >= max_positions or actual <= 0:
            rows.append({**row.to_dict(), "portfolio_action": "skipped_capacity", "portfolio_position_pct": 0.0, "position_value": 0.0})
            continue
        position_value = initial_cash * actual
        accepted = {
            **row.to_dict(),
            "portfolio_action": "accepted",
            "portfolio_position_pct": round(actual, 6),
            "position_value": round(position_value, 2),
        }
        rows.append(accepted)
        active.append({"exit_date": str(row["exit_date"]), "portfolio_position_pct": actual})
    return pd.DataFrame(rows)


def _portfolio_equity_curve(
    portfolio_trades: pd.DataFrame,
    histories: dict[str, list[dict]],
    all_dates: list[str],
    initial_cash: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """生成每日权益曲线、月度收益和组合指标。"""
    accepted = portfolio_trades[portfolio_trades["portfolio_action"] == "accepted"].copy() if not portfolio_trades.empty else pd.DataFrame()
    price_maps = _price_lookup(histories)
    daily_rows: list[dict] = []
    for date in all_dates:
        realized_cash = initial_cash
        active_value = 0.0
        holding_count = 0
        exposure = 0.0
        symbols: list[str] = []
        for _, trade in accepted.iterrows():
            entry_date = str(trade["entry_date"])
            exit_date = str(trade["exit_date"])
            position_value = _safe_float(trade.get("position_value")) or 0.0
            if date < entry_date:
                continue
            if date >= exit_date:
                realized_cash += position_value * (_safe_float(trade.get("net_return")) or 0.0)
                continue
            symbol = str(trade["symbol"])
            entry_price = _safe_float(trade.get("entry_price")) or _safe_float(trade.get("entry_price_raw")) or 0.0
            mark_price = _latest_close_on_or_before(price_maps.get(symbol, {}), date)
            if entry_price > 0 and mark_price is not None:
                active_value += position_value * (mark_price / entry_price)
            else:
                active_value += position_value
            holding_count += 1
            exposure += _safe_float(trade.get("portfolio_position_pct")) or 0.0
            symbols.append(symbol)
        committed_value = sum(
            (_safe_float(trade.get("position_value")) or 0.0)
            for _, trade in accepted.iterrows()
            if str(trade["entry_date"]) <= date < str(trade["exit_date"])
        )
        equity = realized_cash - committed_value + active_value
        daily_rows.append(
            {
                "date": date,
                "equity": round(equity, 2),
                "holding_count": holding_count,
                "gross_exposure_pct": round(exposure, 6),
                "symbols": "；".join(symbols),
            }
        )
    equity_df = pd.DataFrame(daily_rows)
    equity_df["daily_return"] = equity_df["equity"].pct_change().fillna(0.0)
    equity_df["cum_return"] = equity_df["equity"] / initial_cash - 1
    equity_df["peak_equity"] = equity_df["equity"].cummax()
    equity_df["drawdown"] = equity_df["equity"] / equity_df["peak_equity"] - 1

    monthly = equity_df.copy()
    monthly["month"] = pd.to_datetime(monthly["date"]).dt.strftime("%Y-%m")
    monthly_rows = []
    prev_equity = initial_cash
    for month, group in monthly.groupby("month"):
        end_equity = float(group.iloc[-1]["equity"])
        monthly_rows.append(
            {
                "month": month,
                "start_equity": round(prev_equity, 2),
                "end_equity": round(end_equity, 2),
                "monthly_return": round(end_equity / prev_equity - 1, 6) if prev_equity else None,
            }
        )
        prev_equity = end_equity
    monthly_df = pd.DataFrame(monthly_rows)

    ending_equity = float(equity_df.iloc[-1]["equity"]) if not equity_df.empty else initial_cash
    day_count = max(1, len(equity_df))
    total_return = ending_equity / initial_cash - 1
    annualized = (ending_equity / initial_cash) ** (252 / day_count) - 1 if ending_equity > 0 else None
    daily_returns = pd.to_numeric(equity_df["daily_return"], errors="coerce").dropna()
    sharpe = None
    if len(daily_returns) > 1 and daily_returns.std(ddof=1) != 0:
        sharpe = float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252))
    max_dd = float(equity_df["drawdown"].min()) if not equity_df.empty else 0.0
    trough_idx = int(equity_df["drawdown"].idxmin()) if not equity_df.empty else 0
    peak_idx = int(equity_df.loc[:trough_idx, "equity"].idxmax()) if not equity_df.empty else 0
    dd_start = str(equity_df.loc[peak_idx, "date"]) if not equity_df.empty else None
    dd_end = str(equity_df.loc[trough_idx, "date"]) if not equity_df.empty else None
    calmar = float(annualized / abs(max_dd)) if annualized is not None and max_dd < 0 else None
    metrics = {
        "initial_cash": initial_cash,
        "ending_equity": round(ending_equity, 2),
        "total_return": round(total_return, 6),
        "annualized_return": round(float(annualized), 6) if annualized is not None else None,
        "max_drawdown": round(max_dd, 6),
        "sharpe": round(sharpe, 6) if sharpe is not None else None,
        "calmar": round(calmar, 6) if calmar is not None else None,
        "max_drawdown_start": dd_start,
        "max_drawdown_end": dd_end,
        "accepted_trades": int((portfolio_trades["portfolio_action"] == "accepted").sum()) if not portfolio_trades.empty else 0,
        "skipped_trades": int((portfolio_trades["portfolio_action"] == "skipped_capacity").sum()) if not portfolio_trades.empty else 0,
        "max_holding_count": int(equity_df["holding_count"].max()) if not equity_df.empty else 0,
        "max_exposure_pct": round(float(equity_df["gross_exposure_pct"].max()), 6) if not equity_df.empty else 0.0,
    }
    return equity_df, monthly_df, metrics


def _summarize_trade_group(rows: pd.DataFrame) -> dict[str, Any]:
    """按交易收益汇总。"""
    if rows.empty:
        return {
            "trade_count": 0,
            "win_rate": None,
            "average_return": None,
            "median_return": None,
            "max_loss": None,
            "max_drawdown": None,
        }
    returns = pd.to_numeric(rows["net_return"], errors="coerce").dropna()
    equity = (1 + returns).cumprod() if len(returns) else pd.Series(dtype=float)
    peak = equity.cummax() if len(equity) else pd.Series(dtype=float)
    drawdown = equity / peak - 1 if len(equity) else pd.Series(dtype=float)
    return {
        "trade_count": int(len(returns)),
        "win_rate": round(float((returns > 0).mean()), 4) if len(returns) else None,
        "average_return": round(float(returns.mean()), 6) if len(returns) else None,
        "median_return": round(float(returns.median()), 6) if len(returns) else None,
        "max_loss": round(float(returns.min()), 6) if len(returns) else None,
        "max_drawdown": round(float(drawdown.min()), 6) if len(drawdown) else None,
    }


def _exit_reason_summary(trades: pd.DataFrame) -> pd.DataFrame:
    """退出原因分组，补齐零样本类别。"""
    rows = []
    expected = ["take_profit", "stop_loss", "timeout", "time_stop", "limit_down_blocked_exit", "same_day_stop_take_conservative"]
    for reason in expected:
        rows.append({"exit_reason": reason, **_summarize_trade_group(trades[trades["exit_reason"] == reason])})
    return pd.DataFrame(rows)


def _market_summary(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """市场环境分组和开仓风控建议。"""
    expected_labels = ["积极", "中性", "谨慎", "防守"]
    rows = []
    policy = []
    for label in expected_labels:
        group = trades[trades["market_regime_label"] == label]
        summary = _summarize_trade_group(group)
        rows.append({"market_regime_label": label, **summary})
        enough_sample = summary["trade_count"] >= 10
        avg_ret = summary["average_return"]
        median_ret = summary["median_return"]
        win_rate = summary["win_rate"]
        max_dd = summary["max_drawdown"]
        should_ban = enough_sample and (
            (avg_ret is not None and avg_ret < 0 and win_rate is not None and win_rate < 0.45)
            or (avg_ret is not None and avg_ret <= 0 and max_dd is not None and max_dd < -0.35)
        )
        should_reduce = enough_sample and not should_ban and (
            (median_ret is not None and median_ret < 0 and avg_ret is not None and avg_ret > 0)
            or (win_rate is not None and win_rate < 0.45)
            or (max_dd is not None and max_dd < -0.25)
        )
        if should_ban:
            action = "forbid_open"
            reason = "均值为负且胜率偏低，或负收益环境伴随深回撤"
        elif should_reduce:
            action = "reduce_or_tighten"
            reason = "收益分布不够稳，建议降仓或收紧过滤后再观察"
        else:
            action = "allow_observe"
            reason = "暂不禁止；继续观察样本"
        policy.append(
            {
                "market_regime_label": label,
                "trade_count": summary["trade_count"],
                "should_forbid_open": bool(should_ban),
                "risk_action": action,
                "reason": reason,
            }
        )
    return pd.DataFrame(rows), pd.DataFrame(policy)


def _max_losing_streak_detail(trades: pd.DataFrame) -> dict[str, Any]:
    """输出最大连续亏损详情。"""
    sorted_trades = trades.sort_values(["exit_date", "entry_date", "symbol"]).reset_index(drop=True)
    best_start = best_end = -1
    current_start = 0
    best_len = current_len = 0
    for idx, row in sorted_trades.iterrows():
        ret = _safe_float(row.get("net_return")) or 0.0
        if ret < 0:
            if current_len == 0:
                current_start = idx
            current_len += 1
            if current_len > best_len:
                best_len = current_len
                best_start = current_start
                best_end = idx
        else:
            current_len = 0
    if best_len == 0:
        return {"max_consecutive_losses": 0, "trades": []}
    streak = sorted_trades.iloc[best_start: best_end + 1].copy()
    env_counts = Counter(str(x) for x in streak.get("market_regime_label", []))
    industry_counts = Counter(str(x) for x in streak.get("industry", []))
    symbol_counts = Counter(str(x) for x in streak.get("symbol", []))
    return {
        "max_consecutive_losses": int(best_len),
        "start_date": str(streak.iloc[0]["exit_date"]),
        "end_date": str(streak.iloc[-1]["exit_date"]),
        "market_regime_counts": dict(env_counts),
        "industry_counts": dict(industry_counts),
        "symbol_counts": dict(symbol_counts),
        "is_industry_concentrated": bool(industry_counts and max(industry_counts.values()) / best_len >= 0.6),
        "is_symbol_concentrated": bool(symbol_counts and max(symbol_counts.values()) / best_len >= 0.4),
        "trades": streak[
            ["symbol", "name", "industry", "entry_date", "exit_date", "market_regime_label", "exit_reason", "net_return"]
        ].to_dict("records"),
    }


def _universe_zero_audit(daily_universe: pd.DataFrame, output_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """分析 daily universe 为 0 的日期原因，并输出数量统计。"""
    zero_rows = []
    for _, row in daily_universe[daily_universe["stock_count"] == 0].iterrows():
        reason_values = {
            "data_missing": int(row.get("no_bar_on_date", 0)) + int(row.get("suspended_or_invalid_bar", 0)),
            "filter_too_strict": int(row.get("turnover_below_min", 0)) + int(row.get("price_below_min", 0)) + int(row.get("limit_up_cannot_buy", 0)) + int(row.get("limit_down_not_openable", 0)),
            "cache_warmup_or_coverage": int(row.get("insufficient_history", 0)),
        }
        primary = max(reason_values, key=reason_values.get)
        zero_rows.append(
            {
                "date": row["date"],
                "considered_symbols": row.get("considered_symbols"),
                "primary_reason": primary,
                **reason_values,
                "reason_counts_json": row.get("reason_counts_json"),
            }
        )
    zero_df = pd.DataFrame(zero_rows)
    distribution = daily_universe["stock_count"].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).reset_index()
    distribution.columns = ["metric", "stock_count"]
    _write_csv(output_dir / "universe_zero_dates.csv", zero_df)
    _write_csv(output_dir / "universe_count_distribution.csv", distribution)
    return zero_df, distribution


def _run_round4(
    *,
    output_dir: Path,
    cache_dir: Path,
    max_symbols: Optional[int],
    min_bars: int,
    min_history: int,
    train_ratio: float,
    strategy_path: str,
    fee_bps: float,
    slippage_bps: float,
    limit_threshold: float,
) -> dict[str, Any]:
    """执行 Round4。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    scanner._set_active_config(scanner._load_scanner_config(strategy_path))
    histories = fr.load_cached_histories(cache_dir=cache_dir, max_symbols=max_symbols, min_bars=min_bars)
    if not histories:
        raise RuntimeError(f"未找到可用历史 K 线缓存：{cache_dir}")
    metadata = r3._load_scan_metadata()
    alpha040_map = r3._build_alpha040_map(histories)
    universe_by_date, daily_universe = r3._build_dynamic_universe(histories, metadata, alpha040_map, min_history, output_dir)
    active_dates = [date for date, rows in universe_by_date.items() if rows]
    train_end, test_start = fr._split_dates(pd.Series(pd.to_datetime(active_dates)), train_ratio=train_ratio)
    market_timeline = r3._market_timeline()
    max_hold_days = int(scanner._cfg("backtest", "max_hold_days", 5))
    entry_window_days = int(scanner._cfg("backtest", "entry_window_days", 2))
    variants = {
        "current_stop": "A 当前止损",
        "atr_stop": "B ATR 止损",
        "atr_stop_risk_budget": "C ATR 止损 + 风险预算仓位",
    }
    all_trade_frames = []
    portfolio_metrics: dict[str, dict] = {}
    primary_outputs: dict[str, Any] = {}
    initial_cash = float(scanner._cfg("backtest", "portfolio_initial_cash", 1_000_000))
    for variant, label in variants.items():
        trades = _run_variant_events(
            variant,
            universe_by_date,
            histories,
            market_timeline,
            train_end,
            max_hold_days,
            entry_window_days,
            fee_bps,
            slippage_bps,
            limit_threshold,
        )
        trades["experiment_label"] = label
        all_trade_frames.append(trades)
        portfolio = _accept_portfolio_trades(trades, initial_cash)
        equity, monthly, metrics = _portfolio_equity_curve(portfolio, histories, active_dates, initial_cash)
        portfolio_metrics[variant] = {**metrics, "experiment_label": label}
        prefix = f"{variant}_"
        _write_csv(output_dir / f"{prefix}trades.csv", trades)
        _write_csv(output_dir / f"{prefix}portfolio_trades.csv", portfolio)
        _write_csv(output_dir / f"{prefix}daily_equity.csv", equity)
        _write_csv(output_dir / f"{prefix}monthly_returns.csv", monthly)
        if variant == "current_stop":
            _write_csv(output_dir / "daily_equity.csv", equity)
            _write_csv(output_dir / "monthly_returns.csv", monthly)
            _write_csv(output_dir / "portfolio_trades.csv", portfolio)
            primary_outputs = {
                "trades": trades,
                "portfolio": portfolio,
                "equity": equity,
                "monthly": monthly,
                "metrics": metrics,
            }
    all_trades = pd.concat(all_trade_frames, ignore_index=True) if all_trade_frames else pd.DataFrame()
    _write_csv(output_dir / "risk_experiment_trades.csv", all_trades)
    risk_summary = pd.DataFrame(portfolio_metrics).T.reset_index().rename(columns={"index": "experiment"})
    _write_csv(output_dir / "risk_experiment_summary.csv", risk_summary)

    primary_trades = primary_outputs["trades"]
    exit_summary = _exit_reason_summary(primary_trades)
    market_summary, market_policy = _market_summary(primary_trades)
    losing_streak = _max_losing_streak_detail(primary_trades)
    zero_universe, universe_dist = _universe_zero_audit(daily_universe, output_dir)
    _write_csv(output_dir / "exit_reason_summary.csv", exit_summary)
    _write_csv(output_dir / "market_regime_summary.csv", market_summary)
    _write_csv(output_dir / "market_regime_open_policy.csv", market_policy)
    _write_json(output_dir / "max_losing_streak_detail.json", losing_streak)
    _write_json(output_dir / "portfolio_metrics.json", primary_outputs["metrics"])

    return {
        "histories": histories,
        "daily_universe": daily_universe,
        "active_dates": active_dates,
        "train_end": train_end,
        "test_start": test_start,
        "portfolio_metrics": primary_outputs["metrics"],
        "risk_summary": risk_summary,
        "exit_summary": exit_summary,
        "market_summary": market_summary,
        "market_policy": market_policy,
        "losing_streak": losing_streak,
        "zero_universe": zero_universe,
        "universe_dist": universe_dist,
        "primary_trades": primary_trades,
        "initial_cash": initial_cash,
    }


def _render_report(output_dir: Path, ctx: dict[str, Any]) -> str:
    """渲染 Round4 报告。"""
    metrics = ctx["portfolio_metrics"]
    risk_summary = ctx["risk_summary"]
    exit_summary = ctx["exit_summary"]
    market_summary = ctx["market_summary"]
    market_policy = ctx["market_policy"]
    losing = ctx["losing_streak"]
    zero_df = ctx["zero_universe"]
    lines = [
        "# Factor Research Round4 Report",
        "",
        f"生成日期：{datetime.now().strftime('%Y-%m-%d')}",
        f"时间切分：train <= {ctx['train_end'].strftime('%Y-%m-%d')}，test >= {ctx['test_start'].strftime('%Y-%m-%d')}",
        "",
        "免责声明：本报告仅用于个人模拟交易、复盘和策略研究；不构成投资建议，不修改主 market_scanner 策略，不连接实盘接口。",
        "",
        "## 1. 组合层面指标（当前止损）",
        "",
        f"- 初始资金：{metrics.get('initial_cash'):,.2f}",
        f"- 期末权益：{metrics.get('ending_equity'):,.2f}",
        f"- 累计收益：{_fmt_pct(metrics.get('total_return'))}",
        f"- 年化收益：{_fmt_pct(metrics.get('annualized_return'))}",
        f"- 最大回撤：{_fmt_pct(metrics.get('max_drawdown'))}",
        f"- 夏普：{metrics.get('sharpe')}",
        f"- 卡玛：{metrics.get('calmar')}",
        f"- 最大回撤区间：{metrics.get('max_drawdown_start')} 至 {metrics.get('max_drawdown_end')}",
        "- 每日权益曲线：`daily_equity.csv`；月度收益：`monthly_returns.csv`。",
        "",
        "## 2. 退出原因分组",
        "",
        "| 退出原因 | 交易数 | 胜率 | 平均单笔 | 中位数 | 最大亏损 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for _, row in exit_summary.iterrows():
        lines.append(
            f"| {row['exit_reason']} | {int(row['trade_count'])} | {_fmt_pct(row.get('win_rate'))} | "
            f"{_fmt_pct(row.get('average_return'))} | {_fmt_pct(row.get('median_return'))} | {_fmt_pct(row.get('max_loss'))} |"
        )
    lines.extend(["", "## 3. 市场环境分组与开仓建议", "", "| 环境 | 交易数 | 胜率 | 平均单笔 | 中位数 | 最大回撤 | 建议 |", "|---|---:|---:|---:|---:|---:|---|"])
    policy_map = {row["market_regime_label"]: row for _, row in market_policy.iterrows()}
    action_labels = {
        "forbid_open": "禁止开仓",
        "reduce_or_tighten": "降仓/收紧风控",
        "allow_observe": "允许但继续观察",
    }
    for _, row in market_summary.iterrows():
        policy = policy_map.get(row["market_regime_label"], {})
        lines.append(
            f"| {row['market_regime_label']} | {int(row['trade_count'])} | {_fmt_pct(row.get('win_rate'))} | "
            f"{_fmt_pct(row.get('average_return'))} | {_fmt_pct(row.get('median_return'))} | {_fmt_pct(row.get('max_drawdown'))} | "
            f"{action_labels.get(policy.get('risk_action'), '允许但继续观察')} |"
        )
    lines.extend(["", "## 4. 风险控制 Shadow 实验", "", "| 实验 | 期末权益 | 累计收益 | 最大回撤 | 夏普 | 卡玛 | 接受交易 |", "|---|---:|---:|---:|---:|---:|---:|"])
    for _, row in risk_summary.iterrows():
        lines.append(
            f"| {row['experiment_label']} | {row['ending_equity']:,.2f} | {_fmt_pct(row.get('total_return'))} | "
            f"{_fmt_pct(row.get('max_drawdown'))} | {row.get('sharpe')} | {row.get('calmar')} | {int(row.get('accepted_trades') or 0)} |"
        )
    lines.extend(
        [
            "",
            "## 5. 最大连续亏损详情",
            "",
            f"- 连亏笔数：{losing.get('max_consecutive_losses')}",
            f"- 发生时间：{losing.get('start_date')} 至 {losing.get('end_date')}",
            f"- 市场环境分布：{losing.get('market_regime_counts')}",
            f"- 行业/板块分布：{losing.get('industry_counts')}",
            f"- 是否集中同一行业/主题：{losing.get('is_industry_concentrated')}",
            "- 明细：`max_losing_streak_detail.json`。",
            "",
            "## 6. Universe 为 0 的日期",
            "",
            f"- universe 为 0 的日期数：{len(zero_df)}",
            "- 主要原因见 `universe_zero_dates.csv`；数量分布见 `universe_count_distribution.csv`。",
            "- 若主要原因为 `cache_warmup_or_coverage`，说明前期历史长度不足；若为 `filter_too_strict`，说明成交额/价格/涨跌停过滤过严；若为 `data_missing`，说明当日 K 线缺失或停牌。",
            "",
            "## 7. 结论边界",
            "",
            "- Round4 只做风险归因和组合增强回测，不新增复杂因子，不修改主策略。",
            "- ATR 止损和风险预算仓位只作为 shadow evidence；是否升级需要更长样本和人工确认。",
        ]
    )
    report = "\n".join(lines) + "\n"
    (output_dir / "factor_research_round4_report.md").write_text(report, encoding="utf-8")
    return report


def run_factor_research_round4(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = fr.DEFAULT_CACHE_DIR,
    max_symbols: Optional[int] = None,
    min_bars: int = 80,
    min_history: int = 60,
    train_ratio: float = 0.7,
    strategy_path: str = "strategy.json",
    fee_bps: float = 5.0,
    slippage_bps: float = 10.0,
    limit_threshold: float = r3.DEFAULT_LIMIT_THRESHOLD,
) -> dict[str, Any]:
    """运行 Round4 风险归因与组合增强回测。"""
    ctx = _run_round4(
        output_dir=output_dir,
        cache_dir=cache_dir,
        max_symbols=max_symbols,
        min_bars=min_bars,
        min_history=min_history,
        train_ratio=train_ratio,
        strategy_path=strategy_path,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        limit_threshold=limit_threshold,
    )
    report = _render_report(output_dir, ctx)
    summary = {
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "module": "factor_research_round4_risk_attribution_shadow",
        "safety": "不修改主 market_scanner 策略、不写台账、不连接实盘接口。",
        "history_symbol_count": len(ctx["histories"]),
        "active_date_count": len(ctx["active_dates"]),
        "train_end": ctx["train_end"].strftime("%Y-%m-%d"),
        "test_start": ctx["test_start"].strftime("%Y-%m-%d"),
        "initial_cash": ctx["initial_cash"],
        "portfolio_metrics": ctx["portfolio_metrics"],
        "trade_count": int(len(ctx["primary_trades"])),
        "output_dir": str(output_dir),
        "report_path": str(output_dir / "factor_research_round4_report.md"),
        "report_characters": len(report),
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行 Round4 风险归因与组合增强 shadow 回测")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--cache-dir", default=str(fr.DEFAULT_CACHE_DIR), help="股票历史 K 缓存目录")
    parser.add_argument("--max-symbols", type=int, help="最多读取多少只股票缓存")
    parser.add_argument("--min-bars", type=int, default=80, help="单票最少 K 线数量")
    parser.add_argument("--min-history", type=int, default=60, help="动态 universe 最少历史长度")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="按时间顺序训练段比例")
    parser.add_argument("--strategy", default="strategy.json", help="策略配置文件")
    parser.add_argument("--fee-bps", type=float, default=5.0, help="单边手续费 bp")
    parser.add_argument("--slippage-bps", type=float, default=10.0, help="单边滑点 bp")
    parser.add_argument("--limit-threshold", type=float, default=r3.DEFAULT_LIMIT_THRESHOLD, help="涨跌停近似阈值")
    args = parser.parse_args()
    summary = run_factor_research_round4(
        output_dir=Path(args.output),
        cache_dir=Path(args.cache_dir),
        max_symbols=args.max_symbols,
        min_bars=args.min_bars,
        min_history=args.min_history,
        train_ratio=args.train_ratio,
        strategy_path=args.strategy,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        limit_threshold=args.limit_threshold,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
