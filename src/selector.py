"""
selector.py - 研究性选股/观察池排序模块

输入：output/YYYY-MM-DD/analysis/*.json
输出：output/YYYY-MM-DD/selection/selection.json 和 selection.md

本模块只做可解释的多因子排序，不输出买卖建议。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_STRATEGY_PATH = "strategy.json"


def _safe_float(value: Any) -> Optional[float]:
    """安全转 float，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """将分数限制在指定区间。"""
    return max(low, min(high, value))


def _score_threshold(value: Optional[float], good: float, floor: float = 0.0) -> float:
    """按阈值给 0-100 分，缺失时给中性偏低分。"""
    if value is None:
        return 40.0
    if value <= floor:
        return 20.0
    return _clip(value / good * 100.0)


def _load_json(path: Path) -> Optional[dict]:
    """读取 JSON 文件，失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"读取 JSON 失败: {path} ({exc})")
        return None


def load_strategy(strategy_path: str = DEFAULT_STRATEGY_PATH) -> dict:
    """
    加载策略配置。

    Args:
        strategy_path: strategy.json 路径
    Returns:
        策略配置字典
    """
    path = Path(strategy_path)
    data = _load_json(path)
    if data is None:
        raise FileNotFoundError(f"策略配置不可用: {strategy_path}")
    return data


def load_analysis_files(analysis_dir: Path) -> list[dict]:
    """
    加载 analysis 目录下的股票分析结果。

    Args:
        analysis_dir: output/YYYY-MM-DD/analysis
    Returns:
        analysis 字典列表
    """
    analyses: list[dict] = []
    for path in sorted(analysis_dir.glob("*.json")):
        data = _load_json(path)
        if data is not None:
            analyses.append(data)
    return analyses


def _quality_score(metrics: dict, thresholds: dict) -> tuple[float, list[str]]:
    """质量因子：ROE 与数据完整性。"""
    roe = _safe_float(metrics.get("roe"))
    score = _score_threshold(roe, _safe_float(thresholds.get("roe_good")) or 10.0)
    notes = [f"ROE={roe:.1f}%" if roe is not None else "ROE 缺失"]
    return round(score, 1), notes


def _growth_score(metrics: dict, thresholds: dict) -> tuple[float, list[str]]:
    """成长因子：营收同比、净利润同比。"""
    revenue_yoy = _safe_float(metrics.get("revenue_yoy"))
    profit_yoy = _safe_float(metrics.get("net_profit_yoy"))
    rev_score = _score_threshold(revenue_yoy, _safe_float(thresholds.get("revenue_yoy_good")) or 15.0)
    profit_score = _score_threshold(profit_yoy, _safe_float(thresholds.get("net_profit_yoy_good")) or 15.0)
    notes = [
        f"营收同比={revenue_yoy:.1f}%" if revenue_yoy is not None else "营收同比缺失",
        f"净利润同比={profit_yoy:.1f}%" if profit_yoy is not None else "净利润同比缺失",
    ]
    return round((rev_score + profit_score) / 2.0, 1), notes


def _valuation_score(metrics: dict, thresholds: dict) -> tuple[float, list[str]]:
    """估值因子：PE 三年分位越低分数越高，PB 作为轻量惩罚项。"""
    pe_pct = _safe_float(metrics.get("pe_percentile_3y"))
    pb = _safe_float(metrics.get("pb"))
    if pe_pct is None:
        pe_score = 40.0
    else:
        pe_score = _clip(100.0 - pe_pct)

    pb_penalty = 0.0
    if pb is not None and pb > 5:
        pb_penalty = min(20.0, (pb - 5.0) * 4.0)
    score = _clip(pe_score - pb_penalty)
    notes = [
        f"PE 三年分位={pe_pct:.1f}%" if pe_pct is not None else "PE 三年分位缺失",
        f"PB={pb:.2f}" if pb is not None else "PB 缺失",
    ]
    return round(score, 1), notes


def _momentum_score(indicators: dict, signals: dict) -> tuple[float, list[str]]:
    """动量因子：均线位置、MACD、RSI、布林带。"""
    close = _safe_float(indicators.get("close"))
    ma5 = _safe_float(indicators.get("ma5"))
    ma10 = _safe_float(indicators.get("ma10"))
    ma20 = _safe_float(indicators.get("ma20"))
    rsi = _safe_float(indicators.get("rsi14"))
    score = 50.0
    notes: list[str] = []

    if close is not None and ma5 is not None and ma10 is not None and ma20 is not None:
        if close > ma5 > ma10 > ma20:
            score += 25.0
            notes.append("价格位于多头均线结构上方")
        elif close > ma20:
            score += 12.0
            notes.append("价格位于 MA20 上方")
        elif close < ma5 < ma10 < ma20:
            score -= 20.0
            notes.append("价格处于短中期均线下方")
        else:
            notes.append("均线结构中性")
    else:
        notes.append("均线数据缺失")

    macd_status = signals.get("macd_status")
    if macd_status == "golden_recent":
        score += 12.0
        notes.append("MACD 近期金叉")
    elif macd_status == "above_zero":
        score += 8.0
        notes.append("DIF 位于零轴上方")
    elif macd_status == "death_recent":
        score -= 12.0
        notes.append("MACD 近期死叉")
    elif macd_status == "below_zero":
        score -= 6.0
        notes.append("DIF 位于零轴下方")

    if rsi is None:
        notes.append("RSI 缺失")
    elif 40 <= rsi <= 65:
        score += 8.0
        notes.append(f"RSI 中性偏强({rsi:.1f})")
    elif rsi > 75:
        score -= 12.0
        notes.append(f"RSI 偏热({rsi:.1f})")
    elif rsi < 30:
        score -= 6.0
        notes.append(f"RSI 偏弱({rsi:.1f})")

    boll = signals.get("bollinger_position")
    if boll == "upper_break":
        score -= 6.0
        notes.append("价格突破布林带上轨，短线波动风险上升")
    elif boll == "lower_break":
        score -= 4.0
        notes.append("价格跌破布林带下轨，趋势仍需观察")

    return round(_clip(score), 1), notes


def _risk_score(analysis: dict, metrics: dict, indicators: dict, thresholds: dict) -> tuple[float, list[str]]:
    """风险因子：数据质量、估值高分位、RSI 偏热等。高分表示风险较低。"""
    score = 100.0
    notes: list[str] = []

    if analysis.get("data_quality") != "complete":
        score -= 35.0
        notes.append(f"数据质量={analysis.get('data_quality')}")

    pe_pct = _safe_float(metrics.get("pe_percentile_3y"))
    high_pe = _safe_float(thresholds.get("pe_percentile_high")) or 85.0
    if pe_pct is not None and pe_pct >= high_pe:
        score -= 20.0
        notes.append(f"PE 分位偏高({pe_pct:.1f}%)")

    rsi = _safe_float(indicators.get("rsi14"))
    rsi_overheat = _safe_float(thresholds.get("rsi_overheat")) or 75.0
    if rsi is not None and rsi >= rsi_overheat:
        score -= 15.0
        notes.append(f"RSI 偏热({rsi:.1f})")

    if metrics.get("market_cap") is None:
        score -= 20.0
        notes.append("市值缺失")
    if metrics.get("pe_percentile_3y") is None:
        score -= 20.0
        notes.append("PE 分位缺失")

    if not notes:
        notes.append("未触发主要风险扣分项")
    return round(_clip(score), 1), notes


def _apply_hard_filters(analysis: dict, metrics: dict, hard_filters: dict) -> tuple[bool, list[str]]:
    """执行硬过滤，返回是否通过及原因。"""
    reasons: list[str] = []
    if hard_filters.get("require_complete_data", True) and analysis.get("data_quality") != "complete":
        reasons.append("数据质量非 complete")
    if hard_filters.get("exclude_missing_market_cap", True) and metrics.get("market_cap") is None:
        reasons.append("market_cap 缺失")
    if hard_filters.get("exclude_missing_pe_percentile", True) and metrics.get("pe_percentile_3y") is None:
        reasons.append("PE 三年分位缺失")
    return len(reasons) == 0, reasons


def score_stock(analysis: dict, strategy: dict) -> dict:
    """
    对单只股票打分。

    Args:
        analysis: analysis JSON 字典
        strategy: strategy.json 字典
    Returns:
        结构化打分结果
    """
    weights = strategy.get("weights") or {}
    thresholds = strategy.get("thresholds") or {}
    hard_filters = strategy.get("hard_filters") or {}

    technical = analysis.get("technical") or {}
    indicators = technical.get("indicators") or {}
    signals = technical.get("signals") or {}
    fundamental = analysis.get("fundamental") or {}
    metrics = fundamental.get("metrics") or {}

    passed, filter_reasons = _apply_hard_filters(analysis, metrics, hard_filters)
    quality, quality_notes = _quality_score(metrics, thresholds)
    growth, growth_notes = _growth_score(metrics, thresholds)
    valuation, valuation_notes = _valuation_score(metrics, thresholds)
    momentum, momentum_notes = _momentum_score(indicators, signals)
    risk, risk_notes = _risk_score(analysis, metrics, indicators, thresholds)

    factor_scores = {
        "quality": quality,
        "growth": growth,
        "valuation": valuation,
        "momentum": momentum,
        "risk": risk,
    }
    total_score = 0.0
    for factor, score in factor_scores.items():
        total_score += score * float(weights.get(factor, 0.0))
    if not passed:
        total_score *= 0.5

    return {
        "stock_code": analysis.get("stock_code"),
        "data_quality": analysis.get("data_quality"),
        "passed_filters": passed,
        "filter_reasons": filter_reasons,
        "total_score": round(total_score, 1),
        "factor_scores": factor_scores,
        "factor_notes": {
            "quality": quality_notes,
            "growth": growth_notes,
            "valuation": valuation_notes,
            "momentum": momentum_notes,
            "risk": risk_notes,
        },
    }


def _render_selection_markdown(selection: dict) -> str:
    """渲染策略筛选 Markdown。"""
    lines: list[str] = [
        f"# 策略筛选结果 - {selection['date']}",
        "",
        "> 本结果仅用于研究性观察池排序，不构成任何投资建议。",
        "",
        f"- 策略：{selection['strategy']['name']}（{selection['strategy']['version']}）",
        f"- 观察标的数量：{selection['total']}",
        f"- 通过硬过滤：{selection['passed_count']}",
        "",
        "## 观察优先级排序",
        "",
        "| 排名 | 标的 | 总分 | 质量 | 成长 | 估值 | 动量 | 风险 | 过滤 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in selection["ranked"]:
        scores = item["factor_scores"]
        filter_text = "通过" if item["passed_filters"] else "未通过：" + "；".join(item["filter_reasons"])
        lines.append(
            "| {rank} | {code} | {total:.1f} | {quality:.1f} | {growth:.1f} | {valuation:.1f} | {momentum:.1f} | {risk:.1f} | {filter_text} |".format(
                rank=item["rank"],
                code=item["stock_code"],
                total=item["total_score"],
                quality=scores["quality"],
                growth=scores["growth"],
                valuation=scores["valuation"],
                momentum=scores["momentum"],
                risk=scores["risk"],
                filter_text=filter_text,
            )
        )

    lines.extend(["", "## 因子说明", ""])
    for item in selection["ranked"]:
        notes = []
        for factor_notes in item["factor_notes"].values():
            notes.extend(factor_notes[:1])
        lines.append(f"- {item['stock_code']}：{'；'.join(notes[:5])}")
    lines.append("")
    return "\n".join(lines)


def run_selection(
    strategy_path: str = DEFAULT_STRATEGY_PATH,
    output_base: str = "output",
    date: Optional[str] = None,
) -> dict:
    """
    执行观察池策略筛选。

    Args:
        strategy_path: strategy.json 路径
        output_base:   输出根目录
        date:          输出日期（YYYY-MM-DD），None 时取今日
    Returns:
        selection 摘要字典
    """
    today = date or datetime.today().strftime("%Y-%m-%d")
    strategy = load_strategy(strategy_path)
    analysis_dir = Path(output_base) / today / "analysis"
    selection_dir = Path(output_base) / today / "selection"
    selection_dir.mkdir(parents=True, exist_ok=True)

    analyses = load_analysis_files(analysis_dir)
    ranked = [score_stock(analysis, strategy) for analysis in analyses]
    ranked.sort(key=lambda item: (item["passed_filters"], item["total_score"]), reverse=True)
    for idx, item in enumerate(ranked, start=1):
        item["rank"] = idx

    selection = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "strategy": {
            "name": strategy.get("name"),
            "version": strategy.get("version"),
            "description": strategy.get("description"),
            "weights": strategy.get("weights"),
            "thresholds": strategy.get("thresholds"),
            "hard_filters": strategy.get("hard_filters"),
        },
        "total": len(ranked),
        "passed_count": sum(1 for item in ranked if item["passed_filters"]),
        "top_n": strategy.get("top_n", len(ranked)),
        "ranked": ranked,
    }

    json_path = selection_dir / "selection.json"
    md_path = selection_dir / "selection.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(selection, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_render_selection_markdown(selection))

    logger.info(f"策略筛选完成: {json_path}")
    return {
        "path": str(json_path),
        "markdown_path": str(md_path),
        "total": selection["total"],
        "passed": selection["passed_count"],
        "top": ranked[: int(strategy.get("top_n", len(ranked)))],
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = run_selection()
    print(json.dumps(result, ensure_ascii=False, indent=2))
