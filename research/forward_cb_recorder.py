"""
forward_cb_recorder.py - 可转债双低每日前向记录（paper only）

历史复检判决（docs/cb_double_low_verdict_2026-07-12.md）：按协议存档不晋级；
用户决定不做转债（2026-07-12）。本记录器降级为**主板信用温度计观察变量**：
转债发行人多为中小盘主板公司，低价券数量与双低中位数反映其信用压力
（2023H2 低价券恐慌与小盘股压力同源）。每日仍记录双低前 15 与全表快照，
纯研究观察，不构成任何交易线。

数据源：集思录强赎表（ak.bond_cb_redeem_jsl，含现价/转股价/正股价/
剩余规模/强赎状态）；溢价率自算 = 现价 ÷ (正股价/转股价×100) - 1。
与回测口径的已知差异（如实声明）：forward 用真实强赎公告过滤（回测用
140 元代理）；上市满 10 日条件以名单出现日近似。

输出：forward_state/cb/double_low_ledger.csv（每日前15）
     forward_state/cb/redeem_snapshot/YYYY-MM-DD.csv.gz（全表快照，留档）
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "forward_state" / "cb"
LEDGER = OUT / "double_low_ledger.csv"
SNAP_DIR = OUT / "redeem_snapshot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    import akshare as ak

    OUT.mkdir(parents=True, exist_ok=True)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    prev = pd.read_csv(LEDGER, dtype={"代码": str}, encoding="utf-8-sig") if LEDGER.exists() else pd.DataFrame()
    if not prev.empty and (prev["date"] == today).any():
        logger.info("%s 已记录，跳过（幂等）", today)
        return

    d = ak.bond_cb_redeem_jsl()
    for c in ["现价", "转股价", "正股价", "规模"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["现价", "转股价", "正股价"])
    d["转股价值"] = d["正股价"] / d["转股价"] * 100
    d["溢价率pct"] = (d["现价"] / d["转股价值"] - 1) * 100
    d["双低"] = d["现价"] + d["溢价率pct"]

    # 冻结资格规则 + forward 专属的真实强赎过滤
    # venue 过滤同回测口径（11/12 开头=交易所正常挂牌；40xxxx=退市板转让代码）
    # 转股期已开始作为"上市成熟"代理（严于回测的上市满10日，口径差异已声明）
    ok = (d["代码"].astype(str).str[:2].isin(["11", "12"])
          & (d["规模"] >= 3.0) & (d["现价"] < 140)
          & (pd.to_datetime(d["转股起始日"], errors="coerce") <= pd.Timestamp(today))
          & (~d["强赎状态"].astype(str).str.contains("已公告", na=False)))
    top = d[ok].nsmallest(15, "双低")[["代码", "名称", "现价", "溢价率pct", "双低", "剩余规模", "强赎天计数", "强赎状态"]].copy()
    top.insert(0, "date", today)
    top.insert(1, "rank", range(1, len(top) + 1))
    top["recorded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    top["note"] = "paper only; no order; no tuning"
    # 主板信用温度计指标（全表口径，随行留档）
    top["cb_below_100_n"] = int((d.loc[ok, "现价"] < 100).sum())
    top["cb_dlow_median"] = round(float(d.loc[ok, "双低"].median()), 1)

    out = pd.concat([prev, top], ignore_index=True)
    out.to_csv(LEDGER, index=False, encoding="utf-8-sig")
    d.to_csv(SNAP_DIR / f"{today}.csv.gz", index=False, encoding="utf-8", compression="gzip")
    # 快照保留 60 日（全表小，留档窗口宽些）
    snaps = sorted(SNAP_DIR.glob("*.csv.gz"))
    for p in snaps[:-60]:
        p.unlink(missing_ok=True)
    logger.info("已记录 %s: 双低前15（最低 %.1f / 中位 %.1f），全表 %d 只入快照",
                today, top["双低"].min(), top["双低"].median(), len(d))


if __name__ == "__main__":
    main()
