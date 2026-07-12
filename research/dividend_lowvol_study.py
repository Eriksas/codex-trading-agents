"""
dividend_lowvol_study.py - 红利低波规范：预注册四段一次性复检（2026-07-10）

## 预注册声明（运行前写死，全文即协议）

项目首次动用分红事件流（56,541 条）。规则参数全部取自公开指数编制惯例的
合成简化，运行后不得调参；三个配置一次性跑完 2016-2026 全部四段并全量报告。

滚动股息率（point-in-time，基差无关构造）：
  在每个除权日 ex 记 dividend_return = 每股现金分红 / 前收盘价；
  yield_t = 过去 244 个交易日内 dividend_return 之和。
  （送股/配股不计入股息；收益端由事件流复权因子完整捕获分红再投资。）

配置（仅以下三个，出处：中证红利 100 只股息率加权、红利低波 50 只惯例，
25 只为个人账户可执行规模；波动过滤取横截面下半；yield 上限 20% 防
一次性特别分红失真）：
  divlv_25 ：universe ∩ 0<yield<20% ∩ vol120 低半 → yield 前 25 只等权
  divlv_50 ：同上取前 50 只
  div_25   ：无波动过滤对照（分离"红利"与"低波"贡献）
  调仓：每 60 个交易日整仓（季调近似；hold=60, n_tranches=1, stride=60）
  成本：双边 30bp；执行 T+1 开盘（引擎协议）

判定标准（预声明）：修正幸存者偏差（2016-2020 段 -1.2%/年、2021+ 段
-0.33%/年）后全四段累计非负，且各段 MDD ≤ 15%（用户约束）→ 进入
forward 记录与底仓候选；任一不满足 → 存档为负结果。

检验现实披露：2021-2026 段被历轮研究污染、2016-2020 被复检看过一次；
本轮为"惯例参数冻结的一次性复检"（无搜索、无选择），污染风险限于
"我知道红利风格在这些年的大致表现"这一先验——该先验正是选择本家族的
原因，无法也不必伪装不存在，如实声明之。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "research"))
os.environ.setdefault("PANEL_CACHE", str(ROOT_DIR / "data/expanded/panel_cache_v2.pkl"))
import panel_backtester as pb

DUMP_AF = ROOT_DIR / "data/expanded/fuyao_dumps/a_share_adjustment_factors_event_none_all_20260708.parquet"
SEGMENTS = {
    "ext2016": ("2016-07-08", "2020-12-31"),
    "train": ("2021-01-04", "2023-12-29"),
    "validation": ("2024-01-02", "2025-06-30"),
    "burned": ("2025-07-01", "2026-06-26"),
}


def build_yield_panel(market: pb.Market) -> np.ndarray:
    """滚动 12 个月股息率面板（除权日 point-in-time）。"""
    af = pd.read_parquet(DUMP_AF)
    af["ex_date"] = (pd.to_datetime(af["ex_date_ms"], unit="ms", utc=True)
                     .dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d"))
    af["div"] = pd.to_numeric(af["dividend_per_share"], errors="coerce").fillna(0.0)
    af = af[af["div"] > 0]

    dates = list(market.dates)
    date_pos = {d: i for i, d in enumerate(dates)}
    close = market.close_raw
    sym_pos = {s: j for j, s in enumerate(market.symbols)}

    div_ret = np.zeros(close.shape)
    n_applied = n_skipped = 0
    for _, ev in af.iterrows():
        ts = ev["thscode"]
        j = sym_pos.get(ts)
        if j is None:
            n_skipped += 1
            continue
        d = ev["ex_date"]
        if d in date_pos:
            i = date_pos[d]
        else:
            later = [x for x in dates if x >= d]
            if not later:
                n_skipped += 1
                continue
            i = date_pos[later[0]]
        if i == 0:
            n_skipped += 1
            continue
        prev = close[:i, j]
        prev = prev[np.isfinite(prev)]
        if len(prev) == 0:
            n_skipped += 1
            continue
        div_ret[i, j] += float(ev["div"]) / float(prev[-1])
        n_applied += 1
    print(f"dividend events applied={n_applied} skipped={n_skipped}")
    return pd.DataFrame(div_ret).rolling(244, min_periods=1).sum().to_numpy()


def main() -> None:
    market = pb.build_market(pb.load_panels())
    yld = build_yield_panel(market)
    vol120 = pd.DataFrame(market.adj_ret).rolling(120, min_periods=120).std().to_numpy()
    vol_med = np.nanmedian(np.where(market.universe, vol120, np.nan), axis=1, keepdims=True)
    low_vol = vol120 <= vol_med
    base_ok = (yld > 0) & (yld < 0.20)

    configs = {
        "divlv_25": (np.where(base_ok & low_vol, yld, np.nan), 25),
        "divlv_50": (np.where(base_ok & low_vol, yld, np.nan), 50),
        "div_25": (np.where(base_ok, yld, np.nan), 25),
    }
    surv = {"ext2016": 0.012, "train": 0.0033, "validation": 0.0033, "burned": 0.0033}

    rows = []
    for name, (sig, k) in configs.items():
        for seg, (s, e) in SEGMENTS.items():
            res = pb.run_strategy(market, sig, name=f"{name}|{seg}", start=s, end=e,
                                  top_k=k, hold_days=60, n_tranches=1, entry_stride=60)
            st = res.stats
            years = st["n_days"] / 244
            corrected = st["cum_return"] - surv[seg] * years
            rows.append({"config": name, "seg": seg, "cum": st["cum_return"],
                         "cum_surv_corrected": round(corrected, 4),
                         "mdd": st["max_drawdown"], "calmar": st.get("calmar"),
                         "sharpe": st.get("sharpe"), "trades": st["n_trades"],
                         "expo": st.get("avg_exposure")})
            print(f"{name:9s} {seg:10s} cum={st['cum_return']:+7.2%} 修正后={corrected:+7.2%} "
                  f"mdd={st['max_drawdown']:7.2%} sharpe={st.get('sharpe')}")
    out = ROOT_DIR / "output" / "dividend_lowvol_study"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out / "results.csv", index=False, encoding="utf-8-sig")
    print("saved:", out / "results.csv")


if __name__ == "__main__":
    main()
