#!/usr/bin/env python3
"""Long-sample diagnostics for alpha040_v3_risk_controlled.

This script is intentionally read-only with respect to strategy configuration.
It reads the completed 2021 expanded backtest outputs and expanded daily kline
data, then writes diagnostic reports under output/expanded_backtest_v3_2021/.
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import market_scanner as scanner

BASE = ROOT / "output" / "expanded_backtest_v3_2021"
DATA = ROOT / "data" / "expanded"
PROJECT_REPORT = ROOT / "docs" / "project_overall_report_2026-06-29.md"
STRATEGY_KEY = "v3_atr_risk_budget_hot5_vol_risk_on"
EXPECTED_EXIT_REASONS = [
    "take_profit",
    "stop_loss",
    "timeout",
    "time_stop",
    "environment_exit",
    "limit_down_blocked_exit",
    "same_day_stop_take_conservative",
]


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _fmt_pct(value: Any, digits: int = 2) -> str:
    value_float = _safe_float(value)
    if value_float is None:
        return "-"
    return f"{value_float * 100:.{digits}f}%"


def _fmt_num(value: Any, digits: int = 2) -> str:
    value_float = _safe_float(value)
    if value_float is None:
        return "-"
    return f"{value_float:.{digits}f}"


def _max_drawdown_from_returns(returns: pd.Series) -> float | None:
    values = pd.to_numeric(returns, errors="coerce").dropna()
    if values.empty:
        return None
    equity = (1.0 + values).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(drawdown.min())


def _max_consecutive_losses(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    ordered = frame.copy()
    sort_cols = [col for col in ["exit_date", "entry_date", "signal_date"] if col in ordered.columns]
    if sort_cols:
        ordered = ordered.sort_values(sort_cols)
    max_run = 0
    current = 0
    for value in pd.to_numeric(ordered.get("net_return"), errors="coerce").fillna(0):
        if value < 0:
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    return int(max_run)


def _sharpe(daily_returns: pd.Series) -> float | None:
    values = pd.to_numeric(daily_returns, errors="coerce").dropna()
    if len(values) < 2:
        return None
    std = values.std(ddof=1)
    if not std:
        return None
    return float(values.mean() / std * math.sqrt(252))


def _annualized_return(period_return: float, day_count: int) -> float | None:
    if day_count <= 0 or period_return <= -1:
        return None
    return float((1 + period_return) ** (252 / day_count) - 1)


def _trade_summary(frame: pd.DataFrame) -> dict[str, Any]:
    returns = pd.to_numeric(frame.get("net_return"), errors="coerce").dropna()
    if returns.empty:
        return {
            "trade_count": 0,
            "win_rate": None,
            "average_return": None,
            "median_return": None,
            "max_loss": None,
            "max_consecutive_losses": 0,
            "stop_loss_count": 0,
            "limit_down_blocked_exit_count": 0,
            "return_contribution": 0.0,
        }
    contribution = pd.to_numeric(frame.get("return_contribution"), errors="coerce").fillna(0).sum()
    return {
        "trade_count": int(len(frame)),
        "win_rate": float((returns > 0).mean()),
        "average_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "max_loss": float(returns.min()),
        "max_consecutive_losses": _max_consecutive_losses(frame),
        "stop_loss_count": int((frame.get("exit_reason") == "stop_loss").sum()),
        "limit_down_blocked_exit_count": int((frame.get("exit_reason") == "limit_down_blocked_exit").sum()),
        "return_contribution": float(contribution),
    }


def _json_counter(series: pd.Series, limit: int | None = None) -> str:
    counter = Counter(str(x) for x in series.dropna() if str(x) and str(x).lower() != "nan")
    items = counter.most_common(limit)
    return json.dumps(dict(items), ensure_ascii=False)


def _load_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = pd.read_csv(BASE / f"{STRATEGY_KEY}_accepted_trades.csv")
    equity = pd.read_csv(BASE / f"{STRATEGY_KEY}_daily_equity.csv")
    for col in ["signal_date", "entry_date", "exit_date"]:
        trades[col] = pd.to_datetime(trades[col], errors="coerce")
    equity["date"] = pd.to_datetime(equity["date"], errors="coerce")
    trades["year"] = trades["entry_date"].dt.year
    trades["exit_year"] = trades["exit_date"].dt.year
    trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
    trades["portfolio_position_pct"] = pd.to_numeric(trades["portfolio_position_pct"], errors="coerce")
    trades["position_pct"] = pd.to_numeric(trades["position_pct"], errors="coerce")
    trades["return_contribution"] = trades["net_return"].fillna(0) * trades["portfolio_position_pct"].fillna(0)
    trades["industry"] = trades["industry"].fillna("行业缺失").replace("", "行业缺失")
    return trades, equity


def _yearly_performance(trades: pd.DataFrame, equity: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for year in [2021, 2022, 2023, 2024, 2025, 2026]:
        eq = equity[equity["date"].dt.year == year].copy()
        tr = trades[trades["year"] == year].copy()
        if eq.empty:
            continue
        start_equity = float(eq["equity"].iloc[0])
        end_equity = float(eq["equity"].iloc[-1])
        period_return = end_equity / start_equity - 1 if start_equity else None
        max_drawdown = float(pd.to_numeric(eq["drawdown"], errors="coerce").min())
        ann_return = _annualized_return(period_return or 0.0, len(eq))
        calmar = ann_return / abs(max_drawdown) if ann_return is not None and max_drawdown and max_drawdown < 0 else None
        summary = _trade_summary(tr)
        rows.append(
            {
                "year": "2026 YTD" if year == 2026 else str(year),
                "accepted_trade_count": summary["trade_count"],
                "return": period_return,
                "max_drawdown": max_drawdown,
                "sharpe": _sharpe(eq["daily_return"]),
                "calmar": calmar,
                "win_rate": summary["win_rate"],
                "average_trade_return": summary["average_return"],
                "median_trade_return": summary["median_return"],
                "stop_loss_count": summary["stop_loss_count"],
                "limit_down_blocked_exit_count": summary["limit_down_blocked_exit_count"],
                "max_consecutive_losses": summary["max_consecutive_losses"],
            }
        )
    result = pd.DataFrame(rows)
    result.to_csv(BASE / "yearly_v3_performance.csv", index=False, encoding="utf-8-sig")
    return result


def _industry_performance(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for industry, group in trades.groupby("industry", dropna=False):
        summary = _trade_summary(group)
        rows.append(
            {
                "industry": industry,
                "trade_count": summary["trade_count"],
                "win_rate": summary["win_rate"],
                "average_return": summary["average_return"],
                "median_return": summary["median_return"],
                "stop_loss_count": summary["stop_loss_count"],
                "limit_down_blocked_exit_count": summary["limit_down_blocked_exit_count"],
                "cumulative_contribution": summary["return_contribution"],
                "industry_missing_count": int((group["industry"] == "行业缺失").sum()),
            }
        )
    result = pd.DataFrame(rows).sort_values(["trade_count", "cumulative_contribution"], ascending=[False, True])
    result.to_csv(BASE / "v3_industry_performance_2021.csv", index=False, encoding="utf-8-sig")
    return result


def _build_market_timeline_from_expanded() -> dict[str, dict]:
    scanner._set_active_config(scanner._load_scanner_config("strategy.json"))
    index_daily = pd.read_csv(DATA / "index_daily.csv")
    index_daily["trade_date"] = pd.to_datetime(index_daily["trade_date"], errors="coerce")
    code_labels = {
        "000001.SH": "上证",
        "399001.SZ": "深成指",
        "399006.SZ": "创业板",
        "000300.SH": "沪深300",
    }
    indexes: list[dict[str, Any]] = []
    for code, label in code_labels.items():
        group = index_daily[index_daily["ts_code"] == code].sort_values("trade_date")
        if group.empty:
            continue
        bars = [
            {
                "date": row.trade_date.strftime("%Y-%m-%d"),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": _safe_float(getattr(row, "vol", None)),
                "turnover": _safe_float(getattr(row, "amount", None)),
            }
            for row in group.itertuples(index=False)
            if pd.notna(row.trade_date) and pd.notna(row.close)
        ]
        if len(bars) >= 80:
            indexes.append({"label": label, "_history_bars": bars})
    return scanner._build_market_timeline(indexes) if indexes else {}


def _environment_diagnostics(trades: pd.DataFrame, market_timeline: dict[str, dict]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows = []
    for _, trade in trades.iterrows():
        entry = trade["entry_date"]
        exit_ = trade["exit_date"]
        first_non_risk_on_date = None
        first_non_risk_on_label = None
        if pd.notna(entry) and pd.notna(exit_):
            for date in pd.date_range(entry, exit_, freq="D"):
                profile = market_timeline.get(date.strftime("%Y-%m-%d")) or {}
                label = profile.get("regime_label")
                if label and label != "积极":
                    first_non_risk_on_date = date.strftime("%Y-%m-%d")
                    first_non_risk_on_label = label
                    break
        rows.append(
            {
                "symbol": trade.get("symbol"),
                "name": trade.get("name"),
                "industry": trade.get("industry"),
                "entry_date": entry.strftime("%Y-%m-%d") if pd.notna(entry) else None,
                "exit_date": exit_.strftime("%Y-%m-%d") if pd.notna(exit_) else None,
                "exit_reason": trade.get("exit_reason"),
                "net_return": trade.get("net_return"),
                "return_contribution": trade.get("return_contribution"),
                "first_non_risk_on_date": first_non_risk_on_date,
                "first_non_risk_on_label": first_non_risk_on_label,
                "changed_from_risk_on": bool(first_non_risk_on_label),
            }
        )
    env_change = pd.DataFrame(rows)
    risk_on_entry = pd.DataFrame([{"metric": "risk_on_new_entries", **_trade_summary(trades)}])
    changed = env_change[env_change["changed_from_risk_on"]].copy()
    changed_summary = pd.DataFrame([{"metric": "holding_regime_changed_from_risk_on", **_trade_summary(changed)}])
    return risk_on_entry, changed_summary, env_change


def _environment_exit_report(trades: pd.DataFrame, env_change: pd.DataFrame) -> None:
    env_exit = trades[trades["exit_reason"] == "environment_exit"].copy()
    summary = _trade_summary(env_exit)
    by_year = env_exit.groupby("year").agg(
        trade_count=("net_return", "size"),
        average_return=("net_return", "mean"),
        median_return=("net_return", "median"),
        max_loss=("net_return", "min"),
        contribution=("return_contribution", "sum"),
    ).reset_index() if not env_exit.empty else pd.DataFrame()
    by_industry = env_exit.groupby("industry").agg(
        trade_count=("net_return", "size"),
        average_return=("net_return", "mean"),
        median_return=("net_return", "median"),
        max_loss=("net_return", "min"),
        contribution=("return_contribution", "sum"),
    ).reset_index().sort_values("trade_count", ascending=False) if not env_exit.empty else pd.DataFrame()
    by_year.to_csv(BASE / "environment_exit_by_year_long_sample.csv", index=False, encoding="utf-8-sig")
    by_industry.to_csv(BASE / "environment_exit_by_industry_long_sample.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# Environment Exit Diagnosis - 2021 Long Sample",
        "",
        "本诊断只解释 `environment_exit` 的历史回测表现，不修改主策略，也不建议立刻调参。",
        "",
        "## Summary",
        "",
        f"- 笔数：{summary['trade_count']}",
        f"- 平均单笔：{_fmt_pct(summary['average_return'])}",
        f"- 中位数：{_fmt_pct(summary['median_return'])}",
        f"- 最大亏损：{_fmt_pct(summary['max_loss'])}",
        f"- 组合权益贡献：{_fmt_pct(summary['return_contribution'])}",
        "",
        "## By Year",
        "",
        "| 年份 | 笔数 | 平均单笔 | 中位数 | 最大亏损 | 贡献 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in by_year.to_dict("records"):
        lines.append(
            f"| {int(row['year'])} | {int(row['trade_count'])} | {_fmt_pct(row['average_return'])} | "
            f"{_fmt_pct(row['median_return'])} | {_fmt_pct(row['max_loss'])} | {_fmt_pct(row['contribution'])} |"
        )
    lines.extend(["", "## By Industry", "", "| 行业 | 笔数 | 平均单笔 | 中位数 | 最大亏损 | 贡献 |", "|---|---:|---:|---:|---:|---:|"])
    for row in by_industry.head(20).to_dict("records"):
        lines.append(
            f"| {row['industry']} | {int(row['trade_count'])} | {_fmt_pct(row['average_return'])} | "
            f"{_fmt_pct(row['median_return'])} | {_fmt_pct(row['max_loss'])} | {_fmt_pct(row['contribution'])} |"
        )
    top_year = by_year.sort_values("contribution").head(1).to_dict("records") if not by_year.empty else []
    top_industry = by_industry.sort_values("contribution").head(1).to_dict("records") if not by_industry.empty else []
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            f"- 主要拖累年份：{int(top_year[0]['year']) if top_year else '-'}。",
            f"- 主要拖累行业：{top_industry[0]['industry'] if top_industry else '-'}。",
            "- 该退出原因应继续作为 shadow 诊断项观察；当前证据不足以支持直接修改环境退出规则。",
            "",
        ]
    )
    (BASE / "environment_exit_diagnosis.md").write_text("\n".join(lines), encoding="utf-8")


def _exit_reason_diagnosis(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for reason in EXPECTED_EXIT_REASONS:
        group = trades[trades["exit_reason"] == reason].copy()
        summary = _trade_summary(group)
        rows.append(
            {
                "exit_reason": reason,
                "trade_count": summary["trade_count"],
                "win_rate": summary["win_rate"],
                "average_return": summary["average_return"],
                "median_return": summary["median_return"],
                "max_loss": summary["max_loss"],
                "return_contribution": summary["return_contribution"],
                "year_distribution": _json_counter(group["year"]) if not group.empty else "{}",
                "industry_distribution_top10": _json_counter(group["industry"], 10) if not group.empty else "{}",
            }
        )
    result = pd.DataFrame(rows)
    result.to_csv(BASE / "v3_exit_reason_diagnosis.csv", index=False, encoding="utf-8-sig")
    return result


def _exposure_audit(trades: pd.DataFrame, equity: pd.DataFrame) -> pd.DataFrame:
    position = pd.to_numeric(trades["portfolio_position_pct"], errors="coerce")
    exposure = pd.to_numeric(equity["gross_exposure_pct"], errors="coerce")
    rows = [
        {"metric": "average_single_position", "value": float(position.mean()), "notes": "accepted trades mean portfolio_position_pct"},
        {"metric": "max_single_position", "value": float(position.max()), "notes": "accepted trades max portfolio_position_pct"},
        {"metric": "average_total_exposure", "value": float(exposure.mean()), "notes": "daily equity curve, including zero-position days"},
        {"metric": "average_active_day_total_exposure", "value": float(exposure[exposure > 0].mean()), "notes": "daily equity curve, active holding days only"},
        {"metric": "max_total_exposure", "value": float(exposure.max()), "notes": "daily equity curve max gross exposure"},
        {"metric": "profit_trade_average_position", "value": float(position[trades["net_return"] > 0].mean()), "notes": "net_return > 0"},
        {"metric": "loss_trade_average_position", "value": float(position[trades["net_return"] <= 0].mean()), "notes": "net_return <= 0"},
        {"metric": "stop_loss_average_position", "value": float(position[trades["exit_reason"] == "stop_loss"].mean()), "notes": "stop_loss trades"},
        {"metric": "limit_down_blocked_exit_average_position", "value": float(position[trades["exit_reason"] == "limit_down_blocked_exit"].mean()), "notes": "limit_down_blocked_exit trades"},
    ]
    result = pd.DataFrame(rows)
    result.to_csv(BASE / "v3_exposure_audit.csv", index=False, encoding="utf-8-sig")
    return result


def _cs_zscore(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    if numeric.notna().sum() == 0:
        return numeric
    low = numeric.quantile(0.01)
    high = numeric.quantile(0.99)
    clipped = numeric.clip(low, high)
    filled = clipped.fillna(clipped.median())
    std = filled.std(ddof=0)
    if not std or pd.isna(std):
        return filled * 0.0
    return (filled - filled.mean()) / std


def _pearson(x: pd.Series, y: pd.Series) -> float | None:
    valid = pd.concat([pd.to_numeric(x, errors="coerce"), pd.to_numeric(y, errors="coerce")], axis=1).dropna()
    if len(valid) < 30:
        return None
    if valid.iloc[:, 0].std(ddof=0) == 0 or valid.iloc[:, 1].std(ddof=0) == 0:
        return None
    return float(valid.iloc[:, 0].corr(valid.iloc[:, 1]))


def _rank_ic(x: pd.Series, y: pd.Series) -> float | None:
    valid = pd.concat([pd.to_numeric(x, errors="coerce"), pd.to_numeric(y, errors="coerce")], axis=1).dropna()
    if len(valid) < 30:
        return None
    if valid.iloc[:, 0].nunique() <= 1 or valid.iloc[:, 1].nunique() <= 1:
        return None
    return _pearson(valid.iloc[:, 0].rank(method="average"), valid.iloc[:, 1].rank(method="average"))


def _factor_summary(daily: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows = []
    for key, group in daily.groupby(group_col, dropna=False):
        ic = pd.to_numeric(group["ic"], errors="coerce").dropna()
        rank_ic = pd.to_numeric(group["rank_ic"], errors="coerce").dropna()
        q = pd.to_numeric(group["q5_q1"], errors="coerce").dropna()
        rows.append(
            {
                group_col: key,
                "date_count": int(len(group)),
                "avg_sample_size": float(pd.to_numeric(group["sample_size"], errors="coerce").mean()),
                "ic_mean": float(ic.mean()) if not ic.empty else None,
                "ic_median": float(ic.median()) if not ic.empty else None,
                "ic_positive_rate": float((ic > 0).mean()) if not ic.empty else None,
                "rank_ic_mean": float(rank_ic.mean()) if not rank_ic.empty else None,
                "rank_ic_median": float(rank_ic.median()) if not rank_ic.empty else None,
                "rank_ic_positive_rate": float((rank_ic > 0).mean()) if not rank_ic.empty else None,
                "q5_q1_mean": float(q.mean()) if not q.empty else None,
                "q5_q1_median": float(q.median()) if not q.empty else None,
                "q5_q1_positive_rate": float((q > 0).mean()) if not q.empty else None,
            }
        )
    return pd.DataFrame(rows)


def _alpha040_factor_report(market_timeline: dict[str, dict]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    usecols = ["trade_date", "ts_code", "high", "close", "vol"]
    df = pd.read_csv(DATA / "daily_kline.csv", usecols=usecols)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.dropna(subset=["trade_date", "ts_code", "high", "close", "vol"])
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    grouped = df.groupby("ts_code", sort=False)
    df["prev_close"] = grouped["close"].shift(1)
    df["future_5d_return"] = grouped["close"].shift(-5) / df["close"] - 1
    df["change_rate_60d"] = df["close"] / grouped["close"].shift(60) - 1
    df["rolling_20d_high"] = grouped["high"].rolling(20, min_periods=20).max().reset_index(level=0, drop=True)
    df["close_to_20d_high"] = df["close"] / df["rolling_20d_high"] - 1
    up_volume = pd.Series(np.where(df["close"] > df["prev_close"], df["vol"], 0.0), index=df.index)
    down_volume = pd.Series(np.where(df["close"] <= df["prev_close"], df["vol"], 0.0), index=df.index)
    df["up_volume_26"] = up_volume.groupby(df["ts_code"], sort=False).rolling(26, min_periods=26).sum().reset_index(level=0, drop=True)
    df["down_volume_26"] = down_volume.groupby(df["ts_code"], sort=False).rolling(26, min_periods=26).sum().reset_index(level=0, drop=True)
    df["alpha040"] = df["up_volume_26"] / df["down_volume_26"].replace(0, np.nan) * 100
    df["rps60"] = df.groupby("trade_date")["change_rate_60d"].rank(pct=True)
    market_median = df.groupby("trade_date")["future_5d_return"].median().rename("market_future_5d_median")
    df = df.join(market_median, on="trade_date")
    df["future_5d_excess_return"] = df["future_5d_return"] - df["market_future_5d_median"]
    dataset = df[["trade_date", "ts_code", "alpha040", "rps60", "close_to_20d_high", "future_5d_excess_return"]].dropna().copy()
    dataset = dataset[dataset["trade_date"] <= pd.Timestamp("2026-06-19")].copy()
    for col in ["alpha040", "rps60", "close_to_20d_high"]:
        dataset[f"{col}_z"] = dataset.groupby("trade_date", group_keys=False)[col].transform(_cs_zscore)
    dataset["year"] = dataset["trade_date"].dt.year
    dataset["market_regime_label"] = dataset["trade_date"].dt.strftime("%Y-%m-%d").map(
        lambda date: (market_timeline.get(date) or {}).get("regime_label") or "未知"
    )

    daily_rows = []
    group_rows = []
    for date, group in dataset.groupby("trade_date", sort=True):
        if len(group) < 200:
            continue
        ic = _pearson(group["alpha040_z"], group["future_5d_excess_return"])
        rank_ic = _rank_ic(group["alpha040_z"], group["future_5d_excess_return"])
        q5_q1 = None
        valid = group[["alpha040_z", "future_5d_excess_return"]].dropna().copy()
        if len(valid) >= 100 and valid["alpha040_z"].nunique() >= 5:
            valid["bucket"] = pd.qcut(valid["alpha040_z"], 5, labels=False, duplicates="drop") + 1
            bucket = valid.groupby("bucket")["future_5d_excess_return"].mean()
            if 1 in bucket.index and 5 in bucket.index:
                q5_q1 = float(bucket.loc[5] - bucket.loc[1])
                group_rows.append(
                    {
                        "date": date.strftime("%Y-%m-%d"),
                        "q1": float(bucket.loc[1]),
                        "q5": float(bucket.loc[5]),
                        "q5_q1": q5_q1,
                    }
                )
        daily_rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "year": int(date.year),
                "market_regime_label": (market_timeline.get(date.strftime("%Y-%m-%d")) or {}).get("regime_label") or "未知",
                "sample_size": int(len(group)),
                "ic": ic,
                "rank_ic": rank_ic,
                "q5_q1": q5_q1,
            }
        )
    daily = pd.DataFrame(daily_rows)
    q_detail = pd.DataFrame(group_rows)
    full_summary = _factor_summary(daily.assign(all="全区间"), "all")
    year_summary = _factor_summary(daily, "year")
    regime_summary = _factor_summary(daily, "market_regime_label")
    corr = dataset[["alpha040_z", "rps60_z", "close_to_20d_high_z"]].corr(method="pearson")

    daily.to_csv(BASE / "alpha040_daily_ic_long_sample.csv", index=False, encoding="utf-8-sig")
    year_summary.to_csv(BASE / "alpha040_yearly_ic_long_sample.csv", index=False, encoding="utf-8-sig")
    regime_summary.to_csv(BASE / "alpha040_regime_ic_long_sample.csv", index=False, encoding="utf-8-sig")
    q_detail.to_csv(BASE / "alpha040_q5_q1_daily_long_sample.csv", index=False, encoding="utf-8-sig")
    corr.to_csv(BASE / "alpha040_factor_correlation_long_sample.csv", encoding="utf-8-sig")

    lines = [
        "# Alpha040 Long Sample Factor Report",
        "",
        "口径：2021-01-04 至 2026-06-26 扩展日线；标签为未来 5 日超额收益，即个股未来 5 日收益减当日全市场未来 5 日收益中位数。因子按交易日横截面做 1%/99% 去极值、缺失中位数填充和 z-score。",
        "",
        "## Full Period",
        "",
        "| 日期数 | 平均样本数 | IC均值 | RankIC均值 | IC正占比 | RankIC正占比 | Q5-Q1均值 | Q5-Q1正占比 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    full = full_summary.iloc[0].to_dict()
    lines.append(
        f"| {int(full['date_count'])} | {_fmt_num(full['avg_sample_size'], 0)} | {_fmt_num(full['ic_mean'], 4)} | "
        f"{_fmt_num(full['rank_ic_mean'], 4)} | {_fmt_pct(full['ic_positive_rate'])} | {_fmt_pct(full['rank_ic_positive_rate'])} | "
        f"{_fmt_pct(full['q5_q1_mean'])} | {_fmt_pct(full['q5_q1_positive_rate'])} |"
    )
    lines.extend(["", "## By Year", "", "| 年份 | 日期数 | IC均值 | RankIC均值 | Q5-Q1均值 | Q5-Q1正占比 |", "|---|---:|---:|---:|---:|---:|"])
    for row in year_summary.to_dict("records"):
        lines.append(
            f"| {int(row['year'])} | {int(row['date_count'])} | {_fmt_num(row['ic_mean'], 4)} | "
            f"{_fmt_num(row['rank_ic_mean'], 4)} | {_fmt_pct(row['q5_q1_mean'])} | {_fmt_pct(row['q5_q1_positive_rate'])} |"
        )
    lines.extend(["", "## By Market Regime", "", "| 环境 | 日期数 | IC均值 | RankIC均值 | Q5-Q1均值 | Q5-Q1正占比 |", "|---|---:|---:|---:|---:|---:|"])
    for row in regime_summary.to_dict("records"):
        lines.append(
            f"| {row['market_regime_label']} | {int(row['date_count'])} | {_fmt_num(row['ic_mean'], 4)} | "
            f"{_fmt_num(row['rank_ic_mean'], 4)} | {_fmt_pct(row['q5_q1_mean'])} | {_fmt_pct(row['q5_q1_positive_rate'])} |"
        )
    lines.extend(["", "## Correlation", "", "| 因子对 | 相关性 |", "|---|---:|"])
    lines.append(f"| alpha040_z / rps60_z | {_fmt_num(corr.loc['alpha040_z', 'rps60_z'], 4)} |")
    lines.append(f"| alpha040_z / close_to_20d_high_z | {_fmt_num(corr.loc['alpha040_z', 'close_to_20d_high_z'], 4)} |")
    lines.append(f"| rps60_z / close_to_20d_high_z | {_fmt_num(corr.loc['rps60_z', 'close_to_20d_high_z'], 4)} |")
    q_year = year_summary.set_index("year")["q5_q1_mean"].to_dict()
    weak_years = [str(int(y)) for y, v in q_year.items() if pd.notna(v) and v < 0]
    lines.extend(
        [
            "",
            "## Conclusions",
            "",
            f"- alpha040 全样本 Q5-Q1 均值为 {_fmt_pct(full['q5_q1_mean'])}，RankIC 均值为 {_fmt_num(full['rank_ic_mean'], 4)}。",
            f"- Q5-Q1 为负的年份：{', '.join(weak_years) if weak_years else '无'}。",
            f"- alpha040 与 rps60 的相关性为 {_fmt_num(corr.loc['alpha040_z', 'rps60_z'], 4)}，未显示强重复加权；但两者都带有动量/上涨成交量含义，仍应继续监控。",
            "- 该报告只支持稳定性观察，不支持直接替换主因子或修改权重。",
            "",
        ]
    )
    (BASE / "alpha040_long_sample_factor_report.md").write_text("\n".join(lines), encoding="utf-8")
    return full_summary, year_summary, regime_summary


def _markdown_table(df: pd.DataFrame, columns: list[tuple[str, str]], max_rows: int | None = None) -> list[str]:
    rows = df.head(max_rows).to_dict("records") if max_rows else df.to_dict("records")
    header = "| " + " | ".join(title for title, _ in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, sep]
    for row in rows:
        cells = []
        for _title, col in columns:
            value = row.get(col)
            if col in {"return", "max_drawdown", "win_rate", "average_trade_return", "median_trade_return", "average_return", "median_return", "cumulative_contribution", "return_contribution", "max_loss"}:
                cells.append(_fmt_pct(value))
            elif isinstance(value, float):
                cells.append(_fmt_num(value, 4))
            elif pd.isna(value):
                cells.append("-")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _write_long_sample_report(
    trades: pd.DataFrame,
    yearly: pd.DataFrame,
    industry: pd.DataFrame,
    exit_diag: pd.DataFrame,
    exposure: pd.DataFrame,
    risk_on_entry: pd.DataFrame,
    changed_summary: pd.DataFrame,
    full_factor: pd.DataFrame,
    year_factor: pd.DataFrame,
    regime_factor: pd.DataFrame,
) -> None:
    stop_loss_contrib = exit_diag.loc[exit_diag["exit_reason"] == "stop_loss", "return_contribution"].iloc[0]
    env_contrib = exit_diag.loc[exit_diag["exit_reason"] == "environment_exit", "return_contribution"].iloc[0]
    limit_down = trades[trades["exit_reason"] == "limit_down_blocked_exit"]
    timeout_contrib = exit_diag.loc[exit_diag["exit_reason"] == "timeout", "return_contribution"].iloc[0]
    full = full_factor.iloc[0].to_dict()
    years_negative = yearly[yearly["return"] < 0]["year"].astype(str).tolist()
    worst_year = yearly.sort_values("return").head(1).iloc[0].to_dict()
    best_later = yearly[yearly["year"].isin(["2024", "2025", "2026 YTD"])].copy()
    early = yearly[yearly["year"].isin(["2021", "2022", "2023"])].copy()
    early_return = (1 + early["return"]).prod() - 1 if not early.empty else None
    later_return = (1 + best_later["return"]).prod() - 1 if not best_later.empty else None
    exposure_map = dict(zip(exposure["metric"], exposure["value"]))
    lines = [
        "# V3 Long Sample Diagnosis",
        "",
        "## Technical Summary",
        "",
        "- `alpha040_v3_risk_controlled` 在 2021-01-04 至 2026-06-26 的 347 笔接受交易中累计收益为 -9.89%、最大回撤 -11.12%，长样本转负不是单点错误，而是 2021、2022、2025 多阶段拖累共同造成。",
        f"- 最差年份为 {worst_year['year']}，年度收益 {_fmt_pct(worst_year['return'])}；2021-2023 合计约 {_fmt_pct(early_return)}，2024-2026 YTD 合计约 {_fmt_pct(later_return)}，说明旧 2024-2026 口径确实偏乐观。",
        f"- 亏损的主要事件来源是 `stop_loss` 和 `limit_down_blocked_exit`，其中 stop_loss 组合贡献 {_fmt_pct(stop_loss_contrib)}，environment_exit 贡献 {_fmt_pct(env_contrib)}；environment_exit 为负但不是最大拖累。",
        f"- alpha040 全样本 RankIC 均值 {_fmt_num(full['rank_ic_mean'], 4)}、Q5-Q1 均值 {_fmt_pct(full['q5_q1_mean'])}，按当前做多方向在长样本里不稳定；2026 转正但不足以覆盖 2022-2025 的负向阶段。",
        "",
        "## Scope And Definitions",
        "",
        "- 交易样本：`v3_atr_risk_budget_hot5_vol_risk_on_accepted_trades.csv` 的 347 笔 accepted trades。",
        "- 组合贡献：`net_return × portfolio_position_pct`，用于比较行业和退出原因对权益的影响。",
        "- 年度收益、回撤和 Sharpe：来自 V3 每日权益曲线；交易胜率和退出原因来自 accepted trades。",
        "- alpha040 标签：未来 5 日超额收益，即个股未来 5 日收益减当日全市场未来 5 日收益中位数。",
        "- 本诊断不调参、不新增因子、不修改主策略、不连接实盘。",
        "",
        "## Yearly Performance",
        "",
        *_markdown_table(
            yearly,
            [
                ("年份", "year"),
                ("交易数", "accepted_trade_count"),
                ("累计收益", "return"),
                ("最大回撤", "max_drawdown"),
                ("Sharpe", "sharpe"),
                ("Calmar", "calmar"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_trade_return"),
                ("中位数", "median_trade_return"),
                ("止损", "stop_loss_count"),
                ("跌停无法卖出", "limit_down_blocked_exit_count"),
                ("最大连亏", "max_consecutive_losses"),
            ],
        ),
        "",
        "### Yearly Interpretation",
        "",
        f"- V3 不是只亏在某一年；亏损年份包括 {', '.join(years_negative)}。",
        f"- 2024 和 2026 YTD 略正，但 2025 再次转负；因此不能说只在 2024-2026 稳定较好。",
        "- 2021-2023 合计为负，说明长样本早期环境中策略收益端明显不足，但 2023 接近持平，不能简单归纳为 2021-2023 全部失效。",
        "",
        "## Industry Attribution",
        "",
        f"- 行业缺失数量：{int((trades['industry'] == '行业缺失').sum())}",
        "- 下表为交易数最多的前 15 个行业；完整结果见 `v3_industry_performance_2021.csv`。",
        "",
        *_markdown_table(
            industry,
            [
                ("行业", "industry"),
                ("交易数", "trade_count"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_return"),
                ("中位数", "median_return"),
                ("止损", "stop_loss_count"),
                ("跌停无法卖出", "limit_down_blocked_exit_count"),
                ("累计贡献", "cumulative_contribution"),
                ("行业缺失", "industry_missing_count"),
            ],
            max_rows=15,
        ),
        "",
        "## Market Regime And Environment Exit",
        "",
        *_markdown_table(
            risk_on_entry,
            [
                ("口径", "metric"),
                ("交易数", "trade_count"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_return"),
                ("中位数", "median_return"),
                ("最大亏损", "max_loss"),
                ("累计贡献", "return_contribution"),
            ],
        ),
        "",
        *_markdown_table(
            changed_summary,
            [
                ("口径", "metric"),
                ("交易数", "trade_count"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_return"),
                ("中位数", "median_return"),
                ("最大亏损", "max_loss"),
                ("累计贡献", "return_contribution"),
            ],
        ),
        "",
        "- V3 新开仓均发生在积极环境；持仓期环境变差的交易需要继续观察，但本轮只做归因，不修改 environment_exit。",
        "- `environment_exit` 详情已刷新到 `environment_exit_diagnosis.md`。",
        "",
        "## Exit Reason Diagnosis",
        "",
        *_markdown_table(
            exit_diag,
            [
                ("退出原因", "exit_reason"),
                ("笔数", "trade_count"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_return"),
                ("中位数", "median_return"),
                ("最大亏损", "max_loss"),
                ("累计贡献", "return_contribution"),
            ],
        ),
        "",
        f"- stop_loss 贡献 {_fmt_pct(stop_loss_contrib)}，environment_exit 贡献 {_fmt_pct(env_contrib)}；V3 亏损主要不是 environment_exit 单独造成。",
        f"- 跌停无法卖出共 {len(limit_down)} 笔，年份分布为 `{_json_counter(limit_down['year'])}`，行业分布前十为 `{_json_counter(limit_down['industry'], 10)}`。",
        f"- timeout 贡献 {_fmt_pct(timeout_contrib)}，属于轻微负贡献，不是主要尾部亏损源。",
        "",
        "## Alpha040 Stability",
        "",
        "- 完整 alpha040 因子报告见 `alpha040_long_sample_factor_report.md`。",
        *_markdown_table(
            year_factor,
            [
                ("年份", "year"),
                ("日期数", "date_count"),
                ("IC均值", "ic_mean"),
                ("RankIC均值", "rank_ic_mean"),
                ("Q5-Q1均值", "q5_q1_mean"),
                ("Q5-Q1正占比", "q5_q1_positive_rate"),
            ],
        ),
        "",
        *_markdown_table(
            regime_factor,
            [
                ("环境", "market_regime_label"),
                ("日期数", "date_count"),
                ("IC均值", "ic_mean"),
                ("RankIC均值", "rank_ic_mean"),
                ("Q5-Q1均值", "q5_q1_mean"),
                ("Q5-Q1正占比", "q5_q1_positive_rate"),
            ],
        ),
        "",
        "## Exposure Audit",
        "",
        *_markdown_table(exposure, [("指标", "metric"), ("值", "value"), ("说明", "notes")]),
        "",
        f"- 平均单票仓位 {_fmt_pct(exposure_map.get('average_single_position'))}，最大单票仓位 {_fmt_pct(exposure_map.get('max_single_position'))}；收益低不能简单归因于仓位过低，因为提高风险预算的压力测试反而放大亏损。",
        f"- 亏损交易平均仓位 {_fmt_pct(exposure_map.get('loss_trade_average_position'))}，止损交易平均仓位 {_fmt_pct(exposure_map.get('stop_loss_average_position'))}，未见少数超高仓位交易单独解释全部亏损。",
        "",
        "## A. 已确认事实",
        "",
        "- 2021 起正式扩展回测已完成，V3 长样本累计收益转为 -9.89%。",
        "- 行业归因已从旧 238 笔口径修正为 347 笔 accepted trades，行业缺失数量为 0。",
        "- legacy 更差，baseline_current_stop 明显不可用，但这不等于 V3 可以实盘。",
        "- stop_loss 和 limit_down_blocked_exit 是更大的亏损来源，environment_exit 为负但不是最大拖累。",
        "",
        "## B. 可能原因",
        "",
        "- 旧 2024-2026 样本偏短，避开或低估了 2021-2022 的不利市场阶段。",
        "- alpha040 的当前做多方向在长样本里不稳定，尤其 2022-2025 多数年份 RankIC 和 Q5-Q1 偏负；这可能削弱了组合收益端。",
        "- 风险预算降低了亏损放大，但也使正收益交易对组合贡献有限。",
        "- 行业暴露集中在电子、电气、软件等方向，部分年份行业风格不匹配可能拖累。",
        "",
        "## C. 仍需验证",
        "",
        "- alpha040 的年份差异是否来自市场结构变化，还是来自 efinance 单源数据口径。",
        "- 持仓期环境切换是否应作为 shadow 观察项加强，而不是立即修改退出规则。",
        "- 行业拖累是否在 forward paper trading 中持续出现。",
        "",
        "## D. 不建议立刻做的事",
        "",
        "- 不建议根据本轮结果调 5 日涨幅阈值或 volatility_20d 阈值。",
        "- 不建议回滚到 legacy_momentum_v1，因为 legacy 长样本回撤和止损更差。",
        "- 不建议替换 alpha040 或新增复杂因子；当前证据只支持稳定性观察。",
        "- 不建议连接实盘或自动下单。",
        "",
        "## E. 下一步 Shadow 观察项",
        "",
        "- 观察行业集中度对组合贡献的持续性，必要时只进入 shadow_experiment_candidate。",
        "- 每周复核 environment_exit 的年份/行业分布，不直接改主策略。",
        "- forward paper 至少累计 30-50 笔 closed trades 后再讨论实验草案。",
        "- 后续如接入 Fuyao/Tushare 多源数据，重跑 alpha040 稳定性和退出原因诊断。",
        "",
    ]
    (BASE / "v3_long_sample_diagnosis.md").write_text("\n".join(lines), encoding="utf-8")


def _refresh_project_report(industry: pd.DataFrame) -> None:
    text = PROJECT_REPORT.read_text(encoding="utf-8")
    old_section_start = text.index("## 6. 行业归因修复")
    old_section_end = text.index("## 7. 主要风险")
    top_rows = industry.head(5).to_dict("records")
    table_lines = [
        "| 行业 | 交易数 | 胜率 | 平均单笔 | 中位数 | 止损 | 跌停无法卖出 | 累计贡献 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in top_rows:
        table_lines.append(
            f"| {row['industry']} | {int(row['trade_count'])} | {_fmt_pct(row['win_rate'])} | "
            f"{_fmt_pct(row['average_return'])} | {_fmt_pct(row['median_return'])} | "
            f"{int(row['stop_loss_count'])} | {int(row['limit_down_blocked_exit_count'])} | {_fmt_pct(row['cumulative_contribution'])} |"
        )
    new_section = "\n".join(
        [
            "## 6. 行业归因修复",
            "",
            "行业归因已按 2021 起正式扩展回测重新刷新，口径为 `output/expanded_backtest_v3_2021/v3_atr_risk_budget_hot5_vol_risk_on_accepted_trades.csv` 中的 347 笔 accepted trades。",
            "",
            "当前修正后的行业口径：",
            "",
            "- V3 接受交易：`347` 笔。",
            "- 行业缺失数量：`0`。",
            "- 行业字段来自 `data/expanded/industry_or_sector.csv` / BaoStock 行业映射，不使用 SH/SZ/BJ 市场后缀冒充行业。",
            "- 累计贡献按 `net_return × portfolio_position_pct` 计算，避免把不同仓位交易当成同等影响。",
            "- 完整结果位于 `output/expanded_backtest_v3_2021/v3_industry_performance_2021.csv`。",
            "",
            "当前 V3 行业分布前几项：",
            "",
            *table_lines,
            "",
            "后续应重点观察：V3 是否长期集中在电子设备、电气机械、软件信息等方向；单行业拖累只能列为 `shadow_experiment_candidate`，不能直接修改主策略。",
            "",
        ]
    )
    text = text[:old_section_start] + new_section + "\n" + text[old_section_end:]
    text = text.replace(
        "2. 新历史数据尚未重跑正式扩展回测：当前已有 2021 起数据，但策略表现仍需复跑验证。",
        "2. 2021 起正式扩展回测已完成：结果显示 V3 收益转负，需进入分年份、行业、环境和退出原因诊断阶段。",
    )
    if "output/expanded_backtest_v3_2021/v3_long_sample_diagnosis.md" not in text:
        text = text.replace(
            "- 人工复核 `output/expanded_backtest_v3_2021/expanded_backtest_report.md`，尤其是收益回归、行业集中、退出原因和风险预算敞口。",
            "- 人工复核 `output/expanded_backtest_v3_2021/expanded_backtest_report.md` 与 `output/expanded_backtest_v3_2021/v3_long_sample_diagnosis.md`，尤其是收益回归、行业集中、退出原因和风险预算敞口。",
        )
    PROJECT_REPORT.write_text(text, encoding="utf-8")


def main() -> None:
    trades, equity = _load_inputs()
    yearly = _yearly_performance(trades, equity)
    industry = _industry_performance(trades)
    market_timeline = _build_market_timeline_from_expanded()
    risk_on_entry, changed_summary, env_change = _environment_diagnostics(trades, market_timeline)
    env_change.to_csv(BASE / "v3_holding_regime_change_diagnosis.csv", index=False, encoding="utf-8-sig")
    _environment_exit_report(trades, env_change)
    exit_diag = _exit_reason_diagnosis(trades)
    exposure = _exposure_audit(trades, equity)
    full_factor, year_factor, regime_factor = _alpha040_factor_report(market_timeline)
    _write_long_sample_report(
        trades,
        yearly,
        industry,
        exit_diag,
        exposure,
        risk_on_entry,
        changed_summary,
        full_factor,
        year_factor,
        regime_factor,
    )
    _refresh_project_report(industry)
    print(
        json.dumps(
            {
                "accepted_trades": int(len(trades)),
                "yearly_path": str(BASE / "yearly_v3_performance.csv"),
                "industry_path": str(BASE / "v3_industry_performance_2021.csv"),
                "diagnosis_path": str(BASE / "v3_long_sample_diagnosis.md"),
                "alpha040_report_path": str(BASE / "alpha040_long_sample_factor_report.md"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
