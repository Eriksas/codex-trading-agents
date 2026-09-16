"""扫描数据源、缓存和 K 线补齐；沿用现有请求与降级行为。"""

from datetime import datetime, timedelta
from pathlib import Path
from statistics import median, stdev
from typing import Any, Optional
import csv
import json
import logging
import os
import requests
import subprocess

try:
    import certifi
except ImportError:  # pragma: no cover - optional certificate bundle
    certifi = None

if __package__:
    from .scanner_config import (
        _cfg,
    )
    from .scanner_utils import (
        _symbol_fuyao_to_scan,
        _to_float,
        _write_csv,
    )
else:
    from scanner_config import (
        _cfg,
    )
    from scanner_utils import (
        _symbol_fuyao_to_scan,
        _to_float,
        _write_csv,
    )

logger = logging.getLogger(f"{__package__}.market_scanner" if __package__ else "market_scanner")

DEFAULT_INDEXES = [
    ("上证", "000001.SH"),
    ("深成指", "399001.SZ"),
    ("创业板", "399006.SZ"),
    ("沪深300", "000300.SH"),
]

FUYAO_BASE_URL = "https://fuyao.aicubes.cn"


def _verify_path() -> str | bool:
    """返回 HTTPS 证书校验配置。"""
    return certifi.where() if certifi is not None else True


def _fuyao_api_key() -> Optional[str]:
    """从环境变量或文件读取扶摇 API Key。"""
    key = os.environ.get("FUYAO_API_KEY")
    if key:
        return key.strip()
    key_files = [
        os.environ.get("FUYAO_API_KEY_FILE"),
        str(Path.home() / ".secrets" / "fuyao_api_key"),
    ]
    for key_file in key_files:
        if not key_file:
            continue
        path = Path(key_file).expanduser()
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    return None


def _fuyao_get(path: str, params: dict[str, Any], timeout: int = 20) -> dict:
    """调用扶摇 REST API 并拆开统一响应信封。"""
    key = _fuyao_api_key()
    if not key:
        raise RuntimeError("缺少 FUYAO_API_KEY，跳过扶摇数据源")
    resp = requests.get(
        f"{FUYAO_BASE_URL}{path}",
        params=params,
        headers={"X-api-key": key, "User-Agent": "codex-trading-agents/1.0"},
        timeout=timeout,
        verify=_verify_path(),
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"扶摇 API 返回错误 code={payload.get('code')}: {payload.get('message')}")
    return payload.get("data") or {}


def _cache_key(symbol: str) -> str:
    """生成缓存文件名安全键。"""
    return "".join(ch if ch.isalnum() else "_" for ch in symbol)


def _cache_path(kind: str, symbol: str) -> Path:
    """返回历史 K 缓存路径。"""
    cache_dir = Path(str(_cfg("data", "cache_dir", "data/cache/market_scanner")))
    return cache_dir / kind / f"{_cache_key(symbol)}.csv"


def _parse_bar_date(value: str) -> Optional[datetime]:
    """解析缓存中的日期。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _read_bars_cache(kind: str, symbol: str) -> list[dict]:
    """读取历史 K 缓存。"""
    path = _cache_path(kind, symbol)
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            close = _to_float(row.get("close"))
            open_price = _to_float(row.get("open"))
            high = _to_float(row.get("high"))
            low = _to_float(row.get("low"))
            if close is None or open_price is None or high is None or low is None:
                continue
            rows.append(
                {
                    "date": row.get("date") or "",
                    "date_ms": int(float(row["date_ms"])) if row.get("date_ms") else None,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": _to_float(row.get("volume")),
                    "turnover": _to_float(row.get("turnover")),
                }
            )
    return sorted(rows, key=lambda x: x.get("date_ms") or 0)


def _write_bars_cache(kind: str, symbol: str, bars: list[dict]) -> None:
    """写入历史 K 缓存。"""
    if not _cfg("data", "cache_enabled", True):
        return
    path = _cache_path(kind, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["date", "date_ms", "open", "high", "low", "close", "volume", "turnover"]
    _write_csv(path, bars, fields)


def _merge_bars(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """合并历史 K，按 date_ms/date 去重。"""
    merged: dict[str, dict] = {}
    for bar in existing + fresh:
        key = str(bar.get("date_ms") or bar.get("date") or "")
        if key:
            merged[key] = bar
    return sorted(merged.values(), key=lambda x: x.get("date_ms") or 0)


def _cache_is_fresh(bars: list[dict]) -> bool:
    """判断缓存是否已经覆盖到今日或最近一日。"""
    if not bars:
        return False
    last_dt = _parse_bar_date(str(bars[-1].get("date") or ""))
    if last_dt is None:
        return False
    return (datetime.now().date() - last_dt.date()).days <= 1


def _fetch_fuyao_bars(api_path: str, params: dict[str, Any]) -> list[dict]:
    """调用扶摇历史 K 接口并标准化为内部 bars。"""
    data = _fuyao_get(api_path, params, timeout=25)
    bars: list[dict] = []
    for item in data.get("item") or []:
        close = _to_float(item.get("close_price"))
        open_price = _to_float(item.get("open_price"))
        high = _to_float(item.get("high_price"))
        low = _to_float(item.get("low_price"))
        if close is None or open_price is None or high is None or low is None:
            continue
        date_ms = item.get("date_ms")
        date = ""
        if date_ms is not None:
            date = datetime.fromtimestamp(int(date_ms) / 1000).strftime("%Y-%m-%d")
        bars.append(
            {
                "date": date,
                "date_ms": date_ms,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": _to_float(item.get("volume")),
                "turnover": _to_float(item.get("turnover")),
            }
        )
    return sorted(bars, key=lambda x: x.get("date_ms") or 0)


def _history_window_ms(cached: list[dict], lookback_calendar_days: int) -> tuple[int, int]:
    """生成历史 K 请求窗口，支持增量刷新。"""
    end_dt = datetime.now() + timedelta(days=1)
    start_dt = end_dt - timedelta(days=lookback_calendar_days)
    if cached:
        last_dt = _parse_bar_date(str(cached[-1].get("date") or ""))
        if last_dt is not None:
            overlap_days = int(_cfg("data", "refresh_overlap_days", 7))
            start_dt = max(start_dt, last_dt - timedelta(days=overlap_days))
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def _fetch_fuyao_ticker_map(limit: int = 10000) -> dict[str, dict]:
    """拉取 A 股代码表，用于补充中文名。"""
    result: dict[str, dict] = {}
    offset = 0
    while True:
        data = _fuyao_get(
            "/api/meta/tickers/list",
            {"asset_type": "a-share", "limit": limit, "offset": offset},
        )
        items = data.get("item") or []
        for item in items:
            thscode = item.get("thscode")
            if thscode:
                result[thscode] = item
        if len(items) < limit:
            break
        offset += limit
    return result


def _fetch_fuyao_snapshots(page_size: int = 1000, max_pages: int = 8) -> tuple[list[dict], Optional[int]]:
    """分页拉取扶摇 A 股全市场快照。"""
    rows: list[dict] = []
    timestamp: Optional[int] = None
    offset = 0
    total: Optional[int] = None
    for _ in range(max_pages):
        data = _fuyao_get(
            "/api/a-share/prices/snapshot",
            {"limit": page_size, "offset": offset},
        )
        timestamp = data.get("timestamp") or timestamp
        total = data.get("total") or total
        items = data.get("item") or []
        rows.extend(items)
        if len(items) < page_size:
            break
        offset += page_size
        if total is not None and offset >= total:
            break
    return rows, timestamp


def _fetch_fuyao_index_snapshots() -> list[dict]:
    """拉取主要指数行情快照。"""
    thscodes = ",".join(code for _, code in DEFAULT_INDEXES)
    data = _fuyao_get("/api/a-share-index/prices/snapshot", {"thscodes": thscodes})
    label_map = {code: label for label, code in DEFAULT_INDEXES}
    indexes = []
    for item in data.get("item") or []:
        item["label"] = label_map.get(item.get("thscode"), item.get("thscode"))
        indexes.append(item)
    return indexes


def _fetch_fuyao_historical(thscode: str, lookback_calendar_days: int = 260) -> list[dict]:
    """拉取单只 A 股历史日 K，用于恢复多日趋势和回测。"""
    lookback_calendar_days = int(_cfg("data", "stock_lookback_calendar_days", lookback_calendar_days))
    cached = _read_bars_cache("ohlcv", thscode) if _cfg("data", "cache_enabled", True) else []
    if cached and _cache_is_fresh(cached):
        return cached
    start_ms, end_ms = _history_window_ms(cached, lookback_calendar_days)
    try:
        fresh = _fetch_fuyao_bars(
            "/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "start": start_ms,
                "end": end_ms,
                "adjust": "none",
            },
        )
        bars = _merge_bars(cached, fresh)
        _write_bars_cache("ohlcv", thscode, bars)
        return bars
    except Exception:
        if cached and _cfg("data", "stale_cache_allowed", True):
            logger.warning(f"股票历史K线接口失败，使用本地缓存: {thscode}")
            return cached
        raise


def _fetch_fuyao_index_historical(thscode: str, lookback_calendar_days: int = 260) -> list[dict]:
    """拉取单只指数历史日 K，用于市场环境过滤。"""
    lookback_calendar_days = int(_cfg("data", "index_lookback_calendar_days", lookback_calendar_days))
    cached = _read_bars_cache("index", thscode) if _cfg("data", "cache_enabled", True) else []
    if cached and _cache_is_fresh(cached):
        return cached
    start_ms, end_ms = _history_window_ms(cached, lookback_calendar_days)
    try:
        fresh = _fetch_fuyao_bars(
            "/api/a-share-index/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "start": start_ms,
                "end": end_ms,
            },
        )
        bars = _merge_bars(cached, fresh)
        _write_bars_cache("index", thscode, bars)
        return bars
    except Exception:
        if cached and _cfg("data", "stale_cache_allowed", True):
            logger.warning(f"指数历史K线接口失败，使用本地缓存: {thscode}")
            return cached
        raise


def _series_return(values: list[float], window: int) -> Optional[float]:
    """计算窗口收益率，数据不足时返回 None。"""
    if len(values) <= window or values[-window - 1] == 0:
        return None
    return values[-1] / values[-window - 1] - 1


def _moving_average(values: list[float], window: int) -> Optional[float]:
    """计算简单移动平均。"""
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _daily_returns(values: list[float]) -> list[float]:
    """计算相邻收盘价收益率序列。"""
    returns: list[float] = []
    for prev, curr in zip(values, values[1:]):
        if prev:
            returns.append(curr / prev - 1)
    return returns


def _enrich_with_history(row: dict, bars: list[dict]) -> dict:
    """用历史 K 线补齐多日涨幅、均线、均量和波动率字段。"""
    if len(bars) < 30:
        row["history_enriched"] = False
        row["history_error"] = f"历史K线不足 {len(bars)} 条"
        return row

    closes = [bar["close"] for bar in bars if _to_float(bar.get("close")) is not None]
    highs = [bar["high"] for bar in bars if _to_float(bar.get("high")) is not None]
    lows = [bar["low"] for bar in bars if _to_float(bar.get("low")) is not None]
    turnovers = [bar["turnover"] for bar in bars if _to_float(bar.get("turnover")) is not None]
    latest = _to_float(row.get("latest"))
    turnover = _to_float(row.get("turnover"))

    calc_closes = list(closes)
    if latest is not None and calc_closes and abs(latest / calc_closes[-1] - 1) > 0.001:
        calc_closes.append(latest)

    calc_turnovers = list(turnovers)
    if turnover is not None and calc_turnovers and abs(turnover / calc_turnovers[-1] - 1) > 0.001:
        calc_turnovers.append(turnover)

    returns = _daily_returns(calc_closes)
    avg_turnover_20d = _moving_average(calc_turnovers, 20)
    volume_ratio = None
    if turnover is not None and avg_turnover_20d:
        volume_ratio = turnover / avg_turnover_20d

    volatility_20d = None
    if len(returns) >= 10:
        recent_returns = returns[-20:]
        if len(recent_returns) >= 2:
            volatility_20d = stdev(recent_returns)

    high_20d = max(highs[-20:]) if len(highs) >= 20 else None
    low_20d = min(lows[-20:]) if len(lows) >= 20 else None
    close_to_20d_high = latest / high_20d - 1 if latest is not None and high_20d else None

    row.update(
        {
            "change_rate_5d": _series_return(calc_closes, 5),
            "change_rate_20d": _series_return(calc_closes, 20),
            "change_rate_60d": _series_return(calc_closes, 60),
            "ma5": _moving_average(calc_closes, 5),
            "ma10": _moving_average(calc_closes, 10),
            "ma20": _moving_average(calc_closes, 20),
            "avg_turnover_20d": avg_turnover_20d,
            "volume_ratio": volume_ratio,
            "volatility_20d": volatility_20d,
            "high_20d": high_20d,
            "low_20d": low_20d,
            "close_to_20d_high": close_to_20d_high,
            "history_days": len(bars),
            "history_enriched": True,
            "history_error": "",
        }
    )
    return row


def _enrich_index_with_history(row: dict, bars: list[dict]) -> dict:
    """用指数历史 K 线补齐趋势、均线和波动率字段。"""
    if len(bars) < 30:
        row["index_history_enriched"] = False
        row["index_history_error"] = f"指数历史K线不足 {len(bars)} 条"
        return row

    closes = [bar["close"] for bar in bars if _to_float(bar.get("close")) is not None]
    latest = _to_float(row.get("latest"))
    calc_closes = list(closes)
    if latest is not None and calc_closes and abs(latest / calc_closes[-1] - 1) > 0.001:
        calc_closes.append(latest)

    returns = _daily_returns(calc_closes)
    volatility_20d = None
    if len(returns) >= 2:
        volatility_20d = stdev(returns[-20:]) if len(returns[-20:]) >= 2 else None
    ma20 = _moving_average(calc_closes, 20)
    ma60 = _moving_average(calc_closes, 60)
    current = latest if latest is not None else calc_closes[-1]

    row.update(
        {
            "change_rate_5d": _series_return(calc_closes, 5),
            "change_rate_20d": _series_return(calc_closes, 20),
            "change_rate_60d": _series_return(calc_closes, 60),
            "ma20": ma20,
            "ma60": ma60,
            "above_ma20": current >= ma20 if ma20 is not None else None,
            "above_ma60": current >= ma60 if ma60 is not None else None,
            "volatility_20d": volatility_20d,
            "history_days": len(bars),
            "index_history_enriched": True,
            "index_history_error": "",
            "_history_bars": bars,
        }
    )
    return row


def _normalize_fuyao_snapshot(row: dict, ticker_map: dict[str, dict]) -> dict:
    """将扶摇快照转换为扫描器内部字段。"""
    thscode = row.get("thscode") or ""
    meta = ticker_map.get(thscode) or {}
    last = _to_float(row.get("last_price"))
    prev = _to_float(row.get("prev_price"))
    high = _to_float(row.get("high_price"))
    low = _to_float(row.get("low_price"))
    pct = _to_float(row.get("price_change_ratio_pct"))
    amplitude = None
    if prev and prev > 0 and high is not None and low is not None:
        amplitude = (high - low) / prev
    return {
        "name": meta.get("name") or thscode,
        "symkey": _symbol_fuyao_to_scan(thscode),
        "symbol_id": row.get("ticker"),
        "latest": last,
        "close": last,
        "open": row.get("open_price"),
        "high": high,
        "low": low,
        "prev_close": prev,
        "change": row.get("price_change"),
        "change_rate": pct / 100 if pct is not None else None,
        "turnover": row.get("turnover"),
        "volume": row.get("volume"),
        "amplitude": amplitude,
        "trading_status": "NORMAL",
        "industry_sector": {"name": meta.get("exchange") or "未分类"},
        "data_source": "fuyao",
    }


def _normalize_fuyao_index(row: dict) -> dict:
    """将扶摇指数快照转换为报告字段。"""
    pct = _to_float(row.get("price_change_ratio_pct"))
    high = _to_float(row.get("high_price"))
    low = _to_float(row.get("low_price"))
    prev = _to_float(row.get("prev_price"))
    amplitude = None
    if prev and prev > 0 and high is not None and low is not None:
        amplitude = (high - low) / prev
    return {
        "label": row.get("label"),
        "name": row.get("label"),
        "symkey": row.get("thscode"),
        "latest": row.get("last_price"),
        "turnover": row.get("turnover"),
        "amplitude": amplitude,
        "change_rate": pct / 100 if pct is not None else None,
    }


def _fetch_market_snapshot_fuyao() -> tuple[list[dict], list[dict], str]:
    """使用扶摇 API 拉取全市场快照与指数快照。"""
    ticker_map = _fetch_fuyao_ticker_map()
    snapshots, _timestamp = _fetch_fuyao_snapshots()
    stocks = [_normalize_fuyao_snapshot(row, ticker_map) for row in snapshots]
    indexes = []
    for row in _fetch_fuyao_index_snapshots():
        index = _normalize_fuyao_index(row)
        try:
            bars = _fetch_fuyao_index_historical(str(index.get("symkey")))
            _enrich_index_with_history(index, bars)
        except Exception as exc:
            logger.warning(f"指数历史K线补齐失败 {index.get('label')}: {exc}")
            index["index_history_enriched"] = False
            index["index_history_error"] = str(exc)
        indexes.append(index)
    return stocks, indexes, "fuyao"


def _ftshare_runpy_path() -> Path:
    """定位 FTShare skill 的 run.py。"""
    configured = os.environ.get("FTSHARE_RUN_PY")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / ".codex" / "skills" / "ftshare-market-data" / "run.py",
    ]
    for path in candidates:
        if path and path.exists():
            return path
    raise FileNotFoundError("未找到 FTShare run.py，请先安装 ftshare-market-data 或设置 FTSHARE_RUN_PY")


def _run_ftshare(subskill: str, args: list[str], timeout: int = 30) -> dict:
    """调用 FTShare run.py 并解析 JSON。"""
    env = os.environ.copy()
    if certifi is not None:
        env.setdefault("SSL_CERT_FILE", certifi.where())
    proc = subprocess.run(
        ["python3", str(_ftshare_runpy_path()), subskill, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"FTShare {subskill} 失败: {stderr[-500:]}")
    return json.loads(proc.stdout)


def _fetch_quote_pages(order_by: str, pages: int = 3, page_size: int = 80) -> list[dict]:
    """按指定排序拉取 A 股行情分页。"""
    rows: list[dict] = []
    base_filter = '(ex_id = "XSHE" OR ex_id = "XSHG") AND (latest != null)'
    for page in range(1, pages + 1):
        payload = _run_ftshare(
            "stock-quotes-list",
            [
                "--order_by", order_by,
                "--page_no", str(page),
                "--page_size", str(page_size),
                "--filter", base_filter,
            ],
        )
        rows.extend(payload.get("stocks") or [])
    return rows


def fetch_market_snapshot() -> tuple[list[dict], list[dict], str]:
    """
    拉取行情样本与指数快照。

    Returns:
        (stocks, indexes, source)
    """
    try:
        return _fetch_market_snapshot_fuyao()
    except Exception as exc:
        logger.warning(f"扶摇数据源不可用，尝试 FTShare fallback: {exc}")

    combined: dict[str, dict] = {}
    for order_by, pages in [
        ("turnover desc", 4),
        ("change_rate desc", 3),
        ("change_rate_5d desc", 2),
    ]:
        for row in _fetch_quote_pages(order_by, pages=pages):
            symkey = row.get("symkey")
            if symkey:
                combined[symkey] = row

    indexes: list[dict] = []
    masks = "name,symkey,latest,change_rate,change_rate_5d,change_rate_20d,turnover,amplitude"
    ftshare_index_codes = {
        "000001.SH": "000001.XSHG",
        "399001.SZ": "399001.XSHE",
        "399006.SZ": "399006.XSHE",
        "000300.SH": "000300.XSHG",
    }
    for label, code in DEFAULT_INDEXES:
        ftshare_code = ftshare_index_codes.get(code, code)
        try:
            item = _run_ftshare("index-detail", ["--index", ftshare_code, "--masks", masks])
            item["label"] = label
            indexes.append(item)
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"指数 {label} 获取失败: {exc}")
    return list(combined.values()), indexes, "ftshare"
