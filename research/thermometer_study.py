"""
thermometer_study.py - 涨停生态温度计：面板自建历史 + 描述性研究

背景：fuyao 特色数据端点全系快照型（date 参数被无视，2026-07-09 实锤），
涨停生态的历史只能且应当从自有面板推导——价格与时变涨跌停限制齐备，
十年全量、零外部依赖。

本研究为**描述性**：量化温度计与未来指数收益的统计关系，不选任何交易规则、
不做参数搜索。2024 年后数据段已被既往研究烧毁，任何基于本研究的规则设计
只能以 forward 记录为证据面。

指标定义（T 日收盘）：
  limit_up_n    收盘涨停家数（close 距涨停价 0.5% 内，非除权日）
  limit_dn_n    收盘跌停家数
  max_streak    最高连板高度（连续收盘涨停天数的当日最大值）
  streak2_n     ≥2 连板家数
输出：data/expanded/thermometer_daily.csv + 分位数前瞻收益表（stdout）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PANEL_CACHE", str(Path(__file__).resolve().parents[1] / "data/expanded/panel_cache_v2.pkl"))
import panel_backtester as pb


def build_thermometer(market: pb.Market) -> pd.DataFrame:
    dates = market.dates
    close = market.close_raw
    prev = market.prev_close_raw
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = close / prev - 1
    # 时变涨跌停（与 build_market 同规则）
    limit = np.broadcast_to(market.limit_pct, close.shape).copy()
    chinext = np.array([str(c)[:3] in ("300", "301") for c in market.symbols])
    pre = np.array(dates < pb.CHINEXT_20PCT_SINCE)
    limit[np.ix_(pre, chinext)] = 0.10

    up = (ret >= limit - 0.005) & ~market.corp_action & np.isfinite(ret)
    dn = (ret <= -(limit - 0.005)) & ~market.corp_action & np.isfinite(ret)

    # 连板：逐日递推 streak
    streak = np.zeros(close.shape[1], dtype=int)
    max_streak = np.zeros(len(dates), dtype=int)
    streak2_n = np.zeros(len(dates), dtype=int)
    for t in range(len(dates)):
        streak = np.where(up[t], streak + 1, 0)
        max_streak[t] = streak.max() if streak.size else 0
        streak2_n[t] = int((streak >= 2).sum())

    return pd.DataFrame({
        "date": dates,
        "limit_up_n": up.sum(axis=1),
        "limit_dn_n": dn.sum(axis=1),
        "max_streak": max_streak,
        "streak2_n": streak2_n,
    }).set_index("date")


def study(thermo: pd.DataFrame, market: pb.Market) -> None:
    """分位数前瞻收益表 + 秩相关（描述性，无选择）。"""
    for idx_code in ["000300.SH", "000852.SH"]:
        c = market.index_close[idx_code].reindex(market.dates).astype(float)
        for h in (5, 20):
            fwd = (c.shift(-h) / c - 1).to_numpy()
            print(f"\n=== {idx_code} 未来{h}日收益 vs 温度计（2016-2026 全样本，描述性）===")
            for col in ["limit_up_n", "max_streak", "streak2_n", "limit_dn_n"]:
                v = thermo[col].to_numpy(dtype=float)
                ok = np.isfinite(v) & np.isfinite(fwd)
                q = pd.qcut(pd.Series(v[ok]), 5, labels=False, duplicates="drop")
                tbl = pd.Series(fwd[ok]).groupby(q).mean()
                ic = pd.Series(v[ok]).rank().corr(pd.Series(fwd[ok]).rank())
                cells = " ".join(f"Q{int(k)+1}:{x:+.2%}" for k, x in tbl.items())
                print(f"  {col:12s} rankIC={ic:+.3f} | {cells}")


if __name__ == "__main__":
    market = pb.build_market(pb.load_panels())
    thermo = build_thermometer(market)
    out = Path(__file__).resolve().parents[1] / "data/expanded/thermometer_daily.csv"
    thermo.to_csv(out, encoding="utf-8-sig")
    print(f"thermometer written: {out} ({len(thermo)} days)")
    print(thermo.tail(5).to_string())
    study(thermo, market)
