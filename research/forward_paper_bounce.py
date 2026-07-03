"""
forward_paper_bounce.py - idx1000_bounce_h5 前向模拟记录器（shadow candidate）

策略规则（冻结，来自 2026-07 搜索轮，见 docs/strategy_search_2026-07_report.md）：
  触发（T 日收盘判定，中证1000）：ret5 <= -5% 且 当日收阳 且 ret60 > -25%，冷却 5 个交易日；
  选股：ADV20 全市场排名 300-1500 区间、非 ST、收盘价 >= 3 元，取 ADV20 最大的 10 只；
  执行：T+1 开盘买入（模拟），持有 5 个交易日后开盘卖出；双边成本 30bp。

三种模式（本地每日收盘后运行 update + check；有持仓时运行 settle）：
  update  拉取全市场快照，把当日成交额追加进滚动库（ADV20 的数据基础）
  check   用 BaoStock 指数判定触发；触发则记录 10 只候选到信号台账
  settle  为到期（entry 后 5 个交易日）的持仓补录开盘卖出价与净收益

纪律：不满 20 次事件不做任何统计结论与调参；本脚本只记录、不下单、不连接实盘。
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "output" / "forward_paper_bounce"
AMOUNTS_STORE = OUTPUT_DIR / "daily_amounts_store.csv"     # date, code, name, amount, close
SIGNALS_LEDGER = OUTPUT_DIR / "signals_ledger.csv"
EVENTS_LOG = OUTPUT_DIR / "events_log.csv"
COST_BPS = 30.0
HOLD_TRADING_DAYS = 5
COOLDOWN_TRADING_DAYS = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")


def _append_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = not path.exists()
    df.to_csv(path, mode="a", header=header, index=False, encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# update：滚动积累全市场成交额
# ---------------------------------------------------------------------------


def _fetch_snapshot() -> pd.DataFrame:
    """全市场快照：efinance 主源，akshare 备用；均失败则明确报错（不编造）。"""
    try:
        import efinance as ef

        snap = ef.stock.get_realtime_quotes()
        if snap is not None and not snap.empty:
            return snap
    except Exception as exc:  # noqa: BLE001
        logger.warning("efinance 快照失败：%s，尝试 akshare", exc)
    import akshare as ak

    snap = ak.stock_zh_a_spot_em()
    if snap is None or snap.empty:
        raise RuntimeError("efinance 与 akshare 快照均失败；今日不入库（禁止编造数据）")
    return snap


def cmd_update() -> None:
    """拉取当日全市场快照，把成交额追加进滚动库（每交易日收盘后运行一次）。"""
    snap = _fetch_snapshot()
    col_map = {}
    for want, cands in {
        "code": ["股票代码", "代码"],
        "name": ["股票名称", "名称"],
        "amount": ["成交额"],
        "close": ["最新价"],
    }.items():
        for c in cands:
            if c in snap.columns:
                col_map[c] = want
                break
    df = snap.rename(columns=col_map)[["code", "name", "amount", "close"]].copy()
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["amount"])
    df = df[df["amount"] > 0]
    today = datetime.now().strftime("%Y-%m-%d")
    store = _read_csv(AMOUNTS_STORE)
    if not store.empty and (store["date"] == today).any():
        logger.info("今日 %s 已入库，跳过（幂等）", today)
        return
    df.insert(0, "date", today)
    _append_csv(AMOUNTS_STORE, df)
    logger.info("入库 %s：%d 只（滚动库现有 %d 个交易日）", today, len(df),
                store["date"].nunique() + 1 if not store.empty else 1)


# ---------------------------------------------------------------------------
# check：触发判定与信号记录
# ---------------------------------------------------------------------------


def _index_recent_closes(n: int = 90) -> pd.DataFrame:
    """BaoStock 拉取中证1000 近 n 个交易日收盘。"""
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            "sh.000852", "date,close",
            start_date=(datetime.now() - pd.Timedelta(days=n * 2)).strftime("%Y-%m-%d"),
            end_date=datetime.now().strftime("%Y-%m-%d"),
            frequency="d", adjustflag="3",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()
    df = pd.DataFrame(rows, columns=["date", "close"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna().reset_index(drop=True)


def _trigger_today(idx: pd.DataFrame) -> tuple[bool, dict[str, Any]]:
    """按冻结规则判定最新交易日是否触发。"""
    c = idx["close"]
    if len(c) < 61:
        return False, {"reason": "index_history_insufficient", "rows": len(c)}
    ret5 = float(c.iloc[-1] / c.iloc[-6] - 1)
    ret1 = float(c.iloc[-1] / c.iloc[-2] - 1)
    ret60 = float(c.iloc[-1] / c.iloc[-61] - 1)
    detail = {"date": str(idx["date"].iloc[-1]), "ret5": round(ret5, 4),
              "ret1": round(ret1, 4), "ret60": round(ret60, 4)}
    fired = (ret5 <= -0.05) and (ret1 > 0) and (ret60 > -0.25)
    return fired, detail


def _cooldown_active(signal_date: str) -> bool:
    """距上次触发不足 COOLDOWN_TRADING_DAYS 个交易日则冷却。"""
    ledger = _read_csv(SIGNALS_LEDGER)
    if ledger.empty:
        return False
    last = str(ledger["signal_date"].max())
    store = _read_csv(AMOUNTS_STORE)
    trading_days = sorted(store["date"].unique()) if not store.empty else []
    if last in trading_days and signal_date in trading_days:
        gap = trading_days.index(signal_date) - trading_days.index(last)
        return gap <= COOLDOWN_TRADING_DAYS
    # 滚动库缺日期时保守按自然日 7 天冷却
    gap_days = (datetime.strptime(signal_date, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
    return gap_days <= 7


def _pick_candidates(signal_date: str) -> pd.DataFrame:
    """按 ADV20 排名 300-1500 区间选 10 只（非 ST、价格>=3）。"""
    store = _read_csv(AMOUNTS_STORE)
    if store.empty:
        raise RuntimeError("成交额滚动库为空：请先每日运行 update 模式积累至少 20 个交易日。")
    days = sorted(store["date"].unique())
    window = days[-20:]
    if len(window) < 20:
        logger.warning("滚动库仅 %d 个交易日（<20），ADV 用现有窗口近似并在台账标注", len(window))
    sub = store[store["date"].isin(window)]
    adv = sub.groupby(["code", "name"], as_index=False).agg(
        adv=("amount", "mean"), days=("amount", "count"), last_close=("close", "last"))
    adv = adv[adv["days"] >= max(10, len(window) // 2)]
    adv = adv[~adv["name"].str.contains("ST", case=False, na=False)]  # 硬规则：ST 一概不碰
    adv = adv[adv["last_close"] >= 3.0]
    adv = adv.sort_values("adv", ascending=False).reset_index(drop=True)
    band = adv.iloc[300:1500]
    picks = band.nlargest(10, "adv").copy()
    picks.insert(0, "signal_date", signal_date)
    picks["adv_window_days"] = len(window)
    return picks


def cmd_check() -> None:
    """判定今日是否触发；触发则记录候选（模拟次日开盘买入）。"""
    idx = _index_recent_closes()
    fired, detail = _trigger_today(idx)
    detail["fired"] = fired
    detail["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    signal_date = detail.get("date", "")
    if fired and _cooldown_active(signal_date):
        fired = False
        detail["fired"] = False
        detail["suppressed_by_cooldown"] = True
    _append_csv(EVENTS_LOG, pd.DataFrame([detail]))
    if not fired:
        logger.info("未触发：%s", json.dumps(detail, ensure_ascii=False))
        return
    picks = _pick_candidates(signal_date)
    picks["status"] = "pending_entry_next_open"
    picks["entry_date"] = ""
    picks["entry_open"] = ""
    picks["exit_date"] = ""
    picks["exit_open"] = ""
    picks["net_return"] = ""
    picks["note"] = "personal simulation only; no live order"
    _append_csv(SIGNALS_LEDGER, picks)
    logger.info("触发！已记录 %d 只候选（signal_date=%s）。次日收盘后运行 settle 补进场价。", len(picks), signal_date)


# ---------------------------------------------------------------------------
# settle：补录进场/出场价
# ---------------------------------------------------------------------------


def _fetch_opens(code: str, start: str) -> pd.DataFrame:
    """efinance 拉取单票日线（开盘价）。"""
    import efinance as ef

    df = ef.stock.get_quote_history(code, beg=start.replace("-", ""), fqt=0)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"日期": "date", "开盘": "open"})
    return df[["date", "open"]]


def cmd_settle() -> None:
    """为台账中未完成的行补录进场价与到期出场价。"""
    ledger = _read_csv(SIGNALS_LEDGER)
    if ledger.empty:
        logger.info("台账为空")
        return
    changed = False
    for i, row in ledger.iterrows():
        if str(row.get("net_return")) not in ("", "nan"):
            continue
        bars = _fetch_opens(str(row["code"]), str(row["signal_date"]))
        if bars.empty:
            logger.warning("无法获取 %s 行情，跳过", row["code"])
            continue
        after = bars[bars["date"] > str(row["signal_date"])].reset_index(drop=True)
        if after.empty:
            continue
        entry_open = float(after["open"].iloc[0])
        ledger.at[i, "entry_date"] = after["date"].iloc[0]
        ledger.at[i, "entry_open"] = entry_open
        if len(after) > HOLD_TRADING_DAYS:
            exit_open = float(after["open"].iloc[HOLD_TRADING_DAYS])
            ledger.at[i, "exit_date"] = after["date"].iloc[HOLD_TRADING_DAYS]
            ledger.at[i, "exit_open"] = exit_open
            ledger.at[i, "net_return"] = round(exit_open / entry_open - 1 - COST_BPS / 10000, 5)
            ledger.at[i, "status"] = "closed"
        else:
            ledger.at[i, "status"] = "holding"
        changed = True
    if changed:
        ledger.to_csv(SIGNALS_LEDGER, index=False, encoding="utf-8-sig")
        closed = ledger[ledger["status"] == "closed"]
        n_events = closed["signal_date"].nunique() if not closed.empty else 0
        logger.info("台账已更新：closed=%d 笔 / %d 次事件（满 20 次事件前不做结论）", len(closed), n_events)
    else:
        logger.info("无待结算行")


def main() -> None:
    parser = argparse.ArgumentParser(description="idx1000_bounce_h5 forward paper 记录器")
    parser.add_argument("mode", choices=["update", "check", "settle"])
    args = parser.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    {"update": cmd_update, "check": cmd_check, "settle": cmd_settle}[args.mode]()


if __name__ == "__main__":
    main()
