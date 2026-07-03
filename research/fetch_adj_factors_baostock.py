"""
fetch_adj_factors_baostock.py - 用 BaoStock 后复权收盘价重建复权因子

背景：data/expanded/daily_kline.csv 为不复权价（adj_kline.csv 标注 unadjusted），
分红送转会体现为假下跌。本脚本拉取全部 SH/SZ 股票的后复权收盘价，
供回测器计算真实复权收益（adj_factor = hfq_close / raw_close）。

失败处理：任一标的拉取失败记入 fetch_failures.csv，不中断、不填充。
支持断点续跑：已存在的输出文件自动跳过。
"""

from __future__ import annotations

import csv
import logging
import signal
import sys
import time
from pathlib import Path

import baostock as bs

QUERY_TIMEOUT_SECONDS = 60


class _QueryTimeout(Exception):
    """单次查询超时。"""


def _alarm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    raise _QueryTimeout("baostock query timed out")

ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "data" / "expanded" / "baostock_hfq"
KLINE_DIR = ROOT_DIR / "data" / "expanded" / "daily_kline"
START_DATE = "2021-01-01"
END_DATE = "2026-06-30"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(OUTPUT_DIR.parent / "baostock_fetch.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def _bs_code(cache_name: str) -> str:
    """000001_SZ.csv -> sz.000001"""
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
    skipped = 0
    consecutive_failures = 0
    for name in symbols:
        out_path = OUTPUT_DIR / name
        if out_path.exists() and out_path.stat().st_size > 100:
            skipped += 1
            continue
        code = _bs_code(name)
        try:
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(QUERY_TIMEOUT_SECONDS)
            try:
                rs = bs.query_history_k_data_plus(
                    code,
                    "date,close",
                    start_date=START_DATE,
                    end_date=END_DATE,
                    frequency="d",
                    adjustflag="1",  # 后复权
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
                logger.warning("empty result: %s", code)
                continue
            with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["date", "hfq_close"])
                writer.writerows(rows)
            done += 1
            consecutive_failures = 0
            if done % 200 == 0:
                logger.info("fetched %d (skipped %d, failed %d)", done, skipped, len(failures))
        except (_QueryTimeout, Exception) as exc:  # noqa: BLE001 - 记录后继续，不中断批量拉取
            failures.append({"symbol": name, "code": code, "reason": str(exc)})
            logger.warning("fetch failed %s: %s", code, exc)
            consecutive_failures += 1
            # 任何失败都视为连接状态可疑：重建会话（10002007 网络接收错误等
            # 一旦出现会级联到后续所有请求）
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
        fail_path = OUTPUT_DIR.parent / "baostock_fetch_failures.csv"
        with open(fail_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["symbol", "code", "reason"])
            writer.writeheader()
            writer.writerows(failures)
        logger.info("failures written: %s (%d)", fail_path, len(failures))
    logger.info("DONE fetched=%d skipped=%d failed=%d", done, skipped, len(failures))


if __name__ == "__main__":
    main()
