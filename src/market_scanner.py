"""收盘扫描编排与兼容入口。具体职责见 scanner_*.py；历史函数名保留导入。"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
import argparse
import json
import logging
import requests
import subprocess

if __package__:
    from . import scanner_config as _config
    from .scanner_backtest import (
        _apply_risk_budget_positions,
        _compound_position_returns,
        _entry_price_for_bar,
        _parse_trigger_zone,
        _run_backtest,
        _run_portfolio_backtest,
        _run_shadow_experiments,
        _simulate_next_day_open_trade,
        _simulate_trade,
        _stop_execution_metrics,
        _summarize_backtest,
        _summarize_shadow_returns,
    )
    from .scanner_config import (
        ACTIVE_SCANNER_CONFIG,
        DEFAULT_SCANNER_CONFIG,
        _active_strategy_key,
        _cfg,
        _deep_merge,
        _is_alpha040_v3_active,
        _load_scanner_config,
        _strategy_cfg,
        _strategy_config,
        _strategy_display_name,
        _strategy_engine,
    )
    from .scanner_data import (
        DEFAULT_INDEXES,
        FUYAO_BASE_URL,
        _cache_is_fresh,
        _cache_key,
        _cache_path,
        _daily_returns,
        _enrich_index_with_history,
        _enrich_with_history,
        _fetch_fuyao_bars,
        _fetch_fuyao_historical,
        _fetch_fuyao_index_historical,
        _fetch_fuyao_index_snapshots,
        _fetch_fuyao_snapshots,
        _fetch_fuyao_ticker_map,
        _fetch_market_snapshot_fuyao,
        _fetch_quote_pages,
        _ftshare_runpy_path,
        _fuyao_api_key,
        _fuyao_get,
        _history_window_ms,
        _merge_bars,
        _moving_average,
        _normalize_fuyao_index,
        _normalize_fuyao_snapshot,
        _parse_bar_date,
        _read_bars_cache,
        _run_ftshare,
        _series_return,
        _verify_path,
        _write_bars_cache,
        fetch_market_snapshot,
    )
    from .scanner_ledger import (
        _close_ledger_trade,
        _date_diff_days,
        _ledger_paths,
        _normalize_position_audit_fields,
        _snapshot_bar,
        _trade_id,
        _update_trade_ledger,
    )
    from .scanner_report import (
        _market_comment,
        _render_report,
        _render_shadow_experiment_report,
    )
    from .scanner_rules import (
        _alpha040_from_bars,
        _alpha040_v3_rank_score,
        _apply_alpha040_v3_strategy,
        _apply_market_risk_controls,
        _apply_strategy_overlays,
        _atr_from_bars,
        _build_legacy_trade_plan,
        _build_market_profile,
        _build_market_timeline,
        _build_trade_plan,
        _classify_market_regime,
        _excluded_trade_examples,
        _market_allows_backtest_signal,
        _passes_enhanced_strategy,
        _percentile_ranks,
        _score_stock,
        _select_candidates,
        _signal_row_from_bar,
        _standardize_cross_section,
        _upper_shadow_ratio,
    )
    from .scanner_utils import (
        _fmt_pct,
        _fmt_source,
        _fmt_yi,
        _read_csv_rows,
        _symbol_cn_suffix,
        _symbol_fuyao_to_scan,
        _to_float,
        _write_csv,
    )
else:
    import scanner_config as _config
    from scanner_backtest import (
        _apply_risk_budget_positions,
        _compound_position_returns,
        _entry_price_for_bar,
        _parse_trigger_zone,
        _run_backtest,
        _run_portfolio_backtest,
        _run_shadow_experiments,
        _simulate_next_day_open_trade,
        _simulate_trade,
        _stop_execution_metrics,
        _summarize_backtest,
        _summarize_shadow_returns,
    )
    from scanner_config import (
        ACTIVE_SCANNER_CONFIG,
        DEFAULT_SCANNER_CONFIG,
        _active_strategy_key,
        _cfg,
        _deep_merge,
        _is_alpha040_v3_active,
        _load_scanner_config,
        _strategy_cfg,
        _strategy_config,
        _strategy_display_name,
        _strategy_engine,
    )
    from scanner_data import (
        DEFAULT_INDEXES,
        FUYAO_BASE_URL,
        _cache_is_fresh,
        _cache_key,
        _cache_path,
        _daily_returns,
        _enrich_index_with_history,
        _enrich_with_history,
        _fetch_fuyao_bars,
        _fetch_fuyao_historical,
        _fetch_fuyao_index_historical,
        _fetch_fuyao_index_snapshots,
        _fetch_fuyao_snapshots,
        _fetch_fuyao_ticker_map,
        _fetch_market_snapshot_fuyao,
        _fetch_quote_pages,
        _ftshare_runpy_path,
        _fuyao_api_key,
        _fuyao_get,
        _history_window_ms,
        _merge_bars,
        _moving_average,
        _normalize_fuyao_index,
        _normalize_fuyao_snapshot,
        _parse_bar_date,
        _read_bars_cache,
        _run_ftshare,
        _series_return,
        _verify_path,
        _write_bars_cache,
        fetch_market_snapshot,
    )
    from scanner_ledger import (
        _close_ledger_trade,
        _date_diff_days,
        _ledger_paths,
        _normalize_position_audit_fields,
        _snapshot_bar,
        _trade_id,
        _update_trade_ledger,
    )
    from scanner_report import (
        _market_comment,
        _render_report,
        _render_shadow_experiment_report,
    )
    from scanner_rules import (
        _alpha040_from_bars,
        _alpha040_v3_rank_score,
        _apply_alpha040_v3_strategy,
        _apply_market_risk_controls,
        _apply_strategy_overlays,
        _atr_from_bars,
        _build_legacy_trade_plan,
        _build_market_profile,
        _build_market_timeline,
        _build_trade_plan,
        _classify_market_regime,
        _excluded_trade_examples,
        _market_allows_backtest_signal,
        _passes_enhanced_strategy,
        _percentile_ranks,
        _score_stock,
        _select_candidates,
        _signal_row_from_bar,
        _standardize_cross_section,
        _upper_shadow_ratio,
    )
    from scanner_utils import (
        _fmt_pct,
        _fmt_source,
        _fmt_yi,
        _read_csv_rows,
        _symbol_cn_suffix,
        _symbol_fuyao_to_scan,
        _to_float,
        _write_csv,
    )

logger = logging.getLogger(f"{__package__}.market_scanner" if __package__ else "market_scanner")


def _enrich_fuyao_rows_with_history(rows: list[dict], enrich_limit: int) -> tuple[list[dict], dict[str, list[dict]]]:
    """对扶摇快照样本做历史 K 线补齐，避免全市场逐只请求。"""
    initial_scored = sorted([_score_stock(row) for row in rows], key=lambda x: x["score"], reverse=True)
    by_symbol = {row.get("symkey"): row for row in rows if row.get("symkey")}
    turnover_ranked = sorted(
        rows,
        key=lambda x: _to_float(x.get("turnover")) or 0,
        reverse=True,
    )

    seed_symbols: list[str] = []
    for item in initial_scored:
        symbol = item.get("symbol")
        if symbol and symbol in by_symbol and symbol not in seed_symbols:
            seed_symbols.append(symbol)
        if len(seed_symbols) >= enrich_limit:
            break
    for row in turnover_ranked:
        symbol = row.get("symkey")
        if symbol and symbol not in seed_symbols:
            seed_symbols.append(symbol)
        if len(seed_symbols) >= enrich_limit:
            break

    histories: dict[str, list[dict]] = {}
    for idx, symbol in enumerate(seed_symbols, start=1):
        try:
            bars = _fetch_fuyao_historical(symbol)
            histories[symbol] = bars
            _enrich_with_history(by_symbol[symbol], bars)
        except Exception as exc:
            logger.warning(f"历史K线补齐失败 {symbol} ({idx}/{len(seed_symbols)}): {exc}")
            by_symbol[symbol]["history_enriched"] = False
            by_symbol[symbol]["history_error"] = str(exc)
    return rows, histories


def _ensure_review_histories(active_rows: list[dict], histories: dict[str, list[dict]], source: str) -> dict[str, list[dict]]:
    """为已有 pending/open 台账补齐复盘所需历史 K。"""
    review_histories = dict(histories)
    if source != "fuyao":
        return review_histories
    symbols = [
        str(row.get("symbol") or "")
        for row in active_rows
        if (row.get("status") or "pending") in {"pending", "open"} and row.get("symbol")
    ]
    for symbol in sorted(set(symbols)):
        if symbol in review_histories:
            continue
        try:
            review_histories[symbol] = _fetch_fuyao_historical(symbol)
        except (RuntimeError, requests.RequestException, OSError, ValueError) as exc:
            logger.warning(f"复盘历史K线补齐失败 {symbol}: {exc}")
            review_histories[symbol] = []
    return review_histories


def _steward_output_path(output_base: str, date: str, mode: str) -> Path:
    """返回 Hermes 策略管家指定模式的产物路径。"""
    base = Path(output_base)
    scan_dir = base / date / "scan"
    if mode == "deep":
        return scan_dir / "strategy_steward_deep_report.md"
    if mode == "critic":
        return scan_dir / "strategy_steward_critic.md"
    if mode == "experiment":
        return Path("strategy_experiments") / date / "hermes_experiments.json"
    return scan_dir / "strategy_steward_report.md"


def _extract_steward_digest(report_path: Path, max_items: int = 3) -> list[str]:
    """从 Hermes 报告或实验草案中提取适合飞书摘要展示的核心行。"""
    if not report_path.exists():
        return []
    if report_path.suffix.lower() == ".json":
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return ["实验草案 JSON 解析失败，需人工查看原始输出"]
        lines: list[str] = []
        warning = str(payload.get("sample_warning") or "").strip()
        if warning:
            lines.append(warning)
        for experiment in payload.get("experiments") or []:
            if not isinstance(experiment, dict):
                continue
            name = experiment.get("name") or "未命名实验"
            sample_size = experiment.get("sample_size", "-")
            direction = experiment.get("parameter_direction") or "-"
            gate = experiment.get("promotion_gate") or "-"
            lines.append(f"实验草案：{name}｜样本 {sample_size}｜方向 {direction}｜晋级 {gate}")
            if len(lines) >= max_items:
                break
        return lines[:max_items]

    text = report_path.read_text(encoding="utf-8")
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        clean = line[2:].replace("**", "").replace("`", "").strip()
        if not clean or clean.startswith("数据来源："):
            continue
        lines.append(clean)
        if len(lines) >= max_items:
            break
    return lines


def _run_strategy_steward(
    date: str,
    output_base: str,
    mode: str,
    script: str,
    fail_on_error: bool = False,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """调用 Hermes 策略管家脚本，返回只读诊断产物摘要。"""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = Path(script).expanduser()
    if not script_path.is_absolute():
        script_path = repo_root / script_path
    output_path = _steward_output_path(output_base, date, mode)
    if not output_path.is_absolute():
        output_path = repo_root / output_path
    cmd = [
        str(script_path),
        "--date",
        date,
        "--output",
        output_base,
        "--mode",
        mode,
    ]
    summary: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "status": "pending",
        "script": str(script_path),
        "report_path": str(output_path),
        "digest_lines": [],
    }
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        summary.update(
            {
                "status": "failed",
                "returncode": None,
                "error": f"Hermes strategy steward timed out after {timeout_seconds}s",
                "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
                "stderr_tail": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
            }
        )
        if fail_on_error:
            raise RuntimeError(summary["error"]) from exc
        return summary

    summary.update(
        {
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
        }
    )
    if proc.returncode == 0 and output_path.exists():
        summary["status"] = "success"
        summary["digest_lines"] = _extract_steward_digest(output_path)
    else:
        summary["status"] = "failed"
        summary["error"] = f"Hermes strategy steward exited with code {proc.returncode}"
        if fail_on_error:
            raise RuntimeError(summary["error"])
    return summary


def run_market_scan(
    output_base: str = "output",
    date: Optional[str] = None,
    limit: int = 5,
    enrich_limit: Optional[int] = None,
    config_path: str = "strategy.json",
    push: bool = False,
    push_mode: str = "digest",
    push_dry_run: bool = False,
    steward: bool = False,
    steward_mode: Optional[str] = None,
    steward_fail_on_error: bool = False,
) -> dict:
    """
    执行收盘扫描并写入产物。

    Args:
        output_base: 输出根目录
        date:        日期 YYYY-MM-DD，None 时取今日
        limit:       候选数量上限
        enrich_limit:扶摇快照中补齐历史 K 的样本数
        config_path: 策略配置文件
        push:       True 时推送 scan_report.md
        push_mode:  digest 或 full
        push_dry_run: True 时只检查推送配置
        steward:    True 时运行 Hermes 策略管家
        steward_mode: Hermes 模式 daily/deep/critic/experiment
        steward_fail_on_error: True 时 Hermes 失败则中断
    Returns:
        运行摘要
    """
    config = _load_scanner_config(config_path)
    _set_active_config(config)
    today = date or datetime.today().strftime("%Y-%m-%d")
    output_dir = Path(output_base) / today / "scan"
    output_dir.mkdir(parents=True, exist_ok=True)

    stocks, indexes, source = fetch_market_snapshot()
    histories: dict[str, list[dict]] = {}
    if source == "fuyao":
        if enrich_limit is None:
            enrich_limit = int(_cfg("data", "enrich_limit", 120))
        stocks, histories = _enrich_fuyao_rows_with_history(stocks, enrich_limit=enrich_limit)
    pending_path, archive_path = _ledger_paths()
    active_ledger_rows = _read_csv_rows(pending_path)
    review_histories = _ensure_review_histories(active_ledger_rows, histories, source)

    scored = _apply_strategy_overlays([_score_stock(row) for row in stocks], histories=histories)
    scored.sort(key=lambda x: x.get("final_score", x["score"]), reverse=True)
    market_profile = _build_market_profile(indexes, scored)
    candidates = _select_candidates(scored, limit=limit, market_profile=market_profile)
    excluded = _excluded_trade_examples(scored, limit=30)
    stock_rows = {row.get("symkey"): row for row in stocks if row.get("symkey")}
    market_timeline = _build_market_timeline(indexes)
    backtest_trades, backtest_summary = (
        _run_backtest(
            histories,
            stock_rows,
            market_timeline,
            min_score=float(_cfg("backtest", "min_score", 70)),
            max_hold_days=int(_cfg("backtest", "max_hold_days", 5)),
            entry_window_days=int(_cfg("backtest", "entry_window_days", 2)),
            cost_bps=float(_cfg("backtest", "cost_bps", 15)),
        )
        if histories else ([], {})
    )
    portfolio_trades, portfolio_summary = _run_portfolio_backtest(backtest_trades) if backtest_trades else ([], {})
    shadow_rows: list[dict] = []
    shadow_summary: dict[str, Any] = {}
    shadow_report = ""
    if backtest_trades and bool(_cfg("shadow_experiments", "enabled", True)):
        shadow_rows, shadow_summary, shadow_report = _run_shadow_experiments(
            backtest_trades,
            histories,
            cost_bps=float(_cfg("backtest", "cost_bps", 15)),
            max_hold_days=int(_cfg("backtest", "max_hold_days", 5)),
        )
    ledger_summary = _update_trade_ledger(candidates, stocks, today, output_dir)
    review_summary: dict[str, Any] = {}
    if _cfg("review", "enabled", True):
        from reviewer import review_trade_updates

        review_summary = review_trade_updates(
            Path(str(ledger_summary.get("updates_path"))),
            today,
            output_dir,
            stocks,
            histories=review_histories,
            archive_path=Path(str(ledger_summary.get("archive_path"))),
            max_report_items=int(_cfg("review", "max_report_items", 8)),
        )
    health_summary: dict[str, Any] = {}
    if _cfg("strategy_health", "enabled", True):
        from strategy_health import build_strategy_health

        health_summary = build_strategy_health(
            output_base=Path(output_base),
            as_of_date=today,
            output_dir=output_dir,
            archive_path=archive_path,
            windows=list(_cfg("strategy_health", "windows", [5, 20, 60])),
            history_days=int(_cfg("strategy_health", "history_days", 120)),
            min_sample=int(_cfg("strategy_health", "min_sample", 10)),
            max_tag_rows=int(_cfg("strategy_health", "max_tag_rows", 12)),
        )

    fields = [
        "symbol", "name", "sector", "latest", "change_rate", "change_rate_5d",
        "change_rate_20d", "change_rate_60d", "turnover", "turnover_rate",
        "amplitude", "market_cap", "ma5", "ma10", "ma20", "volume_ratio",
        "volatility_20d", "close_to_20d_high", "rps20", "rps60",
        "alpha040", "alpha040_z", "rps60_z", "close_to_20d_high_z",
        "alpha040_core_score", "atr14", "upper_shadow_ratio",
        "strategy_score", "final_score", "strategy_tags", "strategy_notes",
        "history_days", "history_enriched", "score", "passed",
        "filter_reasons", "reasons", "risk_notes",
    ]
    _write_csv(output_dir / "daily_scans.csv", scored, fields)
    _write_csv(output_dir / "research_candidates.csv", candidates, fields)
    trade_fields = [
        "symbol", "name", "sector", "signal_price", "trigger_zone", "stop_loss",
        "first_take_profit", "base_position_pct", "position_pct",
        "raw_position_pct", "position_cap_pct", "market_regime_label",
        "market_position_multiplier", "effective_position_multiplier",
        "position_adjustment_note", "market_filter_note",
        "risk_per_share", "reward_risk",
        "plan", "latest", "change_rate", "change_rate_5d", "turnover",
        "turnover_rate", "amplitude", "rps20", "rps60", "strategy_score",
        "final_score", "alpha040", "alpha040_core_score", "atr14",
        "risk_budget_account_pct", "stop_distance_pct", "upper_shadow_ratio",
        "strategy_tags", "score", "same_symbol_overlap",
        "same_symbol_active_trade_ids", "overlap_risk_note", "risk_notes",
    ]
    _write_csv(output_dir / "simulated_trades.csv", candidates, trade_fields)
    _write_csv(output_dir / "excluded_watchlist.csv", excluded[:30], fields)
    backtest_fields = [
        "symbol", "name", "signal_date", "entry_date", "exit_date",
        "signal_price", "entry_price", "exit_price", "stop_loss",
        "first_take_profit", "holding_days", "exit_reason", "gross_return",
        "net_return", "stop_low_price", "stop_breach_pct", "stop_slippage_bps",
        "slippage_exit_price", "net_return_worst_intraday", "net_return_slippage",
        "slippage_vs_ideal_return", "position_pct", "market_regime_label", "score", "filter_reasons",
    ]
    _write_csv(output_dir / "backtest_trades.csv", backtest_trades, backtest_fields)
    portfolio_fields = [
        "symbol", "name", "signal_date", "entry_date", "exit_date",
        "entry_price", "exit_price", "exit_reason", "net_return",
        "position_pct", "position_value", "portfolio_position_pct",
        "portfolio_action", "score",
    ]
    _write_csv(output_dir / "portfolio_backtest_trades.csv", portfolio_trades, portfolio_fields)
    shadow_fields = [
        "experiment", "symbol", "name", "signal_date", "entry_date", "exit_date",
        "signal_price", "entry_price", "exit_price", "stop_loss", "first_take_profit",
        "holding_days", "exit_reason", "gross_return", "net_return", "position_pct",
        "risk_budget_position_pct", "risk_budget_account_pct", "stop_distance_pct",
        "stop_low_price", "stop_breach_pct", "stop_slippage_bps", "slippage_exit_price",
        "net_return_worst_intraday", "net_return_slippage", "slippage_vs_ideal_return",
        "market_regime_label", "score",
    ]
    _write_csv(output_dir / "shadow_experiments.csv", shadow_rows, shadow_fields)
    with open(output_dir / "backtest_summary.json", "w", encoding="utf-8") as f:
        json.dump(backtest_summary, f, ensure_ascii=False, indent=2)
    with open(output_dir / "portfolio_backtest_summary.json", "w", encoding="utf-8") as f:
        json.dump(portfolio_summary, f, ensure_ascii=False, indent=2)
    with open(output_dir / "shadow_experiments_summary.json", "w", encoding="utf-8") as f:
        json.dump(shadow_summary, f, ensure_ascii=False, indent=2)
    with open(output_dir / "shadow_experiments_report.md", "w", encoding="utf-8") as f:
        f.write(shadow_report)
    with open(output_dir / "market_profile.json", "w", encoding="utf-8") as f:
        json.dump(market_profile, f, ensure_ascii=False, indent=2)
    with open(output_dir / "strategy_version.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "active_strategy": _active_strategy_key(),
                "strategy_name": _strategy_display_name(),
                "engine": _strategy_engine(),
                "config_path": config_path,
                "generated_at": datetime.now().isoformat(),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    report = _render_report(
        today,
        indexes,
        scored,
        candidates,
        excluded,
        source,
        market_profile,
        backtest_summary,
        portfolio_summary,
        shadow_summary,
        ledger_summary,
        review_summary,
        health_summary,
    )
    report_path = output_dir / "scan_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    steward_summary: dict[str, Any] = {}
    learning_summary: dict[str, Any] = {}
    steward_enabled = steward or bool(_cfg("strategy_steward", "enabled", False))
    if steward_enabled:
        selected_steward_mode = steward_mode or str(_cfg("strategy_steward", "mode", "daily"))
        steward_summary = _run_strategy_steward(
            date=today,
            output_base=output_base,
            mode=selected_steward_mode,
            script=str(_cfg("strategy_steward", "script", "scripts/run_strategy_steward.sh")),
            fail_on_error=steward_fail_on_error or bool(_cfg("strategy_steward", "fail_on_error", False)),
            timeout_seconds=int(_cfg("strategy_steward", "timeout_seconds", 900)),
        )
        if bool(_cfg("strategy_learning", "enabled", True)):
            from strategy_learning import update_strategy_learning

            learning_summary = update_strategy_learning(
                output_base=Path(output_base),
                as_of_date=today,
                memory_dir=Path(str(_cfg("strategy_learning", "memory_dir", "data/strategy_learning"))),
            )
        report = _render_report(
            today,
            indexes,
            scored,
            candidates,
            excluded,
            source,
            market_profile,
            backtest_summary,
            portfolio_summary,
            shadow_summary,
            ledger_summary,
            review_summary,
            health_summary,
            steward_summary,
            learning_summary,
        )
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)

    push_results: list[dict] = []
    push_summary_path = output_dir / "push_summary.json"
    if push:
        from notifier import send_report

        results = send_report(report_path, mode=push_mode, dry_run=push_dry_run)
        push_results = [item.__dict__ for item in results]
        with open(push_summary_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": today,
                    "push_time": datetime.now().isoformat(),
                    "report_path": str(report_path),
                    "mode": push_mode,
                    "dry_run": push_dry_run,
                    "results": push_results,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    memory_path = output_dir / "memory.md"
    with open(memory_path, "w", encoding="utf-8") as f:
        f.write(f"# 扫描记忆 - {today}\n\n")
        f.write(f"- 扫描时间：{datetime.now().isoformat()}\n")
        f.write(f"- 当前策略：{_strategy_display_name()} / {_active_strategy_key()}\n")
        f.write(f"- 样本数量：{len(scored)}\n")
        f.write(f"- 历史K线补齐样本：{len(histories)}\n")
        f.write(f"- 市场环境：{market_profile.get('regime_label')}（{market_profile.get('score')}）\n")
        f.write(f"- 候选数量：{len(candidates)}\n")
        f.write(f"- 回测笔数：{backtest_summary.get('trade_count', 0) if backtest_summary else 0}\n")
        f.write(f"- 组合回测接受交易：{portfolio_summary.get('accepted_trades', 0) if portfolio_summary else 0}\n")
        if shadow_summary:
            next_open = shadow_summary.get("next_day_open_buy") or {}
            f.write(
                f"- 影子实验：次日开盘平均 {next_open.get('average_return')}，"
                f"相对当前 {next_open.get('average_return_delta_vs_current')}\n"
            )
        f.write(f"- 台账 active：{ledger_summary.get('active_count', 0)}\n")
        f.write(f"- 复盘对象：{review_summary.get('reviewed_count', 0) if review_summary else 0}\n")
        f.write(f"- 策略健康主窗口样本：{(health_summary.get('primary_window') or {}).get('reviewed_count', 0) if health_summary else 0}\n")
        if steward_summary:
            f.write(
                f"- Hermes 策略管家：{steward_summary.get('mode')} / "
                f"{steward_summary.get('status')} / {steward_summary.get('report_path')}\n"
            )
        if learning_summary:
            f.write(f"- 策略学习记忆：{learning_summary.get('memory_markdown_path')}\n")
        f.write("- 候选：")
        f.write("、".join(f"{item['name']}({item['symbol']})" for item in candidates))
        f.write("\n")

    return {
        "date": today,
        "active_strategy": _active_strategy_key(),
        "strategy_name": _strategy_display_name(),
        "sample_count": len(scored),
        "data_source": source,
        "candidate_count": len(candidates),
        "history_enriched_count": len(histories),
        "market_profile": market_profile,
        "backtest_summary": backtest_summary,
        "portfolio_summary": portfolio_summary,
        "shadow_experiments_summary": shadow_summary,
        "ledger_summary": ledger_summary,
        "review_summary": review_summary,
        "strategy_health_summary": health_summary,
        "strategy_steward_summary": steward_summary,
        "strategy_learning_summary": learning_summary,
        "report_path": str(report_path),
        "daily_scans_path": str(output_dir / "daily_scans.csv"),
        "candidates_path": str(output_dir / "research_candidates.csv"),
        "simulated_trades_path": str(output_dir / "simulated_trades.csv"),
        "market_profile_path": str(output_dir / "market_profile.json"),
        "backtest_trades_path": str(output_dir / "backtest_trades.csv"),
        "backtest_summary_path": str(output_dir / "backtest_summary.json"),
        "portfolio_backtest_trades_path": str(output_dir / "portfolio_backtest_trades.csv"),
        "portfolio_backtest_summary_path": str(output_dir / "portfolio_backtest_summary.json"),
        "shadow_experiments_path": str(output_dir / "shadow_experiments.csv"),
        "shadow_experiments_summary_path": str(output_dir / "shadow_experiments_summary.json"),
        "shadow_experiments_report_path": str(output_dir / "shadow_experiments_report.md"),
        "review_summary_path": review_summary.get("summary_path") if review_summary else None,
        "review_report_path": review_summary.get("report_path") if review_summary else None,
        "strategy_health_summary_path": health_summary.get("summary_path") if health_summary else None,
        "strategy_health_report_path": health_summary.get("report_path") if health_summary else None,
        "strategy_steward_report_path": steward_summary.get("report_path") if steward_summary else None,
        "strategy_learning_summary_path": str(output_dir / "strategy_learning_summary.json") if learning_summary else None,
        "strategy_learning_memory_path": learning_summary.get("memory_markdown_path") if learning_summary else None,
        "push_summary_path": str(push_summary_path) if push else None,
        "push_results": push_results,
        "memory_path": str(memory_path),
        "candidates": candidates,
    }


def _set_active_config(config: dict[str, Any]) -> None:
    """设置本次运行配置。"""
    global ACTIVE_SCANNER_CONFIG
    ACTIVE_SCANNER_CONFIG = config
    _config._set_active_config(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行 A 股收盘扫描、回测、台账和推送")
    parser.add_argument("--date", help="输出日期 YYYY-MM-DD，默认今日")
    parser.add_argument("--output", default="output", help="输出根目录")
    parser.add_argument("--limit", type=int, default=5, help="候选数量上限")
    parser.add_argument("--enrich-limit", type=int, help="历史 K 补齐样本数，默认读 strategy.json")
    parser.add_argument("--strategy", default="strategy.json", help="策略配置 JSON 路径")
    parser.add_argument("--push", action="store_true", help="生成扫描报告后推送")
    parser.add_argument("--push-mode", choices=["digest", "full"], default="digest", help="推送摘要或完整报告")
    parser.add_argument("--push-dry-run", action="store_true", help="只检查推送配置，不发送")
    parser.add_argument("--push-fail-on-error", action="store_true", help="任一推送通道失败时以非零状态退出")
    parser.add_argument("--steward", action="store_true", help="扫描后运行 Hermes 策略管家")
    parser.add_argument(
        "--steward-mode",
        choices=["daily", "deep", "critic", "experiment"],
        help="Hermes 策略管家模式，默认读 strategy.json",
    )
    parser.add_argument("--steward-fail-on-error", action="store_true", help="Hermes 失败时以非零状态退出")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    result = run_market_scan(
        output_base=args.output,
        date=args.date,
        limit=args.limit,
        enrich_limit=args.enrich_limit,
        config_path=args.strategy,
        push=args.push,
        push_mode=args.push_mode,
        push_dry_run=args.push_dry_run,
        steward=args.steward,
        steward_mode=args.steward_mode,
        steward_fail_on_error=args.steward_fail_on_error,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.push and args.push_fail_on_error:
        failed = [item for item in result.get("push_results", []) if item.get("status") == "failed"]
        if failed:
            raise SystemExit(1)
