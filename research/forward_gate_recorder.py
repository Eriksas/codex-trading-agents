"""
forward_gate_recorder.py - 执行框架 forward 观察记录器（G2/G4，paper only）

每交易日收盘后运行一次：从 BaoStock 拉六大指数日线，计算
G2（多尺度趋势投票）与 G4（回撤阶梯）的当日目标暴露，追加留痕。
这是框架修复轮（docs/framework_repair_2026-07.md）的前向检验：
只记录、不下单、不调参；样本不足 120 个交易日不做任何评估。

用法：python3 research/forward_gate_recorder.py
输出：output/forward_gate_recorder/gate_states.csv
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "research"))

OUTPUT_DIR = ROOT_DIR / "output" / "forward_gate_recorder"
LEDGER = OUTPUT_DIR / "gate_states.csv"
INDEX_SET = {"000001.SH": "sh.000001", "399001.SZ": "sz.399001", "399006.SZ": "sz.399006",
             "000300.SH": "sh.000300", "000905.SH": "sh.000905", "000852.SH": "sh.000852"}
BENCH = "000300.SH"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _fetch_index_history(days: int = 800) -> dict[str, pd.Series]:
    """BaoStock 拉六大指数近 days 自然日收盘。"""
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    out: dict[str, pd.Series] = {}
    try:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        for ts, code in INDEX_SET.items():
            rs = bs.query_history_k_data_plus(code, "date,close", start_date=start,
                                              end_date=end, frequency="d", adjustflag="3")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rs.error_code != "0":
                raise RuntimeError(f"{code} query error {rs.error_code}")
            s = pd.DataFrame(rows, columns=["date", "close"]).set_index("date")["close"]
            out[ts] = pd.to_numeric(s, errors="coerce").dropna()
    finally:
        bs.logout()
    return out


def _g2_trend_vote(closes: dict[str, pd.Series]) -> float:
    """G2 当日目标暴露（36 票，无缓冲——缓冲属于执行层，记录层存原始值）。"""
    votes = []
    for ts, s in closes.items():
        c = s.to_numpy()
        if len(c) < 260:
            continue
        v = []
        for n in (20, 60, 120, 200):
            v.append(1.0 if c[-1] > np.mean(c[-n:]) else 0.0)
        for entry_n, exit_n in ((20, 10), (55, 20)):
            # 状态机重放（用近 400 日重建当前状态）
            cur = 0.0
            for i in range(len(c) - 300, len(c)):
                hi = np.max(c[i - entry_n:i]) if i >= entry_n else np.inf
                lo = np.min(c[i - exit_n:i]) if i >= exit_n else -np.inf
                if c[i] >= hi:
                    cur = 1.0
                elif c[i] <= lo:
                    cur = 0.0
            v.append(cur)
        votes.append(float(np.mean(v)))
    return float(np.mean(votes)) if votes else float("nan")


def _g4_dd_ladder(closes: dict[str, pd.Series]) -> float:
    """G4 当日档位（重放近 500 日状态机）。"""
    c = closes[BENCH].to_numpy()
    if len(c) < 300:
        return float("nan")
    r = np.diff(np.log(c), prepend=np.nan)
    lam = 0.94
    var = np.nanvar(r[1:61])
    sig = np.zeros(len(c))
    for i in range(len(c)):
        if np.isfinite(r[i]):
            var = lam * var + (1 - lam) * r[i] ** 2
        sig[i] = np.sqrt(252 * var)
    LEVELS = [1.0, 0.6, 0.3, 0.0]
    cur, slow_days, fast_days = 3, 0, 0
    for i in range(max(252, len(c) - 500), len(c)):
        peak = np.max(c[max(0, i - 251):i + 1])
        dd = 1 - c[i] / peak
        ret21 = c[i] / c[i - 21] - 1 if i >= 21 else np.nan
        z21 = ret21 / (sig[i] * np.sqrt(21 / 252)) if np.isfinite(ret21) and sig[i] > 0 else np.nan
        base = 0 if dd < 0.10 else 1 if dd < 0.20 else 2 if dd < 0.30 else 3
        if np.isfinite(z21) and z21 < -1.5:
            base = min(3, base + 1)
        if base > cur:
            cur, slow_days, fast_days = base, 0, 0
        elif base < cur:
            thresh = [0.10, 0.20, 0.30, np.inf][cur - 1] - 0.02 if cur >= 1 else 0.0
            slow_days = slow_days + 1 if dd < thresh else 0
            if slow_days >= 10:
                cur, slow_days = cur - 1, 0
            fast_days = fast_days + 1 if (np.isfinite(z21) and z21 > 2.0) else 0
            if fast_days >= 5 and cur > 1:
                cur, fast_days = cur - 1, 0
        else:
            slow_days = fast_days = 0
    return LEVELS[cur]


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    closes = _fetch_index_history()
    asof = str(max(s.index.max() for s in closes.values()))
    if LEDGER.exists():
        prev = pd.read_csv(LEDGER, encoding="utf-8-sig")
        if (prev["date"] == asof).any():
            logger.info("%s 已记录，跳过（幂等）", asof)
            return
    row = {
        "date": asof,
        "g2_trend_vote": round(_g2_trend_vote(closes), 4),
        "g4_dd_ladder": round(_g4_dd_ladder(closes), 4),
        "bench_close": float(closes[BENCH].iloc[-1]),
        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "paper only; no order; no tuning",
    }
    header = not LEDGER.exists()
    pd.DataFrame([row]).to_csv(LEDGER, mode="a", header=header, index=False, encoding="utf-8-sig")
    logger.info("已记录 %s: G2=%.3f G4=%.3f", asof, row["g2_trend_vote"], row["g4_dd_ladder"])


if __name__ == "__main__":
    main()
