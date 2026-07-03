"""
quant_core.py - 运行路径共享量化库

汇集每日运行路径（forward_paper_trading_v3、scheduled_v3_reporter）依赖的
数据缓存读取、可交易性判断、动态股票池构建和 V3 策略过滤函数。

函数实现原样抽取自 factor_research / round3 / round6（现位于 research/），
保持行为一致；research/ 下的历史研究轮次不再被运行路径导入。

本模块只读本地缓存与配置，不修改台账、不连接实盘。
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import market_scanner as scanner

DEFAULT_CACHE_DIR = Path("data/cache/market_scanner/ohlcv")
DEFAULT_INDEX_CACHE_DIR = Path("data/cache/market_scanner/index")
DEFAULT_LIMIT_THRESHOLD = 0.095
RISK_ON_LABEL = "积极"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    """安全转换 float。"""
    if value is None or value == "":
        return None
    try:
        value_float = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value_float) or math.isinf(value_float):
        return None
    return value_float


def _json_default(value: Any) -> Any:
    """JSON 序列化辅助。"""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        if np.isnan(value) or np.isinf(value):
            return None
        return float(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    """写 CSV 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)


# ---------------------------------------------------------------------------
# 历史缓存读取
# ---------------------------------------------------------------------------


def _symbol_from_cache_path(path: Path) -> str:
    """从缓存文件名恢复股票代码。"""
    stem = path.stem
    if "_" in stem:
        code, suffix = stem.rsplit("_", 1)
        return f"{code}.{suffix}"
    return stem


def _load_bars_csv(path: Path) -> list[dict]:
    """读取单只股票缓存 K 线。"""
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            open_price = _safe_float(row.get("open"))
            high = _safe_float(row.get("high"))
            low = _safe_float(row.get("low"))
            close = _safe_float(row.get("close"))
            if open_price is None or high is None or low is None or close is None:
                continue
            rows.append(
                {
                    "date": row.get("date") or "",
                    "date_ms": int(float(row["date_ms"])) if row.get("date_ms") else None,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": _safe_float(row.get("volume")),
                    "turnover": _safe_float(row.get("turnover")),
                }
            )
    return sorted(rows, key=lambda item: (item.get("date_ms") or 0, item.get("date") or ""))


def load_cached_histories(
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_symbols: Optional[int] = None,
    min_bars: int = 80,
) -> dict[str, list[dict]]:
    """从 market scanner 缓存读取历史 K 线。"""
    histories: dict[str, list[dict]] = {}
    if not cache_dir.exists():
        return histories
    paths = sorted(cache_dir.glob("*.csv"))
    if max_symbols is not None:
        paths = paths[:max_symbols]
    for path in paths:
        symbol = _symbol_from_cache_path(path)
        bars = _load_bars_csv(path)
        if len(bars) >= min_bars:
            histories[symbol] = bars
    return histories


def _load_scan_metadata(output_base: Path = Path("output")) -> dict[str, dict]:
    """从已有扫描产物补充名称和行业/板块映射。"""
    result: dict[str, dict] = {}
    for path in sorted(output_base.glob("*/scan/daily_scans.csv")):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                symbol = row.get("symbol")
                if symbol:
                    result[symbol] = {
                        "name": row.get("name") or symbol,
                        "sector": row.get("sector") or "未分类",
                    }
    return result


# ---------------------------------------------------------------------------
# 市场环境时间线
# ---------------------------------------------------------------------------


def _market_timeline_from_cache(index_cache_dir: Optional[Path] = None) -> dict[str, dict]:
    """从指数缓存生成市场环境时间线。"""
    if index_cache_dir is None:
        index_cache_dir = DEFAULT_INDEX_CACHE_DIR
    if not index_cache_dir.exists():
        return {}
    indexes: list[dict] = []
    label_by_code = {
        "000001_SH": "上证",
        "399001_SZ": "深成指",
        "399006_SZ": "创业板",
        "000300_SH": "沪深300",
    }
    for path in sorted(index_cache_dir.glob("*.csv")):
        bars = _load_bars_csv(path)
        if len(bars) < 80:
            continue
        indexes.append({"label": label_by_code.get(path.stem, path.stem), "_history_bars": bars})
    return scanner._build_market_timeline(indexes) if indexes else {}


def _market_timeline() -> dict[str, dict]:
    """读取指数缓存生成市场环境时间线。"""
    return _market_timeline_from_cache(DEFAULT_INDEX_CACHE_DIR)


# ---------------------------------------------------------------------------
# 可交易性判断
# ---------------------------------------------------------------------------


def _prev_close_from_bars(bars: list[dict], idx: int) -> Optional[float]:
    """取前收盘价。"""
    if idx <= 0:
        return None
    return _safe_float(bars[idx - 1].get("close"))


def _is_limit_up(bar: dict, prev_close: Optional[float], threshold: float = DEFAULT_LIMIT_THRESHOLD) -> bool:
    """近似判断涨停无法买入。"""
    if prev_close is None or prev_close <= 0:
        return False
    low = _safe_float(bar.get("low"))
    close = _safe_float(bar.get("close"))
    if low is None or close is None:
        return False
    return low >= prev_close * (1 + threshold) or close >= prev_close * (1 + threshold)


def _is_limit_down(bar: dict, prev_close: Optional[float], threshold: float = DEFAULT_LIMIT_THRESHOLD) -> bool:
    """近似判断跌停无法卖出。"""
    if prev_close is None or prev_close <= 0:
        return False
    high = _safe_float(bar.get("high"))
    close = _safe_float(bar.get("close"))
    if high is None or close is None:
        return False
    return high <= prev_close * (1 - threshold) or close <= prev_close * (1 - threshold)


def _is_suspended_or_untradeable(bar: Optional[dict]) -> bool:
    """判断停牌或不可交易。"""
    if not bar:
        return True
    volume = _safe_float(bar.get("volume"))
    turnover = _safe_float(bar.get("turnover"))
    open_price = _safe_float(bar.get("open"))
    high = _safe_float(bar.get("high"))
    low = _safe_float(bar.get("low"))
    close = _safe_float(bar.get("close"))
    if open_price is None or high is None or low is None or close is None:
        return True
    if volume is not None and volume <= 0:
        return True
    if turnover is not None and turnover <= 0:
        return True
    return False


def _can_sell(bar: dict, prev_close: Optional[float], limit_threshold: float) -> bool:
    """判断当天是否可卖。"""
    return not _is_limit_down(bar, prev_close, threshold=limit_threshold) and not _is_suspended_or_untradeable(bar)


# ---------------------------------------------------------------------------
# 因子与横截面处理
# ---------------------------------------------------------------------------


def _upper_shadow_ratio(item: dict) -> Optional[float]:
    """上影线比例，仅作标签。"""
    high = _safe_float(item.get("high"))
    low = _safe_float(item.get("low"))
    close = _safe_float(item.get("latest") or item.get("close"))
    if high is None or low is None or close is None or high <= low:
        return None
    return (high - close) / (high - low)


def _standardize(values: list[Optional[float]]) -> list[Optional[float]]:
    """横截面去极值、缺失填充和 z-score。"""
    series = pd.Series(values, dtype="float64").replace([np.inf, -np.inf], np.nan)
    if series.notna().sum() == 0:
        return [None for _ in values]
    low = series.quantile(0.01)
    high = series.quantile(0.99)
    filled = series.clip(low, high).fillna(series.median())
    std = filled.std(ddof=0)
    if std == 0 or pd.isna(std):
        return [0.0 for _ in values]
    return [float(x) for x in ((filled - filled.mean()) / std)]


def _build_alpha040_map(histories: dict[str, list[dict]]) -> dict[tuple[str, str], float]:
    """计算 alpha040 原始值映射。"""
    result: dict[tuple[str, str], float] = {}
    for symbol, bars in histories.items():
        dates: list[str] = []
        closes: list[Optional[float]] = []
        volumes: list[Optional[float]] = []
        for bar in bars:
            date = str(bar.get("date") or "")
            if not date:
                continue
            dates.append(date)
            closes.append(_safe_float(bar.get("close")))
            volumes.append(_safe_float(bar.get("volume")))
        if len(dates) < 27:
            continue
        close = pd.Series(closes, dtype="float64")
        volume = pd.Series(volumes, dtype="float64")
        prev_close = close.shift(1)
        up_volume = volume.where(close > prev_close, 0.0).rolling(26, min_periods=26).sum()
        down_volume = volume.where(close <= prev_close, 0.0).rolling(26, min_periods=26).sum()
        alpha040 = (up_volume / down_volume.replace(0, np.nan) * 100).replace([np.inf, -np.inf], np.nan)
        for date, value in zip(dates, alpha040):
            if pd.notna(value):
                result[(date, str(symbol))] = float(value)
    return result


# ---------------------------------------------------------------------------
# 动态股票池
# ---------------------------------------------------------------------------


def _date_index(histories: dict[str, list[dict]]) -> tuple[list[str], dict[str, dict[str, int]]]:
    """构建日期索引：date -> symbol -> bar index。"""
    by_date: dict[str, dict[str, int]] = defaultdict(dict)
    for symbol, bars in histories.items():
        for idx, bar in enumerate(bars):
            date = str(bar.get("date") or "")
            if date:
                by_date[date][symbol] = idx
    return sorted(by_date), by_date


def _base_row(symbol: str, metadata: dict[str, dict]) -> dict:
    """构造 scanner 伪快照元数据。"""
    meta = metadata.get(symbol) or {}
    return {
        "name": meta.get("name") or symbol,
        "symkey": symbol,
        "industry_sector": {"name": meta.get("sector") or "未分类"},
        "data_source": "round3_dynamic_universe",
    }


def _build_dynamic_universe(
    histories: dict[str, list[dict]],
    metadata: dict[str, dict],
    alpha040_map: dict[tuple[str, str], float],
    min_history: int,
    output_dir: Path,
) -> tuple[dict[str, list[dict]], pd.DataFrame]:
    """逐日动态生成可交易股票池和剔除原因统计。"""
    dates, by_date = _date_index(histories)
    first_date_by_symbol = {
        symbol: str(bars[0].get("date") or "")
        for symbol, bars in histories.items()
        if bars
    }
    active_symbols = sorted(histories)
    daily_rows: list[dict] = []
    universe_by_date: dict[str, list[dict]] = {}

    min_price = float(scanner._cfg("filters", "min_price", 3))
    min_turnover = float(scanner._cfg("filters", "min_turnover", 1e9))

    for date in dates:
        reason_counts: Counter[str] = Counter()
        rows: list[dict] = []
        considered = 0
        for symbol in active_symbols:
            first_date = first_date_by_symbol.get(symbol)
            if not first_date or first_date > date:
                continue
            considered += 1
            idx = by_date.get(date, {}).get(symbol)
            if idx is None:
                reason_counts["no_bar_on_date"] += 1
                continue
            bars = histories[symbol]
            bar = bars[idx]
            if idx < min_history:
                reason_counts["insufficient_history"] += 1
                continue
            if _is_suspended_or_untradeable(bar):
                reason_counts["suspended_or_invalid_bar"] += 1
                continue
            close = _safe_float(bar.get("close"))
            turnover = _safe_float(bar.get("turnover"))
            prev_close = _prev_close_from_bars(bars, idx)
            if close is None or close <= min_price:
                reason_counts["price_below_min"] += 1
                continue
            if turnover is None or turnover < min_turnover:
                reason_counts["turnover_below_min"] += 1
                continue
            if _is_limit_up(bar, prev_close):
                reason_counts["limit_up_cannot_buy"] += 1
                continue
            if _is_limit_down(bar, prev_close):
                reason_counts["limit_down_not_openable"] += 1
                continue
            row = scanner._signal_row_from_bar(_base_row(symbol, metadata), bars, idx)
            scored = scanner._score_stock(row)
            scored.update(
                {
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "prev_close": row.get("prev_close"),
                    "_signal_idx": idx,
                    "_symbol_key": symbol,
                    "_signal_date": date,
                    "alpha040": alpha040_map.get((date, symbol)),
                    "industry": (metadata.get(symbol) or {}).get("sector") or scored.get("sector") or "未分类",
                    "signal_day_limit_up": _is_limit_up(bar, prev_close),
                    "signal_day_limit_down": _is_limit_down(bar, prev_close),
                }
            )
            scored["upper_shadow_ratio"] = _upper_shadow_ratio(scored)
            rows.append(scored)

        rows = scanner._apply_strategy_overlays(rows) if rows else []
        alpha_z = _standardize([_safe_float(item.get("alpha040")) for item in rows])
        rps60_z = _standardize([_safe_float(item.get("rps60")) for item in rows])
        high_z = _standardize([_safe_float(item.get("close_to_20d_high")) for item in rows])
        for item, alpha_value, rps_value, high_value in zip(rows, alpha_z, rps60_z, high_z):
            item["alpha040_z"] = alpha_value
            item["rps60_z"] = rps_value
            item["close_to_20d_high_z"] = high_value
        universe_by_date[date] = rows
        total_excluded = sum(reason_counts.values())
        daily_rows.append(
            {
                "date": date,
                "considered_symbols": considered,
                "stock_count": len(rows),
                "excluded_total": total_excluded,
                "no_bar_on_date": reason_counts.get("no_bar_on_date", 0),
                "insufficient_history": reason_counts.get("insufficient_history", 0),
                "suspended_or_invalid_bar": reason_counts.get("suspended_or_invalid_bar", 0),
                "price_below_min": reason_counts.get("price_below_min", 0),
                "turnover_below_min": reason_counts.get("turnover_below_min", 0),
                "limit_up_cannot_buy": reason_counts.get("limit_up_cannot_buy", 0),
                "limit_down_not_openable": reason_counts.get("limit_down_not_openable", 0),
                "reason_counts_json": json.dumps(dict(reason_counts), ensure_ascii=False),
            }
        )

    daily_universe = pd.DataFrame(daily_rows)
    _write_csv(output_dir / "daily_universe.csv", daily_universe)
    return universe_by_date, daily_universe


# ---------------------------------------------------------------------------
# V3 策略排序与过滤
# ---------------------------------------------------------------------------


def _alpha040_core_score(item: dict) -> Optional[float]:
    """Alpha040 Core 排序分：不重复加权 rps/change_rate。"""
    alpha = _safe_float(item.get("alpha040_z"))
    rps60 = _safe_float(item.get("rps60_z"))
    high = _safe_float(item.get("close_to_20d_high_z"))
    latest = _safe_float(item.get("latest"))
    ma5 = _safe_float(item.get("ma5"))
    ma10 = _safe_float(item.get("ma10"))
    ma20 = _safe_float(item.get("ma20"))
    if alpha is None or rps60 is None or high is None:
        return None
    if latest is None or ma20 is None or latest < ma20:
        return None
    score = 0.55 * alpha + 0.30 * rps60 + 0.15 * high
    if ma5 is not None and ma10 is not None and ma5 >= ma10 >= ma20:
        score += 0.08
    elif ma5 is not None and ma5 >= ma20:
        score += 0.04
    distance = latest / ma20 - 1 if ma20 else 0.0
    if distance > 0.12:
        score -= min(0.35, (distance - 0.12) * 2.0)
    return round(score, 6)


@dataclass(frozen=True)
class ShadowStrategy:
    """策略过滤配置（源自 Round6 shadow strategy 定义）。"""

    key: str
    label: str
    stop_variant: str
    only_risk_on: bool
    filter_hot_5d: bool
    filter_high_volatility: bool


def _passes_strategy_filters(
    item: dict,
    market_profile: dict[str, Any],
    strategy: ShadowStrategy,
    thresholds: dict[str, float],
) -> tuple[bool, str]:
    """判断某个 Alpha040 候选是否通过策略过滤。"""
    score = _alpha040_core_score(item)
    if score is None:
        return False, "alpha040_core_ineligible"
    if strategy.only_risk_on and market_profile.get("regime_label") != RISK_ON_LABEL:
        return False, "market_not_risk_on"
    if strategy.filter_hot_5d:
        change_5d = _safe_float(item.get("change_rate_5d"))
        if change_5d is None:
            return False, "missing_change_rate_5d"
        if change_5d > thresholds["change_rate_5d_q75"]:
            return False, "hot_5d_filtered"
    if strategy.filter_high_volatility:
        volatility = _safe_float(item.get("volatility_20d"))
        if volatility is None:
            return False, "missing_volatility_20d"
        if volatility > thresholds["volatility_20d_q75"]:
            return False, "high_volatility_filtered"
    return True, "passed"


def _eligible_for_strategy(
    rows: list[dict],
    market_profile: dict[str, Any],
    strategy: ShadowStrategy,
    thresholds: dict[str, float],
) -> tuple[list[dict], dict[str, int]]:
    """返回某策略某日候选及过滤原因计数。"""
    eligible: list[dict] = []
    reason_counts: dict[str, int] = {}
    for item in rows:
        passed, reason = _passes_strategy_filters(item, market_profile, strategy, thresholds)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if not passed:
            continue
        score = _alpha040_core_score(item)
        if score is not None:
            eligible.append({**item, "alpha040_core_score": score})
    eligible.sort(key=lambda row: row.get("alpha040_core_score") or -999, reverse=True)
    return eligible, reason_counts
