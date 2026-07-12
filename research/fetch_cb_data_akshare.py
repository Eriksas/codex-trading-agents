"""
fetch_cb_data_akshare.py - 可转债估值序列全量拉取（双低轮动立项的数据层）

尽调结论（2026-07-10）：bond_zh_cov 列表 1,033 只含退市券；
bond_zh_cov_value_analysis 提供退市券完整生命周期（收盘价/纯债价值/
转股价值/纯债溢价率/转股溢价率）；与新浪日线交叉对账 99.7% 吻合。

输出：data/expanded/cb/cb_list.csv + data/expanded/cb/valuation/<code>.csv
断点续跑：已存在文件自动跳过。失败记录 cb_fetch_failures.csv，不编造。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import akshare as ak
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT_DIR / "data" / "expanded" / "cb"
VAL_DIR = OUT_DIR / "valuation"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(OUT_DIR.parent / "cb_fetch.log", encoding="utf-8"),
                              logging.StreamHandler()])
logger = logging.getLogger(__name__)


def main() -> None:
    VAL_DIR.mkdir(parents=True, exist_ok=True)
    cov = ak.bond_zh_cov()
    cov.to_csv(OUT_DIR / "cb_list.csv", index=False, encoding="utf-8-sig")
    codes = cov["债券代码"].astype(str).tolist()
    logger.info("转债列表 %d 只", len(codes))

    failures: list[dict] = []
    done = skipped = 0
    consecutive = 0
    for code in codes:
        out = VAL_DIR / f"{code}.csv"
        if out.exists() and out.stat().st_size > 200:
            skipped += 1
            continue
        try:
            va = ak.bond_zh_cov_value_analysis(symbol=code)
            if va is None or va.empty:
                failures.append({"code": code, "reason": "empty"})
                continue
            va.to_csv(out, index=False, encoding="utf-8-sig")
            done += 1
            consecutive = 0
            if done % 100 == 0:
                logger.info("fetched %d (skipped %d, failed %d)", done, skipped, len(failures))
            time.sleep(0.25)
        except Exception as exc:  # noqa: BLE001
            failures.append({"code": code, "reason": str(exc)[:120]})
            consecutive += 1
            if consecutive >= 100:
                logger.error("连续失败 %d，中止（稍后续跑）", consecutive)
                break
            if consecutive % 15 == 0:
                logger.warning("连续失败 %d，休眠 60s", consecutive)
                time.sleep(60)
            time.sleep(1.0)

    if failures:
        pd.DataFrame(failures).to_csv(OUT_DIR / "cb_fetch_failures.csv", index=False, encoding="utf-8-sig")
    logger.info("DONE fetched=%d skipped=%d failed=%d", done, skipped, len(failures))


if __name__ == "__main__":
    main()
