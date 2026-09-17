"""只读前向观察台账，生成适合飞书阅读的事实、解释、局限与下一步。"""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FS = ROOT / "forward_state"
OUT = Path("/tmp/forward_daily_report.md")


def _number(value: Any) -> float | None:
    """只展示已提供的有限数值，缺失不会被解释为零。"""
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _fmt(value: Any, *, percent: bool = False, digits: int = 1) -> str:
    """格式化原始统计；比例以百分数显示。"""
    number = _number(value)
    if number is None:
        return "缺失"
    return f"{number:.{digits}%}" if percent else f"{number:.{digits}f}"


def _read(path: Path) -> pd.DataFrame:
    """读取已有台账，不请求网络或修改源文件。"""
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")


def build_report(state_dir: Path = FS, as_of_date: str | None = None) -> str:
    """按明确截止日期汇报；旧记录、重复日期和缺字段均如实披露。"""
    today = as_of_date or datetime.now().strftime("%Y-%m-%d")
    if datetime.strptime(today, "%Y-%m-%d").strftime("%Y-%m-%d") != today:
        raise ValueError("日期必须为 YYYY-MM-DD")
    issues: list[str] = []
    def load(relative: str, label: str, date_column: str = "date") -> pd.DataFrame:
        path = state_dir / relative
        try:
            frame = _read(path)
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            issues.append(f"{label}：未取得可读台账（{type(exc).__name__}）。")
            return pd.DataFrame()
        if date_column not in frame:
            issues.append(f"{label}：缺少 {date_column} 列，无法判断数据日期。")
            return pd.DataFrame()
        parsed = pd.to_datetime(frame[date_column], format="%Y-%m-%d", errors="coerce")
        valid = parsed.notna() & frame[date_column].str.fullmatch(r"\d{4}-\d{2}-\d{2}")
        if not valid.all():
            issues.append(f"{label}：存在无效日期，本报告未采用这些行。")
        future = valid & (frame[date_column] > today)
        if future.any():
            issues.append(f"{label}：存在晚于截止日的记录，未计入本报告。")
        frame = frame[valid & ~future].sort_values(date_column, kind="stable")
        if frame.empty:
            issues.append(f"{label}：截止日内没有可用记录。")
        return frame

    event = load("bounce/events_log.csv", "反弹事件")
    gate = load("gates/gate_states.csv", "风险控制规则")
    temperature = load("thermometer/thermometer_ledger.csv", "市场活跃度")
    cb = load("cb/double_low_ledger.csv", "转债观察")
    for label, frame in (("反弹事件", event), ("风险控制规则", gate), ("市场活跃度", temperature)):
        if not frame.empty and frame["date"].duplicated().any():
            issues.append(f"{label}存在重复日期，展示和记录天数采用每日期最后一条；需复核原台账。")
    event = event.drop_duplicates("date", keep="last") if not event.empty else event
    gate = gate.drop_duplicates("date", keep="last") if not gate.empty else gate
    temperature = temperature.drop_duplicates("date", keep="last") if not temperature.empty else temperature
    dates = {label: str(frame.iloc[-1]["date"]) if not frame.empty else "无记录"
             for label, frame in (("反弹事件", event), ("风险控制规则", gate), ("市场活跃度", temperature), ("转债观察", cb))}
    not_current = [label for label, day in dates.items() if day != today]
    facts: list[str] = []
    details: list[str] = []
    next_steps: list[str] = []

    if not event.empty:
        row = event.iloc[-1]
        fired_text = str(row.get("fired", "")).strip().lower()
        fired = {"true": True, "false": False, "1": True, "0": False}.get(fired_text)
        status = "已触发模拟观察" if fired is True else "未触发模拟观察" if fired is False else "触发状态缺失"
        count = event.loc[event.get("fired", pd.Series("", index=event.index)).astype(str).str.lower().isin(["true", "1"]), "date"].nunique()
        facts.append(f"反弹事件：{row['date']} {status}；累计记录 {count} 个触发日期，不按同日股票只数扩大事件样本。")
        ret5, ret1, ret60 = (_number(row.get(key)) for key in ("ret5", "ret1", "ret60"))
        if fired is None or any(value is None for value in (ret5, ret1, ret60)):
            issues.append("反弹事件存在缺失指标或触发状态，不能完整核对条件。")
        gap = "缺失" if ret5 is None else "已达到阈值" if ret5 <= -0.05 else f"距下跌 5% 的阈值还差 {(ret5 + 0.05) * 100:.2f} 个百分点"
        details += ["### 反弹观察条件", "",
                    f"- 近五日涨跌幅 {_fmt(ret5, percent=True, digits=2)}：{gap}。",
                    f"- 当日涨跌幅 {_fmt(ret1, percent=True, digits=2)}；近 60 日涨跌幅 {_fmt(ret60, percent=True, digits=2)}。",
                    "- 原规则另要求当日涨幅大于 0、近 60 日涨跌幅大于 -25%，且不在冷却期；是否触发以台账为准。", ""]
        if count < 20:
            next_steps.append(f"反弹观察已记录 {count} 个触发日期，尚不足 20 次事件的原定观察门槛，继续记录。")
        else:
            next_steps.append("反弹触发日期已达 20 个；仍需核对结算和样本质量，再按原协议检验，不能自动晋级。")
        ledger = load("bounce/signals_ledger.csv", "反弹模拟明细", "signal_date")
        if not ledger.empty and "status" in ledger:
            holding = int(ledger["status"].eq("holding").sum())
            pending = int(ledger["status"].eq("pending_entry_next_open").sum())
            details += [f"- 已提供明细中：模拟持有 {holding} 条，等待次日入场 {pending} 条；两者分别计数。", ""]
        elif not ledger.empty:
            issues.append("反弹模拟明细缺少状态，无法区分模拟持有与等待入场。")

    if not gate.empty:
        current = gate.iloc[-1]
        previous = gate.iloc[-2] if len(gate) > 1 else None
        facts.append(f"风险控制规则：已有 {len(gate)} 个不同日期的记录；下方比例是模型目标，不是实际账户持仓。")
        details += ["### 三种风险控制规则", ""]
        for column, label in (("g2_trend_vote", "趋势投票（G2）"), ("g3_dvol_trend", "下跌波动与趋势（G3）"), ("g4_dd_ladder", "回撤阶梯（G4）")):
            current_value = _number(current.get(column))
            old_value = _number(previous.get(column)) if previous is not None else None
            comparison = "没有可比较的上一条记录"
            if current_value is not None and old_value is not None and 0 <= current_value <= 1 and 0 <= old_value <= 1:
                comparison = f"比 {previous['date']} 的 {_fmt(old_value, percent=True)} 变化 {(current_value - old_value) * 100:+.1f} 个百分点"
            details.append(f"- {label}：{_fmt(current_value, percent=True)}；{comparison}。")
            if current_value is None or not 0 <= current_value <= 1:
                issues.append(f"{label} 的目标比例缺失或越界，需检查原记录。")
            if old_value is not None and not 0 <= old_value <= 1:
                issues.append(f"{label} 的上一条目标比例越界，未计算变化。")
        details += ["- 目标比例表示规则在模拟框架中允许投入的资金比例；变化不等于收益改善。", ""]
        for threshold, name in ((60, "诊断"), (120, "早停检查"), (250, "主评估"), (500, "确认")):
            if len(gate) < threshold:
                next_steps.append(f"风险控制规则距离第 {threshold} 个记录日的“{name}”检查点，还差 {threshold - len(gate)} 个记录日；未核验日期连续性。")
                break
        else:
            next_steps.append("风险控制规则已达到预设记录天数；应另行执行协议检查，本日报不自动作晋级结论。")

    if not temperature.empty:
        row = temperature.iloc[-1]
        facts.append(f"市场活跃度：{row['date']} 的涨停池有 {_fmt(row.get('limit_up_pool_n'), digits=0)} 只，最长连续涨停 {_fmt(row.get('max_streak'), digits=0)} 天。")
        details += ["### 市场活跃度明细", "",
                    f"- 连续两天及以上涨停 {_fmt(row.get('streak2_n'), digits=0)} 只；尾盘封板 {_fmt(row.get('late_seal_n'), digits=0)} 只；封单合计 {_fmt(row.get('seal_money_sum'))} 亿元。",
                    "- 这些是当日数量记录；缺少可比历史或对照时，不写成“明天会上涨”的判断。", ""]
        if _number(row.get("limit_up_pool_n")) is None:
            issues.append("市场活跃度的涨停数量缺失，不能解释为零只。")

    if not cb.empty:
        day = cb[cb["date"] == cb.iloc[-1]["date"]].copy()
        row = day.iloc[0]
        facts.append(f"转债观察：{row['date']} 的既定筛选范围内，价格低于 100 元的有 {_fmt(row.get('cb_below_100_n'), digits=0)} 只；这是辅助价格指标。")
        details += ["### 转债辅助指标", "",
                    f"- 双低指标中位数 {_fmt(row.get('cb_dlow_median'))}；双低是价格和溢价率的排序指标，不是收益率。",
                    "- 当前没有完整筛选范围的分母，不计算低价券占比；也不由此直接推断违约风险或 A 股涨跌。"]
        if "rank" in day:
            day = day.assign(_rank=pd.to_numeric(day["rank"], errors="coerce")).sort_values("_rank", kind="stable")
        if {"名称", "双低"} <= set(day.columns):
            details.append("- 已记录名单前五项：" + "、".join(f"{item['名称']}（指标 {_fmt(item['双低'])}）" for _, item in day.head(5).iterrows()) + "；仅供观察。")
        details.append("")
        if _number(row.get("cb_below_100_n")) is None:
            issues.append("转债辅助数量缺失，不能据此比较变化。")

    dividend = load("divlv/divlv_ledger.csv", "季度红利名单")
    quarter = f"{today[:4]}Q{(int(today[5:7]) - 1) // 3 + 1}"
    if not dividend.empty and "quarter" in dividend:
        valid = dividend["quarter"].str.fullmatch(r"\d{4}Q[1-4]") & (dividend["quarter"] <= quarter)
        if not valid.all():
            issues.append("季度红利名单含无效或晚于当前季度的标记，未计入数量。")
        dividend = dividend[valid]
        if not dividend.empty:
            latest = dividend["quarter"].max()
            facts.append(f"季度红利名单：{latest} 共 {int(dividend['quarter'].eq(latest).sum())} 条观察记录；季度更新，不要求每天换名单。")
    elif not dividend.empty:
        issues.append("季度红利名单缺少季度标记，无法判断更新范围。")

    conclusion = "本期记录用于检查观察是否持续、数据是否完整，尚不能据此判断策略有效。"
    if not_current:
        conclusion = f"有 {len(not_current)} 条日频观察线未提供 {today} 的记录；以下按各自最新日期汇报，不把旧记录当成今日新信号。"
    lines = [f"# 前向观察日报｜{today}", "",
             "个人模拟研究留痕，不构成投资建议；本报告没有执行交易或调整策略。", "",
             "## 今日结论", "", f"- {conclusion}", "",
             "## 事实依据", "", *[f"- {item}" for item in facts], "",
             "## 如何理解", "",
             "- 先区分事件是否发生、规则输出了什么、后续结果如何。前两项不能替代收益和稳定性验证。",
             "- 当前记录之间即使同时变化，也不能直接写成因果关系；需要历史比较、对照和足够样本。", "",
             "## 还不能判断", "",
             "- 单日触发、目标比例或市场数量，都不足以证明某条策略有效；本报告没有做新的收益回测。",
             "- 日期不一致时先检查交易日历与任务输入，不仅凭更新日期认定任务失败。",
             *[f"- {item}" for item in issues], "",
             "## 下一步", "", *[f"- {item}" for item in next_steps],
             "- 先复核数据缺口，继续按冻结规则记录；策略变化需独立验证和人工确认。", "",
             "## 观察明细", "", *details,
             "## 数据来源", "", "- 最新日频记录：" + "；".join(f"{name} {day}" for name, day in dates.items()) + "。",
             "- 数据来自已留痕的前向观察台账；本次只读取并汇总，没有补造缺失值。", "",
             "个人模拟研究记录，不构成投资建议。", ""]
    return "\n".join(lines)


def main() -> None:
    """保留云端默认输出位置；可指定目录生成只读预览。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=FS)
    parser.add_argument("--date", help="统计截止日期，默认当地日期")
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args()
    report = build_report(args.state_dir, args.date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
