"""
framework_repair_202607.py - 执行框架修复轮 runner

背景（docs/v3_reaudit_2026-07-03.md）：V3 的二元环境 gate 全样本仅 18% 天数放行、
平均暴露 2%，深熊少亏但牛市结构性缺席；Faber 二元滞回带则在震荡市被鞭打。
本轮测试 4 个"连续/分级暴露"框架 + 2 个对照，修的是结构，不是拟合牛市。

## 预声明协议（写在运行之前）
- 选择只依据训练段（2021-01~2023-12）与验证段（2024-01~2025-06）。
- 2025-07 之后为已烧毁参考段（本项目已观察过该段行情），其数字仅标注展示，
  不参与任何选择决策。
- 选择标准：训练与验证两段上 Calmar 均优于满仓对照 G0，且两段累计收益均 ≥ 0。
- 框架参数全部取文献/惯例默认值（各 builder docstring 注明出处），禁止调参。
- 真正的检验 = 2026-07 起 forward paper 每日记录 gate 状态。

## 执行近似说明
引擎为滚动分批模型：批次开仓时锁定当日目标暴露 w，持有期内不变，
组合聚合暴露以约 hold/2 天滞后跟踪目标；目标 ≤0.01 时全部批次次日开盘
强制清仓（崩塌硬切不受滞后影响）。

本模块只读本地数据，不修改主策略、不写台账、不连接实盘。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel_backtester as pb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SPLITS = {
    "train": ("2021-01-04", "2023-12-29"),
    "validation": ("2024-01-02", "2025-06-30"),
    "burned": ("2025-07-01", "2026-06-26"),   # 已烧毁参考段，不参与选择
}
DEFAULT_OUTPUT = pb.ROOT_DIR / "output" / "framework_repair_202607"
INDEX_SET = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "000905.SH", "000852.SH"]
BENCH = "000300.SH"
BUFFER = 0.10  # Carver (2015) 仓位惰性区惯例值


def _idx_close(market: pb.Market, code: str) -> np.ndarray:
    return market.index_close[code].reindex(market.dates).astype(float).to_numpy()


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n, min_periods=n).mean().to_numpy()


def _apply_buffer(w_raw: np.ndarray, buffer: float = BUFFER) -> np.ndarray:
    """Carver 式仓位惰性区：|目标-持有| >= buffer 才移动。"""
    out = np.zeros_like(w_raw)
    held = 0.0
    for i, w in enumerate(w_raw):
        if np.isfinite(w) and abs(w - held) >= buffer:
            held = float(w)
        out[i] = held
    return out


# ---------------------------------------------------------------------------
# 框架暴露序列（行 t 只用 t 及之前数据）
# ---------------------------------------------------------------------------


def expo_g0_full(market: pb.Market) -> np.ndarray:
    """G0 对照：恒定满仓。"""
    return np.ones(len(market.dates))


def expo_g1_v3_regime(market: pb.Market) -> np.ndarray:
    """G1 对照：V3 原版环境 gate（积极=1 否则 0），修正指数上复现。"""
    src_dir = str(pb.ROOT_DIR / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    import quant_core as qc

    qc.DEFAULT_INDEX_CACHE_DIR = pb.EXPANDED_DIR / "index"
    tl = qc._market_timeline()
    w = np.zeros(len(market.dates))
    for i, d in enumerate(market.dates):
        w[i] = 1.0 if (tl.get(str(d)) or {}).get("regime_label") == "积极" else 0.0
    return w


def expo_g2_trend_vote(market: pb.Market) -> np.ndarray:
    """G2 多尺度趋势投票：6 指数 × (MA20/60/120/200 + 海龟20/10、55/20通道) = 36票。

    参数出处：均线 20/60/120/200 = 月/季/半年/年线惯例（Faber 2007, BLL 1992）；
    通道 20入/10出、55入/20出 = 海龟法则原始参数；0.10 缓冲 = Carver 2015。
    """
    votes = []
    for code in INDEX_SET:
        c = _idx_close(market, code)
        ma_votes = [(c > _sma(c, n)).astype(float) for n in (20, 60, 120, 200)]
        for entry_n, exit_n in ((20, 10), (55, 20)):
            hi = pd.Series(c).shift(1).rolling(entry_n, min_periods=entry_n).max().to_numpy()
            lo = pd.Series(c).shift(1).rolling(exit_n, min_periods=exit_n).min().to_numpy()
            state = np.zeros(len(c))
            cur = 0.0
            for i in range(len(c)):
                if np.isfinite(hi[i]) and c[i] >= hi[i]:
                    cur = 1.0
                elif np.isfinite(lo[i]) and c[i] <= lo[i]:
                    cur = 0.0
                state[i] = cur
            ma_votes.append(state)
        votes.append(np.nanmean(np.vstack(ma_votes), axis=0))
    w_raw = np.nanmean(np.vstack(votes), axis=0)
    w_raw = np.where(np.isfinite(w_raw), w_raw, 0.0)
    return _apply_buffer(w_raw)


def _ewma_var(r: np.ndarray, lam: float = 0.94, downside: bool = False) -> np.ndarray:
    """RiskMetrics EWMA 方差（downside=True 时为 2×半下行方差）。"""
    v = np.zeros_like(r)
    cur = np.nanvar(r[:60]) if np.isfinite(r[:60]).sum() > 10 else 1e-6
    for i, x in enumerate(r):
        if np.isfinite(x):
            term = 2.0 * min(x, 0.0) ** 2 if downside else x ** 2
            cur = lam * cur + (1 - lam) * term
        v[i] = cur
    return v


def expo_g3_downside_vol_trend(market: pb.Market) -> np.ndarray:
    """G3 下行半方差目标波动 × 多尺度趋势调制。

    参数出处：λ=0.94 (RiskMetrics 1996)；σ_target=15% (Harvey et al. 2018 惯例上沿)；
    回看 {21,63,126,252} (MOP 2012 / Baltas-Kosowski 2013)；z 截断 ±2；
    T=clip(0.5+s,0,1) 中性半仓先验；缓冲 0.10 (Carver 2015)。
    """
    c = _idx_close(market, BENCH)
    r = np.diff(np.log(np.where(c > 0, c, np.nan)), prepend=np.nan)
    v_down = _ewma_var(r, downside=True)
    sigma_down = np.sqrt(252 * v_down)
    w_vol = np.clip(0.15 / np.where(sigma_down > 0, sigma_down, np.inf), 0, 1)
    v_tot = _ewma_var(r, downside=False)
    sigma_tot = np.sqrt(252 * v_tot)
    ms = []
    for k in (21, 63, 126, 252):
        ret_k = c / np.concatenate([np.full(k, np.nan), c[:-k]]) - 1
        z = ret_k / (sigma_tot * np.sqrt(k / 252))
        ms.append(np.clip(z, -2, 2) / 2)
    s = np.nanmean(np.vstack(ms), axis=0)
    t_mult = np.clip(0.5 + s, 0, 1)
    w_raw = np.where(np.isfinite(t_mult), w_vol * t_mult, 0.0)
    return _apply_buffer(w_raw)


def _g4_ladder_raw(market: pb.Market) -> np.ndarray:
    """G4 原始档位序列（缓冲前）。"""
    c = _idx_close(market, BENCH)
    r = np.diff(np.log(np.where(c > 0, c, np.nan)), prepend=np.nan)
    sigma_tot = np.sqrt(252 * _ewma_var(r))
    ret21 = c / np.concatenate([np.full(21, np.nan), c[:-21]]) - 1
    z21 = ret21 / (sigma_tot * np.sqrt(21 / 252))
    peak = pd.Series(c).rolling(252, min_periods=60).max().to_numpy()
    dd = 1 - c / peak
    LEVELS = [1.0, 0.6, 0.3, 0.0]

    def dd_level(d: float) -> int:
        if not np.isfinite(d):
            return 3
        return 0 if d < 0.10 else 1 if d < 0.20 else 2 if d < 0.30 else 3

    w = np.zeros(len(c))
    cur = 3          # 从防守起步（暖机期）
    slow_days = 0
    fast_days = 0
    for i in range(len(c)):
        base = dd_level(dd[i])
        if np.isfinite(z21[i]) and z21[i] < -1.5:
            base = min(3, base + 1)          # 速度加罚
        if base > cur:
            cur = base                        # 降档立即生效
            slow_days = fast_days = 0
        elif base < cur:
            # 慢通道：回撤回落且带 2% 滞回，连续 10 日
            thresh = [0.10, 0.20, 0.30, np.inf][cur - 1] - 0.02 if cur >= 1 else 0.0
            slow_days = slow_days + 1 if (np.isfinite(dd[i]) and dd[i] < thresh) else 0
            if slow_days >= 10:
                cur -= 1
                slow_days = 0
            # 快通道：z21>2 每满 5 日升一档，封顶 0.6（level 1）
            fast_days = fast_days + 1 if (np.isfinite(z21[i]) and z21[i] > 2.0) else 0
            if fast_days >= 5 and cur > 1:
                cur -= 1
                fast_days = 0
        else:
            slow_days = fast_days = 0
        w[i] = LEVELS[cur]
    return w


def expo_g4_dd_ladder(market: pb.Market) -> np.ndarray:
    """G4 回撤阶梯状态机 + 动量快恢复。

    参数出处：10%/20% = Ned Davis correction/bear 标准定义（A股加 30% 档）；
    Grossman-Zhou (1993) 回撤约束下暴露单调递减；峰值窗 252 日；
    z 阈 -1.5/+2.0 = t 统计惯例；滞回 2%；驻留 10 日/快通道 5 日。
    """
    return _g4_ladder_raw(market)   # 档位本身离散，无需再加缓冲


def expo_g5_min_overlay(market: pb.Market) -> np.ndarray:
    """G5 双层取小：min(G3 原始输出, G4 档位)，再加 0.10 缓冲。

    出处：vol-target 核心 + drawdown overlay 取下界的机构分层实践
    （Harvey et al. 2018；无新增参数）。
    """
    # G3 缓冲前的原始 w
    c = _idx_close(market, BENCH)
    r = np.diff(np.log(np.where(c > 0, c, np.nan)), prepend=np.nan)
    sigma_down = np.sqrt(252 * _ewma_var(r, downside=True))
    w_vol = np.clip(0.15 / np.where(sigma_down > 0, sigma_down, np.inf), 0, 1)
    sigma_tot = np.sqrt(252 * _ewma_var(r))
    ms = []
    for k in (21, 63, 126, 252):
        ret_k = c / np.concatenate([np.full(k, np.nan), c[:-k]]) - 1
        ms.append(np.clip(ret_k / (sigma_tot * np.sqrt(k / 252)), -2, 2) / 2)
    g3_raw = np.where(np.isfinite(np.nanmean(np.vstack(ms), axis=0)),
                      w_vol * np.clip(0.5 + np.nanmean(np.vstack(ms), axis=0), 0, 1), 0.0)
    return _apply_buffer(np.minimum(g3_raw, _g4_ladder_raw(market)))


FRAMEWORKS: dict[str, Callable] = {
    "g0_full": expo_g0_full,
    "g1_v3_regime": expo_g1_v3_regime,
    "g2_trend_vote": expo_g2_trend_vote,
    "g3_dvol_trend": expo_g3_downside_vol_trend,
    "g4_dd_ladder": expo_g4_dd_ladder,
    "g5_min_overlay": expo_g5_min_overlay,
}


# ---------------------------------------------------------------------------
# 篮子
# ---------------------------------------------------------------------------


def basket_signals(market: pb.Market) -> dict[str, tuple[np.ndarray, int]]:
    """两个 beta 载体：B1 流动性前10（集中）、B2 随机300等权（分散）。"""
    adv20 = pd.DataFrame(market.amount).rolling(20, min_periods=20).mean().to_numpy()
    return {
        "b1_liq10": (adv20, 10),
        "b2_broad300": (pb.sig_random(market, seed=42), 300),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="执行框架修复轮")
    parser.add_argument("--splits", default="train,validation")
    parser.add_argument("--include-burned", action="store_true",
                        help="附带已烧毁参考段（仅展示，不得用于选择）")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    market = pb.build_market(pb.load_panels())
    baskets = basket_signals(market)
    expos = {name: fn(market) for name, fn in FRAMEWORKS.items()}
    for name, w in expos.items():
        logger.info("framework %-16s 平均目标暴露 %.2f", name, float(np.nanmean(w)))

    splits = [s.strip() for s in args.splits.split(",")]
    if args.include_burned:
        splits.append("burned")
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    registry = out_dir / "trial_registry.jsonl"

    rows: list[dict[str, Any]] = []
    for split in splits:
        start, end = SPLITS[split]
        for bname, (sig, k) in baskets.items():
            for fname, w in expos.items():
                name = f"{fname}|{bname}"
                res = pb.run_strategy(
                    market, sig, name=name, start=start, end=end,
                    top_k=k, hold_days=20, n_tranches=20, entry_stride=1,
                    exposure_series=w,
                )
                tag = "BURNED_REFERENCE" if split == "burned" else split
                row = {"split": tag, "framework": fname, "basket": bname, **res.stats}
                rows.append(row)
                logger.info("%-10s %-16s %-11s cum=%7.2f%% mdd=%7.2f%% calmar=%6s expo=%.2f",
                            tag, fname, bname, (res.stats.get("cum_return") or 0) * 100,
                            (res.stats.get("max_drawdown") or 0) * 100,
                            res.stats.get("calmar"), res.stats.get("avg_exposure") or 0)
                res.daily_equity.to_csv(out_dir / f"{split}_{fname}_{bname}_equity.csv", encoding="utf-8-sig")
                with open(registry, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                          "split": tag, "framework": fname, "basket": bname,
                                          "stats": res.stats}, ensure_ascii=False, default=str) + "\n")

    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "summary.csv", index=False, encoding="utf-8-sig")
    print(df[["split", "framework", "basket", "cum_return", "max_drawdown", "calmar",
              "sharpe", "avg_exposure", "n_trades", "hard_cut_liquidations"]].to_string(index=False))


if __name__ == "__main__":
    main()
