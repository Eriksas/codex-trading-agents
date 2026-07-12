"""
forward_health_check.py - 前向观察体系自检（每日工作流末尾运行）

检查全部台账：最新记录日期、近 10 个交易日缺口、重复日期、schema 完整性；
附 PROTOCOL_V1 检查点倒计时（gate 满 60/120/250 记录时提示评估）。
发现问题输出 /tmp/forward_health_alert.md（工作流据此推飞书）并以非零退出。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FS = ROOT / "forward_state"

LEDGERS = {
    "bounce_events": (FS / "bounce/events_log.csv", "date", "daily"),
    "gates": (FS / "gates/gate_states.csv", "date", "daily"),
    "thermometer": (FS / "thermometer/thermometer_ledger.csv", "date", "daily"),
    "cb_double_low": (FS / "cb/double_low_ledger.csv", "date", "daily"),
    "divlv_quarterly": (FS / "divlv/divlv_ledger.csv", "date", "quarterly"),
}
CHECKPOINTS = {60: "Day60 诊断（暴露分布/信号一致性）", 120: "Day120 早停检查", 250: "Day250 主决策点"}


def main() -> None:
    problems: list[str] = []
    infos: list[str] = []
    # 参考交易日：gate 台账（BaoStock 驱动）
    gate_path = LEDGERS["gates"][0]
    ref_days: list[str] = []
    if gate_path.exists():
        ref_days = sorted(pd.read_csv(gate_path, encoding="utf-8-sig")["date"].astype(str).unique())
    today = datetime.now().strftime("%Y-%m-%d")

    for name, (path, col, cadence) in LEDGERS.items():
        if not path.exists():
            problems.append(f"{name}: 台账文件不存在（{path.relative_to(ROOT)}）")
            continue
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
        if col not in df.columns or df.empty:
            problems.append(f"{name}: schema 异常或空表")
            continue
        days = sorted(df[col].astype(str).unique())
        last = days[-1]
        stale = (pd.Timestamp(today) - pd.Timestamp(last)).days
        limit = 5 if cadence == "daily" else 100
        if stale > limit:
            problems.append(f"{name}: 最新记录 {last}（已 {stale} 天无更新，容忍 {limit} 天）")
        else:
            infos.append(f"{name}: 最新 {last}，共 {len(days)} 个记录日 ✓")
        if cadence == "daily" and ref_days:
            recent_ref = [d for d in ref_days[-10:] if d >= days[0]]
            missing = [d for d in recent_ref if d not in set(days)]
            if len(missing) > 2:
                problems.append(f"{name}: 近10交易日缺 {len(missing)} 天（{missing[:3]}...）")

    # 检查点倒计时
    n_gate = len(ref_days)
    for k, label in CHECKPOINTS.items():
        if n_gate == k:
            problems.append(f"PROTOCOL_V1 检查点到期：gate 记录满 {k} 天 → {label}")
        elif 0 < k - n_gate <= 5:
            infos.append(f"检查点预告：{label} 还差 {k - n_gate} 个交易日")

    report = ["# 前向观察体系自检", f"检查时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
              f"gate 记录数：{n_gate}", ""]
    if problems:
        report += ["## ⚠ 问题"] + [f"- {p}" for p in problems] + [""]
    report += ["## 状态"] + [f"- {i}" for i in infos]
    text = "\n".join(report)
    print(text)
    if problems:
        Path("/tmp/forward_health_alert.md").write_text(text, encoding="utf-8")
        sys.exit(1)


if __name__ == "__main__":
    main()
