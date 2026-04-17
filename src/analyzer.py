"""
analyzer.py - 技术面与基本面计算模块

纯确定性计算，不调用 LLM，不输出主观判断。
指标库：pandas-ta（禁止手写技术指标公式）
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import akshare as ak
import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)

# pandas-ta 生成的列名常量
_SMA5  = "SMA_5"
_SMA10 = "SMA_10"
_SMA20 = "SMA_20"
_MACD  = "MACD_12_26_9"    # DIF
_MACDH = "MACDh_12_26_9"   # 柱状图（histogram）
_MACDS = "MACDs_12_26_9"   # DEA（signal line）
_RSI   = "RSI_14"
_BBL   = "BBL_20_2.0_2.0"  # 布林下轨
_BBM   = "BBM_20_2.0_2.0"  # 布林中轨
_BBU   = "BBU_20_2.0_2.0"  # 布林上轨
_BBB   = "BBB_20_2.0_2.0"  # 带宽
_BBP   = "BBP_20_2.0_2.0"  # 价格在带内的百分位


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _code_with_suffix(code: str) -> str:
    """将 6 位代码转为带交易所后缀的格式（CLAUDE.md schema 约定）。"""
    suffix = "SH" if code.startswith(("6", "9")) else "SZ"
    return f"{code}.{suffix}"


def _safe_float(val: Any) -> Optional[float]:
    """安全转 float，转换失败返回 None（不抛异常）。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_pct(val: Any) -> Optional[float]:
    """将 '12.34%' 形式字符串转为 float 12.34，失败返回 None。"""
    if val is None:
        return None
    s = str(val).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# OHLCV 加载
# ---------------------------------------------------------------------------

def load_ohlcv(raw_dir: Path, code: str) -> Optional[pd.DataFrame]:
    """
    从 raw/ 目录加载 OHLCV CSV，并做基本校验。

    Args:
        raw_dir: output/YYYY-MM-DD/raw/ 路径
        code:    6 位股票代码
    Returns:
        包含 open/high/low/close/volume 的 DataFrame，失败返回 None
    """
    path = raw_dir / f"{code}_ohlcv.csv"
    if not path.exists():
        logger.error(f"[{code}] OHLCV 文件不存在: {path}")
        return None
    try:
        # utf-8-sig 兼容带 BOM 的文件（data_fetcher 统一写 utf-8-sig）
        df = pd.read_csv(path, parse_dates=["date"], encoding="utf-8-sig")
    except Exception as e:
        logger.error(f"[{code}] OHLCV 读取失败: {e}")
        return None

    required = {"date", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        logger.error(f"[{code}] OHLCV 缺少必要列: {missing}")
        return None

    df = df.sort_values("date").reset_index(drop=True)
    logger.info(f"[{code}] OHLCV 加载成功，{len(df)} 条")
    return df


# ---------------------------------------------------------------------------
# 技术指标计算
# ---------------------------------------------------------------------------

def _compute_indicators(df: pd.DataFrame, code: str) -> Optional[pd.DataFrame]:
    """
    用 pandas-ta 计算 MA / MACD / RSI / Bollinger。

    pandas-ta 要求列名首字母大写（Open/High/Low/Close/Volume）。
    计算完后恢复为小写列名，追加指标列。
    """
    rename_up = {"open": "Open", "high": "High", "low": "Low",
                 "close": "Close", "volume": "Volume"}
    rename_dn = {v: k for k, v in rename_up.items()}

    work = df.rename(columns={k: v for k, v in rename_up.items() if k in df.columns})

    try:
        work.ta.sma(length=5,  append=True)
        work.ta.sma(length=10, append=True)
        work.ta.sma(length=20, append=True)
        work.ta.macd(fast=12, slow=26, signal=9, append=True)
        work.ta.rsi(length=14, append=True)
        work.ta.bbands(length=20, std=2, append=True)
    except Exception as e:
        logger.error(f"[{code}] 指标计算异常: {e}")
        return None

    return work.rename(columns={k: v for k, v in rename_dn.items() if k in work.columns})


def _detect_ma_crossover(df: pd.DataFrame, lookback: int = 5) -> str:
    """
    检测最近 lookback 日内 MA5/MA10 是否出现金叉或死叉。

    金叉定义：SMA_5 由下穿上 SMA_10（前一日 SMA5 < SMA10，当日 SMA5 >= SMA10）
    死叉定义：SMA_5 由上穿下 SMA_10
    若同一窗口内两者都出现，以最近发生的为准。
    """
    window = df[[_SMA5, _SMA10]].dropna().tail(lookback + 1)
    if len(window) < 2:
        return "none"

    last_signal = "none"
    for i in range(1, len(window)):
        prev5, prev10 = window[_SMA5].iloc[i - 1], window[_SMA10].iloc[i - 1]
        curr5, curr10 = window[_SMA5].iloc[i],     window[_SMA10].iloc[i]
        if prev5 < prev10 and curr5 >= curr10:
            last_signal = "golden_5_10"
        elif prev5 > prev10 and curr5 <= curr10:
            last_signal = "death_5_10"
    return last_signal


def _detect_macd_status(df: pd.DataFrame, lookback: int = 5) -> str:
    """
    检测 MACD 状态。

    判断优先级（由高到低）：
    1. 最近 lookback 日内柱状图是否出现由负转正（golden_recent）
    2. 最近 lookback 日内柱状图是否出现由正转负（death_recent）
    3. 当前 DIF 是否在零轴上方（above_zero）/ 下方（below_zero）
    """
    h = df[[_MACDH, _MACD]].dropna()
    if h.empty:
        return "below_zero"

    window_h = h[_MACDH].tail(lookback + 1)
    for i in range(1, len(window_h)):
        if window_h.iloc[i - 1] <= 0 < window_h.iloc[i]:
            return "golden_recent"
        if window_h.iloc[i - 1] >= 0 > window_h.iloc[i]:
            return "death_recent"

    current_dif = _safe_float(h[_MACD].iloc[-1])
    if current_dif is None:
        return "below_zero"
    return "above_zero" if current_dif > 0 else "below_zero"


def _detect_rsi_zone(df: pd.DataFrame) -> str:
    """RSI(14) 当前值所处区间：overbought(>70) / oversold(<30) / neutral。"""
    rsi_val = _safe_float(df[_RSI].dropna().iloc[-1]) if _RSI in df.columns else None
    if rsi_val is None:
        return "neutral"
    if rsi_val > 70:
        return "overbought"
    if rsi_val < 30:
        return "oversold"
    return "neutral"


def _detect_bollinger_position(df: pd.DataFrame) -> str:
    """收盘价相对布林带的位置：upper_break / lower_break / in_band。"""
    last = df[[_BBL, _BBU, "close"]].dropna().iloc[-1] if _BBL in df.columns else None
    if last is None:
        return "in_band"
    close, bbl, bbu = last["close"], last[_BBL], last[_BBU]
    if close > bbu:
        return "upper_break"
    if close < bbl:
        return "lower_break"
    return "in_band"


def compute_technical(df: pd.DataFrame, code: str) -> dict:
    """
    计算全部技术指标并返回结构化字典。

    Args:
        df:   OHLCV DataFrame（含 date/open/high/low/close/volume）
        code: 6 位股票代码（用于日志）
    Returns:
        technical 字典，计算异常时对应字段置 null
    """
    df_ind = _compute_indicators(df, code)
    error_flag = df_ind is None
    if error_flag:
        logger.warning(f"[{code}] 指标计算全部异常，technical 置空")
        df_ind = df  # 继续运行，指标列不存在时各 detect 函数会返回默认值

    def last(col: str) -> Optional[float]:
        if col not in df_ind.columns:
            return None
        series = df_ind[col].dropna()
        return _safe_float(series.iloc[-1]) if not series.empty else None

    indicators: dict = {
        "ma5":         last(_SMA5),
        "ma10":        last(_SMA10),
        "ma20":        last(_SMA20),
        "macd_dif":    last(_MACD),
        "macd_dea":    last(_MACDS),
        "macd_hist":   last(_MACDH),
        "rsi14":       last(_RSI),
        "boll_upper":  last(_BBU),
        "boll_mid":    last(_BBM),
        "boll_lower":  last(_BBL),
        "boll_width":  last(_BBB),
        "close":       _safe_float(df["close"].iloc[-1]),
        "date":        str(df["date"].iloc[-1].date()),
    }

    signals: dict = {
        "ma_crossover":       _detect_ma_crossover(df_ind),
        "macd_status":        _detect_macd_status(df_ind),
        "rsi_zone":           _detect_rsi_zone(df_ind),
        "bollinger_position": _detect_bollinger_position(df_ind),
    }

    if error_flag:
        signals["calculation_error"] = True

    return {"indicators": indicators, "signals": signals}


# ---------------------------------------------------------------------------
# 历史 PE 分位数
# ---------------------------------------------------------------------------

def _fetch_pe_history(code: str, period: str = "近三年") -> Optional[pd.Series]:
    """从百度股市通抓取历史 PE(TTM) 序列，失败返回 None。"""
    try:
        df = ak.stock_zh_valuation_baidu(
            symbol=code, indicator="市盈率(TTM)", period=period
        )
        if df is None or df.empty:
            return None
        # 第二列为 PE 数值
        series = pd.to_numeric(df.iloc[:, 1], errors="coerce").dropna()
        return series if not series.empty else None
    except Exception as e:
        logger.warning(f"[{code}] 历史 PE 抓取失败: {e}")
        return None


def compute_pe_percentile(code: str, current_pe: Optional[float]) -> Optional[float]:
    """
    计算当前 PE 在近三年历史区间中的分位数（0~100）。

    Args:
        code:       6 位股票代码
        current_pe: 当前 PE 值（来自 fundamental JSON）
    Returns:
        百分位数（如 78.5），取不到历史数据时返回 None
    """
    if current_pe is None:
        return None

    pe_hist = _fetch_pe_history(code)
    if pe_hist is None:
        logger.warning(f"[{code}] 无法取历史 PE，pe_percentile 置 null")
        return None

    rank = (pe_hist < current_pe).sum() / len(pe_hist) * 100
    logger.info(f"[{code}] PE={current_pe:.2f}，三年分位={rank:.1f}%，样本={len(pe_hist)}")
    return round(float(rank), 1)


# ---------------------------------------------------------------------------
# 基本面处理
# ---------------------------------------------------------------------------

def load_fundamental(raw_dir: Path, code: str) -> Optional[dict]:
    """
    从 raw/ 目录加载基本面 JSON。

    Args:
        raw_dir: output/YYYY-MM-DD/raw/ 路径
        code:    6 位股票代码
    Returns:
        基本面字典，失败返回 None
    """
    path = raw_dir / f"{code}_fundamental.json"
    if not path.exists():
        logger.error(f"[{code}] fundamental 文件不存在: {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[{code}] fundamental 读取失败: {e}")
        return None


def compute_fundamental(raw_fundamental: dict, code: str) -> dict:
    """
    整理基本面字段，计算 PE 历史分位数，缺失字段显式置 null。

    Args:
        raw_fundamental: load_fundamental() 返回的原始字典
        code:            6 位股票代码
    Returns:
        fundamental 字典
    """
    pe = _safe_float(raw_fundamental.get("pe_ttm"))
    pb = _safe_float(raw_fundamental.get("pb"))

    metrics: dict = {
        "pe_ttm":          pe,
        "pb":              pb,
        "market_cap":      _safe_float(raw_fundamental.get("market_cap")),
        "float_market_cap": _safe_float(raw_fundamental.get("float_market_cap")),
        "roe":             _safe_pct(raw_fundamental.get("roe")),
        "revenue_yoy":     _safe_pct(raw_fundamental.get("revenue_yoy")),
        "net_profit_yoy":  _safe_pct(raw_fundamental.get("net_profit_yoy")),
        "report_date":     raw_fundamental.get("report_date"),
        "pe_percentile_3y": compute_pe_percentile(code, pe),
    }

    fetch_errors = raw_fundamental.get("fetch_errors", [])
    observations: list[str] = []
    if fetch_errors:
        observations.append(f"数据抓取部分失败，缺失来源: {', '.join(fetch_errors)}")
    if metrics["market_cap"] is None:
        observations.append("market_cap 数据缺失（计算异常，已跳过）")
    if metrics["pe_percentile_3y"] is None:
        observations.append("pe_percentile_3y 无法计算（历史 PE 数据不可用）")

    return {"metrics": metrics, "observations": observations}


# ---------------------------------------------------------------------------
# 主分析函数
# ---------------------------------------------------------------------------

def analyze_stock(code: str, raw_dir: Path, analysis_dir: Path) -> dict:
    """
    对单只股票完成技术面 + 基本面分析，输出结构化 JSON。

    Args:
        code:         6 位股票代码
        raw_dir:      output/YYYY-MM-DD/raw/ 路径
        analysis_dir: output/YYYY-MM-DD/analysis/ 路径
    Returns:
        分析结果字典（同时写入 JSON 文件）
    """
    analysis_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "stock_code":  _code_with_suffix(code),
        "analysis_time": datetime.now().isoformat(),
        "technical":   None,
        "fundamental": None,
        "synthesis":   "pending_llm_interpretation",
        "data_quality": "failed",
        "data_quality_reason": None,
    }

    # --- 加载 OHLCV ---
    ohlcv_df = load_ohlcv(raw_dir, code)
    if ohlcv_df is None:
        result["data_quality_reason"] = "OHLCV 文件缺失或读取失败"
        _write_result(result, code, analysis_dir)
        return result

    row_count = len(ohlcv_df)
    if row_count < 30:
        result["data_quality"] = "partial"
        result["data_quality_reason"] = f"OHLCV 仅 {row_count} 条，不足 30 条（部分指标无效）"
        logger.warning(f"[{code}] 数据不足 30 条，data_quality=partial")

    # --- 技术指标 ---
    try:
        technical = compute_technical(ohlcv_df, code)
        result["technical"] = technical
        logger.info(f"[{code}] 技术指标计算完成")
    except Exception as e:
        logger.error(f"[{code}] compute_technical 未捕获异常: {e}")
        result["technical"] = {"error": str(e)}

    # --- 基本面 ---
    raw_fund = load_fundamental(raw_dir, code)
    if raw_fund is not None:
        try:
            fundamental = compute_fundamental(raw_fund, code)
            result["fundamental"] = fundamental
            logger.info(f"[{code}] 基本面处理完成")
        except Exception as e:
            logger.error(f"[{code}] compute_fundamental 未捕获异常: {e}")
            result["fundamental"] = {"error": str(e)}
    else:
        result["data_quality_reason"] = (result.get("data_quality_reason") or "") + \
                                         " fundamental 文件缺失。"

    # --- 数据质量综合判定 ---
    tech_ok = result["technical"] is not None and "error" not in result["technical"]
    fund_ok = result["fundamental"] is not None and "error" not in result["fundamental"]

    if result["data_quality"] != "partial":  # 未被行数不足降级
        if tech_ok and fund_ok:
            result["data_quality"] = "complete"
        elif tech_ok or fund_ok:
            result["data_quality"] = "partial"
        # failed 是初始值，两者都无则保持

    _write_result(result, code, analysis_dir)
    return result


def _write_result(result: dict, code: str, analysis_dir: Path) -> None:
    """将分析结果写入 JSON 文件。"""
    out_path = analysis_dir / f"{code}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"[{code}] 分析结果写入: {out_path}")


# ---------------------------------------------------------------------------
# 批量入口
# ---------------------------------------------------------------------------

def run_analysis(
    watchlist_path: str = "watchlist.json",
    output_base: str = "output",
) -> list[dict]:
    """
    主入口：对 watchlist 中所有股票顺序执行分析。

    Args:
        watchlist_path: watchlist.json 路径
        output_base:    输出根目录
    Returns:
        所有股票的分析状态列表
    """
    import json as _json

    today = datetime.today().strftime("%Y-%m-%d")
    raw_dir = Path(output_base) / today / "raw"
    analysis_dir = Path(output_base) / today / "analysis"

    if not raw_dir.exists():
        logger.error(f"raw 目录不存在: {raw_dir}，请先运行 data_fetcher")
        return []

    with open(watchlist_path, "r", encoding="utf-8") as f:
        stocks = _json.load(f)["stocks"]

    logger.info(f"开始分析 {len(stocks)} 只股票，读取: {raw_dir}，输出: {analysis_dir}")

    summary: list[dict] = []
    for stock in stocks:
        code = stock["code"]
        logger.info(f"分析: {stock['name']} ({code})")
        result = analyze_stock(code, raw_dir, analysis_dir)
        summary.append({
            "code": code,
            "name": stock["name"],
            "data_quality": result["data_quality"],
            "reason": result.get("data_quality_reason"),
        })
        logger.info(f"{stock['name']} 完成，data_quality={result['data_quality']}")

    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    results = run_analysis()
    print()
    for r in results:
        reason = f" ({r['reason']})" if r.get("reason") else ""
        print(f"{r['name']} ({r['code']}): {r['data_quality']}{reason}")
