"""
factor_research.py - 因子研究与策略改进影子实验模块

本模块只读取历史行情缓存或独立拉取研究数据，输出到 output/factor_research/。
它不修改 market scanner 主策略、不写台账、不连接实盘接口。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Optional

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import market_scanner as scanner

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path("output/factor_research")
DEFAULT_CACHE_DIR = Path("data/cache/market_scanner/ohlcv")
DEFAULT_INDEX_CACHE_DIR = Path("data/cache/market_scanner/index")
DEFAULT_LABEL_HORIZON = 5
DEFAULT_MIN_CROSS_SECTION = 20
DEFAULT_GROUPS = 5


@dataclass(frozen=True)
class FactorSpec:
    """因子元数据。"""

    name: str
    family: str
    description: str
    source: str


FACTOR_SPECS: list[FactorSpec] = [
    FactorSpec("base_score", "current", "现有基础量价打分", "market_scanner"),
    FactorSpec("strategy_score", "current", "现有增强策略分", "market_scanner"),
    FactorSpec("final_score", "current", "基础分与增强分合成", "market_scanner"),
    FactorSpec("rps20", "current", "20 日横截面相对强度", "market_scanner"),
    FactorSpec("rps60", "current", "60 日横截面相对强度", "market_scanner"),
    FactorSpec("change_rate", "current", "当日涨跌幅", "market_scanner"),
    FactorSpec("change_rate_5d", "current", "5 日动量", "market_scanner"),
    FactorSpec("change_rate_20d", "current", "20 日动量", "market_scanner"),
    FactorSpec("change_rate_60d", "current", "60 日动量", "market_scanner"),
    FactorSpec("log_turnover", "current", "成交额对数", "market_scanner"),
    FactorSpec("amplitude", "current", "当日振幅", "market_scanner"),
    FactorSpec("volume_ratio", "current", "成交额相对 20 日均额", "market_scanner"),
    FactorSpec("volatility_20d", "current", "20 日收益波动率", "market_scanner"),
    FactorSpec("close_to_20d_high", "current", "收盘价距离 20 日高点", "market_scanner"),
    FactorSpec("trend_ma5_ma10", "current", "MA5 相对 MA10", "market_scanner"),
    FactorSpec("trend_close_ma20", "current", "收盘价相对 MA20", "market_scanner"),
    FactorSpec("alpha001", "alpha191", "成交量变化与日内实体的 6 日相关，取反", "Alpha191 subset"),
    FactorSpec("alpha002", "alpha191", "收盘位置摆动的一日变化，取反", "Alpha191 subset"),
    FactorSpec("alpha003", "alpha191", "6 日真实价格推动量", "Alpha191 subset"),
    FactorSpec("alpha004", "alpha191", "短均线、8 日波动和量能条件信号", "Alpha191 subset"),
    FactorSpec("alpha005", "alpha191", "量能时序排名与高价排名相关强度，取反", "Alpha191 subset"),
    FactorSpec("alpha006", "alpha191", "开高加权价 4 日变化方向，取反", "Alpha191 subset"),
    FactorSpec("alpha008", "alpha191", "高低中价与 VWAP 加权价 4 日变化，取反", "Alpha191 subset"),
    FactorSpec("alpha011", "alpha191", "6 日收盘位置量能累积", "Alpha191 subset"),
    FactorSpec("alpha014", "alpha191", "5 日价格差", "Alpha191 subset"),
    FactorSpec("alpha015", "alpha191", "开盘相对昨收跳空", "Alpha191 subset"),
    FactorSpec("alpha018", "alpha191", "5 日价格比", "Alpha191 subset"),
    FactorSpec("alpha019", "alpha191", "5 日条件收益率", "Alpha191 subset"),
    FactorSpec("alpha020", "alpha191", "6 日收益率百分值", "Alpha191 subset"),
    FactorSpec("alpha024", "alpha191", "5 日价差的 5 日均值", "Alpha191 subset"),
    FactorSpec("alpha028", "alpha191", "9 日随机强弱位置平滑", "Alpha191 subset"),
    FactorSpec("alpha031", "alpha191", "收盘价相对 12 日均线偏离", "Alpha191 subset"),
    FactorSpec("alpha034", "alpha191", "12 日均线相对收盘价", "Alpha191 subset"),
    FactorSpec("alpha040", "alpha191", "26 日上涨量与下跌量比", "Alpha191 subset"),
    FactorSpec("alpha043", "alpha191", "6 日方向量能累积", "Alpha191 subset"),
    FactorSpec("alpha047", "alpha191", "6 日高低区间中的收盘弱势位置", "Alpha191 subset"),
]

CURRENT_FACTOR_COLUMNS = [spec.name for spec in FACTOR_SPECS if spec.family == "current"]
ALPHA191_COLUMNS = [spec.name for spec in FACTOR_SPECS if spec.family == "alpha191"]
FACTOR_COLUMNS = CURRENT_FACTOR_COLUMNS + ALPHA191_COLUMNS


ABLATION_COMPONENTS = {
    "rps": "RPS 动量",
    "trend": "均线趋势",
    "volume": "量能",
    "volatility": "波动",
    "strength_zone": "强势区",
}


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


def _base_stock_rows_from_histories(histories: dict[str, list[dict]]) -> dict[str, dict]:
    """为历史回测构造最小股票元数据。"""
    return {
        symbol: {
            "name": symbol,
            "symkey": symbol,
            "industry_sector": {"name": "未分类"},
            "data_source": "factor_research_cache",
        }
        for symbol in histories
    }


def _bars_to_panel(histories: dict[str, list[dict]]) -> pd.DataFrame:
    """将历史 K 线转成长表。"""
    rows: list[dict] = []
    for symbol, bars in histories.items():
        for bar in bars:
            date = bar.get("date")
            if not date:
                continue
            volume = _safe_float(bar.get("volume"))
            turnover = _safe_float(bar.get("turnover"))
            close = _safe_float(bar.get("close"))
            vwap = None
            if volume and turnover:
                vwap = turnover / volume
            rows.append(
                {
                    "date": pd.to_datetime(date),
                    "symbol": symbol,
                    "open": _safe_float(bar.get("open")),
                    "high": _safe_float(bar.get("high")),
                    "low": _safe_float(bar.get("low")),
                    "close": close,
                    "volume": volume,
                    "turnover": turnover,
                    "vwap": vwap if vwap and close and 0.2 * close <= vwap <= 5 * close else close,
                }
            )
    panel = pd.DataFrame(rows)
    if panel.empty:
        return panel
    return panel.sort_values(["symbol", "date"]).reset_index(drop=True)


def _ts_rank(series: pd.Series, window: int) -> pd.Series:
    """滚动窗口内最后一个值的百分位排名。"""
    return series.rolling(window, min_periods=window).apply(
        lambda values: pd.Series(values).rank(pct=True).iloc[-1],
        raw=False,
    )


def _rolling_corr(left: pd.Series, right: pd.Series, window: int) -> pd.Series:
    """滚动相关系数，统一处理无穷值。"""
    corr = left.rolling(window, min_periods=window).corr(right)
    return corr.replace([np.inf, -np.inf], np.nan)


def _sma_cn(series: pd.Series, window: int, weight: int = 1) -> pd.Series:
    """近似 Alpha191 中 SMA(X,N,M) 的递推平滑。"""
    alpha = weight / window
    return series.ewm(alpha=alpha, adjust=False, min_periods=window).mean()


def _compute_alpha191_for_symbol(group: pd.DataFrame) -> pd.DataFrame:
    """计算 20 个短周期 Alpha191 子集因子。"""
    group = group.sort_values("date").copy()
    open_ = group["open"].astype(float)
    high = group["high"].astype(float)
    low = group["low"].astype(float)
    close = group["close"].astype(float)
    volume = group["volume"].astype(float).replace(0, np.nan)
    vwap = group["vwap"].astype(float).fillna(close)
    ret = close.pct_change()
    high_low = (high - low).replace(0, np.nan)

    close_pos = ((close - low) - (high - close)) / high_low
    group["alpha001"] = -_rolling_corr(np.log(volume).diff(), (close - open_) / open_.replace(0, np.nan), 6)
    group["alpha002"] = -close_pos.diff()

    prev_close = close.shift(1)
    up_leg = close - pd.concat([low, prev_close], axis=1).min(axis=1)
    down_leg = close - pd.concat([high, prev_close], axis=1).max(axis=1)
    alpha003_raw = np.where(close == prev_close, 0, np.where(close > prev_close, up_leg, down_leg))
    group["alpha003"] = pd.Series(alpha003_raw, index=group.index).rolling(6, min_periods=6).sum()

    mean8 = close.rolling(8, min_periods=8).mean()
    std8 = close.rolling(8, min_periods=8).std()
    mean2 = close.rolling(2, min_periods=2).mean()
    vol_ratio20 = volume / volume.rolling(20, min_periods=20).mean()
    group["alpha004"] = np.select(
        [mean8 + std8 < mean2, mean2 < mean8 - std8, vol_ratio20 >= 1],
        [-1.0, 1.0, 1.0],
        default=-1.0,
    )

    group["alpha005"] = -_rolling_corr(_ts_rank(volume, 5), _ts_rank(high, 5), 5).rolling(3, min_periods=3).max()
    group["alpha006"] = -np.sign((open_ * 0.85 + high * 0.15).diff(4))
    group["alpha008"] = -(((high + low) / 2 * 0.2 + vwap * 0.8).diff(4))
    group["alpha011"] = (close_pos * volume).rolling(6, min_periods=6).sum()
    group["alpha014"] = close - close.shift(5)
    group["alpha015"] = open_ / close.shift(1).replace(0, np.nan) - 1
    group["alpha018"] = close / close.shift(5).replace(0, np.nan)

    delayed5 = close.shift(5).replace(0, np.nan)
    group["alpha019"] = np.where(
        close < delayed5,
        (close - delayed5) / delayed5,
        np.where(close == delayed5, 0.0, (close - delayed5) / close.replace(0, np.nan)),
    )
    group["alpha020"] = (close - close.shift(6)) / close.shift(6).replace(0, np.nan) * 100
    group["alpha024"] = _sma_cn(close - close.shift(5), 5, 1)

    low9 = low.rolling(9, min_periods=9).min()
    high9 = high.rolling(9, min_periods=9).max()
    raw_k = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    smooth_k = _sma_cn(raw_k, 3, 1)
    group["alpha028"] = 3 * smooth_k - 2 * _sma_cn(smooth_k, 3, 1)
    ma12 = close.rolling(12, min_periods=12).mean()
    group["alpha031"] = (close - ma12) / ma12.replace(0, np.nan) * 100
    group["alpha034"] = ma12 / close.replace(0, np.nan)

    up_volume = volume.where(close > prev_close, 0.0).rolling(26, min_periods=26).sum()
    down_volume = volume.where(close <= prev_close, 0.0).rolling(26, min_periods=26).sum()
    group["alpha040"] = up_volume / down_volume.replace(0, np.nan) * 100
    direction_volume = np.where(close > prev_close, volume, np.where(close < prev_close, -volume, 0.0))
    group["alpha043"] = pd.Series(direction_volume, index=group.index).rolling(6, min_periods=6).sum()

    high6 = high.rolling(6, min_periods=6).max()
    low6 = low.rolling(6, min_periods=6).min()
    group["alpha047"] = _sma_cn((high6 - close) / (high6 - low6).replace(0, np.nan) * 100, 9, 1)

    for col in ALPHA191_COLUMNS:
        group[col] = pd.to_numeric(group[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    return group


def _compute_alpha191_subset(panel: pd.DataFrame) -> pd.DataFrame:
    """按股票计算 Alpha191 子集。"""
    if panel.empty:
        return panel
    frames = [_compute_alpha191_for_symbol(group) for _symbol, group in panel.groupby("symbol", sort=False)]
    return pd.concat(frames, ignore_index=True) if frames else panel


def _signal_rows_by_date(
    histories: dict[str, list[dict]],
    min_history: int,
    label_horizon: int,
    max_hold_days: int,
    entry_window_days: int,
) -> dict[str, list[dict]]:
    """把历史 K 线转换为按日期聚合的扫描信号行。"""
    base_rows = _base_stock_rows_from_histories(histories)
    rows_by_date: dict[str, list[dict]] = defaultdict(list)
    tail_padding = max(label_horizon, max_hold_days + entry_window_days)
    for symbol, bars in histories.items():
        if len(bars) < min_history + tail_padding + 1:
            continue
        base_row = base_rows[symbol]
        for idx in range(min_history, len(bars) - tail_padding):
            row = scanner._signal_row_from_bar(base_row, bars, idx)
            scored = scanner._score_stock(row)
            scored["open"] = row.get("open")
            scored["high"] = row.get("high")
            scored["low"] = row.get("low")
            scored["close"] = row.get("close")
            scored["prev_close"] = row.get("prev_close")
            scored["_signal_idx"] = idx
            scored["_symbol_key"] = symbol
            signal_date = bars[idx].get("date")
            if signal_date:
                rows_by_date[str(signal_date)].append(scored)
    return rows_by_date


def _component_flags(item: dict) -> dict[str, bool]:
    """计算当前策略增强组件的布尔标签。"""
    latest = _safe_float(item.get("latest"))
    ma5 = _safe_float(item.get("ma5"))
    ma10 = _safe_float(item.get("ma10"))
    ma20 = _safe_float(item.get("ma20"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    volatility_20d = _safe_float(item.get("volatility_20d"))
    close_to_high = _safe_float(item.get("close_to_20d_high"))
    rps20 = _safe_float(item.get("rps20"))
    rps60 = _safe_float(item.get("rps60"))
    return {
        "rps": rps20 is not None and rps60 is not None and rps20 >= 0.65 and rps60 >= 0.45,
        "trend": latest is not None and ma5 is not None and ma10 is not None and ma20 is not None and latest >= ma20 and ma5 >= ma10 >= ma20,
        "volume": volume_ratio is not None and 0.6 <= volume_ratio <= 3.5,
        "volatility": volatility_20d is not None and volatility_20d <= 0.075,
        "strength_zone": close_to_high is not None and close_to_high >= -0.08,
    }


def _component_scores(item: dict) -> dict[str, float]:
    """按当前增强分规则拆出组件贡献。"""
    rps20 = _safe_float(item.get("rps20")) or 0.0
    rps60 = _safe_float(item.get("rps60")) or 0.0
    flags = _component_flags(item)
    volume_ratio = _safe_float(item.get("volume_ratio"))
    volatility_20d = _safe_float(item.get("volatility_20d"))
    close_to_high = _safe_float(item.get("close_to_20d_high"))
    scores = {
        "rps": rps20 * 30 + rps60 * 18,
        "trend": 18.0 if flags["trend"] else 0.0,
        "volume": 12.0 if volume_ratio is not None and 0.8 <= volume_ratio <= 2.8 else (0.0 if volume_ratio is None or volume_ratio >= 5 else 3.0),
        "volatility": 10.0 if volatility_20d is not None and volatility_20d <= 0.055 else 0.0,
        "strength_zone": 12.0 if close_to_high is not None and -0.04 <= close_to_high <= 0.015 else 0.0,
    }
    return scores


def _hard_research_pass(item: dict) -> bool:
    """不含增强组件的基础硬过滤。"""
    latest = _safe_float(item.get("latest"))
    turnover = _safe_float(item.get("turnover"))
    change_rate = _safe_float(item.get("change_rate"))
    change_rate_5d = _safe_float(item.get("change_rate_5d"))
    change_rate_60d = _safe_float(item.get("change_rate_60d"))
    amplitude = _safe_float(item.get("amplitude"))
    market_cap = _safe_float(item.get("market_cap"))
    if latest is None or latest <= scanner._cfg("filters", "min_price", 3):
        return False
    if turnover is None or turnover < scanner._cfg("filters", "min_turnover", 1e9):
        return False
    if market_cap is not None and market_cap < scanner._cfg("filters", "min_market_cap", 5e10):
        return False
    if change_rate is None or change_rate <= 0:
        return False
    if change_rate >= scanner._cfg("filters", "max_change_rate", 0.095):
        return False
    if amplitude is not None and amplitude >= scanner._cfg("filters", "max_amplitude", 0.12):
        return False
    if change_rate_5d is not None and change_rate_5d >= scanner._cfg("filters", "max_5d_return", 0.28):
        return False
    if change_rate_60d is not None and change_rate_60d >= scanner._cfg("filters", "max_60d_return", 1.5):
        return False
    return True


def _upper_shadow_ratio(item: dict) -> Optional[float]:
    """计算冲高回落程度。"""
    high = _safe_float(item.get("high"))
    low = _safe_float(item.get("low"))
    close = _safe_float(item.get("latest") or item.get("close"))
    if high is None or low is None or close is None or high <= low:
        return None
    return (high - close) / (high - low)


def _annotate_scored_items(rows_by_date: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """按日期补 RPS、组件标签和组件分。"""
    annotated: dict[str, list[dict]] = {}
    for date, rows in rows_by_date.items():
        overlaid = scanner._apply_strategy_overlays(rows)
        for item in overlaid:
            flags = _component_flags(item)
            scores = _component_scores(item)
            item["_component_flags"] = flags
            item["_component_scores"] = scores
            item["_upper_shadow_ratio"] = _upper_shadow_ratio(item)
            item["base_score"] = item.get("score")
            item["log_turnover"] = math.log(item["turnover"]) if _safe_float(item.get("turnover")) and item["turnover"] > 0 else None
            ma5 = _safe_float(item.get("ma5"))
            ma10 = _safe_float(item.get("ma10"))
            ma20 = _safe_float(item.get("ma20"))
            latest = _safe_float(item.get("latest"))
            item["trend_ma5_ma10"] = ma5 / ma10 - 1 if ma5 is not None and ma10 else None
            item["trend_close_ma20"] = latest / ma20 - 1 if latest is not None and ma20 else None
        annotated[date] = overlaid
    return annotated


def _market_timeline_from_cache(index_cache_dir: Path = DEFAULT_INDEX_CACHE_DIR) -> dict[str, dict]:
    """从指数缓存生成市场环境时间线。"""
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


def _factor_frame_from_scored(rows_by_date: dict[str, list[dict]]) -> pd.DataFrame:
    """把现有策略因子转成长表。"""
    rows: list[dict] = []
    for date, items in rows_by_date.items():
        for item in items:
            row = {
                "date": pd.to_datetime(date),
                "symbol": item.get("symbol"),
            }
            for col in CURRENT_FACTOR_COLUMNS:
                row[col] = _safe_float(item.get(col))
            rows.append(row)
    return pd.DataFrame(rows)


def _build_factor_dataset(
    histories: dict[str, list[dict]],
    label_horizon: int,
    min_history: int,
    max_hold_days: int,
    entry_window_days: int,
) -> tuple[pd.DataFrame, dict[str, list[dict]]]:
    """构建含当前因子、Alpha191 子集和未来超额收益标签的数据集。"""
    panel = _compute_alpha191_subset(_bars_to_panel(histories))
    if panel.empty:
        return panel, {}

    panel["future_5d_return"] = panel.groupby("symbol")["close"].shift(-label_horizon) / panel["close"] - 1
    market_median = panel.groupby("date")["future_5d_return"].median().rename("market_future_5d_median")
    panel = panel.join(market_median, on="date")
    panel["future_5d_excess_return"] = panel["future_5d_return"] - panel["market_future_5d_median"]

    rows_by_date = _annotate_scored_items(
        _signal_rows_by_date(
            histories,
            min_history=min_history,
            label_horizon=label_horizon,
            max_hold_days=max_hold_days,
            entry_window_days=entry_window_days,
        )
    )
    current_factors = _factor_frame_from_scored(rows_by_date)
    dataset = panel.merge(current_factors, on=["date", "symbol"], how="inner")
    dataset = dataset.dropna(subset=["future_5d_excess_return"]).copy()
    return dataset, rows_by_date


def _cross_section_preprocess(
    dataset: pd.DataFrame,
    factor_cols: list[str],
    lower_q: float = 0.01,
    upper_q: float = 0.99,
) -> pd.DataFrame:
    """因子去极值、缺失处理、横截面标准化。"""
    work = dataset.copy()
    processed_cols: list[str] = []
    for col in factor_cols:
        out_col = f"{col}_z"

        def process_one_day(series: pd.Series) -> pd.Series:
            numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
            if numeric.notna().sum() == 0:
                return numeric
            low = numeric.quantile(lower_q)
            high = numeric.quantile(upper_q)
            clipped = numeric.clip(low, high)
            filled = clipped.fillna(clipped.median())
            std = filled.std(ddof=0)
            if std == 0 or pd.isna(std):
                return filled * 0.0
            return (filled - filled.mean()) / std

        work[out_col] = work.groupby("date", group_keys=False)[col].transform(process_one_day)
        processed_cols.append(out_col)
    work.attrs["processed_factor_columns"] = processed_cols
    return work


def _pearson(x: pd.Series, y: pd.Series) -> Optional[float]:
    """安全 Pearson 相关。"""
    valid = pd.concat([x, y], axis=1).dropna()
    if len(valid) < 3:
        return None
    left = valid.iloc[:, 0]
    right = valid.iloc[:, 1]
    if left.std(ddof=0) == 0 or right.std(ddof=0) == 0:
        return None
    return float(left.corr(right, method="pearson"))


def _rank_ic(x: pd.Series, y: pd.Series) -> Optional[float]:
    """安全 Spearman RankIC。"""
    valid = pd.concat([x, y], axis=1).dropna()
    if len(valid) < 3:
        return None
    left = valid.iloc[:, 0]
    right = valid.iloc[:, 1]
    if left.nunique() <= 1 or right.nunique() <= 1:
        return None
    return _pearson(left.rank(method="average"), right.rank(method="average"))


def _split_dates(dates: Iterable[pd.Timestamp], train_ratio: float = 0.7) -> tuple[pd.Timestamp, pd.Timestamp]:
    """按时间顺序切分日期，禁止随机切分。"""
    unique_dates = sorted(pd.to_datetime(pd.Series(list(dates)).dropna().unique()))
    if len(unique_dates) < 3:
        raise ValueError("有效日期不足，无法进行时间切分")
    cutoff_idx = max(1, min(len(unique_dates) - 2, int(len(unique_dates) * train_ratio) - 1))
    return unique_dates[cutoff_idx], unique_dates[cutoff_idx + 1]


def _compute_ic_tables(
    dataset: pd.DataFrame,
    factor_cols: list[str],
    min_cross_section: int,
    train_end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """计算每日 IC/RankIC 和汇总。"""
    rows: list[dict] = []
    label = "future_5d_excess_return"
    for date, group in dataset.groupby("date"):
        if len(group) < min_cross_section:
            continue
        split = "train" if date <= train_end else "test"
        for col in factor_cols:
            z_col = f"{col}_z"
            ic = _pearson(group[z_col], group[label])
            rank_ic = _rank_ic(group[z_col], group[label])
            rows.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "split": split,
                    "factor": col,
                    "sample_size": int(group[[z_col, label]].dropna().shape[0]),
                    "ic": ic,
                    "rank_ic": rank_ic,
                }
            )
    daily = pd.DataFrame(rows)
    summary_rows: list[dict] = []
    if daily.empty:
        return daily, pd.DataFrame()
    for (factor, split), group in daily.groupby(["factor", "split"]):
        for metric in ["ic", "rank_ic"]:
            values = pd.to_numeric(group[metric], errors="coerce").dropna()
            std = values.std(ddof=1)
            mean = values.mean()
            summary_rows.append(
                {
                    "factor": factor,
                    "split": split,
                    "metric": metric,
                    "date_count": int(values.shape[0]),
                    "mean": round(float(mean), 6) if pd.notna(mean) else None,
                    "median": round(float(values.median()), 6) if not values.empty else None,
                    "std": round(float(std), 6) if pd.notna(std) else None,
                    "ir": round(float(mean / std), 6) if std and pd.notna(std) else None,
                    "positive_rate": round(float((values > 0).mean()), 6) if not values.empty else None,
                    "t_stat": round(float(mean / (std / math.sqrt(len(values)))), 6) if std and len(values) > 1 else None,
                }
            )
    summary = pd.DataFrame(summary_rows).sort_values(["split", "metric", "mean"], ascending=[True, True, False])
    return daily, summary


def _compute_group_returns(
    dataset: pd.DataFrame,
    factor_cols: list[str],
    groups: int,
    min_cross_section: int,
    train_end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """计算因子分组未来超额收益。"""
    label = "future_5d_excess_return"
    rows: list[dict] = []
    for date, group in dataset.groupby("date"):
        if len(group) < min_cross_section:
            continue
        split = "train" if date <= train_end else "test"
        for col in factor_cols:
            z_col = f"{col}_z"
            valid = group[[z_col, label]].dropna().copy()
            if valid[z_col].nunique() < groups or len(valid) < groups * 3:
                continue
            try:
                valid["group"] = pd.qcut(valid[z_col], groups, labels=False, duplicates="drop") + 1
            except ValueError:
                continue
            for bucket, bucket_df in valid.groupby("group"):
                rows.append(
                    {
                        "date": date.strftime("%Y-%m-%d"),
                        "split": split,
                        "factor": col,
                        "group": int(bucket),
                        "sample_size": int(len(bucket_df)),
                        "mean_future_5d_excess_return": float(bucket_df[label].mean()),
                    }
                )
    detail = pd.DataFrame(rows)
    if detail.empty:
        return detail, pd.DataFrame()
    summary_rows: list[dict] = []
    for (factor, split), group in detail.groupby(["factor", "split"]):
        pivot = group.pivot_table(
            index="date",
            columns="group",
            values="mean_future_5d_excess_return",
            aggfunc="mean",
        )
        if 1 not in pivot.columns or groups not in pivot.columns:
            continue
        long_short = pivot[groups] - pivot[1]
        row = {
            "factor": factor,
            "split": split,
            "date_count": int(long_short.dropna().shape[0]),
            "long_short_mean": round(float(long_short.mean()), 6) if long_short.notna().any() else None,
            "long_short_median": round(float(long_short.median()), 6) if long_short.notna().any() else None,
            "long_short_positive_rate": round(float((long_short > 0).mean()), 6) if long_short.notna().any() else None,
        }
        for bucket in range(1, groups + 1):
            if bucket in pivot.columns:
                row[f"group_{bucket}_mean"] = round(float(pivot[bucket].mean()), 6)
        summary_rows.append(row)
    summary = pd.DataFrame(summary_rows).sort_values(["split", "long_short_mean"], ascending=[True, False])
    return detail, summary


def _factor_correlation_matrix(dataset: pd.DataFrame, factor_cols: list[str]) -> pd.DataFrame:
    """计算标准化因子相关性矩阵。"""
    z_cols = [f"{col}_z" for col in factor_cols]
    corr = dataset[z_cols].corr(method="pearson")
    corr.index = factor_cols
    corr.columns = factor_cols
    return corr.round(6)


def _variant_adjusted_score(item: dict, removed_component: Optional[str]) -> float:
    """计算消融后的综合分。"""
    base = _safe_float(item.get("score")) or 0.0
    strategy_score = _safe_float(item.get("strategy_score")) or 0.0
    if removed_component:
        strategy_score -= (item.get("_component_scores") or {}).get(removed_component, 0.0)
    return round(base * 0.5 + max(strategy_score, 0.0) * 0.5, 4)


def _variant_allows_item(
    item: dict,
    variant: str,
    market_profile: Optional[dict],
) -> bool:
    """判断某个影子消融实验是否允许该信号。"""
    if not _hard_research_pass(item):
        return False
    flags = item.get("_component_flags") or {}
    removed = None
    if variant.startswith("remove_"):
        removed = variant.replace("remove_", "", 1)

    for component in ABLATION_COMPONENTS:
        if component == removed:
            continue
        if not flags.get(component):
            return False

    if variant == "add_defensive_no_trade" and market_profile and market_profile.get("regime") == "defensive":
        return False

    if variant == "add_intraday_reversal_filter":
        upper_shadow = item.get("_upper_shadow_ratio")
        if upper_shadow is not None and upper_shadow >= 0.45:
            return False
    return True


def _summarize_trades(trades: list[dict]) -> dict:
    """汇总消融实验交易表现。"""
    returns = [_safe_float(trade.get("net_return")) for trade in trades if _safe_float(trade.get("net_return")) is not None]
    reason_counts: dict[str, int] = {}
    for trade in trades:
        reason = str(trade.get("exit_reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    if not returns:
        return {
            "trade_count": 0,
            "win_rate": None,
            "average_return": None,
            "median_return": None,
            "best_trade_return": None,
            "worst_trade_return": None,
            "event_sequence_max_drawdown": None,
            "exit_reason_counts": reason_counts,
        }
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for ret in returns:
        equity *= 1 + ret
        peak = max(peak, equity)
        if peak:
            max_drawdown = min(max_drawdown, equity / peak - 1)
    return {
        "trade_count": len(returns),
        "win_rate": round(sum(1 for ret in returns if ret > 0) / len(returns), 4),
        "average_return": round(sum(returns) / len(returns), 5),
        "median_return": round(float(median(returns)), 5),
        "best_trade_return": round(max(returns), 5),
        "worst_trade_return": round(min(returns), 5),
        "event_sequence_max_drawdown": round(max_drawdown, 5),
        "exit_reason_counts": reason_counts,
    }


def _run_ablation_experiments(
    histories: dict[str, list[dict]],
    rows_by_date: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    train_end: pd.Timestamp,
    test_start: pd.Timestamp,
    min_score: float,
    max_hold_days: int,
    entry_window_days: int,
    cost_bps: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """运行新增因子消融影子实验。"""
    variants = [
        "baseline_factor_composite",
        "remove_rps",
        "remove_trend",
        "remove_volume",
        "remove_volatility",
        "remove_strength_zone",
        "add_defensive_no_trade",
        "add_time_stop",
        "add_intraday_reversal_filter",
    ]
    variant_labels = {
        "baseline_factor_composite": "当前因子组合影子基线",
        "remove_rps": "去掉 RPS",
        "remove_trend": "去掉趋势",
        "remove_volume": "去掉量能",
        "remove_volatility": "去掉波动",
        "remove_strength_zone": "去掉强势区",
        "add_defensive_no_trade": "加入防守环境不交易",
        "add_time_stop": "加入时间止损",
        "add_intraday_reversal_filter": "加入冲高回落过滤",
    }
    trade_rows: list[dict] = []
    selected_rows: list[dict] = []
    sorted_dates = sorted(rows_by_date)
    for variant in variants:
        next_available_by_symbol: dict[str, str] = {}
        removed = variant.replace("remove_", "", 1) if variant.startswith("remove_") else None
        for date in sorted_dates:
            market_profile = market_timeline.get(date) if market_timeline else None
            if variant == "add_defensive_no_trade" and market_profile and market_profile.get("regime") == "defensive":
                continue
            candidate_limit = int((market_profile or {}).get("candidate_limit") or 5)
            eligible = []
            for item in rows_by_date[date]:
                if not _variant_allows_item(item, variant, market_profile):
                    continue
                adjusted_final_score = _variant_adjusted_score(item, removed)
                if adjusted_final_score < min_score or (_safe_float(item.get("score")) or 0.0) < min_score:
                    continue
                eligible.append({**item, "_adjusted_final_score": adjusted_final_score})
            eligible.sort(key=lambda item: item.get("_adjusted_final_score") or 0.0, reverse=True)
            sector_counts: dict[str, int] = {}
            selected_for_day: list[dict] = []
            for item in eligible:
                sector = item.get("sector") or "unknown"
                if sector_counts.get(sector, 0) >= 2:
                    continue
                selected_for_day.append(item)
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
                if len(selected_for_day) >= candidate_limit:
                    break

            for item in selected_for_day:
                symbol = str(item.get("_symbol_key") or item.get("symbol") or "")
                if symbol and next_available_by_symbol.get(symbol, "") >= date:
                    continue
                bars = histories.get(symbol) or []
                signal_idx = int(item.get("_signal_idx"))
                selected_rows.append(
                    {
                        "experiment": variant,
                        "experiment_label": variant_labels[variant],
                        "signal_date": date,
                        "symbol": item.get("symbol"),
                        "score": item.get("score"),
                        "adjusted_final_score": item.get("_adjusted_final_score"),
                        "market_regime": (market_profile or {}).get("regime"),
                        "market_regime_label": (market_profile or {}).get("regime_label"),
                    }
                )
                trade = scanner._simulate_trade(
                    item,
                    bars,
                    signal_idx,
                    market_profile=market_profile,
                    max_hold_days=3 if variant == "add_time_stop" else max_hold_days,
                    entry_window_days=entry_window_days,
                    cost_bps=cost_bps,
                )
                if trade:
                    if symbol and trade.get("exit_date"):
                        next_available_by_symbol[symbol] = str(trade["exit_date"])
                    split = "train" if pd.to_datetime(trade.get("signal_date")) <= train_end else "test"
                    trade_rows.append(
                        {
                            "experiment": variant,
                            "experiment_label": variant_labels[variant],
                            "split": split,
                            "adjusted_final_score": item.get("_adjusted_final_score"),
                            **{key: value for key, value in trade.items() if not key.startswith("_")},
                        }
                    )
    trades_df = pd.DataFrame(trade_rows)
    selected_df = pd.DataFrame(selected_rows)
    summary_rows: list[dict] = []
    summary_payload: dict[str, Any] = {
        "sample_scope": "消融实验只作用于因子组合影子基线，不修改主 market scanner 策略。",
        "selection_rule": "按日期横截面选择候选，使用回测最低分作为入选底线，并沿用市场环境候选数量上限；该口径用于比较因子组件，不等同于主策略晋级阈值。",
        "time_split": {
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "rule": "按日期升序切分，禁止随机切分。",
        },
        "experiments": {},
    }
    for variant in variants:
        variant_df = trades_df[trades_df["experiment"] == variant] if not trades_df.empty else pd.DataFrame()
        summary_payload["experiments"][variant] = {}
        for split in ["all", "train", "test"]:
            split_df = variant_df if split == "all" else variant_df[variant_df["split"] == split]
            summary = _summarize_trades(split_df.to_dict("records"))
            summary_payload["experiments"][variant][split] = summary
            summary_rows.append(
                {
                    "experiment": variant,
                    "experiment_label": variant_labels[variant],
                    "split": split,
                    **summary,
                }
            )
    summary_df = pd.DataFrame(summary_rows)
    return trades_df, selected_df, summary_df, summary_payload


def _write_dataframe_csv(path: Path, df: pd.DataFrame) -> None:
    """写 DataFrame CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=True if df.index.name else False, encoding="utf-8-sig")


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


def _fmt_pct(value: Any, digits: int = 2) -> str:
    """百分比格式化。"""
    number = _safe_float(value)
    if number is None:
        return "-"
    return f"{number * 100:.{digits}f}%"


def _top_factor_lines(ic_summary: pd.DataFrame, split: str, metric: str, limit: int = 8) -> list[str]:
    """渲染因子排名行。"""
    if ic_summary.empty:
        return ["- 暂无可用 IC 结果。"]
    view = ic_summary[(ic_summary["split"] == split) & (ic_summary["metric"] == metric)].copy()
    if view.empty:
        return [f"- `{split}` 暂无 `{metric}` 结果。"]
    view["abs_mean"] = view["mean"].abs()
    view = view.sort_values(["abs_mean", "date_count"], ascending=[False, False]).head(limit)
    lines = []
    for _, row in view.iterrows():
        lines.append(
            f"- `{row['factor']}`：mean {row['mean']:.4f}，IR {row['ir'] if pd.notna(row['ir']) else '-'}，"
            f"正值率 {_fmt_pct(row['positive_rate'])}，日期数 {int(row['date_count'])}"
        )
    return lines


def _render_report(
    *,
    as_of: str,
    histories: dict[str, list[dict]],
    dataset: pd.DataFrame,
    train_end: pd.Timestamp,
    test_start: pd.Timestamp,
    ic_summary: pd.DataFrame,
    group_summary: pd.DataFrame,
    ablation_summary: pd.DataFrame,
    output_paths: dict[str, str],
) -> str:
    """渲染因子研究报告。"""
    latest_date = dataset["date"].max().strftime("%Y-%m-%d") if not dataset.empty else "-"
    earliest_date = dataset["date"].min().strftime("%Y-%m-%d") if not dataset.empty else "-"
    alpha_count = len(ALPHA191_COLUMNS)
    factor_count = len(FACTOR_COLUMNS)
    lines = [
        "# 因子研究与策略改进 Shadow Report",
        "",
        f"生成日期：{as_of}",
        f"样本区间：{earliest_date} 至 {latest_date}",
        f"股票样本：{len(histories)} 只",
        f"有效因子样本行：{len(dataset)}",
        f"因子数量：{factor_count}，其中 Alpha191 短周期量价子集 {alpha_count} 个",
        "",
        "免责声明：本报告仅用于个人模拟交易、复盘和策略研究，不构成投资建议；本模块不连接实盘接口、不自动下单、不修改主策略。",
        "",
        "## 研究边界",
        "",
        "- 主 `market_scanner` 策略保持不变。",
        "- 所有新增分析只输出到 `output/factor_research/`。",
        "- 标签为未来 5 日超额收益：个股未来 5 日收益 - 当日研究横截面未来 5 日收益中位数。",
        "- 因子预处理统一执行：1%/99% 横截面去极值、当日中位数缺失填充、当日 z-score 标准化。",
        "- 验证使用时间切分：训练段到 `{}`，测试段从 `{}` 开始，禁止随机切分。".format(
            train_end.strftime("%Y-%m-%d"),
            test_start.strftime("%Y-%m-%d"),
        ),
        "- 当前标签的“全市场中位数”以本次载入的研究股票池横截面计算；若缓存未覆盖全 A，报告会保持这个样本边界，不补造缺失股票。",
        "- 消融实验使用回测最低分和每日候选容量做组件对照，不等同于主策略最终晋级阈值。",
        "",
        "## Alpha191 子集",
        "",
        "本轮只纳入 20 个短周期量价因子，覆盖跳空、日内位置、短期动量、量价相关、量能方向和区间强弱，不全量引入 191 个因子。",
        "",
        "| 因子 | 描述 |",
        "|---|---|",
    ]
    for spec in FACTOR_SPECS:
        if spec.family == "alpha191":
            lines.append(f"| `{spec.name}` | {spec.description} |")

    lines.extend(
        [
            "",
            "## IC / RankIC 摘要",
            "",
            "### 测试段绝对 IC 靠前",
            "",
            *_top_factor_lines(ic_summary, "test", "ic"),
            "",
            "### 测试段绝对 RankIC 靠前",
            "",
            *_top_factor_lines(ic_summary, "test", "rank_ic"),
            "",
            "## 分组收益摘要",
            "",
        ]
    )
    if group_summary.empty:
        lines.append("- 暂无可用分组收益结果。")
    else:
        view = group_summary[group_summary["split"] == "test"].copy()
        if view.empty:
            lines.append("- 测试段暂无可用分组收益结果。")
        else:
            view["abs_long_short"] = view["long_short_mean"].abs()
            view = view.sort_values("abs_long_short", ascending=False).head(8)
            for _, row in view.iterrows():
                lines.append(
                    f"- `{row['factor']}`：Q5-Q1 均值 {_fmt_pct(row['long_short_mean'])}，"
                    f"正值率 {_fmt_pct(row['long_short_positive_rate'])}，日期数 {int(row['date_count'])}"
                )

    lines.extend(
        [
            "",
            "## 消融实验摘要",
            "",
            "| 实验 | 分段 | 交易数 | 胜率 | 平均单笔 | 中位单笔 | 最大回撤 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    if not ablation_summary.empty:
        for _, row in ablation_summary[ablation_summary["split"].isin(["test", "all"])].iterrows():
            lines.append(
                f"| {row['experiment_label']} | {row['split']} | {int(row['trade_count'])} | "
                f"{_fmt_pct(row['win_rate'])} | {_fmt_pct(row['average_return'])} | "
                f"{_fmt_pct(row['median_return'])} | {_fmt_pct(row['event_sequence_max_drawdown'])} |"
            )
    else:
        lines.append("| - | - | 0 | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 产物路径",
            "",
            f"- 标准化因子数据集：`{output_paths['factor_dataset']}`",
            f"- 每日 IC：`{output_paths['ic_daily']}`",
            f"- IC 汇总：`{output_paths['ic_summary']}`",
            f"- 分组收益明细：`{output_paths['group_returns']}`",
            f"- 分组收益汇总：`{output_paths['group_summary']}`",
            f"- 因子相关性矩阵：`{output_paths['correlation_matrix']}`",
            f"- 消融交易明细：`{output_paths['ablation_trades']}`",
            f"- 消融汇总：`{output_paths['ablation_summary']}`",
            "",
            "## 下一步建议",
            "",
            "- 只把测试段 RankIC 稳定、分组收益单调、且与现有核心因子相关性不过高的因子放入下一轮 shadow 候选。",
            "- 小样本或训练段好但测试段失效的因子先保留观察，不进入主策略。",
            "- 消融实验只作为策略诊断证据，任何升级都需要人工确认和二次回测。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_factor_research(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    index_cache_dir: Path = DEFAULT_INDEX_CACHE_DIR,
    max_symbols: Optional[int] = None,
    min_bars: int = 80,
    min_history: int = 60,
    label_horizon: int = DEFAULT_LABEL_HORIZON,
    min_cross_section: int = DEFAULT_MIN_CROSS_SECTION,
    groups: int = DEFAULT_GROUPS,
    train_ratio: float = 0.7,
    strategy_path: str = "strategy.json",
) -> dict[str, Any]:
    """执行完整因子研究与策略消融影子实验。"""
    config = scanner._load_scanner_config(strategy_path)
    scanner._set_active_config(config)

    as_of = datetime.now().strftime("%Y-%m-%d")
    output_dir.mkdir(parents=True, exist_ok=True)
    histories = load_cached_histories(cache_dir=cache_dir, max_symbols=max_symbols, min_bars=min_bars)
    if not histories:
        raise RuntimeError(f"未找到可用历史 K 线缓存：{cache_dir}")

    max_hold_days = int(scanner._cfg("backtest", "max_hold_days", 5))
    entry_window_days = int(scanner._cfg("backtest", "entry_window_days", 2))
    cost_bps = float(scanner._cfg("backtest", "cost_bps", 15))
    dataset, rows_by_date = _build_factor_dataset(
        histories,
        label_horizon=label_horizon,
        min_history=min_history,
        max_hold_days=max_hold_days,
        entry_window_days=entry_window_days,
    )
    if dataset.empty:
        raise RuntimeError("因子数据集为空，无法研究")

    dataset = _cross_section_preprocess(dataset, FACTOR_COLUMNS)
    train_end, test_start = _split_dates(dataset["date"], train_ratio=train_ratio)
    ic_daily, ic_summary = _compute_ic_tables(dataset, FACTOR_COLUMNS, min_cross_section, train_end)
    group_returns, group_summary = _compute_group_returns(dataset, FACTOR_COLUMNS, groups, min_cross_section, train_end)
    corr_matrix = _factor_correlation_matrix(dataset, FACTOR_COLUMNS)

    market_timeline = _market_timeline_from_cache(index_cache_dir)
    ablation_trades, ablation_selected, ablation_summary, ablation_payload = _run_ablation_experiments(
        histories=histories,
        rows_by_date=rows_by_date,
        market_timeline=market_timeline,
        train_end=train_end,
        test_start=test_start,
        min_score=float(scanner._cfg("backtest", "min_score", 70)),
        max_hold_days=max_hold_days,
        entry_window_days=entry_window_days,
        cost_bps=cost_bps,
    )

    output_paths = {
        "factor_dataset": str(output_dir / "factor_dataset.csv"),
        "factor_metadata": str(output_dir / "factor_metadata.csv"),
        "ic_daily": str(output_dir / "ic_daily.csv"),
        "ic_summary": str(output_dir / "ic_summary.csv"),
        "group_returns": str(output_dir / "group_returns.csv"),
        "group_summary": str(output_dir / "group_summary.csv"),
        "correlation_matrix": str(output_dir / "correlation_matrix.csv"),
        "ablation_trades": str(output_dir / "ablation_trades.csv"),
        "ablation_selected": str(output_dir / "ablation_selected_signals.csv"),
        "ablation_summary": str(output_dir / "ablation_summary.csv"),
        "ablation_summary_json": str(output_dir / "ablation_summary.json"),
        "summary_json": str(output_dir / "summary.json"),
        "report": str(output_dir / "factor_research_report.md"),
    }

    dataset_export_cols = [
        "date",
        "symbol",
        "future_5d_return",
        "market_future_5d_median",
        "future_5d_excess_return",
        *FACTOR_COLUMNS,
        *[f"{col}_z" for col in FACTOR_COLUMNS],
    ]
    _write_dataframe_csv(Path(output_paths["factor_dataset"]), dataset[dataset_export_cols])
    metadata = pd.DataFrame([spec.__dict__ for spec in FACTOR_SPECS])
    _write_dataframe_csv(Path(output_paths["factor_metadata"]), metadata)
    _write_dataframe_csv(Path(output_paths["ic_daily"]), ic_daily)
    _write_dataframe_csv(Path(output_paths["ic_summary"]), ic_summary)
    _write_dataframe_csv(Path(output_paths["group_returns"]), group_returns)
    _write_dataframe_csv(Path(output_paths["group_summary"]), group_summary)
    corr_matrix.to_csv(output_paths["correlation_matrix"], encoding="utf-8-sig")
    _write_dataframe_csv(Path(output_paths["ablation_trades"]), ablation_trades)
    _write_dataframe_csv(Path(output_paths["ablation_selected"]), ablation_selected)
    _write_dataframe_csv(Path(output_paths["ablation_summary"]), ablation_summary)
    with open(output_paths["ablation_summary_json"], "w", encoding="utf-8") as f:
        json.dump(ablation_payload, f, ensure_ascii=False, indent=2, default=_json_default)

    summary = {
        "as_of": as_of,
        "module": "factor_research_shadow",
        "safety": "不修改主策略、不写台账、不连接实盘接口。",
        "cache_dir": str(cache_dir),
        "history_symbol_count": len(histories),
        "dataset_rows": int(len(dataset)),
        "date_start": dataset["date"].min().strftime("%Y-%m-%d"),
        "date_end": dataset["date"].max().strftime("%Y-%m-%d"),
        "label": "future_5d_excess_return = stock_future_5d_return - cross_section_future_5d_median",
        "preprocess": "1%/99% 横截面去极值 + 当日中位数缺失填充 + 当日 z-score 标准化",
        "time_split": {
            "train_end": train_end.strftime("%Y-%m-%d"),
            "test_start": test_start.strftime("%Y-%m-%d"),
            "train_ratio": train_ratio,
            "random_split": False,
        },
        "factor_count": len(FACTOR_COLUMNS),
        "alpha191_subset_count": len(ALPHA191_COLUMNS),
        "output_paths": output_paths,
    }
    with open(output_paths["summary_json"], "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=_json_default)

    report = _render_report(
        as_of=as_of,
        histories=histories,
        dataset=dataset,
        train_end=train_end,
        test_start=test_start,
        ic_summary=ic_summary,
        group_summary=group_summary,
        ablation_summary=ablation_summary,
        output_paths=output_paths,
    )
    Path(output_paths["report"]).write_text(report, encoding="utf-8")
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行因子研究与策略消融 shadow experiment")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录，默认 output/factor_research")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="股票历史 K 缓存目录")
    parser.add_argument("--index-cache-dir", default=str(DEFAULT_INDEX_CACHE_DIR), help="指数历史 K 缓存目录")
    parser.add_argument("--max-symbols", type=int, help="最多读取多少只股票缓存，默认全部")
    parser.add_argument("--min-bars", type=int, default=80, help="单票最少 K 线数量")
    parser.add_argument("--min-history", type=int, default=60, help="开始生成信号前的最少历史长度")
    parser.add_argument("--label-horizon", type=int, default=DEFAULT_LABEL_HORIZON, help="未来收益标签窗口")
    parser.add_argument("--min-cross-section", type=int, default=DEFAULT_MIN_CROSS_SECTION, help="每日最小横截面样本")
    parser.add_argument("--groups", type=int, default=DEFAULT_GROUPS, help="分组收益组数")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="按时间顺序训练段比例")
    parser.add_argument("--strategy", default="strategy.json", help="读取 market scanner 参数的策略配置")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    summary = run_factor_research(
        output_dir=Path(args.output),
        cache_dir=Path(args.cache_dir),
        index_cache_dir=Path(args.index_cache_dir),
        max_symbols=args.max_symbols,
        min_bars=args.min_bars,
        min_history=args.min_history,
        label_horizon=args.label_horizon,
        min_cross_section=args.min_cross_section,
        groups=args.groups,
        train_ratio=args.train_ratio,
        strategy_path=args.strategy,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
