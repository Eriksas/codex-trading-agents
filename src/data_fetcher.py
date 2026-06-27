"""
data_fetcher.py - 数据抓取模块

从 akshare 抓取 A 股日频 OHLCV 数据和基本面快照。
OHLCV 数据源优先级：东方财富 → 新浪财经 → 腾讯财经
"""

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import akshare as ak
import pandas as pd
import requests

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is declared in requirements
    certifi = None

logger = logging.getLogger(__name__)

# A 股收盘后数据稳定的时间点（15:30 buffer），之后缓存视为最终数据
_MARKET_CLOSE_BUFFER = 15 * 60 + 30  # 分钟转秒数，15:30
_EM_SPOT_CACHE: Optional[dict[str, dict]] = None
_AK_SPOT_CACHE: Optional[dict[str, dict]] = None


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

def _requests_verify() -> str | bool:
    """返回 requests HTTPS 校验配置。"""
    return certifi.where() if certifi is not None else True


def _to_float(val: object) -> Optional[float]:
    """安全转 float，失败返回 None。"""
    if val is None or val == "-":
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_cn_amount(val: object) -> Optional[float]:
    """解析 '272.43亿' / '1.2万' 等中文金额为元。"""
    if val is None:
        return None
    text = str(val).strip().replace(",", "")
    match = re.match(r"^(-?\d+(?:\.\d+)?)(万亿|亿|万)?$", text)
    if not match:
        return _to_float(text)
    number = float(match.group(1))
    unit = match.group(2)
    multiplier = {"万": 1e4, "亿": 1e8, "万亿": 1e12}.get(unit, 1.0)
    return number * multiplier


def _exchange_symbol_sina(code: str) -> str:
    """生成新浪单股行情 symbol。"""
    return f"{_exchange_prefix(code, lower=True)}{code}"


def _exchange_symbol_tx(code: str) -> str:
    """生成腾讯单股行情 symbol。"""
    return f"{_exchange_prefix(code, lower=True)}{code}"


def _stock_symbol_with_suffix(code: str) -> str:
    """生成带交易所后缀的 A 股 symbol。"""
    suffix = "SH" if code.startswith(("6", "9")) else "SZ"
    return f"{code}.{suffix}"


def _ftshare_runpy_path() -> Optional[Path]:
    """返回 FTShare skill 的 run.py 路径，未安装则返回 None。"""
    configured = os.environ.get("FTSHARE_RUN_PY")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / ".codex" / "skills" / "ftshare-market-data" / "run.py",
    ]
    for path in candidates:
        if path and path.exists():
            return path
    return None


def _fetch_security_info_ftshare(code: str) -> dict:
    """
    通过 FTShare-market-data skill 查询单票实时行情与估值。

    Returns:
        含 latest_price/market_cap/pe_ttm/pb/roe_ttm 等字段的字典（失败返回空字典）
    """
    run_py = _ftshare_runpy_path()
    if run_py is None:
        return {}

    env = os.environ.copy()
    verify_path = _requests_verify()
    if isinstance(verify_path, str):
        env.setdefault("SSL_CERT_FILE", verify_path)

    symbol = _stock_symbol_with_suffix(code)
    try:
        proc = subprocess.run(
            ["python3", str(run_py), "stock-security-info", "--symbol", symbol],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(f"[{code}] FTShare stock-security-info 执行失败: {e}")
        return {}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        logger.warning(f"[{code}] FTShare stock-security-info 返回失败: {' '.join(err)}")
        return {}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        logger.warning(f"[{code}] FTShare stock-security-info JSON 解析失败: {e}")
        return {}

    latest_price = _to_float(data.get("close"))
    result: dict = {
        "ftshare_symbol": data.get("symbol"),
        "ftshare_symbol_name": data.get("symbol_name"),
        "ftshare_ts_nanos": data.get("ts_nanos"),
    }
    if latest_price is not None:
        result.update(
            {
                "latest_price": latest_price,
                "latest_price_source": "ftshare_stock_security_info",
            }
        )

    field_map = {
        "market_cap": "market_cap",
        "float_a_market_cap": "float_market_cap",
        "shares": "total_shares",
        "float_a_shares": "float_shares",
        "pe_ttm": "pe_ttm",
        "pb": "pb",
        "roe_ttm": "roe_ttm",
        "eps_ttm": "eps_ttm",
        "bvps": "bvps",
    }
    for src, dst in field_map.items():
        value = _to_float(data.get(src))
        if value is not None:
            result[dst] = value

    if "market_cap" in result or "total_shares" in result:
        result["market_info_source"] = "ftshare_stock_security_info"
    if "pe_ttm" in result:
        result["pe_source"] = "ftshare_stock_security_info"
    if "pb" in result:
        result["pb_source"] = "ftshare_stock_security_info"
    return result


def _fetch_realtime_price_sina(code: str) -> dict:
    """
    从新浪单股行情抓取未复权最新价。

    Returns:
        含 latest_price/latest_price_time 的字典（失败返回空字典）
    """
    symbol = _exchange_symbol_sina(code)
    url = f"https://hq.sinajs.cn/list={symbol}"
    try:
        resp = requests.get(
            url,
            headers={
                "Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=10,
            verify=_requests_verify(),
        )
        resp.raise_for_status()
        payload = resp.text.split("=", 1)[1].strip().strip('";')
        fields = payload.split(",")
        if len(fields) < 32 or not fields[0]:
            return {}
        current = _to_float(fields[3])
        prev_close = _to_float(fields[2])
        latest_price = current if current and current > 0 else prev_close
        if latest_price is None:
            return {}
        return {
            "latest_price": latest_price,
            "latest_price_time": f"{fields[30]} {fields[31]}",
            "latest_price_source": "sina_realtime",
        }
    except (requests.RequestException, IndexError) as e:
        logger.warning(f"[{code}] 新浪实时价格抓取失败: {e}")
        return {}


def _fetch_realtime_quote_tx(code: str) -> dict:
    """
    从腾讯实时行情抓取最新价、市值、股本、PE/PB 等字段。

    Returns:
        含 latest_price/market_cap/total_shares 等字段的字典（失败返回空字典）
    """
    symbol = _exchange_symbol_tx(code)
    url = f"https://qt.gtimg.cn/q={symbol}"
    try:
        resp = requests.get(
            url,
            headers={
                "Referer": "https://stockapp.finance.qq.com/",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=10,
            verify=_requests_verify(),
        )
        resp.encoding = "gbk"
        resp.raise_for_status()
        if '="' not in resp.text:
            return {}
        fields = resp.text.split('="', 1)[1].rstrip('";\n').split("~")
        if len(fields) < 74 or fields[2] != code:
            return {}

        latest_price = _to_float(fields[3])
        float_market_cap_yi = _to_float(fields[44])
        total_market_cap_yi = _to_float(fields[45])
        result: dict = {
            "quote_source_tx": "tencent_realtime",
            "quote_time_tx": fields[30],
        }
        if latest_price is not None:
            result.update(
                {
                    "latest_price": latest_price,
                    "latest_price_time": fields[30],
                    "latest_price_source": "tencent_realtime",
                }
            )
        if total_market_cap_yi is not None:
            result["market_cap"] = total_market_cap_yi * 1e8
        if float_market_cap_yi is not None:
            result["float_market_cap"] = float_market_cap_yi * 1e8

        pe_dynamic = _to_float(fields[39])
        pb = _to_float(fields[46])
        pe_ttm = _to_float(fields[53])
        if pe_ttm is not None:
            result["pe_ttm"] = pe_ttm
            result["pe_source"] = "tencent_realtime"
        elif pe_dynamic is not None:
            result["pe_ttm"] = pe_dynamic
            result["pe_source"] = "tencent_realtime_dynamic"
        if pb is not None:
            result["pb"] = pb
            result["pb_source"] = "tencent_realtime"

        float_shares = _to_float(fields[72])
        total_shares = _to_float(fields[73])
        if total_shares is not None:
            result["total_shares"] = total_shares
        if float_shares is not None:
            result["float_shares"] = float_shares

        if "market_cap" in result or "total_shares" in result:
            result["market_info_source"] = "tencent_realtime"
        return result
    except (requests.RequestException, IndexError) as e:
        logger.warning(f"[{code}] 腾讯实时行情抓取失败: {e}")
        return {}


def _fetch_em_spot_cache() -> dict[str, dict]:
    """
    抓取东方财富全市场实时行情并缓存到进程内。

    Returns:
        code -> 行情字段字典
    """
    global _EM_SPOT_CACHE
    if _EM_SPOT_CACHE is not None:
        return _EM_SPOT_CACHE

    rows_by_code: dict[str, dict] = {}
    page_size = 100
    total_pages = 60
    for page in range(1, total_pages + 1):
        try:
            resp = requests.get(
                "https://push2.eastmoney.com/api/qt/clist/get",
                params={
                    "pn": str(page),
                    "pz": str(page_size),
                    "po": "1",
                    "np": "1",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f3",
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                    "fields": "f12,f14,f2,f3,f8,f9,f20,f21,f23,f100",
                },
                headers={
                    "Referer": "https://quote.eastmoney.com/",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=12,
                verify=_requests_verify(),
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            rows = data.get("diff") or []
            if not rows:
                break
            for row in rows:
                code = str(row.get("f12") or "")
                if code:
                    rows_by_code[code] = row
            total = int(data.get("total") or 0)
            if page * page_size >= total:
                break
            time.sleep(0.1)
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"东方财富全市场实时行情第 {page} 页失败: {e}")
            break

    _EM_SPOT_CACHE = rows_by_code
    return rows_by_code


def _fetch_market_cap_info_em_spot(code: str) -> dict:
    """
    从东方财富全市场实时行情表提取市值、行业、PE/PB。

    Returns:
        含 market_cap/float_market_cap/sector_em 的字典（失败返回空字典）
    """
    row = _fetch_em_spot_cache().get(code)
    if not row:
        return {}
    latest_price = _to_float(row.get("f2"))
    market_cap = _to_float(row.get("f20"))
    float_market_cap = _to_float(row.get("f21"))
    result: dict = {
        "market_info_source": "eastmoney_spot",
        "sector_em": row.get("f100"),
    }
    if latest_price is not None:
        result.update(
            {
                "latest_price": latest_price,
                "latest_price_source": "eastmoney_spot",
            }
        )
    if market_cap is not None:
        result["market_cap"] = market_cap
        if latest_price and latest_price > 0:
            result["total_shares"] = market_cap / latest_price
            result["market_cap_calc"] = "eastmoney_spot_market_cap"
    if float_market_cap is not None:
        result["float_market_cap"] = float_market_cap
        if latest_price and latest_price > 0:
            result["float_shares"] = float_market_cap / latest_price
    pe = _to_float(row.get("f9"))
    pb = _to_float(row.get("f23"))
    if pe is not None:
        result["pe_ttm"] = pe
        result["pe_source"] = "eastmoney_spot"
    if pb is not None:
        result["pb"] = pb
        result["pb_source"] = "eastmoney_spot"
    return result


def _fetch_ak_spot_cache() -> dict[str, dict]:
    """
    通过 AKShare 全市场实时行情接口抓取快照，并缓存到进程内。

    Returns:
        code -> 行情字段字典
    """
    global _AK_SPOT_CACHE
    if _AK_SPOT_CACHE is not None:
        return _AK_SPOT_CACHE

    rows_by_code: dict[str, dict] = {}
    try:
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            for row in df.to_dict("records"):
                row_code = str(row.get("代码") or "")
                if row_code:
                    rows_by_code[row_code] = row
    except Exception as e:
        logger.warning(f"AKShare 全市场实时行情失败: {e}")

    _AK_SPOT_CACHE = rows_by_code
    return rows_by_code


def _fetch_market_cap_info_ak_spot(code: str) -> dict:
    """
    从 AKShare 全市场实时行情表提取市值、行业、PE/PB。

    Returns:
        含 market_cap/float_market_cap/sector_em 的字典（失败返回空字典）
    """
    row = _fetch_ak_spot_cache().get(code)
    if not row:
        return {}
    latest_price = _to_float(row.get("最新价"))
    market_cap = _to_float(row.get("总市值"))
    float_market_cap = _to_float(row.get("流通市值"))
    result: dict = {"market_info_source": "akshare_stock_zh_a_spot_em"}
    if latest_price is not None:
        result.update(
            {
                "latest_price": latest_price,
                "latest_price_source": "akshare_stock_zh_a_spot_em",
            }
        )
    if market_cap is not None:
        result["market_cap"] = market_cap
        if latest_price and latest_price > 0:
            result["total_shares"] = market_cap / latest_price
            result["market_cap_calc"] = "akshare_spot_market_cap"
    if float_market_cap is not None:
        result["float_market_cap"] = float_market_cap
        if latest_price and latest_price > 0:
            result["float_shares"] = float_market_cap / latest_price
    pe = _to_float(row.get("市盈率-动态"))
    pb = _to_float(row.get("市净率"))
    if pe is not None:
        result["pe_ttm"] = pe
        result["pe_source"] = "akshare_stock_zh_a_spot_em"
    if pb is not None:
        result["pb"] = pb
        result["pb_source"] = "akshare_stock_zh_a_spot_em"
    return result


def _fetch_market_cap_info_direct_em(code: str) -> dict:
    """
    直接请求东方财富个股接口，绕过 akshare DataFrame 包装脆弱点。

    Returns:
        含 market_cap/total_shares/sector_em 的字典（失败返回空字典）
    """
    market_code = 1 if code.startswith(("6", "9")) else 0
    try:
        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "fltt": "2",
                "invt": "2",
                "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
                "secid": f"{market_code}.{code}",
            },
            headers={
                "Referer": "https://quote.eastmoney.com/",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=10,
            verify=_requests_verify(),
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        if not data:
            return {}
        return {
            "market_cap": data.get("f116"),
            "float_market_cap": data.get("f117"),
            "total_shares": data.get("f84"),
            "float_shares": data.get("f85"),
            "sector_em": data.get("f127"),
            "market_info_source": "eastmoney_direct",
        }
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"[{code}] 东方财富直接市值接口失败: {e}")
        return {}


def _fetch_market_cap_info(code: str) -> dict:
    """
    从东方财富抓取市值和基本股本信息。

    Returns:
        含 market_cap/total_shares/sector_em 的字典（失败返回空字典）
    """
    sources = [
        _fetch_market_cap_info_direct_em,
        _fetch_realtime_quote_tx,
        _fetch_market_cap_info_em_spot,
        _fetch_market_cap_info_ak_spot,
    ]
    for fetcher in sources:
        info = fetcher(code)
        if info and ("market_cap" in info or "total_shares" in info):
            return info

    try:
        info_df = ak.stock_individual_info_em(symbol=code)
        info = dict(zip(info_df.iloc[:, 0], info_df.iloc[:, 1]))
        return {
            "market_cap": info.get("总市值"),
            "float_market_cap": info.get("流通市值"),
            "total_shares": info.get("总股本"),
            "float_shares": info.get("流通股"),
            "sector_em": info.get("行业"),
            "market_info_source": "stock_individual_info_em",
        }
    except Exception as e:
        logger.warning(f"[{code}] stock_individual_info_em 失败: {e}")
        return {}


def _load_cached_market_info(code: str, output_base: str, current_date: str, latest_price: Optional[float]) -> dict:
    """
    从历史 raw fundamental 中回填静态股本/行业字段，并用最新价重算市值。

    Args:
        code:         6 位股票代码
        output_base:  输出根目录
        current_date: 当前运行日期
        latest_price: 新浪实时最新价
    Returns:
        可回填字段字典，失败返回空字典
    """
    output_root = Path(output_base)
    candidates = sorted(output_root.glob(f"*/raw/{code}_fundamental.json"), reverse=True)
    for path in candidates:
        date_part = path.parts[-3] if len(path.parts) >= 3 else ""
        if date_part >= current_date:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        total_shares = _to_float(cached.get("total_shares"))
        float_shares = _to_float(cached.get("float_shares"))
        if total_shares is None and cached.get("market_cap") is None:
            continue

        result: dict = {
            "total_shares": total_shares,
            "float_shares": float_shares,
            "sector_em": cached.get("sector_em"),
            "market_info_source": "historical_cache",
            "market_info_cache_date": date_part,
        }
        if latest_price is not None and total_shares is not None:
            result["market_cap"] = latest_price * total_shares
            if float_shares is not None:
                result["float_market_cap"] = latest_price * float_shares
            result["market_cap_calc"] = "latest_price_sina * cached_total_shares"
        else:
            result["market_cap"] = cached.get("market_cap")
            result["float_market_cap"] = cached.get("float_market_cap")
            result["market_cap_calc"] = "historical_cache_value"
        return result
    return {}


def _derive_market_info_from_financial(fin_indicators: dict, latest_price: Optional[float]) -> dict:
    """
    用财报净利润 / EPS 推导总股本，再结合最新价计算总市值。

    这是备用计算路径，结果会标记来源，不伪装成直接抓取字段。
    """
    if latest_price is None:
        return {}
    net_profit = _parse_cn_amount(fin_indicators.get("net_profit"))
    eps_basic = _to_float(fin_indicators.get("eps_basic"))
    if net_profit is None or eps_basic is None or eps_basic <= 0:
        return {}
    total_shares = net_profit / eps_basic
    return {
        "total_shares": total_shares,
        "market_cap": latest_price * total_shares,
        "market_info_source": "financial_derived",
        "market_cap_calc": "latest_price_sina * (net_profit / eps_basic)",
    }


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
                result[f"{key}_source"] = "baidu_valuation"
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
            "net_profit": clean(latest.get("净利润")),
            "eps_basic": clean(latest.get("基本每股收益")),
            "report_date": str(latest.get("报告期")),
        }
    except Exception as e:
        logger.warning(f"[{code}] stock_financial_abstract_ths 失败: {e}")
        return {}


def fetch_fundamental(
    code: str,
    output_base: str = "output",
    date: Optional[str] = None,
) -> Optional[dict]:
    """
    抓取单只股票基本面快照：PE、PB、市值、ROE、营收/利润同比增速。

    三个数据源独立抓取，部分失败不阻断；全部失败才返回 None。

    Args:
        code:        6 位股票代码（如 "000001"）
        output_base: 输出根目录，用于历史缓存回填
        date:        当前输出日期（YYYY-MM-DD），None 时取今日
    Returns:
        基本面字典，三个来源全部失败时返回 None
    """
    today = date or datetime.today().strftime("%Y-%m-%d")
    result: dict = {"code": code, "fetch_time": datetime.now().isoformat()}
    errors: list[str] = []
    warnings: list[str] = []

    ftshare_info = _fetch_security_info_ftshare(code)
    if ftshare_info:
        result.update(ftshare_info)

    tx_quote = _fetch_realtime_quote_tx(code)
    if tx_quote:
        for key, value in tx_quote.items():
            result.setdefault(key, value)

    if "latest_price" not in result:
        price_info = _fetch_realtime_price_sina(code)
        if price_info:
            result.update(price_info)

    market_info = ftshare_info if ftshare_info and (
        "market_cap" in ftshare_info or "total_shares" in ftshare_info
    ) else _fetch_market_cap_info(code)
    if market_info:
        for key, value in market_info.items():
            result.setdefault(key, value)
        logger.info(f"[{code}] 市值信息抓取成功")
    else:
        warnings.append("stock_individual_info_em")

    pe_pb = _fetch_pe_pb(code)
    if pe_pb:
        for key, value in pe_pb.items():
            result.setdefault(key, value)
        logger.info(f"[{code}] PE/PB 成功: PE={result.get('pe_ttm')}, PB={result.get('pb')}")
    elif result.get("pe_ttm") is not None or result.get("pb") is not None:
        warnings.append("stock_zh_valuation_baidu")
        logger.info(
            f"[{code}] PE/PB 使用备用实时行情: PE={result.get('pe_ttm')}, PB={result.get('pb')}"
        )
    else:
        errors.append("stock_zh_valuation_baidu")

    fin_indicators = _fetch_financial_indicators(code)
    if fin_indicators:
        result.update(fin_indicators)
        logger.info(f"[{code}] 财务指标抓取成功")
    else:
        errors.append("stock_financial_abstract_ths")

    if not market_info:
        latest_price = _to_float(result.get("latest_price"))
        cached_market = _load_cached_market_info(code, output_base, today, latest_price)
        if cached_market:
            result.update(cached_market)
            logger.info(f"[{code}] 市值信息使用历史缓存回填")
        elif fin_indicators:
            derived_market = _derive_market_info_from_financial(fin_indicators, latest_price)
            if derived_market:
                result.update(derived_market)
                logger.info(f"[{code}] 市值信息使用财报字段推导")
            else:
                errors.append("stock_individual_info_em")
        else:
            errors.append("stock_individual_info_em")

    if warnings:
        result["fetch_warnings"] = warnings
    if errors:
        result["fetch_errors"] = errors

    if len(errors) == 3:
        logger.error(f"[{code}] 三个基本面数据源全部失败")
        return None

    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def fetch_stock_data(stock: dict, output_dir: Path, cache_dir: Path, output_base: str = "output", date: Optional[str] = None) -> dict:
    """
    抓取单只股票完整数据（OHLCV + 基本面）并保存到输出目录。

    Args:
        stock:      watchlist.json 中单只股票字典（含 code/name/sector/note）
        output_dir:  原始数据输出目录（如 output/2024-01-15/raw/）
        cache_dir:   OHLCV 缓存目录（output/cache/）
        output_base: 输出根目录，用于基本面历史缓存回填
        date:        当前输出日期
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

    fundamental = fetch_fundamental(code, output_base=output_base, date=date)
    if fundamental is not None:
        fundamental["stock_name"] = name
        fundamental["sector"] = stock.get("sector", "")
        fundamental["note"] = stock.get("note", "")
        fund_path = output_dir / f"{code}_fundamental.json"
        with open(fund_path, "w", encoding="utf-8") as f:
            json.dump(fundamental, f, ensure_ascii=False, indent=2, default=str)
        status["fundamental_status"] = "partial" if fundamental.get("fetch_errors") else "success"
        status["fundamental_path"] = str(fund_path)
        logger.info(f"[{code}] 基本面已保存: {fund_path}")

    ohlcv_ok = status["ohlcv_status"] == "success"
    fund_ok = status["fundamental_status"] in {"success", "partial"}
    if ohlcv_ok and status["fundamental_status"] == "success":
        status["data_quality"] = "complete"
    elif ohlcv_ok or fund_ok:
        status["data_quality"] = "partial"

    return status


def run_data_fetch(
    watchlist_path: str = "watchlist.json",
    output_base: str = "output",
    date: Optional[str] = None,
) -> list[dict]:
    """
    主入口：读取 watchlist，顺序抓取所有股票数据，保存到当日输出目录。

    Args:
        watchlist_path: watchlist.json 路径
        output_base:    输出根目录
        date:           输出日期（YYYY-MM-DD），None 时取今日
    Returns:
        所有股票的抓取状态列表
    """
    today = date or datetime.today().strftime("%Y-%m-%d")
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
        status = fetch_stock_data(stock, raw_dir, cache_dir, output_base=output_base, date=today)
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
