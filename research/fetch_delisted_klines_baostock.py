"""
fetch_delisted_klines_baostock.py - 拉取 2021 年后退市股票的完整日线

背景：data/expanded/daily_kline 只含现存股票，约 330 只退市股缺失 =
幸存者偏差（长多回测收益向上偏）。BaoStock 已验证供应退市股历史
（含退市前死亡螺旋段），用于反推各策略的幸存者偏差修正幅度。

输出：data/expanded/baostock_delisted/<code>_<market>.csv
      （date,open,high,low,close,volume,amount,isST）
断点续跑：已存在的输出文件自动跳过。
不要与其他 BaoStock 拉取脚本同时运行。
"""

from __future__ import annotations

import csv
import logging
import signal
import sys
import time
from pathlib import Path

import baostock as bs
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "data" / "expanded" / "baostock_delisted_2016"
STOCK_BASIC = ROOT_DIR / "data" / "expanded" / "stock_basic.csv"
START_DATE = "2016-01-01"
QUERY_TIMEOUT_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(OUTPUT_DIR.parent / "baostock_delisted_fetch.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class _QueryTimeout(Exception):
    """单次查询超时。"""


def _alarm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    raise _QueryTimeout("baostock query timed out")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    basic = pd.read_csv(STOCK_BASIC)
    delisted = basic[basic["delist_date"].notna()].copy()
    delisted = delisted[delisted["delist_date"] >= "2016-07-08"]
    logger.info("2021 后退市股票：%d 只", len(delisted))

    lg = bs.login()
    if lg.error_code != "0":
        logger.error("baostock login failed: %s", lg.error_msg)
        sys.exit(1)

    failures: list[dict] = []
    done = 0
    consecutive_failures = 0
    for _, row in delisted.iterrows():
        ts_code = str(row["ts_code"])
        code, market = ts_code.split(".")
        name = f"{code}_{market}.csv"
        out_path = OUTPUT_DIR / name
        if out_path.exists() and out_path.stat().st_size > 100:
            continue
        bs_code = f"{market.lower()}.{code}"
        end_date = str(row["delist_date"])
        try:
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(QUERY_TIMEOUT_SECONDS)
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code, "date,open,high,low,close,volume,amount,isST",
                    start_date=START_DATE, end_date=end_date,
                    frequency="d", adjustflag="3",
                )
                rows: list[list[str]] = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
            finally:
                signal.alarm(0)
            if rs.error_code != "0":
                raise RuntimeError(f"query error {rs.error_code}: {rs.error_msg}")
            if not rows:
                failures.append({"symbol": name, "code": bs_code, "reason": "empty_result"})
                continue
            with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["date", "open", "high", "low", "close", "volume", "amount", "isST"])
                writer.writerows(rows)
            done += 1
            consecutive_failures = 0
            if done % 50 == 0:
                logger.info("fetched %d (failed %d)", done, len(failures))
        except (_QueryTimeout, Exception) as exc:  # noqa: BLE001
            failures.append({"symbol": name, "code": bs_code, "reason": str(exc)})
            logger.warning("fetch failed %s: %s", bs_code, exc)
            consecutive_failures += 1
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
            if consecutive_failures >= 200:
                logger.error("连续失败 %d 次，中止本轮（数据源疑似不可用，稍后重跑续传）", consecutive_failures)
                break
            if consecutive_failures % 20 == 0:
                logger.warning("连续失败 %d 次，休眠 60s 后重连", consecutive_failures)
                time.sleep(60.0)
            bs.login()
            time.sleep(1.0)

    bs.logout()
    if failures:
        fail_path = OUTPUT_DIR.parent / "baostock_delisted_failures.csv"
        with open(fail_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["symbol", "code", "reason"])
            writer.writeheader()
            writer.writerows(failures)
    logger.info("DONE fetched=%d failed=%d", done, len(failures))


if __name__ == "__main__":
    main()
