"""
market_scanner.py - FTShare 收盘扫描与研究候选汇报

输出：output/YYYY-MM-DD/scan/
  - daily_scans.csv
  - research_candidates.csv
  - simulated_trades.csv
  - excluded_watchlist.csv
  - memory.md
  - scan_report.md

本模块输出个人模拟交易计划。所有参数由固定规则生成，需人工复核。
"""

import argparse
import csv
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median, stdev
from typing import Any, Optional

import requests

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None

logger = logging.getLogger(__name__)

DEFAULT_INDEXES = [
    ("上证", "000001.SH"),
    ("深成指", "399001.SZ"),
    ("创业板", "399006.SZ"),
    ("沪深300", "000300.SH"),
]

FUYAO_BASE_URL = "https://fuyao.aicubes.cn"

DEFAULT_SCANNER_CONFIG: dict[str, Any] = {
    "data": {
        "enrich_limit": 120,
        "stock_lookback_calendar_days": 260,
        "index_lookback_calendar_days": 260,
        "cache_enabled": True,
        "cache_dir": "data/cache/market_scanner",
        "refresh_overlap_days": 7,
        "stale_cache_allowed": True,
    },
    "filters": {
        "min_price": 3,
        "min_turnover": 1e9,
        "min_market_cap": 5e10,
        "max_change_rate": 0.095,
        "max_amplitude": 0.12,
        "max_5d_return": 0.28,
        "max_60d_return": 1.5,
        "max_volatility_20d": 0.09,
        "max_volume_ratio": 5,
        "min_close_to_20d_high": -0.12,
    },
    "trade_plan": {
        "base_position_pct": 8,
        "high_amplitude_position_pct": 5,
        "hot_5d_position_pct": 5,
        "hot_day_position_pct": 4,
        "pullback_factor": 0.35,
        "pullback_min": 0.008,
        "pullback_max": 0.025,
        "chase_factor": 0.12,
        "chase_min": 0.003,
        "chase_max": 0.012,
        "stop_factor": 0.75,
        "stop_min": 0.035,
        "stop_max": 0.08,
        "reward_risk": 1.6,
        "high_amplitude_threshold": 0.09,
        "hot_5d_threshold": 0.14,
        "hot_day_threshold": 0.07,
    },
    "market_regimes": {
        "risk_on": {
            "min_score": 70,
            "candidate_limit": 5,
            "position_multiplier": 1.0,
            "max_position_pct": 8,
            "min_final_score": 88,
        },
        "neutral": {
            "min_score": 50,
            "candidate_limit": 4,
            "position_multiplier": 0.8,
            "max_position_pct": 6,
            "min_final_score": 90,
        },
        "cautious": {
            "min_score": 35,
            "candidate_limit": 3,
            "position_multiplier": 0.6,
            "max_position_pct": 5,
            "min_final_score": 92,
        },
        "defensive": {
            "candidate_limit": 1,
            "position_multiplier": 0.35,
            "max_position_pct": 3,
            "min_final_score": 95,
        },
    },
    "backtest": {
        "min_score": 70,
        "max_hold_days": 5,
        "entry_window_days": 2,
        "cost_bps": 15,
        "stop_slippage_bps": 30,
        "portfolio_initial_cash": 1_000_000,
        "portfolio_max_positions": 4,
        "portfolio_max_total_exposure_pct": 0.32,
        "portfolio_default_position_pct": 0.08,
    },
    "ledger": {
        "dir": "data/ledger",
        "pending_file": "pending_trades.csv",
        "archive_file": "trade_archive.csv",
        "same_symbol_active_policy": "skip",
    },
    "review": {
        "enabled": True,
        "max_report_items": 8,
    },
    "strategy_health": {
        "enabled": True,
        "windows": [5, 20, 60],
        "history_days": 120,
        "min_sample": 10,
        "max_tag_rows": 12,
    },
    "strategy_steward": {
        "enabled": False,
        "mode": "daily",
        "script": "scripts/run_strategy_steward.sh",
        "fail_on_error": False,
        "timeout_seconds": 900,
    },
    "strategy_learning": {
        "enabled": True,
        "memory_dir": "data/strategy_learning",
    },
}

ACTIVE_SCANNER_CONFIG: dict[str, Any] = DEFAULT_SCANNER_CONFIG.copy()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并配置。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_scanner_config(config_path: str = "strategy.json") -> dict[str, Any]:
    """读取 market_scanner 配置，缺失字段使用默认值。"""
    path = Path(config_path)
    config = DEFAULT_SCANNER_CONFIG
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        scanner_payload = payload.get("market_scanner") or {}
        if isinstance(scanner_payload, dict):
            config = _deep_merge(DEFAULT_SCANNER_CONFIG, scanner_payload)
    return config


def _set_active_config(config: dict[str, Any]) -> None:
    """设置本次运行配置。"""
    global ACTIVE_SCANNER_CONFIG
    ACTIVE_SCANNER_CONFIG = config


def _cfg(section: str, key: str, default: Any = None) -> Any:
    """读取本次运行配置。"""
    return (ACTIVE_SCANNER_CONFIG.get(section) or {}).get(key, default)


def _to_float(value: Any) -> Optional[float]:
    """安全转换为 float。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: Optional[float], digits: int = 2) -> str:
    """将小数涨跌幅格式化为百分比。"""
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%"


def _fmt_yi(value: Optional[float], digits: int = 1) -> str:
    """将元格式化为亿元。"""
    if value is None:
        return "-"
    return f"{value / 1e8:.{digits}f}亿"


def _fmt_source(source: str) -> str:
    """数据源展示名。"""
    mapping = {
        "fuyao": "同花顺扶摇 API / fuyao.aicubes.cn",
        "ftshare": "FTShare-market-data / market.ft.tech",
    }
    return mapping.get(source, source)


def _symbol_cn_suffix(symkey: str) -> str:
    """将 FTShare symkey 转为常见 SH/SZ/BJ 后缀。"""
    return symkey.replace(".XSHG", ".SH").replace(".XSHE", ".SZ").replace(".BJSE", ".BJ")


def _symbol_fuyao_to_scan(thscode: str) -> str:
    """扶摇 thscode 已是常见 SH/SZ/BJ 后缀，保持原样。"""
    return thscode


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
        headers={"X-api-key": key, "User-Agent": "claude-trading-agents/1.0"},
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


def _enrich_fuyao_rows_with_history(rows: list[dict], enrich_limit: int) -> tuple[list[dict], dict[str, list[dict]]]:
    """对扶摇快照样本做历史 K 线补齐，避免全市场逐只请求。"""
    initial_scored = sorted([_score_stock(row) for row in rows], key=lambda x: x["score"], reverse=True)
    by_symbol = {row.get("symkey"): row for row in rows if row.get("symkey")}
    turnover_ranked = sorted(
        rows,
        key=lambda x: _to_float(x.get("turnover")) or 0,
        reverse=True,
    )

    seed_symbols: list[str] = []
    for item in initial_scored:
        symbol = item.get("symbol")
        if symbol and symbol in by_symbol and symbol not in seed_symbols:
            seed_symbols.append(symbol)
        if len(seed_symbols) >= enrich_limit:
            break
    for row in turnover_ranked:
        symbol = row.get("symkey")
        if symbol and symbol not in seed_symbols:
            seed_symbols.append(symbol)
        if len(seed_symbols) >= enrich_limit:
            break

    histories: dict[str, list[dict]] = {}
    for idx, symbol in enumerate(seed_symbols, start=1):
        try:
            bars = _fetch_fuyao_historical(symbol)
            histories[symbol] = bars
            _enrich_with_history(by_symbol[symbol], bars)
        except Exception as exc:
            logger.warning(f"历史K线补齐失败 {symbol} ({idx}/{len(seed_symbols)}): {exc}")
            by_symbol[symbol]["history_enriched"] = False
            by_symbol[symbol]["history_error"] = str(exc)
    return rows, histories


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


def _score_stock(row: dict) -> dict:
    """对单只股票计算研究观察分数。"""
    latest = _to_float(row.get("latest"))
    change_rate = _to_float(row.get("change_rate"))
    change_rate_5d = _to_float(row.get("change_rate_5d"))
    change_rate_20d = _to_float(row.get("change_rate_20d"))
    change_rate_60d = _to_float(row.get("change_rate_60d"))
    turnover = _to_float(row.get("turnover"))
    turnover_rate = _to_float(row.get("turnover_rate"))
    amplitude = _to_float(row.get("amplitude"))
    market_cap = _to_float(row.get("market_cap_total"))
    ma5 = _to_float(row.get("ma5"))
    ma10 = _to_float(row.get("ma10"))
    ma20 = _to_float(row.get("ma20"))
    volume_ratio = _to_float(row.get("volume_ratio"))
    volatility_20d = _to_float(row.get("volatility_20d"))
    close_to_20d_high = _to_float(row.get("close_to_20d_high"))
    status = row.get("trading_status")

    filter_reasons: list[str] = []
    if status != "NORMAL":
        filter_reasons.append(f"交易状态={status}")
    if row.get("data_source") == "fuyao" and not row.get("history_enriched"):
        filter_reasons.append("历史K线未补齐")
    if latest is None or latest <= _cfg("filters", "min_price", 3):
        filter_reasons.append("价格过低或缺失")
    if turnover is None or turnover < _cfg("filters", "min_turnover", 1e9):
        filter_reasons.append("成交额不足 10 亿")
    if market_cap is not None and market_cap < _cfg("filters", "min_market_cap", 5e10):
        filter_reasons.append("总市值不足 500 亿")
    if change_rate is None or change_rate <= 0:
        filter_reasons.append("当日未转强")
    if change_rate is not None and change_rate >= _cfg("filters", "max_change_rate", 0.095):
        filter_reasons.append("接近涨停或当日涨幅过高")
    if amplitude is not None and amplitude >= _cfg("filters", "max_amplitude", 0.12):
        filter_reasons.append("振幅过大")
    if change_rate_5d is not None and change_rate_5d >= _cfg("filters", "max_5d_return", 0.28):
        filter_reasons.append("5 日涨幅过高")
    if change_rate_60d is not None and change_rate_60d >= _cfg("filters", "max_60d_return", 1.5):
        filter_reasons.append("60 日涨幅过高")
    if latest is not None and ma20 is not None and latest < ma20:
        filter_reasons.append("未站上20日均线")
    if volatility_20d is not None and volatility_20d >= _cfg("filters", "max_volatility_20d", 0.09):
        filter_reasons.append("20日波动率过高")
    if volume_ratio is not None and volume_ratio >= _cfg("filters", "max_volume_ratio", 5):
        filter_reasons.append("当日放量过急")
    if close_to_20d_high is not None and close_to_20d_high < _cfg("filters", "min_close_to_20d_high", -0.12):
        filter_reasons.append("距离20日高点过远")

    score = 0.0
    if turnover is not None:
        score += min(turnover / 1.5e10, 1.0) * 22
    if market_cap is not None:
        score += min(market_cap / 3e11, 1.0) * 12
    else:
        score += 6
    if change_rate is not None:
        if 0.005 <= change_rate <= 0.055:
            score += 24
        elif 0.055 < change_rate < 0.085:
            score += 16
        elif 0 < change_rate < 0.005:
            score += 8
    if change_rate_5d is not None:
        if 0.015 <= change_rate_5d <= 0.14:
            score += 20
        elif -0.02 <= change_rate_5d < 0.015:
            score += 8
    else:
        score += 8
    if change_rate_20d is not None:
        if 0 <= change_rate_20d <= 0.35:
            score += 10
        elif change_rate_20d > 0.35:
            score += 3
    else:
        score += 4
    if amplitude is not None:
        if amplitude <= 0.06:
            score += 8
        elif amplitude <= 0.09:
            score += 4
    if turnover_rate is not None:
        if 0.01 <= turnover_rate <= 0.08:
            score += 4
        elif turnover_rate > 0.12:
            score -= 6
    if latest is not None and ma20 is not None:
        if latest >= ma20:
            score += 6
        if ma5 is not None and ma10 is not None and ma5 >= ma10 >= ma20:
            score += 8
        elif ma5 is not None and ma5 >= ma20:
            score += 4
    if volume_ratio is not None:
        if 0.8 <= volume_ratio <= 2.8:
            score += 7
        elif 2.8 < volume_ratio < 5:
            score += 3
    if volatility_20d is not None:
        if volatility_20d <= 0.04:
            score += 5
        elif volatility_20d <= 0.07:
            score += 2
    if close_to_20d_high is not None:
        if -0.04 <= close_to_20d_high <= 0.015:
            score += 7
        elif -0.08 <= close_to_20d_high < -0.04:
            score += 3

    reasons: list[str] = []
    if turnover is not None:
        reasons.append(f"成交额 {_fmt_yi(turnover)}")
    if change_rate is not None:
        reasons.append(f"日涨跌 {_fmt_pct(change_rate)}")
    if change_rate_5d is not None:
        reasons.append(f"5日 {_fmt_pct(change_rate_5d)}")
    if change_rate_20d is not None:
        reasons.append(f"20日 {_fmt_pct(change_rate_20d)}")
    if amplitude is not None:
        reasons.append(f"振幅 {_fmt_pct(amplitude)}")
    if volume_ratio is not None:
        reasons.append(f"量比20日均额 {volume_ratio:.1f}x")
    if close_to_20d_high is not None:
        reasons.append(f"距20日高点 {_fmt_pct(close_to_20d_high)}")

    risk_notes: list[str] = []
    if change_rate is not None and change_rate > 0.07:
        risk_notes.append("当日涨幅偏高，需观察后续承接")
    if amplitude is not None and amplitude > 0.09:
        risk_notes.append("振幅偏大")
    if change_rate_5d is not None and change_rate_5d > 0.16:
        risk_notes.append("短期涨幅已较高")
    if change_rate_60d is not None and change_rate_60d > 0.8:
        risk_notes.append("中期涨幅较大")
    if volume_ratio is not None and volume_ratio > 3:
        risk_notes.append("量能短期放大，次日承接需复核")
    if volatility_20d is not None and volatility_20d > 0.06:
        risk_notes.append("20日波动偏高")
    if close_to_20d_high is not None and close_to_20d_high < -0.08:
        risk_notes.append("尚未接近20日强势区间")
    if not risk_notes:
        risk_notes.append("未触发主要波动风险项")

    sector = row.get("industry_sector") or {}
    return {
        "symbol": _symbol_cn_suffix(str(row.get("symkey") or "")),
        "name": row.get("name"),
        "sector": sector.get("name"),
        "latest": latest,
        "change_rate": change_rate,
        "change_rate_5d": change_rate_5d,
        "change_rate_20d": change_rate_20d,
        "change_rate_60d": change_rate_60d,
        "turnover": turnover,
        "turnover_rate": turnover_rate,
        "amplitude": amplitude,
        "market_cap": market_cap,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "volume_ratio": volume_ratio,
        "volatility_20d": volatility_20d,
        "close_to_20d_high": close_to_20d_high,
        "history_days": row.get("history_days"),
        "history_enriched": row.get("history_enriched"),
        "score": round(score, 1),
        "passed": len(filter_reasons) == 0,
        "filter_reasons": "；".join(filter_reasons),
        "reasons": "；".join(reasons),
        "risk_notes": "；".join(risk_notes),
    }


def _build_trade_plan(item: dict) -> dict:
    """
    由固定规则生成模拟交易参数。

    触发区间、止损、第一止盈和仓位来自振幅、短期涨幅和最新价，不做主观预测。
    """
    latest = item.get("latest") or 0.0
    amplitude = item.get("amplitude") or 0.05
    change_rate = item.get("change_rate") or 0.0
    change_rate_5d = item.get("change_rate_5d") or 0.0

    pullback_pct = min(
        max(amplitude * _cfg("trade_plan", "pullback_factor", 0.35), _cfg("trade_plan", "pullback_min", 0.008)),
        _cfg("trade_plan", "pullback_max", 0.025),
    )
    chase_pct = min(
        max(amplitude * _cfg("trade_plan", "chase_factor", 0.12), _cfg("trade_plan", "chase_min", 0.003)),
        _cfg("trade_plan", "chase_max", 0.012),
    )
    stop_pct = min(
        max(amplitude * _cfg("trade_plan", "stop_factor", 0.75), _cfg("trade_plan", "stop_min", 0.035)),
        _cfg("trade_plan", "stop_max", 0.08),
    )

    trigger_low = latest * (1 - pullback_pct)
    trigger_high = latest * (1 + chase_pct)
    stop_loss = latest * (1 - stop_pct)
    risk_per_share = max(latest - stop_loss, latest * 0.02)
    reward_risk = _cfg("trade_plan", "reward_risk", 1.6)
    first_take_profit = latest + risk_per_share * reward_risk

    position_pct = int(_cfg("trade_plan", "base_position_pct", 8))
    if amplitude >= _cfg("trade_plan", "high_amplitude_threshold", 0.09):
        position_pct = int(_cfg("trade_plan", "high_amplitude_position_pct", 5))
    if change_rate_5d >= _cfg("trade_plan", "hot_5d_threshold", 0.14):
        position_pct = min(position_pct, int(_cfg("trade_plan", "hot_5d_position_pct", 5)))
    if change_rate >= _cfg("trade_plan", "hot_day_threshold", 0.07):
        position_pct = min(position_pct, int(_cfg("trade_plan", "hot_day_position_pct", 4)))

    if amplitude >= _cfg("trade_plan", "high_amplitude_threshold", 0.09):
        plan = "只看回踩承接，不追高；若次日振幅继续扩大则降级观察"
    elif change_rate_5d >= _cfg("trade_plan", "hot_5d_threshold", 0.14):
        plan = "短期涨幅较高，只在回踩区间企稳时试探；高开不追"
    else:
        plan = "优先观察回踩区间承接；若放量突破触发上沿再复核"

    return {
        **item,
        "signal_price": round(latest, 2),
        "trigger_zone": f"{trigger_low:.2f}-{trigger_high:.2f}",
        "stop_loss": round(stop_loss, 2),
        "first_take_profit": round(first_take_profit, 2),
        "position_pct": position_pct,
        "plan": plan,
        "risk_per_share": round(risk_per_share, 2),
        "reward_risk": reward_risk,
    }


def _select_candidates(scored: list[dict], limit: int = 5, market_profile: Optional[dict] = None) -> list[dict]:
    """选择候选，限制单行业过度集中。"""
    selected: list[dict] = []
    sector_counts: dict[str, int] = {}
    effective_limit = min(limit, int(market_profile.get("candidate_limit", limit))) if market_profile else limit
    min_final_score = _to_float(market_profile.get("min_final_score")) if market_profile else None
    for item in sorted(scored, key=lambda x: x.get("final_score", x["score"]), reverse=True):
        if not item["passed"]:
            continue
        if min_final_score is not None and (item.get("final_score") or item["score"]) < min_final_score:
            continue
        sector = item.get("sector") or "unknown"
        if sector_counts.get(sector, 0) >= 2:
            continue
        selected.append(item)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= effective_limit:
            break
    candidates = [_build_trade_plan(item) for item in selected]
    return _apply_market_risk_controls(candidates, market_profile) if market_profile else candidates


def _excluded_trade_examples(scored: list[dict], limit: int = 30) -> list[dict]:
    """选择容易误判但暂不纳入的样例。"""
    return [
        item for item in scored
        if not item["passed"] and (
            "涨幅过高" in item["filter_reasons"]
            or "振幅过大" in item["filter_reasons"]
            or "5 日涨幅过高" in item["filter_reasons"]
            or "60 日涨幅过高" in item["filter_reasons"]
        )
    ][:limit]


def _market_comment(indexes: list[dict], scored: list[dict], market_profile: Optional[dict] = None) -> str:
    """生成大盘研究性描述。"""
    idx_parts = []
    for item in indexes:
        idx_parts.append(f"{item.get('label')}{_fmt_pct(_to_float(item.get('change_rate')))}")
    passed = [x for x in scored if x["passed"]]
    changes = [x["change_rate"] for x in scored if x.get("change_rate") is not None]
    up_ratio = sum(1 for x in changes if x > 0) / len(changes) if changes else 0
    median_change = median(changes) if changes else None
    hot_sectors: dict[str, int] = {}
    for item in passed[:60]:
        sector = item.get("sector") or "unknown"
        hot_sectors[sector] = hot_sectors.get(sector, 0) + 1
    top_sectors = "、".join([k for k, _ in sorted(hot_sectors.items(), key=lambda x: x[1], reverse=True)[:3]])
    comment = (
        f"指数表现：{ '，'.join(idx_parts) if idx_parts else '指数数据缺失' }。"
        f"高成交额/强势样本中上涨占比约 {up_ratio * 100:.0f}%，中位涨跌为 {_fmt_pct(median_change)}。"
        f"通过初筛的方向集中在 {top_sectors or '若干板块'}，整体更像结构性活跃，而非全面扩散。"
    )
    if market_profile:
        comment += (
            f" 市场环境评级为 {market_profile.get('regime_label')}，"
            f"候选上限 {market_profile.get('candidate_limit')} 只，"
            f"仓位系数 {market_profile.get('position_multiplier')}。"
        )
    return comment


def _classify_market_regime(score: float) -> dict:
    """把市场环境分数映射为候选和仓位规则。"""
    regimes = ACTIVE_SCANNER_CONFIG.get("market_regimes") or {}
    risk_on = regimes.get("risk_on") or {}
    neutral = regimes.get("neutral") or {}
    cautious = regimes.get("cautious") or {}
    defensive = regimes.get("defensive") or {}
    if score >= risk_on.get("min_score", 70):
        return {
            "regime": "risk_on",
            "regime_label": "积极",
            "candidate_limit": risk_on.get("candidate_limit", 5),
            "position_multiplier": risk_on.get("position_multiplier", 1.0),
            "max_position_pct": risk_on.get("max_position_pct", 8),
            "min_final_score": risk_on.get("min_final_score", 88),
        }
    if score >= neutral.get("min_score", 50):
        return {
            "regime": "neutral",
            "regime_label": "中性",
            "candidate_limit": neutral.get("candidate_limit", 4),
            "position_multiplier": neutral.get("position_multiplier", 0.8),
            "max_position_pct": neutral.get("max_position_pct", 6),
            "min_final_score": neutral.get("min_final_score", 90),
        }
    if score >= cautious.get("min_score", 35):
        return {
            "regime": "cautious",
            "regime_label": "谨慎",
            "candidate_limit": cautious.get("candidate_limit", 3),
            "position_multiplier": cautious.get("position_multiplier", 0.6),
            "max_position_pct": cautious.get("max_position_pct", 5),
            "min_final_score": cautious.get("min_final_score", 92),
        }
    return {
        "regime": "defensive",
        "regime_label": "防守",
        "candidate_limit": defensive.get("candidate_limit", 1),
        "position_multiplier": defensive.get("position_multiplier", 0.35),
        "max_position_pct": defensive.get("max_position_pct", 3),
        "min_final_score": defensive.get("min_final_score", 95),
    }


def _build_market_profile(indexes: list[dict], scored: list[dict]) -> dict:
    """根据指数趋势与全市场样本状态生成市场环境过滤规则。"""
    changes = [x["change_rate"] for x in scored if x.get("change_rate") is not None]
    up_ratio = sum(1 for x in changes if x > 0) / len(changes) if changes else None
    median_change = median(changes) if changes else None
    enriched = [x for x in scored if x.get("history_enriched")]
    passed = [x for x in scored if x.get("passed")]
    strong_count = sum(1 for x in passed if (x.get("final_score") or x.get("score") or 0) >= 92)
    strong_ratio = strong_count / len(enriched) if enriched else 0

    above_ma20_values = [x.get("above_ma20") for x in indexes if x.get("above_ma20") is not None]
    above_ma60_values = [x.get("above_ma60") for x in indexes if x.get("above_ma60") is not None]
    index_changes = [_to_float(x.get("change_rate")) for x in indexes if _to_float(x.get("change_rate")) is not None]
    index_vols = [_to_float(x.get("volatility_20d")) for x in indexes if _to_float(x.get("volatility_20d")) is not None]
    above_ma20_ratio = sum(1 for x in above_ma20_values if x) / len(above_ma20_values) if above_ma20_values else None
    above_ma60_ratio = sum(1 for x in above_ma60_values if x) / len(above_ma60_values) if above_ma60_values else None
    avg_index_change = sum(index_changes) / len(index_changes) if index_changes else None
    avg_index_volatility = sum(index_vols) / len(index_vols) if index_vols else None

    score = 0.0
    score += (above_ma20_ratio * 30) if above_ma20_ratio is not None else 12
    score += (above_ma60_ratio * 15) if above_ma60_ratio is not None else 6
    if avg_index_change is not None:
        if avg_index_change >= 0.008:
            score += 15
        elif avg_index_change > 0:
            score += 8
        elif avg_index_change <= -0.01:
            score -= 10
    if up_ratio is not None:
        if up_ratio >= 0.55:
            score += 25
        elif up_ratio >= 0.40:
            score += 12
        elif up_ratio >= 0.28:
            score += 4
        else:
            score -= 8
    if median_change is not None:
        if median_change >= 0:
            score += 10
        elif median_change > -0.01:
            score += 2
        else:
            score -= 6
    if strong_ratio >= 0.06:
        score += 10
    elif strong_count >= 2:
        score += 4
    if avg_index_volatility is not None:
        if avg_index_volatility <= 0.015:
            score += 5
        elif avg_index_volatility >= 0.03:
            score -= 8

    profile = _classify_market_regime(score)
    profile.update(
        {
            "score": round(score, 1),
            "up_ratio": round(up_ratio, 4) if up_ratio is not None else None,
            "median_change": round(median_change, 5) if median_change is not None else None,
            "strong_count": strong_count,
            "strong_ratio": round(strong_ratio, 4),
            "above_ma20_ratio": round(above_ma20_ratio, 4) if above_ma20_ratio is not None else None,
            "above_ma60_ratio": round(above_ma60_ratio, 4) if above_ma60_ratio is not None else None,
            "avg_index_change": round(avg_index_change, 5) if avg_index_change is not None else None,
            "avg_index_volatility": round(avg_index_volatility, 5) if avg_index_volatility is not None else None,
            "index_details": [
                {
                    "label": item.get("label"),
                    "change_rate": item.get("change_rate"),
                    "change_rate_20d": item.get("change_rate_20d"),
                    "above_ma20": item.get("above_ma20"),
                    "above_ma60": item.get("above_ma60"),
                    "volatility_20d": item.get("volatility_20d"),
                }
                for item in indexes
            ],
            "rule": "指数趋势 + 全市场上涨占比 + 强势样本密度 + 指数波动率共同决定候选上限、最低综合分和仓位系数。",
        }
    )
    return profile


def _apply_market_risk_controls(candidates: list[dict], market_profile: dict) -> list[dict]:
    """按市场环境调整模拟仓位并标注原因。"""
    multiplier = _to_float(market_profile.get("position_multiplier")) or 1.0
    max_position = int(market_profile.get("max_position_pct") or 8)
    regime_label = market_profile.get("regime_label") or "未知"
    for item in candidates:
        base_position = int(item.get("position_pct") or 0)
        raw_position = base_position * multiplier
        capped_position = min(max_position, raw_position)
        adjusted = min(max_position, max(1, int(round(capped_position))))
        effective_multiplier = adjusted / base_position if base_position else None
        item["base_position_pct"] = base_position
        item["raw_position_pct"] = round(raw_position, 2)
        item["position_cap_pct"] = max_position
        item["position_pct"] = adjusted
        item["market_regime"] = market_profile.get("regime")
        item["market_regime_label"] = regime_label
        item["market_position_multiplier"] = multiplier
        item["effective_position_multiplier"] = round(effective_multiplier, 4) if effective_multiplier is not None else None
        item["position_adjustment_note"] = (
            f"基础{base_position}% × 环境系数{multiplier} = {raw_position:.2f}%，"
            f"上限{max_position}%，实际{adjusted}%"
        )
        item["market_filter_note"] = (
            f"市场环境{regime_label}，候选上限{market_profile.get('candidate_limit')}，"
            f"仓位系数{multiplier}，实际系数{item['effective_position_multiplier']}"
        )
        if multiplier < 1:
            item["plan"] = f"{item.get('plan') or ''}；市场环境{regime_label}，仓位降级"
    return candidates


def _build_market_timeline(indexes: list[dict]) -> dict[str, dict]:
    """基于指数历史 K 线生成每日市场环境，用于回测信号过滤。"""
    daily_metrics: dict[str, list[dict]] = {}
    for index in indexes:
        bars = index.get("_history_bars") or []
        if len(bars) < 60:
            continue
        for idx in range(60, len(bars)):
            date = bars[idx].get("date")
            if not date:
                continue
            closes = [x["close"] for x in bars[: idx + 1]]
            returns = _daily_returns(closes)
            ma20 = _moving_average(closes, 20)
            ma60 = _moving_average(closes, 60)
            prev_close = bars[idx - 1]["close"] if idx > 0 else None
            change_rate = bars[idx]["close"] / prev_close - 1 if prev_close else None
            volatility_20d = stdev(returns[-20:]) if len(returns[-20:]) >= 2 else None
            daily_metrics.setdefault(date, []).append(
                {
                    "above_ma20": bars[idx]["close"] >= ma20 if ma20 is not None else None,
                    "above_ma60": bars[idx]["close"] >= ma60 if ma60 is not None else None,
                    "change_rate": change_rate,
                    "volatility_20d": volatility_20d,
                }
            )

    timeline: dict[str, dict] = {}
    for date, metrics in daily_metrics.items():
        above20 = [x["above_ma20"] for x in metrics if x.get("above_ma20") is not None]
        above60 = [x["above_ma60"] for x in metrics if x.get("above_ma60") is not None]
        changes = [x["change_rate"] for x in metrics if x.get("change_rate") is not None]
        vols = [x["volatility_20d"] for x in metrics if x.get("volatility_20d") is not None]
        above20_ratio = sum(1 for x in above20 if x) / len(above20) if above20 else None
        above60_ratio = sum(1 for x in above60 if x) / len(above60) if above60 else None
        avg_change = sum(changes) / len(changes) if changes else None
        avg_vol = sum(vols) / len(vols) if vols else None

        score = 0.0
        score += (above20_ratio * 40) if above20_ratio is not None else 15
        score += (above60_ratio * 25) if above60_ratio is not None else 8
        if avg_change is not None:
            if avg_change >= 0.008:
                score += 20
            elif avg_change > 0:
                score += 10
            elif avg_change <= -0.01:
                score -= 12
        if avg_vol is not None:
            if avg_vol <= 0.015:
                score += 8
            elif avg_vol >= 0.03:
                score -= 8
        profile = _classify_market_regime(score)
        profile.update(
            {
                "score": round(score, 1),
                "above_ma20_ratio": round(above20_ratio, 4) if above20_ratio is not None else None,
                "above_ma60_ratio": round(above60_ratio, 4) if above60_ratio is not None else None,
                "avg_index_change": round(avg_change, 5) if avg_change is not None else None,
                "avg_index_volatility": round(avg_vol, 5) if avg_vol is not None else None,
            }
        )
        timeline[date] = profile
    return timeline


def _market_allows_backtest_signal(market_profile: Optional[dict], score: float) -> bool:
    """回测中按历史市场环境过滤新信号。"""
    if not market_profile:
        return True
    regime = market_profile.get("regime")
    if regime == "defensive":
        return False
    if regime == "cautious":
        return score >= 85
    if regime == "neutral":
        return score >= 75
    return True


def _percentile_ranks(items: list[dict], field: str) -> dict[str, float]:
    """按字段计算横截面百分位排名，值越大排名越靠前。"""
    valid = [
        (item.get("symbol"), _to_float(item.get(field)))
        for item in items
        if item.get("symbol") and _to_float(item.get(field)) is not None
    ]
    valid.sort(key=lambda x: x[1], reverse=True)
    if not valid:
        return {}
    if len(valid) == 1:
        return {valid[0][0]: 1.0}
    return {symbol: 1 - idx / (len(valid) - 1) for idx, (symbol, _value) in enumerate(valid)}


def _apply_strategy_overlays(scored: list[dict]) -> list[dict]:
    """加入从开源策略中抽象出的透明规则标签和综合分。"""
    enriched = [item for item in scored if item.get("history_enriched")]
    rps20_map = _percentile_ranks(enriched, "change_rate_20d")
    rps60_map = _percentile_ranks(enriched, "change_rate_60d")

    for item in scored:
        symbol = item.get("symbol")
        rps20 = rps20_map.get(symbol)
        rps60 = rps60_map.get(symbol)
        latest = _to_float(item.get("latest"))
        ma5 = _to_float(item.get("ma5"))
        ma10 = _to_float(item.get("ma10"))
        ma20 = _to_float(item.get("ma20"))
        volume_ratio = _to_float(item.get("volume_ratio"))
        volatility_20d = _to_float(item.get("volatility_20d"))
        close_to_high = _to_float(item.get("close_to_20d_high"))
        change_rate_5d = _to_float(item.get("change_rate_5d"))
        change_rate_60d = _to_float(item.get("change_rate_60d"))

        trend_ok = latest is not None and ma5 is not None and ma10 is not None and ma20 is not None and latest >= ma20 and ma5 >= ma10 >= ma20
        volume_ok = volume_ratio is not None and 0.8 <= volume_ratio <= 2.8
        volatility_ok = volatility_20d is not None and volatility_20d <= 0.055
        near_breakout = close_to_high is not None and -0.04 <= close_to_high <= 0.015
        rps_ok = rps20 is not None and rps60 is not None and rps20 >= 0.65 and rps60 >= 0.45
        overheated = (change_rate_5d is not None and change_rate_5d > 0.16) or (change_rate_60d is not None and change_rate_60d > 1.2)

        strategy_score = 0.0
        tags: list[str] = []
        if rps20 is not None:
            strategy_score += rps20 * 30
        if rps60 is not None:
            strategy_score += rps60 * 18
        if rps_ok:
            tags.append("RPS动量")
        if trend_ok:
            strategy_score += 18
            tags.append("均线趋势")
        if volume_ok:
            strategy_score += 12
            tags.append("温和放量")
        if volatility_ok:
            strategy_score += 10
            tags.append("波动可控")
        if near_breakout:
            strategy_score += 12
            tags.append("接近20日强势区")
        if overheated:
            strategy_score -= 12
            tags.append("过热扣分")

        if item.get("history_enriched"):
            final_score = item["score"] * 0.5 + strategy_score * 0.5
        else:
            final_score = item["score"] * 0.7

        item.update(
            {
                "rps20": round(rps20, 4) if rps20 is not None else None,
                "rps60": round(rps60, 4) if rps60 is not None else None,
                "strategy_score": round(strategy_score, 1),
                "final_score": round(final_score, 1),
                "strategy_tags": "、".join(tags) if tags else "无增强标签",
                "strategy_notes": (
                    "横截面动量 + 均线趋势 + 温和放量 + 波动过滤 + 近20日强势区"
                    if tags else "仅保留基础量价打分"
                ),
            }
        )
    return scored


def _passes_enhanced_strategy(item: dict) -> bool:
    """回测用增强过滤，避免只凭基础分触发。"""
    latest = _to_float(item.get("latest"))
    ma20 = _to_float(item.get("ma20"))
    change_rate_20d = _to_float(item.get("change_rate_20d"))
    volume_ratio = _to_float(item.get("volume_ratio"))
    volatility_20d = _to_float(item.get("volatility_20d"))
    close_to_high = _to_float(item.get("close_to_20d_high"))

    return all(
        [
            latest is not None and ma20 is not None and latest >= ma20,
            change_rate_20d is not None and change_rate_20d >= 0,
            volume_ratio is not None and 0.6 <= volume_ratio <= 3.5,
            volatility_20d is not None and volatility_20d <= 0.075,
            close_to_high is not None and close_to_high >= -0.08,
        ]
    )


def _signal_row_from_bar(base_row: dict, bars: list[dict], idx: int) -> dict:
    """将历史某一天转换成扫描器可评分的伪快照。"""
    bar = bars[idx]
    prev_close = bars[idx - 1]["close"] if idx > 0 else None
    closes = [x["close"] for x in bars[: idx + 1]]
    highs = [x["high"] for x in bars[: idx + 1]]
    lows = [x["low"] for x in bars[: idx + 1]]
    turnovers = [x["turnover"] for x in bars[: idx + 1] if x.get("turnover") is not None]
    change_rate = bar["close"] / prev_close - 1 if prev_close else None
    amplitude = (bar["high"] - bar["low"]) / prev_close if prev_close else None
    avg_turnover_20d = _moving_average(turnovers, 20)
    volume_ratio = None
    if bar.get("turnover") is not None and avg_turnover_20d:
        volume_ratio = bar["turnover"] / avg_turnover_20d
    returns = _daily_returns(closes)
    volatility_20d = stdev(returns[-20:]) if len(returns[-20:]) >= 2 else None
    high_20d = max(highs[-20:]) if len(highs) >= 20 else None
    low_20d = min(lows[-20:]) if len(lows) >= 20 else None
    close_to_20d_high = bar["close"] / high_20d - 1 if high_20d else None
    return {
        "name": base_row.get("name"),
        "symkey": base_row.get("symkey"),
        "latest": bar["close"],
        "close": bar["close"],
        "open": bar["open"],
        "high": bar["high"],
        "low": bar["low"],
        "prev_close": prev_close,
        "change_rate": change_rate,
        "change_rate_5d": _series_return(closes, 5),
        "change_rate_20d": _series_return(closes, 20),
        "change_rate_60d": _series_return(closes, 60),
        "turnover": bar.get("turnover"),
        "volume": bar.get("volume"),
        "amplitude": amplitude,
        "ma5": _moving_average(closes, 5),
        "ma10": _moving_average(closes, 10),
        "ma20": _moving_average(closes, 20),
        "avg_turnover_20d": avg_turnover_20d,
        "volume_ratio": volume_ratio,
        "volatility_20d": volatility_20d,
        "high_20d": high_20d,
        "low_20d": low_20d,
        "close_to_20d_high": close_to_20d_high,
        "history_days": idx + 1,
        "history_enriched": True,
        "trading_status": "NORMAL",
        "industry_sector": base_row.get("industry_sector") or {"name": "未分类"},
        "data_source": base_row.get("data_source") or "fuyao",
    }


def _parse_trigger_zone(zone: str) -> tuple[float, float]:
    """解析触发区间字符串。"""
    low_text, high_text = zone.split("-", maxsplit=1)
    return float(low_text), float(high_text)


def _entry_price_for_bar(bar: dict, trigger_low: float, trigger_high: float) -> Optional[float]:
    """判断 K 线是否触发模拟买入区间，并返回保守成交价。"""
    if bar["low"] > trigger_high or bar["high"] < trigger_low:
        return None
    open_price = bar["open"]
    if trigger_low <= open_price <= trigger_high:
        return open_price
    if open_price > trigger_high:
        return trigger_high
    return trigger_low


def _stop_execution_metrics(
    entry_price: float,
    stop_loss: float,
    bar_low: Optional[float],
    cost_bps: float,
    stop_slippage_bps: float,
) -> dict[str, Optional[float]]:
    """计算止损理想成交、滑点成交和日内最低压力测试指标。"""
    if entry_price <= 0 or stop_loss <= 0:
        return {}
    low_price = bar_low if bar_low is not None else stop_loss
    stop_breach_pct = low_price / stop_loss - 1 if low_price < stop_loss else 0.0
    slippage_price = stop_loss * (1 - stop_slippage_bps / 10000)
    if low_price is not None:
        slippage_price = max(low_price, slippage_price)
    ideal_net = stop_loss / entry_price - 1 - cost_bps / 10000
    worst_intraday_net = low_price / entry_price - 1 - cost_bps / 10000 if low_price is not None else ideal_net
    slippage_net = slippage_price / entry_price - 1 - cost_bps / 10000
    return {
        "stop_low_price": round(low_price, 3) if low_price is not None else None,
        "stop_breach_pct": round(stop_breach_pct, 5),
        "stop_slippage_bps": stop_slippage_bps,
        "slippage_exit_price": round(slippage_price, 3),
        "net_return_worst_intraday": round(worst_intraday_net, 5),
        "net_return_slippage": round(slippage_net, 5),
        "slippage_vs_ideal_return": round(slippage_net - ideal_net, 5),
    }


def _simulate_trade(
    signal: dict,
    bars: list[dict],
    signal_idx: int,
    market_profile: Optional[dict] = None,
    max_hold_days: int = 5,
    entry_window_days: int = 2,
    cost_bps: float = 15,
) -> Optional[dict]:
    """按信号后的触发区间、止损、第一止盈做保守回放。"""
    stop_slippage_bps = float(_cfg("backtest", "stop_slippage_bps", 30))
    plan = _build_trade_plan(signal)
    if market_profile:
        plan = _apply_market_risk_controls([plan], market_profile)[0]
    trigger_low, trigger_high = _parse_trigger_zone(plan["trigger_zone"])
    entry_idx: Optional[int] = None
    entry_price: Optional[float] = None
    for idx in range(signal_idx + 1, min(signal_idx + 1 + entry_window_days, len(bars))):
        price = _entry_price_for_bar(bars[idx], trigger_low, trigger_high)
        if price is not None:
            entry_idx = idx
            entry_price = price
            break
    if entry_idx is None or entry_price is None:
        return None

    stop_loss = float(plan["stop_loss"])
    first_take_profit = float(plan["first_take_profit"])
    exit_idx = min(entry_idx + max_hold_days - 1, len(bars) - 1)
    exit_price = bars[exit_idx]["close"]
    exit_reason = "timeout"
    stop_metrics: dict[str, Optional[float]] = {
        "stop_low_price": None,
        "stop_breach_pct": None,
        "stop_slippage_bps": stop_slippage_bps,
        "slippage_exit_price": None,
        "net_return_worst_intraday": None,
        "net_return_slippage": None,
        "slippage_vs_ideal_return": None,
    }

    for idx in range(entry_idx, min(entry_idx + max_hold_days, len(bars))):
        bar = bars[idx]
        if bar["low"] <= stop_loss:
            exit_idx = idx
            exit_price = stop_loss
            exit_reason = "stop_loss"
            stop_metrics.update(
                _stop_execution_metrics(entry_price, stop_loss, bar.get("low"), cost_bps, stop_slippage_bps)
            )
            break
        if bar["high"] >= first_take_profit:
            exit_idx = idx
            exit_price = first_take_profit
            exit_reason = "take_profit"
            break

    gross_return = exit_price / entry_price - 1
    net_return = gross_return - cost_bps / 10000
    return {
        "symbol": signal.get("symbol"),
        "name": signal.get("name"),
        "signal_date": bars[signal_idx].get("date"),
        "entry_date": bars[entry_idx].get("date"),
        "exit_date": bars[exit_idx].get("date"),
        "signal_price": plan.get("signal_price"),
        "entry_price": round(entry_price, 3),
        "exit_price": round(exit_price, 3),
        "stop_loss": plan.get("stop_loss"),
        "first_take_profit": plan.get("first_take_profit"),
        "position_pct": plan.get("position_pct"),
        "base_position_pct": plan.get("base_position_pct", plan.get("position_pct")),
        "market_regime_label": plan.get("market_regime_label"),
        "holding_days": exit_idx - entry_idx + 1,
        "exit_reason": exit_reason,
        "gross_return": round(gross_return, 5),
        "net_return": round(net_return, 5),
        **stop_metrics,
        "score": signal.get("score"),
        "filter_reasons": signal.get("filter_reasons"),
        "_exit_idx": exit_idx,
    }


def _summarize_backtest(trades: list[dict], symbol_count: int, cost_bps: float) -> dict:
    """汇总回测指标。"""
    returns = [trade["net_return"] for trade in trades]
    slippage_returns = [
        trade["net_return_slippage"]
        for trade in trades
        if trade.get("net_return_slippage") is not None
    ]
    worst_intraday_returns = [
        trade["net_return_worst_intraday"]
        for trade in trades
        if trade.get("net_return_worst_intraday") is not None
    ]
    stop_trades = [trade for trade in trades if trade.get("exit_reason") == "stop_loss"]
    stop_breaches = [
        trade.get("stop_breach_pct")
        for trade in stop_trades
        if trade.get("stop_breach_pct") is not None and trade.get("stop_breach_pct") < 0
    ]
    slippage_vs_ideal = [
        trade.get("slippage_vs_ideal_return")
        for trade in stop_trades
        if trade.get("slippage_vs_ideal_return") is not None
    ]
    event_equity = 1.0
    peak = 1.0
    event_sequence_max_drawdown = 0.0
    for ret in returns:
        event_equity *= 1 + ret
        peak = max(peak, event_equity)
        if peak:
            event_sequence_max_drawdown = min(event_sequence_max_drawdown, event_equity / peak - 1)
    reason_counts: dict[str, int] = {}
    for trade in trades:
        reason = trade.get("exit_reason") or "unknown"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "symbol_count": symbol_count,
        "trade_count": len(trades),
        "win_rate": round(sum(1 for ret in returns if ret > 0) / len(returns), 4) if returns else None,
        "average_return": round(sum(returns) / len(returns), 5) if returns else None,
        "median_return": round(median(returns), 5) if returns else None,
        "best_trade_return": round(max(returns), 5) if returns else None,
        "worst_trade_return": round(min(returns), 5) if returns else None,
        "average_slippage_return": round(sum(slippage_returns) / len(slippage_returns), 5) if slippage_returns else None,
        "average_worst_intraday_return": (
            round(sum(worst_intraday_returns) / len(worst_intraday_returns), 5) if worst_intraday_returns else None
        ),
        "event_sequence_max_drawdown": round(event_sequence_max_drawdown, 5) if returns else None,
        "exit_reason_counts": reason_counts,
        "stop_loss_breach_count": len(stop_breaches),
        "stop_loss_breach_rate": round(len(stop_breaches) / len(stop_trades), 5) if stop_trades else None,
        "average_stop_breach_pct": round(sum(stop_breaches) / len(stop_breaches), 5) if stop_breaches else None,
        "worst_stop_breach_pct": round(min(stop_breaches), 5) if stop_breaches else None,
        "average_slippage_vs_ideal_return": (
            round(sum(slippage_vs_ideal) / len(slippage_vs_ideal), 5) if slippage_vs_ideal else None
        ),
        "cost_bps": cost_bps,
        "stop_slippage_bps": _cfg("backtest", "stop_slippage_bps", 30),
        "rule": "信号日收盘生成计划；后2个交易日触发区间有效；持有最多5个交易日；同日先触发止损则按理想止损价计，同时输出滑点和日内最低压力测试。",
        "note": "回测为事件级规则回放，未构建资金占用、同时持仓上限和组合再平衡曲线；止损成交价默认仍为规则价，压力测试指标用于暴露跳空/滑点风险。",
    }


def _run_backtest(
    histories: dict[str, list[dict]],
    stock_rows: dict[str, dict],
    market_timeline: Optional[dict[str, dict]] = None,
    min_score: float = 70,
    max_hold_days: int = 5,
    entry_window_days: int = 2,
    cost_bps: float = 15,
) -> tuple[list[dict], dict]:
    """对已补齐历史 K 的样本执行规则回测。"""
    trades: list[dict] = []
    for symbol, bars in histories.items():
        if len(bars) < 80:
            continue
        base_row = stock_rows.get(symbol)
        if not base_row:
            continue
        idx = 60
        while idx < len(bars) - entry_window_days:
            signal_row = _signal_row_from_bar(base_row, bars, idx)
            scored_signal = _score_stock(signal_row)
            signal_date = bars[idx].get("date")
            signal_market = market_timeline.get(signal_date) if market_timeline and signal_date else None
            if (
                scored_signal["passed"]
                and scored_signal["score"] >= min_score
                and _passes_enhanced_strategy(scored_signal)
                and _market_allows_backtest_signal(signal_market, scored_signal["score"])
            ):
                trade = _simulate_trade(
                    scored_signal,
                    bars,
                    idx,
                    market_profile=signal_market,
                    max_hold_days=max_hold_days,
                    entry_window_days=entry_window_days,
                    cost_bps=cost_bps,
                )
                if trade:
                    trades.append(trade)
                    idx = int(trade.pop("_exit_idx")) + 1
                    continue
            idx += 1
    summary = _summarize_backtest(trades, len(histories), cost_bps)
    summary["market_filter_applied"] = bool(market_timeline)
    if market_timeline:
        summary["note"] = (
            "回测为事件级规则回放，已按历史指数环境过滤新信号；"
            "未构建资金占用、同时持仓上限和组合再平衡曲线；"
            "止损成交价默认仍为规则价，另输出滑点和日内最低压力测试。"
        )
    return trades, summary


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    """写 CSV。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _read_csv_rows(path: Path) -> list[dict]:
    """读取 CSV，不存在时返回空列表。"""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _ledger_paths() -> tuple[Path, Path]:
    """返回 pending 台账与归档台账路径。"""
    ledger_dir = Path(str(_cfg("ledger", "dir", "data/ledger")))
    return (
        ledger_dir / str(_cfg("ledger", "pending_file", "pending_trades.csv")),
        ledger_dir / str(_cfg("ledger", "archive_file", "trade_archive.csv")),
    )


def _ensure_review_histories(active_rows: list[dict], histories: dict[str, list[dict]], source: str) -> dict[str, list[dict]]:
    """为已有 pending/open 台账补齐复盘所需历史 K。"""
    review_histories = dict(histories)
    if source != "fuyao":
        return review_histories
    symbols = [
        str(row.get("symbol") or "")
        for row in active_rows
        if (row.get("status") or "pending") in {"pending", "open"} and row.get("symbol")
    ]
    for symbol in sorted(set(symbols)):
        if symbol in review_histories:
            continue
        try:
            review_histories[symbol] = _fetch_fuyao_historical(symbol)
        except (RuntimeError, requests.RequestException, OSError, ValueError) as exc:
            logger.warning(f"复盘历史K线补齐失败 {symbol}: {exc}")
            review_histories[symbol] = []
    return review_histories


def _trade_id(symbol: str, signal_date: str) -> str:
    """生成模拟交易唯一 ID。"""
    return f"{symbol}_{signal_date}"


def _date_diff_days(start: str, end: str) -> int:
    """计算两个 YYYY-MM-DD 日期相差天数。"""
    start_dt = _parse_bar_date(start)
    end_dt = _parse_bar_date(end)
    if start_dt is None or end_dt is None:
        return 0
    return (end_dt.date() - start_dt.date()).days


def _snapshot_bar(row: dict) -> Optional[dict]:
    """将当日快照转换为简化 K 线，用于 pending 台账复核。"""
    open_price = _to_float(row.get("open"))
    high = _to_float(row.get("high"))
    low = _to_float(row.get("low"))
    close = _to_float(row.get("latest") or row.get("close"))
    if close is None:
        return None
    return {
        "open": open_price if open_price is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
    }


def _close_ledger_trade(
    row: dict,
    exit_date: str,
    exit_price: float,
    exit_reason: str,
    execution_metrics: Optional[dict[str, Any]] = None,
) -> dict:
    """关闭台账记录并计算模拟收益。"""
    entry_price = _to_float(row.get("entry_price"))
    net_return = None
    gross_return = None
    if entry_price:
        gross_return = exit_price / entry_price - 1
        net_return = gross_return - (_cfg("backtest", "cost_bps", 15) / 10000)
    row.update(
        {
            "status": "closed" if exit_reason != "expired_no_entry" else "expired",
            "exit_date": exit_date,
            "exit_price": round(exit_price, 3),
            "exit_reason": exit_reason,
            "gross_return": round(gross_return, 5) if gross_return is not None else "",
            "net_return": round(net_return, 5) if net_return is not None else "",
            "last_update": datetime.now().isoformat(),
        }
    )
    if execution_metrics:
        row.update(execution_metrics)
    return row


def _normalize_position_audit_fields(row: dict) -> dict:
    """为旧台账行补齐仓位审计字段。"""
    base_position = _to_float(row.get("base_position_pct"))
    position = _to_float(row.get("position_pct"))
    multiplier = _to_float(row.get("market_position_multiplier"))
    if base_position is not None and position is not None and not row.get("effective_position_multiplier"):
        row["effective_position_multiplier"] = round(position / base_position, 4) if base_position else ""
    if base_position is not None and multiplier is not None and not row.get("raw_position_pct"):
        row["raw_position_pct"] = round(base_position * multiplier, 2)
    if not row.get("position_adjustment_note") and base_position is not None and position is not None:
        if multiplier is not None:
            row["position_adjustment_note"] = (
                f"基础{base_position:g}% × 环境系数{multiplier:g} = {(base_position * multiplier):.2f}%，"
                f"实际{position:g}%"
            )
        else:
            row["position_adjustment_note"] = f"基础{base_position:g}%，实际{position:g}%"
    return row


def _update_trade_ledger(candidates: list[dict], stocks: list[dict], today: str, output_dir: Path) -> dict:
    """滚动维护 pending/open/closed 模拟交易台账。"""
    pending_path, archive_path = _ledger_paths()
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    active_rows = _read_csv_rows(pending_path)
    archive_rows = _read_csv_rows(archive_path)
    archive_ids = {row.get("trade_id") for row in archive_rows}
    stock_map = {str(row.get("symkey")): row for row in stocks if row.get("symkey")}
    entry_window_days = int(_cfg("backtest", "entry_window_days", 2))
    max_hold_days = int(_cfg("backtest", "max_hold_days", 5))
    stop_slippage_bps = float(_cfg("backtest", "stop_slippage_bps", 30))
    same_symbol_policy = str(_cfg("ledger", "same_symbol_active_policy", "skip")).lower()

    updates: list[dict] = []
    next_active: list[dict] = []
    closed_rows: list[dict] = []
    skipped_same_symbol_count = 0

    for row in active_rows:
        row = _normalize_position_audit_fields(row)
        status = row.get("status") or "pending"
        symbol = row.get("symbol") or ""
        signal_date = row.get("signal_date") or today
        bar = _snapshot_bar(stock_map.get(symbol) or {})
        action = "kept"

        if signal_date == today:
            next_active.append(row)
            updates.append({**row, "ledger_action": action})
            continue

        if status == "pending":
            if _date_diff_days(signal_date, today) > entry_window_days:
                row = _close_ledger_trade(row, today, _to_float(row.get("signal_price")) or 0.0, "expired_no_entry")
                closed_rows.append(row)
                action = "expired_no_entry"
            elif bar:
                trigger_low, trigger_high = _parse_trigger_zone(str(row.get("trigger_zone")))
                entry_price = _entry_price_for_bar(bar, trigger_low, trigger_high)
                if entry_price is not None:
                    row.update(
                        {
                            "status": "open",
                            "entry_date": today,
                            "entry_price": round(entry_price, 3),
                            "holding_days": 1,
                            "last_update": datetime.now().isoformat(),
                        }
                    )
                    status = "open"
                    action = "entered"

        if status == "open":
            if bar:
                stop_loss = _to_float(row.get("stop_loss")) or 0.0
                first_take_profit = _to_float(row.get("first_take_profit")) or 0.0
                holding_days = max(1, _date_diff_days(row.get("entry_date") or today, today) + 1)
                row["holding_days"] = holding_days
                if bar["low"] <= stop_loss:
                    entry_price = _to_float(row.get("entry_price")) or stop_loss
                    stop_metrics = _stop_execution_metrics(
                        entry_price,
                        stop_loss,
                        bar.get("low"),
                        float(_cfg("backtest", "cost_bps", 15)),
                        stop_slippage_bps,
                    )
                    row = _close_ledger_trade(row, today, stop_loss, "stop_loss", stop_metrics)
                    closed_rows.append(row)
                    action = "closed_stop_loss"
                elif bar["high"] >= first_take_profit:
                    row = _close_ledger_trade(row, today, first_take_profit, "take_profit")
                    closed_rows.append(row)
                    action = "closed_take_profit"
                elif holding_days >= max_hold_days:
                    row = _close_ledger_trade(row, today, bar["close"], "timeout")
                    closed_rows.append(row)
                    action = "closed_timeout"
                else:
                    row["last_update"] = datetime.now().isoformat()
                    next_active.append(row)
                    action = "open_kept"
            else:
                row["last_update"] = datetime.now().isoformat()
                next_active.append(row)
                action = "missing_snapshot"
        elif row.get("status") in {"closed", "expired"}:
            pass
        elif action == "kept":
            row["last_update"] = datetime.now().isoformat()
            next_active.append(row)

        updates.append({**row, "ledger_action": action})

    active_ids = {row.get("trade_id") for row in next_active}
    active_by_symbol: dict[str, list[dict]] = {}
    for row in next_active:
        if (row.get("status") or "pending") in {"pending", "open"} and row.get("symbol"):
            active_by_symbol.setdefault(str(row.get("symbol")), []).append(row)
    for item in candidates:
        symbol = item.get("symbol")
        if not symbol:
            continue
        trade_id = _trade_id(symbol, today)
        if trade_id in active_ids or trade_id in archive_ids:
            continue
        overlapping = active_by_symbol.get(str(symbol), [])
        if overlapping and same_symbol_policy == "skip":
            skipped_same_symbol_count += 1
            overlapping_ids = ",".join(str(row.get("trade_id") or "") for row in overlapping if row.get("trade_id"))
            overlap_note = f"同标的已有 active/pending：{overlapping_ids}，本轮不新增 pending"
            item["same_symbol_overlap"] = True
            item["same_symbol_active_trade_ids"] = overlapping_ids
            item["overlap_risk_note"] = overlap_note
            item["plan"] = f"{item.get('plan') or ''}；{overlap_note}"
            skipped_row = {
                "trade_id": trade_id,
                "status": "skipped_same_symbol",
                "symbol": symbol,
                "name": item.get("name"),
                "signal_date": today,
                "entry_date": "",
                "exit_date": "",
                "signal_price": item.get("signal_price"),
                "trigger_zone": item.get("trigger_zone"),
                "entry_price": "",
                "exit_price": "",
                "stop_loss": item.get("stop_loss"),
                "first_take_profit": item.get("first_take_profit"),
                "base_position_pct": item.get("base_position_pct", item.get("position_pct")),
                "raw_position_pct": item.get("raw_position_pct"),
                "position_cap_pct": item.get("position_cap_pct"),
                "position_pct": 0,
                "market_regime_label": item.get("market_regime_label"),
                "market_position_multiplier": item.get("market_position_multiplier"),
                "effective_position_multiplier": 0,
                "position_adjustment_note": item.get("position_adjustment_note"),
                "holding_days": "",
                "exit_reason": "skipped_same_symbol_active",
                "gross_return": "",
                "net_return": "",
                "stop_low_price": "",
                "stop_breach_pct": "",
                "stop_slippage_bps": stop_slippage_bps,
                "slippage_exit_price": "",
                "net_return_worst_intraday": "",
                "net_return_slippage": "",
                "slippage_vs_ideal_return": "",
                "same_symbol_active_trade_ids": overlapping_ids,
                "overlap_risk_note": overlap_note,
                "plan": item.get("plan"),
                "last_update": datetime.now().isoformat(),
            }
            updates.append({**skipped_row, "ledger_action": "skipped_same_symbol_active"})
            continue
        row = {
            "trade_id": trade_id,
            "status": "pending",
            "symbol": symbol,
            "name": item.get("name"),
            "signal_date": today,
            "entry_date": "",
            "exit_date": "",
            "signal_price": item.get("signal_price"),
            "trigger_zone": item.get("trigger_zone"),
            "entry_price": "",
            "exit_price": "",
            "stop_loss": item.get("stop_loss"),
            "first_take_profit": item.get("first_take_profit"),
            "base_position_pct": item.get("base_position_pct", item.get("position_pct")),
            "raw_position_pct": item.get("raw_position_pct"),
            "position_cap_pct": item.get("position_cap_pct"),
            "position_pct": item.get("position_pct"),
            "market_regime_label": item.get("market_regime_label"),
            "market_position_multiplier": item.get("market_position_multiplier"),
            "effective_position_multiplier": item.get("effective_position_multiplier"),
            "position_adjustment_note": item.get("position_adjustment_note"),
            "holding_days": "",
            "exit_reason": "",
            "gross_return": "",
            "net_return": "",
            "stop_low_price": "",
            "stop_breach_pct": "",
            "stop_slippage_bps": stop_slippage_bps,
            "slippage_exit_price": "",
            "net_return_worst_intraday": "",
            "net_return_slippage": "",
            "slippage_vs_ideal_return": "",
            "same_symbol_active_trade_ids": "",
            "overlap_risk_note": "",
            "plan": item.get("plan"),
            "last_update": datetime.now().isoformat(),
        }
        next_active.append(row)
        active_ids.add(trade_id)
        active_by_symbol.setdefault(str(symbol), []).append(row)
        updates.append({**row, "ledger_action": "new_pending"})

    ledger_fields = [
        "trade_id", "status", "symbol", "name", "signal_date", "entry_date", "exit_date",
        "signal_price", "trigger_zone", "entry_price", "exit_price", "stop_loss",
        "first_take_profit", "base_position_pct", "raw_position_pct", "position_cap_pct",
        "position_pct", "market_regime_label", "market_position_multiplier",
        "effective_position_multiplier", "position_adjustment_note", "holding_days",
        "exit_reason", "gross_return", "net_return", "stop_low_price", "stop_breach_pct",
        "stop_slippage_bps", "slippage_exit_price", "net_return_worst_intraday",
        "net_return_slippage", "slippage_vs_ideal_return", "same_symbol_active_trade_ids",
        "overlap_risk_note", "plan", "last_update",
    ]
    _write_csv(pending_path, next_active, ledger_fields)
    archive_combined = list(archive_rows)
    for row in closed_rows:
        if row.get("trade_id") not in archive_ids:
            archive_combined.append(row)
            archive_ids.add(row.get("trade_id"))
    _write_csv(archive_path, archive_combined, ledger_fields)
    _write_csv(output_dir / "ledger_updates.csv", updates, [*ledger_fields, "ledger_action"])

    return {
        "active_count": len(next_active),
        "new_pending_count": sum(1 for row in updates if row.get("ledger_action") == "new_pending"),
        "skipped_same_symbol_count": skipped_same_symbol_count,
        "closed_count": len(closed_rows),
        "pending_path": str(pending_path),
        "archive_path": str(archive_path),
        "updates_path": str(output_dir / "ledger_updates.csv"),
    }


def _run_portfolio_backtest(trades: list[dict]) -> tuple[list[dict], dict]:
    """在事件级交易上叠加现金、持仓上限和总敞口约束。"""
    initial_cash = float(_cfg("backtest", "portfolio_initial_cash", 1_000_000))
    max_positions = int(_cfg("backtest", "portfolio_max_positions", 4))
    max_total_exposure = float(_cfg("backtest", "portfolio_max_total_exposure_pct", 0.32))
    default_position_pct = float(_cfg("backtest", "portfolio_default_position_pct", 0.08))

    sorted_trades = sorted(trades, key=lambda x: (x.get("entry_date") or "", -(x.get("score") or 0)))
    active: list[dict] = []
    accepted: list[dict] = []
    skipped: list[dict] = []
    cash = initial_cash
    realized_equity = initial_cash
    peak_equity = initial_cash
    max_drawdown = 0.0

    def close_due_positions(current_date: str) -> None:
        nonlocal cash, realized_equity, peak_equity, max_drawdown, active
        still_active: list[dict] = []
        for pos in active:
            if pos["exit_date"] <= current_date:
                exit_value = pos["position_value"] * (1 + pos["net_return"])
                cash += exit_value
                realized_equity = cash + sum(item["position_value"] for item in still_active)
                peak_equity = max(peak_equity, realized_equity)
                if peak_equity:
                    max_drawdown = min(max_drawdown, realized_equity / peak_equity - 1)
            else:
                still_active.append(pos)
        active = still_active

    for trade in sorted_trades:
        entry_date = trade.get("entry_date") or ""
        exit_date = trade.get("exit_date") or ""
        if not entry_date or not exit_date:
            continue
        close_due_positions(entry_date)
        current_exposure = sum(pos["position_value"] for pos in active)
        position_pct = (_to_float(trade.get("position_pct")) or (default_position_pct * 100)) / 100
        target_value = initial_cash * position_pct
        remaining_exposure = initial_cash * max_total_exposure - current_exposure
        position_value = min(target_value, cash, remaining_exposure)
        if len(active) >= max_positions or position_value <= 0:
            skipped.append({**trade, "portfolio_action": "skipped_capacity"})
            continue
        trade_row = {
            **trade,
            "portfolio_action": "accepted",
            "position_value": round(position_value, 2),
            "portfolio_position_pct": round(position_value / initial_cash, 4),
        }
        cash -= position_value
        active.append(
            {
                "exit_date": exit_date,
                "position_value": position_value,
                "net_return": trade.get("net_return") or 0.0,
            }
        )
        accepted.append(trade_row)

    close_due_positions("9999-12-31")
    returns = [row.get("net_return") for row in accepted if row.get("net_return") is not None]
    summary = {
        "initial_cash": initial_cash,
        "ending_equity": round(cash, 2),
        "total_return": round(cash / initial_cash - 1, 5) if initial_cash else None,
        "accepted_trades": len(accepted),
        "skipped_trades": len(skipped),
        "max_positions": max_positions,
        "max_total_exposure_pct": max_total_exposure,
        "win_rate": round(sum(1 for ret in returns if ret > 0) / len(returns), 4) if returns else None,
        "average_trade_return": round(sum(returns) / len(returns), 5) if returns else None,
        "max_drawdown": round(max_drawdown, 5),
        "note": "组合级回测加入现金、同时持仓上限和总敞口限制；仍使用日 K 事件回放，非实盘成交证明。",
    }
    return [*accepted, *skipped], summary


def _steward_output_path(output_base: str, date: str, mode: str) -> Path:
    """返回 Hermes 策略管家指定模式的产物路径。"""
    base = Path(output_base)
    scan_dir = base / date / "scan"
    if mode == "deep":
        return scan_dir / "strategy_steward_deep_report.md"
    if mode == "critic":
        return scan_dir / "strategy_steward_critic.md"
    if mode == "experiment":
        return Path("strategy_experiments") / date / "hermes_experiments.json"
    return scan_dir / "strategy_steward_report.md"


def _extract_steward_digest(report_path: Path, max_items: int = 3) -> list[str]:
    """从 Hermes 报告或实验草案中提取适合飞书摘要展示的核心行。"""
    if not report_path.exists():
        return []
    if report_path.suffix.lower() == ".json":
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return ["实验草案 JSON 解析失败，需人工查看原始输出"]
        lines: list[str] = []
        warning = str(payload.get("sample_warning") or "").strip()
        if warning:
            lines.append(warning)
        for experiment in payload.get("experiments") or []:
            if not isinstance(experiment, dict):
                continue
            name = experiment.get("name") or "未命名实验"
            sample_size = experiment.get("sample_size", "-")
            direction = experiment.get("parameter_direction") or "-"
            gate = experiment.get("promotion_gate") or "-"
            lines.append(f"实验草案：{name}｜样本 {sample_size}｜方向 {direction}｜晋级 {gate}")
            if len(lines) >= max_items:
                break
        return lines[:max_items]

    text = report_path.read_text(encoding="utf-8")
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        clean = line[2:].replace("**", "").replace("`", "").strip()
        if not clean or clean.startswith("数据来源："):
            continue
        lines.append(clean)
        if len(lines) >= max_items:
            break
    return lines


def _run_strategy_steward(
    date: str,
    output_base: str,
    mode: str,
    script: str,
    fail_on_error: bool = False,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """调用 Hermes 策略管家脚本，返回只读诊断产物摘要。"""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = Path(script).expanduser()
    if not script_path.is_absolute():
        script_path = repo_root / script_path
    output_path = _steward_output_path(output_base, date, mode)
    if not output_path.is_absolute():
        output_path = repo_root / output_path
    cmd = [
        str(script_path),
        "--date",
        date,
        "--output",
        output_base,
        "--mode",
        mode,
    ]
    summary: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "status": "pending",
        "script": str(script_path),
        "report_path": str(output_path),
        "digest_lines": [],
    }
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        summary.update(
            {
                "status": "failed",
                "returncode": None,
                "error": f"Hermes strategy steward timed out after {timeout_seconds}s",
                "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
                "stderr_tail": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
            }
        )
        if fail_on_error:
            raise RuntimeError(summary["error"]) from exc
        return summary

    summary.update(
        {
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
        }
    )
    if proc.returncode == 0 and output_path.exists():
        summary["status"] = "success"
        summary["digest_lines"] = _extract_steward_digest(output_path)
    else:
        summary["status"] = "failed"
        summary["error"] = f"Hermes strategy steward exited with code {proc.returncode}"
        if fail_on_error:
            raise RuntimeError(summary["error"])
    return summary


def _render_report(
    date: str,
    indexes: list[dict],
    scored: list[dict],
    candidates: list[dict],
    excluded: list[dict],
    source: str,
    market_profile: Optional[dict] = None,
    backtest_summary: Optional[dict] = None,
    portfolio_summary: Optional[dict] = None,
    ledger_summary: Optional[dict] = None,
    review_summary: Optional[dict] = None,
    health_summary: Optional[dict] = None,
    steward_summary: Optional[dict] = None,
    learning_summary: Optional[dict] = None,
) -> str:
    """渲染仿图片风格的研究汇报。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"使用市场数据接口完成了 {now} 收盘扫描，并已维护观察台账：",
        "",
        "- 已写入 `daily_scans.csv`",
        f"- 已写入 {len(candidates)} 只研究候选到 `research_candidates.csv`",
        f"- 已写入 {len(candidates)} 笔 pending 到 `simulated_trades.csv`",
        "- 已写入市场环境过滤到 `market_profile.json`",
        "- 已写入规则回测到 `backtest_trades.csv` / `backtest_summary.json`",
        "- 已写入组合级回测到 `portfolio_backtest_trades.csv` / `portfolio_backtest_summary.json`",
        "- 已更新 pending/持仓台账到 `data/ledger/`",
        "- 已写入计划复盘到 `review_summary.json` / `review_report.md`",
        "- 已写入策略健康监控到 `strategy_health_summary.json` / `strategy_health_report.md`",
        "- 自动化记忆已更新到 `memory.md`",
        f"- 数据来源：{_fmt_source(source)}",
    ]
    if steward_summary:
        if steward_summary.get("status") == "success":
            lines.append(f"- Hermes 策略管家已写入 `{steward_summary.get('report_path')}`")
        else:
            lines.append(f"- Hermes 策略管家运行状态：{steward_summary.get('status')}")
    if learning_summary:
        lines.append(f"- 策略学习记忆已更新 `{learning_summary.get('memory_markdown_path')}`")
    if review_summary:
        lines.extend(
            [
                "",
                "**昨日计划复盘**",
                "",
                (
                f"- 复盘对象：{review_summary.get('reviewed_count')} 笔，"
                f"今日新 pending：{review_summary.get('new_pending_count')} 笔"
            ),
            (
                f"- 今日新触发：{review_summary.get('newly_triggered_count', review_summary.get('triggered_count'))}，"
                f"历史已入场/退出：{review_summary.get('entry_confirmed_count', '-')}，"
                f"未触发：{review_summary.get('not_triggered_count')}，"
                f"继续观察：{review_summary.get('active_open_count')}，"
                f"本次归档：{review_summary.get('closed_count')}，"
                f"同标的跳过：{review_summary.get('skipped_same_symbol_count', 0)}"
            ),
                (
                    f"- 平均收盘相对信号价：{_fmt_pct(review_summary.get('average_close_vs_signal_pct'))}，"
                    f"最大顺向波动：{_fmt_pct(review_summary.get('best_max_favorable_pct'))}，"
                    f"最大逆向波动：{_fmt_pct(review_summary.get('worst_max_adverse_pct'))}"
                ),
                f"- 复盘详情：`{review_summary.get('report_path')}`",
            ]
        )
        report_items = review_summary.get("report_items") or []
        if report_items:
            lines.extend(
                [
                    "",
                    "| 标的 | 信号日 | 复盘状态 | 收盘偏离 | 最大顺向 | 最大逆向 | 观察 |",
                    "|---|---:|---|---:|---:|---:|---|",
                ]
            )
            for item in report_items[:5]:
                observation = str(item.get("observation") or "-").replace("|", "/")
                lines.append(
                    "| {name} {symbol} | {signal_date} | {status} | {close_dev} | {mfe} | {mae} | {observation} |".format(
                        name=item.get("name") or "",
                        symbol=item.get("symbol") or "",
                        signal_date=item.get("signal_date") or "-",
                        status=item.get("status_label") or item.get("status") or "-",
                        close_dev=_fmt_pct(item.get("close_vs_signal_pct")),
                        mfe=_fmt_pct(item.get("max_favorable_pct")),
                        mae=_fmt_pct(item.get("max_adverse_pct")),
                        observation=observation,
                    )
                )
    lines.extend(["", "**大盘判断**", _market_comment(indexes, scored, market_profile)])
    if health_summary:
        primary = health_summary.get("primary_window") or {}
        lines.extend(
            [
                "",
                "**策略健康监控**",
                "",
                (
                    f"- 主观察窗口：{primary.get('window_days', '-')} 日，"
                    f"复盘样本 {primary.get('reviewed_count', 0)} 笔，"
                    f"触发率 {_fmt_pct(primary.get('trigger_rate'))}，"
                    f"第一止盈率 {_fmt_pct(primary.get('take_profit_rate'))}，"
                    f"止损率 {_fmt_pct(primary.get('stop_loss_rate'))}"
                ),
                (
                    f"- 已归档平均净收益：{_fmt_pct(primary.get('avg_net_return'))}，"
                    f"平均顺向 {_fmt_pct(primary.get('avg_max_favorable_pct'))}，"
                    f"平均逆向 {_fmt_pct(primary.get('avg_max_adverse_pct'))}"
                ),
                f"- 体检详情：`{health_summary.get('report_path')}`",
            ]
        )
        for note in (primary.get("diagnostics") or [])[:3]:
            lines.append(f"- 健康提示：{note}")
        for experiment in (primary.get("experiment_hypotheses") or [])[:2]:
            lines.append(f"- 候选实验：{experiment}")
    if steward_summary:
        lines.extend(
            [
                "",
                "**Hermes 策略管家**",
                "",
                f"- 模式：{steward_summary.get('mode')}，状态：{steward_summary.get('status')}",
                f"- 报告/草案路径：`{steward_summary.get('report_path')}`",
            ]
        )
        digest_lines = steward_summary.get("digest_lines") or []
        if digest_lines:
            for line in digest_lines[:3]:
                lines.append(f"- 核心诊断：{line}")
        elif steward_summary.get("status") == "failed":
            error = steward_summary.get("error") or steward_summary.get("stderr_tail") or "未生成诊断"
            lines.append(f"- 运行提示：{error}")
        else:
            lines.append("- 运行提示：已生成完整文件，摘要提取为空，请查看报告路径")
    if learning_summary:
        lines.extend(
            [
                "",
                "**策略学习记忆**",
                "",
                f"- 经验条目：{learning_summary.get('lesson_count')} 条",
                f"- 本次更新：{', '.join(learning_summary.get('updated_lessons') or []) or '无新增'}",
                f"- 记忆路径：`{learning_summary.get('memory_markdown_path')}`",
            ]
        )
    if market_profile:
        lines.extend(
            [
                "",
                "**市场环境过滤**",
                "",
                f"- 环境评级：{market_profile.get('regime_label')}（分数 {market_profile.get('score')}）",
                (
                    f"- 执行参数：候选上限 {market_profile.get('candidate_limit')}，"
                    f"最低综合分 {market_profile.get('min_final_score')}，"
                    f"仓位系数 {market_profile.get('position_multiplier')}，"
                    f"单票上限 {market_profile.get('max_position_pct')}%"
                ),
                "- 仓位口径：`market_position_multiplier` 为环境目标系数，"
                "`effective_position_multiplier` 为四舍五入和仓位上限后的实际生效系数。",
                (
                    f"- 市场宽度：上涨占比 {_fmt_pct(market_profile.get('up_ratio'))}，"
                    f"中位涨跌 {_fmt_pct(market_profile.get('median_change'))}，"
                    f"强势样本 {market_profile.get('strong_count')} 只"
                ),
                (
                    f"- 指数趋势：站上 MA20 比例 {_fmt_pct(market_profile.get('above_ma20_ratio'))}，"
                    f"站上 MA60 比例 {_fmt_pct(market_profile.get('above_ma60_ratio'))}，"
                    f"指数平均涨跌 {_fmt_pct(market_profile.get('avg_index_change'))}"
                ),
            ]
        )
        for item in market_profile.get("index_details") or []:
            lines.append(
                f"- {item.get('label')}：{_fmt_pct(item.get('change_rate'))}，"
                f"20日 {_fmt_pct(item.get('change_rate_20d'))}，"
                f"{'站上' if item.get('above_ma20') else '未站上'}MA20"
            )
    lines.extend(
        [
        "",
        "**今日候选，不超过 5 只**",
        "",
        "| 标的 | 综合分 | 策略标签 | 当前/信号价 | 触发区间 | 止损 | 第一止盈 | 仓位 | 计划 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in candidates:
        lines.append(
            "| {name} {symbol} | {final_score:.1f} | {tags} | {signal:.2f} | {zone} | {stop:.2f} | {take:.2f} | {position}% | {plan} |".format(
                name=item.get("name"),
                symbol=item.get("symbol"),
                final_score=item.get("final_score") or item.get("score") or 0,
                tags=item.get("strategy_tags") or "-",
                signal=item.get("signal_price") or item.get("latest") or 0,
                zone=item.get("trigger_zone") or "-",
                stop=item.get("stop_loss") or 0,
                take=item.get("first_take_profit") or 0,
                position=item.get("position_pct") or 0,
                plan=item.get("plan") or "-",
            )
        )

    lines.extend(
        [
            "",
            "**本轮策略来源与落地**",
            "",
            "- qstock 思路：借鉴 RPS、趋势和资金/量能模型，但只落地为可由 OHLCV 复现的横截面动量与量能过滤。",
            "- Momentum-Investing 思路：采用市场内横截面动量、趋势过滤和波动率约束，不引入黑箱预测。",
            "- tw_stocker 思路：采用“20 日动量 × 60 日趋势”的隔夜筛选框架，并用流动性样本先缩小股票池。",
            "- A-Share Volatility Strategy 思路：把历史波动率和成交活跃度作为风险过滤，而不是只按涨幅排序。",
            "- QuantsPlaybook 思路：保留多因子/择时可复现原则，本轮仅使用透明技术因子，不接入深度学习或复杂基本面模型。",
        ]
    )

    if backtest_summary:
        win_rate = backtest_summary.get("win_rate")
        avg_ret = backtest_summary.get("average_return")
        median_ret = backtest_summary.get("median_return")
        event_dd = backtest_summary.get("event_sequence_max_drawdown")
        worst_trade = backtest_summary.get("worst_trade_return")
        reason_counts = backtest_summary.get("exit_reason_counts") or {}
        lines.extend(
            [
                "",
                "**规则回测摘要（历史 K 样本）**",
                "",
                f"- 覆盖标的：{backtest_summary.get('symbol_count')} 只，回测笔数：{backtest_summary.get('trade_count')} 笔",
                f"- 胜率：{_fmt_pct(win_rate)}，平均单笔：{_fmt_pct(avg_ret)}，中位单笔：{_fmt_pct(median_ret)}",
                f"- 最差单笔：{_fmt_pct(worst_trade)}，事件序列回撤：{_fmt_pct(event_dd)}，扣除成本：{backtest_summary.get('cost_bps')}bp/笔",
                (
                    "- 退出分布："
                    f"止盈 {reason_counts.get('take_profit', 0)}，"
                    f"止损 {reason_counts.get('stop_loss', 0)}，"
                    f"到期 {reason_counts.get('timeout', 0)}"
                ),
                (
                    f"- 止损执行压力：下穿止损 {backtest_summary.get('stop_loss_breach_count', 0)} 笔，"
                    f"下穿率 {_fmt_pct(backtest_summary.get('stop_loss_breach_rate'))}，"
                    f"平均下穿 {_fmt_pct(backtest_summary.get('average_stop_breach_pct'))}，"
                    f"最深下穿 {_fmt_pct(backtest_summary.get('worst_stop_breach_pct'))}，"
                    f"止损滑点版平均 {_fmt_pct(backtest_summary.get('average_slippage_return'))}"
                ),
                f"- 规则：{backtest_summary.get('rule')}",
                f"- 注：{backtest_summary.get('note')}",
            ]
        )

    if portfolio_summary:
        lines.extend(
            [
                "",
                "**组合级回测摘要**",
                "",
                (
                    f"- 初始资金：{portfolio_summary.get('initial_cash'):.0f}，"
                    f"期末权益：{portfolio_summary.get('ending_equity'):.0f}，"
                    f"组合收益：{_fmt_pct(portfolio_summary.get('total_return'))}"
                ),
                (
                    f"- 接受交易：{portfolio_summary.get('accepted_trades')}，"
                    f"因持仓/敞口跳过：{portfolio_summary.get('skipped_trades')}，"
                    f"最大同时持仓：{portfolio_summary.get('max_positions')}，"
                    f"总敞口上限：{_fmt_pct(portfolio_summary.get('max_total_exposure_pct'))}"
                ),
                (
                    f"- 组合胜率：{_fmt_pct(portfolio_summary.get('win_rate'))}，"
                    f"平均单笔：{_fmt_pct(portfolio_summary.get('average_trade_return'))}，"
                    f"最大回撤：{_fmt_pct(portfolio_summary.get('max_drawdown'))}"
                ),
                f"- 注：{portfolio_summary.get('note')}",
            ]
        )

    if ledger_summary:
        lines.extend(
            [
                "",
                "**Pending 台账**",
                "",
                (
                    f"- 当前 active：{ledger_summary.get('active_count')}，"
                    f"新 pending：{ledger_summary.get('new_pending_count')}，"
                    f"本次归档：{ledger_summary.get('closed_count')}，"
                    f"同标的跳过：{ledger_summary.get('skipped_same_symbol_count', 0)}"
                ),
                f"- 台账路径：`{ledger_summary.get('pending_path')}`",
            ]
        )

    lines.extend(["", "**容易误判但今天不列入**", ""])
    for item in excluded[:5]:
        lines.append(
            f"- {item.get('name')} {item.get('symbol')}：{item.get('filter_reasons') or item.get('risk_notes')}"
        )

    lines.extend(
        [
            "",
            "**明日复核要点**",
            "早盘运行时，优先复核今日 pending 的成交额延续性、触发区间承接、以及同板块是否继续扩散。若弱开后无法收复触发区间，取消；若高开过大且换手急升，不追。",
            "",
            "---",
            "免责声明：本扫描为个人模拟交易计划，所有参数由规则自动生成，仅供复盘与风控演练。真实交易风险自担。",
            "",
        ]
    )
    return "\n".join(lines)


def run_market_scan(
    output_base: str = "output",
    date: Optional[str] = None,
    limit: int = 5,
    enrich_limit: Optional[int] = None,
    config_path: str = "strategy.json",
    push: bool = False,
    push_mode: str = "digest",
    push_dry_run: bool = False,
    steward: bool = False,
    steward_mode: Optional[str] = None,
    steward_fail_on_error: bool = False,
) -> dict:
    """
    执行收盘扫描并写入产物。

    Args:
        output_base: 输出根目录
        date:        日期 YYYY-MM-DD，None 时取今日
        limit:       候选数量上限
        enrich_limit:扶摇快照中补齐历史 K 的样本数
        config_path: 策略配置文件
        push:       True 时推送 scan_report.md
        push_mode:  digest 或 full
        push_dry_run: True 时只检查推送配置
        steward:    True 时运行 Hermes 策略管家
        steward_mode: Hermes 模式 daily/deep/critic/experiment
        steward_fail_on_error: True 时 Hermes 失败则中断
    Returns:
        运行摘要
    """
    config = _load_scanner_config(config_path)
    _set_active_config(config)
    today = date or datetime.today().strftime("%Y-%m-%d")
    output_dir = Path(output_base) / today / "scan"
    output_dir.mkdir(parents=True, exist_ok=True)

    stocks, indexes, source = fetch_market_snapshot()
    histories: dict[str, list[dict]] = {}
    if source == "fuyao":
        if enrich_limit is None:
            enrich_limit = int(_cfg("data", "enrich_limit", 120))
        stocks, histories = _enrich_fuyao_rows_with_history(stocks, enrich_limit=enrich_limit)
    pending_path, archive_path = _ledger_paths()
    active_ledger_rows = _read_csv_rows(pending_path)
    review_histories = _ensure_review_histories(active_ledger_rows, histories, source)

    scored = _apply_strategy_overlays([_score_stock(row) for row in stocks])
    scored.sort(key=lambda x: x.get("final_score", x["score"]), reverse=True)
    market_profile = _build_market_profile(indexes, scored)
    candidates = _select_candidates(scored, limit=limit, market_profile=market_profile)
    excluded = _excluded_trade_examples(scored, limit=30)
    stock_rows = {row.get("symkey"): row for row in stocks if row.get("symkey")}
    market_timeline = _build_market_timeline(indexes)
    backtest_trades, backtest_summary = (
        _run_backtest(
            histories,
            stock_rows,
            market_timeline,
            min_score=float(_cfg("backtest", "min_score", 70)),
            max_hold_days=int(_cfg("backtest", "max_hold_days", 5)),
            entry_window_days=int(_cfg("backtest", "entry_window_days", 2)),
            cost_bps=float(_cfg("backtest", "cost_bps", 15)),
        )
        if histories else ([], {})
    )
    portfolio_trades, portfolio_summary = _run_portfolio_backtest(backtest_trades) if backtest_trades else ([], {})
    ledger_summary = _update_trade_ledger(candidates, stocks, today, output_dir)
    review_summary: dict[str, Any] = {}
    if _cfg("review", "enabled", True):
        from reviewer import review_trade_updates

        review_summary = review_trade_updates(
            Path(str(ledger_summary.get("updates_path"))),
            today,
            output_dir,
            stocks,
            histories=review_histories,
            archive_path=Path(str(ledger_summary.get("archive_path"))),
            max_report_items=int(_cfg("review", "max_report_items", 8)),
        )
    health_summary: dict[str, Any] = {}
    if _cfg("strategy_health", "enabled", True):
        from strategy_health import build_strategy_health

        health_summary = build_strategy_health(
            output_base=Path(output_base),
            as_of_date=today,
            output_dir=output_dir,
            archive_path=archive_path,
            windows=list(_cfg("strategy_health", "windows", [5, 20, 60])),
            history_days=int(_cfg("strategy_health", "history_days", 120)),
            min_sample=int(_cfg("strategy_health", "min_sample", 10)),
            max_tag_rows=int(_cfg("strategy_health", "max_tag_rows", 12)),
        )

    fields = [
        "symbol", "name", "sector", "latest", "change_rate", "change_rate_5d",
        "change_rate_20d", "change_rate_60d", "turnover", "turnover_rate",
        "amplitude", "market_cap", "ma5", "ma10", "ma20", "volume_ratio",
        "volatility_20d", "close_to_20d_high", "rps20", "rps60",
        "strategy_score", "final_score", "strategy_tags", "strategy_notes",
        "history_days", "history_enriched", "score", "passed",
        "filter_reasons", "reasons", "risk_notes",
    ]
    _write_csv(output_dir / "daily_scans.csv", scored, fields)
    _write_csv(output_dir / "research_candidates.csv", candidates, fields)
    trade_fields = [
        "symbol", "name", "sector", "signal_price", "trigger_zone", "stop_loss",
        "first_take_profit", "base_position_pct", "position_pct",
        "raw_position_pct", "position_cap_pct", "market_regime_label",
        "market_position_multiplier", "effective_position_multiplier",
        "position_adjustment_note", "market_filter_note",
        "risk_per_share", "reward_risk",
        "plan", "latest", "change_rate", "change_rate_5d", "turnover",
        "turnover_rate", "amplitude", "rps20", "rps60", "strategy_score",
        "final_score", "strategy_tags", "score", "same_symbol_overlap",
        "same_symbol_active_trade_ids", "overlap_risk_note", "risk_notes",
    ]
    _write_csv(output_dir / "simulated_trades.csv", candidates, trade_fields)
    _write_csv(output_dir / "excluded_watchlist.csv", excluded[:30], fields)
    backtest_fields = [
        "symbol", "name", "signal_date", "entry_date", "exit_date",
        "signal_price", "entry_price", "exit_price", "stop_loss",
        "first_take_profit", "holding_days", "exit_reason", "gross_return",
        "net_return", "stop_low_price", "stop_breach_pct", "stop_slippage_bps",
        "slippage_exit_price", "net_return_worst_intraday", "net_return_slippage",
        "slippage_vs_ideal_return", "position_pct", "market_regime_label", "score", "filter_reasons",
    ]
    _write_csv(output_dir / "backtest_trades.csv", backtest_trades, backtest_fields)
    portfolio_fields = [
        "symbol", "name", "signal_date", "entry_date", "exit_date",
        "entry_price", "exit_price", "exit_reason", "net_return",
        "position_pct", "position_value", "portfolio_position_pct",
        "portfolio_action", "score",
    ]
    _write_csv(output_dir / "portfolio_backtest_trades.csv", portfolio_trades, portfolio_fields)
    with open(output_dir / "backtest_summary.json", "w", encoding="utf-8") as f:
        json.dump(backtest_summary, f, ensure_ascii=False, indent=2)
    with open(output_dir / "portfolio_backtest_summary.json", "w", encoding="utf-8") as f:
        json.dump(portfolio_summary, f, ensure_ascii=False, indent=2)
    with open(output_dir / "market_profile.json", "w", encoding="utf-8") as f:
        json.dump(market_profile, f, ensure_ascii=False, indent=2)

    report = _render_report(
        today,
        indexes,
        scored,
        candidates,
        excluded,
        source,
        market_profile,
        backtest_summary,
        portfolio_summary,
        ledger_summary,
        review_summary,
        health_summary,
    )
    report_path = output_dir / "scan_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    steward_summary: dict[str, Any] = {}
    learning_summary: dict[str, Any] = {}
    steward_enabled = steward or bool(_cfg("strategy_steward", "enabled", False))
    if steward_enabled:
        selected_steward_mode = steward_mode or str(_cfg("strategy_steward", "mode", "daily"))
        steward_summary = _run_strategy_steward(
            date=today,
            output_base=output_base,
            mode=selected_steward_mode,
            script=str(_cfg("strategy_steward", "script", "scripts/run_strategy_steward.sh")),
            fail_on_error=steward_fail_on_error or bool(_cfg("strategy_steward", "fail_on_error", False)),
            timeout_seconds=int(_cfg("strategy_steward", "timeout_seconds", 900)),
        )
        if bool(_cfg("strategy_learning", "enabled", True)):
            from strategy_learning import update_strategy_learning

            learning_summary = update_strategy_learning(
                output_base=Path(output_base),
                as_of_date=today,
                memory_dir=Path(str(_cfg("strategy_learning", "memory_dir", "data/strategy_learning"))),
            )
        report = _render_report(
            today,
            indexes,
            scored,
            candidates,
            excluded,
            source,
            market_profile,
            backtest_summary,
            portfolio_summary,
            ledger_summary,
            review_summary,
            health_summary,
            steward_summary,
            learning_summary,
        )
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)

    push_results: list[dict] = []
    push_summary_path = output_dir / "push_summary.json"
    if push:
        from notifier import send_report

        results = send_report(report_path, mode=push_mode, dry_run=push_dry_run)
        push_results = [item.__dict__ for item in results]
        with open(push_summary_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": today,
                    "push_time": datetime.now().isoformat(),
                    "report_path": str(report_path),
                    "mode": push_mode,
                    "dry_run": push_dry_run,
                    "results": push_results,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    memory_path = output_dir / "memory.md"
    with open(memory_path, "w", encoding="utf-8") as f:
        f.write(f"# 扫描记忆 - {today}\n\n")
        f.write(f"- 扫描时间：{datetime.now().isoformat()}\n")
        f.write(f"- 样本数量：{len(scored)}\n")
        f.write(f"- 历史K线补齐样本：{len(histories)}\n")
        f.write(f"- 市场环境：{market_profile.get('regime_label')}（{market_profile.get('score')}）\n")
        f.write(f"- 候选数量：{len(candidates)}\n")
        f.write(f"- 回测笔数：{backtest_summary.get('trade_count', 0) if backtest_summary else 0}\n")
        f.write(f"- 组合回测接受交易：{portfolio_summary.get('accepted_trades', 0) if portfolio_summary else 0}\n")
        f.write(f"- 台账 active：{ledger_summary.get('active_count', 0)}\n")
        f.write(f"- 复盘对象：{review_summary.get('reviewed_count', 0) if review_summary else 0}\n")
        f.write(f"- 策略健康主窗口样本：{(health_summary.get('primary_window') or {}).get('reviewed_count', 0) if health_summary else 0}\n")
        if steward_summary:
            f.write(
                f"- Hermes 策略管家：{steward_summary.get('mode')} / "
                f"{steward_summary.get('status')} / {steward_summary.get('report_path')}\n"
            )
        if learning_summary:
            f.write(f"- 策略学习记忆：{learning_summary.get('memory_markdown_path')}\n")
        f.write("- 候选：")
        f.write("、".join(f"{item['name']}({item['symbol']})" for item in candidates))
        f.write("\n")

    return {
        "date": today,
        "sample_count": len(scored),
        "data_source": source,
        "candidate_count": len(candidates),
        "history_enriched_count": len(histories),
        "market_profile": market_profile,
        "backtest_summary": backtest_summary,
        "portfolio_summary": portfolio_summary,
        "ledger_summary": ledger_summary,
        "review_summary": review_summary,
        "strategy_health_summary": health_summary,
        "strategy_steward_summary": steward_summary,
        "strategy_learning_summary": learning_summary,
        "report_path": str(report_path),
        "daily_scans_path": str(output_dir / "daily_scans.csv"),
        "candidates_path": str(output_dir / "research_candidates.csv"),
        "simulated_trades_path": str(output_dir / "simulated_trades.csv"),
        "market_profile_path": str(output_dir / "market_profile.json"),
        "backtest_trades_path": str(output_dir / "backtest_trades.csv"),
        "backtest_summary_path": str(output_dir / "backtest_summary.json"),
        "portfolio_backtest_trades_path": str(output_dir / "portfolio_backtest_trades.csv"),
        "portfolio_backtest_summary_path": str(output_dir / "portfolio_backtest_summary.json"),
        "review_summary_path": review_summary.get("summary_path") if review_summary else None,
        "review_report_path": review_summary.get("report_path") if review_summary else None,
        "strategy_health_summary_path": health_summary.get("summary_path") if health_summary else None,
        "strategy_health_report_path": health_summary.get("report_path") if health_summary else None,
        "strategy_steward_report_path": steward_summary.get("report_path") if steward_summary else None,
        "strategy_learning_summary_path": str(output_dir / "strategy_learning_summary.json") if learning_summary else None,
        "strategy_learning_memory_path": learning_summary.get("memory_markdown_path") if learning_summary else None,
        "push_summary_path": str(push_summary_path) if push else None,
        "push_results": push_results,
        "memory_path": str(memory_path),
        "candidates": candidates,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行 A 股收盘扫描、回测、台账和推送")
    parser.add_argument("--date", help="输出日期 YYYY-MM-DD，默认今日")
    parser.add_argument("--output", default="output", help="输出根目录")
    parser.add_argument("--limit", type=int, default=5, help="候选数量上限")
    parser.add_argument("--enrich-limit", type=int, help="历史 K 补齐样本数，默认读 strategy.json")
    parser.add_argument("--strategy", default="strategy.json", help="策略配置 JSON 路径")
    parser.add_argument("--push", action="store_true", help="生成扫描报告后推送")
    parser.add_argument("--push-mode", choices=["digest", "full"], default="digest", help="推送摘要或完整报告")
    parser.add_argument("--push-dry-run", action="store_true", help="只检查推送配置，不发送")
    parser.add_argument("--push-fail-on-error", action="store_true", help="任一推送通道失败时以非零状态退出")
    parser.add_argument("--steward", action="store_true", help="扫描后运行 Hermes 策略管家")
    parser.add_argument(
        "--steward-mode",
        choices=["daily", "deep", "critic", "experiment"],
        help="Hermes 策略管家模式，默认读 strategy.json",
    )
    parser.add_argument("--steward-fail-on-error", action="store_true", help="Hermes 失败时以非零状态退出")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    result = run_market_scan(
        output_base=args.output,
        date=args.date,
        limit=args.limit,
        enrich_limit=args.enrich_limit,
        config_path=args.strategy,
        push=args.push,
        push_mode=args.push_mode,
        push_dry_run=args.push_dry_run,
        steward=args.steward,
        steward_mode=args.steward_mode,
        steward_fail_on_error=args.steward_fail_on_error,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.push and args.push_fail_on_error:
        failed = [item for item in result.get("push_results", []) if item.get("status") == "failed"]
        if failed:
            raise SystemExit(1)
