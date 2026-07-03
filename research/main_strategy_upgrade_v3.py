"""
main_strategy_upgrade_v3.py - 主策略升级审计入口

本脚本只做升级后的复跑验证、对比报告和审计留痕。
策略配置文件由人工/代码变更完成；若 V3 复跑与 Freeze V3 原始结果不一致，
脚本会输出 mismatch_report.md，供人工回滚。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
for _dep_dir in (ROOT_DIR / "src", ROOT_DIR / "archive"):
    if str(_dep_dir) not in sys.path:
        sys.path.insert(0, str(_dep_dir))

import factor_research as fr
import factor_research_freeze_v3 as freeze
import factor_research_round3 as r3
import factor_research_round4 as r4
import factor_research_round5 as r5
import factor_research_round6 as r6
import market_scanner as scanner
import market_scanner_legacy_v1 as legacy_scanner

DEFAULT_OUTPUT_DIR = Path("output/main_strategy_upgrade_v3")
DEFAULT_ROUND6_DIR = Path("output/factor_research_round6")
V3_FREEZE_KEY = "v3_atr_risk_budget_hot5_vol_risk_on"
NEW_MAIN_KEY = "alpha040_v3_risk_controlled"
LEGACY_KEY = "legacy_momentum_v1"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=fr._json_default)


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    """写 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _read_json(path: Path) -> dict[str, Any]:
    """读取 JSON。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_float(value: Any) -> Optional[float]:
    """安全转换 float。"""
    return fr._safe_float(value)


def _fmt_pct(value: Any, digits: int = 2) -> str:
    """百分比格式化。"""
    if value is None:
        return "-"
    try:
        if isinstance(value, float) and math.isnan(value):
            return "-"
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _load_csv(path: Path) -> pd.DataFrame:
    """读取 CSV，不存在时返回空表。"""
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _active_dates_from_freeze_universe(freeze_dir: Path, histories: dict[str, list[dict]]) -> list[str]:
    """优先使用 Freeze 复跑的动态 universe 日期；缺失则回退到缓存交易日。"""
    universe = _load_csv(freeze_dir / "daily_universe.csv")
    if not universe.empty and {"date", "stock_count"}.issubset(universe.columns):
        active = universe[pd.to_numeric(universe["stock_count"], errors="coerce").fillna(0) > 0]
        dates = sorted(str(x) for x in active["date"].dropna().tolist())
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


def _legacy_stock_rows(histories: dict[str, list[dict]], metadata: dict[str, dict]) -> dict[str, dict]:
    """构造 legacy scanner 回测所需的基础股票行。"""
    rows: dict[str, dict] = {}
    for symbol in histories:
        meta = metadata.get(symbol) or {}
        rows[symbol] = {
            "name": meta.get("name") or symbol,
            "symkey": symbol,
            "industry_sector": {"name": meta.get("sector") or "未分类"},
            "data_source": "legacy_upgrade_validation",
        }
    return rows


def _accepted_trades(portfolio: pd.DataFrame) -> pd.DataFrame:
    """取组合接受交易。"""
    if portfolio.empty or "portfolio_action" not in portfolio.columns:
        return pd.DataFrame()
    return portfolio[portfolio["portfolio_action"] == "accepted"].copy()


def _summary_row_from_result(
    *,
    key: str,
    label: str,
    source: str,
    trades: pd.DataFrame,
    accepted: pd.DataFrame,
    monthly: pd.DataFrame,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """把组合回测结果转换为升级对比行。"""
    stats = r6._trade_stats(accepted)
    monthly_stats = r6._monthly_stability(monthly)
    return {
        "strategy_key": key,
        "strategy_label": label,
        "source": source,
        "event_trade_count": int(len(trades)),
        "accepted_trade_count": int(metrics.get("accepted_trades") or len(accepted)),
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


def _run_legacy_validation(
    *,
    histories: dict[str, list[dict]],
    active_dates: list[str],
    output_dir: Path,
    legacy_strategy_path: Path,
) -> dict[str, Any]:
    """复跑旧主策略，并输出组合层面指标。"""
    legacy_config = legacy_scanner._load_scanner_config(str(legacy_strategy_path))
    legacy_scanner._set_active_config(legacy_config)
    scanner._set_active_config(scanner._load_scanner_config("strategy.json"))

    metadata = r3._load_scan_metadata()
    stock_rows = _legacy_stock_rows(histories, metadata)
    market_timeline = r3._market_timeline()
    trades_list, event_summary = legacy_scanner._run_backtest(
        histories,
        stock_rows,
        market_timeline=market_timeline,
        min_score=float(legacy_scanner._cfg("backtest", "min_score", 70)),
        max_hold_days=int(legacy_scanner._cfg("backtest", "max_hold_days", 5)),
        entry_window_days=int(legacy_scanner._cfg("backtest", "entry_window_days", 2)),
        cost_bps=float(legacy_scanner._cfg("backtest", "cost_bps", 15)),
    )
    trades = pd.DataFrame(trades_list)
    if not trades.empty:
        trades["rank_score"] = pd.to_numeric(trades.get("score"), errors="coerce").fillna(0.0)
        if "market_regime_label" not in trades.columns or trades["market_regime_label"].isna().all():
            trades["market_regime_label"] = trades["signal_date"].map(
                lambda date: (market_timeline.get(str(date)) or {}).get("regime_label")
            )
        trades["strategy_key"] = LEGACY_KEY
        trades["strategy_label"] = "Legacy Momentum V1"
    else:
        trades = pd.DataFrame(
            columns=[
                "symbol",
                "entry_date",
                "exit_date",
                "rank_score",
                "position_pct",
                "net_return",
                "exit_reason",
            ]
        )

    initial_cash = float(scanner._cfg("backtest", "portfolio_initial_cash", 1_000_000))
    portfolio = r4._accept_portfolio_trades(trades, initial_cash)
    equity, monthly, metrics = r4._portfolio_equity_curve(portfolio, histories, active_dates, initial_cash)
    accepted = _accepted_trades(portfolio)

    _write_csv(output_dir / "legacy_momentum_v1_trades.csv", trades)
    _write_csv(output_dir / "legacy_momentum_v1_portfolio_trades.csv", portfolio)
    _write_csv(output_dir / "legacy_momentum_v1_accepted_trades.csv", accepted)
    _write_csv(output_dir / "legacy_momentum_v1_daily_equity.csv", equity)
    _write_csv(output_dir / "legacy_momentum_v1_monthly_returns.csv", monthly)
    _write_json(output_dir / "legacy_momentum_v1_event_summary.json", event_summary)

    return {
        "trades": trades,
        "portfolio": portfolio,
        "accepted": accepted,
        "equity": equity,
        "monthly": monthly,
        "metrics": metrics,
        "summary_row": _summary_row_from_result(
            key=LEGACY_KEY,
            label="Legacy Momentum V1",
            source="strategy_legacy_v1.json + market_scanner_legacy_v1.py",
            trades=trades,
            accepted=accepted,
            monthly=monthly,
            metrics=metrics,
        ),
    }


def _row_from_freeze_rerun(freeze_dir: Path) -> dict[str, Any]:
    """读取当前主策略 V3 复跑结果。"""
    rerun = _load_csv(freeze_dir / "freeze_rerun_summary.csv")
    if rerun.empty:
        raise RuntimeError(f"缺少 Freeze 复跑汇总：{freeze_dir / 'freeze_rerun_summary.csv'}")
    row = rerun[rerun["key"] == V3_FREEZE_KEY]
    if row.empty:
        raise RuntimeError("Freeze 复跑汇总中未找到 V3 行。")
    payload = row.iloc[0].to_dict()
    payload["strategy_key"] = NEW_MAIN_KEY
    payload["strategy_label"] = "Alpha040 V3 Risk Controlled"
    payload["source"] = str(freeze_dir / "freeze_rerun_summary.csv")
    for old in ["key", "label", "scenario_group"]:
        payload.pop(old, None)
    return payload


def _row_from_round6_original(round6_dir: Path) -> dict[str, Any]:
    """读取 Freeze V3 原始 Round6 结果。"""
    comparison = _load_csv(round6_dir / "strategy_comparison.csv")
    if comparison.empty:
        raise RuntimeError(f"缺少 Round6 原始汇总：{round6_dir / 'strategy_comparison.csv'}")
    row = comparison[comparison["strategy_key"] == V3_FREEZE_KEY]
    if row.empty:
        raise RuntimeError("Round6 原始汇总中未找到 V3 行。")
    payload = row.iloc[0].to_dict()
    payload["strategy_key"] = "freeze_v3_original"
    payload["strategy_label"] = "Freeze V3 Original"
    payload["source"] = str(round6_dir / "strategy_comparison.csv")
    return payload


def _monthly_wide(output_dir: Path, freeze_dir: Path, round6_dir: Path) -> pd.DataFrame:
    """生成 legacy / current V3 / original V3 月度收益宽表。"""
    sources = {
        LEGACY_KEY: output_dir / "legacy_momentum_v1_monthly_returns.csv",
        NEW_MAIN_KEY: freeze_dir / f"rerun_{V3_FREEZE_KEY}_monthly_returns.csv",
        "freeze_v3_original": round6_dir / f"{V3_FREEZE_KEY}_monthly_returns.csv",
    }
    rows: dict[str, dict[str, Any]] = {}
    for key, path in sources.items():
        df = _load_csv(path)
        if df.empty or "month" not in df.columns or "monthly_return" not in df.columns:
            continue
        for _, item in df.iterrows():
            month = str(item["month"])
            rows.setdefault(month, {"month": month})[key] = item.get("monthly_return")
    result = pd.DataFrame([rows[key] for key in sorted(rows)])
    _write_csv(output_dir / "upgrade_monthly_returns.csv", result)
    return result


def _compare_current_vs_freeze(current: dict[str, Any], original: dict[str, Any]) -> pd.DataFrame:
    """比较当前 V3 复跑与 Freeze 原始结果。"""
    metrics = [
        "event_trade_count",
        "accepted_trade_count",
        "ending_equity",
        "total_return",
        "max_drawdown",
        "sharpe",
        "calmar",
        "max_consecutive_losses",
        "stop_loss_count",
        "limit_down_blocked_exit_count",
    ]
    rows = []
    for metric in metrics:
        left = _safe_float(current.get(metric))
        right = _safe_float(original.get(metric))
        if left is None and right is None:
            match = True
            diff = None
        elif left is None or right is None:
            match = False
            diff = None
        else:
            diff = left - right
            tolerance = 0.01 if metric == "ending_equity" else 1e-6
            match = abs(diff) <= tolerance
        rows.append(
            {
                "metric": metric,
                "alpha040_v3_risk_controlled": left,
                "freeze_v3_original": right,
                "diff": diff,
                "match": match,
            }
        )
    return pd.DataFrame(rows)


def _write_mismatch_report(output_dir: Path, mismatch: pd.DataFrame) -> Path:
    """输出 mismatch 报告。"""
    bad = mismatch[mismatch["match"] != True]
    lines = [
        "# Mismatch Report",
        "",
        "Alpha040 V3 当前主策略复跑与 Freeze V3 原始结果不一致。升级应暂停，按 rollback guide 回退或先定位口径差异。",
        "",
        "| Metric | Current V3 | Freeze Original | Diff |",
        "|---|---:|---:|---:|",
    ]
    for _, row in bad.iterrows():
        lines.append(
            f"| {row['metric']} | {row['alpha040_v3_risk_controlled']} | "
            f"{row['freeze_v3_original']} | {row['diff']} |"
        )
    path = output_dir / "mismatch_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _strategy_assertions(strategy_path: Path, freeze_config_path: Path) -> dict[str, Any]:
    """检查策略配置是否处于预期升级状态。"""
    strategy = _read_json(strategy_path)
    freeze_config = _read_json(freeze_config_path)
    strategies = strategy.get("strategies") or {}
    new_strategy = strategies.get(NEW_MAIN_KEY) or {}
    execution_boundary = new_strategy.get("execution_boundary") or {}
    live_trading = execution_boundary.get("live_trading")
    if live_trading is None:
        live_trading = (new_strategy.get("live_trading") or {}).get("enabled")
    auto_order = execution_boundary.get("auto_order")
    if auto_order is None:
        auto_order = (new_strategy.get("auto_order") or {}).get("enabled")
    return {
        "strategy_path": str(strategy_path),
        "freeze_config_path": str(freeze_config_path),
        "active_strategy": strategy.get("active_strategy"),
        "market_scanner_active_strategy": (strategy.get("market_scanner") or {}).get("active_strategy"),
        "has_legacy_strategy": LEGACY_KEY in strategies,
        "has_new_strategy": NEW_MAIN_KEY in strategies,
        "freeze_strategy_name": freeze_config.get("strategy_name") or freeze_config.get("version"),
        "live_trading_enabled": bool(live_trading),
        "auto_order_enabled": bool(auto_order),
    }


def _module_smoke_lines(output_dir: Path) -> list[str]:
    """汇总新增模块 smoke 运行结果。"""
    lines = ["", "## Module Smoke Validation", ""]
    daily_summaries = sorted((output_dir / "daily_reports").glob("*/v3_daily_report_summary.json"))
    if daily_summaries:
        payload = _read_json(daily_summaries[-1])
        lines.append(
            f"- Scheduled report: ok, date {payload.get('date')}, "
            f"regime {payload.get('market_regime_label')}, path `{payload.get('report_path')}`"
        )
    else:
        lines.append("- Scheduled report: not run in this output directory.")

    forward_summary = output_dir / "forward_paper_trading" / "last_run_summary.json"
    if forward_summary.exists():
        payload = _read_json(forward_summary)
        lines.append(
            f"- Forward paper trading: ok, signal date {payload.get('signal_date')}, "
            f"signals {payload.get('signal_count')}, ledger `{payload.get('ledger_path')}`"
        )
    else:
        lines.append("- Forward paper trading: not run in this output directory.")

    expansion_summary = output_dir / "data_expansion" / "summary.json"
    if expansion_summary.exists():
        payload = _read_json(expansion_summary)
        lines.append(
            f"- Data expansion smoke: ok, symbols {payload.get('symbol_count')}, "
            f"trade dates {payload.get('trade_date_count')}, dir `{payload.get('expanded_dir')}`"
        )
    else:
        lines.append("- Data expansion smoke: not run in this output directory.")

    expanded_backtest_summary = output_dir / "expanded_backtest" / "expanded_backtest_summary.json"
    if expanded_backtest_summary.exists():
        payload = _read_json(expanded_backtest_summary)
        lines.append(
            f"- Expanded backtest entry: ok, data source {payload.get('data_source')}, "
            f"accepted trades {payload.get('accepted_trades')}, report `{payload.get('report_path')}`"
        )
    else:
        lines.append("- Expanded backtest entry: not run in this output directory.")
    return lines


def _render_report(
    *,
    output_dir: Path,
    assertions: dict[str, Any],
    comparison: pd.DataFrame,
    mismatch: pd.DataFrame,
    monthly: pd.DataFrame,
    freeze_summary: dict[str, Any],
) -> str:
    """生成主策略升级报告。"""
    has_mismatch = bool((mismatch["match"] != True).any())
    status = "BLOCKED: Freeze V3 mismatch" if has_mismatch else "PASSED"
    lines = [
        "# Main Strategy Upgrade V3 Report",
        "",
        f"- Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Upgrade status: {status}",
        f"- Active strategy: {assertions.get('active_strategy')}",
        f"- Market scanner active strategy: {assertions.get('market_scanner_active_strategy')}",
        f"- Legacy retained: {assertions.get('has_legacy_strategy')}",
        f"- New strategy retained: {assertions.get('has_new_strategy')}",
        f"- Live trading enabled: {assertions.get('live_trading_enabled')}",
        f"- Auto order enabled: {assertions.get('auto_order_enabled')}",
        "",
        "## Backup And Rollback",
        "",
        "- Legacy config: `strategy_legacy_v1.json`",
        "- Legacy scanner: `src/market_scanner_legacy_v1.py`",
        "- Legacy notes: `docs/legacy_strategy_notes.md`",
        "- Rollback guide: `docs/rollback_guide.md`",
        "",
        "## Strategy Comparison",
        "",
        "| Strategy | Source | Event Trades | Accepted | Return | Max DD | Sharpe | Calmar | Stop Loss | Limit Down Blocked | Max Loss Streak |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in comparison.iterrows():
        lines.append(
            f"| {row.get('strategy_key')} | {row.get('source')} | "
            f"{int(row.get('event_trade_count') or 0)} | {int(row.get('accepted_trade_count') or 0)} | "
            f"{_fmt_pct(row.get('total_return'))} | {_fmt_pct(row.get('max_drawdown'))} | "
            f"{row.get('sharpe')} | {row.get('calmar')} | "
            f"{int(row.get('stop_loss_count') or 0)} | {int(row.get('limit_down_blocked_exit_count') or 0)} | "
            f"{int(row.get('max_consecutive_losses') or 0)} |"
        )
    lines.extend(
        [
            "",
            "## Freeze Reproducibility",
            "",
            f"- Freeze validation reproducible: {freeze_summary.get('reproducible')}",
            f"- Freeze validation report: `{freeze_summary.get('report_path')}`",
            f"- Mismatch report: `{output_dir / 'mismatch_report.md'}`" if has_mismatch else "- Mismatch report: not generated",
            "",
            "## Monthly Returns",
            "",
        ]
    )
    if monthly.empty:
        lines.append("- No monthly return table was generated.")
    else:
        columns = [col for col in [LEGACY_KEY, NEW_MAIN_KEY, "freeze_v3_original"] if col in monthly.columns]
        lines.append("| Month | " + " | ".join(columns) + " |")
        lines.append("|---|" + "|".join(["---:"] * len(columns)) + "|")
        for _, row in monthly.iterrows():
            values = " | ".join(_fmt_pct(row.get(col)) for col in columns)
            lines.append(f"| {row.get('month')} | {values} |")
    lines.extend(
        [
            "",
            "## New Modules",
            "",
            "- Data expansion: `src/data_expansion_pipeline.py`",
            "- Expanded V3 backtest: `src/backtest_v3_expanded.py`",
            "- Scheduled V3 daily report: `src/scheduled_v3_reporter.py`",
            "- Forward paper trading: `src/forward_paper_trading_v3.py`",
        ]
    )
    lines.extend(_module_smoke_lines(output_dir))
    lines.extend(
        [
            "",
            "## Data Boundary",
            "",
            "- 当前升级验证仍以本地缓存样本为主，不能视为完整全市场长期回测。",
            "- 扩展历史数据模块已提供 Fuyao/Tushare/本地缓存降级链路；完整扩展数据回测需在数据覆盖报告通过后再作为新审计口径。",
            "- 本项目不连接实盘接口，不自动下单；所有计划均用于个人模拟交易与复盘。",
        ]
    )
    report = "\n".join(lines) + "\n"
    (output_dir / "main_strategy_upgrade_v3_report.md").write_text(report, encoding="utf-8")
    return report


def run_main_strategy_upgrade_v3(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    strategy_path: Path = Path("strategy.json"),
    freeze_config_path: Path = Path("freeze_v3_strategy.json"),
    legacy_strategy_path: Path = Path("strategy_legacy_v1.json"),
    round6_dir: Path = DEFAULT_ROUND6_DIR,
    cache_dir: Path = fr.DEFAULT_CACHE_DIR,
    min_bars: int = 80,
    min_history: int = 60,
    train_ratio: float = 0.7,
) -> dict[str, Any]:
    """执行主策略升级后的审计复跑。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    freeze_dir = output_dir / "freeze_validation"
    freeze_summary = freeze.run_freeze_v3_validation(
        output_dir=freeze_dir,
        round6_dir=round6_dir,
        cache_dir=cache_dir,
        min_bars=min_bars,
        min_history=min_history,
        train_ratio=train_ratio,
        strategy_path=str(strategy_path),
    )

    scanner._set_active_config(scanner._load_scanner_config(str(strategy_path)))
    histories = fr.load_cached_histories(cache_dir=cache_dir, min_bars=min_bars)
    if not histories:
        raise RuntimeError(f"未找到可用历史 K 线缓存：{cache_dir}")
    active_dates = _active_dates_from_freeze_universe(freeze_dir, histories)
    legacy_result = _run_legacy_validation(
        histories=histories,
        active_dates=active_dates,
        output_dir=output_dir,
        legacy_strategy_path=legacy_strategy_path,
    )
    current_v3 = _row_from_freeze_rerun(freeze_dir)
    original_v3 = _row_from_round6_original(round6_dir)
    comparison = pd.DataFrame([legacy_result["summary_row"], current_v3, original_v3])
    ordered_columns = [
        "strategy_key",
        "strategy_label",
        "source",
        "event_trade_count",
        "accepted_trade_count",
        "ending_equity",
        "total_return",
        "max_drawdown",
        "sharpe",
        "calmar",
        "win_rate",
        "average_trade_return",
        "median_trade_return",
        "max_consecutive_losses",
        "stop_loss_count",
        "limit_down_blocked_exit_count",
        "positive_month_rate",
        "negative_month_count",
        "worst_month",
        "worst_month_return",
        "monthly_return_std",
    ]
    comparison = comparison[[col for col in ordered_columns if col in comparison.columns]]
    _write_csv(output_dir / "upgrade_comparison.csv", comparison)

    monthly = _monthly_wide(output_dir, freeze_dir, round6_dir)
    mismatch = _compare_current_vs_freeze(current_v3, original_v3)
    _write_csv(output_dir / "v3_reproducibility_check.csv", mismatch)
    mismatch_path = None
    if bool((mismatch["match"] != True).any()):
        mismatch_path = _write_mismatch_report(output_dir, mismatch)

    assertions = _strategy_assertions(strategy_path, freeze_config_path)
    report = _render_report(
        output_dir=output_dir,
        assertions=assertions,
        comparison=comparison,
        mismatch=mismatch,
        monthly=monthly,
        freeze_summary=freeze_summary,
    )
    summary = {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "module": "main_strategy_upgrade_v3",
        "status": "blocked_mismatch" if mismatch_path else "passed",
        "active_strategy": assertions.get("active_strategy"),
        "comparison_path": str(output_dir / "upgrade_comparison.csv"),
        "monthly_returns_path": str(output_dir / "upgrade_monthly_returns.csv"),
        "reproducibility_check_path": str(output_dir / "v3_reproducibility_check.csv"),
        "mismatch_report_path": str(mismatch_path) if mismatch_path else None,
        "report_path": str(output_dir / "main_strategy_upgrade_v3_report.md"),
        "report_characters": len(report),
        "freeze_validation": freeze_summary,
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="执行主策略升级 V3 审计复跑")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--strategy", default="strategy.json", help="当前策略配置")
    parser.add_argument("--freeze-config", default="freeze_v3_strategy.json", help="冻结策略配置")
    parser.add_argument("--legacy-strategy", default="strategy_legacy_v1.json", help="旧策略备份配置")
    parser.add_argument("--round6-dir", default=str(DEFAULT_ROUND6_DIR), help="Round6 原始输出目录")
    parser.add_argument("--cache-dir", default=str(fr.DEFAULT_CACHE_DIR), help="历史 K 线缓存目录")
    parser.add_argument("--min-bars", type=int, default=80, help="单票最少 K 线数量")
    parser.add_argument("--min-history", type=int, default=60, help="动态 universe 最少历史长度")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="按时间顺序训练段比例")
    args = parser.parse_args()
    summary = run_main_strategy_upgrade_v3(
        output_dir=Path(args.output),
        strategy_path=Path(args.strategy),
        freeze_config_path=Path(args.freeze_config),
        legacy_strategy_path=Path(args.legacy_strategy),
        round6_dir=Path(args.round6_dir),
        cache_dir=Path(args.cache_dir),
        min_bars=args.min_bars,
        min_history=args.min_history,
        train_ratio=args.train_ratio,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=fr._json_default))


if __name__ == "__main__":
    main()
