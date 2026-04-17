"""
data_fetcher.py - 数据抓取模块

从 akshare 抓取 A 股日频 OHLCV 数据和基本面快照。
OHLCV 数据源优先级：东方财富 → 新浪财经 → 腾讯财经
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)

# A 股收盘后数据稳定的时间点（15:30 buffer），之后缓存视为最终数据
_MARKET_CLOSE_BUFFER = 15 * 60 + 30  # 分钟转秒数，15:30


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def load_watchlist(watchlist_path: str = "watchlist.json") -> list[dict]:
    """
    从 watchlist.json 加载自选股列表。

    Args:
        watchlist_path: watchlist.json 文件路径
    Returns:
        股票信息字典列表，每项含 code/name/sector/note
    """
    with open(watchlist_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["stocks"]


def _exchange_prefix(code: str, lower: bool = False) -> str:
    """
    根据股票代码推断交易所前缀。

    Args:
        code:  6 位股票代码
        lower: True 返回 "sh"/"sz"（新浪/腾讯格式），False 返回 "SH"/"SZ"
    """
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return prefix if lower else prefix.upper()


def _is_cache_valid(cache_path: Path, today: str) -> bool:
    """
    判断缓存文件是否仍然有效。

    当日 15:30 前：缓存当日数据但市场未收盘，可使用（盘中数据）
    当日 15:30 后：当日收盘数据已最终确定，缓存永久有效
    历史日期：无条件有效
    """
    if not cache_path.exists():
        return False

    # 从文件名解析缓存日期（格式：{code}_{YYYY-MM-DD}.csv）
    stem = cache_path.stem  # e.g. "000001_2026-04-17"
    parts = stem.rsplit("_", 1)
    if len(parts) != 2:
        return False
    cache_date = parts[1]

    if cache_date < today:
        return True  # 历史数据永久有效

    if cache_date == today:
        now = datetime.now()
        minutes_since_midnight = now.hour * 60 + now.minute
        close_minutes = 15 * 60 + 30  # 15:30
        # 盘中缓存也接受：避免频繁打数据源，analyzer 下游会标注"盘中数据"
        return True

    return False  # 未来日期不合法


# ---------------------------------------------------------------------------
# OHLCV 数据抓取（三源 fallback + 本地缓存）
# ---------------------------------------------------------------------------

def _normalize_ohlcv(df: pd.DataFrame, days: int, source: str) -> pd.DataFrame:
    """
    将各数据源的 DataFrame 统一为标准列格式。

    标准列：date, open, high, low, close, volume, amount, turnover_rate,
            pct_change, price_gap_flag, data_source
    """
    # 新浪列映射（stock_zh_a_daily）
    sina_map = {"turnover": "turnover_rate"}
    # 腾讯列映射（stock_zh_a_hist_tx）：amount 实际是成交量（手）
    tx_map = {"amount": "volume"}

    if source == "sina":
        df = df.rename(columns=sina_map)
        # 新浪无 amount（成交额），填 NaN
        if "amount" not in df.columns:
            df["amount"] = float("nan")
    elif source == "tx":
        df = df.rename(columns=tx_map)
        df["amount"] = float("nan")
        df["turnover_rate"] = float("nan")
    # 东方财富已在 fetch_ohlcv_em 内映射好

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").tail(days).reset_index(drop=True)

    # 计算涨跌幅（来源无此列时从收盘价计算）
    if "pct_change" not in df.columns and "close" in df.columns:
        df["pct_change"] = df["close"].pct_change() * 100

    # 标记价格跳空（>10% 单日变动）
    if "pct_change" in df.columns:
        df["price_gap_flag"] = df["pct_change"].abs() > 10.0
    else:
        df["price_gap_flag"] = False

    df["data_source"] = source

    # 仅保留标准列，顺序固定
    standard_cols = [
        "date", "open", "high", "low", "close",
        "volume", "amount", "turnover_rate",
        "pct_change", "price_gap_flag", "data_source",
    ]
    present = [c for c in standard_cols if c in df.columns]
    return df[present]


def _fetch_ohlcv_em(code: str, start_date: str, end_date: str, adjust: str) -> Optional[pd.DataFrame]:
    """东方财富来源（主力）。"""
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start_date, end_date=end_date, adjust=adjust,
        )
        if df is None or df.empty:
            return None
        col_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "换手率": "turnover_rate",
            "涨跌幅": "pct_change", "涨跌额": "price_change", "振幅": "amplitude",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        df = df.drop(columns=["股票代码", "price_change", "amplitude"], errors="ignore")
        return df
    except Exception as e:
        logger.debug(f"[{code}] 东方财富失败: {e}")
        return None


def _fetch_ohlcv_sina(code: str, start_date: str, end_date: str, adjust: str) -> Optional[pd.DataFrame]:
    """新浪财经来源（fallback 1）。"""
    symbol = f"{_exchange_prefix(code, lower=True)}{code}"
    try:
        df = ak.stock_zh_a_daily(
            symbol=symbol, start_date=start_date, end_date=end_date, adjust=adjust,
        )
        if df is None or df.empty:
            return None
        df = df.rename(columns={"turnover": "turnover_rate"})
        df = df.drop(columns=["outstanding_share"], errors="ignore")
        return df
    except Exception as e:
        logger.debug(f"[{code}] 新浪失败: {e}")
        return None


def _fetch_ohlcv_tx(code: str, start_date: str, end_date: str, adjust: str) -> Optional[pd.DataFrame]:
    """腾讯财经来源（fallback 2）。"""
    symbol = f"{_exchange_prefix(code, lower=True)}{code}"
    try:
        df = ak.stock_zh_a_hist_tx(
            symbol=symbol, start_date=start_date, end_date=end_date, adjust=adjust,
        )
        if df is None or df.empty:
            return None
        # 腾讯的 amount 列实际是成交量（手），重命名为 volume
        df = df.rename(columns={"amount": "volume"})
        df["amount"] = float("nan")
        df["turnover_rate"] = float("nan")
        return df
    except Exception as e:
        logger.debug(f"[{code}] 腾讯失败: {e}")
        return None


def _load_from_cache(code: str, cache_dir: Path, today: str) -> Optional[pd.DataFrame]:
    """从本地缓存加载当日 OHLCV 数据。"""
    cache_path = cache_dir / f"{code}_{today}.csv"
    if _is_cache_valid(cache_path, today):
        try:
            df = pd.read_csv(cache_path, parse_dates=["date"], encoding="utf-8-sig")
            logger.info(f"[{code}] 命中本地缓存: {cache_path}")
            return df
        except Exception as e:
            logger.warning(f"[{code}] 缓存读取失败，将重新抓取: {e}")
    return None


def _save_to_cache(df: pd.DataFrame, code: str, cache_dir: Path, today: str) -> None:
    """将 OHLCV DataFrame 保存到本地缓存。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{code}_{today}.csv"
    df.to_csv(cache_path, index=False, encoding="utf-8-sig")
    logger.info(f"[{code}] 缓存已写入: {cache_path}")


def fetch_ohlcv(
    code: str,
    days: int = 60,
    adjust: str = "qfq",
    cache_dir: Optional[Path] = None,
) -> Optional[pd.DataFrame]:
    """
    抓取单只股票近 N 个交易日的日频 OHLCV 数据。

    数据源优先级：本地缓存 → 东方财富 → 新浪财经 → 腾讯财经。
    任一网络来源成功后写入缓存，下次调用直接命中。

    Args:
        code:      6 位股票代码（如 "000001"）
        days:      需要的交易日数量（默认 60）
        adjust:    复权方式，"qfq" 前复权 / "hfq" 后复权 / "" 不复权
        cache_dir: 缓存目录，None 时使用 output/cache/
    Returns:
        标准化 DataFrame，三源全部失败时返回 None
    """
    if cache_dir is None:
        cache_dir = Path("output/cache")

    today = datetime.today().strftime("%Y-%m-%d")

    # 1. 查本地缓存
    cached = _load_from_cache(code, cache_dir, today)
    if cached is not None:
        return cached

    # 2. 计算日期范围（多取 2 倍自然日以保证足够的交易日）
    end_date = datetime.today()
    start_date = end_date - timedelta(days=days * 2)
    sd = start_date.strftime("%Y%m%d")
    ed = end_date.strftime("%Y%m%d")

    # 3. 三源依次尝试
    sources = [
        ("东方财富", "em",   lambda: _fetch_ohlcv_em(code, sd, ed, adjust)),
        ("新浪财经", "sina", lambda: _fetch_ohlcv_sina(code, sd, ed, adjust)),
        ("腾讯财经", "tx",   lambda: _fetch_ohlcv_tx(code, sd, ed, adjust)),
    ]

    raw_df: Optional[pd.DataFrame] = None
    used_source = ""
    for name, source_key, fetcher in sources:
        logger.info(f"[{code}] 尝试数据源: {name}")
        raw_df = fetcher()
        if raw_df is not None:
            used_source = source_key
            logger.info(f"[{code}] {name} 成功")
            break
        logger.warning(f"[{code}] {name} 失败，切换下一来源")

    if raw_df is None:
        logger.error(f"[{code}] 三个 OHLCV 数据源全部失败")
        return None

    # 4. 标准化列
    df = _normalize_ohlcv(raw_df, days, used_source)

    # 数据质量校验
    null_count = df.isnull().sum().sum()
    if null_count > 0:
        logger.warning(f"[{code}] OHLCV 含 {null_count} 个缺失值（来源: {used_source}）")

    gap_count = int(df.get("price_gap_flag", pd.Series([])).sum())
    if gap_count > 0:
        logger.warning(f"[{code}] 检测到 {gap_count} 个价格跳空日（>10%）")

    logger.info(f"[{code}] OHLCV 完成，{len(df)} 条记录，来源: {used_source}")

    # 5. 写入缓存
    _save_to_cache(df, code, cache_dir, today)

    return df


# ---------------------------------------------------------------------------
# 基本面数据抓取
# ---------------------------------------------------------------------------

def _fetch_market_cap_info(code: str) -> dict:
    """
    从东方财富抓取市值和基本股本信息。

    Returns:
        含 market_cap/total_shares/sector_em 的字典（失败返回空字典）
    """
    try:
        info_df = ak.stock_individual_info_em(symbol=code)
        info = dict(zip(info_df.iloc[:, 0], info_df.iloc[:, 1]))
        return {
            "market_cap": info.get("总市值"),
            "float_market_cap": info.get("流通市值"),
            "total_shares": info.get("总股本"),
            "float_shares": info.get("流通股"),
            "sector_em": info.get("行业"),
        }
    except Exception as e:
        logger.warning(f"[{code}] stock_individual_info_em 失败: {e}")
        return {}


def _fetch_pe_pb(code: str) -> dict:
    """
    从百度股市通抓取最新 PE(TTM) 和 PB。

    Returns:
        含 pe_ttm/pb 的字典（失败返回空字典）
    """
    result: dict = {}
    for indicator, key in [("市盈率(TTM)", "pe_ttm"), ("市净率", "pb")]:
        try:
            df = ak.stock_zh_valuation_baidu(
                symbol=code, indicator=indicator, period="近一年"
            )
            if df is not None and not df.empty:
                result[key] = df.iloc[-1, 1]
        except Exception as e:
            logger.warning(f"[{code}] {indicator} 抓取失败: {e}")
    return result


def _fetch_financial_indicators(code: str) -> dict:
    """
    从同花顺抓取最新财报期的 ROE、营收同比、净利润同比增速。

    Returns:
        含 roe/revenue_yoy/net_profit_yoy/report_date 的字典（失败返回空字典）
    """
    try:
        df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
        if df is None or df.empty:
            logger.warning(f"[{code}] THS 财务摘要返回空")
            return {}

        # THS 数据从旧到新，最后一行为最新报告期
        latest = df.iloc[-1]

        def clean(val: object) -> Optional[str]:
            return str(val) if val is not False and val is not None else None

        return {
            "roe": clean(latest.get("净资产收益率")),
            "revenue_yoy": clean(latest.get("营业总收入同比增长率")),
            "net_profit_yoy": clean(latest.get("净利润同比增长率")),
            "report_date": str(latest.get("报告期")),
        }
    except Exception as e:
        logger.warning(f"[{code}] stock_financial_abstract_ths 失败: {e}")
        return {}


def fetch_fundamental(code: str) -> Optional[dict]:
    """
    抓取单只股票基本面快照：PE、PB、市值、ROE、营收/利润同比增速。

    三个数据源独立抓取，部分失败不阻断；全部失败才返回 None。

    Args:
        code: 6 位股票代码（如 "000001"）
    Returns:
        基本面字典，三个来源全部失败时返回 None
    """
    result: dict = {"code": code, "fetch_time": datetime.now().isoformat()}
    errors: list[str] = []

    market_info = _fetch_market_cap_info(code)
    if market_info:
        result.update(market_info)
        logger.info(f"[{code}] 市值信息抓取成功")
    else:
        errors.append("stock_individual_info_em")

    pe_pb = _fetch_pe_pb(code)
    if pe_pb:
        result.update(pe_pb)
        logger.info(f"[{code}] PE/PB 成功: PE={pe_pb.get('pe_ttm')}, PB={pe_pb.get('pb')}")
    else:
        errors.append("stock_zh_valuation_baidu")

    fin_indicators = _fetch_financial_indicators(code)
    if fin_indicators:
        result.update(fin_indicators)
        logger.info(f"[{code}] 财务指标抓取成功")
    else:
        errors.append("stock_financial_abstract_ths")

    if errors:
        result["fetch_errors"] = errors

    if len(errors) == 3:
        logger.error(f"[{code}] 三个基本面数据源全部失败")
        return None

    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def fetch_stock_data(stock: dict, output_dir: Path, cache_dir: Path) -> dict:
    """
    抓取单只股票完整数据（OHLCV + 基本面）并保存到输出目录。

    Args:
        stock:      watchlist.json 中单只股票字典（含 code/name/sector/note）
        output_dir: 原始数据输出目录（如 output/2024-01-15/raw/）
        cache_dir:  OHLCV 缓存目录（output/cache/）
    Returns:
        状态字典，含 code/name/ohlcv_status/fundamental_status/data_quality
    """
    code = stock["code"]
    name = stock["name"]
    output_dir.mkdir(parents=True, exist_ok=True)

    status: dict = {
        "code": code,
        "name": name,
        "ohlcv_status": "failed",
        "fundamental_status": "failed",
        "data_quality": "failed",
    }

    ohlcv_df = fetch_ohlcv(code, days=60, cache_dir=cache_dir)
    if ohlcv_df is not None:
        ohlcv_path = output_dir / f"{code}_ohlcv.csv"
        ohlcv_df.to_csv(ohlcv_path, index=False, encoding="utf-8-sig")
        status["ohlcv_status"] = "success"
        status["ohlcv_rows"] = len(ohlcv_df)
        status["ohlcv_source"] = ohlcv_df["data_source"].iloc[0] if "data_source" in ohlcv_df.columns else "unknown"
        status["ohlcv_path"] = str(ohlcv_path)
        logger.info(f"[{code}] OHLCV 已保存: {ohlcv_path}")

    fundamental = fetch_fundamental(code)
    if fundamental is not None:
        fundamental["stock_name"] = name
        fundamental["sector"] = stock.get("sector", "")
        fundamental["note"] = stock.get("note", "")
        fund_path = output_dir / f"{code}_fundamental.json"
        with open(fund_path, "w", encoding="utf-8") as f:
            json.dump(fundamental, f, ensure_ascii=False, indent=2, default=str)
        status["fundamental_status"] = "success"
        status["fundamental_path"] = str(fund_path)
        logger.info(f"[{code}] 基本面已保存: {fund_path}")

    ohlcv_ok = status["ohlcv_status"] == "success"
    fund_ok = status["fundamental_status"] == "success"
    if ohlcv_ok and fund_ok:
        status["data_quality"] = "complete"
    elif ohlcv_ok or fund_ok:
        status["data_quality"] = "partial"

    return status


def run_data_fetch(
    watchlist_path: str = "watchlist.json",
    output_base: str = "output",
) -> list[dict]:
    """
    主入口：读取 watchlist，顺序抓取所有股票数据，保存到当日输出目录。

    Args:
        watchlist_path: watchlist.json 路径
        output_base:    输出根目录
    Returns:
        所有股票的抓取状态列表
    """
    today = datetime.today().strftime("%Y-%m-%d")
    raw_dir = Path(output_base) / today / "raw"
    cache_dir = Path(output_base) / "cache"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    stocks = load_watchlist(watchlist_path)
    logger.info(f"开始抓取 {len(stocks)} 只股票，输出: {raw_dir}，缓存: {cache_dir}")

    results: list[dict] = []
    for i, stock in enumerate(stocks):
        if i > 0:
            time.sleep(1)  # 防止连续请求触发反爬
        logger.info(f"处理: {stock['name']} ({stock['code']})")
        status = fetch_stock_data(stock, raw_dir, cache_dir)
        results.append(status)
        logger.info(f"{stock['name']} 完成，数据质量: {status['data_quality']}")

    summary = {
        "date": today,
        "total": len(stocks),
        "success_count": sum(1 for r in results if r["data_quality"] == "complete"),
        "partial_count": sum(1 for r in results if r["data_quality"] == "partial"),
        "failed_count": sum(1 for r in results if r["data_quality"] == "failed"),
        "results": results,
    }
    summary_path = raw_dir / "fetch_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info(f"抓取完成，汇总: {summary_path}")

    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    results = run_data_fetch()
    print()
    for r in results:
        src = r.get("ohlcv_source", "-")
        print(f"{r['name']} ({r['code']}): {r['data_quality']}  "
              f"OHLCV={r['ohlcv_status']}[{src}]  基本面={r['fundamental_status']}")
