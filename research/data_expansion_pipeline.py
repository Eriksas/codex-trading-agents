"""
data_expansion_pipeline.py - expanded historical data pipeline for V3 validation.

优先尝试 Fuyao 和 Tushare，失败时降级到本地缓存。输出统一长表、按股票拆分
的回测缓存、动态 universe、数据质量报告和缺失数据报告。本模块不修改主策略、
不连接实盘、不自动下单。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import factor_research_round3 as r3
import market_scanner as scanner

DEFAULT_OUTPUT_DIR = Path("output/data_expansion")
DEFAULT_EXPANDED_DIR = Path("data/expanded")
DEFAULT_START = "2024-01-01"
DEFAULT_EXTENDED_START = "2021-01-01"
INDEX_CODES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}
SOURCE_PRIORITY = {"fuyao": 0, "tushare": 1, "efinance": 2, "baostock": 3, "akshare": 4, "local_cache": 5}
REQUIRED_DAILY_COLUMNS = [
    "trade_date",
    "ts_code",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "vol",
    "amount",
]
REQUIRED_BASIC_COLUMNS = ["trade_date", "ts_code", "turnover_rate", "total_mv", "circ_mv", "pe", "pb", "volume_ratio"]
DAILY_UNIVERSE_COLUMNS = [
    "trade_date",
    "total_candidates_before_filter",
    "after_history_filter",
    "after_price_filter",
    "after_amount_filter",
    "after_limit_filter",
    "after_st_filter",
    "final_universe_count",
    "data_missing",
    "history_length_insufficient",
    "price_filter",
    "amount_filter",
    "limit_filter",
    "st_or_suspended_filter",
    "other_filter",
    "exclusion_reason_summary",
]


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    """写 CSV 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=fr._json_default)


def _df_to_markdown(df: pd.DataFrame, max_rows: int = 30) -> str:
    """不依赖 tabulate 的简易 Markdown 表格。"""
    if df.empty:
        return ""
    sample = df.head(max_rows).fillna("")
    columns = [str(col) for col in sample.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for _, row in sample.iterrows():
        values = [str(row.get(col, ""))[:160].replace("\n", " ") for col in sample.columns]
        lines.append("| " + " | ".join(values) + " |")
    if len(df) > max_rows:
        lines.append(f"\n仅展示前 {max_rows} 行，共 {len(df)} 行。")
    return "\n".join(lines)


def _safe_float(value: Any) -> Optional[float]:
    """安全转换 float。"""
    return fr._safe_float(value)


def _dash_date(value: Any) -> str:
    """统一日期为 YYYY-MM-DD。"""
    if value is None or value == "":
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return ""
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return pd.to_datetime(text).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return text


def _compact_date(value: str) -> str:
    """YYYY-MM-DD 转 YYYYMMDD。"""
    return value.replace("-", "")


def _date_ms(value: str) -> int:
    """日期转毫秒时间戳。"""
    return int(datetime.strptime(value, "%Y-%m-%d").timestamp() * 1000)


def _cache_key(symbol: str) -> str:
    """生成 per-symbol 缓存文件名。"""
    return "".join(ch if ch.isalnum() else "_" for ch in str(symbol))


def _symbol_from_cache_path(path: Path) -> str:
    """从缓存文件名恢复股票代码。"""
    stem = path.stem
    if "_" in stem:
        code, suffix = stem.rsplit("_", 1)
        return f"{code}.{suffix}"
    return stem


def _market_from_symbol(symbol: str) -> str:
    """根据代码后缀返回市场。"""
    if symbol.endswith(".SH"):
        return "SH"
    if symbol.endswith(".SZ"):
        return "SZ"
    if symbol.endswith(".BJ"):
        return "BJ"
    return ""


def _baostock_code_to_ts_code(code: Any) -> str:
    """BaoStock 代码 sh.600000 转为 600000.SH。"""
    text = str(code or "").strip()
    if "." not in text:
        return text
    market, raw = text.split(".", 1)
    suffix = market.upper()
    return f"{raw}.{suffix}"


def _ts_code_to_baostock_code(symbol: Any) -> str:
    """标准 ts_code 转 BaoStock 代码。"""
    text = str(symbol or "").strip()
    if "." not in text:
        return text
    raw, suffix = text.split(".", 1)
    return f"{suffix.lower()}.{raw}"


def _plain_code_to_ts_code(code: Any) -> str:
    """AKShare 纯 6 位代码转标准 ts_code。"""
    raw = str(code or "").strip().zfill(6)
    if raw.startswith(("6", "9")):
        return f"{raw}.SH"
    if raw.startswith(("8", "4")):
        return f"{raw}.BJ"
    return f"{raw}.SZ"


def _ts_code_to_plain_code(symbol: Any) -> str:
    """标准 ts_code 转 AKShare 纯代码。"""
    return str(symbol or "").split(".", 1)[0].zfill(6)


def _normalize_daily_frame(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """标准化日线长表。"""
    if df.empty:
        return pd.DataFrame(columns=[*REQUIRED_DAILY_COLUMNS, "source", "source_priority"])
    work = df.copy()
    if "ts_code" in work.columns:
        work = work.drop(columns=[col for col in ["symbol", "code", "股票代码"] if col in work.columns], errors="ignore")
    if "trade_date" in work.columns:
        work = work.drop(columns=[col for col in ["date", "日期"] if col in work.columns], errors="ignore")
    rename_map = {
        "date": "trade_date",
        "日期": "trade_date",
        "symbol": "ts_code",
        "code": "ts_code",
        "股票代码": "ts_code",
        "volume": "vol",
        "成交量": "vol",
        "turnover": "amount",
        "成交额": "amount",
        "change_rate": "pct_chg",
        "涨跌幅": "pct_chg",
        "pctChg": "pct_chg",
        "preclose": "pre_close",
        "pre_close": "pre_close",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
    }
    work = work.rename(columns=rename_map)
    if "trade_date" in work.columns:
        work["trade_date"] = work["trade_date"].map(_dash_date)
    if "ts_code" in work.columns:
        work["ts_code"] = work["ts_code"].astype(str)
    for col in ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]:
        if col not in work.columns:
            work[col] = np.nan
        work[col] = pd.to_numeric(work[col], errors="coerce")
    if work["pct_chg"].dropna().abs().max() is not None and work["pct_chg"].dropna().abs().max() > 1.5:
        work["pct_chg"] = work["pct_chg"] / 100.0
    work["source"] = source
    work["source_priority"] = SOURCE_PRIORITY.get(source, 99)
    return work[[*REQUIRED_DAILY_COLUMNS, "source", "source_priority"]]


def _normalize_basic_frame(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """标准化 daily_basic 长表。"""
    if df.empty:
        return pd.DataFrame(columns=[*REQUIRED_BASIC_COLUMNS, "source", "source_priority"])
    work = df.copy()
    if "trade_date" in work.columns:
        work["trade_date"] = work["trade_date"].map(_dash_date)
    if "ts_code" in work.columns:
        work["ts_code"] = work["ts_code"].astype(str)
    for col in REQUIRED_BASIC_COLUMNS:
        if col not in work.columns:
            work[col] = np.nan
    for col in ["turnover_rate", "total_mv", "circ_mv", "pe", "pb", "volume_ratio"]:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work["source"] = source
    work["source_priority"] = SOURCE_PRIORITY.get(source, 99)
    return work[[*REQUIRED_BASIC_COLUMNS, "source", "source_priority"]]


def _merge_by_priority(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """按数据源优先级合并去重。"""
    if df.empty:
        return df.copy()
    df = df.loc[:, ~df.columns.duplicated()].copy()
    work = df.sort_values([*keys, "source_priority"]).copy()
    return work.drop_duplicates(keys, keep="first").reset_index(drop=True)


def _concat_frames(frames: list[pd.DataFrame], columns: Optional[list[str]] = None) -> pd.DataFrame:
    """合并多来源表，先清理重复列，避免不同接口字段重名导致 concat 失败。"""
    cleaned = [df.loc[:, ~df.columns.duplicated()].copy() for df in frames if not df.empty]
    if not cleaned:
        return pd.DataFrame(columns=columns or [])
    return pd.concat(cleaned, ignore_index=True)


@contextmanager
def _time_limit(seconds: Optional[int]) -> Any:
    """给阻塞式网络调用设置软超时。"""
    if not seconds or seconds <= 0:
        yield
        return

    def _handle_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"query timed out after {seconds}s")

    old_handler = signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _filter_date_range(df: pd.DataFrame, start: str, end: str, date_col: str = "trade_date") -> pd.DataFrame:
    """按请求日期过滤，避免本地缓存或快照把未来数据混入历史回测窗口。"""
    if df.empty or date_col not in df.columns:
        return df
    work = df.copy()
    work[date_col] = work[date_col].map(_dash_date)
    return work[(work[date_col] >= start) & (work[date_col] <= end)].reset_index(drop=True)


def _drop_incomplete_ohlc_rows(df: pd.DataFrame) -> pd.DataFrame:
    """剔除不适合进入历史回测的盘中或缺失 OHLC 行。"""
    if df.empty:
        return df
    required = ["trade_date", "ts_code", "open", "high", "low", "close"]
    if any(col not in df.columns for col in required):
        return df
    work = df.copy()
    mask = work["trade_date"].notna() & work["ts_code"].notna()
    for col in ["open", "high", "low", "close"]:
        mask &= pd.to_numeric(work[col], errors="coerce").notna()
    return work.loc[mask].reset_index(drop=True)


def _local_daily_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """读取本地缓存作为降级数据源。"""
    histories = fr.load_cached_histories(cache_dir=fr.DEFAULT_CACHE_DIR, min_bars=1)
    daily_rows: list[dict[str, Any]] = []
    stock_rows: list[dict[str, Any]] = []
    metadata = r3._load_scan_metadata()
    for symbol, bars in histories.items():
        symbol_meta = metadata.get(symbol) or {}
        sorted_bars = sorted(bars, key=lambda item: str(item.get("date") or ""))
        first_date = str(sorted_bars[0].get("date") or "") if sorted_bars else ""
        name = symbol_meta.get("name") or symbol
        stock_rows.append(
            {
                "ts_code": symbol,
                "name": name,
                "list_date": first_date,
                "delist_date": "",
                "market": _market_from_symbol(symbol),
                "is_st": bool("ST" in str(name).upper()),
                "source": "local_cache",
            }
        )
        prev_close: Optional[float] = None
        for bar in sorted_bars:
            trade_date = str(bar.get("date") or "")
            close = _safe_float(bar.get("close"))
            pct_chg = close / prev_close - 1 if close is not None and prev_close else np.nan
            daily_rows.append(
                {
                    "trade_date": trade_date,
                    "ts_code": symbol,
                    "open": _safe_float(bar.get("open")),
                    "high": _safe_float(bar.get("high")),
                    "low": _safe_float(bar.get("low")),
                    "close": close,
                    "pre_close": prev_close,
                    "pct_chg": pct_chg,
                    "vol": _safe_float(bar.get("volume")),
                    "amount": _safe_float(bar.get("turnover")),
                    "source": "local_cache",
                }
            )
            if close is not None:
                prev_close = close

    index_rows: list[dict[str, Any]] = []
    for path in sorted(fr.DEFAULT_INDEX_CACHE_DIR.glob("*.csv")) if fr.DEFAULT_INDEX_CACHE_DIR.exists() else []:
        symbol = _symbol_from_cache_path(path)
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            prev_close = None
            for row in csv.DictReader(f):
                close = _safe_float(row.get("close"))
                pct_chg = close / prev_close - 1 if close is not None and prev_close else np.nan
                index_rows.append(
                    {
                        "trade_date": _dash_date(row.get("date")),
                        "ts_code": symbol,
                        "name": INDEX_CODES.get(symbol, symbol),
                        "open": _safe_float(row.get("open")),
                        "high": _safe_float(row.get("high")),
                        "low": _safe_float(row.get("low")),
                        "close": close,
                        "pre_close": prev_close,
                        "pct_chg": pct_chg,
                        "vol": _safe_float(row.get("volume")),
                        "amount": _safe_float(row.get("turnover")),
                        "source": "local_cache",
                    }
                )
                if close is not None:
                    prev_close = close

    daily = _normalize_daily_frame(pd.DataFrame(daily_rows), "local_cache")
    stock_basic = pd.DataFrame(stock_rows)
    if stock_basic.empty:
        stock_basic = pd.DataFrame(columns=["ts_code", "name", "list_date", "delist_date", "market", "is_st", "source"])
    basic = _normalize_basic_frame(
        pd.DataFrame(
            {
                "trade_date": daily["trade_date"] if not daily.empty else [],
                "ts_code": daily["ts_code"] if not daily.empty else [],
            }
        ),
        "local_cache",
    )
    index_daily = _normalize_daily_frame(pd.DataFrame(index_rows), "local_cache")
    if not index_daily.empty:
        name_map = {row["ts_code"]: row["name"] for row in index_rows if row.get("ts_code")}
        index_daily["name"] = index_daily["ts_code"].map(name_map).fillna(index_daily["ts_code"])
    return daily, basic, stock_basic, index_daily


def _fuyao_frames(start: str, end: str, max_symbols: Optional[int], provider_events: list[dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """尝试读取 Fuyao 数据。"""
    if not scanner._fuyao_api_key():
        provider_events.append({"source": "fuyao", "stage": "auth", "status": "skipped", "reason": "missing FUYAO_API_KEY"})
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    stock_rows: list[dict[str, Any]] = []
    try:
        ticker_map = scanner._fetch_fuyao_ticker_map()
        for symbol, item in ticker_map.items():
            name = item.get("name") or item.get("sec_name") or symbol
            stock_rows.append(
                {
                    "ts_code": symbol,
                    "name": name,
                    "list_date": _dash_date(item.get("listed_date") or item.get("list_date")),
                    "delist_date": _dash_date(item.get("delisted_date") or item.get("delist_date")),
                    "market": _market_from_symbol(symbol),
                    "is_st": bool("ST" in str(name).upper()),
                    "source": "fuyao",
                }
            )
        provider_events.append({"source": "fuyao", "stage": "stock_basic", "status": "success", "rows": len(stock_rows)})
    except Exception as exc:
        provider_events.append({"source": "fuyao", "stage": "stock_basic", "status": "failed", "reason": str(exc)})

    daily_rows: list[dict[str, Any]] = []
    today = datetime.now().strftime("%Y-%m-%d")
    if end >= today:
        try:
            snapshots, _timestamp = scanner._fetch_fuyao_snapshots(page_size=1000, max_pages=8)
            for item in snapshots:
                symbol = item.get("thscode")
                close = _safe_float(item.get("latest") or item.get("close_price"))
                pct_chg = _safe_float(item.get("change_rate") or item.get("pct_chg"))
                pre_close = None
                if close is not None and pct_chg is not None and pct_chg > -1:
                    pre_close = close / (1 + pct_chg)
                daily_rows.append(
                    {
                        "trade_date": end,
                        "ts_code": symbol,
                        "open": _safe_float(item.get("open_price")),
                        "high": _safe_float(item.get("high_price")),
                        "low": _safe_float(item.get("low_price")),
                        "close": close,
                        "pre_close": pre_close,
                        "pct_chg": pct_chg,
                        "vol": _safe_float(item.get("volume")),
                        "amount": _safe_float(item.get("turnover")),
                        "source": "fuyao",
                    }
                )
            provider_events.append({"source": "fuyao", "stage": "snapshot", "status": "success", "rows": len(daily_rows)})
        except Exception as exc:
            provider_events.append({"source": "fuyao", "stage": "snapshot", "status": "failed", "reason": str(exc)})
    else:
        provider_events.append({"source": "fuyao", "stage": "snapshot", "status": "skipped", "reason": "snapshot_is_current_only"})

    # Fuyao 当前公开封装只有单票历史接口。为避免 5000+ 逐票循环，默认只在
    # 调试限制 max_symbols 存在时补充少量历史；正式全市场历史优先交给 Tushare 按交易日拉取。
    if max_symbols and stock_rows:
        start_ms = _date_ms(start)
        end_ms = _date_ms(end)
        historical_rows_before = len(daily_rows)
        for item in stock_rows[:max_symbols]:
            symbol = str(item["ts_code"])
            try:
                bars = scanner._fetch_fuyao_bars(
                    "/api/a-share/prices/historical",
                    {"thscode": symbol, "interval": "1d", "start": start_ms, "end": end_ms, "adjust": "none"},
                )
                for bar in bars:
                    close = _safe_float(bar.get("close"))
                    pre_close = None
                    daily_rows.append(
                        {
                            "trade_date": bar.get("date"),
                            "ts_code": symbol,
                            "open": _safe_float(bar.get("open")),
                            "high": _safe_float(bar.get("high")),
                            "low": _safe_float(bar.get("low")),
                            "close": close,
                            "pre_close": pre_close,
                            "pct_chg": np.nan,
                            "vol": _safe_float(bar.get("volume")),
                            "amount": _safe_float(bar.get("turnover")),
                            "source": "fuyao",
                        }
                    )
            except Exception as exc:
                provider_events.append({"source": "fuyao", "stage": "historical", "status": "failed", "ts_code": symbol, "reason": str(exc)})
        provider_events.append({"source": "fuyao", "stage": "historical", "status": "success", "rows": len(daily_rows) - historical_rows_before})
    else:
        provider_events.append(
            {
                "source": "fuyao",
                "stage": "historical_daily",
                "status": "skipped",
                "reason": "bulk_daily_endpoint_not_configured; per_symbol_full_market_fetch_disabled",
            }
        )

    index_daily = pd.DataFrame()
    try:
        index_rows = []
        start_ms = _date_ms(start)
        end_ms = _date_ms(end)
        for symbol, name in INDEX_CODES.items():
            bars = scanner._fetch_fuyao_bars(
                "/api/a-share-index/prices/historical",
                {"thscode": symbol, "interval": "1d", "start": start_ms, "end": end_ms},
            )
            prev_close = None
            for bar in bars:
                close = _safe_float(bar.get("close"))
                pct_chg = close / prev_close - 1 if close is not None and prev_close else np.nan
                index_rows.append({**bar, "trade_date": bar.get("date"), "ts_code": symbol, "name": name, "pre_close": prev_close, "pct_chg": pct_chg, "source": "fuyao"})
                if close is not None:
                    prev_close = close
        index_daily = _normalize_daily_frame(pd.DataFrame(index_rows), "fuyao")
        if not index_daily.empty:
            index_daily["name"] = index_daily["ts_code"].map(INDEX_CODES)
        provider_events.append({"source": "fuyao", "stage": "index_daily", "status": "success", "rows": len(index_daily)})
    except Exception as exc:
        provider_events.append({"source": "fuyao", "stage": "index_daily", "status": "failed", "reason": str(exc)})

    return _normalize_daily_frame(pd.DataFrame(daily_rows), "fuyao"), pd.DataFrame(), pd.DataFrame(stock_rows), index_daily


def _tushare_query(pro: Any, api_name: str, provider_events: list[dict[str, Any]], retries: int, sleep_seconds: float, **params: Any) -> pd.DataFrame:
    """带重试的 Tushare 查询。"""
    for attempt in range(retries + 1):
        try:
            df = getattr(pro, api_name)(**params)
            time.sleep(sleep_seconds)
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        except Exception as exc:
            reason = str(exc)
            provider_events.append({"source": "tushare", "stage": api_name, "status": "retry" if attempt < retries else "failed", "attempt": attempt + 1, "reason": reason})
            if attempt >= retries:
                return pd.DataFrame()
            time.sleep(max(sleep_seconds * (attempt + 2), 1.0))
    return pd.DataFrame()


def _tushare_frames(
    start: str,
    end: str,
    provider_events: list[dict[str, Any]],
    max_trade_dates: Optional[int],
    retries: int,
    sleep_seconds: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """按 trade_date 拉取 Tushare 全市场数据。"""
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        provider_events.append({"source": "tushare", "stage": "auth", "status": "skipped", "reason": "missing TUSHARE_TOKEN"})
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    try:
        import tushare as ts  # type: ignore
    except ImportError as exc:
        provider_events.append({"source": "tushare", "stage": "import", "status": "failed", "reason": str(exc)})
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    pro = ts.pro_api(token)
    start_raw = _compact_date(start)
    end_raw = _compact_date(end)
    stock_frames = []
    for status in ["L", "D", "P"]:
        stock_frames.append(
            _tushare_query(
                pro,
                "stock_basic",
                provider_events,
                retries,
                sleep_seconds,
                exchange="",
                list_status=status,
                fields="ts_code,symbol,name,area,industry,market,list_date,delist_date,list_status",
            )
        )
    stock_basic = _concat_frames(stock_frames)
    if not stock_basic.empty:
        stock_basic["list_date"] = stock_basic["list_date"].map(_dash_date)
        stock_basic["delist_date"] = stock_basic["delist_date"].map(_dash_date)
        stock_basic["market"] = stock_basic.get("market", "")
        stock_basic["is_st"] = stock_basic["name"].astype(str).str.upper().str.contains("ST", na=False)
        stock_basic["source"] = "tushare"
    provider_events.append({"source": "tushare", "stage": "stock_basic", "status": "success" if not stock_basic.empty else "empty", "rows": len(stock_basic)})

    trade_calendar = _tushare_query(
        pro,
        "trade_cal",
        provider_events,
        retries,
        sleep_seconds,
        exchange="SSE",
        start_date=start_raw,
        end_date=end_raw,
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    if not trade_calendar.empty:
        trade_calendar["trade_date"] = trade_calendar["cal_date"].map(_dash_date)
        trade_calendar["is_open"] = pd.to_numeric(trade_calendar["is_open"], errors="coerce").fillna(0).astype(int)
    open_dates = []
    if not trade_calendar.empty:
        open_dates = sorted(trade_calendar.loc[trade_calendar["is_open"] == 1, "cal_date"].astype(str).tolist())
    if max_trade_dates:
        open_dates = open_dates[-max_trade_dates:]
    provider_events.append({"source": "tushare", "stage": "trade_calendar", "status": "success" if open_dates else "empty", "open_dates": len(open_dates)})

    daily_frames: list[pd.DataFrame] = []
    basic_frames: list[pd.DataFrame] = []
    for trade_date in open_dates:
        daily = _tushare_query(pro, "daily", provider_events, retries, sleep_seconds, trade_date=trade_date)
        if not daily.empty:
            daily_frames.append(daily)
        basic = _tushare_query(
            pro,
            "daily_basic",
            provider_events,
            retries,
            sleep_seconds,
            trade_date=trade_date,
            fields="ts_code,trade_date,turnover_rate,total_mv,circ_mv,pe,pb,volume_ratio",
        )
        if not basic.empty:
            basic_frames.append(basic)

    daily_kline = _concat_frames(daily_frames)
    if not daily_kline.empty:
        daily_kline["trade_date"] = daily_kline["trade_date"].map(_dash_date)
        # Tushare amount 单位通常为千元，这里统一成元，便于与本地缓存和成交额过滤一致。
        daily_kline["amount"] = pd.to_numeric(daily_kline["amount"], errors="coerce") * 1000.0
    daily_basic = _concat_frames(basic_frames)
    provider_events.append({"source": "tushare", "stage": "daily", "status": "success" if not daily_kline.empty else "empty", "rows": len(daily_kline)})
    provider_events.append({"source": "tushare", "stage": "daily_basic", "status": "success" if not daily_basic.empty else "empty", "rows": len(daily_basic)})

    index_frames = []
    for symbol, name in INDEX_CODES.items():
        index_df = _tushare_query(pro, "index_daily", provider_events, retries, sleep_seconds, ts_code=symbol, start_date=start_raw, end_date=end_raw)
        if not index_df.empty:
            index_df["name"] = name
            index_frames.append(index_df)
    index_daily = _concat_frames(index_frames)
    if not index_daily.empty:
        index_daily["trade_date"] = index_daily["trade_date"].map(_dash_date)
        index_daily["amount"] = pd.to_numeric(index_daily.get("amount"), errors="coerce") * 1000.0
    provider_events.append({"source": "tushare", "stage": "index_daily", "status": "success" if not index_daily.empty else "empty", "rows": len(index_daily)})

    return (
        _normalize_daily_frame(daily_kline, "tushare"),
        _normalize_basic_frame(daily_basic, "tushare"),
        stock_basic,
        trade_calendar,
        _normalize_daily_frame(index_daily, "tushare").assign(name=index_daily.get("name") if not index_daily.empty else np.nan),
    )


def _baostock_result_to_frame(result: Any) -> pd.DataFrame:
    """把 BaoStock ResultData 转为 DataFrame。"""
    rows: list[list[str]] = []
    while result.next():
        rows.append(result.get_row_data())
    return pd.DataFrame(rows, columns=result.fields)


def _baostock_query_frame(
    bs: Any,
    api_name: str,
    provider_events: list[dict[str, Any]],
    timeout_seconds: Optional[int] = None,
    **params: Any,
) -> pd.DataFrame:
    """查询 BaoStock 并转换为 DataFrame。"""
    try:
        with _time_limit(timeout_seconds):
            result = getattr(bs, api_name)(**params)
    except TimeoutError as exc:
        provider_events.append({"source": "baostock", "stage": api_name, "status": "timeout", "reason": str(exc), "code": params.get("code")})
        return pd.DataFrame()
    except Exception as exc:
        provider_events.append({"source": "baostock", "stage": api_name, "status": "failed", "reason": str(exc), "code": params.get("code")})
        return pd.DataFrame()
    if getattr(result, "error_code", "0") != "0":
        provider_events.append({"source": "baostock", "stage": api_name, "status": "failed", "reason": getattr(result, "error_msg", ""), "code": params.get("code")})
        return pd.DataFrame()
    return _baostock_result_to_frame(result)


def _baostock_frames(
    start: str,
    end: str,
    provider_events: list[dict[str, Any]],
    max_symbols: Optional[int],
    sleep_seconds: float,
    checkpoint_dir: Optional[Path] = None,
    query_timeout_seconds: Optional[int] = 20,
    fetch_daily: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """使用 BaoStock 免费源补充历史日线、交易日、行业和指数。"""
    try:
        import baostock as bs  # type: ignore
    except ImportError as exc:
        provider_events.append({"source": "baostock", "stage": "import", "status": "skipped", "reason": str(exc)})
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        provider_events.append({"source": "baostock", "stage": "auth", "status": "failed", "reason": getattr(login, "error_msg", "")})
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    try:
        raw_basic = _baostock_query_frame(bs, "query_stock_basic", provider_events, timeout_seconds=query_timeout_seconds)
        stock_basic = pd.DataFrame(columns=["ts_code", "name", "list_date", "delist_date", "market", "industry", "is_st", "source"])
        if not raw_basic.empty:
            work = raw_basic.copy()
            work = work[work["code"].astype(str).str.startswith(("sh.", "sz.", "bj."))]
            if "type" in work.columns:
                work = work[work["type"].astype(str) == "1"]
            stock_basic = pd.DataFrame(
                {
                    "ts_code": work["code"].map(_baostock_code_to_ts_code),
                    "name": work.get("code_name", "").astype(str),
                    "list_date": work.get("ipoDate", "").map(_dash_date),
                    "delist_date": work.get("outDate", "").map(_dash_date),
                    "market": work["code"].map(lambda value: _market_from_symbol(_baostock_code_to_ts_code(value))),
                    "industry": "",
                    "is_st": work.get("code_name", "").astype(str).str.upper().str.contains("ST", na=False),
                    "source": "baostock",
                }
            )
        provider_events.append({"source": "baostock", "stage": "stock_basic", "status": "success" if not stock_basic.empty else "empty", "rows": len(stock_basic)})

        industry = _baostock_query_frame(bs, "query_stock_industry", provider_events, timeout_seconds=query_timeout_seconds)
        if not industry.empty and {"code", "industry"}.issubset(industry.columns) and not stock_basic.empty:
            industry_map = industry.assign(ts_code=industry["code"].map(_baostock_code_to_ts_code)).set_index("ts_code")["industry"].to_dict()
            stock_basic["industry"] = stock_basic["ts_code"].map(industry_map).fillna("")
            provider_events.append({"source": "baostock", "stage": "industry", "status": "success", "rows": len(industry_map)})
        else:
            provider_events.append({"source": "baostock", "stage": "industry", "status": "empty", "rows": 0})

        calendar_raw = _baostock_query_frame(bs, "query_trade_dates", provider_events, timeout_seconds=query_timeout_seconds, start_date=start, end_date=end)
        trade_calendar = pd.DataFrame(columns=["trade_date", "is_open"])
        if not calendar_raw.empty:
            trade_calendar = pd.DataFrame(
                {
                    "trade_date": calendar_raw["calendar_date"].map(_dash_date),
                    "is_open": pd.to_numeric(calendar_raw["is_trading_day"], errors="coerce").fillna(0).astype(int),
                }
            )
        provider_events.append({"source": "baostock", "stage": "trade_calendar", "status": "success" if not trade_calendar.empty else "empty", "rows": len(trade_calendar)})

        symbols = stock_basic.sort_values(["list_date", "ts_code"])["ts_code"].dropna().astype(str).tolist() if not stock_basic.empty else []
        symbols = [symbol for symbol in symbols if symbol and not (symbol.endswith(".BJ"))]
        if max_symbols:
            symbols = symbols[:max_symbols]
        daily_frames: list[pd.DataFrame] = []
        basic_frames: list[pd.DataFrame] = []
        failed_symbols = 0
        cache_dir = checkpoint_dir / "baostock_daily" if checkpoint_dir else None
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        fields = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,isST"
        if fetch_daily:
            for idx, symbol in enumerate(symbols, start=1):
                cache_path = cache_dir / f"{_cache_key(symbol)}.csv" if cache_dir else None
                if cache_path and cache_path.exists() and cache_path.stat().st_size > 0:
                    frame = pd.read_csv(cache_path, encoding="utf-8-sig")
                else:
                    frame = _baostock_query_frame(
                        bs,
                        "query_history_k_data_plus",
                        provider_events,
                        timeout_seconds=query_timeout_seconds,
                        code=_ts_code_to_baostock_code(symbol),
                        fields=fields,
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="3",
                    )
                    time.sleep(sleep_seconds)
                    if frame.empty:
                        bs.logout()
                        login = bs.login()
                        if getattr(login, "error_code", "0") != "0":
                            provider_events.append({"source": "baostock", "stage": "reconnect", "status": "failed", "reason": getattr(login, "error_msg", "")})
                        failed_symbols += 1
                        if idx % 250 == 0:
                            print(f"[baostock] daily progress {idx}/{len(symbols)} failed={failed_symbols}", flush=True)
                        continue
                    if cache_path:
                        _write_csv(cache_path, frame)
                if frame.empty:
                    continue
                frame["ts_code"] = symbol
                daily_frames.append(frame)
                basic_frames.append(
                    pd.DataFrame(
                        {
                            "trade_date": frame["date"].map(_dash_date),
                            "ts_code": symbol,
                            "turnover_rate": pd.to_numeric(frame.get("turn"), errors="coerce"),
                        }
                    )
                )
                if idx % 250 == 0:
                    print(f"[baostock] daily progress {idx}/{len(symbols)} failed={failed_symbols}", flush=True)
        else:
            provider_events.append({"source": "baostock", "stage": "daily", "status": "skipped", "reason": "fast_history_provider_available"})
        daily = _concat_frames(daily_frames)
        if not daily.empty:
            daily["trade_date"] = daily["date"].map(_dash_date)
            daily["ts_code"] = daily["code"].map(_baostock_code_to_ts_code).where(daily["code"].notna(), daily.get("ts_code"))
        daily_basic = _normalize_basic_frame(_concat_frames(basic_frames), "baostock")
        provider_events.append({"source": "baostock", "stage": "daily", "status": "success" if not daily.empty else "empty", "rows": len(daily), "symbols": len(symbols), "failed_symbols": failed_symbols})
        provider_events.append({"source": "baostock", "stage": "daily_basic", "status": "success" if not daily_basic.empty else "empty", "rows": len(daily_basic)})

        index_frames: list[pd.DataFrame] = []
        index_fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg"
        for symbol, name in INDEX_CODES.items():
            frame = _baostock_query_frame(
                bs,
                "query_history_k_data_plus",
                provider_events,
                timeout_seconds=query_timeout_seconds,
                code=_ts_code_to_baostock_code(symbol),
                fields=index_fields,
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="3",
            )
            if not frame.empty:
                frame["ts_code"] = symbol
                frame["name"] = name
                index_frames.append(frame)
            time.sleep(sleep_seconds)
        index_daily = _concat_frames(index_frames)
        if not index_daily.empty:
            index_daily["trade_date"] = index_daily["date"].map(_dash_date)
        normalized_index = _normalize_daily_frame(index_daily, "baostock")
        if not normalized_index.empty:
            normalized_index["name"] = normalized_index["ts_code"].map(INDEX_CODES)
        provider_events.append({"source": "baostock", "stage": "index_daily", "status": "success" if not normalized_index.empty else "empty", "rows": len(normalized_index)})

        return _normalize_daily_frame(daily, "baostock"), daily_basic, stock_basic, trade_calendar, normalized_index
    finally:
        bs.logout()


def _efinance_frames(
    start: str,
    end: str,
    stock_basic_seed: pd.DataFrame,
    provider_events: list[dict[str, Any]],
    max_symbols: Optional[int],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """使用 efinance 东方财富接口快速补充多股票历史行情。"""
    try:
        import efinance as ef  # type: ignore
    except ImportError as exc:
        provider_events.append({"source": "efinance", "stage": "import", "status": "skipped", "reason": str(exc)})
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    stock_basic = pd.DataFrame()
    if not stock_basic_seed.empty and "ts_code" in stock_basic_seed.columns:
        stock_basic = stock_basic_seed.copy()
    else:
        try:
            import akshare as ak  # type: ignore

            info = ak.stock_info_a_code_name()
            stock_basic = pd.DataFrame(
                {
                    "ts_code": info["code"].map(_plain_code_to_ts_code),
                    "name": info["name"].astype(str),
                    "list_date": "",
                    "delist_date": "",
                    "market": info["code"].map(lambda value: _market_from_symbol(_plain_code_to_ts_code(value))),
                    "industry": "",
                    "is_st": info["name"].astype(str).str.upper().str.contains("ST", na=False),
                    "source": "efinance",
                }
            )
        except Exception as exc:
            provider_events.append({"source": "efinance", "stage": "stock_basic_seed", "status": "failed", "reason": str(exc)})
            return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    stock_basic = stock_basic.drop_duplicates("ts_code", keep="first")
    symbols = stock_basic["ts_code"].dropna().astype(str).tolist()
    symbols = [symbol for symbol in symbols if symbol and not symbol.endswith(".BJ")]
    if max_symbols:
        symbols = symbols[:max_symbols]
    codes = [_ts_code_to_plain_code(symbol) for symbol in symbols]
    if not codes:
        provider_events.append({"source": "efinance", "stage": "daily", "status": "empty", "rows": 0, "symbols": 0})
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    try:
        result = ef.stock.get_quote_history(codes, beg=_compact_date(start), end=_compact_date(end), klt=101, fqt=0)
    except Exception as exc:
        provider_events.append({"source": "efinance", "stage": "daily", "status": "failed", "reason": str(exc), "symbols": len(codes)})
        return pd.DataFrame(), pd.DataFrame(), stock_basic.assign(source="efinance"), pd.DataFrame()

    if isinstance(result, pd.DataFrame):
        frames = [result]
    elif isinstance(result, dict):
        frames = [frame for frame in result.values() if isinstance(frame, pd.DataFrame) and not frame.empty]
    else:
        frames = []
    daily = _concat_frames(frames)
    if daily.empty:
        provider_events.append({"source": "efinance", "stage": "daily", "status": "empty", "rows": 0, "symbols": len(codes)})
        return pd.DataFrame(), pd.DataFrame(), stock_basic.assign(source="efinance"), pd.DataFrame()
    daily["trade_date"] = daily["日期"].map(_dash_date)
    daily["ts_code"] = daily["股票代码"].map(_plain_code_to_ts_code)
    daily_basic = _normalize_basic_frame(
        pd.DataFrame(
            {
                "trade_date": daily["trade_date"],
                "ts_code": daily["ts_code"],
                "turnover_rate": pd.to_numeric(daily.get("换手率"), errors="coerce"),
            }
        ),
        "efinance",
    )
    stock_output = stock_basic.assign(source="efinance") if stock_basic_seed.empty else pd.DataFrame()
    provider_events.append({"source": "efinance", "stage": "daily", "status": "success", "rows": len(daily), "symbols": len(codes)})
    provider_events.append({"source": "efinance", "stage": "daily_basic", "status": "success", "rows": len(daily_basic)})
    return _normalize_daily_frame(daily, "efinance"), daily_basic, stock_output, pd.DataFrame()


def _akshare_frames(
    start: str,
    end: str,
    stock_basic_seed: pd.DataFrame,
    provider_events: list[dict[str, Any]],
    max_symbols: Optional[int],
    sleep_seconds: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """使用 AKShare 东方财富历史行情作为免费兜底。"""
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        provider_events.append({"source": "akshare", "stage": "import", "status": "skipped", "reason": str(exc)})
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    seeded_stock_basic = not stock_basic_seed.empty
    stock_basic = pd.DataFrame()
    if not stock_basic_seed.empty:
        stock_basic = stock_basic_seed.copy()
    else:
        try:
            info = ak.stock_info_a_code_name()
            stock_basic = pd.DataFrame(
                {
                    "ts_code": info["code"].map(_plain_code_to_ts_code),
                    "name": info["name"].astype(str),
                    "list_date": "",
                    "delist_date": "",
                    "market": info["code"].map(lambda value: _market_from_symbol(_plain_code_to_ts_code(value))),
                    "industry": "",
                    "is_st": info["name"].astype(str).str.upper().str.contains("ST", na=False),
                    "source": "akshare",
                }
            )
            provider_events.append({"source": "akshare", "stage": "stock_basic", "status": "success", "rows": len(stock_basic)})
        except Exception as exc:
            provider_events.append({"source": "akshare", "stage": "stock_basic", "status": "failed", "reason": str(exc)})
            return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    symbols = stock_basic.sort_values(["list_date", "ts_code"])["ts_code"].dropna().astype(str).tolist()
    if max_symbols:
        symbols = symbols[:max_symbols]
    start_raw = _compact_date(start)
    end_raw = _compact_date(end)
    daily_frames: list[pd.DataFrame] = []
    basic_frames: list[pd.DataFrame] = []
    failed_symbols = 0
    for symbol in symbols:
        try:
            frame = ak.stock_zh_a_hist(symbol=_ts_code_to_plain_code(symbol), period="daily", start_date=start_raw, end_date=end_raw, adjust="")
            time.sleep(sleep_seconds)
        except Exception as exc:
            failed_symbols += 1
            if failed_symbols <= 20:
                provider_events.append({"source": "akshare", "stage": "daily", "status": "failed", "ts_code": symbol, "reason": str(exc)})
            continue
        if frame.empty:
            continue
        frame["ts_code"] = symbol
        daily_frames.append(frame)
        basic_frames.append(
            pd.DataFrame(
                {
                    "trade_date": frame["日期"].map(_dash_date),
                    "ts_code": symbol,
                    "turnover_rate": pd.to_numeric(frame.get("换手率"), errors="coerce"),
                }
            )
        )
    daily = _concat_frames(daily_frames)
    if not daily.empty:
        daily["trade_date"] = daily["日期"].map(_dash_date)
    daily_basic = _normalize_basic_frame(_concat_frames(basic_frames), "akshare")
    provider_events.append({"source": "akshare", "stage": "daily", "status": "success" if not daily.empty else "empty", "rows": len(daily), "symbols": len(symbols), "failed_symbols": failed_symbols})
    provider_events.append({"source": "akshare", "stage": "daily_basic", "status": "success" if not daily_basic.empty else "empty", "rows": len(daily_basic)})
    stock_output = pd.DataFrame() if seeded_stock_basic else stock_basic.assign(source="akshare")
    return _normalize_daily_frame(daily, "akshare"), daily_basic, stock_output, pd.DataFrame()


def _build_trade_calendar(trade_calendar: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """合成交易日历。"""
    if not trade_calendar.empty and "trade_date" in trade_calendar.columns:
        result = trade_calendar[["trade_date", "is_open"]].copy()
        result["trade_date"] = result["trade_date"].map(_dash_date)
        result["is_open"] = pd.to_numeric(result["is_open"], errors="coerce").fillna(0).astype(int)
        return result.drop_duplicates("trade_date").sort_values("trade_date").reset_index(drop=True)
    dates = sorted(daily["trade_date"].dropna().astype(str).unique()) if not daily.empty else []
    return pd.DataFrame({"trade_date": dates, "is_open": 1})


def _fill_stock_basic(stock_basic: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """补齐 stock_basic 基础字段。"""
    if stock_basic.empty:
        symbols = sorted(daily["ts_code"].dropna().astype(str).unique()) if not daily.empty else []
        first_seen = daily.groupby("ts_code")["trade_date"].min().to_dict() if not daily.empty else {}
        stock_basic = pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "name": "",
                    "list_date": first_seen.get(symbol, ""),
                    "delist_date": "",
                    "market": _market_from_symbol(symbol),
                    "is_st": False,
                    "source": "derived_from_daily",
                }
                for symbol in symbols
            ]
        )
    work = stock_basic.copy()
    for col in ["ts_code", "name", "list_date", "delist_date", "market", "industry", "is_st", "source"]:
        if col not in work.columns:
            work[col] = ""
    work["ts_code"] = work["ts_code"].astype(str)
    work["source_priority"] = work["source"].map(lambda source: SOURCE_PRIORITY.get(str(source), 99))
    work["name"] = work["name"].fillna("")
    work["list_date"] = work["list_date"].map(_dash_date)
    work["delist_date"] = work["delist_date"].map(_dash_date)
    work["market"] = work["market"].fillna("").astype(str)
    work["industry"] = work["industry"].fillna("").astype(str)
    work["is_st"] = work["is_st"].fillna(False).astype(bool) | work["name"].astype(str).str.upper().str.contains("ST", na=False)

    def _first_valid(group: pd.DataFrame, column: str) -> str:
        for value in group.sort_values("source_priority")[column].tolist():
            text = str(value).strip()
            if text and text.lower() not in {"nan", "nat", "none"}:
                return text
        return ""

    rows: list[dict[str, Any]] = []
    for symbol, group in work.groupby("ts_code", sort=True):
        rows.append(
            {
                "ts_code": symbol,
                "name": _first_valid(group, "name") or str(symbol),
                "list_date": _first_valid(group, "list_date"),
                "delist_date": _first_valid(group, "delist_date"),
                "market": _first_valid(group, "market") or _market_from_symbol(symbol),
                "industry": _first_valid(group, "industry"),
                "is_st": bool(group["is_st"].any()),
                "source": _first_valid(group, "source"),
            }
        )
    return pd.DataFrame(rows, columns=["ts_code", "name", "list_date", "delist_date", "market", "industry", "is_st", "source"])


def _industry_table(stock_basic: pd.DataFrame) -> pd.DataFrame:
    """生成行业表；没有行业则保留空字段。"""
    rows = []
    for _, row in stock_basic.iterrows():
        industry = row.get("industry") if "industry" in stock_basic.columns else ""
        rows.append({"ts_code": row.get("ts_code"), "industry": industry if isinstance(industry, str) else "", "sector": ""})
    return pd.DataFrame(rows)


def _limit_flag(df: pd.DataFrame) -> pd.Series:
    """近似涨跌停过滤。"""
    pct = pd.to_numeric(df.get("pct_chg"), errors="coerce")
    close = pd.to_numeric(df.get("close"), errors="coerce")
    pre_close = pd.to_numeric(df.get("pre_close"), errors="coerce")
    ratio = close / pre_close - 1
    return (pct >= 0.095) | (pct <= -0.095) | (ratio >= 0.095) | (ratio <= -0.095)


def _dynamic_universe(
    daily: pd.DataFrame,
    stock_basic: pd.DataFrame,
    min_history: int,
    min_price: float,
    min_amount: float,
) -> pd.DataFrame:
    """逐日动态生成 universe，禁止使用未来信息。"""
    if daily.empty:
        return pd.DataFrame(columns=DAILY_UNIVERSE_COLUMNS)
    work = daily.copy()
    work["trade_date"] = work["trade_date"].astype(str)
    work = work.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    work["history_days"] = work.groupby("ts_code").cumcount() + 1
    required_numeric = ["open", "high", "low", "close", "amount"]
    work["missing_required_data"] = work[required_numeric].apply(pd.to_numeric, errors="coerce").isna().any(axis=1)
    work["invalid_ohlc"] = (
        pd.to_numeric(work["high"], errors="coerce").lt(pd.to_numeric(work["low"], errors="coerce"))
        | pd.to_numeric(work["close"], errors="coerce").le(0)
        | pd.to_numeric(work["amount"], errors="coerce").le(0)
    )
    work["limit_flag"] = _limit_flag(work)
    basic = _fill_stock_basic(stock_basic, work)
    basic_by_symbol = basic.set_index("ts_code").to_dict("index")
    first_seen = work.groupby("ts_code")["trade_date"].min().to_dict()
    by_date = {date: group.set_index("ts_code") for date, group in work.groupby("trade_date")}
    all_symbols = sorted(set(basic["ts_code"].astype(str)) | set(work["ts_code"].astype(str)))
    rows: list[dict[str, Any]] = []
    for trade_date in sorted(work["trade_date"].unique()):
        total = 0
        data_missing = 0
        history_bad = 0
        price_bad = 0
        amount_bad = 0
        limit_bad = 0
        st_bad = 0
        other_bad = 0
        after_history = after_price = after_amount = after_limit = after_st = 0
        today = by_date.get(trade_date, pd.DataFrame())
        for symbol in all_symbols:
            meta = basic_by_symbol.get(symbol, {})
            list_date_value = meta.get("list_date")
            delist_date_value = meta.get("delist_date")
            list_date = _dash_date(list_date_value) if pd.notna(list_date_value) else ""
            delist_date = _dash_date(delist_date_value) if pd.notna(delist_date_value) else ""
            if not list_date:
                list_date = str(first_seen.get(symbol) or "")
            if list_date and list_date > trade_date:
                continue
            if delist_date and delist_date < trade_date:
                continue
            total += 1
            if symbol not in today.index:
                data_missing += 1
                continue
            record = today.loc[symbol]
            if isinstance(record, pd.DataFrame):
                record = record.iloc[0]
            if bool(record.get("missing_required_data")):
                data_missing += 1
                continue
            if bool(record.get("invalid_ohlc")):
                other_bad += 1
                continue
            if int(record.get("history_days") or 0) < min_history:
                history_bad += 1
                continue
            after_history += 1
            close = _safe_float(record.get("close"))
            if close is None or close <= min_price:
                price_bad += 1
                continue
            after_price += 1
            amount = _safe_float(record.get("amount"))
            if amount is None or amount < min_amount:
                amount_bad += 1
                continue
            after_amount += 1
            if bool(record.get("limit_flag")):
                limit_bad += 1
                continue
            after_limit += 1
            if bool(meta.get("is_st")):
                st_bad += 1
                continue
            after_st += 1
        reason_counts = {
            "data_missing": data_missing,
            "history_length_insufficient": history_bad,
            "price_filter": price_bad,
            "amount_filter": amount_bad,
            "limit_filter": limit_bad,
            "st_or_suspended_filter": st_bad,
            "other_filter": other_bad,
        }
        rows.append(
            {
                "trade_date": trade_date,
                "total_candidates_before_filter": total,
                "after_history_filter": after_history,
                "after_price_filter": after_price,
                "after_amount_filter": after_amount,
                "after_limit_filter": after_limit,
                "after_st_filter": after_st,
                "final_universe_count": after_st,
                **reason_counts,
                "exclusion_reason_summary": json.dumps(reason_counts, ensure_ascii=False),
            }
        )
    return pd.DataFrame(rows, columns=DAILY_UNIVERSE_COLUMNS)


def _write_per_symbol_cache(daily: pd.DataFrame, expanded_dir: Path) -> None:
    """写出兼容既有回测模块的 per-symbol K 线缓存。"""
    cache_dir = expanded_dir / "daily_kline"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for old_file in cache_dir.glob("*.csv"):
        old_file.unlink()
    if daily.empty:
        return
    for symbol, group in daily.sort_values(["ts_code", "trade_date"]).groupby("ts_code"):
        rows = []
        for _, row in group.iterrows():
            trade_date = str(row["trade_date"])
            rows.append(
                {
                    "date": trade_date,
                    "date_ms": _date_ms(trade_date),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("vol"),
                    "turnover": row.get("amount"),
                }
            )
        _write_csv(cache_dir / f"{_cache_key(symbol)}.csv", pd.DataFrame(rows))


def _write_index_cache(index_daily: pd.DataFrame, expanded_dir: Path) -> None:
    """写出兼容市场环境函数的指数 K 线缓存。"""
    index_dir = expanded_dir / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    for old_file in index_dir.glob("*.csv"):
        old_file.unlink()
    if index_daily.empty:
        return
    for symbol, group in index_daily.sort_values(["ts_code", "trade_date"]).groupby("ts_code"):
        rows = []
        for _, row in group.iterrows():
            trade_date = str(row["trade_date"])
            rows.append(
                {
                    "date": trade_date,
                    "date_ms": _date_ms(trade_date),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("vol"),
                    "turnover": row.get("amount"),
                }
            )
        _write_csv(index_dir / f"{_cache_key(symbol)}.csv", pd.DataFrame(rows))


def _source_coverage(daily: pd.DataFrame) -> pd.DataFrame:
    """按 source 汇总覆盖。"""
    if daily.empty:
        return pd.DataFrame()
    rows = []
    for source, group in daily.groupby("source"):
        rows.append(
            {
                "source": source,
                "rows": int(len(group)),
                "symbols": int(group["ts_code"].nunique()),
                "start_date": group["trade_date"].min(),
                "end_date": group["trade_date"].max(),
            }
        )
    return pd.DataFrame(rows)


def _overlap_consistency(raw_daily: pd.DataFrame) -> dict[str, Any]:
    """检查 Fuyao/Tushare/本地重叠记录的一致性。"""
    if raw_daily.empty:
        return {"overlap_keys": 0, "close_mismatch_count": 0, "amount_mismatch_count": 0}
    if "source" in raw_daily.columns and raw_daily["source"].nunique(dropna=True) <= 1:
        return {
            "overlap_keys": 0,
            "close_mismatch_count": 0,
            "amount_mismatch_count": 0,
            "sample_mismatches": [],
        }
    overlap_mask = raw_daily.duplicated(["trade_date", "ts_code"], keep=False)
    overlap_daily = raw_daily.loc[overlap_mask].copy()
    if overlap_daily.empty:
        return {
            "overlap_keys": 0,
            "close_mismatch_count": 0,
            "amount_mismatch_count": 0,
            "sample_mismatches": [],
        }
    grouped = overlap_daily.groupby(["trade_date", "ts_code"])
    overlap_keys = 0
    close_mismatch = 0
    amount_mismatch = 0
    samples: list[dict[str, Any]] = []
    for (trade_date, symbol), group in grouped:
        if group["source"].nunique() <= 1:
            continue
        overlap_keys += 1
        closes = pd.to_numeric(group["close"], errors="coerce").dropna()
        amounts = pd.to_numeric(group["amount"], errors="coerce").dropna()
        close_bad = bool(len(closes) > 1 and closes.max() != 0 and (closes.max() - closes.min()) / abs(closes.max()) > 0.001)
        amount_bad = bool(len(amounts) > 1 and amounts.max() != 0 and (amounts.max() - amounts.min()) / abs(amounts.max()) > 0.05)
        close_mismatch += int(close_bad)
        amount_mismatch += int(amount_bad)
        if (close_bad or amount_bad) and len(samples) < 10:
            samples.append({"trade_date": trade_date, "ts_code": symbol, "sources": ",".join(sorted(group["source"].unique()))})
    return {
        "overlap_keys": overlap_keys,
        "close_mismatch_count": close_mismatch,
        "amount_mismatch_count": amount_mismatch,
        "sample_mismatches": samples,
    }


def _quality_metrics(raw_daily: pd.DataFrame, daily: pd.DataFrame, trade_calendar: pd.DataFrame, daily_universe: pd.DataFrame) -> dict[str, Any]:
    """生成数据质量指标。"""
    missing_fields = {}
    for field in REQUIRED_DAILY_COLUMNS:
        missing_fields[field] = int(daily[field].isna().sum()) if field in daily.columns else len(daily)
    duplicate_raw = int(raw_daily.duplicated(["trade_date", "ts_code"]).sum()) if not raw_daily.empty else 0
    duplicate_final = int(daily.duplicated(["trade_date", "ts_code"]).sum()) if not daily.empty else 0
    high_low = int((pd.to_numeric(daily["high"], errors="coerce") < pd.to_numeric(daily["low"], errors="coerce")).sum()) if not daily.empty else 0
    close_bad = int((pd.to_numeric(daily["close"], errors="coerce") <= 0).sum()) if not daily.empty else 0
    amount_bad = int((pd.to_numeric(daily["amount"], errors="coerce") <= 0).sum()) if not daily.empty else 0
    limit_count = int(_limit_flag(daily).sum()) if not daily.empty else 0
    expected_dates = set(trade_calendar.loc[trade_calendar["is_open"] == 1, "trade_date"].astype(str)) if not trade_calendar.empty else set()
    actual_dates = set(daily["trade_date"].dropna().astype(str)) if not daily.empty else set()
    missing_dates = sorted(expected_dates - actual_dates)
    daily_counts = daily.groupby("trade_date")["ts_code"].nunique() if not daily.empty else pd.Series(dtype=float)
    unusable = []
    if not daily_universe.empty:
        unusable = daily_universe.loc[daily_universe["final_universe_count"] < 30, "trade_date"].astype(str).tolist()
    return {
        "row_count": int(len(daily)),
        "symbol_count": int(daily["ts_code"].nunique()) if not daily.empty else 0,
        "start_date": daily["trade_date"].min() if not daily.empty else None,
        "end_date": daily["trade_date"].max() if not daily.empty else None,
        "daily_stock_count_min": int(daily_counts.min()) if len(daily_counts) else 0,
        "daily_stock_count_median": float(daily_counts.median()) if len(daily_counts) else 0,
        "daily_stock_count_max": int(daily_counts.max()) if len(daily_counts) else 0,
        "missing_dates": missing_dates[:100],
        "missing_date_count": len(missing_dates),
        "missing_fields": missing_fields,
        "duplicate_raw_records": duplicate_raw,
        "duplicate_final_records": duplicate_final,
        "ohlc_high_lt_low": high_low,
        "close_le_zero": close_bad,
        "amount_le_zero": amount_bad,
        "limit_like_records": limit_count,
        "overlap_consistency": _overlap_consistency(raw_daily),
        "local_cache_supplement_ratio": float((daily["source"] == "local_cache").mean()) if not daily.empty else None,
        "unusable_dates": unusable[:120],
        "unusable_date_count": len(unusable),
    }


def _write_reports(
    *,
    output_dir: Path,
    expanded_dir: Path,
    start: str,
    end: str,
    effective_start: str,
    raw_daily: pd.DataFrame,
    daily: pd.DataFrame,
    daily_basic: pd.DataFrame,
    stock_basic: pd.DataFrame,
    trade_calendar: pd.DataFrame,
    daily_universe: pd.DataFrame,
    provider_events: list[dict[str, Any]],
    quality: dict[str, Any],
) -> None:
    """写数据扩展、质量和 universe 覆盖报告。"""
    coverage = _source_coverage(daily)
    _write_csv(output_dir / "source_coverage.csv", coverage)
    _write_csv(output_dir / "provider_events.csv", pd.DataFrame(provider_events))
    _write_csv(output_dir / "daily_stock_counts.csv", daily.groupby("trade_date")["ts_code"].nunique().rename("stock_count").reset_index() if not daily.empty else pd.DataFrame(columns=["trade_date", "stock_count"]))
    _write_json(output_dir / "data_quality_summary.json", quality)

    provider_failures = [event for event in provider_events if event.get("status") in {"failed", "skipped", "empty"}]
    missing_lines = [
        "# Missing Data Report",
        "",
        "本报告记录数据源不可用、权限不足、缺包或覆盖不足的原因。流程不会因此中断，会降级使用下一数据源或本地缓存。",
        "",
        "| Source | Stage | Status | Reason |",
        "|---|---|---|---|",
    ]
    for event in provider_failures[:200]:
        missing_lines.append(f"| {event.get('source')} | {event.get('stage')} | {event.get('status')} | {str(event.get('reason') or event.get('rows') or event.get('open_dates') or '')[:180]} |")
    if not provider_failures:
        missing_lines.append("| - | - | - | No provider failures recorded |")
    (output_dir / "missing_data_report.md").write_text("\n".join(missing_lines) + "\n", encoding="utf-8")

    zero_dates = daily_universe[daily_universe["final_universe_count"] == 0] if not daily_universe.empty else pd.DataFrame()
    low_dates = daily_universe[(daily_universe["final_universe_count"] > 0) & (daily_universe["final_universe_count"] < 30)] if not daily_universe.empty else pd.DataFrame()
    active_dates = daily_universe[pd.to_numeric(daily_universe.get("final_universe_count", pd.Series(dtype=float)), errors="coerce").fillna(0) >= 30] if not daily_universe.empty else pd.DataFrame()
    reason_totals = Counter()
    for col in ["data_missing", "history_length_insufficient", "price_filter", "amount_filter", "limit_filter", "st_or_suspended_filter", "other_filter"]:
        reason_totals[col] = int(daily_universe[col].sum()) if col in daily_universe.columns else 0
    if len(daily_universe):
        enough_for_backtest = (
            quality.get("start_date") is not None
            and int(quality.get("symbol_count") or 0) >= 500
            and float(quality.get("daily_stock_count_median") or 0) >= 300
            and len(active_dates) >= 120
        )
    else:
        enough_for_backtest = False
    universe_lines = [
        "# Universe Coverage Report",
        "",
        f"- Requested priority range: {start} to {end}",
        f"- Effective provider request start: {effective_start}",
        f"- Final data range: {quality.get('start_date')} to {quality.get('end_date')}",
        f"- Final symbols: {quality.get('symbol_count')}",
        f"- Median daily stock count: {quality.get('daily_stock_count_median')}",
        f"- Universe zero dates: {len(zero_dates)}",
        f"- Universe low-count dates (<30): {len(low_dates)}",
        f"- Active signal dates (universe >=30): {len(active_dates)}",
        f"- Enough for expanded backtest after warmup: {enough_for_backtest}",
        "",
        "## Reason Totals",
        "",
        "| Reason | Count |",
        "|---|---:|",
    ]
    for key, value in reason_totals.items():
        universe_lines.append(f"| {key} | {value} |")
    if not zero_dates.empty:
        universe_lines.extend(["", "## Universe 为 0 的日期（前 40 条）", "", ", ".join(zero_dates["trade_date"].astype(str).head(40).tolist())])
    if not low_dates.empty:
        universe_lines.extend(["", "## Universe 数量过低日期（前 40 条）", "", ", ".join(low_dates["trade_date"].astype(str).head(40).tolist())])
    if not enough_for_backtest:
        universe_lines.extend(
            [
                "",
                "## 判断",
                "",
                "当前数据仍不足以视为完整全市场长期回测。若数据源没有成功覆盖全市场，扩展回测只能作为本地缓存样本或部分市场样本的复跑。",
            ]
        )
    (output_dir / "universe_coverage_report.md").write_text("\n".join(universe_lines) + "\n", encoding="utf-8")

    quality_lines = [
        "# Data Quality Report",
        "",
        f"- Final data rows: {quality.get('row_count')}",
        f"- Final symbols: {quality.get('symbol_count')}",
        f"- Final date range: {quality.get('start_date')} to {quality.get('end_date')}",
        f"- Daily stock count min / median / max: {quality.get('daily_stock_count_min')} / {quality.get('daily_stock_count_median')} / {quality.get('daily_stock_count_max')}",
        f"- Missing open trade dates: {quality.get('missing_date_count')}",
        f"- Raw duplicate records: {quality.get('duplicate_raw_records')}",
        f"- Final duplicate records: {quality.get('duplicate_final_records')}",
        f"- OHLC high < low: {quality.get('ohlc_high_lt_low')}",
        f"- close <= 0: {quality.get('close_le_zero')}",
        f"- amount <= 0: {quality.get('amount_le_zero')}",
        f"- Limit-like records: {quality.get('limit_like_records')}",
        f"- Local cache supplement ratio: {quality.get('local_cache_supplement_ratio')}",
        "",
        "## Source Coverage",
        "",
        _df_to_markdown(coverage) if not coverage.empty else "No source coverage.",
        "",
        "## Missing Fields",
        "",
        "| Field | Missing Rows |",
        "|---|---:|",
    ]
    for field, count in quality.get("missing_fields", {}).items():
        quality_lines.append(f"| {field} | {count} |")
    overlap = quality.get("overlap_consistency") or {}
    quality_lines.extend(
        [
            "",
            "## Fuyao / Tushare / Local Overlap Consistency",
            "",
            f"- Overlap keys: {overlap.get('overlap_keys')}",
            f"- Close mismatches: {overlap.get('close_mismatch_count')}",
            f"- Amount mismatches: {overlap.get('amount_mismatch_count')}",
            "",
            "## Dates Not Suitable For Formal Backtest",
            "",
            f"- Count: {quality.get('unusable_date_count')}",
            f"- Sample: {', '.join(quality.get('unusable_dates') or [])}",
        ]
    )
    if not enough_for_backtest:
        quality_lines.extend(
            [
                "",
                "## Boundary",
                "",
                "当前扩展回测仍不能视为完整全市场长期回测。原因通常是数据源权限缺失、历史覆盖不足、或 universe 中位股票数过低。",
            ]
        )
    (output_dir / "data_quality_report.md").write_text("\n".join(quality_lines) + "\n", encoding="utf-8")

    expansion_lines = [
        "# Data Expansion Report",
        "",
        f"- Requested priority range: {start} to {end}",
        f"- Effective provider request start: {effective_start}",
        f"- Expanded directory: `{expanded_dir}`",
        f"- Unified daily kline: `{expanded_dir / 'daily_kline.csv'}`",
        f"- Unified adjusted kline: `{expanded_dir / 'adj_kline.csv'}`",
        f"- Daily basic: `{expanded_dir / 'daily_basic.csv'}`",
        f"- Trade calendar: `{expanded_dir / 'trade_calendar.csv'}`",
        f"- Stock basic: `{expanded_dir / 'stock_basic.csv'}`",
        f"- Index daily: `{expanded_dir / 'index_daily.csv'}`",
        f"- Daily universe: `{expanded_dir / 'daily_universe.csv'}`",
        "",
        "## Provider Events",
        "",
        _df_to_markdown(pd.DataFrame(provider_events).tail(30)) if provider_events else "No provider events.",
        "",
        "## Conclusion",
        "",
    ]
    if enough_for_backtest:
        expansion_lines.append("数据覆盖初步满足扩展回测条件，但仍需结合 `data_quality_report.md` 做人工复核。")
    else:
        expansion_lines.append("数据覆盖不足，扩展回测将自动降级为当前可用样本，不能视为完整全市场长期回测。")
    (output_dir / "data_expansion_report.md").write_text("\n".join(expansion_lines) + "\n", encoding="utf-8")


def run_data_expansion(
    *,
    start: str = DEFAULT_START,
    end: Optional[str] = None,
    extended_start: str = DEFAULT_EXTENDED_START,
    expanded_dir: Path = DEFAULT_EXPANDED_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    max_symbols: Optional[int] = None,
    max_trade_dates: Optional[int] = None,
    min_history: int = 60,
    min_price: Optional[float] = None,
    min_amount: Optional[float] = None,
    tushare_retries: int = 3,
    tushare_sleep_seconds: float = 0.25,
    enable_free_history: bool = True,
    free_provider_sleep: float = 0.05,
    baostock_timeout: int = 20,
    disable_provider_cache: bool = False,
    no_extended: bool = False,
) -> dict[str, Any]:
    """执行历史数据扩展与质量审计。"""
    end = end or datetime.now().strftime("%Y-%m-%d")
    scanner._set_active_config(scanner._load_scanner_config("strategy.json"))
    min_price = float(min_price if min_price is not None else scanner._cfg("filters", "min_price", 3))
    min_amount = float(min_amount if min_amount is not None else scanner._cfg("filters", "min_turnover", 1e9))
    has_provider = bool(scanner._fuyao_api_key() or os.getenv("TUSHARE_TOKEN") or enable_free_history)
    effective_start = extended_start if has_provider and not no_extended else start
    output_dir.mkdir(parents=True, exist_ok=True)
    expanded_dir.mkdir(parents=True, exist_ok=True)

    provider_events: list[dict[str, Any]] = []
    fuyao_daily, fuyao_basic, fuyao_stock, fuyao_index = _fuyao_frames(effective_start, end, max_symbols, provider_events)
    tushare_daily, tushare_basic, tushare_stock, tushare_calendar, tushare_index = _tushare_frames(
        effective_start,
        end,
        provider_events,
        max_trade_dates=max_trade_dates,
        retries=tushare_retries,
        sleep_seconds=tushare_sleep_seconds,
    )
    if enable_free_history:
        efinance_daily, efinance_basic, efinance_stock, efinance_index = _efinance_frames(
            effective_start,
            end,
            _concat_frames([fuyao_stock, tushare_stock]),
            provider_events,
            max_symbols=max_symbols,
        )
        baostock_daily, baostock_basic, baostock_stock, baostock_calendar, baostock_index = _baostock_frames(
            effective_start,
            end,
            provider_events,
            max_symbols=max_symbols,
            sleep_seconds=free_provider_sleep,
            checkpoint_dir=None if disable_provider_cache else expanded_dir / "provider_cache",
            query_timeout_seconds=baostock_timeout,
            fetch_daily=efinance_daily.empty,
        )
        if efinance_daily.empty and baostock_daily.empty:
            akshare_seed = _concat_frames([baostock_stock, efinance_stock, tushare_stock, fuyao_stock])
            akshare_daily, akshare_basic, akshare_stock, akshare_index = _akshare_frames(
                effective_start,
                end,
                akshare_seed,
                provider_events,
                max_symbols=max_symbols,
                sleep_seconds=free_provider_sleep,
            )
        else:
            provider_events.append({"source": "akshare", "stage": "daily", "status": "skipped", "reason": "fast_history_provider_available"})
            akshare_daily = akshare_basic = akshare_stock = akshare_index = pd.DataFrame()
    else:
        provider_events.append({"source": "free_history", "stage": "all", "status": "skipped", "reason": "disabled_by_cli"})
        efinance_daily = efinance_basic = efinance_stock = efinance_index = pd.DataFrame()
        baostock_daily = baostock_basic = baostock_stock = baostock_calendar = baostock_index = pd.DataFrame()
        akshare_daily = akshare_basic = akshare_stock = akshare_index = pd.DataFrame()
    local_daily, local_basic, local_stock, local_index = _local_daily_frames()

    raw_daily = _filter_date_range(
        _concat_frames([fuyao_daily, tushare_daily, efinance_daily, baostock_daily, akshare_daily, local_daily], columns=[*REQUIRED_DAILY_COLUMNS, "source", "source_priority"]),
        effective_start,
        end,
    )
    daily = _merge_by_priority(raw_daily, ["trade_date", "ts_code"])
    daily = daily.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    daily["pre_close"] = daily.groupby("ts_code")["close"].shift(1).where(daily["pre_close"].isna(), daily["pre_close"])
    missing_pct = daily["pct_chg"].isna() & daily["pre_close"].notna() & daily["pre_close"].ne(0)
    daily.loc[missing_pct, "pct_chg"] = daily.loc[missing_pct, "close"] / daily.loc[missing_pct, "pre_close"] - 1
    daily = _drop_incomplete_ohlc_rows(daily)

    raw_basic = _filter_date_range(
        _concat_frames([fuyao_basic, tushare_basic, efinance_basic, baostock_basic, akshare_basic, local_basic], columns=[*REQUIRED_BASIC_COLUMNS, "source", "source_priority"]),
        effective_start,
        end,
    )
    daily_basic = _merge_by_priority(raw_basic, ["trade_date", "ts_code"])
    if not daily_basic.empty and not daily.empty:
        valid_keys = daily[["trade_date", "ts_code"]].drop_duplicates()
        daily_basic = daily_basic.merge(valid_keys, on=["trade_date", "ts_code"], how="inner")
    stock_basic = _fill_stock_basic(_concat_frames([fuyao_stock, tushare_stock, efinance_stock, baostock_stock, akshare_stock, local_stock]), daily)
    trade_calendar = _build_trade_calendar(_concat_frames([tushare_calendar, baostock_calendar]), daily)
    raw_index = _filter_date_range(_concat_frames([fuyao_index, tushare_index, efinance_index, baostock_index, akshare_index, local_index]), effective_start, end)
    index_daily = _merge_by_priority(raw_index, ["trade_date", "ts_code"]) if not raw_index.empty else pd.DataFrame(columns=[*REQUIRED_DAILY_COLUMNS, "source", "source_priority", "name"])
    if not index_daily.empty and "name" not in index_daily.columns:
        index_daily["name"] = index_daily["ts_code"].map(INDEX_CODES)
    index_daily = _drop_incomplete_ohlc_rows(index_daily)
    industry = _industry_table(stock_basic)
    daily_universe = _dynamic_universe(daily, stock_basic, min_history=min_history, min_price=min_price, min_amount=min_amount)

    adj_kline = daily.copy()
    adj_kline["adjustment"] = "unadjusted"
    _write_csv(expanded_dir / "daily_kline.csv", daily)
    _write_csv(expanded_dir / "adj_kline.csv", adj_kline)
    _write_csv(expanded_dir / "daily_basic.csv", daily_basic)
    _write_csv(expanded_dir / "trade_calendar.csv", trade_calendar)
    _write_csv(expanded_dir / "stock_basic.csv", stock_basic)
    _write_csv(expanded_dir / "index_daily.csv", index_daily)
    _write_csv(expanded_dir / "daily_universe.csv", daily_universe)
    _write_csv(expanded_dir / "industry_or_sector.csv", industry)
    _write_csv(output_dir / "daily_universe.csv", daily_universe)
    _write_per_symbol_cache(daily, expanded_dir)
    _write_index_cache(index_daily, expanded_dir)

    quality = _quality_metrics(raw_daily, daily, trade_calendar, daily_universe)
    _write_reports(
        output_dir=output_dir,
        expanded_dir=expanded_dir,
        start=start,
        end=end,
        effective_start=effective_start,
        raw_daily=raw_daily,
        daily=daily,
        daily_basic=daily_basic,
        stock_basic=stock_basic,
        trade_calendar=trade_calendar,
        daily_universe=daily_universe,
        provider_events=provider_events,
        quality=quality,
    )
    summary = {
        "module": "data_expansion_pipeline",
        "start": start,
        "extended_start": extended_start,
        "effective_start": effective_start,
        "end": end,
        "enable_free_history": enable_free_history,
        "provider_cache_enabled": not disable_provider_cache,
        "expanded_dir": str(expanded_dir),
        "output_dir": str(output_dir),
        "daily_rows": int(len(daily)),
        "symbol_count": int(daily["ts_code"].nunique()) if not daily.empty else 0,
        "trade_date_count": int(daily["trade_date"].nunique()) if not daily.empty else 0,
        "source_coverage_path": str(output_dir / "source_coverage.csv"),
        "data_expansion_report": str(output_dir / "data_expansion_report.md"),
        "data_quality_report": str(output_dir / "data_quality_report.md"),
        "universe_coverage_report": str(output_dir / "universe_coverage_report.md"),
        "missing_data_report": str(output_dir / "missing_data_report.md"),
        "quality": quality,
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="扩展历史数据并生成数据质量报告")
    parser.add_argument("--start", default=DEFAULT_START, help="优先覆盖起始日期")
    parser.add_argument("--extended-start", default=DEFAULT_EXTENDED_START, help="有权限时尝试更早起始日期")
    parser.add_argument("--end", help="结束日期，默认今日")
    parser.add_argument("--expanded-dir", default=str(DEFAULT_EXPANDED_DIR), help="扩展数据目录")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="报告输出目录")
    parser.add_argument("--max-symbols", type=int, help="单票历史调试时最多拉多少只股票；不传则尝试全量")
    parser.add_argument("--max-trade-dates", type=int, help="Tushare 调试时最多拉最近多少个交易日")
    parser.add_argument("--min-history", type=int, default=60, help="动态 universe 最少历史长度")
    parser.add_argument("--min-price", type=float, help="动态 universe 最低价格")
    parser.add_argument("--min-amount", type=float, help="动态 universe 最低成交额，单位元")
    parser.add_argument("--tushare-retries", type=int, default=3, help="Tushare 重试次数")
    parser.add_argument("--tushare-sleep", type=float, default=0.25, help="Tushare 每次调用后 sleep 秒数")
    parser.add_argument("--disable-free-history", action="store_true", help="禁用 BaoStock/AKShare 免费历史补数层")
    parser.add_argument("--free-provider-sleep", type=float, default=0.05, help="BaoStock/AKShare 单票请求间隔秒数")
    parser.add_argument("--baostock-timeout", type=int, default=20, help="BaoStock 单次请求软超时秒数")
    parser.add_argument("--disable-provider-cache", action="store_true", help="禁用逐票 provider cache")
    parser.add_argument("--no-extended", action="store_true", help="即使有 provider token 也只尝试 --start")
    args = parser.parse_args()
    summary = run_data_expansion(
        start=args.start,
        end=args.end,
        extended_start=args.extended_start,
        expanded_dir=Path(args.expanded_dir),
        output_dir=Path(args.output),
        max_symbols=args.max_symbols,
        max_trade_dates=args.max_trade_dates,
        min_history=args.min_history,
        min_price=args.min_price,
        min_amount=args.min_amount,
        tushare_retries=args.tushare_retries,
        tushare_sleep_seconds=args.tushare_sleep,
        enable_free_history=not args.disable_free_history,
        free_provider_sleep=args.free_provider_sleep,
        baostock_timeout=args.baostock_timeout,
        disable_provider_cache=args.disable_provider_cache,
        no_extended=args.no_extended,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=fr._json_default))


if __name__ == "__main__":
    main()
