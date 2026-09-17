"""
scheduled_v3_reporter.py - Daily report for alpha040_v3_risk_controlled

每日收盘后生成 output/YYYY-MM-DD/v3_daily_report.md。
非积极环境只输出 no_trade_report，不生成买入候选。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import forward_paper_trading_v3 as forward
import market_scanner as scanner
import quant_core as qc

DEFAULT_OUTPUT_BASE = Path("output")
DEFAULT_FREEZE_CONFIG = Path("freeze_v3_strategy.json")


def _fmt_pct(value: Any) -> str:
    """百分比格式化。"""
    try:
        number = float(value)
        return f"{number * 100:.2f}%" if math.isfinite(number) else "缺失"
    except (TypeError, ValueError):
        return "缺失"


def render_v3_report(report_date: str, market_profile: dict[str, Any], candidates: list[dict],
                     reason_counts: dict[str, int], history_count: int, universe_count: int,
                     risk_on_threshold: Any = None) -> str:
    """将既有筛选结果写成事实、解释、局限和下一步，不推断策略有效性。"""
    regime = market_profile.get("regime_label")
    def number(value: Any) -> str:
        try:
            return f"{float(value):.2f}" if math.isfinite(float(value)) else "缺失"
        except (ValueError, TypeError):
            return "缺失"
    environment_evidence: list[str] = []
    if market_profile.get("score") is not None:
        threshold_text = f"；进入“积极”分类的原定门槛为 {number(risk_on_threshold)} 分" if risk_on_threshold is not None else ""
        environment_evidence.append(f"- 环境规则得分 {number(market_profile['score'])}{threshold_text}。分数不是胜率。")
    if market_profile.get("above_ma20_ratio") is not None:
        environment_evidence.append(f"- 已提供的指数样本中，位于自身 20 日均线之上的占 {_fmt_pct(market_profile['above_ma20_ratio'])}；指数平均当日涨跌幅 {_fmt_pct(market_profile.get('avg_index_change'))}。")
    if not regime:
        conclusion = "市场环境数据不足，本次无法完成候选判断；没有生成新的模拟候选。"
    elif regime != "积极":
        conclusion = f"本次没有新增模拟候选。环境被固定规则归为“{regime}”，尚未满足“积极环境才允许新开仓”的条件。"
    elif not candidates:
        conclusion = "环境条件已满足，但本次没有标的进入模拟观察名单；需要结合下方筛选原因理解。"
    else:
        conclusion = f"本次有 {len(candidates)} 只标的进入模拟观察名单；这表示符合冻结规则，需要后续复盘，不是策略已有效的证据。"
    reason_names = {
        "alpha040_core_ineligible": "排序基础条件未满足",
        "market_not_risk_on": "环境条件未满足",
        "missing_change_rate_5d": "缺少近五日涨跌幅",
        "hot_5d_filtered": "近五日涨幅超过原定上限",
        "missing_volatility_20d": "缺少波动率数据",
        "high_volatility_filtered": "波动率超过原定上限",
        "passed": "通过筛选",
    }
    reason_text = "；".join(f"{reason_names.get(key, '其他未识别筛选项')} {value} 只" for key, value in reason_counts.items()) or "无可用筛选记录"
    lines = [f"# 冻结策略观察日报｜{report_date}", "",
             "仅用于个人模拟研究与复盘，不构成投资建议，不连接实盘、不自动下单。", "",
             "## 今日结论", "", f"- {conclusion}", "",
             "## 事实依据", "",
             f"- 数据对应日期：{report_date}；环境分类：{regime or '缺失'}。",
             *environment_evidence,
             f"- 本次读取历史缓存 {history_count} 只，其中该日期进入可交易样本范围 {universe_count} 只。",
             f"- 筛选记录：{reason_text}。",
             "- 上述原因按每只标的首个未通过条件计数；通过筛选后还受候选数量上限约束。", "",
             "## 如何理解", "",
             "- 环境分类用于执行事先设定的过滤规则，不是对明日涨跌的预测。",
             "- 名单和模拟参数来自固定程序；排序靠前不等于上涨概率更高。", "",
             "## 还不能判断", "",
             "- 当前只覆盖已取得的缓存样本，不能视为完整全市场分析。",
             "- 单日有无候选都不能证明策略有效；本日报没有完成长样本或独立对照验证。", "",
             "## 下一步", "",
             "- 先检查数据日期与缺失项，再按原规则记录后续触发、退出和未触发情况。",
             "- 用后续样本和预先设定的对照验证假设；不因一天的结果调整参数或晋级策略。", "",
             "## 观察明细", ""]
    if not candidates:
        lines.append("- 本次无新增模拟候选。")
    for item in candidates:
        plan = scanner._build_trade_plan(item)
        lines += [f"- {item.get('name') or '名称缺失'}（{item.get('symbol') or '代码缺失'}）：",
                  f"  模拟触发区间 {plan.get('trigger_zone') or '缺失'}；止损 {number(plan.get('stop_loss'))}；第一止盈 {number(plan.get('first_take_profit'))}。",
                  f"  规则计算的模拟仓位 {number(plan.get('position_pct'))}%；不代表账户已持有。",
                  f"  排序参考：量价因子 {number(item.get('alpha040'))}、60 日相对强弱分位 {_fmt_pct(item.get('rps60'))}（不是胜率）；距近 20 日高点 {_fmt_pct(item.get('close_to_20d_high'))}。"]
    lines += ["", "## 数据来源", "",
              "- 来源为本次历史缓存、冻结策略配置与筛选记录；日期以本报告列明的统计日期为准。",
              "- 排名、计划参数属于规则输出；本报告未生成新的因果归因。", "",
              "仅为个人模拟研究记录；重要策略变化仍需独立实验、历史验证与人工确认。", ""]
    return "\n".join(lines)


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
    cache_dir: Path = qc.DEFAULT_CACHE_DIR,
) -> dict[str, Any]:
    """生成每日 V3 报告。"""
    scanner._set_active_config(scanner._load_scanner_config("strategy.json"))
    config = forward._load_freeze_config(freeze_config_path)
    thresholds = forward._freeze_thresholds(config)
    histories = qc.load_cached_histories(cache_dir=cache_dir, min_bars=80)
    metadata = qc._load_scan_metadata()
    alpha040_map = qc._build_alpha040_map(histories)
    universe_by_date, daily_universe = qc._build_dynamic_universe(histories, metadata, alpha040_map, 60, output_base / "_v3_daily_tmp")
    report_date = date or _latest_signal_date(universe_by_date)
    market_timeline = qc._market_timeline()
    market_profile = market_timeline.get(report_date) or {}
    strategy = qc.ShadowStrategy(
        key="v3_atr_risk_budget_hot5_vol_risk_on",
        label="alpha040_v3_risk_controlled",
        stop_variant="atr_stop_risk_budget",
        only_risk_on=True,
        filter_hot_5d=True,
        filter_high_volatility=True,
    )
    eligible, reason_counts = qc._eligible_for_strategy(universe_by_date.get(report_date, []), market_profile, strategy, thresholds)
    candidate_limit = int(market_profile.get("candidate_limit") or 5)
    candidates = eligible[:candidate_limit] if market_profile.get("regime_label") == "积极" else []
    output_dir = output_base / report_date
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "v3_daily_report.md"
    report = render_v3_report(report_date, market_profile, candidates, reason_counts,
                              len(histories), len(universe_by_date.get(report_date, [])),
                              (scanner._cfg("market_regimes", "risk_on", {}) or {}).get("min_score", 70))
    report_path.write_text(report, encoding="utf-8")
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
