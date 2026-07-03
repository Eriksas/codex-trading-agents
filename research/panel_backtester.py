"""
panel_backtester.py - 全市场日频向量化回测引擎（2026-07 策略搜索用）

设计目标（相对既往 r3/r4/r6 事件回测的修正）：
1. 复权正确性：优先使用 BaoStock 后复权因子还原真实收益；
   因子缺失时按板块涨跌停物理限制检测除权日并中性化（现金分红不可检测，
   收益被系统性低估约 1.5-2%/年，属保守方向偏差，报告中必须披露）。
2. 执行现实性：T 日收盘出信号，T+1 开盘买入（开盘涨停/停牌禁买），
   持有 H 个交易日后开盘卖出（开盘跌停/停牌顺延），双边成本默认 30bp。
3. 防未来函数：信号矩阵行 t 只允许使用 t 日收盘及之前的信息；
   引擎内部通过"信号日 t → 执行日 t+1"的显式索引偏移完成对齐。
4. 归因基线：内置等权持有基线与随机选股基线，用于区分选股 alpha 与择时 beta。

本模块只读本地数据，不修改主策略、不写台账、不连接实盘。
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
EXPANDED_DIR = ROOT_DIR / "data" / "expanded"
PANEL_CACHE = EXPANDED_DIR / "panel_cache_v1.pkl"
BAOSTOCK_HFQ_DIR = EXPANDED_DIR / "baostock_hfq"

logger = logging.getLogger(__name__)

DEFAULT_COST_BPS_ROUNDTRIP = 30.0
MIN_PRICE = 3.0
MIN_AMOUNT = 3e7            # 日成交额下限（元）；提案口径 ADV≥3000万，个人规模足够
MIN_HISTORY_DAYS = 60       # 上市暖机
LIMIT_TOL = 0.02            # 除权检测容差：|收益|>板块限制+容差 视为除权日
ENTRY_LIMIT_BUFFER = 0.01   # 开盘价距涨/跌停不足 1% 视为无法成交


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------


def _board_limit(ts_code: str) -> float:
    """按代码前缀返回涨跌停幅度（ST 股在 universe 层已剔除）。"""
    prefix = ts_code[:3]
    if prefix in ("300", "301", "688", "689"):
        return 0.20
    return 0.10


def load_panels(force_rebuild: bool = False) -> dict[str, Any]:
    """加载宽表面板，带本地 pickle 缓存。

    Returns 包含:
      open/high/low/close/vol/amount/turnover: DataFrame [date x symbol] float32
      adj_factor: DataFrame，复权因子（BaoStock hfq/raw；缺失时为除权日中性化近似因子）
      adj_source: 'baostock' | 'neutralized'
      limit_pct: Series [symbol] 板块涨跌停幅度
      st_mask / index_close: 辅助数据
    """
    if PANEL_CACHE.exists() and not force_rebuild:
        with open(PANEL_CACHE, "rb") as f:
            panels = pickle.load(f)
        logger.info("panel cache loaded: %s (adj_source=%s)", PANEL_CACHE, panels.get("adj_source"))
        return panels

    logger.info("building panels from %s ...", EXPANDED_DIR)
    kline = pd.read_csv(
        EXPANDED_DIR / "daily_kline.csv",
        usecols=["trade_date", "ts_code", "open", "high", "low", "close", "vol", "amount"],
        dtype={"ts_code": "category"},
    )
    basic = pd.read_csv(
        EXPANDED_DIR / "daily_basic.csv",
        usecols=["trade_date", "ts_code", "turnover_rate"],
        dtype={"ts_code": "category"},
    )
    stock_basic = pd.read_csv(EXPANDED_DIR / "stock_basic.csv")
    # 指数优先用 BaoStock 重建版：原 index_daily.csv (fuyao 源) 2023 年后严重失真
    # （沪深300 2023-12-29 记录 4940 vs 真实 3431），已用多点核对确认。
    index_path = EXPANDED_DIR / "index_daily_baostock.csv"
    if not index_path.exists():
        raise FileNotFoundError(
            "缺少 index_daily_baostock.csv：原 index_daily.csv 数据失真，禁止回退使用。"
        )
    index_daily = pd.read_csv(index_path, usecols=["trade_date", "ts_code", "close"])

    panels: dict[str, Any] = {}
    for col in ["open", "high", "low", "close", "vol", "amount"]:
        panels[col] = kline.pivot(index="trade_date", columns="ts_code", values=col).astype("float32")
    panels["turnover"] = basic.pivot(index="trade_date", columns="ts_code", values="turnover_rate").astype("float32")
    panels["turnover"] = panels["turnover"].reindex(index=panels["close"].index, columns=panels["close"].columns)

    symbols = panels["close"].columns
    panels["limit_pct"] = pd.Series([_board_limit(str(c)) for c in symbols], index=symbols, dtype="float32")

    st_codes = set(stock_basic.loc[stock_basic["is_st"] == True, "ts_code"])  # noqa: E712
    panels["st_mask"] = pd.Series([str(c) in st_codes for c in symbols], index=symbols)

    # 时变 ST 面板（BaoStock isST 日频；硬规则"ST 一概不碰"按历史每日状态执行）
    st_dir = EXPANDED_DIR / "baostock_st"
    st_files = list(st_dir.glob("*.csv")) if st_dir.exists() else []
    if len(st_files) / max(1, len(symbols)) >= 0.95:
        st_daily = pd.DataFrame(False, index=panels["close"].index, columns=symbols)
        for path in st_files:
            code, market = path.stem.rsplit("_", 1)
            ts_code = f"{code}.{market}"
            if ts_code not in st_daily.columns:
                continue
            s = pd.read_csv(path)
            flags = pd.to_numeric(s.set_index("date")["isST"], errors="coerce").fillna(0)
            flags = flags.reindex(panels["close"].index).ffill().fillna(0)
            st_daily[ts_code] = flags.astype(bool)
        panels["st_daily"] = st_daily
        logger.info("time-varying ST panel loaded: %d symbols, %d ST cells",
                    len(st_files), int(st_daily.to_numpy().sum()))
    else:
        panels["st_daily"] = None
        logger.warning("baostock_st coverage insufficient; fallback to static st_mask")

    panels["index_close"] = index_daily.pivot(index="trade_date", columns="ts_code", values="close")

    adj, source = _build_adj_factor(panels)
    panels["adj_factor"] = adj
    panels["adj_source"] = source

    with open(PANEL_CACHE, "wb") as f:
        pickle.dump(panels, f, protocol=4)
    logger.info("panel cache written: %s (adj_source=%s)", PANEL_CACHE, source)
    return panels


def _build_adj_factor(panels: dict[str, Any]) -> tuple[pd.DataFrame, str]:
    """构建复权因子面板。

    优先 BaoStock 后复权数据（factor = hfq_close / raw_close）；
    覆盖不足时退回除权日中性化：把超出板块涨跌停物理限制的收益整体视为除权，
    该日真实收益按 0 处理（现金分红不可检测，不做补偿）。
    """
    close = panels["close"]
    hfq_files = list(BAOSTOCK_HFQ_DIR.glob("*.csv")) if BAOSTOCK_HFQ_DIR.exists() else []
    coverage = len(hfq_files) / max(1, close.shape[1])
    if coverage >= 0.95:
        logger.info("using baostock hfq factors (coverage=%.1f%%)", coverage * 100)
        factor = pd.DataFrame(index=close.index, columns=close.columns, dtype="float64")
        for path in hfq_files:
            code, market = path.stem.rsplit("_", 1)
            ts_code = f"{code}.{market}"
            if ts_code not in close.columns:
                continue
            df = pd.read_csv(path)
            series = df.set_index("date")["hfq_close"].astype(float)
            series = series.reindex(close.index)
            raw = close[ts_code].astype(float)
            f = series / raw
            factor[ts_code] = f
        factor = factor.ffill().bfill()
        missing = factor.isna().all()
        if missing.any():
            logger.warning("hfq factor missing for %d symbols; fallback to 1.0", int(missing.sum()))
            factor.loc[:, missing] = 1.0
        return factor.astype("float64"), "baostock"

    logger.warning("baostock hfq coverage %.1f%% < 95%%; using ex-date neutralization", coverage * 100)
    raw_ret = close / close.shift(1) - 1
    limit = panels["limit_pct"]
    breach = raw_ret.abs().gt(limit + LIMIT_TOL, axis=1)
    # 除权日：把整段异常收益视为调整，等价于因子乘 prev_close/close
    factor_step = pd.DataFrame(1.0, index=close.index, columns=close.columns)
    prev_close = close.shift(1)
    factor_step[breach] = (prev_close / close)[breach]
    factor = factor_step.cumprod()
    return factor.astype("float64"), "neutralized"


# ---------------------------------------------------------------------------
# 派生面板与 universe
# ---------------------------------------------------------------------------


@dataclass
class Market:
    """回测所需的全部派生数据。"""

    dates: pd.Index
    symbols: pd.Index
    open_raw: np.ndarray
    close_raw: np.ndarray
    prev_close_raw: np.ndarray
    adj_open: np.ndarray
    adj_close: np.ndarray
    adj_ret: np.ndarray            # 复权日收益（close-to-close）
    vol: np.ndarray
    amount: np.ndarray
    turnover: np.ndarray
    limit_pct: np.ndarray          # [symbol]
    universe: np.ndarray           # [date x symbol] bool，t 日收盘时点可入候选
    entry_blocked: np.ndarray      # [date x symbol] bool，该日开盘无法买入
    exit_blocked: np.ndarray       # [date x symbol] bool，该日开盘无法卖出
    corp_action: np.ndarray        # [date x symbol] bool，除权检测日
    index_close: pd.DataFrame
    adj_source: str
    panels: dict[str, Any] = field(repr=False, default_factory=dict)

    def date_slice(self, start: str, end: str) -> np.ndarray:
        """返回 [start, end] 闭区间的日期行索引。"""
        idx = np.where((self.dates >= start) & (self.dates <= end))[0]
        return idx


def build_market(panels: dict[str, Any]) -> Market:
    """由面板构建 Market（全部执行约束在此显式生成）。"""
    close = panels["close"].astype("float64")
    open_ = panels["open"].astype("float64")
    vol = panels["vol"]
    amount = panels["amount"]
    factor = panels["adj_factor"]
    limit = panels["limit_pct"]

    prev_close = close.shift(1)
    adj_close = close * factor
    adj_open = open_ * factor
    adj_ret = adj_close / adj_close.shift(1) - 1

    raw_ret = close / prev_close - 1
    corp_action = raw_ret.abs().gt(limit + LIMIT_TOL, axis=1)

    suspended = vol.isna() | (vol <= 0) | close.isna() | open_.isna()

    # 开盘涨停无法买入 / 开盘跌停无法卖出（近似：开盘价进入距离限价 1% 以内）
    open_gap = open_ / prev_close - 1
    near_limit_up = open_gap.ge(limit.astype("float64") - ENTRY_LIMIT_BUFFER, axis=1)
    near_limit_down = open_gap.le(-(limit.astype("float64") - ENTRY_LIMIT_BUFFER), axis=1)
    entry_blocked = (suspended | near_limit_up | corp_action).fillna(True)
    exit_blocked = (suspended | near_limit_down).fillna(True)

    # universe：t 日收盘视角；ST 剔除优先用时变面板（AGENTS.md 硬规则）
    history_ok = close.notna().cumsum() >= MIN_HISTORY_DAYS
    price_ok = close > MIN_PRICE
    amount_ok = amount >= MIN_AMOUNT
    base = history_ok & price_ok & amount_ok & (~suspended)
    if panels.get("st_daily") is not None:
        universe = (base & (~panels["st_daily"])).fillna(False)
    else:
        universe = base.mul(~panels["st_mask"], axis=1).fillna(False)

    return Market(
        dates=close.index,
        symbols=close.columns,
        open_raw=open_.to_numpy(),
        close_raw=close.to_numpy(),
        prev_close_raw=prev_close.to_numpy(),
        adj_open=adj_open.to_numpy(),
        adj_close=adj_close.to_numpy(),
        adj_ret=adj_ret.to_numpy(),
        vol=vol.to_numpy(dtype="float64"),
        amount=amount.to_numpy(dtype="float64"),
        turnover=panels["turnover"].to_numpy(dtype="float64"),
        limit_pct=limit.to_numpy(dtype="float64"),
        universe=universe.to_numpy(dtype=bool),
        entry_blocked=entry_blocked.to_numpy(dtype=bool),
        exit_blocked=exit_blocked.to_numpy(dtype=bool),
        corp_action=corp_action.to_numpy(dtype=bool),
        index_close=panels["index_close"],
        adj_source=panels["adj_source"],
        panels=panels,
    )


# ---------------------------------------------------------------------------
# 回测核心：滚动分批持仓（tranche 模型）
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    """单笔模拟交易（仅个人模拟复盘用）。"""

    symbol: str
    signal_date: str
    entry_date: str
    exit_date: str
    entry_px: float
    exit_px: float
    net_return: float
    deferred_exit_days: int
    corp_action_days: int


@dataclass
class BacktestResult:
    """回测结果汇总。"""

    name: str
    daily_equity: pd.Series
    trades: list[Trade]
    stats: dict[str, Any]

    def to_row(self) -> dict[str, Any]:
        return {"name": self.name, **self.stats}


def run_strategy(
    market: Market,
    signal: np.ndarray,
    *,
    name: str,
    start: str,
    end: str,
    top_k: int = 10,
    hold_days: int = 10,
    n_tranches: Optional[int] = None,
    entry_stride: int = 1,
    cost_bps: float = DEFAULT_COST_BPS_ROUNDTRIP,
    regime_on: Optional[np.ndarray] = None,
    min_candidates: int = 1,
) -> BacktestResult:
    """分批持仓组合回测，支持三种模式：

    - 日滚动分批：n_tranches=hold_days, entry_stride=1（默认）
    - 低频批次轮动：如 hold_days=60, n_tranches=3, entry_stride=20
    - 事件驱动单批：n_tranches=1, entry_stride=1（空仓时任一信号日全仓进）

    Args:
        signal: [date x symbol]，行 t 只含 t 日收盘及之前信息；值越大越优先，
            NaN 表示该股当日无信号（事件型策略在非事件日整行 NaN）。
        regime_on: [date] bool，t 日收盘时点的环境开关；关闭时不开新仓。

    执行细节：
        信号日 t → t+1 开盘买入（entry_blocked 剔除）→ t+1+hold_days 开盘卖出，
        卖出日 exit_blocked 时顺延（顺延期间该批资金不再当日复用）；
        成本按双边合计 cost_bps 计入每笔收益。
    """
    if n_tranches is None:
        n_tranches = max(1, hold_days // entry_stride)
    dates = market.dates
    idx_range = market.date_slice(start, end)
    n_dates = len(dates)
    tranche_equity = np.full(n_tranches, 1.0 / n_tranches)
    equity_curve: dict[str, float] = {}
    trades: list[Trade] = []
    blocked_entries = 0
    no_candidate_days = 0

    # 各批次当前持仓：list[(sym_idx, entry_px, entry_i)]；None 表示持币
    open_positions: list[Optional[list[tuple[int, float, int]]]] = [None] * n_tranches
    tranche_exit_at: list[Optional[int]] = [None] * n_tranches
    tranche_locked_until: list[int] = [0] * n_tranches  # 顺延卖出后资金锁定期
    exposure_acc: list[float] = []

    step = 0
    for t in idx_range:
        entry_i = t + 1
        if entry_i >= n_dates:
            break

        # 1) 所有到期批次平仓（在 entry_i 开盘）
        for j in range(n_tranches):
            if open_positions[j] is None or tranche_exit_at[j] is None or entry_i < tranche_exit_at[j]:
                continue
            batch = open_positions[j]
            rets = []
            max_exit = entry_i
            for sym_idx, entry_px, ei in batch:
                real_exit = entry_i
                deferred = 0
                while real_exit < n_dates - 1 and market.exit_blocked[real_exit, sym_idx]:
                    real_exit += 1
                    deferred += 1
                max_exit = max(max_exit, real_exit)
                exit_px = market.adj_open[real_exit, sym_idx]
                if not np.isfinite(exit_px):
                    exit_px = market.adj_close[real_exit, sym_idx]
                if not np.isfinite(exit_px) or not np.isfinite(entry_px) or entry_px <= 0:
                    continue
                gross = exit_px / entry_px - 1
                net = gross - cost_bps / 10000.0
                ca_days = int(market.corp_action[ei:real_exit + 1, sym_idx].sum())
                rets.append(net)
                trades.append(Trade(
                    symbol=str(market.symbols[sym_idx]),
                    signal_date=str(dates[ei - 1]),
                    entry_date=str(dates[ei]),
                    exit_date=str(dates[real_exit]),
                    entry_px=float(entry_px),
                    exit_px=float(exit_px),
                    net_return=float(net),
                    deferred_exit_days=deferred,
                    corp_action_days=ca_days,
                ))
            if rets:
                tranche_equity[j] *= 1.0 + float(np.mean(rets))
            open_positions[j] = None
            tranche_exit_at[j] = None
            if max_exit > entry_i:
                tranche_locked_until[j] = max_exit + 1  # 资金实际晚回笼

        # 2) 开新仓：仅在 stride 对齐日；事件型（stride=1）任意空仓日均可
        if step % entry_stride == 0 or entry_stride == 1:
            free = [j for j in range(n_tranches) if open_positions[j] is None and tranche_locked_until[j] <= entry_i]
            if free:
                j = free[0]
                if regime_on is not None and not bool(regime_on[t]):
                    pass  # 环境关闭：持币
                else:
                    eligible = market.universe[t] & (~market.entry_blocked[entry_i]) & np.isfinite(signal[t])
                    n_eligible = int(eligible.sum())
                    if n_eligible >= min_candidates:
                        sig_row = np.where(eligible, signal[t], -np.inf)
                        k = min(top_k, n_eligible)
                        picks = np.argpartition(-sig_row, k - 1)[:k]
                        picks = picks[np.isfinite(sig_row[picks])]
                        batch = []
                        for sym_idx in picks:
                            entry_px = market.adj_open[entry_i, sym_idx]
                            if not np.isfinite(entry_px) or entry_px <= 0:
                                blocked_entries += 1
                                continue
                            batch.append((int(sym_idx), float(entry_px), int(entry_i)))
                        if batch:
                            open_positions[j] = batch
                            tranche_exit_at[j] = entry_i + hold_days
                    else:
                        no_candidate_days += 1

        # 3) 逐日盯市（用复权收盘估值）
        total = 0.0
        invested = 0.0
        for j in range(n_tranches):
            batch = open_positions[j]
            if batch is None:
                total += tranche_equity[j]
            else:
                vals = []
                for sym_idx, entry_px, ei in batch:
                    mark = market.adj_close[t, sym_idx]
                    if not np.isfinite(mark) and ei <= t:
                        mark = entry_px
                    vals.append(mark / entry_px if ei <= t else 1.0)
                part = tranche_equity[j] * float(np.mean(vals)) if vals else tranche_equity[j]
                total += part
                invested += part
        equity_curve[str(dates[t])] = total
        exposure_acc.append(invested / total if total > 0 else 0.0)
        step += 1

    # 收盘所有未平仓头寸（按期末复权收盘估值，含成本）
    last_i = idx_range[-1] if len(idx_range) else n_dates - 1
    for j in range(n_tranches):
        batch = open_positions[j]
        if batch is None:
            continue
        rets = []
        for sym_idx, entry_px, ei in batch:
            mark = market.adj_close[last_i, sym_idx]
            if np.isfinite(mark) and entry_px > 0:
                rets.append(mark / entry_px - 1 - cost_bps / 10000.0)
        if rets:
            tranche_equity[j] *= 1.0 + float(np.mean(rets))

    equity = pd.Series(equity_curve, dtype="float64")
    stats = _compute_stats(equity, trades, blocked_entries, no_candidate_days)
    stats["avg_exposure"] = round(float(np.mean(exposure_acc)), 4) if exposure_acc else 0.0
    return BacktestResult(name=name, daily_equity=equity, trades=trades, stats=stats)


def _compute_stats(equity: pd.Series, trades: list[Trade], blocked_entries: int, no_candidate_days: int) -> dict[str, Any]:
    """从日净值与交易列表计算汇总指标。"""
    if equity.empty:
        return {"cum_return": None, "note": "empty equity"}
    daily_ret = equity.pct_change().dropna()
    cum = float(equity.iloc[-1] / equity.iloc[0] - 1)
    n_days = len(equity)
    ann = float((1 + cum) ** (244.0 / max(n_days, 1)) - 1)
    vol = float(daily_ret.std() * np.sqrt(244)) if len(daily_ret) > 2 else float("nan")
    sharpe = float(ann / vol) if vol and np.isfinite(vol) and vol > 0 else float("nan")
    peak = equity.cummax()
    mdd = float((equity / peak - 1).min())
    rets = np.array([tr.net_return for tr in trades]) if trades else np.array([])
    contaminated = sum(1 for tr in trades if tr.corp_action_days > 0)
    return {
        "cum_return": round(cum, 4),
        "ann_return": round(ann, 4),
        "ann_vol": round(vol, 4) if np.isfinite(vol) else None,
        "sharpe": round(sharpe, 3) if np.isfinite(sharpe) else None,
        "max_drawdown": round(mdd, 4),
        "calmar": round(ann / abs(mdd), 3) if mdd < 0 else None,
        "n_trades": len(trades),
        "win_rate": round(float((rets > 0).mean()), 4) if len(rets) else None,
        "avg_trade": round(float(rets.mean()), 5) if len(rets) else None,
        "median_trade": round(float(np.median(rets)), 5) if len(rets) else None,
        "deferred_exits": sum(1 for tr in trades if tr.deferred_exit_days > 0),
        "corp_action_trades": contaminated,
        "blocked_entries": blocked_entries,
        "no_candidate_days": no_candidate_days,
        "n_days": n_days,
    }


# ---------------------------------------------------------------------------
# 信号与环境库（供 runner 组装；行 t 只用 t 及之前数据）
# ---------------------------------------------------------------------------


def _rolling(arr: np.ndarray, window: int, fn: str) -> np.ndarray:
    """基于 pandas 的滚动统计（保持 NaN 语义）。"""
    df = pd.DataFrame(arr)
    if fn == "mean":
        out = df.rolling(window, min_periods=window).mean()
    elif fn == "std":
        out = df.rolling(window, min_periods=window).std()
    elif fn == "max":
        out = df.rolling(window, min_periods=window).max()
    elif fn == "min":
        out = df.rolling(window, min_periods=window).min()
    elif fn == "sum":
        out = df.rolling(window, min_periods=window).sum()
    else:
        raise ValueError(fn)
    return out.to_numpy()


def sig_reversal(market: Market, n: int = 10) -> np.ndarray:
    """短期反转：过去 n 日复权收益越低越优先。"""
    adj_close = market.adj_close
    ret_n = adj_close / np.vstack([np.full((n, adj_close.shape[1]), np.nan), adj_close[:-n]]) - 1
    return -ret_n


def sig_reversal_vs_index(market: Market, n: int = 10, index_code: str = "000905.SH") -> np.ndarray:
    """相对指数的超额反转：剔除市场整体下跌的成分。"""
    raw = -sig_reversal(market, n)  # 个股 n 日收益
    idx = market.index_close[index_code].reindex(market.dates).astype(float).to_numpy()
    idx_ret = idx / np.concatenate([np.full(n, np.nan), idx[:-n]]) - 1
    excess = raw - idx_ret[:, None]
    return -excess


def sig_low_turnover(market: Market, n: int = 20) -> np.ndarray:
    """低换手：过去 n 日平均换手率越低越优先。"""
    return -_rolling(market.turnover, n, "mean")


def sig_abnormal_turnover(market: Market, short: int = 5, long: int = 60) -> np.ndarray:
    """异常换手（短/长换手比）越低越优先。"""
    s = _rolling(market.turnover, short, "mean")
    l = _rolling(market.turnover, long, "mean")
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(l > 0, s / l, np.nan)
    return -ratio


def sig_low_vol(market: Market, n: int = 20) -> np.ndarray:
    """低波动：过去 n 日复权收益标准差越低越优先。"""
    return -_rolling(market.adj_ret, n, "std")


def sig_low_max_ret(market: Market, n: int = 20) -> np.ndarray:
    """MAX 效应（彩票偏好反向）：过去 n 日最大单日收益越低越优先。"""
    return -_rolling(market.adj_ret, n, "max")


def sig_small_amount(market: Market, n: int = 20) -> np.ndarray:
    """小额成交（规模/流动性溢价近似）：过去 n 日均成交额越低越优先。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        return -np.log(_rolling(market.amount, n, "mean"))


def sig_drawdown_pullback(market: Market, high_n: int = 60, trend_n: int = 120) -> np.ndarray:
    """长趋势内回撤买入：处于长期趋势之上但短期深度回撤的股票优先。"""
    adj_close = market.adj_close
    roll_max = _rolling(adj_close, high_n, "max")
    drawdown = adj_close / roll_max - 1          # 越负回撤越深
    trend_ma = _rolling(adj_close, trend_n, "mean")
    above_trend = adj_close > trend_ma
    score = -drawdown                             # 回撤深 → 分高
    return np.where(above_trend, score, np.nan)


def sig_gap_down_reversal(market: Market, n: int = 3) -> np.ndarray:
    """连续放量下跌后的开盘承接：过去 n 日累计收益低且当日收在开盘之上。"""
    rev = sig_reversal(market, n)
    close_above_open = market.close_raw > market.open_raw
    return np.where(close_above_open, rev, np.nan)


def rank_composite(signals: list[np.ndarray], weights: Optional[list[float]] = None) -> np.ndarray:
    """横截面分位数合成：逐日把每个信号转为 [0,1] 分位，再加权平均。"""
    if weights is None:
        weights = [1.0] * len(signals)
    total = None
    wsum = 0.0
    for sig, w in zip(signals, weights):
        ranked = pd.DataFrame(sig).rank(axis=1, pct=True).to_numpy()
        total = ranked * w if total is None else total + ranked * w
        wsum += w
    return total / wsum


# --- 市场环境（regime）信号：返回 [date] bool，行 t 只用 t 及之前数据 ---


def regime_index_above_ma(market: Market, index_code: str = "000905.SH", n: int = 120) -> np.ndarray:
    """指数收盘在 n 日均线之上。"""
    idx = market.index_close[index_code].reindex(market.dates).astype(float)
    return (idx > idx.rolling(n, min_periods=n).mean()).fillna(False).to_numpy()


def regime_index_dd_ok(market: Market, index_code: str = "000905.SH", n: int = 60, max_dd: float = 0.10) -> np.ndarray:
    """指数距 n 日高点回撤不超过 max_dd。"""
    idx = market.index_close[index_code].reindex(market.dates).astype(float)
    dd = idx / idx.rolling(n, min_periods=n).max() - 1
    return (dd > -max_dd).fillna(False).to_numpy()


def regime_breadth(market: Market, ma_n: int = 20, min_frac: float = 0.45) -> np.ndarray:
    """市场宽度：universe 内收盘站上 MA{ma_n} 的比例不低于 min_frac。"""
    adj_close = market.adj_close
    ma = _rolling(adj_close, ma_n, "mean")
    above = (adj_close > ma) & market.universe
    denom = market.universe.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.where(denom > 0, above.sum(axis=1) / denom, 0.0)
    return frac >= min_frac


def regime_and(*regimes: np.ndarray) -> np.ndarray:
    """多环境条件与。"""
    out = regimes[0].copy()
    for r in regimes[1:]:
        out &= r
    return out


# ---------------------------------------------------------------------------
# 基线
# ---------------------------------------------------------------------------


def sig_random(market: Market, seed: int) -> np.ndarray:
    """随机信号基线（同一执行框架下测量 universe+择时的 beta）。"""
    rng = np.random.default_rng(seed)
    return rng.random(size=market.adj_close.shape)


def sig_equal_weight_all(market: Market) -> np.ndarray:
    """等权持有基线：所有 universe 内股票同分（top_k 需设为极大值）。"""
    return np.zeros_like(market.adj_close)
