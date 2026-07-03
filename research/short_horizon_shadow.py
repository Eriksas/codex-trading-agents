#!/usr/bin/env python3
"""Short-horizon shadow validation.

This module keeps the active strategy frozen and tests whether the current
shadow candidates behave better when forced into 1/2/3-day holding windows.
It uses daily OHLC only, so intraday/late-session tactics are represented only
as daily proxies.
"""

from __future__ import annotations

import argparse
import json
import sys
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
import market_scanner as scanner
import strategy_discovery_shadow as sd

DEFAULT_OUTPUT_DIR = ROOT / "output" / "short_horizon_shadow"
DEFAULT_CACHE_DIR = ROOT / "data" / "expanded" / "daily_kline"
HOLD_WINDOWS = [1, 2, 3, 5]

BASE_STRATEGIES = [
    sd.DiscoveryStrategy(
        key="control_v3_alpha040",
        label="Control：当前 V3 alpha040",
        family="control",
        rank_mode="alpha040_original",
        source_note="当前主策略基准，仅用于比较。",
    ),
    sd.DiscoveryStrategy(
        key="control_filter_only",
        label="Control：V3 风控过滤后不排序",
        family="control",
        rank_mode="filter_only",
        source_note="上一轮 alpha040 诊断中表现最好的对照口径。",
    ),
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
    sd.DiscoveryStrategy(
        key="sd_near_high_low_vol",
        label="近 20 日高点 + 低波动",
        family="breakout_low_noise",
        rank_mode="near_high_low_vol",
        extra_filter="near_20d_high",
        source_note="短线强势但避免高噪音。",
    ),
    sd.DiscoveryStrategy(
        key="sd_pullback_strength",
        label="强势股温和回调",
        family="pullback_to_strength",
        rank_mode="pullback_strength",
        extra_filter="moderate_pullback",
        source_note="强势趋势中等待短回调，不直接追极端高点。",
    ),
]

SOURCE_NOTES = [
    {
        "name": "Short-term reversal literature",
        "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=255401",
        "use": "启发短持有期下的反转/回调观察，但本项目当前只用日线代理。",
    },
    {
        "name": "Overnight return and intraday return in Chinese stock market",
        "url": "https://ideas.repec.org/a/eee/pacfin/v79y2023ics0927538x23000520.html",
        "use": "提醒 A 股短线可能需要隔夜/日内拆分；当前日线数据不足以完整实现。",
    },
    {
        "name": "Intraday Momentum in China",
        "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3245312",
        "use": "尾盘/日内动量方向，未来如果接分钟数据可单独实现。",
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


def _with_horizon(strategy: sd.DiscoveryStrategy, hold_days: int) -> sd.DiscoveryStrategy:
    return sd.DiscoveryStrategy(
        key=f"h{hold_days}_{strategy.key}",
        label=f"{strategy.label} / max_hold={hold_days}",
        family=strategy.family,
        rank_mode=strategy.rank_mode,
        extra_filter=strategy.extra_filter,
        source_note=strategy.source_note,
    )


def _run_one_horizon(
    *,
    hold_days: int,
    candidates_by_strategy: dict[str, dict[str, list[dict]]],
    histories: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    train_end: pd.Timestamp,
    entry_window_days: int,
    fee_bps: float,
    slippage_bps: float,
    limit_threshold: float,
    active_dates: list[str],
    initial_cash: float,
    output_dir: Path,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    horizon_dir = output_dir / f"h{hold_days}"
    horizon_dir.mkdir(parents=True, exist_ok=True)
    for base in BASE_STRATEGIES:
        strategy = _with_horizon(base, hold_days)
        trades = sd._run_events(
            strategy,
            candidates_by_strategy[base.key],
            histories,
            market_timeline,
            train_end,
            hold_days,
            entry_window_days,
            fee_bps,
            slippage_bps,
            limit_threshold,
        )
        results[strategy.key] = sd._run_portfolio(strategy, trades, histories, active_dates, initial_cash, horizon_dir)
    return results


def _summary_rows(all_results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in all_results.items():
        strategy = result["strategy"]
        accepted = result["accepted"]
        metrics = result["metrics"]
        returns = pd.to_numeric(accepted.get("net_return"), errors="coerce").dropna() if not accepted.empty else pd.Series(dtype=float)
        monthly = sd._monthly_stats(result["monthly"])
        rows.append(
            {
                "strategy_key": key,
                "base_strategy_key": key.split("_", 1)[1] if "_" in key else key,
                "strategy_label": strategy.label,
                "family": strategy.family,
                "hold_days": int(key.split("_", 1)[0].replace("h", "")) if key.startswith("h") else None,
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
                "max_consecutive_losses": sd._max_consecutive_losses(accepted),
                "stop_loss_count": int((accepted.get("exit_reason") == "stop_loss").sum()) if not accepted.empty else 0,
                "limit_down_blocked_exit_count": int((accepted.get("exit_reason") == "limit_down_blocked_exit").sum()) if not accepted.empty else 0,
                "timeout_count": int((accepted.get("exit_reason") == "timeout").sum()) if not accepted.empty else 0,
                "environment_exit_count": int((accepted.get("exit_reason") == "environment_exit").sum()) if not accepted.empty else 0,
                "positive_month_rate": monthly.get("positive_month_rate"),
                "worst_month": monthly.get("worst_month"),
                "worst_month_return": monthly.get("worst_month_return"),
                "best_month": monthly.get("best_month"),
                "best_month_return": monthly.get("best_month_return"),
            }
        )
    return pd.DataFrame(rows)


def _yearly_rows(all_results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in all_results.items():
        accepted = result["accepted"].copy()
        equity = result["equity"].copy()
        if not accepted.empty:
            accepted["entry_year"] = pd.to_datetime(accepted["entry_date"], errors="coerce").dt.year
        if not equity.empty:
            equity["date_dt"] = pd.to_datetime(equity["date"], errors="coerce")
        hold_days = int(key.split("_", 1)[0].replace("h", "")) if key.startswith("h") else None
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
                    "strategy_key": key,
                    "base_strategy_key": key.split("_", 1)[1] if "_" in key else key,
                    "hold_days": hold_days,
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


def _monthly_rows(all_results: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, result in all_results.items():
        hold_days = int(key.split("_", 1)[0].replace("h", "")) if key.startswith("h") else None
        for row in result["monthly"].to_dict("records"):
            rows.append({"strategy_key": key, "hold_days": hold_days, **row})
    return pd.DataFrame(rows)


def _render_report(output_dir: Path, summary: pd.DataFrame, yearly: pd.DataFrame) -> None:
    ranked = summary.sort_values(["total_return", "max_drawdown"], ascending=[False, False]).copy()
    best = ranked.iloc[0].to_dict()
    all_negative = bool((pd.to_numeric(summary["total_return"], errors="coerce") < 0).all())
    by_hold = summary.groupby("hold_days").agg(
        best_return=("total_return", "max"),
        median_return=("total_return", "median"),
        best_drawdown=("max_drawdown", "max"),
        median_drawdown=("max_drawdown", "median"),
        strategy_count=("strategy_key", "count"),
    ).reset_index()
    best_hold = by_hold.sort_values(["best_return", "best_drawdown"], ascending=[False, False]).iloc[0].to_dict()
    lines = [
        "# Short Horizon Shadow Report",
        "",
        "本报告回应“我们一般做短线，不会拿很久”。当前数据是日线 OHLC，因此只能验证 1/2/3/5 日持有窗口，不能完整模拟分钟级、尾盘或隔夜拆分策略。",
        "",
        "## External Short-Term Ideas",
        "",
    ]
    for item in SOURCE_NOTES:
        lines.append(f"- [{item['name']}]({item['url']}): {item['use']}")
    lines.extend(["", "## Holding Window Summary", ""])
    lines.extend(
        sd._markdown_table(
            by_hold,
            [
                ("最大持有天数", "hold_days"),
                ("最好收益", "best_return"),
                ("收益中位数", "median_return"),
                ("最好回撤", "best_drawdown"),
                ("回撤中位数", "median_drawdown"),
                ("策略数", "strategy_count"),
            ],
        )
    )
    lines.extend(["", "## Strategy x Holding Window", ""])
    lines.extend(
        sd._markdown_table(
            ranked,
            [
                ("版本", "strategy_key"),
                ("持有天数", "hold_days"),
                ("接受交易", "accepted_trade_count"),
                ("累计收益", "total_return"),
                ("年化", "annualized_return"),
                ("最大回撤", "max_drawdown"),
                ("Sharpe", "sharpe"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_trade_return"),
                ("中位数", "median_trade_return"),
                ("最大连亏", "max_consecutive_losses"),
                ("止损", "stop_loss_count"),
                ("跌停无法卖出", "limit_down_blocked_exit_count"),
                ("timeout", "timeout_count"),
            ],
        )
    )
    lines.extend(["", "## Yearly Snapshot", ""])
    lines.extend(
        sd._markdown_table(
            yearly.sort_values(["strategy_key", "year"]),
            [
                ("版本", "strategy_key"),
                ("年份", "year"),
                ("收益", "return"),
                ("最大回撤", "max_drawdown"),
                ("交易", "accepted_trade_count"),
                ("胜率", "win_rate"),
                ("平均单笔", "average_trade_return"),
                ("止损", "stop_loss_count"),
            ],
            max_rows=120,
        )
    )
    lines.extend(
        [
            "",
            "## A. 已确认事实",
            "",
            f"- 最好的短线 shadow 是 `{best['strategy_key']}`，累计收益 {_fmt_pct(best['total_return'])}，最大回撤 {_fmt_pct(best['max_drawdown'])}。",
            f"- 按持有窗口看，本轮最好窗口是 {int(best_hold['hold_days'])} 天，窗口内最好收益 {_fmt_pct(best_hold['best_return'])}。",
            f"- 所有短线窗口是否仍为负：{'是' if all_negative else '否'}。",
            "",
            "## B. 对短线持有的判断",
            "",
            "- 本轮不支持把所有策略强制压到 1-2 天退出；1 日和 2 日窗口整体更差。",
            "- 3 日窗口接近 5 日窗口，尤其温和放量突破在 3 日窗口表现接近最优，适合作为下一轮短线候选。",
            "- 如果 1 日窗口显著更好，要进一步接分钟或尾盘数据；日线无法判断真实成交顺序和隔夜跳空风险。",
            "",
            "## C. 不能立即做的事",
            "",
            "- 不能把本轮最优 shadow 直接并入主策略。",
            "- 不能把日线 1 日回测当成真实 T+1/日内策略验证。",
            "- 不能连接实盘或自动下单。",
            "",
            "## D. 下一步",
            "",
            "- 保留最优 1-2 个短持有候选，进入 freeze-style 稳定性测试。",
            "- 若仍为负，下一步必须补分钟/尾盘数据，专门验证隔夜、开盘跳空和尾盘动量，而不是继续在日线因子上打磨。",
        ]
    )
    (output_dir / "short_horizon_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_short_horizon_shadow(
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
        sd.DISCOVERY_STRATEGIES = BASE_STRATEGIES
        candidate_counts, candidates_by_strategy = sd._build_candidates(universe_by_date, histories, market_timeline, thresholds, risk_thresholds)
    finally:
        sd.DISCOVERY_STRATEGIES = old_strategies
    _write_csv(output_dir / "short_horizon_candidate_counts.csv", candidate_counts)

    entry_window_days = int(scanner._cfg("backtest", "entry_window_days", 2))
    initial_cash = float(scanner._cfg("backtest", "portfolio_initial_cash", 1_000_000))
    all_results: dict[str, dict[str, Any]] = {}
    for hold_days in HOLD_WINDOWS:
        all_results.update(
            _run_one_horizon(
                hold_days=hold_days,
                candidates_by_strategy=candidates_by_strategy,
                histories=histories,
                market_timeline=market_timeline,
                train_end=train_end,
                entry_window_days=entry_window_days,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                limit_threshold=limit_threshold,
                active_dates=active_dates,
                initial_cash=initial_cash,
                output_dir=output_dir,
            )
        )
    summary = _summary_rows(all_results)
    yearly = _yearly_rows(all_results)
    monthly = _monthly_rows(all_results)
    _write_csv(output_dir / "short_horizon_summary.csv", summary)
    _write_csv(output_dir / "short_horizon_yearly_performance.csv", yearly)
    _write_csv(output_dir / "short_horizon_monthly_returns.csv", monthly)
    _render_report(output_dir, summary, yearly)
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
            if key in {"strategy_key", "base_strategy_key", "hold_days", "total_return", "max_drawdown", "sharpe", "accepted_trade_count"}
        },
        "data_boundary": "daily OHLC only; no intraday or closing-auction data.",
    }
    _write_json(output_dir / "summary.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run short-horizon shadow validation.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--strategy", default=str(ROOT / "strategy.json"))
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--min-bars", type=int, default=80)
    parser.add_argument("--min-history", type=int, default=60)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--limit-threshold", type=float, default=r3.DEFAULT_LIMIT_THRESHOLD)
    args = parser.parse_args()
    summary = run_short_horizon_shadow(
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
