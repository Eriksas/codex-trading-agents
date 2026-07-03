"""
backtest_v3_expanded.py - formal expanded backtest for alpha040_v3_risk_controlled.

读取 data/expanded/daily_kline 的扩展历史数据；若不可用，明确降级到本地缓存。
回测只做研究验证，不修改冻结规则、不连接实盘、不自动下单。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import factor_research_freeze_v3 as freeze
import factor_research_round3 as r3
import factor_research_round4 as r4
import factor_research_round6 as r6
import main_strategy_upgrade_v3 as upgrade

DEFAULT_EXPANDED_DIR = Path("data/expanded")
DEFAULT_EXPANDED_CACHE = DEFAULT_EXPANDED_DIR / "daily_kline"
DEFAULT_EXPANDED_INDEX_CACHE = DEFAULT_EXPANDED_DIR / "index"
DEFAULT_OUTPUT_DIR = Path("output/expanded_backtest_v3")
V3_KEY = "v3_atr_risk_budget_hot5_vol_risk_on"
OLD_EXPANDED_BACKTEST_DIR = Path("output/expanded_backtest_v3")
INITIAL_CASH = 1_000_000.0
EXPECTED_REGIME_LABELS = ["积极", "中性", "谨慎", "防守"]
EXPECTED_EXIT_REASONS = [
    "take_profit",
    "stop_loss",
    "timeout",
    "time_stop",
    "environment_exit",
    "limit_down_blocked_exit",
    "same_day_stop_take_conservative",
]


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    """写 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=fr._json_default)


def _df_to_markdown(df: pd.DataFrame, max_rows: int = 20) -> str:
    """不依赖 tabulate 的简易 Markdown 表格。"""
    if df.empty:
        return "No rows."
    sample = df.head(max_rows).fillna("")
    columns = [str(col) for col in sample.columns]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for _, row in sample.iterrows():
        values = [str(row.get(col, ""))[:160].replace("\n", " ") for col in sample.columns]
        lines.append("| " + " | ".join(values) + " |")
    if len(df) > max_rows:
        lines.append(f"\n仅展示前 {max_rows} 行，共 {len(df)} 行。")
    return "\n".join(lines)


def _fmt_pct(value: Any) -> str:
    """百分比格式化。"""
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


def _safe_float(value: Any) -> Optional[float]:
    """安全转 float。"""
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _max_consecutive_losses(trades: pd.DataFrame) -> int:
    """按退出日期计算最大连续亏损笔数。"""
    if trades.empty or "net_return" not in trades.columns:
        return 0
    date_col = "exit_date" if "exit_date" in trades.columns else "entry_date"
    work = trades.copy()
    work["_return"] = pd.to_numeric(work["net_return"], errors="coerce")
    work = work.dropna(subset=["_return"]).sort_values(date_col)
    best = current = 0
    for value in work["_return"]:
        if value < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return int(best)


def _trade_return_stats(trades: pd.DataFrame) -> dict[str, Any]:
    """交易收益统计。"""
    if trades.empty or "net_return" not in trades.columns:
        return {
            "trade_count": 0,
            "win_rate": None,
            "average_return": None,
            "median_return": None,
            "max_loss": None,
            "max_consecutive_losses": 0,
        }
    returns = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    if returns.empty:
        return {
            "trade_count": 0,
            "win_rate": None,
            "average_return": None,
            "median_return": None,
            "max_loss": None,
            "max_consecutive_losses": 0,
        }
    return {
        "trade_count": int(len(returns)),
        "win_rate": round(float((returns > 0).mean()), 6),
        "average_return": round(float(returns.mean()), 6),
        "median_return": round(float(returns.median()), 6),
        "max_loss": round(float(returns.min()), 6),
        "max_consecutive_losses": _max_consecutive_losses(trades),
    }


def _equity_metrics(equity: pd.DataFrame, initial_cash: float = INITIAL_CASH) -> dict[str, Any]:
    """从每日权益曲线计算组合指标。"""
    if equity.empty or "equity" not in equity.columns:
        return {
            "ending_equity": None,
            "total_return": None,
            "annualized_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "calmar": None,
            "average_total_exposure": None,
            "max_total_exposure": None,
        }
    work = equity.copy()
    work["equity"] = pd.to_numeric(work["equity"], errors="coerce")
    work = work.dropna(subset=["equity"])
    if work.empty:
        return {
            "ending_equity": None,
            "total_return": None,
            "annualized_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "calmar": None,
            "average_total_exposure": None,
            "max_total_exposure": None,
        }
    ending_equity = float(work.iloc[-1]["equity"])
    day_count = max(1, len(work))
    total_return = ending_equity / initial_cash - 1
    annualized = (ending_equity / initial_cash) ** (252 / day_count) - 1 if ending_equity > 0 else None
    if "daily_return" in work.columns:
        daily_returns = pd.to_numeric(work["daily_return"], errors="coerce").dropna()
    else:
        daily_returns = work["equity"].pct_change().fillna(0.0)
    sharpe = None
    if len(daily_returns) > 1 and daily_returns.std(ddof=1) != 0:
        sharpe = float(daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252))
    if "drawdown" in work.columns:
        drawdown = pd.to_numeric(work["drawdown"], errors="coerce")
    else:
        peak = work["equity"].cummax()
        drawdown = work["equity"] / peak - 1
    max_drawdown = float(drawdown.min()) if len(drawdown.dropna()) else 0.0
    calmar = float(annualized / abs(max_drawdown)) if annualized is not None and max_drawdown < 0 else None
    exposure = pd.to_numeric(work.get("gross_exposure_pct"), errors="coerce")
    return {
        "ending_equity": round(ending_equity, 2),
        "total_return": round(float(total_return), 6),
        "annualized_return": round(float(annualized), 6) if annualized is not None else None,
        "max_drawdown": round(max_drawdown, 6),
        "sharpe": round(sharpe, 6) if sharpe is not None else None,
        "calmar": round(calmar, 6) if calmar is not None else None,
        "average_total_exposure": round(float(exposure.mean()), 6) if len(exposure.dropna()) else None,
        "max_total_exposure": round(float(exposure.max()), 6) if len(exposure.dropna()) else None,
    }


def _positive_month_stats(monthly: pd.DataFrame) -> dict[str, Any]:
    """月度正收益比例和极值月份。"""
    if monthly.empty:
        return {
            "positive_month_rate": None,
            "worst_month": None,
            "worst_month_return": None,
            "best_month": None,
            "best_month_return": None,
        }
    return_col = "return" if "return" in monthly.columns else "monthly_return"
    period_col = "period" if "period" in monthly.columns else "month"
    returns = pd.to_numeric(monthly.get(return_col), errors="coerce")
    valid = monthly.loc[returns.notna()].copy()
    valid["_return"] = returns[returns.notna()].values
    if valid.empty:
        return {
            "positive_month_rate": None,
            "worst_month": None,
            "worst_month_return": None,
            "best_month": None,
            "best_month_return": None,
        }
    worst = valid.loc[valid["_return"].idxmin()]
    best = valid.loc[valid["_return"].idxmax()]
    return {
        "positive_month_rate": round(float((valid["_return"] > 0).mean()), 6),
        "worst_month": str(worst.get(period_col)),
        "worst_month_return": round(float(worst.get("_return")), 6),
        "best_month": str(best.get(period_col)),
        "best_month_return": round(float(best.get("_return")), 6),
    }


def _cache_dir_or_fallback(cache_dir: Path) -> tuple[Path, str]:
    """选择可用股票 K 线缓存。"""
    if cache_dir.exists() and any(cache_dir.glob("*.csv")):
        if cache_dir.resolve() == fr.DEFAULT_CACHE_DIR.resolve():
            return cache_dir, "local_cache_explicit"
        return cache_dir, "expanded"
    return fr.DEFAULT_CACHE_DIR, "local_cache_fallback"


def _index_cache_or_fallback(index_cache_dir: Path) -> tuple[Path, str]:
    """选择可用指数 K 线缓存。"""
    if index_cache_dir.exists() and any(index_cache_dir.glob("*.csv")):
        if index_cache_dir.resolve() == fr.DEFAULT_INDEX_CACHE_DIR.resolve():
            return index_cache_dir, "local_index_cache_explicit"
        return index_cache_dir, "expanded_index"
    return fr.DEFAULT_INDEX_CACHE_DIR, "local_index_cache_fallback"


def _read_csv(path: Path) -> pd.DataFrame:
    """读取 CSV，不存在时返回空表。"""
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def _accepted_trades(portfolio: pd.DataFrame) -> pd.DataFrame:
    """取组合接受交易。"""
    if portfolio.empty or "portfolio_action" not in portfolio.columns:
        return pd.DataFrame()
    return portfolio[portfolio["portfolio_action"] == "accepted"].copy()


def _event_group_summary(df: pd.DataFrame, field: str, expected_values: Optional[list[str]] = None) -> pd.DataFrame:
    """按事件交易字段汇总。"""
    if df.empty or field not in df.columns:
        values = expected_values or []
        return pd.DataFrame([{field: value, **_trade_return_stats(pd.DataFrame())} for value in values])
    rows = []
    grouped = {value: group.copy() for value, group in df.groupby(field, dropna=False)}
    values = list(expected_values or [])
    for value in grouped:
        normalized = value if value else "unknown"
        if normalized not in values:
            values.append(normalized)
    for value in values:
        group = grouped.get(value, pd.DataFrame())
        stats = _trade_return_stats(group)
        exit_reasons = group["exit_reason"] if not group.empty and "exit_reason" in group.columns else pd.Series(dtype=object)
        rows.append(
            {
                field: value if value else "unknown",
                **stats,
                "stop_loss_count": int((exit_reasons == "stop_loss").sum()),
                "limit_down_blocked_exit_count": int((exit_reasons == "limit_down_blocked_exit").sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("trade_count", ascending=False)


def _valid_industry_label(value: Any) -> Optional[str]:
    """返回真实行业标签；市场后缀和占位值不当作行业。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"SH", "SZ", "BJ"} or text in {"未分类", "unknown", "nan", "None"}:
        return None
    return text


def _normalize_industry_labels(trades: pd.DataFrame, expanded_dir: Path) -> pd.DataFrame:
    """使用扩展数据行业表重写交易行业标签，缺失时显式标记，不用市场后缀冒充行业。"""
    if trades.empty:
        return trades
    work = trades.copy()
    industry_path = expanded_dir / "industry_or_sector.csv"
    lookup: dict[str, str] = {}
    if industry_path.exists():
        industry = pd.read_csv(industry_path, encoding="utf-8-sig")
        if {"ts_code", "industry"}.issubset(industry.columns):
            for _, row in industry.iterrows():
                label = _valid_industry_label(row.get("industry")) or _valid_industry_label(row.get("sector"))
                if label:
                    lookup[str(row.get("ts_code"))] = label
    work["industry"] = work["symbol"].map(lookup).fillna("行业缺失")
    work["industry_source"] = work["symbol"].map(lambda symbol: "expanded_industry_or_sector" if str(symbol) in lookup else "missing")
    return work


def _period_equity_performance(equity: pd.DataFrame, freq: str) -> pd.DataFrame:
    """从每日权益曲线生成年度/月度组合表现。"""
    if equity.empty or "date" not in equity.columns or "equity" not in equity.columns:
        return pd.DataFrame()
    work = equity.copy()
    work["date"] = pd.to_datetime(work["date"])
    work["period"] = work["date"].dt.to_period(freq).astype(str)
    rows = []
    for period, group in work.groupby("period"):
        start_equity = float(group.iloc[0]["equity"])
        end_equity = float(group.iloc[-1]["equity"])
        drawdown = pd.to_numeric(group.get("drawdown"), errors="coerce")
        rows.append(
            {
                "period": period,
                "start_equity": round(start_equity, 2),
                "end_equity": round(end_equity, 2),
                "return": round(end_equity / start_equity - 1, 6) if start_equity else None,
                "max_drawdown": round(float(drawdown.min()), 6) if len(drawdown.dropna()) else None,
            }
        )
    return pd.DataFrame(rows)


def _max_drawdown_window(equity: pd.DataFrame) -> dict[str, Any]:
    """计算最大回撤区间。"""
    if equity.empty or "equity" not in equity.columns:
        return {"max_drawdown": None, "start": None, "end": None}
    work = equity.copy()
    work["equity"] = pd.to_numeric(work["equity"], errors="coerce")
    work["peak"] = work["equity"].cummax()
    work["drawdown"] = work["equity"] / work["peak"] - 1
    trough_idx = int(work["drawdown"].idxmin())
    peak_idx = int(work.loc[:trough_idx, "equity"].idxmax())
    return {
        "max_drawdown": round(float(work.loc[trough_idx, "drawdown"]), 6),
        "start": str(work.loc[peak_idx, "date"]),
        "end": str(work.loc[trough_idx, "date"]),
    }


def _enrich_comparison(comparison: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """用各策略权益曲线和交易明细补齐全区间指标。"""
    if comparison.empty or "strategy_key" not in comparison.columns:
        return comparison
    rows: list[dict[str, Any]] = []
    for _, row in comparison.iterrows():
        payload = row.to_dict()
        key = str(payload.get("strategy_key"))
        equity = _read_csv(output_dir / f"{key}_daily_equity.csv")
        accepted = _read_csv(output_dir / f"{key}_accepted_trades.csv")
        trades = _read_csv(output_dir / f"{key}_trades.csv")
        monthly = _read_csv(output_dir / f"{key}_monthly_returns.csv")
        payload.update({k: v for k, v in _equity_metrics(equity).items() if v is not None})
        stats = _trade_return_stats(accepted)
        payload.update(
            {
                "event_trade_count": int(len(trades)) if not trades.empty else int(payload.get("event_trade_count") or 0),
                "accepted_trade_count": int(len(accepted)) if not accepted.empty else int(payload.get("accepted_trade_count") or 0),
                "win_rate": stats.get("win_rate"),
                "average_trade_return": stats.get("average_return"),
                "median_trade_return": stats.get("median_return"),
                "max_consecutive_losses": stats.get("max_consecutive_losses"),
                "stop_loss_count": int((accepted.get("exit_reason") == "stop_loss").sum()) if not accepted.empty else 0,
                "limit_down_blocked_exit_count": int((accepted.get("exit_reason") == "limit_down_blocked_exit").sum()) if not accepted.empty else 0,
            }
        )
        payload.update({k: v for k, v in _positive_month_stats(monthly).items() if v is not None})
        rows.append(payload)
    return pd.DataFrame(rows)


def _market_regime_summary(v3_accepted: pd.DataFrame, v3_equity: pd.DataFrame) -> pd.DataFrame:
    """同时输出事件级和组合权益曲线级的市场环境表现。"""
    event = _event_group_summary(v3_accepted, "market_regime_label", EXPECTED_REGIME_LABELS)
    if v3_equity.empty or "date" not in v3_equity.columns:
        for col in ["portfolio_return", "portfolio_max_drawdown"]:
            event[col] = None
        return event
    timeline = r3._market_timeline()
    equity = v3_equity.copy()
    equity["market_regime_label"] = equity["date"].map(lambda date: (timeline.get(str(date)) or {}).get("regime_label") or "unknown")
    rows = []
    for label in EXPECTED_REGIME_LABELS:
        group = equity[equity["market_regime_label"] == label].copy()
        if group.empty:
            rows.append({"market_regime_label": label, "portfolio_return": None, "portfolio_max_drawdown": None})
            continue
        daily_returns = pd.to_numeric(group.get("daily_return"), errors="coerce").fillna(0.0)
        curve = (1 + daily_returns).cumprod()
        drawdown = curve / curve.cummax() - 1
        rows.append(
            {
                "market_regime_label": label,
                "portfolio_return": round(float(curve.iloc[-1] - 1), 6) if len(curve) else None,
                "portfolio_max_drawdown": round(float(drawdown.min()), 6) if len(drawdown) else None,
            }
        )
    portfolio = pd.DataFrame(rows)
    return event.merge(portfolio, on="market_regime_label", how="left")


def _build_risk_budget_exposure_audit(stress: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """输出压力测试和风险预算场景的仓位与敞口口径。"""
    if stress.empty:
        audit = pd.DataFrame()
        _write_csv(output_dir / "risk_budget_exposure_audit.csv", audit)
        return audit
    rows: list[dict[str, Any]] = []
    for _, row in stress.iterrows():
        key = str(row.get("key") or row.get("strategy_key") or "")
        accepted = _read_csv(output_dir / "stress_tests" / f"stress_{key}_accepted_trades.csv")
        equity = _read_csv(output_dir / "stress_tests" / f"stress_{key}_daily_equity.csv")
        position_col = "portfolio_position_pct" if "portfolio_position_pct" in accepted.columns else "position_pct"
        positions = pd.to_numeric(accepted.get(position_col), errors="coerce") if not accepted.empty else pd.Series(dtype=float)
        exposure = pd.to_numeric(equity.get("gross_exposure_pct"), errors="coerce") if not equity.empty else pd.Series(dtype=float)
        rows.append(
            {
                "scenario_key": key,
                "scenario_label": row.get("label") or row.get("strategy_label"),
                "scenario_group": row.get("scenario_group") or row.get("group"),
                "accepted_trade_count": int(row.get("accepted_trade_count") or len(accepted)),
                "total_return": row.get("total_return"),
                "max_drawdown": row.get("max_drawdown"),
                "sharpe": row.get("sharpe"),
                "calmar": row.get("calmar"),
                "average_position_pct": round(float(positions.mean()), 6) if len(positions.dropna()) else None,
                "max_single_position_pct": round(float(positions.max()), 6) if len(positions.dropna()) else None,
                "average_total_exposure": round(float(exposure.mean()), 6) if len(exposure.dropna()) else None,
                "max_total_exposure": round(float(exposure.max()), 6) if len(exposure.dropna()) else None,
            }
        )
    audit = pd.DataFrame(rows)
    _write_csv(output_dir / "risk_budget_exposure_audit.csv", audit)
    return audit


def _old_backtest_comparison(current: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """对比旧 2024-2026 扩展回测和本轮 2021-2026 回测。"""
    old_path = OLD_EXPANDED_BACKTEST_DIR / "expanded_strategy_comparison.csv"
    if not old_path.exists() or current.empty:
        result = pd.DataFrame([{"status": "missing_old_backtest", "path": str(old_path)}])
        _write_csv(output_dir / "old_2024_2026_comparison.csv", result)
        return result
    old = pd.read_csv(old_path, encoding="utf-8-sig")
    metrics = [
        "event_trade_count",
        "accepted_trade_count",
        "total_return",
        "annualized_return",
        "max_drawdown",
        "sharpe",
        "calmar",
        "win_rate",
        "average_trade_return",
        "median_trade_return",
        "max_consecutive_losses",
        "stop_loss_count",
        "limit_down_blocked_exit_count",
    ]
    rows: list[dict[str, Any]] = []
    for strategy_key in sorted(set(old.get("strategy_key", [])) | set(current.get("strategy_key", []))):
        old_row = old[old["strategy_key"] == strategy_key].iloc[0].to_dict() if strategy_key in set(old.get("strategy_key", [])) else {}
        new_row = current[current["strategy_key"] == strategy_key].iloc[0].to_dict() if strategy_key in set(current.get("strategy_key", [])) else {}
        for metric in metrics:
            old_value = _safe_float(old_row.get(metric))
            new_value = _safe_float(new_row.get(metric))
            rows.append(
                {
                    "strategy_key": strategy_key,
                    "metric": metric,
                    "old_2024_2026": old_value,
                    "new_2021_2026": new_value,
                    "diff": round(float(new_value - old_value), 6) if old_value is not None and new_value is not None else None,
                }
            )
    result = pd.DataFrame(rows)
    _write_csv(output_dir / "old_2024_2026_comparison.csv", result)
    return result


def _write_environment_exit_diagnosis(v3_accepted: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """输出 environment_exit 归因报告。"""
    env_exit = v3_accepted[v3_accepted.get("exit_reason") == "environment_exit"].copy() if not v3_accepted.empty else pd.DataFrame()
    stats = _trade_return_stats(env_exit)
    if not env_exit.empty:
        env_exit["exit_year"] = pd.to_datetime(env_exit["exit_date"], errors="coerce").dt.year.astype("Int64").astype(str)
        by_year = _event_group_summary(env_exit, "exit_year")
        by_regime = _event_group_summary(env_exit, "market_regime_label", EXPECTED_REGIME_LABELS)
        sample = env_exit.sort_values("net_return").head(20)[
            ["symbol", "name", "industry", "entry_date", "exit_date", "market_regime_label", "net_return", "blocked_exit_days"]
        ]
    else:
        by_year = pd.DataFrame()
        by_regime = _event_group_summary(env_exit, "market_regime_label", EXPECTED_REGIME_LABELS)
        sample = pd.DataFrame()
    _write_csv(output_dir / "environment_exit_by_year.csv", by_year)
    _write_csv(output_dir / "environment_exit_by_regime.csv", by_regime)
    _write_csv(output_dir / "environment_exit_worst_samples.csv", sample)
    lines = [
        "# Environment Exit Diagnosis",
        "",
        "本诊断只作为 shadow 归因，不修改 `alpha040_v3_risk_controlled` 主策略规则。",
        "",
        "## Summary",
        "",
        f"- 笔数：{stats.get('trade_count')}",
        f"- 平均单笔：{_fmt_pct(stats.get('average_return'))}",
        f"- 中位数：{_fmt_pct(stats.get('median_return'))}",
        f"- 最大亏损：{_fmt_pct(stats.get('max_loss'))}",
        f"- 最大连续亏损：{stats.get('max_consecutive_losses')}",
        "",
        "## By Year",
        "",
        _df_to_markdown(by_year),
        "",
        "## By Market Regime",
        "",
        _df_to_markdown(by_regime),
        "",
        "## Worst Samples",
        "",
        _df_to_markdown(sample),
        "",
        "## Interpretation",
        "",
        "- `environment_exit` 反映持仓期间市场环境变化后的退出纪律，不代表入场环境被放宽。",
        "- 本轮只记录归因和样本分布，不根据结果调参。",
    ]
    (output_dir / "environment_exit_diagnosis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_exit


def _active_dates_from_daily_universe(output_dir: Path, histories: dict[str, list[dict]]) -> list[str]:
    """读取 Round6 动态 universe 日期。"""
    daily_universe = _read_csv(output_dir / "daily_universe.csv")
    if not daily_universe.empty:
        date_col = "date" if "date" in daily_universe.columns else "trade_date"
        count_col = "stock_count" if "stock_count" in daily_universe.columns else "final_universe_count"
        active = daily_universe[pd.to_numeric(daily_universe[count_col], errors="coerce").fillna(0) > 0]
        dates = sorted(str(x) for x in active[date_col].dropna().tolist())
        if dates:
            return dates
    return sorted(
        {
            str(bar.get("date"))
            for bars in histories.values()
            for bar in bars
            if bar.get("date")
        }
    )


def _run_legacy_comparison(cache_dir: Path, output_dir: Path, active_dates: list[str]) -> dict[str, Any]:
    """运行 legacy_momentum_v1 对比。"""
    histories = fr.load_cached_histories(cache_dir=cache_dir, min_bars=80)
    if not histories:
        return {"summary_row": {}, "accepted": pd.DataFrame()}
    result = upgrade._run_legacy_validation(
        histories=histories,
        active_dates=active_dates,
        output_dir=output_dir,
        legacy_strategy_path=Path("strategy_legacy_v1.json"),
    )
    return result


def _run_stress_tests(cache_dir: Path, output_dir: Path) -> pd.DataFrame:
    """运行固定 V3 压力测试和仓位口径审计。"""
    stress_dir = output_dir / "stress_tests"
    context = freeze._load_context(
        output_dir=stress_dir / "context",
        cache_dir=cache_dir,
        max_symbols=None,
        min_bars=80,
        min_history=60,
        train_ratio=0.7,
        strategy_path="strategy.json",
    )
    v3_strategy = [strategy for strategy in freeze._freeze_strategies() if strategy.key == freeze.FREEZE_VERSION][0]
    scenarios = [
        {"key": "base", "label": "V3 基准", "fee_bps": 5.0, "slippage_bps": 10.0, "max_positions": 4, "risk_budget_pct": r4.DEFAULT_RISK_BUDGET_PCT, "group": "base"},
        {"key": "cost_double", "label": "交易成本加倍", "fee_bps": 10.0, "slippage_bps": 10.0, "max_positions": 4, "risk_budget_pct": r4.DEFAULT_RISK_BUDGET_PCT, "group": "cost"},
        {"key": "slippage_double", "label": "滑点加倍", "fee_bps": 5.0, "slippage_bps": 20.0, "max_positions": 4, "risk_budget_pct": r4.DEFAULT_RISK_BUDGET_PCT, "group": "slippage"},
    ]
    for max_positions in [2, 3, 4]:
        scenarios.append({"key": f"max_positions_{max_positions}", "label": f"最大持仓数 {max_positions}", "fee_bps": 5.0, "slippage_bps": 10.0, "max_positions": max_positions, "risk_budget_pct": r4.DEFAULT_RISK_BUDGET_PCT, "group": "max_positions"})
    for risk_budget in [0.005, 0.0075, 0.01]:
        scenarios.append({"key": f"risk_budget_{str(risk_budget).replace('.', '_')}", "label": f"风险预算 {risk_budget:.2%}", "fee_bps": 5.0, "slippage_bps": 10.0, "max_positions": 4, "risk_budget_pct": risk_budget, "group": "risk_budget"})

    rows = []
    for scenario in scenarios:
        result = freeze._run_strategy(
            v3_strategy,
            context=context,
            thresholds=freeze._fixed_thresholds(),
            fee_bps=scenario["fee_bps"],
            slippage_bps=scenario["slippage_bps"],
            max_positions=scenario["max_positions"],
            risk_budget_pct=scenario["risk_budget_pct"],
            output_dir=stress_dir,
            prefix=f"stress_{scenario['key']}",
            write_files=True,
        )
        rows.append(
            {
                **freeze._summary_row(
                result,
                key=scenario["key"],
                label=scenario["label"],
                group=scenario["group"],
                ),
                "fee_bps": scenario["fee_bps"],
                "slippage_bps": scenario["slippage_bps"],
                "max_positions": scenario["max_positions"],
                "risk_budget_pct": scenario["risk_budget_pct"],
            }
        )
    stress = pd.DataFrame(rows)
    _write_csv(output_dir / "stress_test_summary.csv", stress)
    return stress


def _freeze_cache_comparison(current_v3: pd.Series, output_dir: Path) -> pd.DataFrame:
    """与当前 Freeze V3 缓存样本结果对比。"""
    freeze_path = Path("output/factor_research_round6/strategy_comparison.csv")
    if not freeze_path.exists():
        result = pd.DataFrame([{"status": "missing_freeze_cache_result", "path": str(freeze_path)}])
        _write_csv(output_dir / "freeze_cache_comparison.csv", result)
        return result
    old = pd.read_csv(freeze_path, encoding="utf-8-sig")
    old_v3 = old[old["strategy_key"] == V3_KEY]
    if old_v3.empty:
        result = pd.DataFrame([{"status": "missing_v3_row", "path": str(freeze_path)}])
        _write_csv(output_dir / "freeze_cache_comparison.csv", result)
        return result
    old_row = old_v3.iloc[0]
    metrics = ["event_trade_count", "accepted_trade_count", "total_return", "max_drawdown", "sharpe", "calmar", "max_consecutive_losses", "stop_loss_count", "limit_down_blocked_exit_count"]
    rows = []
    for metric in metrics:
        current = fr._safe_float(current_v3.get(metric))
        previous = fr._safe_float(old_row.get(metric))
        rows.append({"metric": metric, "expanded_backtest": current, "freeze_cache_sample": previous, "diff": current - previous if current is not None and previous is not None else None})
    result = pd.DataFrame(rows)
    _write_csv(output_dir / "freeze_cache_comparison.csv", result)
    return result


def _render_report(
    *,
    output_dir: Path,
    data_source: str,
    selected_cache: Path,
    selected_index_cache: Path,
    comparison: pd.DataFrame,
    yearly: pd.DataFrame,
    monthly: pd.DataFrame,
    market: pd.DataFrame,
    industry: pd.DataFrame,
    exit_reason: pd.DataFrame,
    stress: pd.DataFrame,
    risk_audit: pd.DataFrame,
    freeze_compare: pd.DataFrame,
    old_compare: pd.DataFrame,
    drawdown_window: dict[str, Any],
    data_boundary: str,
) -> str:
    """生成扩展回测报告。"""
    v3_row = comparison[comparison["strategy_key"] == V3_KEY].iloc[0] if not comparison[comparison["strategy_key"] == V3_KEY].empty else pd.Series(dtype=object)
    legacy_row = comparison[comparison["strategy_key"] == "legacy_momentum_v1"].iloc[0] if not comparison[comparison["strategy_key"] == "legacy_momentum_v1"].empty else pd.Series(dtype=object)
    monthly_stats = _positive_month_stats(monthly)
    if {"industry", "trade_count"}.issubset(industry.columns):
        missing_industry_rows = industry[industry["industry"] == "行业缺失"]
        industry_missing = int(pd.to_numeric(missing_industry_rows["trade_count"], errors="coerce").fillna(0).sum())
    else:
        industry_missing = None
    v3_vs_legacy = ""
    v3_return = _safe_float(v3_row.get("total_return"))
    legacy_return = _safe_float(legacy_row.get("total_return"))
    v3_dd = _safe_float(v3_row.get("max_drawdown"))
    legacy_dd = _safe_float(legacy_row.get("max_drawdown"))
    if v3_return is not None and legacy_return is not None and v3_dd is not None and legacy_dd is not None:
        v3_vs_legacy = (
            f"V3 全区间收益 {_fmt_pct(v3_return)}，legacy {_fmt_pct(legacy_return)}；"
            f"V3 最大回撤 {_fmt_pct(v3_dd)}，legacy {_fmt_pct(legacy_dd)}。"
        )
    lines = [
        "# Expanded Backtest V3 Report",
        "",
        "本报告仅用于个人模拟交易、复盘和技术研究；不构成投资建议，不连接实盘，不自动下单。",
        "",
        "## Technical Summary",
        "",
        f"- 本轮使用 2021 起扩展数据复跑，数据边界为：{data_boundary}",
        f"- {v3_vs_legacy}" if v3_vs_legacy else "- V3 与 legacy 的对比见下方全区间表现表。",
        "- 本轮未调参、未新增因子、未修改 `alpha040_v3_risk_controlled` 固定规则。",
        "- 如果 V3 收益相对 2024 旧样本变化明显，只能解释为样本扩展后的表现回归或环境覆盖变化，不能据此自动调参。",
        "",
        "## Scope And Data Boundary",
        "",
        f"- Strategy: `alpha040_v3_risk_controlled`",
        f"- Legacy comparison: `legacy_momentum_v1`",
        f"- Backtest range: `2021-01-04` to `2026-06-26`",
        f"- Old comparison range: `2024-01-02` to `2026-06-26`",
        f"- Data source mode: `{data_source}`",
        f"- Stock cache: `{selected_cache}`",
        f"- Index cache: `{selected_index_cache}`",
        f"- Data boundary: {data_boundary}",
        "",
        "## 1. 全区间组合表现",
        "",
        _df_to_markdown(comparison),
        "",
        "## 2. 按年份表现",
        "",
        _df_to_markdown(yearly),
        "",
        "## 3. 按月份表现",
        "",
        _df_to_markdown(monthly, max_rows=36),
        "",
        f"- 正收益月份占比：{_fmt_pct(monthly_stats.get('positive_month_rate'))}",
        f"- 最差月份：{monthly_stats.get('worst_month')}（{_fmt_pct(monthly_stats.get('worst_month_return'))}）",
        f"- 最好月份：{monthly_stats.get('best_month')}（{_fmt_pct(monthly_stats.get('best_month_return'))}）",
        "",
        "## 4. 按市场环境表现",
        "",
        "市场环境表同时包含事件级交易统计和组合级权益曲线统计；两者口径不同，不混用最大回撤。",
        "",
        _df_to_markdown(market),
        "",
        "## 5. 按行业表现",
        "",
        "行业字段只使用 `data/expanded/industry_or_sector.csv` 中的真实行业；缺失时统一标为“行业缺失”，不使用 SH/SZ/BJ 市场后缀代替行业。",
        "",
        f"- 行业缺失数量：{industry_missing}",
        "- 行业集中度 Top 10 见下表前 10 行。",
        "",
        _df_to_markdown(industry, max_rows=30),
        "",
        "## 6. 退出原因表现",
        "",
        _df_to_markdown(exit_reason),
        "",
        "## 7. Environment Exit 归因",
        "",
        f"- 诊断报告：`{output_dir / 'environment_exit_diagnosis.md'}`",
        "- 本节只做 shadow 诊断，不修改主策略。",
        "",
        "## 8. 止损 / 跌停无法卖出 / 最大连续亏损",
        "",
    ]
    if not v3_row.empty:
        row = v3_row
        lines.extend(
            [
                f"- 止损次数：{row.get('stop_loss_count')}",
                f"- 跌停无法卖出次数：{row.get('limit_down_blocked_exit_count')}",
                f"- 最大连续亏损：{row.get('max_consecutive_losses')}",
            ]
        )
    lines.extend(
        [
            "",
            "## 9. 最大回撤区间",
            "",
            f"- 最大回撤：{_fmt_pct(drawdown_window.get('max_drawdown'))}",
            f"- 起点：{drawdown_window.get('start')}",
            f"- 终点：{drawdown_window.get('end')}",
            "",
            "## 10. 成本、滑点、持仓数、风险预算压力测试",
            "",
            _df_to_markdown(stress),
            "",
            "## 11. 风险预算敞口审计",
            "",
            "风险预算收益不能脱离仓位口径解释；下表列出平均单票仓位、最大单票仓位、平均总敞口和最大总敞口。",
            "",
            _df_to_markdown(risk_audit),
            "",
            "## 12. 与旧版 2024-2026 扩展回测对比",
            "",
            "旧结果来自 `output/expanded_backtest_v3/`，区间为 2024-01-02 至 2026-06-26；新结果来自本报告，区间为 2021-01-04 至 2026-06-26。",
            "",
            _df_to_markdown(old_compare, max_rows=60),
            "",
            "## 13. 与 Freeze V3 缓存样本对比",
            "",
            _df_to_markdown(freeze_compare),
            "",
            "## 14. 结论",
            "",
            "### A. 可以确认的事实",
            "",
            "- 本轮使用 2021 起扩展数据复跑，且未修改主策略、阈值或因子。",
            "- 本轮输出了全区间、年份、月份、市场环境、行业、退出原因、压力测试、风险预算敞口审计和旧版对比。",
            "- 当前数据源仍以 efinance 单源日线为主，不能宣传为多源完整全市场长期回测。",
            "",
            "### B. 仍然不确定的问题",
            "",
            "- efinance 单源数据与 Fuyao/Tushare 多源校验尚未完成，数据源一致性仍需后续补证。",
            "- 样本扩展后若收益下降，只能说明更长样本下表现回归或环境变化，不能直接推出应调参。",
            "- 行业集中和 environment_exit 的长期稳定性仍需 forward paper trading 继续观察。",
            "",
            "### C. 下一步建议",
            "",
            "- 先人工复核本报告与 `environment_exit_diagnosis.md`，不要立即改策略。",
            "- 继续运行 forward paper trading，累计 30-50 笔闭环模拟交易后再讨论 shadow 实验。",
            "- 后续若拿到 Fuyao/Tushare 可用历史权限，再做多源一致性复跑。",
            "",
            "## 15. 重要边界",
            "",
            "- 本轮没有修改 `alpha040_v3_risk_controlled` 冻结规则。",
            "- 本轮没有新增因子、没有调参、没有连接实盘、没有自动下单。",
            "- 如果数据源降级为本地缓存或 universe 覆盖不足，则当前扩展回测仍不能视为完整全市场长期回测。",
        ]
    )
    report = "\n".join(lines) + "\n"
    (output_dir / "expanded_backtest_report.md").write_text(report, encoding="utf-8")
    return report


def run_expanded_backtest(
    *,
    cache_dir: Path = DEFAULT_EXPANDED_CACHE,
    index_cache_dir: Path = DEFAULT_EXPANDED_INDEX_CACHE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    min_bars: int = 80,
    min_history: int = 60,
) -> dict[str, Any]:
    """运行扩展数据正式回测。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_cache, data_source = _cache_dir_or_fallback(cache_dir)
    selected_index_cache, index_source = _index_cache_or_fallback(index_cache_dir)
    old_index_cache = fr.DEFAULT_INDEX_CACHE_DIR
    fr.DEFAULT_INDEX_CACHE_DIR = selected_index_cache
    try:
        summary = r6.run_factor_research_round6(
            output_dir=output_dir,
            cache_dir=selected_cache,
            min_bars=min_bars,
            min_history=min_history,
            strategy_path="strategy.json",
        )
        histories = fr.load_cached_histories(cache_dir=selected_cache, min_bars=min_bars)
        active_dates = _active_dates_from_daily_universe(output_dir, histories)
        legacy_result = _run_legacy_comparison(selected_cache, output_dir, active_dates)
        comparison = _read_csv(output_dir / "strategy_comparison.csv")
        if legacy_result.get("summary_row"):
            comparison = pd.concat([pd.DataFrame([legacy_result["summary_row"]]), comparison], ignore_index=True)
        comparison = _enrich_comparison(comparison, output_dir)
        _write_csv(output_dir / "expanded_strategy_comparison.csv", comparison)

        v3_trades = _read_csv(output_dir / f"{V3_KEY}_trades.csv")
        v3_accepted = _read_csv(output_dir / f"{V3_KEY}_accepted_trades.csv")
        expanded_dir = selected_cache.parent if selected_cache.name == "daily_kline" else selected_cache
        v3_trades = _normalize_industry_labels(v3_trades, expanded_dir)
        v3_accepted = _normalize_industry_labels(v3_accepted, expanded_dir)
        _write_csv(output_dir / f"{V3_KEY}_trades.csv", v3_trades)
        _write_csv(output_dir / f"{V3_KEY}_accepted_trades.csv", v3_accepted)
        v3_equity = _read_csv(output_dir / f"{V3_KEY}_daily_equity.csv")
        v3_monthly = _read_csv(output_dir / f"{V3_KEY}_monthly_returns.csv")
        yearly = _period_equity_performance(v3_equity, "Y")
        monthly = v3_monthly.rename(columns={"month": "period", "monthly_return": "return"}) if not v3_monthly.empty else _period_equity_performance(v3_equity, "M")
        market = _market_regime_summary(v3_accepted, v3_equity)
        industry = _event_group_summary(v3_accepted, "industry")
        exit_reason = _event_group_summary(v3_accepted, "exit_reason", EXPECTED_EXIT_REASONS)
        _write_csv(output_dir / "yearly_performance.csv", yearly)
        _write_csv(output_dir / "monthly_returns.csv", monthly)
        _write_csv(output_dir / "monthly_performance.csv", monthly)
        _write_csv(output_dir / "market_regime_performance.csv", market)
        _write_csv(output_dir / "industry_performance.csv", industry)
        _write_csv(output_dir / "exit_reason_performance.csv", exit_reason)
        _write_csv(output_dir / "risk_budget_position_audit.csv", v3_accepted)
        _write_environment_exit_diagnosis(v3_accepted, output_dir)
        stress = _run_stress_tests(selected_cache, output_dir)
        risk_audit = _build_risk_budget_exposure_audit(stress, output_dir)
        v3_row = comparison[comparison["strategy_key"] == V3_KEY].iloc[0] if not comparison[comparison["strategy_key"] == V3_KEY].empty else pd.Series(dtype=object)
        freeze_compare = _freeze_cache_comparison(v3_row, output_dir)
        old_compare = _old_backtest_comparison(comparison, output_dir)
        drawdown_window = _max_drawdown_window(v3_equity)
        data_boundary = "expanded_data" if data_source == "expanded" else "当前扩展回测使用本地缓存降级样本，不能视为完整全市场长期回测。"
        report = _render_report(
            output_dir=output_dir,
            data_source=data_source,
            selected_cache=selected_cache,
            selected_index_cache=selected_index_cache,
            comparison=comparison,
            yearly=yearly,
            monthly=monthly,
            market=market,
            industry=industry,
            exit_reason=exit_reason,
            stress=stress,
            risk_audit=risk_audit,
            freeze_compare=freeze_compare,
            old_compare=old_compare,
            drawdown_window=drawdown_window,
            data_boundary=data_boundary,
        )
    finally:
        fr.DEFAULT_INDEX_CACHE_DIR = old_index_cache

    result = {
        **summary,
        "module": "expanded_backtest_v3",
        "data_source": data_source,
        "index_source": index_source,
        "selected_cache": str(selected_cache),
        "selected_index_cache": str(selected_index_cache),
        "report_path": str(output_dir / "expanded_backtest_report.md"),
        "report_characters": len(report),
    }
    _write_json(output_dir / "expanded_backtest_summary.json", result)
    return result


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行 Alpha040 V3 扩展数据正式回测")
    parser.add_argument("--cache-dir", default=str(DEFAULT_EXPANDED_CACHE), help="扩展股票 K 线目录")
    parser.add_argument("--index-cache-dir", default=str(DEFAULT_EXPANDED_INDEX_CACHE), help="扩展指数 K 线目录")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--min-bars", type=int, default=80, help="单票最少 K 线数量")
    parser.add_argument("--min-history", type=int, default=60, help="动态 universe 最少历史长度")
    args = parser.parse_args()
    summary = run_expanded_backtest(
        cache_dir=Path(args.cache_dir),
        index_cache_dir=Path(args.index_cache_dir),
        output_dir=Path(args.output),
        min_bars=args.min_bars,
        min_history=args.min_history,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=fr._json_default))


if __name__ == "__main__":
    main()
