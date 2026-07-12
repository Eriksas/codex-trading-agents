"""
cb_double_low_study.py - 可转债双低轮动：冻结参数四段一次性复检（2026-07-12）

## 预注册声明（运行前冻结，运行后不得调参）

数据：akshare 三源交叉验证通过（含退市券完整生命周期，99.7% 价格吻合），
1,015 只券估值序列。**数据自带退市券 → 无幸存者偏差修正项。**

口径修正（2026-07-12 首跑后）：首跑发现 40xxxx 代码为退市转债的老三板
转让代码（厘价报价、无可执行性），其双低值恒为极低导致回测爆炸
（2024 段 +414 万%，两次运行均留痕于 output/）。策略池修正为交易所
正常挂牌券（代码 11/12 开头）——这是 universe 范围定义修正而非调参；
真实违约事件（如岭南）在退市转板前的下跌仍完整保留在样本内。

规则（出处=集思录双低指数公式与社区惯例，一次冻结）：
  资格：上市满 10 个交易日；发行规模 ≥3 亿；收盘价 <140 元（强赎进程保守代理）；
       转股溢价率非缺失。
  打分：双低值 = 收盘价 + 转股溢价率（百分点）。
  持仓：双低最低 15 只等权；每月首个交易日按收盘调仓；
       缓冲带：已持仓券跌出双低前 30 名才换出（减换手）。
  退出假设：每只券在其数据终点前 15 个交易日强制清仓
       （历史强赎公告不可得的保守代理，如实标注）。
  成本：双边 10bp（转债无印花税；佣金+滑点保守值）。
  票息：不计入（约 -0.3%/年保守方向偏差）。
  分段：2018-2020 / 2021-2023 / 2024-2025H1 / 2025H2-2026
       （2016-2017 横截面 <40 只，无统计意义，仅披露不判定）。

判定标准（预声明）：四段累计全部非负 且 各段 MDD ≤ 15%
  → 进入 forward 双轨（快照采集线已另案）；任一不满足 → 存档。
  注：转债数据对本项目为处女地（历轮挖掘未触碰），本检验的污染程度
  显著低于股票面板，但集思录双低策略的公开流行本身构成家族先验，如实声明。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CB_DIR = ROOT / "data" / "expanded" / "cb"
OUT = ROOT / "output" / "cb_double_low_study"
COST = 0.0010          # 双边
SEGMENTS = {
    "seg2018_20": ("2018-01-01", "2020-12-31"),
    "seg2021_23": ("2021-01-01", "2023-12-29"),
    "seg2024_25H1": ("2024-01-02", "2025-06-30"),
    "seg2025H2_26": ("2025-07-01", "2026-07-10"),
}

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def load_cb_panels() -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """返回 close/premium 宽表与发行规模。"""
    lst = pd.read_csv(CB_DIR / "cb_list.csv", dtype={"债券代码": str})
    size = lst.set_index("债券代码")["发行规模"]
    size = pd.to_numeric(size, errors="coerce")
    closes, prems = {}, {}
    for p in sorted((CB_DIR / "valuation").glob("*.csv")):
        code = p.stem
        df = pd.read_csv(p, dtype={"日期": str})
        df = df.set_index("日期")
        closes[code] = pd.to_numeric(df["收盘价"], errors="coerce")
        prems[code] = pd.to_numeric(df["转股溢价率"], errors="coerce")
    close = pd.DataFrame(closes).sort_index()
    prem = pd.DataFrame(prems).reindex(index=close.index)
    return close, prem, size


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    close, prem, size = load_cb_panels()
    dates = close.index.to_numpy()
    log.info("CB panel: %d 日 x %d 券 (%s ~ %s)", *close.shape, dates[0], dates[-1])

    # 资格面板
    listed_days = close.notna().cumsum()
    ok_listed = listed_days >= 10
    ok_size = pd.Series({c: (size.get(c, np.nan) >= 3.0) for c in close.columns})
    ok_price = close < 140
    # 数据终点前 15 日强制退出：构造"可持有"掩码
    last_valid = close.apply(lambda s: s.last_valid_index())
    holdable = pd.DataFrame(True, index=close.index, columns=close.columns)
    for c in close.columns:
        lv = last_valid[c]
        if lv is None:
            holdable[c] = False
            continue
        pos = close.index.get_loc(lv)
        cutoff = max(0, pos - 15)
        holdable.iloc[cutoff:, holdable.columns.get_loc(c)] = False

    normal_venue = pd.Series({c: str(c)[:2] in ("11", "12") for c in close.columns})
    eligible = (ok_listed & ok_price & prem.notna() & close.notna() & holdable).mul(ok_size & normal_venue, axis=1).fillna(False)
    dlow = (close + prem).where(eligible)

    # 月首调仓日
    months = pd.Series(close.index).str[:7]
    is_month_start = months.ne(months.shift(1)).to_numpy()
    rebal_idx = np.where(is_month_start)[0]

    rows = []
    for seg, (s, e) in SEGMENTS.items():
        in_seg = (dates >= s) & (dates <= e)
        seg_days = np.where(in_seg)[0]
        if len(seg_days) == 0:
            continue
        held: list[str] = []
        equity = [1.0]
        eq_dates = []
        n_trades = 0
        width = []
        for t in seg_days:
            d = close.index[t]
            if t in rebal_idx or not held:
                row = dlow.iloc[t].dropna().sort_values()
                width.append(len(row))
                if len(row) >= 15:
                    top15 = list(row.index[:15])
                    top30 = set(row.index[:30])
                    keep = [c for c in held if c in top30 and np.isfinite(dlow.iloc[t].get(c, np.nan))]
                    add = [c for c in top15 if c not in keep][: 15 - len(keep)]
                    new_held = keep + add
                    turnover = len(set(new_held) ^ set(held)) / 30.0
                    equity[-1] *= 1 - turnover * COST * 15 / max(1, len(new_held))
                    n_trades += len(set(new_held) ^ set(held))
                    held = new_held
            # 当日收益（等权，持仓券当日无价则视为持平——强制退出规则已提前清仓）
            if held and t + 1 < len(dates):
                rets = []
                for c in held:
                    a, b = close.iloc[t][c], close.iloc[t + 1][c]
                    if np.isfinite(a) and np.isfinite(b) and a > 0:
                        rets.append(b / a - 1)
                if rets:
                    equity.append(equity[-1] * (1 + float(np.mean(rets))))
                    eq_dates.append(close.index[t + 1])
        eqs = pd.Series(equity[1:], index=eq_dates[:len(equity) - 1], dtype=float)
        if eqs.empty:
            continue
        cum = float(eqs.iloc[-1] / eqs.iloc[0] - 1)
        mdd = float((eqs / eqs.cummax() - 1).min())
        years = len(eqs) / 244
        ann = (1 + cum) ** (1 / years) - 1
        dr = eqs.pct_change().dropna()
        sharpe = float(dr.mean() / dr.std() * np.sqrt(244)) if dr.std() > 0 else float("nan")
        rows.append({"seg": seg, "cum": round(cum, 4), "ann": round(ann, 4),
                     "mdd": round(mdd, 4), "sharpe": round(sharpe, 3),
                     "trades": n_trades, "avg_width": int(np.mean(width)) if width else 0})
        log.info("%-12s cum=%+7.2f%% ann=%+6.2f%% mdd=%7.2f%% sharpe=%6.2f 截面均宽=%d 换券=%d",
                 seg, cum * 100, ann * 100, mdd * 100, sharpe,
                 int(np.mean(width)) if width else 0, n_trades)
        eqs.to_csv(OUT / f"{seg}_equity.csv", encoding="utf-8-sig")
    pd.DataFrame(rows).to_csv(OUT / "results.csv", index=False, encoding="utf-8-sig")
    log.info("saved: %s", OUT / "results.csv")


if __name__ == "__main__":
    main()
