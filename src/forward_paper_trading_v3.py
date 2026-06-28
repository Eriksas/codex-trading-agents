"""
forward_paper_trading_v3.py - Frozen v3 forward paper trading logger

本模块只记录冻结 v3 的信号、模拟触发区间和计划参数。
它不连接实盘接口、不自动下单、不根据 forward 结果修改规则。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import factor_research_round3 as r3
import factor_research_round6 as r6
import market_scanner as scanner

DEFAULT_FREEZE_CONFIG = Path("freeze_v3_strategy.json")
DEFAULT_OUTPUT_DIR = Path("output/main_strategy_upgrade_v3/forward_paper_trading")


def _safe_float(value: Any) -> Optional[float]:
    """安全转换 float。"""
    return fr._safe_float(value)


def _write_csv_append(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    """追加写 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    """读取 CSV。"""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _load_freeze_config(path: Path) -> dict[str, Any]:
    """读取冻结策略配置。"""
    if not path.exists():
        raise FileNotFoundError(f"缺少冻结配置：{path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _freeze_thresholds(config: dict[str, Any]) -> dict[str, float]:
    """从冻结配置读取阈值。"""
    filters = config.get("rules", {}).get("risk_filters", {})
    return {
        "change_rate_5d_q75": float(filters["change_rate_5d_max"]),
        "volatility_20d_q75": float(filters["volatility_20d_max"]),
    }


def _latest_signal_date(universe_by_date: dict[str, list[dict]]) -> str:
    """取最新有 universe 的信号日期。"""
    dates = [date for date, rows in universe_by_date.items() if rows]
    if not dates:
        raise RuntimeError("没有可用 universe 日期，无法生成 forward paper 信号。")
    return max(dates)


def _select_frozen_v3_signals(
    universe_by_date: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    thresholds: dict[str, float],
    signal_date: str,
) -> list[dict[str, Any]]:
    """选择冻结 v3 信号。"""
    strategy = r6.ShadowStrategy(
        key="v3_atr_risk_budget_hot5_vol_risk_on",
        label="Frozen v3 forward paper",
        stop_variant="atr_stop_risk_budget",
        only_risk_on=True,
        filter_hot_5d=True,
        filter_high_volatility=True,
    )
    market_profile = market_timeline.get(signal_date) or {}
    candidate_limit = int(market_profile.get("candidate_limit") or 5)
    eligible, _ = r6._eligible_for_strategy(universe_by_date.get(signal_date, []), market_profile, strategy, thresholds)
    return eligible[:candidate_limit]


def run_forward_paper_trading(
    *,
    freeze_config_path: Path = DEFAULT_FREEZE_CONFIG,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = fr.DEFAULT_CACHE_DIR,
    signal_date: Optional[str] = None,
    min_bars: int = 80,
    min_history: int = 60,
    strategy_path: str = "strategy.json",
) -> dict[str, Any]:
    """记录冻结 v3 forward paper 信号。"""
    config = _load_freeze_config(freeze_config_path)
    scanner._set_active_config(scanner._load_scanner_config(strategy_path))
    histories = fr.load_cached_histories(cache_dir=cache_dir, min_bars=min_bars)
    metadata = r3._load_scan_metadata()
    alpha040_map = r3._build_alpha040_map(histories)
    universe_by_date, _ = r3._build_dynamic_universe(histories, metadata, alpha040_map, min_history, output_dir)
    selected_date = signal_date or _latest_signal_date(universe_by_date)
    thresholds = _freeze_thresholds(config)
    market_timeline = r3._market_timeline()
    signals = _select_frozen_v3_signals(universe_by_date, market_timeline, thresholds, selected_date)
    rows: list[dict[str, Any]] = []
    for item in signals:
        plan = scanner._build_trade_plan(item)
        rows.append(
            {
                "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "signal_date": selected_date,
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "market_regime_label": (market_timeline.get(selected_date) or {}).get("regime_label"),
                "alpha040_core_score": item.get("alpha040_core_score"),
                "alpha040": item.get("alpha040"),
                "rps60": item.get("rps60"),
                "close_to_20d_high": item.get("close_to_20d_high"),
                "change_rate_5d": item.get("change_rate_5d"),
                "volatility_20d": item.get("volatility_20d"),
                "upper_shadow_ratio": item.get("upper_shadow_ratio"),
                "trigger_zone": plan.get("trigger_zone"),
                "stop_loss": plan.get("stop_loss"),
                "first_take_profit": plan.get("first_take_profit"),
                "position_pct": plan.get("position_pct"),
                "status": "pending_next_day_simulated_fill",
                "rule_version": config.get("freeze_version"),
                "notes": "forward paper only; no live order; no rule update",
            }
        )
    fieldnames = [
        "recorded_at",
        "signal_date",
        "symbol",
        "name",
        "market_regime_label",
        "alpha040_core_score",
        "alpha040",
        "rps60",
        "close_to_20d_high",
        "change_rate_5d",
        "volatility_20d",
        "upper_shadow_ratio",
        "trigger_zone",
        "stop_loss",
        "first_take_profit",
        "position_pct",
        "status",
        "rule_version",
        "notes",
    ]
    _write_csv_append(output_dir / "forward_signals.csv", rows, fieldnames)
    ledger_fields = [
        "recorded_at",
        "signal_date",
        "symbol",
        "name",
        "market_regime_label",
        "alpha040_core_score",
        "trigger_zone",
        "stop_loss",
        "first_take_profit",
        "position_pct",
        "status",
        "rule_version",
        "notes",
    ]
    _write_csv_append(output_dir / "paper_trading_ledger.csv", rows, ledger_fields)
    _write_weekly_review(output_dir)
    summary = {
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signal_date": selected_date,
        "signal_count": len(rows),
        "output_path": str(output_dir / "forward_signals.csv"),
        "ledger_path": str(output_dir / "paper_trading_ledger.csv"),
        "weekly_review_path": str(output_dir / "weekly_paper_review.md"),
        "safety": "仅记录冻结 v3 模拟信号，不连接实盘，不根据结果改规则。",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "last_run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _write_weekly_review(output_dir: Path) -> None:
    """输出最近 7 天 forward paper review。"""
    ledger = _read_csv(output_dir / "paper_trading_ledger.csv")
    now = datetime.now()
    recent = []
    for row in ledger:
        try:
            recorded_at = datetime.strptime(str(row.get("recorded_at")), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if now - recorded_at <= timedelta(days=7):
            recent.append(row)
    lines = [
        "# Weekly Paper Review",
        "",
        "Freeze V3 forward paper trading review. This file is observational only; parameters must not be changed from short-term results.",
        "",
        f"- Recent signal rows: {len(recent)}",
        f"- Total ledger rows: {len(ledger)}",
        "- Rule update allowed: no",
        "",
        "| Signal Date | Symbol | Name | Status | Position |",
        "|---|---|---|---|---:|",
    ]
    for row in recent[-30:]:
        lines.append(
            f"| {row.get('signal_date')} | {row.get('symbol')} | {row.get('name')} | "
            f"{row.get('status')} | {row.get('position_pct')} |"
        )
    (output_dir / "weekly_paper_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="记录冻结 v3 forward paper 信号")
    parser.add_argument("--freeze-config", default=str(DEFAULT_FREEZE_CONFIG), help="冻结策略配置路径")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--cache-dir", default=str(fr.DEFAULT_CACHE_DIR), help="股票历史 K 缓存目录")
    parser.add_argument("--date", help="指定信号日期；默认取最新可用缓存日期")
    parser.add_argument("--strategy", default="strategy.json", help="策略配置文件")
    args = parser.parse_args()
    summary = run_forward_paper_trading(
        freeze_config_path=Path(args.freeze_config),
        output_dir=Path(args.output),
        cache_dir=Path(args.cache_dir),
        signal_date=args.date,
        strategy_path=args.strategy,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
