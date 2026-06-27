"""
synthesizer.py - 确定性综合观察生成模块

将 analysis JSON 中的结构化指标转为中文描述，不调用 LLM。
输出定位为研究性数据描述，不包含投资建议或操作性语言。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

FORBIDDEN_TERMS = ("建议", "推荐", "看涨", "看跌", "买入", "卖出", "目标价", "应该")


def _safe_float(value: Any) -> Optional[float]:
    """安全转换为 float，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    """格式化数值，缺失时返回'数据缺失'。"""
    number = _safe_float(value)
    if number is None:
        return "数据缺失"
    return f"{number:.{digits}f}{suffix}"


def _valuation_bucket(pe_percentile: Any) -> str:
    """根据 PE 三年分位生成估值描述。"""
    pct = _safe_float(pe_percentile)
    if pct is None:
        return "PE 历史分位数据缺失"
    if pct >= 80:
        return f"PE 位于近三年高分位（{pct:.1f}%）"
    if pct >= 60:
        return f"PE 位于近三年中高分位（{pct:.1f}%）"
    if pct >= 40:
        return f"PE 位于近三年中位区间（{pct:.1f}%）"
    if pct >= 20:
        return f"PE 位于近三年中低分位（{pct:.1f}%）"
    return f"PE 位于近三年低分位（{pct:.1f}%）"


def _technical_tone(signals: dict, indicators: dict) -> str:
    """生成技术面方向的中性描述。"""
    parts: list[str] = []

    close = _safe_float(indicators.get("close"))
    ma5 = _safe_float(indicators.get("ma5"))
    ma10 = _safe_float(indicators.get("ma10"))
    ma20 = _safe_float(indicators.get("ma20"))
    if close is not None and ma5 is not None and ma10 is not None and ma20 is not None:
        if close >= max(ma5, ma10, ma20):
            parts.append("收盘价位于 MA5、MA10、MA20 上方")
        elif close <= min(ma5, ma10, ma20):
            parts.append("收盘价位于 MA5、MA10、MA20 下方")
        else:
            parts.append("收盘价位于主要均线之间")
    else:
        parts.append("均线位置数据不完整")

    ma_signal = signals.get("ma_crossover")
    if ma_signal == "golden_5_10":
        parts.append("近期 MA5 上穿 MA10")
    elif ma_signal == "death_5_10":
        parts.append("近期 MA5 下穿 MA10")

    macd_status = signals.get("macd_status")
    if macd_status == "golden_recent":
        parts.append("MACD 近期由负转正")
    elif macd_status == "death_recent":
        parts.append("MACD 近期由正转负")
    elif macd_status == "above_zero":
        parts.append("DIF 位于零轴上方")
    elif macd_status == "below_zero":
        parts.append("DIF 位于零轴下方")

    rsi_zone = signals.get("rsi_zone")
    rsi_value = _fmt(indicators.get("rsi14"), digits=1)
    if rsi_zone == "overbought":
        parts.append(f"RSI 处于偏高区间（{rsi_value}）")
    elif rsi_zone == "oversold":
        parts.append(f"RSI 处于偏低区间（{rsi_value}）")
    else:
        parts.append(f"RSI 处于中性区间（{rsi_value}）")

    bollinger = signals.get("bollinger_position")
    if bollinger == "upper_break":
        parts.append("价格高于布林带上轨")
    elif bollinger == "lower_break":
        parts.append("价格低于布林带下轨")
    else:
        parts.append("价格运行于布林带内")

    return "，".join(parts)


def _fundamental_tone(metrics: dict) -> str:
    """生成基本面指标的中性描述。"""
    pe_text = _valuation_bucket(metrics.get("pe_percentile_3y"))
    pb_text = f"PB 为 {_fmt(metrics.get('pb'))}"
    roe_text = f"ROE 为 {_fmt(metrics.get('roe'), digits=1, suffix='%')}"

    revenue = _safe_float(metrics.get("revenue_yoy"))
    profit = _safe_float(metrics.get("net_profit_yoy"))
    if revenue is None and profit is None:
        growth_text = "营收与净利润同比数据缺失"
    elif revenue is not None and profit is not None:
        growth_text = f"营收同比 {_fmt(revenue, digits=1, suffix='%')}，净利润同比 {_fmt(profit, digits=1, suffix='%')}"
    elif revenue is not None:
        growth_text = f"营收同比 {_fmt(revenue, digits=1, suffix='%')}，净利润同比数据缺失"
    else:
        growth_text = f"营收同比数据缺失，净利润同比 {_fmt(profit, digits=1, suffix='%')}"

    return f"{pe_text}，{pb_text}，{roe_text}，{growth_text}"


def _alignment_text(signals: dict, metrics: dict) -> str:
    """根据技术面与基本面信号生成一致性描述。"""
    tech_score = 0
    if signals.get("ma_crossover") == "golden_5_10":
        tech_score += 1
    elif signals.get("ma_crossover") == "death_5_10":
        tech_score -= 1
    if signals.get("macd_status") in {"golden_recent", "above_zero"}:
        tech_score += 1
    elif signals.get("macd_status") in {"death_recent", "below_zero"}:
        tech_score -= 1
    if signals.get("rsi_zone") == "overbought":
        tech_score += 1
    elif signals.get("rsi_zone") == "oversold":
        tech_score -= 1

    fund_score = 0
    for key in ("revenue_yoy", "net_profit_yoy", "roe"):
        value = _safe_float(metrics.get(key))
        if value is None:
            continue
        if value > 0:
            fund_score += 1
        elif value < 0:
            fund_score -= 1

    if tech_score == 0 or fund_score == 0:
        return "技术面与基本面存在部分数据缺口，当前以已获取指标作描述性观察"
    if (tech_score > 0 and fund_score > 0) or (tech_score < 0 and fund_score < 0):
        return "技术面与基本面方向较为一致"
    return "技术面与基本面方向存在分化"


def build_synthesis(analysis: dict) -> str:
    """
    生成单只股票的综合观察文本。

    Args:
        analysis: analyzer.py 输出的分析字典
    Returns:
        80~200 字左右的中文综合观察
    """
    if analysis.get("data_quality") == "failed":
        return "数据质量不足，无法生成综合观察。"

    technical = analysis.get("technical") or {}
    fundamental = analysis.get("fundamental") or {}
    indicators = technical.get("indicators") or {}
    signals = technical.get("signals") or {}
    metrics = fundamental.get("metrics") or {}

    date_text = indicators.get("date") or "数据日期缺失"
    text = (
        f"截至 {date_text}，{_technical_tone(signals, indicators)}。"
        f"基本面数据显示，{_fundamental_tone(metrics)}。"
        f"{_alignment_text(signals, metrics)}。"
    )

    for term in FORBIDDEN_TERMS:
        if term in text:
            raise ValueError(f"synthesis contains forbidden term: {term}")

    return text


def synthesize_file(analysis_path: Path) -> dict:
    """
    读取并更新单个 analysis JSON 的 synthesis 字段。

    Args:
        analysis_path: analysis/{code}.json 路径
    Returns:
        状态字典
    """
    with open(analysis_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    synthesis = build_synthesis(data)
    data["synthesis"] = synthesis
    data["synthesis_source"] = "template"
    data["synthesis_time"] = datetime.now().isoformat()

    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    code = analysis_path.stem
    logger.info(f"[{code}] synthesis 已写入: {analysis_path}")
    return {"code": code, "status": "success", "synthesis_chars": len(synthesis)}


def run_synthesis(
    watchlist_path: str = "watchlist.json",
    output_base: str = "output",
    date: Optional[str] = None,
) -> list[dict]:
    """
    批量生成综合观察。

    Args:
        watchlist_path: watchlist.json 路径
        output_base:    输出根目录
        date:           输出日期（YYYY-MM-DD），None 时取今日
    Returns:
        每只股票的 synthesis 状态列表
    """
    if date is None:
        date = datetime.today().strftime("%Y-%m-%d")

    with open(watchlist_path, "r", encoding="utf-8") as f:
        stocks = json.load(f)["stocks"]

    analysis_dir = Path(output_base) / date / "analysis"
    results: list[dict] = []
    for stock in stocks:
        code = stock["code"]
        path = analysis_dir / f"{code}.json"
        if not path.exists():
            logger.warning(f"[{code}] analysis JSON 不存在，跳过 synthesis")
            results.append({"code": code, "status": "skipped", "error": "analysis JSON missing"})
            continue
        try:
            results.append(synthesize_file(path))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.error(f"[{code}] synthesis 失败: {exc}")
            results.append({"code": code, "status": "failed", "error": str(exc)})
    return results
