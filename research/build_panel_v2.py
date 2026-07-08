"""
build_panel_v2.py - 面板 v2 构建器（fuyao dump 2016-2026 数据栈）

数据栈（docs/fuyao_api_capability_2026-07.md 验证结论）：
  价格   = fuyao 10y dump 原始价（已验证与 BaoStock 逐日一致）
  复权   = fuyao 分红/送股/配股事件流推导（与 BaoStock hfq 对账偏差 0.00036%）
  指数   = BaoStock 2016-2026
  换手率 = 既有 daily_basic（efinance，2021+；2016-2020 为 NaN，特征满窗要求下自动排除）
  时变ST = baostock_st_2016（覆盖不足时回退：2021+ 时变 + 2016-2020 静态名单）

除权参考价公式（沪深交易所口径）：
  ref = (prev_close - 现金红利 + 配股价×配股比例) / (1 + 送转比例 + 配股比例)
  hfq 因子跳变 = prev_close / ref；因子 = 跳变的时序累积

输出：data/expanded/panel_cache_v2.pkl（与 v1 同 schema，panel_backtester 直接可用）
构建后自动执行两道对账：
  A. 事件流复权 vs BaoStock hfq（2021+ 抽样 60 只，日收益最大偏差）
  B. v2 vs v1 重叠段（2021-2026）原始收盘价一致性

已知边界：dump 不含退市股（幸存者偏差随 2016-2020 延伸而增大，报告必须披露）；
dump volume 单位为股（v1 为手），仅用于停牌判定，不受影响。
"""

from __future__ import annotations

import logging
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
EXPANDED = ROOT_DIR / "data" / "expanded"
DUMP_10Y = EXPANDED / "fuyao_dumps" / "a_share_daily_k_1d_none_10y_20260708.parquet"
DUMP_AF = EXPANDED / "fuyao_dumps" / "a_share_adjustment_factors_event_none_all_20260708.parquet"
OUT_CACHE = EXPANDED / "panel_cache_v2.pkl"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _board_limit(ts_code: str) -> float:
    prefix = ts_code[:3]
    return 0.20 if prefix in ("300", "301", "688", "689") else 0.10


def build() -> dict:
    logger.info("loading 10y dump ...")
    kl = pd.read_parquet(DUMP_10Y, columns=[
        "thscode", "date_ms", "open_price", "high_price", "low_price",
        "close_price", "volume", "turnover"])
    kl["date"] = (pd.to_datetime(kl["date_ms"], unit="ms", utc=True)
                  .dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d"))

    panels: dict = {}
    for src, dst in [("open_price", "open"), ("high_price", "high"), ("low_price", "low"),
                     ("close_price", "close"), ("volume", "vol"), ("turnover", "amount")]:
        panels[dst] = kl.pivot(index="date", columns="thscode", values=src).astype("float32")
    dates, symbols = panels["close"].index, panels["close"].columns
    logger.info("panels: %d dates x %d symbols (%s ~ %s)", len(dates), len(symbols), dates[0], dates[-1])

    # 换手率：沿用 v1 的 daily_basic（2021+），2016-2020 NaN
    basic = pd.read_csv(EXPANDED / "daily_basic.csv",
                        usecols=["trade_date", "ts_code", "turnover_rate"], dtype={"ts_code": "category"})
    turn = basic.pivot(index="trade_date", columns="ts_code", values="turnover_rate").astype("float32")
    panels["turnover"] = turn.reindex(index=dates, columns=symbols)

    panels["limit_pct"] = pd.Series([_board_limit(str(c)) for c in symbols], index=symbols, dtype="float32")

    stock_basic = pd.read_csv(EXPANDED / "stock_basic.csv")
    st_codes = set(stock_basic.loc[stock_basic["is_st"] == True, "ts_code"])  # noqa: E712
    panels["st_mask"] = pd.Series([str(c) in st_codes for c in symbols], index=symbols)

    # 时变 ST：优先 2016 全量目录，回退 2021+ 目录（2016-2020 用静态名单近似并记录）
    st_daily = pd.DataFrame(False, index=dates, columns=symbols)
    st_note = "static_fallback"
    for st_dir, note in [(EXPANDED / "baostock_st_2016", "timevarying_2016"),
                         (EXPANDED / "baostock_st", "timevarying_2021_static_before")]:
        files = list(st_dir.glob("*.csv")) if st_dir.exists() else []
        if len(files) / max(1, len(symbols)) >= 0.90:
            for pth in files:
                code, mkt = pth.stem.rsplit("_", 1)
                ts = f"{code}.{mkt}"
                if ts not in st_daily.columns:
                    continue
                s = pd.read_csv(pth)
                flags = pd.to_numeric(s.set_index("date")["isST"], errors="coerce").fillna(0)
                st_daily[ts] = flags.reindex(dates).ffill().fillna(0).astype(bool)
            st_note = note
            if note.endswith("static_before"):
                pre = dates < "2021-01-01"
                for c in st_codes:
                    if c in st_daily.columns:
                        st_daily.loc[pre, c] = True
            break
    panels["st_daily"] = st_daily
    logger.info("ST handling: %s (ST cells=%d)", st_note, int(st_daily.to_numpy().sum()))

    # 指数（BaoStock 2016-2026）
    idx = pd.read_csv(EXPANDED / "index_daily_baostock.csv", usecols=["trade_date", "ts_code", "close"])
    panels["index_close"] = idx.pivot(index="trade_date", columns="ts_code", values="close").reindex(dates)

    # 复权因子：事件流推导
    af = pd.read_parquet(DUMP_AF)
    af["ex_date"] = (pd.to_datetime(af["ex_date_ms"], unit="ms", utc=True)
                     .dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d"))
    for c in ["dividend_per_share", "per_share_bonus", "allotment_ratio", "allotment_price"]:
        af[c] = pd.to_numeric(af[c], errors="coerce").fillna(0.0)
    close = panels["close"].astype("float64")
    date_pos = {d: i for i, d in enumerate(dates)}
    factor = pd.DataFrame(1.0, index=dates, columns=symbols)
    skipped = applied = 0
    for ts, grp in af.groupby("thscode"):
        if ts not in factor.columns:
            skipped += len(grp)
            continue
        col = close[ts].to_numpy()
        jumps: list[tuple[int, float]] = []
        for _, ev in grp.sort_values("ex_date").iterrows():
            d = ev["ex_date"]
            if d not in date_pos:
                # 除权日停牌/非交易日：找其后首个交易日位置
                later = dates[dates >= d]
                if len(later) == 0:
                    skipped += 1
                    continue
                pos = date_pos[later[0]]
            else:
                pos = date_pos[d]
            if pos == 0:
                skipped += 1
                continue
            prev_arr = col[:pos]
            prev_valid = prev_arr[np.isfinite(prev_arr)]
            if len(prev_valid) == 0:
                skipped += 1
                continue
            prev_close = float(prev_valid[-1])
            ref = (prev_close - ev["dividend_per_share"]
                   + ev["allotment_price"] * ev["allotment_ratio"]) / (
                1.0 + ev["per_share_bonus"] + ev["allotment_ratio"])
            if ref <= 0:
                skipped += 1
                continue
            jumps.append((pos, prev_close / ref))
            applied += 1
        if jumps:
            f = np.ones(len(dates))
            for pos, j in jumps:
                f[pos:] *= j
            factor[ts] = f
    panels["adj_factor"] = factor
    panels["adj_source"] = "fuyao_event_stream"
    logger.info("factor derivation: applied=%d skipped=%d", applied, skipped)

    with open(OUT_CACHE, "wb") as f:
        pickle.dump(panels, f, protocol=4)
    logger.info("panel v2 written: %s", OUT_CACHE)
    return panels


def validate(panels: dict) -> None:
    """对账 A：事件流复权 vs BaoStock hfq；对账 B：v2 vs v1 原始价。"""
    rng = np.random.default_rng(9)
    dates = panels["close"].index
    close = panels["close"].astype("float64")
    factor = panels["adj_factor"]

    hfq_dir = EXPANDED / "baostock_hfq"
    candidates = [p for p in hfq_dir.glob("*.csv")]
    sample = rng.choice(len(candidates), 60, replace=False)
    worst = 0.0
    bad_days = 0
    total_days = 0
    worst_code = ""
    for i in sample:
        pth = candidates[int(i)]
        code, mkt = pth.stem.rsplit("_", 1)
        ts = f"{code}.{mkt}"
        if ts not in close.columns:
            continue
        hfq = pd.read_csv(pth).set_index("date")["hfq_close"].astype(float)
        common = hfq.index.intersection(dates[dates >= "2021-01-04"])
        if len(common) < 300:
            continue
        v2_adj = (close[ts] * factor[ts]).reindex(common)
        r_v2 = v2_adj.pct_change().to_numpy()
        r_bs = hfq.reindex(common).pct_change().to_numpy()
        ok = np.isfinite(r_v2) & np.isfinite(r_bs)
        dev = np.abs(r_v2[ok] - r_bs[ok])
        total_days += int(ok.sum())
        bad_days += int((dev > 1e-3).sum())
        if len(dev) and dev.max() > worst:
            worst, worst_code = float(dev.max()), ts
    logger.info("对账A 复权收益 vs BaoStock hfq: 抽样60只 %d 股票日, >0.1%%偏差 %d 天 (%.4f%%), 最大偏差 %.5f (%s)",
                total_days, bad_days, 100 * bad_days / max(1, total_days), worst, worst_code)

    v1 = pickle.load(open(EXPANDED / "panel_cache_v1.pkl", "rb"))
    c1 = v1["close"].astype("float64")
    common_d = c1.index.intersection(dates)
    common_s = c1.columns.intersection(close.columns)
    a = c1.loc[common_d, common_s].to_numpy()
    b = close.loc[common_d, common_s].to_numpy()
    both = np.isfinite(a) & np.isfinite(b)
    mism = np.abs(a[both] - b[both]) > 0.011
    logger.info("对账B v2 vs v1 原始收盘: 重叠 %d 天 x %d 只, 共同有值 %d 格, 不一致(>0.011) %d 格 (%.5f%%)",
                len(common_d), len(common_s), int(both.sum()), int(mism.sum()),
                100 * mism.sum() / max(1, both.sum()))


if __name__ == "__main__":
    p = build()
    validate(p)
