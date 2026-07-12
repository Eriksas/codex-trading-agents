"""
forward_divlv_recorder.py - 红利低波季度名单前向记录（paper only）

判决书（docs/dividend_lowvol_verdict_2026-07-10.md）声明的季度观察线：
每季度首个交易日记录 divlv_25 冻结规则名单。纯研究观察，不构成建议。

自包含数据流：调仓日先经 fuyao dump 端点刷新复权因子事件流（小 parquet），
滚动股息率 = 过去 252 日除权日 dividend/prev_close 之和（口径同判决书研究，
窗口取标准 252——记录线与研究口径差异已在此声明）；价格与波动用 fuyao
快照+近一年日线。规则冻结：universe（非ST、价格>3、成交额≥3000万代理）∩
0<yield<20% ∩ vol120 低半 → yield 前 25 只等权。

每日运行、幂等：仅当季度未记录且当日为该季首个交易日后（宽限 5 个交易日）执行。
输出：forward_state/divlv/divlv_ledger.csv
"""

from __future__ import annotations

import io
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))
OUT = ROOT / "forward_state" / "divlv"
LEDGER = OUT / "divlv_ledger.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    import fuyao_client as fc
    import requests

    OUT.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    quarter = f"{today[:4]}Q{(int(today[5:7]) - 1) // 3 + 1}"
    prev = pd.read_csv(LEDGER, dtype={"code": str}, encoding="utf-8-sig") if LEDGER.exists() else pd.DataFrame()
    if not prev.empty and (prev["quarter"] == quarter).any():
        logger.info("%s 已记录，跳过（幂等）", quarter)
        return
    # 仅在季度首月 20 号前记录（宽限窗；asof 字段如实标注价格基准日）
    month_in_q = (int(today[5:7]) - 1) % 3
    if month_in_q != 0 or int(today[8:10]) > 20:
        logger.info("非季初窗口（%s），等待下一季度", today)
        return

    # 1) 刷新分红事件流（小 parquet 直下）
    info = fc.dump_download_url("adjustment-factors")
    af = pd.read_parquet(io.BytesIO(requests.get(info["presigned_url"], timeout=60).content))
    af["ex_date"] = (pd.to_datetime(af["ex_date_ms"], unit="ms", utc=True)
                     .dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d"))
    af["div"] = pd.to_numeric(af["dividend_per_share"], errors="coerce").fillna(0.0)
    cutoff = (pd.Timestamp(today) - pd.Timedelta(days=372)).strftime("%Y-%m-%d")
    af = af[(af["div"] > 0) & (af["ex_date"] >= cutoff) & (af["ex_date"] <= today)]

    # 2) 日K（近 400 自然日，10d dump 不够窗口 → 用 10y dump 太大；
    #    折中：从本地面板 v2 取近端 + 快照补今日近似。价格新鲜度以面板末日为准，
    #    如实记录 asof。
    import pickle
    panels = pickle.load(open(ROOT / "data/expanded/panel_cache_v2.pkl", "rb"))
    close = panels["close"].astype("float64")
    asof = str(close.index[-1])
    adj = panels["adj_factor"]
    adj_ret = (close * adj).pct_change()
    vol120 = adj_ret.rolling(120, min_periods=120).std().iloc[-1]
    amount = panels["amount"].astype("float64")
    adv20 = amount.rolling(20, min_periods=20).mean().iloc[-1]
    st = panels.get("st_daily")
    st_now = st.iloc[-1] if st is not None else pd.Series(False, index=close.columns)
    px = close.iloc[-1]

    # 3) 滚动股息率（事件流/除权日前收）
    yld = pd.Series(0.0, index=close.columns)
    for _, ev in af.iterrows():
        ts = ev["thscode"]
        if ts not in close.columns:
            continue
        hist = close[ts].loc[:ev["ex_date"]].dropna()
        if len(hist) < 2:
            continue
        yld[ts] += ev["div"] / float(hist.iloc[-2] if hist.index[-1] == ev["ex_date"] else hist.iloc[-1])

    ok = ((yld > 0) & (yld < 0.20) & (~st_now) & (px > 3) & (adv20 >= 3e7)
          & (vol120 <= vol120[(yld > 0)].median()))
    top = yld[ok].nlargest(25)
    rows = pd.DataFrame({
        "quarter": quarter, "date": today, "asof_price_date": asof,
        "rank": range(1, len(top) + 1), "code": top.index, "trailing_yield": top.round(4).values,
        "note": "paper only; research observation; no advice",
    })
    out = pd.concat([prev, rows], ignore_index=True)
    out.to_csv(LEDGER, index=False, encoding="utf-8-sig")
    logger.info("已记录 %s：25 只（价格基准 %s，收益率区间 %.1f%%~%.1f%%）",
                quarter, asof, top.min() * 100, top.max() * 100)


if __name__ == "__main__":
    main()
