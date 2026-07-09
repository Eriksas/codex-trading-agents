"""
forward_thermometer_recorder.py - 涨停生态温度计每日前向记录（paper only）

历史研究（research/thermometer_study.py，2016-2026 面板自建）结论：
温度计与前瞻指数收益仅有微弱非线性关联（极端连板→小盘转弱、跌停潮→反弹），
不构成规则；本记录器为 forward 证据积累。

数据源：fuyao 快照端点（已实锤仅当日有效，date 参数无效——正好只用于当日记录）。
输出：forward_state/thermometer/thermometer_ledger.csv
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "research"))
import fuyao_client as fc

OUTPUT_DIR = ROOT_DIR / "forward_state" / "thermometer"
LEDGER = OUTPUT_DIR / "thermometer_ledger.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _pool_all_pages(date: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        data = fc.get("/api/a-share/special-data/limit-up-pool", {"date": date, "page": page})
        batch = data.get("item") or []
        items += batch
        pg = data.get("pagination") or {}
        if page >= int(pg.get("pages") or 1):
            break
        page += 1
    return items


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    ladder = fc.limit_up_ladder(today)
    ladder_items = ladder.get("item") or []
    if not ladder_items:
        logger.info("无天梯数据，跳过")
        return
    latest = max(x["date"] for x in ladder_items)   # 快照实际日期（端点无视入参）
    prev = pd.read_csv(LEDGER, encoding="utf-8-sig") if LEDGER.exists() else pd.DataFrame()
    if not prev.empty and (prev["date"] == latest).any():
        logger.info("%s 已记录，跳过（幂等）", latest)
        return
    pool = _pool_all_pages(latest)
    cnts = [int(x.get("continue_day_cnt") or 1) for x in pool]
    seals = [float(x.get("seal_money") or 0) for x in pool]
    row = {
        "date": latest,
        "limit_up_pool_n": len(pool),
        "max_streak": max(cnts) if cnts else 0,
        "streak2_n": sum(1 for c in cnts if c >= 2),
        "seal_money_sum": round(sum(seals) / 1e8, 2),      # 亿元
        "late_seal_n": sum(1 for x in pool if str(x.get("limit_up_time") or "") >= "14:00"),
        "new_in_pool": sum(1 for x in pool if x.get("is_new")),
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "paper only; snapshot endpoint (date param inert)",
    }
    out = pd.concat([prev, pd.DataFrame([row])], ignore_index=True)
    out.to_csv(LEDGER, index=False, encoding="utf-8-sig")
    logger.info("已记录 %s: 涨停池=%d 最高连板=%d 二板以上=%d 封单%.1f亿", latest,
                row["limit_up_pool_n"], row["max_streak"], row["streak2_n"], row["seal_money_sum"])


if __name__ == "__main__":
    main()
