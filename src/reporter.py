"""
reporter.py - 模板化报告生成模块

纯模板渲染，不调用 LLM，不输出主观判断。
输入：output/YYYY-MM-DD/analysis/*.json
输出：output/YYYY-MM-DD/report.md
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 中文标签映射
# ---------------------------------------------------------------------------

MA_CROSSOVER_LABELS: dict[str, str] = {
    "golden_5_10": "近 5 日 MA5 上穿 MA10（金叉）",
    "death_5_10":  "近 5 日 MA5 下穿 MA10（死叉）",
    "none":        "近期无交叉",
}

MACD_STATUS_LABELS: dict[str, str] = {
    "golden_recent": "MACD 近期金叉",
    "death_recent":  "MACD 近期死叉",
    "above_zero":    "DIF 位于零轴上方",
    "below_zero":    "DIF 位于零轴下方",
}

RSI_ZONE_LABELS: dict[str, str] = {
    "overbought": "超买区（RSI > 70）",
    "oversold":   "超卖区（RSI < 30）",
    "neutral":    "中性区（30～70）",
}

BOLLINGER_POSITION_LABELS: dict[str, str] = {
    "upper_break": "突破布林带上轨",
    "lower_break": "跌破布林带下轨",
    "in_band":     "运行于布林带内",
}

DISCLAIMER = """\
---
⚠️ **免责声明**
本报告由 AI 工作流自动生成，仅用于个人学习与技术探索，不构成任何投资建议。
所有分析基于公开历史数据，不保证准确性或完整性。
报告中的观察仅为数据描述，不应作为投资决策依据。
投资有风险，入市需谨慎。
---"""


# ---------------------------------------------------------------------------
# 格式化工具
# ---------------------------------------------------------------------------

def _fmt_float(val: Any, digits: int = 2, suffix: str = "") -> str:
    """数值格式化，None 显示"数据缺失"。"""
    if val is None:
        return "数据缺失"
    try:
        return f"{float(val):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "数据缺失"


def _fmt_pct(val: Any, digits: int = 1) -> str:
    """百分比格式化（值已是数值，如 32.53），None 显示"数据缺失"。"""
    if val is None:
        return "数据缺失"
    try:
        return f"{float(val):.{digits}f}%"
    except (TypeError, ValueError):
        return "数据缺失"


def _fmt_market_cap(val: Any) -> str:
    """市值（元）转亿元显示，None 显示"数据缺失"。"""
    if val is None:
        return "数据缺失"
    try:
        return f"{float(val) / 1e8:.0f} 亿"
    except (TypeError, ValueError):
        return "数据缺失"


def _label(mapping: dict[str, str], key: Optional[str]) -> str:
    """从映射表取中文标签，key 为 None 或未知时返回"数据缺失"。"""
    if key is None:
        return "数据缺失"
    return mapping.get(key, f"未知状态（{key}）")


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_analysis_files(analysis_dir: Path) -> list[dict]:
    """
    加载 analysis/ 目录下所有 JSON，按股票代码排序。

    Args:
        analysis_dir: output/YYYY-MM-DD/analysis/ 路径
    Returns:
        分析结果字典列表
    """
    results: list[dict] = []
    for path in sorted(analysis_dir.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                results.append(json.load(f))
            logger.info(f"加载: {path.name}")
        except Exception as e:
            logger.error(f"读取 {path.name} 失败: {e}")
    return results


def load_fetch_summary(raw_dir: Path) -> Optional[dict]:
    """加载 fetch_summary.json（用于元信息章节）。"""
    path = raw_dir / "fetch_summary.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"fetch_summary.json 读取失败: {e}")
        return None


def load_watchlist(watchlist_path: str = "watchlist.json") -> dict[str, dict]:
    """返回 code → stock_info 映射，用于补充名称、行业。"""
    try:
        with open(watchlist_path, "r", encoding="utf-8") as f:
            stocks = json.load(f)["stocks"]
        return {s["code"]: s for s in stocks}
    except Exception as e:
        logger.warning(f"watchlist.json 读取失败: {e}")
        return {}


# ---------------------------------------------------------------------------
# 报告各节渲染函数
# ---------------------------------------------------------------------------

def _render_header(date_str: str) -> str:
    return f"# 每日市场观察报告 - {date_str}\n"


def _render_overview(analyses: list[dict], date_str: str) -> str:
    complete = sum(1 for a in analyses if a.get("data_quality") == "complete")
    partial  = sum(1 for a in analyses if a.get("data_quality") == "partial")
    failed   = sum(1 for a in analyses if a.get("data_quality") == "failed")
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        "## 一、市场概览\n",
        f"- **观察标的数量**：{len(analyses)} 只",
        f"- **数据质量**：complete {complete} 只，partial {partial} 只，failed {failed} 只",
        f"- **数据来源**：OHLCV（新浪财经）、基本面（东方财富 + 百度股市通 + 同花顺）",
        f"- **报告生成时间**：{now_str}",
    ]
    return "\n".join(lines) + "\n"


def _render_single_stock(
    analysis: dict,
    idx: int,
    watchlist_map: dict[str, dict],
) -> str:
    code_with_suffix: str = analysis.get("stock_code", "")
    code6 = code_with_suffix.split(".")[0]
    stock_info = watchlist_map.get(code6, {})
    name   = stock_info.get("name", code6)
    sector = stock_info.get("sector", "")
    note   = stock_info.get("note", "")

    quality = analysis.get("data_quality", "failed")

    lines: list[str] = [
        f"### {idx}. {name} ({code_with_suffix}){f' | {sector}' if sector else ''}\n",
    ]

    # failed 状态只列出，不展开
    if quality == "failed":
        reason = analysis.get("data_quality_reason") or "原因未知"
        lines.append(f"> ⚠️ 数据质量异常，无法生成分析（{reason}）\n")
        return "\n".join(lines) + "\n"

    # ---------- 技术面 ----------
    tech = analysis.get("technical") or {}
    ind  = tech.get("indicators") or {}
    sig  = tech.get("signals") or {}

    lines.append("**技术面数据**\n")
    lines.append(f"- 收盘价：{_fmt_float(ind.get('close'))} 元  （数据日期：{ind.get('date', '数据缺失')}）")
    lines.append(f"- MA5 / MA10 / MA20：{_fmt_float(ind.get('ma5'))} / {_fmt_float(ind.get('ma10'))} / {_fmt_float(ind.get('ma20'))}")
    lines.append(f"- MACD：DIF = {_fmt_float(ind.get('macd_dif'))}, DEA = {_fmt_float(ind.get('macd_dea'))}, 柱状图 = {_fmt_float(ind.get('macd_hist'))}")
    lines.append(f"- RSI(14)：{_fmt_float(ind.get('rsi14'))}")
    lines.append(f"- 布林带：上轨 {_fmt_float(ind.get('boll_upper'))} / 中轨 {_fmt_float(ind.get('boll_mid'))} / 下轨 {_fmt_float(ind.get('boll_lower'))}")

    lines.append("")
    lines.append("**机械信号**\n")
    lines.append(f"- 均线交叉：{_label(MA_CROSSOVER_LABELS,       sig.get('ma_crossover'))}")
    lines.append(f"- MACD 状态：{_label(MACD_STATUS_LABELS,        sig.get('macd_status'))}")
    lines.append(f"- RSI 区间：{_label(RSI_ZONE_LABELS,            sig.get('rsi_zone'))}")
    lines.append(f"- 布林带位置：{_label(BOLLINGER_POSITION_LABELS, sig.get('bollinger_position'))}")

    if sig.get("calculation_error"):
        lines.append("\n> ⚠️ 部分指标计算异常，已跳过，请核查原始数据。")

    # ---------- 基本面 ----------
    fund    = analysis.get("fundamental") or {}
    metrics = fund.get("metrics") or {}
    obs     = fund.get("observations") or []

    pe_pct_str = _fmt_pct(metrics.get("pe_percentile_3y")) if metrics.get("pe_percentile_3y") is not None else "数据缺失"

    lines.append("")
    lines.append("**基本面数据**\n")
    lines.append(f"- PE (TTM)：{_fmt_float(metrics.get('pe_ttm'))} | 近三年分位数：{pe_pct_str}")
    lines.append(f"- PB：{_fmt_float(metrics.get('pb'))}")
    lines.append(f"- ROE：{_fmt_pct(metrics.get('roe'))}")
    lines.append(f"- 营收同比增速：{_fmt_pct(metrics.get('revenue_yoy'))}")
    lines.append(f"- 净利润同比增速：{_fmt_pct(metrics.get('net_profit_yoy'))}")
    lines.append(f"- 总市值：{_fmt_market_cap(metrics.get('market_cap'))}")
    lines.append(f"- 最新财报期：{metrics.get('report_date') or '数据缺失'}")

    if obs:
        lines.append("")
        for o in obs:
            lines.append(f"> ℹ️ {o}")

    # ---------- 综合观察 ----------
    synthesis = analysis.get("synthesis")
    lines.append("")
    lines.append("**综合观察**\n")
    if not synthesis or synthesis == "pending_llm_interpretation":
        lines.append("> _综合观察待 LLM sub-agent 填入（阶段 2 功能）_")
    else:
        lines.append(synthesis)

    if note:
        lines.append("")
        lines.append(f"> 📌 观察备注：{note}")

    lines.append("")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _render_all_stocks(analyses: list[dict], watchlist_map: dict[str, dict]) -> str:
    lines = ["## 二、逐股观察\n"]
    for idx, analysis in enumerate(analyses, start=1):
        lines.append(_render_single_stock(analysis, idx, watchlist_map))
    return "\n".join(lines)


def _render_quality_section(analyses: list[dict], fetch_summary: Optional[dict]) -> str:
    lines = ["## 三、数据质量与异常提示\n"]

    # 非 complete 的股票
    abnormal = [a for a in analyses if a.get("data_quality") != "complete"]
    if not abnormal:
        lines.append("- 本次运行所有标的数据质量均为 complete，无异常。")
    else:
        lines.append("**数据质量异常标的：**\n")
        for a in abnormal:
            code = a.get("stock_code", "")
            quality = a.get("data_quality", "unknown")
            reason = a.get("data_quality_reason") or "无详细原因"
            lines.append(f"- {code}：{quality}（{reason}）")

    lines.append("")

    # 数据源 fallback 情况
    lines.append("**数据源使用情况：**\n")
    if fetch_summary and "results" in fetch_summary:
        source_map: dict[str, list[str]] = {}
        for r in fetch_summary["results"]:
            src = r.get("ohlcv_source", "unknown")
            source_map.setdefault(src, []).append(r.get("name", r.get("code", "")))
        for src, names in source_map.items():
            src_label = {"em": "东方财富（主力）", "sina": "新浪财经（fallback）", "tx": "腾讯财经（fallback）"}.get(src, src)
            lines.append(f"- {src_label}：{', '.join(names)}")
    else:
        lines.append("- fetch_summary.json 不可用，来源信息缺失。")

    # 基本面来源缺失汇总
    lines.append("")
    missing_fund: list[str] = []
    for a in analyses:
        fund = a.get("fundamental") or {}
        obs  = fund.get("observations") or []
        for o in obs:
            if "缺失来源" in o:
                code = a.get("stock_code", "")
                missing_fund.append(f"- {code}：{o}")
    if missing_fund:
        lines.append("**基本面数据缺失明细：**\n")
        lines.extend(missing_fund)

    return "\n".join(lines) + "\n"


def _render_meta(date_str: str, fetch_summary: Optional[dict]) -> str:
    import akshare as ak
    import pandas_ta as ta

    py_ver  = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ak_ver  = getattr(ak, "__version__", "unknown")
    ta_ver  = getattr(ta, "version", "unknown")

    lines = [
        "## 四、附录：本次运行元信息\n",
        f"- **Python 版本**：{py_ver}",
        f"- **akshare 版本**：{ak_ver}",
        f"- **pandas-ta 版本**：{ta_ver}",
    ]

    if fetch_summary:
        results = fetch_summary.get("results", [])
        cache_count = 0  # fetch_summary 中未单独标记缓存命中，此处从 ohlcv_source 推断
        # 缓存命中：当 OHLCV 不经历 fallback 重试时 ohlcv_source 仍为 sina/em/tx
        # 严格说缓存命中无法从 fetch_summary 直接推断，标注说明
        lines.append("")
        lines.append("**OHLCV 数据源明细（本次抓取）：**\n")
        for r in results:
            src = r.get("ohlcv_source", "-")
            rows = r.get("ohlcv_rows", "-")
            lines.append(f"- {r.get('name', r.get('code'))}（{r.get('code')}）：{src}，{rows} 条")
        lines.append("")
        lines.append("> ℹ️ 缓存命中时直接读取 output/cache/ 本地文件，与 fetch_summary 中来源标记一致，不额外区分。")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def generate_report(
    watchlist_path: str = "watchlist.json",
    output_base: str = "output",
) -> Optional[Path]:
    """
    主入口：读取当日分析结果，生成 Markdown 报告。

    Args:
        watchlist_path: watchlist.json 路径
        output_base:    输出根目录
    Returns:
        报告文件路径，失败返回 None
    """
    today = datetime.today().strftime("%Y-%m-%d")
    analysis_dir = Path(output_base) / today / "analysis"
    raw_dir      = Path(output_base) / today / "raw"
    report_path  = Path(output_base) / today / "report.md"

    if not analysis_dir.exists():
        logger.error(f"analysis 目录不存在: {analysis_dir}，请先运行 analyzer")
        return None

    analyses      = load_analysis_files(analysis_dir)
    fetch_summary = load_fetch_summary(raw_dir)
    watchlist_map = load_watchlist(watchlist_path)

    if not analyses:
        logger.error("未找到任何分析文件，报告生成中止")
        return None

    logger.info(f"共加载 {len(analyses)} 份分析，生成报告: {report_path}")

    sections = [
        _render_header(today),
        "",
        DISCLAIMER,
        "",
        _render_overview(analyses, today),
        "",
        _render_all_stocks(analyses, watchlist_map),
        "",
        _render_quality_section(analyses, fetch_summary),
        "",
        _render_meta(today, fetch_summary),
        "",
        DISCLAIMER,
        "",
    ]

    report_content = "\n".join(sections)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    logger.info(f"报告写入完成: {report_path}（{len(report_content)} 字符）")
    return report_path


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    path = generate_report()
    if path:
        print(f"\n报告已生成: {path}")
    else:
        print("报告生成失败，请检查日志")
