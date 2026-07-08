"""
strategy_search_202607.py - 2026-07 策略搜索 runner

12 个候选策略（4 个机制族）+ 基线对照，统一走 panel_backtester 执行协议。
纪律约束：
  - 训练集 2021-01-04~2023-12-29；验证集 2024-01-02~2025-06-30；
    holdout 2025-07-01~2026-06-26 仅在 --unlock-holdout 时可运行。
  - 每次运行写入 trial_registry.jsonl（多重检验披露：所有试过的配置都留痕）。
  - 参数使用提案默认值，不做网格搜索；如需调参必须新增命名配置并留痕。

本模块只读本地数据，不修改主策略、不写台账、不连接实盘。
候选产出仅为 shadow research，不构成任何收益承诺。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel_backtester as pb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SPLITS = {
    "train": ("2021-01-04", "2023-12-29"),
    "validation": ("2024-01-02", "2025-06-30"),
    "holdout": ("2025-07-01", "2026-06-26"),
    # 加长复检段（需 PANEL_CACHE=panel_cache_v2.pkl）：候选规则冻结时从未见过
    # 这段数据 → 对冻结候选构成真样本外。注意 2016-2020 幸存者偏差更重、
    # 换手率特征缺失（依赖 turnover 的信号不可测）。
    "extended2016": ("2016-07-08", "2020-12-31"),
}
DEFAULT_OUTPUT = pb.ROOT_DIR / "output" / "strategy_search_202607"


# ---------------------------------------------------------------------------
# 派生特征（带缓存；所有行 t 只用 t 及之前数据）
# ---------------------------------------------------------------------------


class Features:
    """惰性特征面板缓存。"""

    def __init__(self, market: pb.Market) -> None:
        self.m = market
        self._cache: dict[str, np.ndarray] = {}

    def get(self, key: str) -> np.ndarray:
        if key not in self._cache:
            self._cache[key] = self._build(key)
        return self._cache[key]

    def _roll(self, arr: np.ndarray, window: int, fn: str) -> np.ndarray:
        return pb._rolling(arr, window, fn)

    def _build(self, key: str) -> np.ndarray:
        m = self.m
        if key.startswith("ret"):                      # retN：N 日复权收益
            n = int(key[3:])
            ac = m.adj_close
            shifted = np.vstack([np.full((n, ac.shape[1]), np.nan), ac[:-n]])
            return ac / shifted - 1
        if key.startswith("vol"):                      # volN：N 日收益标准差
            n = int(key[3:])
            return self._roll(m.adj_ret, n, "std")
        if key.startswith("turn_mean"):                # turn_meanN
            n = int(key[9:])
            return self._roll(m.turnover, n, "mean")
        if key.startswith("adv"):                      # advN：N 日均成交额
            n = int(key[3:])
            return self._roll(m.amount, n, "mean")
        if key == "max1_20":                           # 20 日最大单日收益
            return self._roll(m.adj_ret, 20, "max")
        if key == "prev_close_ratio":                  # 当日 raw 收益（用于涨跌停判定）
            return m.close_raw / m.prev_close_raw - 1
        if key == "limit_down_touch":                  # 当日最低触及跌停（近似）
            low_ret = m.panels["low"].to_numpy(dtype="float64") / m.prev_close_raw - 1
            lim = m.limit_pct[None, :]
            return ((low_ret <= -(lim - 0.005)) & ~m.corp_action).astype("float64")
        if key == "limit_up_close":                    # 收盘涨停（近似）
            r = self.get("prev_close_ratio")
            lim = m.limit_pct[None, :]
            return ((r >= lim - 0.005) & ~m.corp_action).astype("float64")
        if key == "limit_down_close":                  # 收盘跌停（近似）
            r = self.get("prev_close_ratio")
            lim = m.limit_pct[None, :]
            return ((r <= -(lim - 0.005)) & ~m.corp_action).astype("float64")
        if key == "on_ret":                            # 隔夜对数收益（除权日置 0）
            with np.errstate(divide="ignore", invalid="ignore"):
                on = np.log(m.open_raw / m.prev_close_raw)
            on = np.where(m.corp_action, 0.0, on)
            return on
        if key == "on20":                              # 20 日累计隔夜收益
            return self._roll(self.get("on_ret"), 20, "sum")
        if key == "id20":                              # 20 日累计日内收益
            with np.errstate(divide="ignore", invalid="ignore"):
                intraday = np.log(m.close_raw / m.open_raw)
            return self._roll(intraday, 20, "sum")
        if key == "fmc":                               # 流通市值代理 = adv20 / (turn20/100)
            adv = self.get("adv20")
            turn = self.get("turn_mean20")
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.where(turn > 0, adv / (turn / 100.0), np.nan)
        if key == "low_min20":                         # 20 日最低价
            return self._roll(m.panels["low"].to_numpy(dtype="float64"), 20, "min")
        if key == "close_min20":
            return self._roll(m.adj_close, 20, "min")
        if key == "close_min3":
            return self._roll(m.adj_close, 3, "min")
        if key == "breadth":                           # universe 内站上 MA20 比例（[date]）
            ma20 = self._roll(m.adj_close, 20, "mean")
            above = (m.adj_close > ma20) & m.universe
            denom = m.universe.sum(axis=1).astype(float)
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.where(denom > 0, above.sum(axis=1) / denom, np.nan)
        raise KeyError(key)

    # --- 指数序列 ---

    def index_close(self, code: str) -> np.ndarray:
        s = self.m.index_close[code].reindex(self.m.dates).astype(float)
        return s.to_numpy()

    def index_ret(self, code: str, n: int) -> np.ndarray:
        c = self.index_close(code)
        return c / np.concatenate([np.full(n, np.nan), c[:-n]]) - 1


def rank_pct(arr: np.ndarray) -> np.ndarray:
    """逐日横截面分位（0~1，升序：值小 → 分位低）。"""
    return pd.DataFrame(arr).rank(axis=1, pct=True).to_numpy()


def zscore_cs(arr: np.ndarray, clip: float = 3.0) -> np.ndarray:
    """逐日横截面 z 分（±clip 截尾）。"""
    df = pd.DataFrame(arr)
    z = df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1).replace(0, np.nan), axis=0)
    return z.clip(-clip, clip).to_numpy()


def _event_series_with_cooldown(trigger: np.ndarray, cooldown: int) -> np.ndarray:
    """事件触发序列加冷却期（触发后 cooldown 个交易日内不再触发）。"""
    out = np.zeros_like(trigger, dtype=bool)
    last = -10**9
    for i, flag in enumerate(trigger):
        if flag and i - last > cooldown:
            out[i] = True
            last = i
    return out


# ---------------------------------------------------------------------------
# 策略定义
# ---------------------------------------------------------------------------


def _base_quality_mask(f: Features) -> np.ndarray:
    """通用附加过滤（提案共用）：剔除流通市值代理最低 30% 分位。"""
    fmc_rank = rank_pct(f.get("fmc"))
    return fmc_rank > 0.30


def strat_rev20_h10(f: Features) -> tuple[np.ndarray, dict]:
    """R20 超跌反转：近 20 日跌幅最深，排除接飞刀与跌停链。"""
    ret20 = f.get("ret20")
    ret5 = f.get("ret5")
    ld3 = pb._rolling(f.get("limit_down_close"), 3, "sum")
    ok = _base_quality_mask(f) & (ret5 > -0.20) & (ld3 < 1)
    sig = np.where(ok, -ret20, np.nan)
    return sig, {"top_k": 10, "hold_days": 10}


def strat_rev20_stab_h10(f: Features) -> tuple[np.ndarray, dict]:
    """20 日超跌 + 缩量企稳：换手萎缩且近 3 日未创 20 日新低。"""
    ret20 = f.get("ret20")
    abturn = f.get("turn_mean5") / f.get("turn_mean60")
    stab = f.get("close_min3") > f.get("close_min20")
    ok = _base_quality_mask(f) & (abturn < 0.7) & stab & (ret20 > -0.35)
    sig = np.where(ok, -ret20, np.nan)
    return sig, {"top_k": 10, "hold_days": 10}


def strat_rev20_stab_gated(f: Features) -> tuple[np.ndarray, dict]:
    """同上 + 组合层风控：中证1000 近 20 日跌超 10% 时暂停新开仓。"""
    sig, kw = strat_rev20_stab_h10(f)
    idx_ret20 = f.index_ret("000852.SH", 20)
    regime = ~(idx_ret20 < -0.10)
    regime = np.where(np.isnan(idx_ret20), True, regime)
    kw["regime_on"] = regime
    return sig, kw


def strat_lowturn_h20(f: Features) -> tuple[np.ndarray, dict]:
    """冷门低换手轮动：0.6×低换手 + 0.4×低波动，月度批次。"""
    score = 0.6 * rank_pct(f.get("turn_mean60")) + 0.4 * rank_pct(f.get("vol120"))
    adv60_rank = rank_pct(f.get("adv60"))
    ok = adv60_rank > 0.50
    sig = np.where(ok, -score, np.nan)
    return sig, {"top_k": 10, "hold_days": 20, "n_tranches": 1, "entry_stride": 20}


def strat_abturn_h20(f: Features) -> tuple[np.ndarray, dict]:
    """异常缩量溢价：10/120 日换手比最低 + 偏低波。"""
    abturn = f.get("turn_mean10") / f.get("turn_mean120")
    score = rank_pct(abturn) + 0.5 * rank_pct(f.get("vol20"))
    ok = _base_quality_mask(f) & (abturn > 0.15) & (f.get("ret120") > -0.35)
    sig = np.where(ok, -score, np.nan)
    return sig, {"top_k": 10, "hold_days": 20, "n_tranches": 10, "entry_stride": 2}


def strat_lowvol_h20(f: Features) -> tuple[np.ndarray, dict]:
    """低波动 + 反 MAX 防御组合。"""
    score = rank_pct(f.get("vol60")) + rank_pct(f.get("max1_20"))
    lim20 = pb._rolling(f.get("limit_up_close") + f.get("limit_down_close"), 20, "sum")
    ok = _base_quality_mask(f) & (f.get("ret120") > -0.30) & (lim20 < 1)
    sig = np.where(ok, -score, np.nan)
    return sig, {"top_k": 10, "hold_days": 20, "n_tranches": 1, "entry_stride": 20}


def strat_composite3_h15(f: Features) -> tuple[np.ndarray, dict]:
    """三因子复合：反转 + 冷门 + 反彩票，等权 z 分。"""
    abturn = f.get("turn_mean10") / f.get("turn_mean120")
    z1 = zscore_cs(-f.get("ret20"))
    z2 = zscore_cs(-abturn)
    z3 = zscore_cs(-f.get("max1_20"))
    ld3 = pb._rolling(f.get("limit_down_close"), 3, "sum")
    ok = _base_quality_mask(f) & (f.get("ret5") > -0.20) & (ld3 < 1)
    sig = np.where(ok, (z1 + z2 + z3) / 3.0, np.nan)
    return sig, {"top_k": 10, "hold_days": 15, "n_tranches": 5, "entry_stride": 3}


def strat_longloser_h60(f: Features) -> tuple[np.ndarray, dict]:
    """长周期输家修复：240 日输家 + 近 60 日企稳 + 关注度退潮 + 波动收敛。"""
    ret240 = f.get("ret240")
    loser = rank_pct(ret240) < 0.30
    stab = f.get("ret60") > -0.15
    attn = (f.get("turn_mean20") / f.get("turn_mean240")) < 0.7
    vol_conv = f.get("vol20") < f.get("vol120")
    adv_ok = rank_pct(f.get("adv60")) > 0.50
    ok = loser & stab & attn & vol_conv & adv_ok
    sig = np.where(ok, -ret240, np.nan)
    return sig, {"top_k": 10, "hold_days": 60, "n_tranches": 3, "entry_stride": 20}


def strat_overnight_rev_h10(f: Features) -> tuple[np.ndarray, dict]:
    """隔夜折价反转：20 日累计隔夜收益最低 + 日内被承接。"""
    on20 = f.get("on20")
    id20 = f.get("id20")
    lu20 = pb._rolling(f.get("limit_up_close"), 20, "sum")
    ld_today = f.get("limit_down_close") > 0
    ok = _base_quality_mask(f) & (id20 >= 0) & (lu20 <= 2) & (~ld_today)
    sig = np.where(ok, -on20, np.nan)
    return sig, {"top_k": 10, "hold_days": 10}


def strat_panic_liquidity_h20(f: Features) -> tuple[np.ndarray, dict]:
    """恐慌日大盘股流动性提供：沪深300 急跌触发，买波动标准化超跌最深的大盘股。"""
    r3 = f.index_ret("000300.SH", 3)
    r1 = f.index_ret("000300.SH", 1)
    trigger = _event_series_with_cooldown((r3 <= -0.045) | (r1 <= -0.03), cooldown=10)
    adv240_rank_desc = pd.DataFrame(f.get("adv240")).rank(axis=1, ascending=False).to_numpy()
    big = adv240_rank_desc <= 300
    ret3 = f.get("ret3")
    vol60 = f.get("vol60")
    with np.errstate(divide="ignore", invalid="ignore"):
        panic_z = ret3 / (vol60 * np.sqrt(3))
    sig = np.where(big & trigger[:, None], -panic_z, np.nan)
    return sig, {"top_k": 10, "hold_days": 20, "n_tranches": 1, "entry_stride": 1}


def strat_idx1000_bounce_h5(f: Features) -> tuple[np.ndarray, dict]:
    """中证1000 超跌首阳反弹：急跌后首个反弹日买中盘流动性篮。"""
    c = f.index_close("000852.SH")
    r5 = f.index_ret("000852.SH", 5)
    r1 = f.index_ret("000852.SH", 1)
    r60 = f.index_ret("000852.SH", 60)
    raw_trigger = (r5 <= -0.05) & (r1 > 0) & (r60 > -0.25)
    trigger = _event_series_with_cooldown(np.nan_to_num(raw_trigger, nan=False).astype(bool), cooldown=5)
    adv_rank_desc = pd.DataFrame(f.get("adv20")).rank(axis=1, ascending=False).to_numpy()
    mid = (adv_rank_desc > 300) & (adv_rank_desc <= 1500)
    sig = np.where(mid & trigger[:, None], f.get("adv20"), np.nan)
    return sig, {"top_k": 10, "hold_days": 5, "n_tranches": 1, "entry_stride": 1}


def strat_limitdown_recover_h2(f: Features) -> tuple[np.ndarray, dict]:
    """跌停开板收复：首次触跌停但收盘收复 3% 以上，赚处置效应反弹。"""
    m = f.m
    touch = f.get("limit_down_touch") > 0
    touch_prev = np.vstack([np.zeros((1, touch.shape[1]), dtype=bool), touch[:-1]])
    lim = m.limit_pct[None, :]
    ld_price_ratio = m.close_raw / (m.prev_close_raw * (1 - lim))
    recovered = ld_price_ratio >= 1.03
    ok = touch & (~touch_prev) & recovered & (f.get("ret20") > -0.30) & _base_quality_mask(f)
    # 跌停潮熔断：全市场收盘跌停家数 > 60 当日作废
    ld_count = f.get("limit_down_close").sum(axis=1)
    ok = ok & (ld_count[:, None] <= 60)
    low = m.panels["low"].to_numpy(dtype="float64")
    strength = (m.close_raw - low) / np.where(low > 0, low, np.nan)
    sig = np.where(ok, strength, np.nan)
    return sig, {"top_k": 10, "hold_days": 2, "n_tranches": 1, "entry_stride": 1}


def strat_hammer_h5(f: Features) -> tuple[np.ndarray, dict]:
    """阴跌尾声放量长下影：卖方衰竭确认后的短反弹。"""
    m = f.m
    low = m.panels["low"].to_numpy(dtype="float64")
    high = m.panels["high"].to_numpy(dtype="float64")
    new_low = low <= f.get("low_min20")
    vol_mult = m.amount / f.get("adv20")
    with np.errstate(divide="ignore", invalid="ignore"):
        close_pos = (m.close_raw - low) / np.where(high > low, high - low, np.nan)
    ret10 = f.get("ret10")
    lu20 = pb._rolling(f.get("limit_up_close"), 20, "sum")
    ld_today = f.get("limit_down_close") > 0
    ok = (new_low & (vol_mult >= 2.0) & (close_pos >= 0.6) & (ret10 <= -0.08)
          & (lu20 < 1) & (~ld_today) & (~m.corp_action) & _base_quality_mask(f))
    sig = np.where(ok, vol_mult, np.nan)
    return sig, {"top_k": 10, "hold_days": 5, "n_tranches": 1, "entry_stride": 1}


def _liquidity_basket_signal(f: Features) -> np.ndarray:
    """流动性篮：adv20 最大者优先（择时类策略的持仓标的）。"""
    return f.get("adv20")


def _faber_regime(f: Features) -> np.ndarray:
    """慢趋势滞回带：HS300 > 1.01×MA200 连续3日进，< 0.99×MA200 连续3日出。"""
    c = f.index_close("000300.SH")
    ma200 = pd.Series(c).rolling(200, min_periods=200).mean().to_numpy()
    ma200_prev20 = np.concatenate([np.full(20, np.nan), ma200[:-20]])
    logret = np.diff(np.log(np.where(c > 0, c, np.nan)), prepend=np.nan)
    sigma20 = pd.Series(logret).rolling(20, min_periods=20).std().to_numpy() * np.sqrt(244)
    ma50 = pd.Series(c).rolling(50, min_periods=50).mean().to_numpy()

    enter_cond = (c > 1.01 * ma200) & (ma200 > ma200_prev20) & (sigma20 < 0.35)
    exit_cond = (c < 0.99 * ma200) | ((sigma20 > 0.40) & (c < ma50))
    state = np.zeros(len(c), dtype=bool)
    in_mkt = False
    streak_in = 0
    streak_out = 0
    for i in range(len(c)):
        if not np.isfinite(ma200[i]):
            state[i] = False
            continue
        streak_in = streak_in + 1 if enter_cond[i] else 0
        streak_out = streak_out + 1 if exit_cond[i] else 0
        if not in_mkt and streak_in >= 3:
            in_mkt = True
        elif in_mkt and streak_out >= 3:
            in_mkt = False
        state[i] = in_mkt
    return state


def strat_faber_basket(f: Features) -> tuple[np.ndarray, dict]:
    """Faber 式 MA200 滞回择时 + 流动性十股篮（20 日滚动近似持续持有）。"""
    sig = _liquidity_basket_signal(f)
    return np.where(np.isfinite(sig), sig, np.nan), {
        "top_k": 10, "hold_days": 20, "n_tranches": 1, "entry_stride": 1,
        "regime_on": _faber_regime(f),
    }


def _breadth_thrust_regime(f: Features) -> np.ndarray:
    """宽度冲量状态机：B≥0.55 且 10 日内曾 ≤0.35 进；B<0.45 连续 5 日出；冷却 10 日。"""
    b = f.get("breadth")
    c300 = f.index_close("000300.SH")
    logret = np.diff(np.log(np.where(c300 > 0, c300, np.nan)), prepend=np.nan)
    sigma20 = pd.Series(logret).rolling(20, min_periods=20).std().to_numpy() * np.sqrt(244)
    state = np.zeros(len(b), dtype=bool)
    in_mkt = False
    below_streak = 0
    cooldown_until = -1
    for i in range(len(b)):
        if not np.isfinite(b[i]):
            state[i] = False
            continue
        window_lo = np.nanmin(b[max(0, i - 10):max(1, i - 2)]) if i >= 3 else np.nan
        if not in_mkt and i > cooldown_until:
            if b[i] >= 0.55 and np.isfinite(window_lo) and window_lo <= 0.35 and (np.isfinite(sigma20[i]) and sigma20[i] < 0.35):
                in_mkt = True
                below_streak = 0
        elif in_mkt:
            below_streak = below_streak + 1 if b[i] < 0.45 else 0
            if below_streak >= 5:
                in_mkt = False
                cooldown_until = i + 10
        state[i] = in_mkt
    return state


def strat_breadth_thrust(f: Features) -> tuple[np.ndarray, dict]:
    """全市场宽度拐点择时 + 流动性十股篮。"""
    sig = _liquidity_basket_signal(f)
    return np.where(np.isfinite(sig), sig, np.nan), {
        "top_k": 10, "hold_days": 20, "n_tranches": 1, "entry_stride": 1,
        "regime_on": _breadth_thrust_regime(f),
    }


# --- 基线 ---


def baseline_ew500_h20(f: Features) -> tuple[np.ndarray, dict]:
    """等权 500 只基线（universe 内随机代表样本，月度换仓）。"""
    return pb.sig_random(f.m, seed=1), {"top_k": 500, "hold_days": 20, "n_tranches": 1, "entry_stride": 20}


def baseline_ew500_faber(f: Features) -> tuple[np.ndarray, dict]:
    """等权 500 + Faber 择时（归因：择时 beta 单独值多少）。"""
    sig, kw = baseline_ew500_h20(f)
    kw = {**kw, "n_tranches": 1, "entry_stride": 1, "regime_on": _faber_regime(f)}
    return sig, kw


def baseline_ew500_breadth(f: Features) -> tuple[np.ndarray, dict]:
    """等权 500 + 宽度冲量择时。"""
    sig, kw = baseline_ew500_h20(f)
    kw = {**kw, "n_tranches": 1, "entry_stride": 1, "regime_on": _breadth_thrust_regime(f)}
    return sig, kw


def _make_random10(seed: int) -> Callable:
    def _f(f: Features) -> tuple[np.ndarray, dict]:
        return pb.sig_random(f.m, seed=seed), {"top_k": 10, "hold_days": 20}
    _f.__doc__ = f"随机 10 只基线 seed={seed}（选股运气带）。"
    return _f


def _abturn_variant(short: int, long: int, top_k: int = 10, hold: int = 20,
                    cost: float = pb.DEFAULT_COST_BPS_ROUNDTRIP) -> Callable:
    """abturn 稳健性变体工厂（窗口/持仓数/持有期/成本扰动）。"""
    def _f(f: Features) -> tuple[np.ndarray, dict]:
        abturn = f.get(f"turn_mean{short}") / f.get(f"turn_mean{long}")
        score = rank_pct(abturn) + 0.5 * rank_pct(f.get("vol20"))
        ok = _base_quality_mask(f) & (abturn > 0.15) & (f.get("ret120") > -0.35)
        sig = np.where(ok, -score, np.nan)
        stride = max(1, hold // 10)
        return sig, {"top_k": top_k, "hold_days": hold,
                     "n_tranches": max(1, hold // stride), "entry_stride": stride,
                     "cost_bps": cost}
    _f.__doc__ = f"abturn 变体 short={short} long={long} k={top_k} h={hold} cost={cost}"
    return _f


def strat_composite3_cost50(f: Features) -> tuple[np.ndarray, dict]:
    """composite3 成本加压变体（50bp 双边）。"""
    sig, kw = strat_composite3_h15(f)
    kw["cost_bps"] = 50.0
    return sig, kw


ROBUSTNESS_VARIANTS: dict[str, Callable] = {
    "abturn_cost50": _abturn_variant(10, 120, cost=50.0),
    "abturn_w5_60": _abturn_variant(5, 60),
    "abturn_w20_240": _abturn_variant(20, 240),
    "abturn_k20": _abturn_variant(10, 120, top_k=20),
    "abturn_h10": _abturn_variant(10, 120, hold=10),
    "abturn_h30": _abturn_variant(10, 120, hold=30),
    "composite3_cost50": strat_composite3_cost50,
}

STRATEGIES: dict[str, Callable] = {
    "rev20_h10": strat_rev20_h10,
    "rev20_stab_h10": strat_rev20_stab_h10,
    "rev20_stab_gated": strat_rev20_stab_gated,
    "lowturn_h20": strat_lowturn_h20,
    "abturn_h20": strat_abturn_h20,
    "lowvol_h20": strat_lowvol_h20,
    "composite3_h15": strat_composite3_h15,
    "longloser_h60": strat_longloser_h60,
    "overnight_rev_h10": strat_overnight_rev_h10,
    "panic_liquidity_h20": strat_panic_liquidity_h20,
    "idx1000_bounce_h5": strat_idx1000_bounce_h5,
    "limitdown_recover_h2": strat_limitdown_recover_h2,
    "hammer_h5": strat_hammer_h5,
    "faber_basket": strat_faber_basket,
    "breadth_thrust": strat_breadth_thrust,
    "bl_ew500_h20": baseline_ew500_h20,
    "bl_ew500_faber": baseline_ew500_faber,
    "bl_ew500_breadth": baseline_ew500_breadth,
}
for _seed in range(5):
    STRATEGIES[f"bl_rand10_s{_seed}"] = _make_random10(_seed)
STRATEGIES.update(ROBUSTNESS_VARIANTS)


def index_buyhold_stats(f: Features, start: str, end: str) -> list[dict[str, Any]]:
    """指数买入持有对照。"""
    rows = []
    idx_sel = (f.m.dates >= start) & (f.m.dates <= end)
    for code in ["000300.SH", "000905.SH", "000852.SH", "399006.SZ"]:
        c = f.index_close(code)[idx_sel]
        c = c[np.isfinite(c)]
        if len(c) < 2:
            continue
        rows.append({"name": f"index_{code}", "cum_return": round(float(c[-1] / c[0] - 1), 4)})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="2026-07 策略搜索")
    parser.add_argument("--split", default="train", choices=list(SPLITS))
    parser.add_argument("--strategies", default="all", help="逗号分隔的策略名，或 all")
    parser.add_argument("--unlock-holdout", action="store_true", help="显式解锁 holdout（仅限最终候选）")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    if args.split == "holdout" and not args.unlock_holdout:
        raise SystemExit("holdout 已锁定：仅最终候选可用 --unlock-holdout 运行，防止数据窥探。")

    start, end = SPLITS[args.split]
    output_dir = Path(args.output) / args.split
    output_dir.mkdir(parents=True, exist_ok=True)

    panels = pb.load_panels()
    market = pb.build_market(panels)
    feats = Features(market)
    logger.info("market ready: %d dates x %d symbols, adj=%s", len(market.dates), len(market.symbols), market.adj_source)

    names = list(STRATEGIES) if args.strategies == "all" else [s.strip() for s in args.strategies.split(",")]
    rows: list[dict[str, Any]] = []
    registry_path = Path(args.output) / "trial_registry.jsonl"
    for name in names:
        builder = STRATEGIES[name]
        sig, kw = builder(feats)
        regime = kw.pop("regime_on", None)
        result = pb.run_strategy(
            market, sig, name=name, start=start, end=end, regime_on=regime, **kw,
        )
        row = {"name": name, "split": args.split, **result.stats}
        rows.append(row)
        logger.info("%-22s cum=%7.2f%% mdd=%7.2f%% sharpe=%6s trades=%5s expo=%.2f",
                    name, (result.stats.get("cum_return") or 0) * 100,
                    (result.stats.get("max_drawdown") or 0) * 100,
                    result.stats.get("sharpe"), result.stats.get("n_trades"),
                    result.stats.get("avg_exposure") or 0)
        result.daily_equity.to_csv(output_dir / f"{name}_equity.csv", encoding="utf-8-sig")
        pd.DataFrame([t.__dict__ for t in result.trades]).to_csv(
            output_dir / f"{name}_trades.csv", index=False, encoding="utf-8-sig")
        with open(registry_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "name": name, "split": args.split, "params": {k: str(v) for k, v in kw.items()},
                "adj_source": market.adj_source, "stats": result.stats,
            }, ensure_ascii=False, default=str) + "\n")

    rows.extend(index_buyhold_stats(feats, start, end))
    summary = pd.DataFrame(rows).sort_values("cum_return", ascending=False)
    summary.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
