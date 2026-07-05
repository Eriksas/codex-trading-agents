"""
test_panel_backtester.py - 回测引擎手工算例验证

用 4 只虚构股票 × 14 天的已知价格路径，逐笔核对：
  T1 进出场时序与成本（信号日t → t+1开盘进 → t+1+H开盘出）
  T2 开盘涨停禁买
  T3 开盘跌停卖出顺延
  T4 除权日中性化（-40% 假暴跌不进收益）
  T5 平价路径下净收益恰为 -成本
全部断言通过输出 ALL-TESTS-PASSED。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel_backtester as pb

pb.MIN_HISTORY_DAYS = 1
pb.MIN_AMOUNT = 0.0
pb.BAOSTOCK_HFQ_DIR = Path("/nonexistent_hfq_dir")

DATES = [f"2025-01-{d:02d}" for d in range(1, 15)]
SYMS = ["000010.SZ", "000020.SZ", "000030.SZ", "300010.SZ"]


def _mk_panels() -> dict:
    """构造已知价格路径。"""
    n = len(DATES)
    close = pd.DataFrame(index=pd.Index(DATES), columns=SYMS, dtype=float)
    open_ = close.copy()

    # A 000010.SZ：温和上行，开盘=前收*1.005，收盘逐日+1
    a_close = [100 + i for i in range(n)]
    close["000010.SZ"] = a_close
    open_["000010.SZ"] = [a_close[0]] + [round(a_close[i - 1] * 1.005, 4) for i in range(1, n)]

    # B 000020.SZ：第3行开盘 +9.6%（近涨停，禁买），其余平稳
    b_close = [50.0] * n
    close["000020.SZ"] = b_close
    b_open = [50.0] * n
    b_open[3] = round(50.0 * 1.096, 4)
    open_["000020.SZ"] = b_open

    # C 000030.SZ：第5行开盘 -9.6%（近跌停，卖出顺延），价格路径已知
    c_close = [80.0, 80, 80, 80, 80, 73.0, 74.0, 75.0, 75, 75, 75, 75, 75, 75]
    close["000030.SZ"] = c_close
    c_open = [80.0] * n
    c_open[5] = round(80.0 * 0.904, 4)   # -9.6% 开盘
    c_open[6] = 73.5
    open_["000030.SZ"] = c_open

    # D 300010.SZ（20%板）：第4行收盘 -40%（除权），此后按新价基
    d_close = [100.0, 100, 100, 100, 60.0, 63.0, 64.0, 65.0, 65, 65, 65, 65, 65, 65]
    close["300010.SZ"] = d_close
    d_open = [100.0, 100, 100, 100, 60.0, 61.0, 63.5, 64.5, 65, 65, 65, 65, 65, 65]
    open_["300010.SZ"] = d_open

    vol = pd.DataFrame(1e6, index=close.index, columns=close.columns)
    amount = pd.DataFrame(1e9, index=close.index, columns=close.columns)
    turnover = pd.DataFrame(1.0, index=close.index, columns=close.columns)
    panels = {
        "close": close, "open": open_, "high": close * 1.01, "low": close * 0.99,
        "vol": vol, "amount": amount, "turnover": turnover,
        "limit_pct": pd.Series([pb._board_limit(s) for s in SYMS], index=SYMS, dtype="float32"),
        "st_mask": pd.Series(False, index=SYMS),
        "index_close": pd.DataFrame({"000300.SH": 4000.0}, index=close.index),
    }
    adj, source = pb._build_adj_factor(panels)
    panels["adj_factor"] = adj
    panels["adj_source"] = source
    return panels


def _sig_only(market: pb.Market, sym: str, row: int) -> np.ndarray:
    sig = np.full((len(DATES), len(SYMS)), np.nan)
    sig[row, SYMS.index(sym)] = 1.0
    return sig


def main() -> None:
    panels = _mk_panels()
    market = pb.build_market(panels)
    cost = 30.0

    # T1 时序与成本：A 信号在行2 → 行3开盘买入(100.5*1.005? 见fixture) → 行5开盘卖出
    res = pb.run_strategy(market, _sig_only(market, "000010.SZ", 2), name="t1",
                          start=DATES[0], end=DATES[-1], top_k=1, hold_days=2,
                          n_tranches=1, cost_bps=cost)
    tr = res.trades[0]
    a_open = panels["open"]["000010.SZ"]
    expect = a_open.iloc[5] / a_open.iloc[3] - 1 - cost / 10000
    assert tr.entry_date == DATES[3] and tr.exit_date == DATES[5], (tr.entry_date, tr.exit_date)
    assert abs(tr.net_return - expect) < 1e-9, (tr.net_return, expect)
    print("T1 entry/exit/cost OK", round(expect, 6))

    # T2 涨停禁买：行2 信号 B 分数最高、A 次之，top_k=1 → B 开盘近涨停被剔除，成交 A
    sig = np.full((len(DATES), len(SYMS)), np.nan)
    sig[2, SYMS.index("000020.SZ")] = 2.0
    sig[2, SYMS.index("000010.SZ")] = 1.0
    res = pb.run_strategy(market, sig, name="t2", start=DATES[0], end=DATES[-1],
                          top_k=1, hold_days=2, n_tranches=1, cost_bps=cost)
    assert len(res.trades) == 1 and res.trades[0].symbol == "000010.SZ", [t.symbol for t in res.trades]
    print("T2 limit-up entry block OK")

    # T3 跌停顺延：C 信号行2 → 行3买入 → 应行5卖出，但行5开盘-9.6%顺延 → 行6卖出
    res = pb.run_strategy(market, _sig_only(market, "000030.SZ", 2), name="t3",
                          start=DATES[0], end=DATES[-1], top_k=1, hold_days=2,
                          n_tranches=1, cost_bps=cost)
    tr = res.trades[0]
    assert tr.exit_date == DATES[6] and tr.deferred_exit_days == 1, (tr.exit_date, tr.deferred_exit_days)
    expect = 73.5 / panels["open"]["000030.SZ"].iloc[3] - 1 - cost / 10000
    assert abs(tr.net_return - expect) < 1e-9
    print("T3 limit-down exit deferral OK", round(expect, 6))

    # T4 除权中性化：D 行2信号 → 行3买入(开盘100) → hold=4 → 行7开盘卖出
    # 复权因子行4起 = 100/60；调整后行7开盘 = 64.5*100/60 = 107.5 → 毛收益 +7.5%
    res = pb.run_strategy(market, _sig_only(market, "300010.SZ", 2), name="t4",
                          start=DATES[0], end=DATES[-1], top_k=1, hold_days=4,
                          n_tranches=1, cost_bps=cost)
    tr = res.trades[0]
    factor = 100.0 / 60.0
    expect = (64.5 * factor) / 100.0 - 1 - cost / 10000
    assert abs(tr.net_return - expect) < 1e-9, (tr.net_return, expect)
    assert tr.corp_action_days == 1
    raw_fake = 64.5 / 100.0 - 1
    print(f"T4 corp-action neutralization OK: net={tr.net_return:.4f} (raw would be {raw_fake:.4f})")

    # T5 平价路径：B（除行3外恒定50）→ 信号行6，行7买入50，行9卖出50 → 净收益 = -成本
    res = pb.run_strategy(market, _sig_only(market, "000020.SZ", 6), name="t5",
                          start=DATES[0], end=DATES[-1], top_k=1, hold_days=2,
                          n_tranches=1, cost_bps=cost)
    tr = res.trades[0]
    assert abs(tr.net_return - (-cost / 10000)) < 1e-9, tr.net_return
    print("T5 flat-path net == -cost OK")

    # T6 除权日禁买：D 行3 信号（行4=除权日不能买）→ 应无交易或顺延到其它日
    res = pb.run_strategy(market, _sig_only(market, "300010.SZ", 3), name="t6",
                          start=DATES[0], end=DATES[-1], top_k=1, hold_days=2,
                          n_tranches=1, cost_bps=cost)
    assert len(res.trades) == 0, res.trades
    print("T6 corp-action-day entry block OK")

    # T7 半仓数学：目标暴露维持 0.5（水平序列语义），A 信号行2 → 净值变化 = 0.5×单笔净收益
    expo = np.full(len(DATES), 0.5)
    res = pb.run_strategy(market, _sig_only(market, "000010.SZ", 2), name="t7",
                          start=DATES[0], end=DATES[-1], top_k=1, hold_days=2,
                          n_tranches=1, cost_bps=cost, exposure_series=expo)
    a_open = panels["open"]["000010.SZ"]
    trade_ret = a_open.iloc[5] / a_open.iloc[3] - 1 - cost / 10000
    final = res.daily_equity.iloc[-1]
    assert abs(final - (1 + 0.5 * trade_ret)) < 1e-9, (final, 1 + 0.5 * trade_ret)
    print(f"T7 fractional exposure OK: equity {final:.6f} == 1+0.5×{trade_ret:.6f}")

    # T8 崩塌硬切：行2 全仓开仓（hold=8 应持到行11），行4 目标暴露归零 → 行5 开盘强制清仓
    expo = np.ones(len(DATES)); expo[4:] = 0.0
    res = pb.run_strategy(market, _sig_only(market, "000010.SZ", 2), name="t8",
                          start=DATES[0], end=DATES[-1], top_k=1, hold_days=8,
                          n_tranches=1, cost_bps=cost, exposure_series=expo)
    tr = res.trades[0]
    assert tr.exit_date == DATES[5], tr.exit_date
    assert res.stats["hard_cut_liquidations"] == 1
    print("T8 hard risk-off liquidation OK (exit at", tr.exit_date, ")")

    print("ALL-TESTS-PASSED")


if __name__ == "__main__":
    main()
