"""
factor_research_round5.py - Alpha040 Core 风险口径修正与止损归因

输出：output/factor_research_round5/
本模块只读本地历史缓存、Round4 输出和策略配置，不修改 market_scanner 主策略、不写台账、不连接实盘。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import factor_research_round3 as r3
import factor_research_round4 as r4
import market_scanner as scanner

DEFAULT_OUTPUT_DIR = Path("output/factor_research_round5")
DEFAULT_ROUND4_DIR = Path("output/factor_research_round4")
REGIME_LABELS = ["积极", "中性", "谨慎", "防守"]
RISK_EXIT_REASONS = {"stop_loss", "limit_down_blocked_exit"}


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


def _read_csv(path: Path) -> pd.DataFrame:
    """读取 CSV 文件。"""
    return pd.read_csv(path, encoding="utf-8-sig")


def _parse_reason_counts(value: Any) -> dict[str, int]:
    """解析 universe 剔除原因 JSON。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return {str(key): int(val) for key, val in payload.items() if _safe_float(val) is not None}


def _reason_buckets(row: pd.Series) -> dict[str, int]:
    """把 Round4 universe 0 日期剔除原因归入 Round5 审计桶。"""
    counts = _parse_reason_counts(row.get("reason_counts_json"))
    data_missing = int(counts.get("no_bar_on_date", row.get("data_missing", 0) or 0)) + int(
        counts.get("suspended_or_invalid_bar", 0)
    )
    filter_too_strict = int(counts.get("price_below_min", 0)) + int(counts.get("turnover_below_min", 0))
    cache_coverage = int(counts.get("insufficient_history", row.get("cache_warmup_or_coverage", 0) or 0))
    limit_filter = int(counts.get("limit_up_cannot_buy", 0)) + int(counts.get("limit_down_not_openable", 0))
    known = data_missing + filter_too_strict + cache_coverage + limit_filter
    considered = int(row.get("considered_symbols", 0) or 0)
    other = max(0, considered - known)
    return {
        "cache_warmup_or_coverage": cache_coverage,
        "filter_too_strict": filter_too_strict,
        "data_missing": data_missing,
        "limit_up_or_limit_down_filter": limit_filter,
        "other": other,
    }


def _audit_universe_zero(round4_dir: Path, output_dir: Path) -> dict[str, Any]:
    """读取 Round4 universe 审计输出，重新归类 universe 为 0 日期原因。"""
    zero_path = round4_dir / "universe_zero_dates.csv"
    dist_path = round4_dir / "universe_count_distribution.csv"
    if not zero_path.exists() or not dist_path.exists():
        raise FileNotFoundError(f"缺少 Round4 universe 审计文件：{zero_path} 或 {dist_path}")
    zero_df = _read_csv(zero_path)
    dist_df = _read_csv(dist_path)
    detailed_rows: list[dict[str, Any]] = []
    for _, row in zero_df.iterrows():
        buckets = _reason_buckets(row)
        primary_reason = max(buckets, key=buckets.get) if buckets else "other"
        detailed_rows.append({"date": row.get("date"), "primary_reason_round5": primary_reason, **buckets})
    detailed = pd.DataFrame(detailed_rows)
    total_zero_dates = int(len(detailed))
    summary_rows = []
    for reason in ["cache_warmup_or_coverage", "filter_too_strict", "data_missing", "limit_up_or_limit_down_filter", "other"]:
        count = int((detailed["primary_reason_round5"] == reason).sum()) if not detailed.empty else 0
        summary_rows.append(
            {
                "reason": reason,
                "zero_date_count": count,
                "zero_date_share": round(count / total_zero_dates, 6) if total_zero_dates else None,
            }
        )
    summary = pd.DataFrame(summary_rows)
    dominant = summary.sort_values("zero_date_count", ascending=False).iloc[0].to_dict() if total_zero_dates else {}
    caveats: list[str] = []
    relax_plan: list[dict[str, str]] = []
    if dominant.get("reason") == "cache_warmup_or_coverage" and (dominant.get("zero_date_share") or 0) >= 0.5:
        caveats.append("Universe 为 0 主要来自缓存预热或覆盖不足，当前回测不能视为完整全市场回测。")
    if dominant.get("reason") == "filter_too_strict" or summary.loc[summary["reason"] == "filter_too_strict", "zero_date_count"].sum() > 0:
        relax_plan.extend(
            [
                {
                    "plan": "turnover_relax_shadow",
                    "description": "仅在 shadow 回测中测试降低最小成交额阈值，例如 10 亿 -> 6 亿/8 亿。",
                },
                {
                    "plan": "price_floor_relax_shadow",
                    "description": "仅在 shadow 回测中测试放宽最低价格过滤，但保留 ST/停牌/涨跌停限制。",
                },
                {
                    "plan": "two_stage_filter_shadow",
                    "description": "先用较宽 universe 打分，再在入选前做流动性和波动二次确认。",
                },
            ]
        )
    _write_csv(output_dir / "universe_zero_reason_detail.csv", detailed)
    _write_csv(output_dir / "universe_zero_reason_summary.csv", summary)
    _write_csv(output_dir / "universe_count_distribution.csv", dist_df)
    _write_json(output_dir / "universe_zero_audit.json", {"dominant_reason": dominant, "caveats": caveats, "relax_plan": relax_plan})
    return {
        "zero_detail": detailed,
        "zero_summary": summary,
        "universe_distribution": dist_df,
        "dominant_reason": dominant,
        "caveats": caveats,
        "relax_plan": relax_plan,
    }


def _signal_feature_map(universe_by_date: dict[str, list[dict]]) -> dict[tuple[str, str], dict[str, Any]]:
    """生成信号日特征映射。"""
    feature_cols = [
        "alpha040",
        "alpha040_z",
        "rps20",
        "rps60",
        "rps60_z",
        "close_to_20d_high",
        "close_to_20d_high_z",
        "volatility_20d",
        "amplitude",
        "upper_shadow_ratio",
        "change_rate_5d",
        "change_rate_20d",
        "volume_ratio",
        "turnover",
        "latest",
        "ma5",
        "ma10",
        "ma20",
        "signal_day_limit_up",
        "signal_day_limit_down",
    ]
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for date, rows in universe_by_date.items():
        for item in rows:
            symbol = str(item.get("_symbol_key") or item.get("symbol") or "")
            if not symbol:
                continue
            result[(date, symbol)] = {col: item.get(col) for col in feature_cols}
    return result


def _bar_index_by_date(histories: dict[str, list[dict]]) -> dict[str, dict[str, int]]:
    """生成 symbol -> date -> index。"""
    result: dict[str, dict[str, int]] = {}
    for symbol, bars in histories.items():
        result[symbol] = {str(bar.get("date")): idx for idx, bar in enumerate(bars) if bar.get("date")}
    return result


def _entry_context(
    symbol: str,
    entry_date: str,
    entry_price_raw: Optional[float],
    histories: dict[str, list[dict]],
    index_by_date: dict[str, dict[str, int]],
    limit_threshold: float,
) -> dict[str, Any]:
    """计算开仓日距离涨停等执行特征。"""
    idx = index_by_date.get(symbol, {}).get(entry_date)
    if idx is None:
        return {
            "entry_prev_close": None,
            "entry_limit_up_price": None,
            "entry_limit_up_distance": None,
            "entry_near_limit_up": False,
            "entry_day_change_rate": None,
        }
    bars = histories.get(symbol, [])
    bar = bars[idx]
    prev_close = r3._prev_close_from_bars(bars, idx)
    close = _safe_float(bar.get("close"))
    if prev_close is None or prev_close <= 0 or entry_price_raw is None:
        return {
            "entry_prev_close": prev_close,
            "entry_limit_up_price": None,
            "entry_limit_up_distance": None,
            "entry_near_limit_up": False,
            "entry_day_change_rate": close / prev_close - 1 if prev_close and close else None,
        }
    limit_up_price = prev_close * (1 + limit_threshold)
    distance = (limit_up_price - entry_price_raw) / prev_close
    return {
        "entry_prev_close": round(prev_close, 4),
        "entry_limit_up_price": round(limit_up_price, 4),
        "entry_limit_up_distance": round(distance, 6),
        "entry_near_limit_up": bool(distance <= 0.02),
        "entry_day_change_rate": round(close / prev_close - 1, 6) if close is not None else None,
    }


def _enrich_trades(
    trades: pd.DataFrame,
    universe_by_date: dict[str, list[dict]],
    histories: dict[str, list[dict]],
    limit_threshold: float,
) -> pd.DataFrame:
    """为交易明细补充信号因子和开仓执行特征。"""
    if trades.empty:
        return trades.copy()
    features = _signal_feature_map(universe_by_date)
    index_by_date = _bar_index_by_date(histories)
    rows = []
    for _, row in trades.iterrows():
        item = row.to_dict()
        key = (str(item.get("signal_date")), str(item.get("symbol")))
        for col, value in features.get(key, {}).items():
            if col not in item or pd.isna(item.get(col)):
                item[col] = value
        item.update(
            _entry_context(
                str(item.get("symbol")),
                str(item.get("entry_date")),
                _safe_float(item.get("entry_price_raw")),
                histories,
                index_by_date,
                limit_threshold,
            )
        )
        item["risk_exit"] = bool(item.get("exit_reason") in RISK_EXIT_REASONS)
        item["profit_trade"] = bool((_safe_float(item.get("net_return")) or 0.0) > 0)
        rows.append(item)
    return pd.DataFrame(rows)


def _max_consecutive_losses(trades: pd.DataFrame) -> int:
    """计算最大连续亏损笔数。"""
    if trades.empty or "net_return" not in trades.columns:
        return 0
    sorted_rows = trades.sort_values(["exit_date", "entry_date", "symbol"])
    max_streak = 0
    current = 0
    for value in pd.to_numeric(sorted_rows["net_return"], errors="coerce").fillna(0.0):
        if value < 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return int(max_streak)


def _event_summary(rows: pd.DataFrame) -> dict[str, Any]:
    """事件级交易序列表现，不输出事件级最大回撤。"""
    if rows.empty:
        return {"trade_count": 0, "win_rate": None, "average_return": None, "median_return": None, "max_consecutive_losses": 0}
    returns = pd.to_numeric(rows["net_return"], errors="coerce").dropna()
    return {
        "trade_count": int(len(returns)),
        "win_rate": round(float((returns > 0).mean()), 6) if len(returns) else None,
        "average_return": round(float(returns.mean()), 6) if len(returns) else None,
        "median_return": round(float(returns.median()), 6) if len(returns) else None,
        "max_consecutive_losses": _max_consecutive_losses(rows),
    }


def _portfolio_regime_summary(equity: pd.DataFrame, market_timeline: dict[str, dict]) -> pd.DataFrame:
    """按每日市场环境统计组合级收益和回撤。"""
    if equity.empty:
        return pd.DataFrame()
    daily = equity.copy()
    daily["market_regime_label"] = daily["date"].map(lambda x: (market_timeline.get(str(x)) or {}).get("regime_label") or "未知")
    rows: list[dict[str, Any]] = []
    for label in REGIME_LABELS:
        group = daily[daily["market_regime_label"] == label].copy()
        returns = pd.to_numeric(group["daily_return"], errors="coerce").fillna(0.0)
        curve = (1 + returns).cumprod()
        drawdown = curve / curve.cummax() - 1 if len(curve) else pd.Series(dtype=float)
        rows.append(
            {
                "market_regime_label": label,
                "portfolio_days": int(len(group)),
                "portfolio_total_return": round(float(curve.iloc[-1] - 1), 6) if len(curve) else None,
                "portfolio_max_drawdown": round(float(drawdown.min()), 6) if len(drawdown) else None,
                "portfolio_positive_day_rate": round(float((returns > 0).mean()), 6) if len(returns) else None,
            }
        )
    return pd.DataFrame(rows)


def _market_regime_dual_summary(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    market_timeline: dict[str, dict],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """拆分事件级交易序列和组合级权益曲线表现。"""
    event_rows = []
    for label in REGIME_LABELS:
        group = trades[trades["market_regime_label"] == label]
        event_rows.append({"market_regime_label": label, **_event_summary(group)})
    event_df = pd.DataFrame(event_rows)
    portfolio_df = _portfolio_regime_summary(equity, market_timeline)
    combined = event_df.merge(portfolio_df, on="market_regime_label", how="left")
    policy_rows = []
    for _, row in combined.iterrows():
        trade_count = int(row.get("trade_count") or 0)
        win_rate = row.get("win_rate")
        avg_ret = row.get("average_return")
        median_ret = row.get("median_return")
        portfolio_return = row.get("portfolio_total_return")
        portfolio_dd = row.get("portfolio_max_drawdown")
        max_streak = int(row.get("max_consecutive_losses") or 0)
        if trade_count < 10:
            action = "sample_insufficient"
            reason = "样本不足，不建议据此调整开仓环境"
        elif (
            avg_ret is not None
            and avg_ret < 0
            and win_rate is not None
            and win_rate < 0.45
        ):
            action = "forbid_open_shadow"
            reason = "事件级均值为负且胜率偏低"
        elif portfolio_return is not None and portfolio_return < 0 and portfolio_dd is not None and portfolio_dd < -0.02:
            action = "forbid_open_shadow"
            reason = "组合级权益曲线在该环境下累计收益为负且回撤偏深"
        elif (
            (median_ret is not None and median_ret < 0)
            or max_streak >= 5
            or (portfolio_dd is not None and portfolio_dd < -0.03)
        ):
            action = "reduce_or_tighten_shadow"
            reason = "收益分布或组合回撤不稳，建议只做降仓/收紧过滤 shadow 测试"
        else:
            action = "allow_observe_shadow"
            reason = "事件级和组合级未同时恶化，继续观察"
        policy_rows.append(
            {
                "market_regime_label": row["market_regime_label"],
                "risk_action": action,
                "reason": reason,
            }
        )
    return event_df, portfolio_df, combined.merge(pd.DataFrame(policy_rows), on="market_regime_label", how="left")


def _group_feature_profile(trades: pd.DataFrame) -> pd.DataFrame:
    """比较止损、跌停无法卖出与盈利交易的特征差异。"""
    feature_cols = [
        "alpha040",
        "rps60",
        "close_to_20d_high",
        "volatility_20d",
        "amplitude",
        "upper_shadow_ratio",
        "change_rate_5d",
        "volume_ratio",
        "entry_limit_up_distance",
    ]
    groups = {
        "profit_trade": trades[trades["profit_trade"]],
        "stop_loss": trades[trades["exit_reason"] == "stop_loss"],
        "limit_down_blocked_exit": trades[trades["exit_reason"] == "limit_down_blocked_exit"],
        "risk_exit_combined": trades[trades["risk_exit"]],
    }
    rows: list[dict[str, Any]] = []
    for group_name, group in groups.items():
        row: dict[str, Any] = {"group": group_name, "trade_count": int(len(group))}
        for col in feature_cols:
            series = pd.to_numeric(group.get(col), errors="coerce").dropna() if col in group.columns else pd.Series(dtype=float)
            row[f"{col}_median"] = round(float(series.median()), 6) if len(series) else None
            row[f"{col}_mean"] = round(float(series.mean()), 6) if len(series) else None
        row["risk_on_share"] = round(float((group.get("market_regime_label") == "积极").mean()), 6) if len(group) else None
        row["near_limit_up_share"] = round(float(group.get("entry_near_limit_up", pd.Series(dtype=bool)).fillna(False).mean()), 6) if len(group) else None
        rows.append(row)
    return pd.DataFrame(rows)


def _risk_group_market_distribution(trades: pd.DataFrame) -> pd.DataFrame:
    """输出盈利、止损、跌停无法卖出和风险合并组的市场环境分布。"""
    groups = {
        "profit_trade": trades[trades["profit_trade"]],
        "stop_loss": trades[trades["exit_reason"] == "stop_loss"],
        "limit_down_blocked_exit": trades[trades["exit_reason"] == "limit_down_blocked_exit"],
        "risk_exit_combined": trades[trades["risk_exit"]],
    }
    rows: list[dict[str, Any]] = []
    for group_name, group in groups.items():
        total = len(group)
        counts = Counter(str(value) for value in group.get("market_regime_label", []))
        for label in REGIME_LABELS:
            count = int(counts.get(label, 0))
            rows.append(
                {
                    "group": group_name,
                    "market_regime_label": label,
                    "trade_count": count,
                    "share_in_group": round(count / total, 6) if total else None,
                }
            )
    return pd.DataFrame(rows)


def _feature_risk_lift(trades: pd.DataFrame) -> pd.DataFrame:
    """计算特征分桶对止损/跌停风险的 lift。"""
    specs = [
        ("alpha040", "low"),
        ("rps60", "low"),
        ("close_to_20d_high", "low"),
        ("volatility_20d", "high"),
        ("amplitude", "high"),
        ("upper_shadow_ratio", "high"),
        ("change_rate_5d", "high"),
        ("volume_ratio", "high"),
        ("entry_limit_up_distance", "low"),
    ]
    rows: list[dict[str, Any]] = []
    base = trades.copy()
    base["risk_exit"] = base["exit_reason"].isin(RISK_EXIT_REASONS)
    for feature, direction in specs:
        if feature not in base.columns:
            continue
        series = pd.to_numeric(base[feature], errors="coerce")
        valid = base[series.notna()].copy()
        values = pd.to_numeric(valid[feature], errors="coerce")
        if len(valid) < 20 or values.nunique(dropna=True) < 3:
            continue
        threshold = values.quantile(0.75 if direction == "high" else 0.25)
        bucket = values >= threshold if direction == "high" else values <= threshold
        bucket_rows = valid[bucket]
        rest_rows = valid[~bucket]
        if bucket_rows.empty or rest_rows.empty:
            continue
        bad_rate_bucket = float(bucket_rows["risk_exit"].mean())
        bad_rate_rest = float(rest_rows["risk_exit"].mean())
        rows.append(
            {
                "feature": feature,
                "risk_direction": direction,
                "threshold": round(float(threshold), 6),
                "bucket_trade_count": int(len(bucket_rows)),
                "rest_trade_count": int(len(rest_rows)),
                "risk_exit_rate_bucket": round(bad_rate_bucket, 6),
                "risk_exit_rate_rest": round(bad_rate_rest, 6),
                "risk_lift": round(bad_rate_bucket - bad_rate_rest, 6),
                "stop_loss_rate_bucket": round(float((bucket_rows["exit_reason"] == "stop_loss").mean()), 6),
                "limit_down_blocked_rate_bucket": round(float((bucket_rows["exit_reason"] == "limit_down_blocked_exit").mean()), 6),
            }
        )
    if "entry_near_limit_up" in base.columns:
        bucket_rows = base[base["entry_near_limit_up"].fillna(False).astype(bool)]
        rest_rows = base[~base["entry_near_limit_up"].fillna(False).astype(bool)]
        if len(bucket_rows) and len(rest_rows):
            rows.append(
                {
                    "feature": "entry_near_limit_up",
                    "risk_direction": "true",
                    "threshold": 1,
                    "bucket_trade_count": int(len(bucket_rows)),
                    "rest_trade_count": int(len(rest_rows)),
                    "risk_exit_rate_bucket": round(float(bucket_rows["risk_exit"].mean()), 6),
                    "risk_exit_rate_rest": round(float(rest_rows["risk_exit"].mean()), 6),
                    "risk_lift": round(float(bucket_rows["risk_exit"].mean() - rest_rows["risk_exit"].mean()), 6),
                    "stop_loss_rate_bucket": round(float((bucket_rows["exit_reason"] == "stop_loss").mean()), 6),
                    "limit_down_blocked_rate_bucket": round(float((bucket_rows["exit_reason"] == "limit_down_blocked_exit").mean()), 6),
                }
            )
    return pd.DataFrame(rows).sort_values("risk_lift", ascending=False)


def _thresholds_for_filter_experiments(trades: pd.DataFrame) -> dict[str, Any]:
    """生成 Round5 shadow 过滤阈值。"""
    def q75(col: str) -> Optional[float]:
        if col not in trades.columns:
            return None
        series = pd.to_numeric(trades[col], errors="coerce").dropna()
        return round(float(series.quantile(0.75)), 6) if len(series) else None

    return {
        "amplitude_q75": q75("amplitude"),
        "volatility_20d_q75": q75("volatility_20d"),
        "entry_limit_up_distance_threshold": 0.02,
        "upper_shadow_ratio_q75": q75("upper_shadow_ratio"),
        "change_rate_5d_q75": q75("change_rate_5d"),
    }


def _apply_filter(trades: pd.DataFrame, experiment: str, thresholds: dict[str, Any]) -> pd.Series:
    """返回需要剔除的样本布尔序列。"""
    false_mask = pd.Series(False, index=trades.index)
    if experiment == "exclude_high_amplitude":
        threshold = thresholds.get("amplitude_q75")
        return pd.to_numeric(trades.get("amplitude"), errors="coerce") >= threshold if threshold is not None else false_mask
    if experiment == "exclude_high_volatility_20d":
        threshold = thresholds.get("volatility_20d_q75")
        return pd.to_numeric(trades.get("volatility_20d"), errors="coerce") >= threshold if threshold is not None else false_mask
    if experiment == "exclude_near_limit_up_entry":
        return trades.get("entry_near_limit_up", false_mask).fillna(False).astype(bool)
    if experiment == "exclude_high_upper_shadow_ratio":
        threshold = thresholds.get("upper_shadow_ratio_q75")
        return pd.to_numeric(trades.get("upper_shadow_ratio"), errors="coerce") >= threshold if threshold is not None else false_mask
    if experiment == "exclude_hot_5d_return":
        threshold = thresholds.get("change_rate_5d_q75")
        return pd.to_numeric(trades.get("change_rate_5d"), errors="coerce") >= threshold if threshold is not None else false_mask
    return false_mask


def _portfolio_for_trades(
    trades: pd.DataFrame,
    histories: dict[str, list[dict]],
    active_dates: list[str],
    initial_cash: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """从事件交易生成组合交易、权益曲线、月度收益和指标。"""
    portfolio = r4._accept_portfolio_trades(trades, initial_cash)
    equity, monthly, metrics = r4._portfolio_equity_curve(portfolio, histories, active_dates, initial_cash)
    return portfolio, equity, monthly, metrics


def _filter_experiment_summary(
    trades: pd.DataFrame,
    histories: dict[str, list[dict]],
    active_dates: list[str],
    initial_cash: float,
    output_dir: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """执行五组止损归因 shadow 过滤实验。"""
    thresholds = _thresholds_for_filter_experiments(trades)
    experiments = {
        "baseline_current_stop": "A baseline 当前止损",
        "exclude_high_amplitude": "剔除高 amplitude 样本",
        "exclude_high_volatility_20d": "剔除高 volatility_20d 样本",
        "exclude_near_limit_up_entry": "剔除距离涨停过近样本",
        "exclude_high_upper_shadow_ratio": "剔除高 upper_shadow_ratio 样本",
        "exclude_hot_5d_return": "剔除 5 日涨幅过热样本",
    }
    rows: list[dict[str, Any]] = []
    for experiment, label in experiments.items():
        mask = pd.Series(False, index=trades.index) if experiment == "baseline_current_stop" else _apply_filter(trades, experiment, thresholds)
        filtered = trades[~mask].copy()
        portfolio, equity, monthly, metrics = _portfolio_for_trades(filtered, histories, active_dates, initial_cash)
        accepted = portfolio[portfolio.get("portfolio_action") == "accepted"] if not portfolio.empty else pd.DataFrame()
        rows.append(
            {
                "experiment": experiment,
                "experiment_label": label,
                "removed_trades": int(mask.sum()),
                "event_trade_count": int(len(filtered)),
                "accepted_trades": int(metrics.get("accepted_trades") or 0),
                "ending_equity": metrics.get("ending_equity"),
                "total_return": metrics.get("total_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe": metrics.get("sharpe"),
                "calmar": metrics.get("calmar"),
                "stop_loss_count": int((accepted.get("exit_reason") == "stop_loss").sum()) if not accepted.empty else 0,
                "limit_down_blocked_exit_count": int((accepted.get("exit_reason") == "limit_down_blocked_exit").sum()) if not accepted.empty else 0,
            }
        )
        prefix = f"{experiment}_"
        _write_csv(output_dir / f"{prefix}portfolio_trades.csv", portfolio)
        _write_csv(output_dir / f"{prefix}daily_equity.csv", equity)
        _write_csv(output_dir / f"{prefix}monthly_returns.csv", monthly)
    summary = pd.DataFrame(rows)
    _write_csv(output_dir / "stop_risk_filter_experiment_summary.csv", summary)
    _write_json(output_dir / "stop_risk_filter_thresholds.json", thresholds)
    return summary, thresholds


def _monthly_stability(monthly: pd.DataFrame) -> dict[str, Any]:
    """月度稳定性指标。"""
    if monthly.empty or "monthly_return" not in monthly.columns:
        return {"month_count": 0, "positive_month_rate": None, "worst_month_return": None, "monthly_return_std": None}
    returns = pd.to_numeric(monthly["monthly_return"], errors="coerce").dropna()
    if returns.empty:
        return {"month_count": 0, "positive_month_rate": None, "worst_month_return": None, "monthly_return_std": None}
    worst_idx = returns.idxmin()
    return {
        "month_count": int(len(returns)),
        "positive_month_rate": round(float((returns > 0).mean()), 6),
        "negative_month_count": int((returns < 0).sum()),
        "worst_month": str(monthly.loc[worst_idx, "month"]) if "month" in monthly.columns else None,
        "worst_month_return": round(float(returns.min()), 6),
        "monthly_return_std": round(float(returns.std(ddof=1)), 6) if len(returns) > 1 else 0.0,
    }


def _risk_control_ac_comparison(
    variants: dict[str, dict[str, Any]],
    output_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """比较 A 当前止损和 C ATR 止损 + 风险预算仓位。"""
    rows = []
    monthly_rows = []
    for variant, ctx in variants.items():
        portfolio = ctx["portfolio"]
        accepted = portfolio[portfolio.get("portfolio_action") == "accepted"] if not portfolio.empty else pd.DataFrame()
        metrics = ctx["metrics"]
        monthly_stats = _monthly_stability(ctx["monthly"])
        rows.append(
            {
                "variant": variant,
                "variant_label": ctx["label"],
                "ending_equity": metrics.get("ending_equity"),
                "total_return": metrics.get("total_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe": metrics.get("sharpe"),
                "calmar": metrics.get("calmar"),
                "max_consecutive_losses": _max_consecutive_losses(accepted),
                "stop_loss_count": int((accepted.get("exit_reason") == "stop_loss").sum()) if not accepted.empty else 0,
                "limit_down_blocked_exit_count": int((accepted.get("exit_reason") == "limit_down_blocked_exit").sum()) if not accepted.empty else 0,
                **monthly_stats,
            }
        )
        for _, row in ctx["monthly"].iterrows():
            monthly_rows.append({"variant": variant, "variant_label": ctx["label"], **row.to_dict()})
    comparison = pd.DataFrame(rows)
    monthly_compare = pd.DataFrame(monthly_rows)
    a_row = comparison[comparison["variant"] == "current_stop"].iloc[0].to_dict()
    c_row = comparison[comparison["variant"] == "atr_stop_risk_budget"].iloc[0].to_dict()
    c_has_lower_dd = abs(c_row.get("max_drawdown") or 0) < abs(a_row.get("max_drawdown") or 0)
    c_has_better_sharpe = (c_row.get("sharpe") or -999) >= (a_row.get("sharpe") or -999)
    c_has_fewer_risk_exits = (
        (c_row.get("stop_loss_count") or 0) + (c_row.get("limit_down_blocked_exit_count") or 0)
        <= (a_row.get("stop_loss_count") or 0) + (a_row.get("limit_down_blocked_exit_count") or 0)
    )
    if c_has_lower_dd and (c_has_better_sharpe or c_has_fewer_risk_exits):
        recommendation = {
            "recommended_shadow_variant": "atr_stop_risk_budget",
            "reason": "C 的组合回撤和风险调整收益更优，适合作为下一轮风险优先 shadow 版本；不自动并入主策略。",
        }
    else:
        recommendation = {
            "recommended_shadow_variant": "current_stop",
            "reason": "A 在当前样本下综合更稳，C 继续观察；不自动并入主策略。",
        }
    _write_csv(output_dir / "risk_control_a_c_comparison.csv", comparison)
    _write_csv(output_dir / "monthly_stability_comparison.csv", monthly_compare)
    _write_json(output_dir / "risk_control_recommendation.json", recommendation)
    return comparison, monthly_compare, recommendation


def _run_variant(
    variant: str,
    label: str,
    universe_by_date: dict[str, list[dict]],
    histories: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    active_dates: list[str],
    train_end: pd.Timestamp,
    initial_cash: float,
    max_hold_days: int,
    entry_window_days: int,
    fee_bps: float,
    slippage_bps: float,
    limit_threshold: float,
    output_dir: Path,
) -> dict[str, Any]:
    """运行并落盘单个风险控制版本。"""
    trades = r4._run_variant_events(
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
    trades = _enrich_trades(trades, universe_by_date, histories, limit_threshold)
    trades["experiment_label"] = label
    portfolio, equity, monthly, metrics = _portfolio_for_trades(trades, histories, active_dates, initial_cash)
    prefix = f"{variant}_"
    _write_csv(output_dir / f"{prefix}trades.csv", trades)
    _write_csv(output_dir / f"{prefix}portfolio_trades.csv", portfolio)
    _write_csv(output_dir / f"{prefix}daily_equity.csv", equity)
    _write_csv(output_dir / f"{prefix}monthly_returns.csv", monthly)
    return {"label": label, "trades": trades, "portfolio": portfolio, "equity": equity, "monthly": monthly, "metrics": metrics}


def _render_universe_audit_md(output_dir: Path, universe_ctx: dict[str, Any]) -> None:
    """输出 universe 审计 Markdown。"""
    lines = [
        "# Round5 Universe Zero Audit",
        "",
        "本审计读取 Round4 的 `universe_zero_dates.csv` 和 `universe_count_distribution.csv`，重新按原因桶归类。",
        "",
        "| 原因 | 日期数 | 占比 |",
        "|---|---:|---:|",
    ]
    for _, row in universe_ctx["zero_summary"].iterrows():
        lines.append(f"| {row['reason']} | {int(row['zero_date_count'])} | {_fmt_pct(row.get('zero_date_share'))} |")
    if universe_ctx["caveats"]:
        lines.extend(["", "## 关键限制", ""])
        lines.extend([f"- {item}" for item in universe_ctx["caveats"]])
    if universe_ctx["relax_plan"]:
        lines.extend(["", "## 可测试放宽方案", ""])
        lines.extend([f"- {item['plan']}：{item['description']}" for item in universe_ctx["relax_plan"]])
    (output_dir / "universe_zero_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_report(output_dir: Path, ctx: dict[str, Any]) -> str:
    """渲染 Round5 报告。"""
    metrics = ctx["variants"]["current_stop"]["metrics"]
    universe_ctx = ctx["universe_audit"]
    market_combined = ctx["market_combined"]
    filter_summary = ctx["filter_summary"]
    ac_comparison = ctx["ac_comparison"]
    recommendation = ctx["recommendation"]
    top_lifts = ctx["feature_lift"].head(5)
    action_labels = {
        "sample_insufficient": "样本不足",
        "forbid_open_shadow": "shadow 禁止开仓",
        "reduce_or_tighten_shadow": "shadow 降仓/收紧",
        "allow_observe_shadow": "允许但观察",
    }
    lines = [
        "# Factor Research Round5 Report",
        "",
        f"生成日期：{datetime.now().strftime('%Y-%m-%d')}",
        f"时间切分：train <= {ctx['train_end'].strftime('%Y-%m-%d')}，test >= {ctx['test_start'].strftime('%Y-%m-%d')}",
        "",
        "免责声明：本报告仅用于个人模拟交易、复盘和策略研究；不构成投资建议，不修改主 market_scanner 策略，不连接实盘接口。",
        "",
        "## 1. Universe 为 0 日期审计",
        "",
        "| 原因 | 日期数 | 占比 |",
        "|---|---:|---:|",
    ]
    for _, row in universe_ctx["zero_summary"].iterrows():
        lines.append(f"| {row['reason']} | {int(row['zero_date_count'])} | {_fmt_pct(row.get('zero_date_share'))} |")
    if universe_ctx["caveats"]:
        lines.extend(["", *[f"- {item}" for item in universe_ctx["caveats"]]])
    if universe_ctx["relax_plan"]:
        lines.extend(["", "可测试放宽方案（仅 shadow，不改主策略）："])
        lines.extend([f"- {item['plan']}：{item['description']}" for item in universe_ctx["relax_plan"]])

    lines.extend(
        [
            "",
            "## 2. 市场环境表现口径修正",
            "",
            "下表将交易数、胜率、平均单笔、中位数、最大连续亏损作为 event-level trade sequence；组合累计收益和组合最大回撤来自 portfolio-level equity curve，两者不混用。",
            "",
            "| 环境 | 交易数 | 胜率 | 平均单笔 | 中位数 | 组合累计收益 | 组合最大回撤 | 最大连续亏损 | 建议 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for _, row in market_combined.iterrows():
        lines.append(
            f"| {row['market_regime_label']} | {int(row['trade_count'])} | {_fmt_pct(row.get('win_rate'))} | "
            f"{_fmt_pct(row.get('average_return'))} | {_fmt_pct(row.get('median_return'))} | "
            f"{_fmt_pct(row.get('portfolio_total_return'))} | {_fmt_pct(row.get('portfolio_max_drawdown'))} | "
            f"{int(row.get('max_consecutive_losses') or 0)} | {action_labels.get(row.get('risk_action'), row.get('risk_action'))} |"
        )

    lines.extend(
        [
            "",
            "## 3. 止损与跌停无法卖出归因",
            "",
            "风险交易定义为 `stop_loss` 或 `limit_down_blocked_exit`。下列特征为风险 lift 最高的前 5 项：",
            "",
            "| 特征 | 风险方向 | 阈值 | 风险率 | 对照风险率 | lift |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in top_lifts.iterrows():
        lines.append(
            f"| {row['feature']} | {row['risk_direction']} | {row['threshold']} | "
            f"{_fmt_pct(row.get('risk_exit_rate_bucket'))} | {_fmt_pct(row.get('risk_exit_rate_rest'))} | {_fmt_pct(row.get('risk_lift'))} |"
        )
    lines.extend(["", "Shadow 过滤实验：", "", "| 实验 | 剔除笔数 | 组合收益 | 最大回撤 | 夏普 | 卡玛 | 止损次数 | 跌停无法卖出 |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for _, row in filter_summary.iterrows():
        lines.append(
            f"| {row['experiment_label']} | {int(row['removed_trades'])} | {_fmt_pct(row.get('total_return'))} | "
            f"{_fmt_pct(row.get('max_drawdown'))} | {row.get('sharpe')} | {row.get('calmar')} | "
            f"{int(row.get('stop_loss_count') or 0)} | {int(row.get('limit_down_blocked_exit_count') or 0)} |"
        )

    lines.extend(["", "## 4. A 与 C 风控版本对比", "", "| 版本 | 累计收益 | 最大回撤 | 夏普 | 最大连续亏损 | 止损次数 | 跌停无法卖出 | 正收益月份占比 | 最差月份 |", "|---|---:|---:|---:|---:|---:|---:|---:|---|"])
    for _, row in ac_comparison.iterrows():
        lines.append(
            f"| {row['variant_label']} | {_fmt_pct(row.get('total_return'))} | {_fmt_pct(row.get('max_drawdown'))} | "
            f"{row.get('sharpe')} | {int(row.get('max_consecutive_losses') or 0)} | "
            f"{int(row.get('stop_loss_count') or 0)} | {int(row.get('limit_down_blocked_exit_count') or 0)} | "
            f"{_fmt_pct(row.get('positive_month_rate'))} | {row.get('worst_month')} {_fmt_pct(row.get('worst_month_return'))} |"
        )
    lines.extend(
        [
            "",
            f"推荐 shadow 版本：`{recommendation['recommended_shadow_variant']}`。",
            recommendation["reason"],
            "",
            "## 5. 当前 baseline 组合指标",
            "",
            f"- 初始资金：{metrics.get('initial_cash'):,.2f}",
            f"- 期末权益：{metrics.get('ending_equity'):,.2f}",
            f"- 累计收益：{_fmt_pct(metrics.get('total_return'))}",
            f"- 最大回撤：{_fmt_pct(metrics.get('max_drawdown'))}",
            f"- 夏普：{metrics.get('sharpe')}",
            f"- 卡玛：{metrics.get('calmar')}",
            "",
            "## 6. 输出文件",
            "",
            "- `universe_zero_reason_summary.csv` / `universe_zero_audit.md`",
            "- `market_regime_event_summary.csv` / `market_regime_portfolio_summary.csv` / `market_regime_dual_summary.csv`",
            "- `stop_loss_feature_comparison.csv` / `risk_group_market_distribution.csv` / `risk_feature_lift.csv`",
            "- `stop_risk_filter_experiment_summary.csv`",
            "- `risk_control_a_c_comparison.csv` / `monthly_stability_comparison.csv`",
            "",
            "Round5 仅修正研究口径和风险归因，不新增复杂因子，不自动并入主策略。",
        ]
    )
    report = "\n".join(lines) + "\n"
    (output_dir / "factor_research_round5_report.md").write_text(report, encoding="utf-8")
    return report


def _run_round5(
    *,
    output_dir: Path,
    round4_dir: Path,
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
    """执行 Round5。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    scanner._set_active_config(scanner._load_scanner_config(strategy_path))
    universe_audit = _audit_universe_zero(round4_dir, output_dir)
    _render_universe_audit_md(output_dir, universe_audit)

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
    initial_cash = float(scanner._cfg("backtest", "portfolio_initial_cash", 1_000_000))

    variants = {
        "current_stop": "A 当前止损 baseline",
        "atr_stop_risk_budget": "C ATR 止损 + 风险预算仓位",
    }
    variant_ctx = {
        variant: _run_variant(
            variant,
            label,
            universe_by_date,
            histories,
            market_timeline,
            active_dates,
            train_end,
            initial_cash,
            max_hold_days,
            entry_window_days,
            fee_bps,
            slippage_bps,
            limit_threshold,
            output_dir,
        )
        for variant, label in variants.items()
    }
    baseline = variant_ctx["current_stop"]
    baseline_trades = baseline["trades"]
    baseline_portfolio = baseline["portfolio"]
    baseline_accepted = baseline_portfolio[baseline_portfolio["portfolio_action"] == "accepted"].copy()

    event_summary, portfolio_summary, market_combined = _market_regime_dual_summary(
        baseline_trades,
        baseline["equity"],
        market_timeline,
    )
    feature_profile = _group_feature_profile(baseline_trades)
    market_distribution = _risk_group_market_distribution(baseline_trades)
    feature_lift = _feature_risk_lift(baseline_trades)
    filter_summary, filter_thresholds = _filter_experiment_summary(baseline_trades, histories, active_dates, initial_cash, output_dir)
    ac_comparison, monthly_compare, recommendation = _risk_control_ac_comparison(variant_ctx, output_dir)

    _write_csv(output_dir / "market_regime_event_summary.csv", event_summary)
    _write_csv(output_dir / "market_regime_portfolio_summary.csv", portfolio_summary)
    _write_csv(output_dir / "market_regime_dual_summary.csv", market_combined)
    _write_csv(output_dir / "stop_loss_feature_comparison.csv", feature_profile)
    _write_csv(output_dir / "risk_group_market_distribution.csv", market_distribution)
    _write_csv(output_dir / "risk_feature_lift.csv", feature_lift)
    _write_csv(output_dir / "baseline_accepted_trades.csv", baseline_accepted)
    _write_json(output_dir / "portfolio_metrics.json", baseline["metrics"])

    return {
        "histories": histories,
        "active_dates": active_dates,
        "train_end": train_end,
        "test_start": test_start,
        "daily_universe": daily_universe,
        "universe_audit": universe_audit,
        "variants": variant_ctx,
        "market_event": event_summary,
        "market_portfolio": portfolio_summary,
        "market_combined": market_combined,
        "feature_profile": feature_profile,
        "market_distribution": market_distribution,
        "feature_lift": feature_lift,
        "filter_summary": filter_summary,
        "filter_thresholds": filter_thresholds,
        "ac_comparison": ac_comparison,
        "monthly_compare": monthly_compare,
        "recommendation": recommendation,
    }


def run_factor_research_round5(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    round4_dir: Path = DEFAULT_ROUND4_DIR,
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
    """运行 Round5 风险口径修正与止损归因。"""
    ctx = _run_round5(
        output_dir=output_dir,
        round4_dir=round4_dir,
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
    baseline_metrics = ctx["variants"]["current_stop"]["metrics"]
    summary = {
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "module": "factor_research_round5_risk_attribution_shadow",
        "safety": "不修改主 market_scanner 策略、不写台账、不连接实盘接口。",
        "history_symbol_count": len(ctx["histories"]),
        "active_date_count": len(ctx["active_dates"]),
        "train_end": ctx["train_end"].strftime("%Y-%m-%d"),
        "test_start": ctx["test_start"].strftime("%Y-%m-%d"),
        "baseline_portfolio_metrics": baseline_metrics,
        "recommended_shadow_variant": ctx["recommendation"]["recommended_shadow_variant"],
        "output_dir": str(output_dir),
        "report_path": str(output_dir / "factor_research_round5_report.md"),
        "report_characters": len(report),
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行 Round5 风险口径修正与止损归因 shadow 回测")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--round4-dir", default=str(DEFAULT_ROUND4_DIR), help="Round4 输出目录")
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
    summary = run_factor_research_round5(
        output_dir=Path(args.output),
        round4_dir=Path(args.round4_dir),
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
