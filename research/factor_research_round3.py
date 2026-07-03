"""
factor_research_round3.py - Alpha040 Core shadow strategy

输出：output/factor_research_round3/
本模块只读本地历史缓存和策略配置，不修改 market_scanner 主策略、不写台账、不连接实盘。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
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
import market_scanner as scanner

DEFAULT_OUTPUT_DIR = Path("output/factor_research_round3")
DEFAULT_LIMIT_THRESHOLD = 0.095


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


def _load_scan_metadata(output_base: Path = Path("output")) -> dict[str, dict]:
    """从已有扫描产物补充名称和行业/板块映射。"""
    result: dict[str, dict] = {}
    for path in sorted(output_base.glob("*/scan/daily_scans.csv")):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                symbol = row.get("symbol")
                if symbol:
                    result[symbol] = {
                        "name": row.get("name") or symbol,
                        "sector": row.get("sector") or "未分类",
                    }
    return result


def _prev_close_from_bars(bars: list[dict], idx: int) -> Optional[float]:
    """取前收盘价。"""
    if idx <= 0:
        return None
    return _safe_float(bars[idx - 1].get("close"))


def _is_limit_up(bar: dict, prev_close: Optional[float], threshold: float = DEFAULT_LIMIT_THRESHOLD) -> bool:
    """近似判断涨停无法买入。"""
    if prev_close is None or prev_close <= 0:
        return False
    low = _safe_float(bar.get("low"))
    close = _safe_float(bar.get("close"))
    if low is None or close is None:
        return False
    return low >= prev_close * (1 + threshold) or close >= prev_close * (1 + threshold)


def _is_limit_down(bar: dict, prev_close: Optional[float], threshold: float = DEFAULT_LIMIT_THRESHOLD) -> bool:
    """近似判断跌停无法卖出。"""
    if prev_close is None or prev_close <= 0:
        return False
    high = _safe_float(bar.get("high"))
    close = _safe_float(bar.get("close"))
    if high is None or close is None:
        return False
    return high <= prev_close * (1 - threshold) or close <= prev_close * (1 - threshold)


def _is_suspended_or_untradeable(bar: Optional[dict]) -> bool:
    """判断停牌或不可交易。"""
    if not bar:
        return True
    volume = _safe_float(bar.get("volume"))
    turnover = _safe_float(bar.get("turnover"))
    open_price = _safe_float(bar.get("open"))
    high = _safe_float(bar.get("high"))
    low = _safe_float(bar.get("low"))
    close = _safe_float(bar.get("close"))
    if open_price is None or high is None or low is None or close is None:
        return True
    if volume is not None and volume <= 0:
        return True
    if turnover is not None and turnover <= 0:
        return True
    return False


def _upper_shadow_ratio(item: dict) -> Optional[float]:
    """上影线比例，仅作标签。"""
    high = _safe_float(item.get("high"))
    low = _safe_float(item.get("low"))
    close = _safe_float(item.get("latest") or item.get("close"))
    if high is None or low is None or close is None or high <= low:
        return None
    return (high - close) / (high - low)


def _standardize(values: list[Optional[float]]) -> list[Optional[float]]:
    """横截面去极值、缺失填充和 z-score。"""
    series = pd.Series(values, dtype="float64").replace([np.inf, -np.inf], np.nan)
    if series.notna().sum() == 0:
        return [None for _ in values]
    low = series.quantile(0.01)
    high = series.quantile(0.99)
    filled = series.clip(low, high).fillna(series.median())
    std = filled.std(ddof=0)
    if std == 0 or pd.isna(std):
        return [0.0 for _ in values]
    return [float(x) for x in ((filled - filled.mean()) / std)]


def _build_alpha040_map(histories: dict[str, list[dict]]) -> dict[tuple[str, str], float]:
    """计算 alpha040 原始值映射。"""
    result: dict[tuple[str, str], float] = {}
    for symbol, bars in histories.items():
        dates: list[str] = []
        closes: list[Optional[float]] = []
        volumes: list[Optional[float]] = []
        for bar in bars:
            date = str(bar.get("date") or "")
            if not date:
                continue
            dates.append(date)
            closes.append(_safe_float(bar.get("close")))
            volumes.append(_safe_float(bar.get("volume")))
        if len(dates) < 27:
            continue
        close = pd.Series(closes, dtype="float64")
        volume = pd.Series(volumes, dtype="float64")
        prev_close = close.shift(1)
        up_volume = volume.where(close > prev_close, 0.0).rolling(26, min_periods=26).sum()
        down_volume = volume.where(close <= prev_close, 0.0).rolling(26, min_periods=26).sum()
        alpha040 = (up_volume / down_volume.replace(0, np.nan) * 100).replace([np.inf, -np.inf], np.nan)
        for date, value in zip(dates, alpha040):
            if pd.notna(value):
                result[(date, str(symbol))] = float(value)
    return result


def _date_index(histories: dict[str, list[dict]]) -> tuple[list[str], dict[str, dict[str, int]]]:
    """构建日期索引：date -> symbol -> bar index。"""
    by_date: dict[str, dict[str, int]] = defaultdict(dict)
    for symbol, bars in histories.items():
        for idx, bar in enumerate(bars):
            date = str(bar.get("date") or "")
            if date:
                by_date[date][symbol] = idx
    return sorted(by_date), by_date


def _base_row(symbol: str, metadata: dict[str, dict]) -> dict:
    """构造 scanner 伪快照元数据。"""
    meta = metadata.get(symbol) or {}
    return {
        "name": meta.get("name") or symbol,
        "symkey": symbol,
        "industry_sector": {"name": meta.get("sector") or "未分类"},
        "data_source": "round3_dynamic_universe",
    }


def _build_dynamic_universe(
    histories: dict[str, list[dict]],
    metadata: dict[str, dict],
    alpha040_map: dict[tuple[str, str], float],
    min_history: int,
    output_dir: Path,
) -> tuple[dict[str, list[dict]], pd.DataFrame]:
    """逐日动态生成可交易股票池和剔除原因统计。"""
    dates, by_date = _date_index(histories)
    first_date_by_symbol = {
        symbol: str(bars[0].get("date") or "")
        for symbol, bars in histories.items()
        if bars
    }
    active_symbols = sorted(histories)
    daily_rows: list[dict] = []
    universe_by_date: dict[str, list[dict]] = {}

    min_price = float(scanner._cfg("filters", "min_price", 3))
    min_turnover = float(scanner._cfg("filters", "min_turnover", 1e9))

    for date in dates:
        reason_counts: Counter[str] = Counter()
        rows: list[dict] = []
        considered = 0
        for symbol in active_symbols:
            first_date = first_date_by_symbol.get(symbol)
            if not first_date or first_date > date:
                continue
            considered += 1
            idx = by_date.get(date, {}).get(symbol)
            if idx is None:
                reason_counts["no_bar_on_date"] += 1
                continue
            bars = histories[symbol]
            bar = bars[idx]
            if idx < min_history:
                reason_counts["insufficient_history"] += 1
                continue
            if _is_suspended_or_untradeable(bar):
                reason_counts["suspended_or_invalid_bar"] += 1
                continue
            close = _safe_float(bar.get("close"))
            turnover = _safe_float(bar.get("turnover"))
            prev_close = _prev_close_from_bars(bars, idx)
            if close is None or close <= min_price:
                reason_counts["price_below_min"] += 1
                continue
            if turnover is None or turnover < min_turnover:
                reason_counts["turnover_below_min"] += 1
                continue
            if _is_limit_up(bar, prev_close):
                reason_counts["limit_up_cannot_buy"] += 1
                continue
            if _is_limit_down(bar, prev_close):
                reason_counts["limit_down_not_openable"] += 1
                continue
            row = scanner._signal_row_from_bar(_base_row(symbol, metadata), bars, idx)
            scored = scanner._score_stock(row)
            scored.update(
                {
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "prev_close": row.get("prev_close"),
                    "_signal_idx": idx,
                    "_symbol_key": symbol,
                    "_signal_date": date,
                    "alpha040": alpha040_map.get((date, symbol)),
                    "industry": (metadata.get(symbol) or {}).get("sector") or scored.get("sector") or "未分类",
                    "signal_day_limit_up": _is_limit_up(bar, prev_close),
                    "signal_day_limit_down": _is_limit_down(bar, prev_close),
                }
            )
            scored["upper_shadow_ratio"] = _upper_shadow_ratio(scored)
            rows.append(scored)

        rows = scanner._apply_strategy_overlays(rows) if rows else []
        alpha_z = _standardize([_safe_float(item.get("alpha040")) for item in rows])
        rps60_z = _standardize([_safe_float(item.get("rps60")) for item in rows])
        high_z = _standardize([_safe_float(item.get("close_to_20d_high")) for item in rows])
        for item, alpha_value, rps_value, high_value in zip(rows, alpha_z, rps60_z, high_z):
            item["alpha040_z"] = alpha_value
            item["rps60_z"] = rps_value
            item["close_to_20d_high_z"] = high_value
        universe_by_date[date] = rows
        total_excluded = sum(reason_counts.values())
        daily_rows.append(
            {
                "date": date,
                "considered_symbols": considered,
                "stock_count": len(rows),
                "excluded_total": total_excluded,
                "no_bar_on_date": reason_counts.get("no_bar_on_date", 0),
                "insufficient_history": reason_counts.get("insufficient_history", 0),
                "suspended_or_invalid_bar": reason_counts.get("suspended_or_invalid_bar", 0),
                "price_below_min": reason_counts.get("price_below_min", 0),
                "turnover_below_min": reason_counts.get("turnover_below_min", 0),
                "limit_up_cannot_buy": reason_counts.get("limit_up_cannot_buy", 0),
                "limit_down_not_openable": reason_counts.get("limit_down_not_openable", 0),
                "reason_counts_json": json.dumps(dict(reason_counts), ensure_ascii=False),
            }
        )

    daily_universe = pd.DataFrame(daily_rows)
    _write_csv(output_dir / "daily_universe.csv", daily_universe)
    return universe_by_date, daily_universe


def _market_timeline() -> dict[str, dict]:
    """读取指数缓存生成市场环境时间线。"""
    return fr._market_timeline_from_cache(fr.DEFAULT_INDEX_CACHE_DIR)


def _alpha040_core_score(item: dict) -> Optional[float]:
    """Alpha040 Core 排序分：不重复加权 rps/change_rate。"""
    alpha = _safe_float(item.get("alpha040_z"))
    rps60 = _safe_float(item.get("rps60_z"))
    high = _safe_float(item.get("close_to_20d_high_z"))
    latest = _safe_float(item.get("latest"))
    ma5 = _safe_float(item.get("ma5"))
    ma10 = _safe_float(item.get("ma10"))
    ma20 = _safe_float(item.get("ma20"))
    if alpha is None or rps60 is None or high is None:
        return None
    if latest is None or ma20 is None or latest < ma20:
        return None
    score = 0.55 * alpha + 0.30 * rps60 + 0.15 * high
    if ma5 is not None and ma10 is not None and ma5 >= ma10 >= ma20:
        score += 0.08
    elif ma5 is not None and ma5 >= ma20:
        score += 0.04
    distance = latest / ma20 - 1 if ma20 else 0.0
    if distance > 0.12:
        score -= min(0.35, (distance - 0.12) * 2.0)
    return round(score, 6)


def _can_sell(bar: dict, prev_close: Optional[float], limit_threshold: float) -> bool:
    """判断当天是否可卖。"""
    return not _is_limit_down(bar, prev_close, threshold=limit_threshold) and not _is_suspended_or_untradeable(bar)


def _simulate_alpha040_trade(
    signal: dict,
    bars: list[dict],
    signal_idx: int,
    market_timeline: dict[str, dict],
    max_hold_days: int,
    entry_window_days: int,
    fee_bps: float,
    slippage_bps: float,
    limit_threshold: float,
) -> Optional[dict]:
    """带交易成本与执行限制的 Alpha040 Core 事件回放。"""
    plan = scanner._build_trade_plan(signal)
    trigger_low, trigger_high = scanner._parse_trigger_zone(plan["trigger_zone"])
    entry_idx: Optional[int] = None
    entry_price_raw: Optional[float] = None
    entry_block_reasons: list[str] = []
    for idx in range(signal_idx + 1, min(signal_idx + 1 + entry_window_days, len(bars))):
        bar = bars[idx]
        prev_close = _prev_close_from_bars(bars, idx)
        if _is_suspended_or_untradeable(bar):
            entry_block_reasons.append("suspended_no_entry")
            continue
        if _is_limit_up(bar, prev_close, threshold=limit_threshold):
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
    stop_loss = float(plan["stop_loss"])
    first_take_profit = float(plan["first_take_profit"])
    max_gain = -1.0
    blocked_exit_days = 0
    exit_idx = min(entry_idx + max_hold_days - 1, len(bars) - 1)
    exit_price_raw = _safe_float(bars[exit_idx].get("close")) or entry_price_raw
    exit_reason = "timeout"

    for idx in range(entry_idx, len(bars)):
        bar = bars[idx]
        prev_close = _prev_close_from_bars(bars, idx)
        date = str(bar.get("date") or "")
        high = _safe_float(bar.get("high")) or entry_price_raw
        low = _safe_float(bar.get("low")) or entry_price_raw
        close = _safe_float(bar.get("close")) or entry_price_raw
        max_gain = max(max_gain, high / entry_price_raw - 1)
        holding_days = idx - entry_idx + 1
        can_sell = _can_sell(bar, prev_close, limit_threshold)

        desired_exit: Optional[tuple[str, float]] = None
        hit_stop = low <= stop_loss
        hit_take = high >= first_take_profit
        if hit_stop and hit_take:
            desired_exit = ("stop_loss", stop_loss)
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
            if idx < len(bars) - 1:
                continue
        exit_reason, exit_price_raw = desired_exit
        exit_idx = idx
        break

    exit_price = exit_price_raw * (1 - slippage_bps / 10000)
    gross_return = exit_price / entry_price - 1
    total_cost = fee_bps * 2 / 10000
    net_return = gross_return - total_cost
    signal_date = str(bars[signal_idx].get("date") or "")
    entry_date = str(bars[entry_idx].get("date") or "")
    exit_date = str(bars[exit_idx].get("date") or "")
    return {
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
        "stop_loss": plan.get("stop_loss"),
        "first_take_profit": plan.get("first_take_profit"),
        "holding_days": exit_idx - entry_idx + 1,
        "exit_reason": exit_reason,
        "gross_return": round(gross_return, 5),
        "net_return": round(net_return, 5),
        "fee_bps_per_side": fee_bps,
        "slippage_bps_per_side": slippage_bps,
        "blocked_exit_days": blocked_exit_days,
        "entry_block_reasons": "；".join(entry_block_reasons),
        "position_pct": plan.get("position_pct"),
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


def _select_and_backtest(
    universe_by_date: dict[str, list[dict]],
    histories: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    train_end: pd.Timestamp,
    max_hold_days: int,
    entry_window_days: int,
    fee_bps: float,
    slippage_bps: float,
    limit_threshold: float,
    output_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成 Alpha040 Core 信号并执行事件回测。"""
    trades: list[dict] = []
    selected_rows: list[dict] = []
    next_available_by_symbol: dict[str, str] = {}
    for date in sorted(universe_by_date):
        market_profile = market_timeline.get(date) or {}
        candidate_limit = int(market_profile.get("candidate_limit") or 5)
        eligible: list[dict] = []
        for item in universe_by_date[date]:
            score = _alpha040_core_score(item)
            if score is None:
                continue
            item = {**item, "alpha040_core_score": score}
            eligible.append(item)
        eligible.sort(key=lambda row: row.get("alpha040_core_score") or -999, reverse=True)
        for item in eligible[:candidate_limit]:
            symbol = str(item.get("_symbol_key") or item.get("symbol") or "")
            if not symbol or next_available_by_symbol.get(symbol, "") >= date:
                continue
            selected_rows.append(
                {
                    "signal_date": date,
                    "symbol": item.get("symbol"),
                    "name": item.get("name"),
                    "industry": item.get("industry"),
                    "alpha040_core_score": item.get("alpha040_core_score"),
                    "alpha040_z": item.get("alpha040_z"),
                    "rps60_z": item.get("rps60_z"),
                    "close_to_20d_high_z": item.get("close_to_20d_high_z"),
                    "upper_shadow_ratio": item.get("upper_shadow_ratio"),
                    "market_regime": market_profile.get("regime"),
                    "market_regime_label": market_profile.get("regime_label"),
                }
            )
            trade = _simulate_alpha040_trade(
                item,
                histories[symbol],
                int(item["_signal_idx"]),
                market_timeline,
                max_hold_days=max_hold_days,
                entry_window_days=entry_window_days,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                limit_threshold=limit_threshold,
            )
            if not trade:
                continue
            trade["split"] = "train" if pd.to_datetime(trade["signal_date"]) <= train_end else "test"
            trades.append(trade)
            if trade.get("exit_date"):
                next_available_by_symbol[symbol] = str(trade["exit_date"])
    trades_df = pd.DataFrame(trades)
    selected_df = pd.DataFrame(selected_rows)
    _write_csv(output_dir / "alpha040_core_trades.csv", trades_df)
    _write_csv(output_dir / "alpha040_core_selected_signals.csv", selected_df)
    return trades_df, selected_df


def _summarize_returns(rows: pd.DataFrame) -> dict[str, Any]:
    """汇总交易收益。"""
    if rows.empty or "net_return" not in rows.columns:
        return {
            "trade_count": 0,
            "win_rate": None,
            "average_return": None,
            "median_return": None,
            "best_trade_return": None,
            "worst_trade_return": None,
            "max_consecutive_losses": 0,
            "single_symbol_worst_return": None,
        }
    returns = pd.to_numeric(rows["net_return"], errors="coerce").dropna()
    max_streak = 0
    current_streak = 0
    for ret in returns:
        if ret < 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    return {
        "trade_count": int(len(returns)),
        "win_rate": round(float((returns > 0).mean()), 4) if len(returns) else None,
        "average_return": round(float(returns.mean()), 5) if len(returns) else None,
        "median_return": round(float(returns.median()), 5) if len(returns) else None,
        "best_trade_return": round(float(returns.max()), 5) if len(returns) else None,
        "worst_trade_return": round(float(returns.min()), 5) if len(returns) else None,
        "max_consecutive_losses": int(max_streak),
        "single_symbol_worst_return": round(float(returns.min()), 5) if len(returns) else None,
    }


def _group_summary(trades: pd.DataFrame, group_field: str, output_path: Path) -> pd.DataFrame:
    """按字段分组汇总。"""
    rows = []
    if not trades.empty and group_field in trades.columns:
        for value, group in trades.groupby(group_field, dropna=False):
            rows.append({group_field: value if value else "未知", **_summarize_returns(group)})
    df = pd.DataFrame(rows).sort_values("trade_count", ascending=False) if rows else pd.DataFrame()
    _write_csv(output_path, df)
    return df


def _portfolio_outputs(
    trades: pd.DataFrame,
    all_dates: list[str],
    output_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """组合容量、月度收益和每日敞口输出。"""
    if trades.empty:
        empty = pd.DataFrame()
        _write_csv(output_dir / "portfolio_trades.csv", empty)
        _write_csv(output_dir / "monthly_returns.csv", empty)
        _write_csv(output_dir / "daily_positions.csv", empty)
        return empty, empty, empty

    max_positions = int(scanner._cfg("backtest", "portfolio_max_positions", 4))
    max_total_exposure = float(scanner._cfg("backtest", "portfolio_max_total_exposure_pct", 0.32))
    accepted: list[dict] = []
    skipped: list[dict] = []
    active: list[dict] = []
    for _, row in trades.sort_values(["entry_date", "rank_score"], ascending=[True, False]).iterrows():
        entry_date = str(row["entry_date"])
        active = [item for item in active if item["exit_date"] >= entry_date]
        current_exposure = sum(item["position_pct"] for item in active)
        position_pct = (_safe_float(row.get("position_pct")) or 8.0) / 100
        allowed_exposure = max_total_exposure - current_exposure
        actual_position = min(position_pct, allowed_exposure)
        if len(active) >= max_positions or actual_position <= 0:
            skipped.append({**row.to_dict(), "portfolio_action": "skipped_capacity", "portfolio_position_pct": 0.0})
            continue
        accepted_row = {**row.to_dict(), "portfolio_action": "accepted", "portfolio_position_pct": round(actual_position, 4)}
        accepted.append(accepted_row)
        active.append(
            {
                "symbol": row["symbol"],
                "entry_date": entry_date,
                "exit_date": str(row["exit_date"]),
                "position_pct": actual_position,
                "net_return": _safe_float(row.get("net_return")) or 0.0,
            }
        )

    portfolio = pd.DataFrame([*accepted, *skipped])
    _write_csv(output_dir / "portfolio_trades.csv", portfolio)

    monthly_rows = []
    accepted_df = pd.DataFrame(accepted)
    if not accepted_df.empty:
        accepted_df["month"] = pd.to_datetime(accepted_df["exit_date"]).dt.strftime("%Y-%m")
        accepted_df["weighted_return"] = pd.to_numeric(accepted_df["net_return"], errors="coerce") * pd.to_numeric(
            accepted_df["portfolio_position_pct"], errors="coerce"
        )
        for month, group in accepted_df.groupby("month"):
            monthly_rows.append(
                {
                    "month": month,
                    "trade_count": int(len(group)),
                    "weighted_return": round(float(group["weighted_return"].sum()), 6),
                    "average_trade_return": round(float(pd.to_numeric(group["net_return"], errors="coerce").mean()), 6),
                }
            )
    monthly = pd.DataFrame(monthly_rows)
    _write_csv(output_dir / "monthly_returns.csv", monthly)

    daily_rows = []
    accepted_records = accepted_df.to_dict("records") if not accepted_df.empty else []
    for date in all_dates:
        active_positions = [
            row for row in accepted_records
            if str(row.get("entry_date")) <= date <= str(row.get("exit_date"))
        ]
        exposure = sum(_safe_float(row.get("portfolio_position_pct")) or 0.0 for row in active_positions)
        daily_rows.append(
            {
                "date": date,
                "holding_count": len(active_positions),
                "gross_exposure_pct": round(exposure, 4),
                "symbols": "；".join(str(row.get("symbol")) for row in active_positions),
            }
        )
    daily_positions = pd.DataFrame(daily_rows)
    _write_csv(output_dir / "daily_positions.csv", daily_positions)
    return portfolio, monthly, daily_positions


def _build_all_summaries(
    trades: pd.DataFrame,
    market_timeline: dict[str, dict],
    all_dates: list[str],
    output_dir: Path,
) -> dict[str, Any]:
    """生成全部分组绩效输出。"""
    split_rows = []
    for split in ["all", "train", "test"]:
        group = trades if split == "all" else trades[trades["split"] == split]
        split_rows.append({"split": split, **_summarize_returns(group)})
    split_summary = pd.DataFrame(split_rows)
    _write_csv(output_dir / "train_test_performance.csv", split_summary)

    market_summary = _group_summary(trades, "market_regime_label", output_dir / "market_regime_performance.csv")
    industry_summary = _group_summary(trades, "industry", output_dir / "industry_performance.csv")
    exit_summary = _group_summary(trades, "exit_reason", output_dir / "exit_reason_performance.csv")
    if exit_summary.empty:
        exit_summary = pd.DataFrame(columns=["exit_reason", "trade_count"])
    present_reasons = set(exit_summary["exit_reason"].astype(str)) if "exit_reason" in exit_summary.columns else set()
    missing_reason_rows = []
    for reason in ["take_profit", "stop_loss", "time_stop", "timeout", "environment_exit"]:
        if reason not in present_reasons:
            missing_reason_rows.append(
                {
                    "exit_reason": reason,
                    "trade_count": 0,
                    "win_rate": None,
                    "average_return": None,
                    "median_return": None,
                    "best_trade_return": None,
                    "worst_trade_return": None,
                    "max_consecutive_losses": 0,
                    "single_symbol_worst_return": None,
                }
            )
    if missing_reason_rows:
        exit_records = exit_summary.to_dict("records") if not exit_summary.empty else []
        exit_summary = pd.DataFrame([*exit_records, *missing_reason_rows])
        _write_csv(output_dir / "exit_reason_performance.csv", exit_summary)
    symbol_loss_rows = []
    if not trades.empty:
        for symbol, group in trades.groupby("symbol", dropna=False):
            returns = pd.to_numeric(group["net_return"], errors="coerce").dropna()
            if returns.empty:
                continue
            worst_idx = returns.idxmin()
            worst_row = trades.loc[worst_idx]
            symbol_loss_rows.append(
                {
                    "symbol": symbol,
                    "name": worst_row.get("name"),
                    "trade_count": int(len(group)),
                    "worst_net_return": round(float(returns.min()), 6),
                    "average_net_return": round(float(returns.mean()), 6),
                    "worst_signal_date": worst_row.get("signal_date"),
                    "worst_exit_date": worst_row.get("exit_date"),
                    "worst_exit_reason": worst_row.get("exit_reason"),
                }
            )
    symbol_loss = pd.DataFrame(symbol_loss_rows).sort_values("worst_net_return") if symbol_loss_rows else pd.DataFrame()
    _write_csv(output_dir / "single_symbol_loss_summary.csv", symbol_loss)
    portfolio, monthly, daily_positions = _portfolio_outputs(trades, all_dates, output_dir)

    summary = {
        "split_performance": split_summary.to_dict("records"),
        "market_groups": market_summary.to_dict("records"),
        "industry_groups": industry_summary.to_dict("records"),
        "exit_reason_groups": exit_summary.to_dict("records"),
        "portfolio_trade_count": int(len(portfolio)) if not portfolio.empty else 0,
        "accepted_portfolio_trades": int((portfolio["portfolio_action"] == "accepted").sum()) if not portfolio.empty else 0,
        "monthly_count": int(len(monthly)) if not monthly.empty else 0,
        "max_daily_holding_count": int(daily_positions["holding_count"].max()) if not daily_positions.empty else 0,
        "max_daily_exposure_pct": round(float(daily_positions["gross_exposure_pct"].max()), 4) if not daily_positions.empty else 0.0,
        "single_symbol_worst_loss": symbol_loss.head(1).to_dict("records")[0] if not symbol_loss.empty else {},
    }
    _write_json(output_dir / "performance_summary.json", summary)
    return summary


def _summary_row(summary_df: pd.DataFrame, split: str) -> dict[str, Any]:
    """读取分段摘要行。"""
    rows = summary_df[summary_df["split"] == split]
    return rows.iloc[0].to_dict() if not rows.empty else {}


def _render_report(
    output_dir: Path,
    daily_universe: pd.DataFrame,
    trades: pd.DataFrame,
    performance: dict[str, Any],
    train_end: pd.Timestamp,
    test_start: pd.Timestamp,
    fee_bps: float,
    slippage_bps: float,
    limit_threshold: float,
) -> str:
    """渲染 Round3 报告。"""
    split_df = pd.DataFrame(performance.get("split_performance") or [])
    train = _summary_row(split_df, "train")
    test = _summary_row(split_df, "test")
    all_row = _summary_row(split_df, "all")
    lines = [
        "# Factor Research Round3 Report",
        "",
        f"生成日期：{datetime.now().strftime('%Y-%m-%d')}",
        f"时间切分：train <= {train_end.strftime('%Y-%m-%d')}，test >= {test_start.strftime('%Y-%m-%d')}",
        "",
        "免责声明：本报告仅用于个人模拟交易、复盘和策略研究；不构成投资建议，不修改主 market_scanner 策略，不连接实盘接口。",
        "",
        "## 1. Dynamic Universe",
        "",
        "- 每个交易日动态生成可交易股票池：当日必须有 K 线、截至当日满足最少历史长度、OHLCV 有效、成交额达标、非涨停不可买、非跌停不可开仓。",
        "- 因子和过滤只使用信号日及以前数据；未来价格只用于回测执行。",
        "- 仍需注意：当前本地历史数据覆盖范围来自已缓存股票，不能代表完整全 A 历史数据库。",
        f"- 每日 universe 股票数范围：{int(daily_universe['stock_count'].min())} 至 {int(daily_universe['stock_count'].max())}。",
        "- 明细：`daily_universe.csv`。",
        "",
        "## 2. Alpha040 Core Shadow Strategy",
        "",
        "- 主排序因子：`alpha040`。",
        "- 辅助因子：`rps60`、`close_to_20d_high`。",
        "- 不重复加权 `rps60/change_rate_60d`，不使用 `rps20/change_rate_20d`。",
        "- 趋势只做弱过滤：收盘价 >= MA20；MA 多头只给小权重，距离 MA20 过远扣分。",
        "- 冲高回落只输出 `upper_shadow_ratio` 标签，不做剔除。",
        "",
        "## 3. 执行限制",
        "",
        f"- 单边手续费：{fee_bps}bp；单边滑点：{slippage_bps}bp。",
        f"- 涨跌停近似阈值：{_fmt_pct(limit_threshold)}。",
        "- 涨停无法买入；跌停无法卖出；停牌/无有效 K 线无法交易。",
        "- 同一天同时触及止盈和止损时，按更保守的止损结果处理。",
        "",
        "## 4. Train/Test 表现",
        "",
        "| 分段 | 交易数 | 胜率 | 平均单笔 | 中位数 | 最差单笔 | 最大连续亏损 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, row in [("all", all_row), ("train", train), ("test", test)]:
        lines.append(
            f"| {label} | {int(row.get('trade_count') or 0)} | {_fmt_pct(row.get('win_rate'))} | "
            f"{_fmt_pct(row.get('average_return'))} | {_fmt_pct(row.get('median_return'))} | "
            f"{_fmt_pct(row.get('worst_trade_return'))} | {int(row.get('max_consecutive_losses') or 0)} |"
        )
    lines.extend(
        [
            "",
            "## 5. 组合与风险",
            "",
            f"- 事件交易数：{len(trades)}",
            f"- 组合接受交易数：{performance.get('accepted_portfolio_trades')}",
            f"- 最大每日持仓数：{performance.get('max_daily_holding_count')}",
            f"- 最大每日敞口：{_fmt_pct(performance.get('max_daily_exposure_pct'))}",
            f"- 单票最大亏损：{(performance.get('single_symbol_worst_loss') or {}).get('symbol', '-')} / {_fmt_pct((performance.get('single_symbol_worst_loss') or {}).get('worst_net_return'))}",
            "- 月度收益：`monthly_returns.csv`。",
            "- 每日持仓和敞口：`daily_positions.csv`。",
            "- 单票亏损汇总：`single_symbol_loss_summary.csv`。",
            "",
            "## 6. 分组产物",
            "",
            "- 市场环境分组：`market_regime_performance.csv`。",
            "- 行业分组：`industry_performance.csv`。注意：当前历史缓存没有真正行业字段，暂用已有扫描产物中的 `sector` 映射；若该字段只有 SH/SZ，则不能解读为行业差异。",
            "- 退出原因分组：`exit_reason_performance.csv`。",
            "- 每笔交易：`alpha040_core_trades.csv`。",
            "",
            "## 7. 结论边界",
            "",
            "- Round3 修复了“每日 universe 固定为当前缓存集合”的主要偏差，但仍受本地历史数据覆盖范围限制。",
            "- Alpha040 Core 仍是 shadow strategy，不能自动进入主策略。",
            "- 下一步若要进一步降低偏差，应保存每日全市场快照或接入可回放的历史全市场数据源。",
        ]
    )
    report = "\n".join(lines) + "\n"
    (output_dir / "factor_research_round3_report.md").write_text(report, encoding="utf-8")
    return report


def run_factor_research_round3(
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
    limit_threshold: float = DEFAULT_LIMIT_THRESHOLD,
) -> dict[str, Any]:
    """运行 Round3 Alpha040 Core shadow strategy。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    config = scanner._load_scanner_config(strategy_path)
    scanner._set_active_config(config)
    histories = fr.load_cached_histories(cache_dir=cache_dir, max_symbols=max_symbols, min_bars=min_bars)
    if not histories:
        raise RuntimeError(f"未找到可用历史 K 线缓存：{cache_dir}")
    metadata = _load_scan_metadata()
    alpha040_map = _build_alpha040_map(histories)
    universe_by_date, daily_universe = _build_dynamic_universe(
        histories,
        metadata,
        alpha040_map,
        min_history=min_history,
        output_dir=output_dir,
    )
    active_dates = [date for date, rows in universe_by_date.items() if rows]
    if len(active_dates) < 3:
        raise RuntimeError("动态 universe 有效日期不足")
    train_end, test_start = fr._split_dates(pd.Series(pd.to_datetime(active_dates)), train_ratio=train_ratio)
    market_timeline = _market_timeline()
    max_hold_days = int(scanner._cfg("backtest", "max_hold_days", 5))
    entry_window_days = int(scanner._cfg("backtest", "entry_window_days", 2))
    trades, selected = _select_and_backtest(
        universe_by_date,
        histories,
        market_timeline,
        train_end=train_end,
        max_hold_days=max_hold_days,
        entry_window_days=entry_window_days,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        limit_threshold=limit_threshold,
        output_dir=output_dir,
    )
    performance = _build_all_summaries(trades, market_timeline, active_dates, output_dir)
    report = _render_report(
        output_dir,
        daily_universe,
        trades,
        performance,
        train_end,
        test_start,
        fee_bps,
        slippage_bps,
        limit_threshold,
    )
    summary = {
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "module": "factor_research_round3_alpha040_core_shadow",
        "safety": "不修改主 market_scanner 策略、不写台账、不连接实盘接口。",
        "history_symbol_count": len(histories),
        "active_date_count": len(active_dates),
        "train_end": train_end.strftime("%Y-%m-%d"),
        "test_start": test_start.strftime("%Y-%m-%d"),
        "selected_signal_count": int(len(selected)),
        "trade_count": int(len(trades)),
        "fee_bps_per_side": fee_bps,
        "slippage_bps_per_side": slippage_bps,
        "limit_threshold": limit_threshold,
        "output_dir": str(output_dir),
        "report_path": str(output_dir / "factor_research_round3_report.md"),
        "daily_universe_path": str(output_dir / "daily_universe.csv"),
        "trades_path": str(output_dir / "alpha040_core_trades.csv"),
        "daily_positions_path": str(output_dir / "daily_positions.csv"),
        "monthly_returns_path": str(output_dir / "monthly_returns.csv"),
        "report_characters": len(report),
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行 Round3 Alpha040 Core shadow strategy")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--cache-dir", default=str(fr.DEFAULT_CACHE_DIR), help="股票历史 K 缓存目录")
    parser.add_argument("--max-symbols", type=int, help="最多读取多少只股票缓存")
    parser.add_argument("--min-bars", type=int, default=80, help="单票最少 K 线数量")
    parser.add_argument("--min-history", type=int, default=60, help="动态 universe 最少历史长度")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="按时间顺序训练段比例")
    parser.add_argument("--strategy", default="strategy.json", help="策略配置文件")
    parser.add_argument("--fee-bps", type=float, default=5.0, help="单边手续费 bp")
    parser.add_argument("--slippage-bps", type=float, default=10.0, help="单边滑点 bp")
    parser.add_argument("--limit-threshold", type=float, default=DEFAULT_LIMIT_THRESHOLD, help="涨跌停近似阈值")
    args = parser.parse_args()
    summary = run_factor_research_round3(
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
