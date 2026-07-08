"""
fetch_st_status_baostock.py - 拉取全部股票的逐日 ST 状态（时变）

背景：stock_basic.csv 的 is_st 是静态快照，无法反映历史戴帽/摘帽。
项目硬规则（AGENTS.md）：ST/*ST 一概不碰，回测需按当日真实 ST 状态剔除。
BaoStock query_history_k_data_plus 提供 isST 日频字段。

输出：data/expanded/baostock_st/<code>_<market>.csv（date,isST）
断点续跑：已存在的输出文件自动跳过。与 fetch_adj_factors_baostock.py
不要同时运行（共用 BaoStock 连接配额）。
"""

from __future__ import annotations

import csv
import logging
import signal
import sys
import time
from pathlib import Path

import baostock as bs

ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "data" / "expanded" / "baostock_st_2016"
KLINE_DIR = ROOT_DIR / "data" / "expanded" / "daily_kline"
START_DATE = "2016-01-01"
END_DATE = "2026-06-30"
QUERY_TIMEOUT_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(OUTPUT_DIR.parent / "baostock_st_fetch.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class _QueryTimeout(Exception):
    """单次查询超时。"""


def _alarm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    raise _QueryTimeout("baostock query timed out")


def _bs_code(cache_name: str) -> str:
    stem = cache_name.replace(".csv", "")
    code, market = stem.rsplit("_", 1)
    return f"{market.lower()}.{code}"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    symbols = sorted(p.name for p in KLINE_DIR.glob("*.csv"))
    logger.info("total symbols: %d", len(symbols))
    lg = bs.login()
    if lg.error_code != "0":
        logger.error("baostock login failed: %s", lg.error_msg)
        sys.exit(1)

    failures: list[dict] = []
    done = 0
    consecutive_failures = 0
    skipped = 0
    for name in symbols:
        out_path = OUTPUT_DIR / name
        if out_path.exists() and out_path.stat().st_size > 50:
            skipped += 1
            continue
        code = _bs_code(name)
        try:
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(QUERY_TIMEOUT_SECONDS)
            try:
                rs = bs.query_history_k_data_plus(
                    code, "date,isST",
                    start_date=START_DATE, end_date=END_DATE,
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
                failures.append({"symbol": name, "code": code, "reason": "empty_result"})
                continue
            with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["date", "isST"])
                writer.writerows(rows)
            done += 1
            consecutive_failures = 0
            if done % 200 == 0:
                logger.info("fetched %d (skipped %d, failed %d)", done, skipped, len(failures))
        except (_QueryTimeout, Exception) as exc:  # noqa: BLE001
            failures.append({"symbol": name, "code": code, "reason": str(exc)})
            logger.warning("fetch failed %s: %s", code, exc)
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
        fail_path = OUTPUT_DIR.parent / "baostock_st_fetch_failures.csv"
        with open(fail_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["symbol", "code", "reason"])
            writer.writeheader()
            writer.writerows(failures)
    logger.info("DONE fetched=%d skipped=%d failed=%d", done, skipped, len(failures))


if __name__ == "__main__":
    main()
