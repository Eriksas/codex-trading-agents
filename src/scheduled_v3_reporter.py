"""
scheduled_v3_reporter.py - Daily report for alpha040_v3_risk_controlled

每日收盘后生成 output/YYYY-MM-DD/v3_daily_report.md。
非积极环境只输出 no_trade_report，不生成买入候选。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import factor_research_round3 as r3
import factor_research_round6 as r6
import forward_paper_trading_v3 as forward
import market_scanner as scanner

DEFAULT_OUTPUT_BASE = Path("output")
DEFAULT_FREEZE_CONFIG = Path("freeze_v3_strategy.json")


def _fmt_pct(value: Any) -> str:
    """百分比格式化。"""
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


def _latest_signal_date(universe_by_date: dict[str, list[dict]]) -> str:
    """取最新有数据日期。"""
    dates = [date for date, rows in universe_by_date.items() if rows]
    if not dates:
        raise RuntimeError("没有可用 universe 日期")
    return max(dates)


def _hermes_digest(date: str) -> str:
    """读取 Hermes 学习摘要。"""
    candidates = [
        Path("data/strategy_learning/memory.md"),
        Path("output") / date / "scan" / "strategy_learning_summary.json",
    ]
    for path in candidates:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            return "\n".join(text.splitlines()[:6])
    return "暂无 Hermes 学习摘要。"


def generate_v3_daily_report(
    *,
    date: Optional[str] = None,
    output_base: Path = DEFAULT_OUTPUT_BASE,
    freeze_config_path: Path = DEFAULT_FREEZE_CONFIG,
    cache_dir: Path = fr.DEFAULT_CACHE_DIR,
) -> dict[str, Any]:
    """生成每日 V3 报告。"""
    scanner._set_active_config(scanner._load_scanner_config("strategy.json"))
    config = forward._load_freeze_config(freeze_config_path)
    thresholds = forward._freeze_thresholds(config)
    histories = fr.load_cached_histories(cache_dir=cache_dir, min_bars=80)
    metadata = r3._load_scan_metadata()
    alpha040_map = r3._build_alpha040_map(histories)
    universe_by_date, daily_universe = r3._build_dynamic_universe(histories, metadata, alpha040_map, 60, output_base / "_v3_daily_tmp")
    report_date = date or _latest_signal_date(universe_by_date)
    market_timeline = r3._market_timeline()
    market_profile = market_timeline.get(report_date) or {}
    strategy = r6.ShadowStrategy(
        key="v3_atr_risk_budget_hot5_vol_risk_on",
        label="alpha040_v3_risk_controlled",
        stop_variant="atr_stop_risk_budget",
        only_risk_on=True,
        filter_hot_5d=True,
        filter_high_volatility=True,
    )
    eligible, reason_counts = r6._eligible_for_strategy(universe_by_date.get(report_date, []), market_profile, strategy, thresholds)
    candidate_limit = int(market_profile.get("candidate_limit") or 5)
    candidates = eligible[:candidate_limit] if market_profile.get("regime_label") == "积极" else []
    output_dir = output_base / report_date
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "v3_daily_report.md"
    lines = [
        f"# Alpha040 V3 Daily Report - {report_date}",
        "",
        "本报告仅用于个人模拟交易、复盘和技术研究；不构成投资建议，不连接实盘，不自动下单。",
        "",
        "## 市场环境",
        "",
        f"- 当前市场环境：{market_profile.get('regime_label') or '未知'}",
        f"- 是否允许开仓：{'是' if market_profile.get('regime_label') == '积极' else '否'}",
    ]
    if market_profile.get("regime_label") != "积极":
        lines.extend(
            [
                "",
                "## No Trade Report",
                "",
                "- 非积极市场环境，冻结 V3 禁止新开仓。",
                f"- 过滤原因统计：`{json.dumps(reason_counts, ensure_ascii=False)}`",
            ]
        )
    else:
        lines.extend(["", "## 今日候选", "", "| 标的 | alpha040 | rps60 | close_to_20d_high | 触发区间 | ATR止损 | 风险预算仓位 | 第一止盈 | 上影线标签 |", "|---|---:|---:|---:|---|---:|---:|---:|---:|"])
        for item in candidates:
            plan = scanner._build_trade_plan(item)
            lines.append(
                f"| {item.get('name')} {item.get('symbol')} | {item.get('alpha040')} | {item.get('rps60')} | "
                f"{_fmt_pct(item.get('close_to_20d_high'))} | {plan.get('trigger_zone')} | {plan.get('stop_loss')} | "
                f"{plan.get('position_pct')}% | {plan.get('first_take_profit')} | {_fmt_pct(item.get('upper_shadow_ratio'))} |"
            )
    lines.extend(
        [
            "",
            "## 当前模拟持仓与事件",
            "",
            "- 详见 `data/ledger/pending_trades.csv` 与 `data/ledger/trade_archive.csv`。",
            "",
            "## 风险提示",
            "",
            "- 冻结 V3 当前仍处于个人模拟和 forward paper 阶段。",
            "- 当前 universe 受本地缓存覆盖限制，不能视为完整全市场扫描。",
            "",
            "## Hermes 学习摘要",
            "",
            _hermes_digest(report_date),
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = {
        "date": report_date,
        "report_path": str(report_path),
        "market_regime_label": market_profile.get("regime_label"),
        "candidate_count": len(candidates),
        "no_trade_report": market_profile.get("regime_label") != "积极",
    }
    (output_dir / "v3_daily_report_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="生成 Alpha040 V3 每日报告")
    parser.add_argument("--date", help="报告日期，默认最新缓存日期")
    parser.add_argument("--output-base", default=str(DEFAULT_OUTPUT_BASE), help="输出根目录")
    parser.add_argument("--freeze-config", default=str(DEFAULT_FREEZE_CONFIG), help="冻结配置")
    args = parser.parse_args()
    summary = generate_v3_daily_report(
        date=args.date,
        output_base=Path(args.output_base),
        freeze_config_path=Path(args.freeze_config),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
