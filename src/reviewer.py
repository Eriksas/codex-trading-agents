"""
reviewer.py - 模拟交易计划复盘模块

读取扫描器维护的台账更新结果，结合当日快照与历史 K 线，生成确定性的
复盘 JSON、CSV 和 Markdown。该模块只做事实判断与规则归类，不生成投资建议。
"""

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DISCLAIMER = "免责声明：本复盘仅用于个人模拟交易、策略校验与技术探索，不构成任何投资建议。"

STATUS_LABELS = {
    "new_pending": "今日新计划",
    "triggered": "触发观察区间",
    "not_triggered": "未触发",
    "expired": "过期未触发",
    "stopped": "触及止损",
    "take_profit": "触及第一止盈",
    "timeout": "持有期结束",
    "active_open": "继续观察",
    "missing_data": "数据缺失",
    "skipped_same_symbol": "同标的叠加跳过",
}


def _to_float(value: Any) -> Optional[float]:
    """安全转换为 float。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_optional(value: Optional[float], digits: int = 5) -> Optional[float]:
    """对可空数字做 round。"""
    if value is None:
        return None
    return round(value, digits)


def _fmt_pct(value: Optional[float], digits: int = 2) -> str:
    """格式化百分比。"""
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%"


def _parse_date(value: str) -> Optional[datetime]:
    """解析 YYYY-MM-DD 日期。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _date_diff_days(start: str, end: str) -> int:
    """计算两个日期的自然日差。"""
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)
    if start_dt is None or end_dt is None:
        return 0
    return (end_dt.date() - start_dt.date()).days


def _read_csv_rows(path: Path) -> list[dict]:
    """读取 CSV 文件，不存在时返回空列表。"""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _archive_action(row: dict) -> str:
    """根据归档记录推断台账动作。"""
    if (row.get("status") or "") == "expired":
        return "expired_no_entry"
    reason = row.get("exit_reason") or ""
    if reason == "stop_loss":
        return "closed_stop_loss"
    if reason == "take_profit":
        return "closed_take_profit"
    if reason == "timeout":
        return "closed_timeout"
    return "closed"


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    """写入 CSV 文件。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _parse_trigger_zone(zone: str) -> tuple[Optional[float], Optional[float]]:
    """解析触发区间。"""
    if not zone or "-" not in zone:
        return None, None
    low_text, high_text = zone.split("-", maxsplit=1)
    low = _to_float(low_text)
    high = _to_float(high_text)
    return low, high


def _snapshot_bar(row: dict) -> Optional[dict]:
    """将行情快照转换成简化日 K。"""
    open_price = _to_float(row.get("open"))
    high = _to_float(row.get("high"))
    low = _to_float(row.get("low"))
    close = _to_float(row.get("latest") or row.get("close"))
    if close is None:
        return None
    return {
        "date": row.get("date") or "",
        "open": open_price if open_price is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "turnover": _to_float(row.get("turnover")),
        "volume": _to_float(row.get("volume")),
    }


def _history_period(
    bars: list[dict],
    start_date: str,
    end_date: str,
    include_start: bool,
) -> list[dict]:
    """截取历史 K 线区间。"""
    result: list[dict] = []
    for bar in bars:
        date = str(bar.get("date") or "")
        if not date:
            continue
        if include_start:
            in_range = start_date <= date <= end_date
        else:
            in_range = start_date < date <= end_date
        if in_range:
            open_price = _to_float(bar.get("open"))
            high = _to_float(bar.get("high"))
            low = _to_float(bar.get("low"))
            close = _to_float(bar.get("close"))
            if open_price is None or high is None or low is None or close is None:
                continue
            result.append(
                {
                    "date": date,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "turnover": _to_float(bar.get("turnover")),
                    "volume": _to_float(bar.get("volume")),
                }
            )
    return result


def _merge_snapshot_bar(period_bars: list[dict], snapshot: Optional[dict], review_date: str) -> list[dict]:
    """用当日快照补齐复盘区间末日 K 线。"""
    if snapshot is None:
        return period_bars
    merged = [bar for bar in period_bars if bar.get("date") != review_date]
    merged.append({**snapshot, "date": review_date})
    return sorted(merged, key=lambda x: x.get("date") or "")


def _bar_touches_zone(bar: dict, low: Optional[float], high: Optional[float]) -> bool:
    """判断单根 K 线是否触及区间。"""
    if low is None or high is None:
        return False
    bar_low = _to_float(bar.get("low"))
    bar_high = _to_float(bar.get("high"))
    if bar_low is None or bar_high is None:
        return False
    return not (bar_low > high or bar_high < low)


def _status_from_action(row: dict, review_date: str) -> str:
    """将台账动作映射为复盘状态。"""
    action = row.get("ledger_action") or ""
    status = row.get("status") or ""
    signal_date = row.get("signal_date") or ""
    if action == "skipped_same_symbol_active" or status == "skipped_same_symbol":
        return "skipped_same_symbol"
    if action == "new_pending" or signal_date == review_date:
        return "new_pending"
    if action == "entered":
        return "triggered"
    if action == "expired_no_entry":
        return "expired"
    if action == "closed_stop_loss":
        return "stopped"
    if action == "closed_take_profit":
        return "take_profit"
    if action == "closed_timeout":
        return "timeout"
    if action == "open_kept" or status == "open":
        return "active_open"
    if action == "missing_snapshot":
        return "missing_data"
    if status == "expired":
        return "expired"
    if status == "closed":
        reason = row.get("exit_reason")
        if reason == "stop_loss":
            return "stopped"
        if reason == "take_profit":
            return "take_profit"
        return "timeout"
    return "not_triggered"


def _build_observation(status: str, row: dict, entry_zone_touched: bool, has_bars: bool) -> str:
    """生成规则化复盘观察。"""
    if status == "new_pending":
        return "今日新生成的模拟计划，后续交易日再进入复盘窗口。"
    if status == "skipped_same_symbol":
        return row.get("overlap_risk_note") or "同标的已有 active/pending，本轮未新增 pending。"
    if not has_bars:
        return "缺少可用于复盘的当日行情或历史 K 线，本次只保留台账状态。"
    if status == "triggered":
        return "价格触及触发区间，台账状态已转为 open，后续按止损、第一止盈和持有期继续复核。"
    if status == "not_triggered":
        return "尚未触及触发区间，仍按原有效期观察是否失效。"
    if status == "expired":
        return "触发区间在有效期内未被触及，模拟计划已按规则过期归档。"
    if status == "stopped":
        return "盘中触及规则止损位，台账已按模拟规则归档。"
    if status == "take_profit":
        return "盘中触及第一止盈位，台账已按模拟规则归档。"
    if status == "timeout":
        return "达到最大持有期，台账按收盘价完成模拟归档。"
    if status == "active_open":
        return "仍处于 open 观察状态，尚未触及退出规则。"
    if entry_zone_touched:
        return "价格曾触及触发区间，但台账动作需结合成交规则继续核对。"
    return "未发现触发或退出事件。"


def _review_row(
    row: dict,
    review_date: str,
    snapshot: Optional[dict],
    history_bars: list[dict],
) -> dict:
    """复盘单条台账更新记录。"""
    symbol = row.get("symbol") or ""
    signal_date = row.get("signal_date") or ""
    entry_date = row.get("entry_date") or ""
    signal_price = _to_float(row.get("signal_price"))
    entry_price = _to_float(row.get("entry_price"))
    stop_loss = _to_float(row.get("stop_loss"))
    first_take_profit = _to_float(row.get("first_take_profit"))
    trigger_low, trigger_high = _parse_trigger_zone(str(row.get("trigger_zone") or ""))

    status = _status_from_action(row, review_date)
    include_start = bool(entry_date)
    start_date = entry_date or signal_date
    if status == "new_pending":
        period_bars = []
    else:
        period_bars = _history_period(history_bars, start_date, review_date, include_start=include_start) if start_date else []
        period_bars = _merge_snapshot_bar(period_bars, snapshot, review_date)
    has_bars = bool(period_bars)

    period_high = max((bar["high"] for bar in period_bars), default=None)
    period_low = min((bar["low"] for bar in period_bars), default=None)
    period_close = period_bars[-1]["close"] if period_bars else None
    period_turnover = period_bars[-1].get("turnover") if period_bars else None
    entry_zone_touched = any(_bar_touches_zone(bar, trigger_low, trigger_high) for bar in period_bars)
    stop_touched = any((_to_float(bar.get("low")) or 0) <= stop_loss for bar in period_bars) if stop_loss is not None else False
    take_profit_touched = any((_to_float(bar.get("high")) or 0) >= first_take_profit for bar in period_bars) if first_take_profit is not None else False

    reference_price = entry_price or signal_price
    max_favorable_pct = period_high / reference_price - 1 if period_high is not None and reference_price else None
    max_adverse_pct = period_low / reference_price - 1 if period_low is not None and reference_price else None
    close_vs_signal_pct = period_close / signal_price - 1 if period_close is not None and signal_price else None
    close_vs_entry_pct = period_close / entry_price - 1 if period_close is not None and entry_price else None

    if status != "new_pending" and (
        status == "missing_data" or (row.get("ledger_action") not in {"new_pending"} and not has_bars)
    ):
        status = "missing_data"

    observation = _build_observation(status, row, entry_zone_touched, has_bars)
    return {
        "trade_id": row.get("trade_id"),
        "symbol": symbol,
        "name": row.get("name"),
        "signal_date": signal_date,
        "review_date": review_date,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "ledger_action": row.get("ledger_action"),
        "entry_date": entry_date,
        "exit_date": row.get("exit_date") or "",
        "signal_price": signal_price,
        "entry_price": entry_price,
        "exit_price": _to_float(row.get("exit_price")),
        "trigger_zone": row.get("trigger_zone"),
        "stop_loss": stop_loss,
        "first_take_profit": first_take_profit,
        "base_position_pct": _to_float(row.get("base_position_pct")),
        "raw_position_pct": _to_float(row.get("raw_position_pct")),
        "position_cap_pct": _to_float(row.get("position_cap_pct")),
        "position_pct": _to_float(row.get("position_pct")),
        "market_position_multiplier": _to_float(row.get("market_position_multiplier")),
        "effective_position_multiplier": _to_float(row.get("effective_position_multiplier")),
        "position_adjustment_note": row.get("position_adjustment_note"),
        "days_since_signal": _date_diff_days(signal_date, review_date) if signal_date else None,
        "holding_days": _to_float(row.get("holding_days")),
        "entry_zone_touched": entry_zone_touched,
        "stop_touched": stop_touched,
        "take_profit_touched": take_profit_touched,
        "period_high": _round_optional(period_high, 3),
        "period_low": _round_optional(period_low, 3),
        "period_close": _round_optional(period_close, 3),
        "period_turnover": _round_optional(period_turnover, 2),
        "max_favorable_pct": _round_optional(max_favorable_pct),
        "max_adverse_pct": _round_optional(max_adverse_pct),
        "stop_low_price": _to_float(row.get("stop_low_price")),
        "stop_breach_pct": _to_float(row.get("stop_breach_pct")),
        "stop_slippage_bps": _to_float(row.get("stop_slippage_bps")),
        "slippage_exit_price": _to_float(row.get("slippage_exit_price")),
        "net_return_worst_intraday": _to_float(row.get("net_return_worst_intraday")),
        "net_return_slippage": _to_float(row.get("net_return_slippage")),
        "slippage_vs_ideal_return": _to_float(row.get("slippage_vs_ideal_return")),
        "close_vs_signal_pct": _round_optional(close_vs_signal_pct),
        "close_vs_entry_pct": _round_optional(close_vs_entry_pct),
        "same_symbol_active_trade_ids": row.get("same_symbol_active_trade_ids"),
        "overlap_risk_note": row.get("overlap_risk_note"),
        "observation": observation,
        "plan": row.get("plan"),
        "data_quality": (
            "not_applicable"
            if status in {"new_pending", "skipped_same_symbol"}
            else ("complete" if has_bars else "missing_bar")
        ),
    }


def _summarize_reviews(details: list[dict], review_date: str, max_report_items: int) -> dict:
    """汇总复盘明细。"""
    prior_items = [item for item in details if item.get("status") not in {"new_pending", "skipped_same_symbol"}]
    status_counts: dict[str, int] = {}
    for item in details:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    close_deviations = [
        item["close_vs_signal_pct"]
        for item in prior_items
        if item.get("close_vs_signal_pct") is not None
    ]
    favorable_values = [
        item["max_favorable_pct"]
        for item in prior_items
        if item.get("max_favorable_pct") is not None
    ]
    adverse_values = [
        item["max_adverse_pct"]
        for item in prior_items
        if item.get("max_adverse_pct") is not None
    ]
    closed_statuses = {"expired", "stopped", "take_profit", "timeout"}
    entry_confirmed_statuses = {"triggered", "active_open", "stopped", "take_profit", "timeout"}
    return {
        "review_date": review_date,
        "generated_at": datetime.now().isoformat(),
        "reviewed_count": len(prior_items),
        "new_pending_count": status_counts.get("new_pending", 0),
        "triggered_count": status_counts.get("triggered", 0),
        "newly_triggered_count": status_counts.get("triggered", 0),
        "entry_confirmed_count": sum(status_counts.get(status, 0) for status in entry_confirmed_statuses),
        "not_triggered_count": status_counts.get("not_triggered", 0),
        "active_open_count": status_counts.get("active_open", 0),
        "closed_count": sum(status_counts.get(status, 0) for status in closed_statuses),
        "skipped_same_symbol_count": status_counts.get("skipped_same_symbol", 0),
        "missing_data_count": status_counts.get("missing_data", 0),
        "status_counts": status_counts,
        "field_definitions": {
            "triggered_count": "当日台账动作 entered 的新触发数量，等同 newly_triggered_count。",
            "entry_confirmed_count": "已确认入场或已完成退出的历史复盘数量，包含 active_open/stopped/take_profit/timeout/triggered。",
            "new_pending_count": "今日新生成并进入 pending 台账的计划数量；同标的叠加跳过不计入。",
            "skipped_same_symbol_count": "因同标的已有 active/pending 而未新增 pending 的候选数量。",
        },
        "average_close_vs_signal_pct": (
            round(sum(close_deviations) / len(close_deviations), 5) if close_deviations else None
        ),
        "best_max_favorable_pct": round(max(favorable_values), 5) if favorable_values else None,
        "worst_max_adverse_pct": round(min(adverse_values), 5) if adverse_values else None,
        "details": details,
        "report_items": prior_items[:max_report_items],
        "new_pending_items": [item for item in details if item.get("status") == "new_pending"],
        "note": "复盘由规则和行情数据生成，只描述模拟计划状态与历史波动，不代表未来走势。",
    }


def render_review_report(summary: dict) -> str:
    """渲染复盘 Markdown 报告。"""
    lines = [
        f"# 模拟计划复盘 - {summary.get('review_date')}",
        "",
        DISCLAIMER,
        "",
        "**复盘摘要**",
        "",
        f"- 复盘对象：{summary.get('reviewed_count')} 笔，今日新 pending：{summary.get('new_pending_count')} 笔",
        (
            f"- 今日新触发：{summary.get('newly_triggered_count')}，"
            f"历史已入场/退出：{summary.get('entry_confirmed_count')}，"
            f"未触发：{summary.get('not_triggered_count')}，继续观察：{summary.get('active_open_count')}，"
            f"本次归档：{summary.get('closed_count')}，同标的跳过：{summary.get('skipped_same_symbol_count')}"
        ),
        (
            f"- 平均收盘相对信号价：{_fmt_pct(summary.get('average_close_vs_signal_pct'))}，"
            f"最大顺向波动：{_fmt_pct(summary.get('best_max_favorable_pct'))}，"
            f"最大逆向波动：{_fmt_pct(summary.get('worst_max_adverse_pct'))}"
        ),
        f"- 数据缺失：{summary.get('missing_data_count')} 笔",
        "",
        "**逐笔复盘**",
        "",
        "| 标的 | 信号日 | 复盘状态 | 收盘偏离 | 最大顺向 | 最大逆向 | 观察 |",
        "|---|---:|---|---:|---:|---:|---|",
    ]
    report_items = summary.get("report_items") or []
    if report_items:
        for item in report_items:
            observation = str(item.get("observation") or "-").replace("|", "/")
            lines.append(
                "| {name} {symbol} | {signal_date} | {status} | {close_dev} | {mfe} | {mae} | {observation} |".format(
                    name=item.get("name") or "",
                    symbol=item.get("symbol") or "",
                    signal_date=item.get("signal_date") or "-",
                    status=item.get("status_label") or item.get("status") or "-",
                    close_dev=_fmt_pct(item.get("close_vs_signal_pct")),
                    mfe=_fmt_pct(item.get("max_favorable_pct")),
                    mae=_fmt_pct(item.get("max_adverse_pct")),
                    observation=observation,
                )
            )
    else:
        lines.append("| - | - | - | - | - | - | 暂无可复盘的历史 pending/open 记录 |")

    new_pending = summary.get("new_pending_items") or []
    if new_pending:
        lines.extend(["", "**今日新 pending**", ""])
        for item in new_pending:
            lines.append(
                f"- {item.get('name')} {item.get('symbol')}：信号价 {item.get('signal_price')}，触发区间 {item.get('trigger_zone')}"
            )

    skipped_items = [item for item in summary.get("details") or [] if item.get("status") == "skipped_same_symbol"]
    if skipped_items:
        lines.extend(["", "**同标的叠加跳过**", ""])
        for item in skipped_items:
            lines.append(
                f"- {item.get('name')} {item.get('symbol')}：{item.get('overlap_risk_note') or item.get('observation')}"
            )

    lines.extend(["", DISCLAIMER, ""])
    return "\n".join(lines)


def review_trade_updates(
    ledger_updates_path: Path,
    review_date: str,
    output_dir: Path,
    stock_snapshots: list[dict],
    histories: Optional[dict[str, list[dict]]] = None,
    archive_path: Optional[Path] = None,
    max_report_items: int = 8,
) -> dict:
    """
    根据台账更新生成复盘产物。

    Args:
        ledger_updates_path: 台账更新 CSV 路径
        review_date:         复盘日期 YYYY-MM-DD
        output_dir:          输出目录
        stock_snapshots:     当日行情快照
        histories:           symbol -> 历史 K 线
        archive_path:        归档台账路径，用于同日重复运行时补回当日归档记录
        max_report_items:    Markdown 报告最多展示明细数
    Returns:
        复盘汇总字典
    """
    histories = histories or {}
    rows = _read_csv_rows(ledger_updates_path)
    seen_trade_ids = {row.get("trade_id") for row in rows}
    if archive_path is not None:
        for row in _read_csv_rows(archive_path):
            if row.get("trade_id") in seen_trade_ids or row.get("exit_date") != review_date:
                continue
            rows.append({**row, "ledger_action": _archive_action(row)})
            seen_trade_ids.add(row.get("trade_id"))

    snapshot_map = {str(row.get("symkey") or ""): _snapshot_bar(row) for row in stock_snapshots if row.get("symkey")}
    details = [
        _review_row(
            row,
            review_date,
            snapshot_map.get(str(row.get("symbol") or "")),
            histories.get(str(row.get("symbol") or ""), []),
        )
        for row in rows
    ]
    summary = _summarize_reviews(details, review_date, max_report_items=max_report_items)

    output_dir.mkdir(parents=True, exist_ok=True)
    details_path = output_dir / "review_details.csv"
    summary_path = output_dir / "review_summary.json"
    report_path = output_dir / "review_report.md"
    fields = [
        "trade_id", "symbol", "name", "signal_date", "review_date", "status",
        "status_label", "ledger_action", "entry_date", "exit_date", "signal_price",
        "entry_price", "exit_price", "trigger_zone", "stop_loss", "first_take_profit",
        "base_position_pct", "raw_position_pct", "position_cap_pct", "position_pct",
        "market_position_multiplier", "effective_position_multiplier", "position_adjustment_note",
        "days_since_signal", "holding_days", "entry_zone_touched",
        "stop_touched", "take_profit_touched", "period_high", "period_low",
        "period_close", "period_turnover", "max_favorable_pct", "max_adverse_pct",
        "stop_low_price", "stop_breach_pct", "stop_slippage_bps", "slippage_exit_price",
        "net_return_worst_intraday", "net_return_slippage", "slippage_vs_ideal_return",
        "close_vs_signal_pct", "close_vs_entry_pct", "observation", "data_quality",
        "same_symbol_active_trade_ids", "overlap_risk_note", "plan",
    ]
    _write_csv(details_path, details, fields)
    report = render_review_report(summary)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    summary["details_path"] = str(details_path)
    summary["report_path"] = str(report_path)
    summary["summary_path"] = str(summary_path)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info("review generated: %s", summary_path)
    return summary
