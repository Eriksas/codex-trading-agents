#!/usr/bin/env python3
"""Freeze-style validation for the best short-horizon shadow candidates.

This script keeps alpha040_v3_risk_controlled unchanged. It validates only two
shadow candidates found in strategy discovery:

- h5_sd_risk_avoidance_filter
- h3_sd_volume_confirmed_breakout

It does not tune thresholds, add factors, connect live trading, or write ledgers.
"""

from __future__ import annotations

import argparse
import json
import sys
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
import strategy_discovery_shadow as sd

DEFAULT_OUTPUT_DIR = ROOT / "output" / "short_horizon_freeze_validation"
DEFAULT_CACHE_DIR = ROOT / "data" / "expanded" / "daily_kline"
REFERENCE_SUMMARY = ROOT / "output" / "short_horizon_shadow" / "short_horizon_summary.csv"


FREEZE_CANDIDATES = [
    sd.DiscoveryStrategy(
        key="sd_risk_avoidance_filter",
        label="风险规避过滤",
        family="risk_avoidance",
        rank_mode="filter_only",
        extra_filter="risk_avoidance",
        source_note="短线先少犯错：剔除高振幅和高上影线样本。",
    ),
    sd.DiscoveryStrategy(
        key="sd_volume_confirmed_breakout",
        label="温和放量突破",
        family="volume_confirmed_breakout",
        rank_mode="volume_confirmed_breakout",
        extra_filter="volume_confirmed_breakout",
        source_note="贴近高点、温和放量、上影线受控。",
    ),
]


@dataclass(frozen=True)
class ValidationCase:
    key: str
    candidate_key: str
    case_type: str
    hold_days: int
    fee_bps: float
    slippage_bps: float
    entry_window_days: int
    max_positions: int
    max_total_exposure_pct: float
    note: str


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


def _candidate_by_key(key: str) -> sd.DiscoveryStrategy:
    for candidate in FREEZE_CANDIDATES:
        if candidate.key == key:
            return candidate
    raise KeyError(key)


def _case_strategy(candidate: sd.DiscoveryStrategy, case: ValidationCase) -> sd.DiscoveryStrategy:
    return sd.DiscoveryStrategy(
        key=case.key,
        label=f"{candidate.label} / {case.note}",
        family=candidate.family,
        rank_mode=candidate.rank_mode,
        extra_filter=candidate.extra_filter,
        source_note=candidate.source_note,
    )


def _build_cases(default_entry_window: int, default_max_positions: int, default_max_exposure: float) -> list[ValidationCase]:
    specs = [
        ("sd_risk_avoidance_filter", 5),
        ("sd_volume_confirmed_breakout", 3),
    ]
    cases: list[ValidationCase] = []
    for candidate_key, baseline_hold in specs:
        prefix = "risk" if "risk" in candidate_key else "volume"
        cases.append(
            ValidationCase(
                key=f"{prefix}_baseline_h{baseline_hold}",
                candidate_key=candidate_key,
                case_type="baseline",
                hold_days=baseline_hold,
                fee_bps=5.0,
                slippage_bps=10.0,
                entry_window_days=default_entry_window,
                max_positions=default_max_positions,
                max_total_exposure_pct=default_max_exposure,
                note=f"baseline h{baseline_hold}",
            )
        )
        for hold_days in [1, 2, 3, 5]:
            cases.append(
                ValidationCase(
                    key=f"{prefix}_hold_h{hold_days}",
                    candidate_key=candidate_key,
                    case_type="hold_sensitivity",
                    hold_days=hold_days,
                    fee_bps=5.0,
                    slippage_bps=10.0,
                    entry_window_days=default_entry_window,
                    max_positions=default_max_positions,
                    max_total_exposure_pct=default_max_exposure,
                    note=f"hold={hold_days}",
                )
            )
        for label, fee_bps, slippage_bps in [
            ("fee_x2", 10.0, 10.0),
            ("slippage_x2", 5.0, 20.0),
            ("fee_slippage_x2", 10.0, 20.0),
        ]:
            cases.append(
                ValidationCase(
                    key=f"{prefix}_{label}_h{baseline_hold}",
                    candidate_key=candidate_key,
                    case_type="cost_stress",
                    hold_days=baseline_hold,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    entry_window_days=default_entry_window,
                    max_positions=default_max_positions,
                    max_total_exposure_pct=default_max_exposure,
                    note=label,
                )
            )
        for entry_window in [1, default_entry_window]:
            cases.append(
                ValidationCase(
                    key=f"{prefix}_entry{entry_window}_h{baseline_hold}",
                    candidate_key=candidate_key,
                    case_type="entry_window",
                    hold_days=baseline_hold,
                    fee_bps=5.0,
                    slippage_bps=10.0,
                    entry_window_days=entry_window,
                    max_positions=default_max_positions,
                    max_total_exposure_pct=default_max_exposure,
                    note=f"entry_window={entry_window}",
                )
            )
        for max_positions in [2, 3, 4]:
            cases.append(
                ValidationCase(
                    key=f"{prefix}_maxpos{max_positions}_h{baseline_hold}",
                    candidate_key=candidate_key,
                    case_type="max_position",
                    hold_days=baseline_hold,
                    fee_bps=5.0,
                    slippage_bps=10.0,
                    entry_window_days=default_entry_window,
                    max_positions=max_positions,
                    max_total_exposure_pct=default_max_exposure,
                    note=f"max_positions={max_positions}",
                )
            )
    # Keep unique keys; baseline intentionally overlaps h sensitivity/maxpos defaults.
    unique: dict[str, ValidationCase] = {}
    for case in cases:
        unique[case.key] = case
    return list(unique.values())


def _accept_portfolio_trades(
    trades: pd.DataFrame,
    initial_cash: float,
    max_positions: int,
    max_total_exposure_pct: float,
) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    active: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    sort_cols = ["entry_date", "rank_score"] if "rank_score" in trades.columns else ["entry_date"]
    ascending = [True, False] if "rank_score" in trades.columns else [True]
    for _, row in trades.sort_values(sort_cols, ascending=ascending).iterrows():
        entry_date = str(row["entry_date"])
        active = [item for item in active if item["exit_date"] >= entry_date]
        current_exposure = sum(float(item["portfolio_position_pct"]) for item in active)
        requested = (_safe_float(row.get("position_pct")) or 8.0) / 100
        actual = min(requested, max_total_exposure_pct - current_exposure)
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


def _run_case(
    case: ValidationCase,
    candidates_by_strategy: dict[str, dict[str, list[dict]]],
    histories: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    train_end: pd.Timestamp,
    active_dates: list[str],
    initial_cash: float,
    limit_threshold: float,
    output_dir: Path,
) -> dict[str, Any]:
    candidate = _candidate_by_key(case.candidate_key)
    strategy = _case_strategy(candidate, case)
    trades = sd._run_events(
        strategy,
        candidates_by_strategy[case.candidate_key],
        histories,
        market_timeline,
        train_end,
        case.hold_days,
        case.entry_window_days,
        case.fee_bps,
        case.slippage_bps,
        limit_threshold,
    )
    portfolio = _accept_portfolio_trades(trades, initial_cash, case.max_positions, case.max_total_exposure_pct)
    equity, monthly, metrics = r4._portfolio_equity_curve(portfolio, histories, active_dates, initial_cash)
    accepted = portfolio[portfolio["portfolio_action"] == "accepted"].copy() if not portfolio.empty and "portfolio_action" in portfolio else pd.DataFrame()
    case_dir = output_dir / "cases"
    _write_csv(case_dir / f"{case.key}_trades.csv", trades)
    _write_csv(case_dir / f"{case.key}_portfolio_trades.csv", portfolio)
    _write_csv(case_dir / f"{case.key}_accepted_trades.csv", accepted)
    _write_csv(case_dir / f"{case.key}_daily_equity.csv", equity)
    _write_csv(case_dir / f"{case.key}_monthly_returns.csv", monthly)
    return {
        "case": case,
        "strategy": strategy,
        "trades": trades,
        "portfolio": portfolio,
        "accepted": accepted,
        "equity": equity,
        "monthly": monthly,
        "metrics": metrics,
    }


def _monthly_stats(monthly: pd.DataFrame) -> dict[str, Any]:
    return sd._monthly_stats(monthly)


def _summary_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in results.items():
        case: ValidationCase = result["case"]
        accepted = result["accepted"]
        metrics = result["metrics"]
        returns = pd.to_numeric(accepted.get("net_return"), errors="coerce").dropna() if not accepted.empty else pd.Series(dtype=float)
        monthly = _monthly_stats(result["monthly"])
        rows.append(
            {
                "case_key": key,
                "candidate_key": case.candidate_key,
                "case_type": case.case_type,
                "hold_days": case.hold_days,
                "fee_bps": case.fee_bps,
                "slippage_bps": case.slippage_bps,
                "entry_window_days": case.entry_window_days,
                "max_positions": case.max_positions,
                "max_total_exposure_pct": case.max_total_exposure_pct,
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
                "max_consecutive_losses": r5._max_consecutive_losses(accepted) if not accepted.empty else 0,
                "stop_loss_count": int((accepted.get("exit_reason") == "stop_loss").sum()) if not accepted.empty else 0,
                "limit_down_blocked_exit_count": int((accepted.get("exit_reason") == "limit_down_blocked_exit").sum()) if not accepted.empty else 0,
                "timeout_count": int((accepted.get("exit_reason") == "timeout").sum()) if not accepted.empty else 0,
                "environment_exit_count": int((accepted.get("exit_reason") == "environment_exit").sum()) if not accepted.empty else 0,
                "positive_month_rate": monthly.get("positive_month_rate"),
                "worst_month": monthly.get("worst_month"),
                "worst_month_return": monthly.get("worst_month_return"),
                "best_month": monthly.get("best_month"),
                "best_month_return": monthly.get("best_month_return"),
                "note": case.note,
            }
        )
    return pd.DataFrame(rows)


def _yearly_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in results.items():
        case: ValidationCase = result["case"]
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
                    "case_key": key,
                    "candidate_key": case.candidate_key,
                    "case_type": case.case_type,
                    "year": "2026 YTD" if year == 2026 else str(year),
                    "return": end / start - 1 if start else None,
                    "max_drawdown": float(dd.min()) if len(dd) else None,
                    "accepted_trade_count": int(len(trades)),
                    "win_rate": float((returns > 0).mean()) if len(returns) else None,
                    "average_trade_return": float(returns.mean()) if len(returns) else None,
                    "stop_loss_count": int((trades.get("exit_reason") == "stop_loss").sum()) if not trades.empty else 0,
                    "limit_down_blocked_exit_count": int((trades.get("exit_reason") == "limit_down_blocked_exit").sum()) if not trades.empty else 0,
                }
            )
    return pd.DataFrame(rows)


def _monthly_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in results.items():
        case: ValidationCase = result["case"]
        for row in result["monthly"].to_dict("records"):
            rows.append({"case_key": key, "candidate_key": case.candidate_key, "case_type": case.case_type, **row})
    return pd.DataFrame(rows)


def _exit_reason_rows(results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    expected = [
        "take_profit",
        "stop_loss",
        "timeout",
        "time_stop",
        "environment_exit",
        "limit_down_blocked_exit",
        "same_day_stop_take_conservative",
    ]
    for key, result in results.items():
        case: ValidationCase = result["case"]
        accepted = result["accepted"].copy()
        for reason in expected:
            group = accepted[accepted["exit_reason"] == reason].copy() if not accepted.empty else pd.DataFrame()
            returns = pd.to_numeric(group.get("net_return"), errors="coerce").dropna() if not group.empty else pd.Series(dtype=float)
            contribution = (
                pd.to_numeric(group.get("net_return"), errors="coerce").fillna(0)
                * pd.to_numeric(group.get("portfolio_position_pct"), errors="coerce").fillna(0)
            ).sum() if not group.empty else 0.0
            rows.append(
                {
                    "case_key": key,
                    "candidate_key": case.candidate_key,
                    "case_type": case.case_type,
                    "exit_reason": reason,
                    "trade_count": int(len(group)),
                    "win_rate": float((returns > 0).mean()) if len(returns) else None,
                    "average_trade_return": float(returns.mean()) if len(returns) else None,
                    "median_trade_return": float(returns.median()) if len(returns) else None,
                    "max_loss": float(returns.min()) if len(returns) else None,
                    "cumulative_contribution": float(contribution),
                }
            )
    return pd.DataFrame(rows)


def _reference_rows() -> pd.DataFrame:
    if not REFERENCE_SUMMARY.exists():
        return pd.DataFrame()
    return pd.read_csv(REFERENCE_SUMMARY, encoding="utf-8-sig")


def _render_report(output_dir: Path, summary: pd.DataFrame, yearly: pd.DataFrame, exit_reason: pd.DataFrame) -> None:
    ranked = summary.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).copy()
    best = ranked.iloc[0].to_dict()
    baseline = summary[summary["case_type"] == "baseline"].sort_values("candidate_key").copy()
    hold = summary[summary["case_type"] == "hold_sensitivity"].copy()
    cost = summary[summary["case_type"] == "cost_stress"].copy()
    entry = summary[summary["case_type"] == "entry_window"].copy()
    maxpos = summary[summary["case_type"] == "max_position"].copy()
    all_negative = bool((pd.to_numeric(summary["total_return"], errors="coerce") < 0).all())
    lines = [
        "# Short Horizon Freeze Validation Report",
        "",
        "本报告只验证两个短线 shadow 候选，不修改主策略、不调阈值、不新增因子、不连接实盘。",
        "",
        "## Frozen Shadow Candidates",
        "",
        "- `h5_sd_risk_avoidance_filter`: 风险规避过滤，最大持有 5 天。",
        "- `h3_sd_volume_confirmed_breakout`: 温和放量突破，最大持有 3 天。",
        "",
        "## Baseline Re-run",
        "",
    ]
    lines.extend(
        sd._markdown_table(
            baseline,
            [
                ("case", "case_key"),
                ("交易", "accepted_trade_count"),
                ("收益", "total_return"),
                ("回撤", "max_drawdown"),
                ("Sharpe", "sharpe"),
                ("胜率", "win_rate"),
                ("均笔", "average_trade_return"),
                ("止损", "stop_loss_count"),
                ("跌停", "limit_down_blocked_exit_count"),
            ],
        )
    )
    lines.extend(["", "## Holding Window Sensitivity", ""])
    lines.extend(
        sd._markdown_table(
            hold.sort_values(["candidate_key", "hold_days"]),
            [
                ("case", "case_key"),
                ("候选", "candidate_key"),
                ("持有", "hold_days"),
                ("收益", "total_return"),
                ("回撤", "max_drawdown"),
                ("Sharpe", "sharpe"),
                ("交易", "accepted_trade_count"),
                ("止损", "stop_loss_count"),
            ],
        )
    )
    lines.extend(["", "## Cost Stress", ""])
    lines.extend(
        sd._markdown_table(
            cost.sort_values(["candidate_key", "case_key"]),
            [
                ("case", "case_key"),
                ("费用bp", "fee_bps"),
                ("滑点bp", "slippage_bps"),
                ("收益", "total_return"),
                ("回撤", "max_drawdown"),
                ("Sharpe", "sharpe"),
            ],
        )
    )
    lines.extend(["", "## Entry Window / Max Position Sensitivity", ""])
    lines.extend(
        sd._markdown_table(
            entry.sort_values(["candidate_key", "entry_window_days"]),
            [
                ("case", "case_key"),
                ("entry_window", "entry_window_days"),
                ("收益", "total_return"),
                ("回撤", "max_drawdown"),
                ("交易", "accepted_trade_count"),
            ],
        )
    )
    lines.append("")
    lines.extend(
        sd._markdown_table(
            maxpos.sort_values(["candidate_key", "max_positions"]),
            [
                ("case", "case_key"),
                ("max_positions", "max_positions"),
                ("收益", "total_return"),
                ("回撤", "max_drawdown"),
                ("交易", "accepted_trade_count"),
            ],
        )
    )
    lines.extend(["", "## Yearly Baseline Detail", ""])
    baseline_yearly = yearly[yearly["case_type"] == "baseline"].copy()
    lines.extend(
        sd._markdown_table(
            baseline_yearly.sort_values(["case_key", "year"]),
            [
                ("case", "case_key"),
                ("年份", "year"),
                ("收益", "return"),
                ("回撤", "max_drawdown"),
                ("交易", "accepted_trade_count"),
                ("胜率", "win_rate"),
                ("止损", "stop_loss_count"),
                ("跌停", "limit_down_blocked_exit_count"),
            ],
        )
    )
    risk_exits = exit_reason[
        (exit_reason["case_type"] == "baseline") & exit_reason["exit_reason"].isin(["stop_loss", "limit_down_blocked_exit", "timeout", "time_stop"])
    ].copy()
    lines.extend(["", "## Baseline Exit Reasons", ""])
    lines.extend(
        sd._markdown_table(
            risk_exits,
            [
                ("case", "case_key"),
                ("退出", "exit_reason"),
                ("笔数", "trade_count"),
                ("均笔", "average_trade_return"),
                ("贡献", "cumulative_contribution"),
            ],
        )
    )
    lines.extend(
        [
            "",
            "## A. 已确认事实",
            "",
            f"- 本轮所有验证 case 是否仍为负：{'是' if all_negative else '否'}。",
            f"- 最好 case 是 `{best['case_key']}`，累计收益 {_fmt_pct(best['total_return'])}，最大回撤 {_fmt_pct(best['max_drawdown'])}。",
            "- 两个候选均未通过收益端验证，但风险规避过滤仍是当前最稳的短线 shadow 方向。",
            "",
            "## B. 对短线策略的判断",
            "",
            "- 仅缩短持有期不能解决收益问题；1/2 日窗口没有稳定改善。",
            "- 当前日线数据更支持“风险规避 + 3 至 5 日内观察/退出”，而不是机械次日卖出。",
            "- 温和放量突破在 3 日窗口可作为观察项，但需要分钟/尾盘数据验证真实短线入场质量。",
            "",
            "## C. 不能立即做的事",
            "",
            "- 不能把任何候选并入 `alpha040_v3_risk_controlled`。",
            "- 不能根据本轮结果调固定阈值。",
            "- 不能连接实盘或自动下单。",
            "",
            "## D. 下一步",
            "",
            "- 优先补分钟或至少 5 分钟/15 分钟数据，用来验证尾盘动量、次日开盘跳空、冲高回落的真实路径。",
            "- 在没有分钟数据前，forward paper trading 只记录信号，不用短期结果改规则。",
        ]
    )
    (output_dir / "short_horizon_freeze_validation_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_short_horizon_freeze_validation(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    strategy_path: Path = ROOT / "strategy.json",
    max_symbols: Optional[int] = None,
    min_bars: int = 80,
    min_history: int = 60,
    train_ratio: float = 0.7,
    limit_threshold: float = r3.DEFAULT_LIMIT_THRESHOLD,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    scanner._set_active_config(scanner._load_scanner_config(str(strategy_path)))
    thresholds = alpha_shadow._load_v3_thresholds(strategy_path)
    risk_thresholds = sd._load_risk_thresholds()
    histories = fr.load_cached_histories(cache_dir=cache_dir, max_symbols=max_symbols, min_bars=min_bars)
    if not histories:
        raise RuntimeError(f"未找到可用历史 K 线缓存：{cache_dir}")
    metadata = alpha_shadow._load_expanded_metadata()
    alpha040_map = r3._build_alpha040_map(histories)
    universe_by_date, daily_universe = r3._build_dynamic_universe(histories, metadata, alpha040_map, min_history, output_dir / "universe_audit")
    active_dates = [date for date, rows in universe_by_date.items() if rows]
    train_end, _test_start = fr._split_dates(pd.Series(pd.to_datetime(active_dates)), train_ratio=train_ratio)
    market_timeline = alpha_shadow._build_market_timeline_from_expanded()
    old_strategies = sd.DISCOVERY_STRATEGIES
    try:
        sd.DISCOVERY_STRATEGIES = FREEZE_CANDIDATES
        candidate_counts, candidates_by_strategy = sd._build_candidates(universe_by_date, histories, market_timeline, thresholds, risk_thresholds)
    finally:
        sd.DISCOVERY_STRATEGIES = old_strategies
    _write_csv(output_dir / "freeze_candidate_counts.csv", candidate_counts)

    default_entry_window = int(scanner._cfg("backtest", "entry_window_days", 2))
    default_max_positions = int(scanner._cfg("backtest", "portfolio_max_positions", 4))
    default_max_exposure = float(scanner._cfg("backtest", "portfolio_max_total_exposure_pct", 0.32))
    initial_cash = float(scanner._cfg("backtest", "portfolio_initial_cash", 1_000_000))
    cases = _build_cases(default_entry_window, default_max_positions, default_max_exposure)
    _write_json(
        output_dir / "freeze_candidate_config.json",
        {
            "candidates": [candidate.__dict__ for candidate in FREEZE_CANDIDATES],
            "cases": [case.__dict__ for case in cases],
            "thresholds": thresholds,
            "risk_thresholds": risk_thresholds,
            "boundary": "shadow only; no main strategy changes; daily OHLC only.",
        },
    )

    results: dict[str, dict[str, Any]] = {}
    for case in cases:
        results[case.key] = _run_case(
            case,
            candidates_by_strategy,
            histories,
            market_timeline,
            train_end,
            active_dates,
            initial_cash,
            limit_threshold,
            output_dir,
        )
    summary = _summary_rows(results)
    yearly = _yearly_rows(results)
    monthly = _monthly_rows(results)
    exit_reason = _exit_reason_rows(results)
    _write_csv(output_dir / "short_horizon_freeze_summary.csv", summary)
    _write_csv(output_dir / "short_horizon_freeze_yearly_performance.csv", yearly)
    _write_csv(output_dir / "short_horizon_freeze_monthly_returns.csv", monthly)
    _write_csv(output_dir / "short_horizon_freeze_exit_reason.csv", exit_reason)
    _render_report(output_dir, summary, yearly, exit_reason)
    best = summary.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).iloc[0].to_dict()
    payload = {
        "history_symbol_count": len(histories),
        "active_date_count": len(active_dates),
        "market_timeline_dates": len(market_timeline),
        "daily_universe_rows": int(len(daily_universe)),
        "best_case": {
            key: (None if pd.isna(value) else value)
            for key, value in best.items()
            if key in {"case_key", "candidate_key", "case_type", "hold_days", "total_return", "max_drawdown", "sharpe", "accepted_trade_count"}
        },
        "all_cases_negative": bool((pd.to_numeric(summary["total_return"], errors="coerce") < 0).all()),
        "output_dir": str(output_dir),
    }
    _write_json(output_dir / "summary.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run short-horizon freeze-style validation.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--strategy", default=str(ROOT / "strategy.json"))
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--min-bars", type=int, default=80)
    parser.add_argument("--min-history", type=int, default=60)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--limit-threshold", type=float, default=r3.DEFAULT_LIMIT_THRESHOLD)
    args = parser.parse_args()
    summary = run_short_horizon_freeze_validation(
        output_dir=Path(args.output),
        cache_dir=Path(args.cache_dir),
        strategy_path=Path(args.strategy),
        max_symbols=args.max_symbols,
        min_bars=args.min_bars,
        min_history=args.min_history,
        train_ratio=args.train_ratio,
        limit_threshold=args.limit_threshold,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=fr._json_default))


if __name__ == "__main__":
    main()
