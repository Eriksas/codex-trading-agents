"""
fetch_delisted_dividends_baostock.py - 补拉退市股分红史（红利策略幸存者偏差量化）

背景：fuyao 分红事件流对退市股覆盖率 0%（docs/dividend_lowvol_verdict_2026-07-10.md
待办项）。BaoStock query_dividend_data 按股票+年度查询，用于量化
「高息后暴雷」型标的对红利策略回测的污染幅度。

输出：data/expanded/baostock_delisted_dividends.csv（长表）
不要与其他 BaoStock 拉取脚本同时运行。
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path

import baostock as bs
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "expanded" / "baostock_delisted_dividends.csv"
QUERY_TIMEOUT_SECONDS = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class _QueryTimeout(Exception):
    """单次查询超时。"""


def _alarm(signum: int, frame: object) -> None:  # noqa: ARG001
    raise _QueryTimeout("timeout")


def main() -> None:
    basic = pd.read_csv(ROOT / "data/expanded/stock_basic.csv")
    dl = basic[(basic["delist_date"].notna()) & (basic["delist_date"] >= "2016-07-08")]
    logger.info("退市股 %d 只 × 年度查询", len(dl))
    bs.login()
    rows: list[dict] = []
    fails = 0
    consecutive = 0
    for _, r in dl.iterrows():
        code, mkt = str(r["ts_code"]).split(".")
        bs_code = f"{mkt.lower()}.{code}"
        y0 = max(2016, int(str(r.get("list_date") or "2016")[:4]))
        y1 = int(str(r["delist_date"])[:4])
        for year in range(y0, y1 + 1):
            try:
                signal.signal(signal.SIGALRM, _alarm)
                signal.alarm(QUERY_TIMEOUT_SECONDS)
                try:
                    rs = bs.query_dividend_data(code=bs_code, year=str(year), yearType="operate")
                    while rs.error_code == "0" and rs.next():
                        d = dict(zip(rs.fields, rs.get_row_data()))
                        d["ts_code"] = r["ts_code"]
                        rows.append(d)
                finally:
                    signal.alarm(0)
                consecutive = 0
            except Exception as exc:  # noqa: BLE001
                fails += 1
                consecutive += 1
                try:
                    bs.logout()
                except Exception:  # noqa: BLE001
                    pass
                if consecutive >= 100:
                    logger.error("连续失败 %d，中止", consecutive)
                    break
                if consecutive % 20 == 0:
                    time.sleep(60)
                bs.login()
                time.sleep(0.5)
        if consecutive >= 100:
            break
    bs.logout()
    pd.DataFrame(rows).to_csv(OUT, index=False, encoding="utf-8-sig")
    logger.info("DONE 分红记录 %d 条, 失败 %d 次 -> %s", len(rows), fails, OUT)


if __name__ == "__main__":
    main()
