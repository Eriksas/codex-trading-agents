"""
factor_research_round6.py - Alpha040 Risk-Controlled Shadow Strategy

输出：output/factor_research_round6/
本模块只读本地历史缓存、Round5 阈值和策略配置，不修改 market_scanner 主策略、不写台账、不连接实盘。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import factor_research_round3 as r3
import factor_research_round4 as r4
import factor_research_round5 as r5
import market_scanner as scanner

DEFAULT_OUTPUT_DIR = Path("output/factor_research_round6")
DEFAULT_ROUND5_DIR = Path("output/factor_research_round5")
RISK_ON_LABEL = "积极"


@dataclass(frozen=True)
class ShadowStrategy:
    """Round6 shadow strategy 配置。"""

    key: str
    label: str
    stop_variant: str
    only_risk_on: bool
    filter_hot_5d: bool
    filter_high_volatility: bool


STRATEGIES = [
    ShadowStrategy(
        key="baseline_current_stop",
        label="Baseline：Alpha040 Core 当前止损",
        stop_variant="current_stop",
        only_risk_on=False,
        filter_hot_5d=False,
        filter_high_volatility=False,
    ),
    ShadowStrategy(
        key="v1_current_stop_hot5_risk_on",
        label="版本1：当前止损 + 5日过热过滤 + 只开积极环境",
        stop_variant="current_stop",
        only_risk_on=True,
        filter_hot_5d=True,
        filter_high_volatility=False,
    ),
    ShadowStrategy(
        key="v2_atr_risk_budget_hot5_risk_on",
        label="版本2：ATR风控仓位 + 5日过热过滤 + 只开积极环境",
        stop_variant="atr_stop_risk_budget",
        only_risk_on=True,
        filter_hot_5d=True,
        filter_high_volatility=False,
    ),
    ShadowStrategy(
        key="v3_atr_risk_budget_hot5_vol_risk_on",
        label="版本3：版本2 + volatility_20d 过滤",
        stop_variant="atr_stop_risk_budget",
        only_risk_on=True,
        filter_hot_5d=True,
        filter_high_volatility=True,
    ),
]


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


def _load_round5_thresholds(round5_dir: Path) -> dict[str, float]:
    """读取 Round5 风险阈值。"""
    path = round5_dir / "stop_risk_filter_thresholds.json"
    if not path.exists():
        raise FileNotFoundError(f"缺少 Round5 阈值文件：{path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    required = ["change_rate_5d_q75", "volatility_20d_q75"]
    missing = [key for key in required if _safe_float(raw.get(key)) is None]
    if missing:
        raise ValueError(f"Round5 阈值文件缺少字段：{missing}")
    return {key: float(raw[key]) for key in raw}


def _passes_strategy_filters(
    item: dict,
    market_profile: dict[str, Any],
    strategy: ShadowStrategy,
    thresholds: dict[str, float],
) -> tuple[bool, str]:
    """判断某个 Alpha040 候选是否通过 Round6 策略过滤。"""
    score = r3._alpha040_core_score(item)
    if score is None:
        return False, "alpha040_core_ineligible"
    if strategy.only_risk_on and market_profile.get("regime_label") != RISK_ON_LABEL:
        return False, "market_not_risk_on"
    if strategy.filter_hot_5d:
        change_5d = _safe_float(item.get("change_rate_5d"))
        if change_5d is None:
            return False, "missing_change_rate_5d"
        if change_5d > thresholds["change_rate_5d_q75"]:
            return False, "hot_5d_filtered"
    if strategy.filter_high_volatility:
        volatility = _safe_float(item.get("volatility_20d"))
        if volatility is None:
            return False, "missing_volatility_20d"
        if volatility > thresholds["volatility_20d_q75"]:
            return False, "high_volatility_filtered"
    return True, "passed"


def _eligible_for_strategy(
    rows: list[dict],
    market_profile: dict[str, Any],
    strategy: ShadowStrategy,
    thresholds: dict[str, float],
) -> tuple[list[dict], dict[str, int]]:
    """返回某策略某日候选及过滤原因计数。"""
    eligible: list[dict] = []
    reason_counts: dict[str, int] = {}
    for item in rows:
        passed, reason = _passes_strategy_filters(item, market_profile, strategy, thresholds)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if not passed:
            continue
        score = r3._alpha040_core_score(item)
        if score is not None:
            eligible.append({**item, "alpha040_core_score": score})
    eligible.sort(key=lambda row: row.get("alpha040_core_score") or -999, reverse=True)
    return eligible, reason_counts


def _build_daily_candidate_counts(
    universe_by_date: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    thresholds: dict[str, float],
) -> tuple[pd.DataFrame, dict[str, dict[str, list[dict]]]]:
    """输出每日候选数量，并缓存各策略候选列表。"""
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
            eligible, reason_counts = _eligible_for_strategy(universe_by_date[date], market_profile, strategy, thresholds)
            candidates_by_strategy[strategy.key][date] = eligible
            row[f"{strategy.key}_candidate_count"] = len(eligible)
            row[f"{strategy.key}_selected_count"] = min(len(eligible), candidate_limit)
            row[f"{strategy.key}_filter_reasons_json"] = json.dumps(reason_counts, ensure_ascii=False)
        rows.append(row)
    return pd.DataFrame(rows), candidates_by_strategy


def _run_strategy_events(
    strategy: ShadowStrategy,
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
    """运行 Round6 单个策略的事件交易回测。"""
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
                strategy.stop_variant,
            )
            if not trade:
                continue
            trade["strategy_key"] = strategy.key
            trade["strategy_label"] = strategy.label
            trade["split"] = "train" if pd.to_datetime(trade["signal_date"]) <= train_end else "test"
            trades.append(trade)
            if trade.get("exit_date"):
                next_available_by_symbol[symbol] = str(trade["exit_date"])
    trades_df = pd.DataFrame(trades)
    return r5._enrich_trades(trades_df, {date: candidates_by_date[date] for date in candidates_by_date}, histories, limit_threshold)


def _accepted_trades(portfolio: pd.DataFrame) -> pd.DataFrame:
    """取组合接受交易。"""
    if portfolio.empty or "portfolio_action" not in portfolio.columns:
        return pd.DataFrame()
    return portfolio[portfolio["portfolio_action"] == "accepted"].copy()


def _trade_stats(trades: pd.DataFrame) -> dict[str, Any]:
    """计算接受交易的胜率、均值、中位数等。"""
    if trades.empty or "net_return" not in trades.columns:
        return {
            "win_rate": None,
            "average_trade_return": None,
            "median_trade_return": None,
            "max_consecutive_losses": 0,
            "stop_loss_count": 0,
            "limit_down_blocked_exit_count": 0,
        }
    returns = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    return {
        "win_rate": round(float((returns > 0).mean()), 6) if len(returns) else None,
        "average_trade_return": round(float(returns.mean()), 6) if len(returns) else None,
        "median_trade_return": round(float(returns.median()), 6) if len(returns) else None,
        "max_consecutive_losses": r5._max_consecutive_losses(trades),
        "stop_loss_count": int((trades["exit_reason"] == "stop_loss").sum()),
        "limit_down_blocked_exit_count": int((trades["exit_reason"] == "limit_down_blocked_exit").sum()),
    }


def _monthly_stability(monthly: pd.DataFrame) -> dict[str, Any]:
    """月度稳定性指标。"""
    return r5._monthly_stability(monthly)


def _run_portfolio(
    strategy: ShadowStrategy,
    trades: pd.DataFrame,
    histories: dict[str, list[dict]],
    active_dates: list[str],
    initial_cash: float,
    output_dir: Path,
) -> dict[str, Any]:
    """生成组合层面输出。"""
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


def _comparison_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """生成策略对比表。"""
    rows: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        result = results[strategy.key]
        metrics = result["metrics"]
        accepted = result["accepted"]
        stats = _trade_stats(accepted)
        monthly_stats = _monthly_stability(result["monthly"])
        rows.append(
            {
                "strategy_key": strategy.key,
                "strategy_label": strategy.label,
                "event_trade_count": int(len(result["trades"])),
                "accepted_trade_count": int(metrics.get("accepted_trades") or 0),
                "ending_equity": metrics.get("ending_equity"),
                "total_return": metrics.get("total_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe": metrics.get("sharpe"),
                "calmar": metrics.get("calmar"),
                **stats,
                "positive_month_rate": monthly_stats.get("positive_month_rate"),
                "negative_month_count": monthly_stats.get("negative_month_count"),
                "worst_month": monthly_stats.get("worst_month"),
                "worst_month_return": monthly_stats.get("worst_month_return"),
                "monthly_return_std": monthly_stats.get("monthly_return_std"),
            }
        )
    return pd.DataFrame(rows)


def _monthly_table(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """生成月度收益宽表。"""
    monthly_maps: dict[str, pd.DataFrame] = {}
    all_months: set[str] = set()
    for key, result in results.items():
        monthly = result["monthly"].copy()
        if "month" not in monthly.columns:
            continue
        monthly_maps[key] = monthly[["month", "monthly_return"]]
        all_months.update(str(x) for x in monthly["month"])
    rows: list[dict[str, Any]] = []
    for month in sorted(all_months):
        row: dict[str, Any] = {"month": month}
        for strategy in STRATEGIES:
            df = monthly_maps.get(strategy.key)
            value = None
            if df is not None:
                match = df[df["month"] == month]
                if not match.empty:
                    value = float(match.iloc[0]["monthly_return"])
            row[strategy.key] = round(value, 6) if value is not None else None
        rows.append(row)
    return pd.DataFrame(rows)


def _candidate_count_summary(daily_counts: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """汇总每日候选数量并判断过滤是否过度。"""
    rows: list[dict[str, Any]] = []
    baseline_active = pd.to_numeric(daily_counts["baseline_current_stop_candidate_count"], errors="coerce").fillna(0) > 0
    for strategy in STRATEGIES:
        col = f"{strategy.key}_candidate_count"
        selected_col = f"{strategy.key}_selected_count"
        series = pd.to_numeric(daily_counts[col], errors="coerce").fillna(0)
        selected = pd.to_numeric(daily_counts[selected_col], errors="coerce").fillna(0)
        all_zero_share = float((series <= 0).mean()) if len(series) else 0.0
        if strategy.only_risk_on:
            evaluation_mask = baseline_active & (daily_counts["market_regime_label"] == RISK_ON_LABEL)
        else:
            evaluation_mask = baseline_active
        eval_series = series[evaluation_mask]
        eval_selected = selected[evaluation_mask]
        zero_share = float((eval_series <= 0).mean()) if len(eval_series) else 0.0
        rows.append(
            {
                "strategy_key": strategy.key,
                "strategy_label": strategy.label,
                "date_count": int(len(series)),
                "all_zero_candidate_days": int((series <= 0).sum()),
                "all_zero_candidate_share": round(all_zero_share, 6),
                "evaluated_date_count": int(len(eval_series)),
                "zero_candidate_days": int((eval_series <= 0).sum()),
                "zero_candidate_share": round(zero_share, 6),
                "median_candidate_count": round(float(eval_series.median()), 4) if len(eval_series) else None,
                "mean_candidate_count": round(float(eval_series.mean()), 4) if len(eval_series) else None,
                "median_selected_count": round(float(eval_selected.median()), 4) if len(eval_selected) else None,
                "mean_selected_count": round(float(eval_selected.mean()), 4) if len(eval_selected) else None,
                "over_filtered": bool(zero_share > 0.5),
            }
        )
    summary = pd.DataFrame(rows)
    v3 = summary[summary["strategy_key"] == "v3_atr_risk_budget_hot5_vol_risk_on"].iloc[0].to_dict()
    audit = {
        "v3_over_filtered": bool(v3["over_filtered"]),
        "v3_zero_candidate_share": v3["zero_candidate_share"],
        "v3_median_candidate_count": v3["median_candidate_count"],
        "criterion": "在允许开仓且 baseline 有候选的评估日期内，zero_candidate_share > 50% 视为大多数日期没有候选。",
    }
    return summary, audit


def _recommend_shadow(comparison: pd.DataFrame, candidate_summary: pd.DataFrame) -> dict[str, Any]:
    """按减少亏损交易的目标给出下一轮 shadow 观察版本。"""
    candidates = comparison[comparison["strategy_key"] != "baseline_current_stop"].copy()
    merged = candidates.merge(candidate_summary[["strategy_key", "over_filtered"]], on="strategy_key", how="left")
    usable = merged[~merged["over_filtered"].fillna(False)].copy()
    if usable.empty:
        return {
            "recommended_shadow_variant": "v1_current_stop_hot5_risk_on",
            "reason": "更强过滤版本候选不足，优先保留过滤较轻版本继续观察；不自动并入主策略。",
        }
    usable["risk_exit_count"] = usable["stop_loss_count"] + usable["limit_down_blocked_exit_count"]
    usable = usable.sort_values(["risk_exit_count", "max_drawdown", "max_consecutive_losses"], ascending=[True, False, True])
    best = usable.iloc[0]
    return {
        "recommended_shadow_variant": str(best["strategy_key"]),
        "reason": "按减少止损/跌停无法卖出次数为第一目标，并排除候选过度过滤版本后，该版本风险退出最少；不自动并入主策略。",
    }


def _data_boundary_note(round5_dir: Path, output_dir: Path) -> dict[str, Any]:
    """保留本地缓存覆盖边界说明。"""
    summary_path = round5_dir / "universe_zero_reason_summary.csv"
    caveat = "当前 universe 仍受本地缓存覆盖限制，不能视为完整全市场回测。"
    if summary_path.exists():
        summary = pd.read_csv(summary_path, encoding="utf-8-sig")
        cache_row = summary[summary["reason"] == "cache_warmup_or_coverage"]
        if not cache_row.empty and float(cache_row.iloc[0].get("zero_date_share") or 0) >= 0.5:
            caveat = "Round5 显示 universe 为 0 日期主要来自 cache_warmup_or_coverage，当前回测不能视为完整全市场回测。"
    payload = {
        "boundary": caveat,
        "source": str(summary_path),
        "impact": "Round6 只能评价当前本地缓存样本中的 shadow 规则，不能外推为全 A 稳健结论。",
    }
    _write_json(output_dir / "data_boundary.json", payload)
    (output_dir / "data_boundary.md").write_text(
        "# Round6 Data Boundary\n\n"
        f"- {payload['boundary']}\n"
        f"- {payload['impact']}\n",
        encoding="utf-8",
    )
    return payload


def _render_report(output_dir: Path, ctx: dict[str, Any]) -> str:
    """渲染 Round6 报告。"""
    comparison = ctx["comparison"]
    candidate_summary = ctx["candidate_summary"]
    monthly_table = ctx["monthly_table"]
    recommendation = ctx["recommendation"]
    data_boundary = ctx["data_boundary"]
    thresholds = ctx["thresholds"]
    lines = [
        "# Factor Research Round6 Report",
        "",
        f"生成日期：{datetime.now().strftime('%Y-%m-%d')}",
        f"时间切分：train <= {ctx['train_end'].strftime('%Y-%m-%d')}，test >= {ctx['test_start'].strftime('%Y-%m-%d')}",
        "",
        "免责声明：本报告仅用于个人模拟交易、复盘和策略研究；不构成投资建议，不修改主 market_scanner 策略，不连接实盘接口。",
        "",
        "## 1. 数据边界",
        "",
        f"- {data_boundary['boundary']}",
        f"- {data_boundary['impact']}",
        "",
        "## 2. Round5 风险阈值",
        "",
        f"- 5 日涨幅过热阈值：{_fmt_pct(thresholds.get('change_rate_5d_q75'))}",
        f"- volatility_20d 高波动阈值：{_fmt_pct(thresholds.get('volatility_20d_q75'))}",
        "",
        "## 3. 策略对比",
        "",
        "胜率、平均单笔、中位数、最大连续亏损、止损次数和跌停无法卖出次数均基于组合接受交易；事件交易数单独列出。",
        "",
        "| 策略 | 事件交易 | 接受交易 | 累计收益 | 最大回撤 | 夏普 | 卡玛 | 胜率 | 平均单笔 | 中位数 | 最大连亏 | 止损 | 跌停无法卖出 | 正收益月份 | 最差月份 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for _, row in comparison.iterrows():
        lines.append(
            f"| {row['strategy_label']} | {int(row['event_trade_count'])} | {int(row['accepted_trade_count'])} | "
            f"{_fmt_pct(row.get('total_return'))} | {_fmt_pct(row.get('max_drawdown'))} | {row.get('sharpe')} | {row.get('calmar')} | "
            f"{_fmt_pct(row.get('win_rate'))} | {_fmt_pct(row.get('average_trade_return'))} | {_fmt_pct(row.get('median_trade_return'))} | "
            f"{int(row.get('max_consecutive_losses') or 0)} | {int(row.get('stop_loss_count') or 0)} | "
            f"{int(row.get('limit_down_blocked_exit_count') or 0)} | {_fmt_pct(row.get('positive_month_rate'))} | "
            f"{row.get('worst_month')} {_fmt_pct(row.get('worst_month_return'))} |"
        )
    lines.extend(
        [
            "",
            "## 4. 过度过滤检查",
            "",
            "过度过滤按“允许开仓且 baseline 有候选”的评估日期计算；非积极环境本来就禁止开仓，不计入版本 1/2/3 的过度过滤判定。",
            "",
            "| 策略 | 零候选日期占比 | 候选数中位数 | 候选数均值 | 是否过度过滤 |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for _, row in candidate_summary.iterrows():
        lines.append(
            f"| {row['strategy_label']} | {_fmt_pct(row.get('zero_candidate_share'))} | "
            f"{row.get('median_candidate_count')} | {row.get('mean_candidate_count')} | "
            f"{'是' if row.get('over_filtered') else '否'} |"
        )
    over_audit = ctx["over_filter_audit"]
    if over_audit["v3_over_filtered"]:
        lines.append("")
        lines.append("- 版本3 在多数日期没有候选，存在过滤过度。")
    else:
        lines.append("")
        lines.append("- 版本3 未触发“大多数日期没有候选”的过度过滤条件。")
    lines.extend(
        [
            "",
            "## 5. 月度收益表",
            "",
            "| 月份 | Baseline | 版本1 | 版本2 | 版本3 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for _, row in monthly_table.iterrows():
        lines.append(
            f"| {row['month']} | {_fmt_pct(row.get('baseline_current_stop'))} | "
            f"{_fmt_pct(row.get('v1_current_stop_hot5_risk_on'))} | "
            f"{_fmt_pct(row.get('v2_atr_risk_budget_hot5_risk_on'))} | "
            f"{_fmt_pct(row.get('v3_atr_risk_budget_hot5_vol_risk_on'))} |"
        )
    lines.extend(
        [
            "",
            "## 6. Shadow 观察结论",
            "",
            f"- 风控优先观察版本：`{recommendation['recommended_shadow_variant']}`。",
            f"- {recommendation['reason']}",
            "- 本结论仅表示下一轮 shadow research 优先级，不自动并入主策略。",
            "",
            "## 7. 输出文件",
            "",
            "- `strategy_comparison.csv`",
            "- `monthly_returns_by_version.csv`",
            "- `daily_candidate_counts.csv` / `candidate_count_summary.csv` / `over_filter_audit.json`",
            "- 每个版本的 `*_trades.csv`、`*_portfolio_trades.csv`、`*_daily_equity.csv`、`*_monthly_returns.csv`",
        ]
    )
    report = "\n".join(lines) + "\n"
    (output_dir / "factor_research_round6_report.md").write_text(report, encoding="utf-8")
    return report


def _run_round6(
    *,
    output_dir: Path,
    round5_dir: Path,
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
    """执行 Round6。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    scanner._set_active_config(scanner._load_scanner_config(strategy_path))
    thresholds = _load_round5_thresholds(round5_dir)
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
    daily_counts, candidates_by_strategy = _build_daily_candidate_counts(universe_by_date, market_timeline, thresholds)
    _write_csv(output_dir / "daily_candidate_counts.csv", daily_counts)
    candidate_summary, over_filter_audit = _candidate_count_summary(daily_counts)
    _write_csv(output_dir / "candidate_count_summary.csv", candidate_summary)
    _write_json(output_dir / "over_filter_audit.json", over_filter_audit)

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

    comparison = _comparison_rows(results)
    monthly = _monthly_table(results)
    recommendation = _recommend_shadow(comparison, candidate_summary)
    data_boundary = _data_boundary_note(round5_dir, output_dir)
    _write_csv(output_dir / "strategy_comparison.csv", comparison)
    _write_csv(output_dir / "monthly_returns_by_version.csv", monthly)
    _write_json(output_dir / "risk_control_recommendation.json", recommendation)
    _write_json(output_dir / "round5_thresholds.json", thresholds)
    return {
        "histories": histories,
        "active_dates": active_dates,
        "train_end": train_end,
        "test_start": test_start,
        "thresholds": thresholds,
        "daily_universe": daily_universe,
        "daily_counts": daily_counts,
        "candidate_summary": candidate_summary,
        "over_filter_audit": over_filter_audit,
        "results": results,
        "comparison": comparison,
        "monthly_table": monthly,
        "recommendation": recommendation,
        "data_boundary": data_boundary,
    }


def run_factor_research_round6(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    round5_dir: Path = DEFAULT_ROUND5_DIR,
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
    """运行 Round6 Alpha040 Risk-Controlled Shadow Strategy。"""
    ctx = _run_round6(
        output_dir=output_dir,
        round5_dir=round5_dir,
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
        "module": "factor_research_round6_alpha040_risk_controlled_shadow",
        "safety": "不修改主 market_scanner 策略、不写台账、不连接实盘接口。",
        "history_symbol_count": len(ctx["histories"]),
        "active_date_count": len(ctx["active_dates"]),
        "train_end": ctx["train_end"].strftime("%Y-%m-%d"),
        "test_start": ctx["test_start"].strftime("%Y-%m-%d"),
        "thresholds": ctx["thresholds"],
        "recommended_shadow_variant": ctx["recommendation"]["recommended_shadow_variant"],
        "output_dir": str(output_dir),
        "report_path": str(output_dir / "factor_research_round6_report.md"),
        "report_characters": len(report),
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行 Round6 Alpha040 Risk-Controlled Shadow Strategy")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--round5-dir", default=str(DEFAULT_ROUND5_DIR), help="Round5 输出目录")
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
    summary = run_factor_research_round6(
        output_dir=Path(args.output),
        round5_dir=Path(args.round5_dir),
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
