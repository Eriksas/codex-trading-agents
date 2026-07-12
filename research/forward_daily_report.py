"""
forward_daily_report.py - 每日前向观察综合战报（推飞书用）

汇总六条观察线当日状态为一份详细 Markdown：市场触发距离、gate 暴露及变化、
涨停生态、双低与主板信用温度计、红利季度名单状态、检查点倒计时、台账健康。
输出 /tmp/forward_daily_report.md，由工作流经 notifier 推送（--mode full）。

纯读取汇报，不做任何判断或建议；所有数字来自当日已留痕台账。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FS = ROOT / "forward_state"
OUT = Path("/tmp/forward_daily_report.md")


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str)


def main() -> None:
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 1) 恐慌反弹：触发距离
    ev = _read(FS / "bounce/events_log.csv")
    if not ev.empty:
        r = ev.iloc[-1]
        ret5, ret1, ret60 = float(r["ret5"]), float(r["ret1"]), float(r["ret60"])
        fired = str(r.get("fired", "False")) == "True"
        gap5 = ret5 - (-0.05)
        cond = []
        cond.append(f"五日 {ret5:+.2%}（{'✓已过线' if ret5 <= -0.05 else f'距 -5% 还差 {gap5:+.2%}'}）")
        cond.append(f"当日 {ret1:+.2%}（{'✓阳线' if ret1 > 0 else '需收阳'}）")
        cond.append(f"60日 {ret60:+.2%}（{'✓' if ret60 > -0.25 else '✗深熊过滤'}）")
        status = "🔴 已触发" if fired else "⚪ 未触发"
        lines += [f"## 恐慌反弹（{r['date']}）{status}", "", "- " + "\n- ".join(cond), ""]
        led = _read(FS / "bounce/signals_ledger.csv")
        if not led.empty:
            open_pos = led[led.get("status", pd.Series(dtype=str)).isin(["holding", "pending_entry_next_open"])]
            if not open_pos.empty:
                lines += [f"- 持仓中：{len(open_pos)} 笔（详见台账）", ""]

    # 2) Gate 暴露
    g = _read(FS / "gates/gate_states.csv")
    if not g.empty:
        cur = g.iloc[-1]
        prev = g.iloc[-2] if len(g) > 1 else cur

        def _f(row: pd.Series, col: str) -> str:
            v = row.get(col)
            return f"{float(v):.0%}" if pd.notna(v) and str(v) != "" else "—"

        lines += [f"## 框架目标暴露（{cur['date']}，gate 记录第 {len(g)} 天）", "",
                  f"- G2 趋势投票：{_f(cur,'g2_trend_vote')}（前日 {_f(prev,'g2_trend_vote')}）",
                  f"- G3 半方差×动量：{_f(cur,'g3_dvol_trend')}（前日 {_f(prev,'g3_dvol_trend')}）",
                  f"- G4 回撤阶梯：{_f(cur,'g4_dd_ladder')}（前日 {_f(prev,'g4_dd_ladder')}）",
                  f"- 沪深300 收盘：{float(cur['bench_close']):.1f}", ""]
        for k, label in [(60, "Day60 诊断"), (120, "Day120 早停检查"), (250, "Day250 主决策")]:
            if len(g) < k:
                lines += [f"- 检查点：{label} 还差 {k - len(g)} 个交易日", ""]
                break

    # 3) 涨停生态温度计
    t = _read(FS / "thermometer/thermometer_ledger.csv")
    if not t.empty:
        r = t.iloc[-1]
        lines += [f"## 涨停生态（{r['date']}）", "",
                  f"- 涨停池 {r['limit_up_pool_n']} 只 | 最高连板 {r['max_streak']} | "
                  f"二板以上 {r['streak2_n']} | 封单合计 {r['seal_money_sum']} 亿 | "
                  f"尾盘封板 {r['late_seal_n']} 只", ""]

    # 4) 双低 + 主板信用温度计
    cb = _read(FS / "cb/double_low_ledger.csv")
    if not cb.empty:
        last_date = cb["date"].iloc[-1]
        day = cb[cb["date"] == last_date].head(5)
        below = day.iloc[0].get("cb_below_100_n", "—")
        med = day.iloc[0].get("cb_dlow_median", "—")
        lines += [f"## 转债信用温度计（{last_date}，主板中小盘压力代理）", "",
                  f"- 百元以下券 {below} 只 | 双低中位数 {med}",
                  "- 双低前5：" + "、".join(f"{r['名称']}({float(r['双低']):.0f})" for _, r in day.iterrows()), ""]

    # 5) 红利季度名单
    dv = _read(FS / "divlv/divlv_ledger.csv")
    if not dv.empty:
        q = dv["quarter"].iloc[-1]
        n = (dv["quarter"] == q).sum()
        lines += [f"## 红利观察名单：{q} 已记录 {n} 只（季度线，纯观察）", ""]

    # 6) 台账健康一行
    fresh = []
    for name, p in [("bounce", FS / "bounce/events_log.csv"), ("gates", FS / "gates/gate_states.csv"),
                    ("温度计", FS / "thermometer/thermometer_ledger.csv"),
                    ("双低", FS / "cb/double_low_ledger.csv")]:
        df = _read(p)
        fresh.append(f"{name}:{df['date'].iloc[-1][5:] if not df.empty else '无'}")
    lines += ["## 台账健康", "", "- 最新记录 " + " | ".join(fresh),
              "", "---", "本报告由前向观察体系自动生成，仅为个人模拟研究留痕，不构成投资建议。"]

    report = f"# 前向观察日报 {now}\n\n" + "\n".join(lines)
    OUT.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
