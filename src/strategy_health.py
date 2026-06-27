"""
strategy_health.py - 策略健康监控模块

聚合每日复盘、候选元数据和模拟交易归档，生成滚动策略体检报告。
本模块只做统计与诊断提示，不修改策略参数。
"""

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

DISCLAIMER = "免责声明：策略健康报告仅用于个人模拟复盘和参数实验设计，不构成任何投资建议。"

CLOSED_STATUSES = {"expired", "stopped", "take_profit", "timeout"}
TRIGGERED_STATUSES = {"triggered", "active_open", "stopped", "take_profit", "timeout"}


def _to_float(value: Any) -> Optional[float]:
    """安全转换为 float。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> Optional[datetime]:
    """解析 YYYY-MM-DD 日期。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _fmt_pct(value: Optional[float], digits: int = 2) -> str:
    """格式化百分比。"""
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%"


def _read_csv_rows(path: Path) -> list[dict]:
    """读取 CSV 文件，不存在时返回空列表。"""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    """写入 CSV 文件。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _within_window(date_text: str, as_of: datetime, days: int) -> bool:
    """判断日期是否落在滚动窗口内。"""
    dt = _parse_date(date_text)
    if dt is None:
        return False
    return as_of - timedelta(days=days - 1) <= dt <= as_of


def _scan_date_from_path(path: Path) -> Optional[str]:
    """从 output/YYYY-MM-DD/scan/xxx 路径中提取日期。"""
    try:
        return path.parent.parent.name
    except IndexError:
        return None


def _trade_id(symbol: str, signal_date: str) -> str:
    """生成模拟交易 ID。"""
    return f"{symbol}_{signal_date}"


def _load_review_records(output_base: Path, as_of_date: str, history_days: int) -> list[dict]:
    """读取历史复盘明细。"""
    as_of = _parse_date(as_of_date)
    if as_of is None:
        return []
    records: list[dict] = []
    for path in sorted(output_base.glob("*/scan/review_details.csv")):
        scan_date = _scan_date_from_path(path)
        if not scan_date or not _within_window(scan_date, as_of, history_days):
            continue
        for row in _read_csv_rows(path):
            row.setdefault("review_date", scan_date)
            records.append(row)
    return records


def _load_candidate_metadata(output_base: Path, as_of_date: str, history_days: int) -> dict[str, dict]:
    """读取候选元数据，用于按标签和环境拆分表现。"""
    as_of = _parse_date(as_of_date)
    if as_of is None:
        return {}
    result: dict[str, dict] = {}
    for path in sorted(output_base.glob("*/scan/simulated_trades.csv")):
        scan_date = _scan_date_from_path(path)
        if not scan_date or not _within_window(scan_date, as_of, history_days):
            continue
        for row in _read_csv_rows(path):
            symbol = row.get("symbol") or ""
            if not symbol:
                continue
            row["signal_date"] = scan_date
            row["trade_id"] = _trade_id(symbol, scan_date)
            result[row["trade_id"]] = row
    return result


def _latest_records(records: list[dict], as_of: datetime, days: int) -> list[dict]:
    """窗口内按 trade_id 取最新复盘状态。"""
    latest: dict[str, dict] = {}
    for row in records:
        review_date = row.get("review_date") or ""
        trade_id = row.get("trade_id") or ""
        if not trade_id or not _within_window(review_date, as_of, days):
            continue
        prev = latest.get(trade_id)
        if prev is None or (row.get("review_date") or "") >= (prev.get("review_date") or ""):
            latest[trade_id] = row
    return list(latest.values())


def _archive_rows_for_window(archive_rows: list[dict], as_of: datetime, days: int) -> list[dict]:
    """取窗口内退出的归档记录。"""
    return [row for row in archive_rows if _within_window(row.get("exit_date") or "", as_of, days)]


def _avg(values: list[Optional[float]]) -> Optional[float]:
    """计算可空平均值。"""
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return round(sum(valid) / len(valid), 5)


def _rate(count: int, total: int) -> Optional[float]:
    """计算比例。"""
    if total <= 0:
        return None
    return round(count / total, 5)


def _split_tags(tags: str) -> list[str]:
    """拆分策略标签。"""
    return [item.strip() for item in tags.replace(",", "、").split("、") if item.strip() and item.strip() != "无增强标签"]


def _summarize_group(records: list[dict], archive_rows: list[dict]) -> dict:
    """汇总一组复盘记录。"""
    prior = [row for row in records if row.get("status") not in {"new_pending", "skipped_same_symbol"}]
    reviewed = len(prior)
    triggered = sum(
        1
        for row in prior
        if row.get("status") in TRIGGERED_STATUSES or bool(row.get("entry_date"))
    )
    stopped = sum(1 for row in prior if row.get("status") == "stopped")
    take_profit = sum(1 for row in prior if row.get("status") == "take_profit")
    expired = sum(1 for row in prior if row.get("status") == "expired")
    active_open = sum(1 for row in prior if row.get("status") == "active_open")
    missing_data = sum(1 for row in records if row.get("data_quality") == "missing_bar")
    archive_returns = [_to_float(row.get("net_return")) for row in archive_rows if _to_float(row.get("net_return")) is not None]
    return {
        "reviewed_count": reviewed,
        "new_pending_count": sum(1 for row in records if row.get("status") == "new_pending"),
        "skipped_same_symbol_count": sum(1 for row in records if row.get("status") == "skipped_same_symbol"),
        "triggered_count": triggered,
        "take_profit_count": take_profit,
        "stopped_count": stopped,
        "expired_count": expired,
        "active_open_count": active_open,
        "closed_count": sum(1 for row in prior if row.get("status") in CLOSED_STATUSES),
        "missing_data_count": missing_data,
        "trigger_rate": _rate(triggered, reviewed),
        "take_profit_rate": _rate(take_profit, reviewed),
        "stop_loss_rate": _rate(stopped, reviewed),
        "expired_rate": _rate(expired, reviewed),
        "avg_close_vs_signal_pct": _avg([_to_float(row.get("close_vs_signal_pct")) for row in prior]),
        "avg_max_favorable_pct": _avg([_to_float(row.get("max_favorable_pct")) for row in prior]),
        "avg_max_adverse_pct": _avg([_to_float(row.get("max_adverse_pct")) for row in prior]),
        "realized_trade_count": len(archive_returns),
        "realized_win_rate": _rate(sum(1 for value in archive_returns if value > 0), len(archive_returns)),
        "avg_net_return": _avg(archive_returns),
    }


def _tag_breakdown(records: list[dict], metadata: dict[str, dict], max_rows: int) -> list[dict]:
    """按策略标签拆分表现。"""
    grouped: dict[str, list[dict]] = {}
    for row in records:
        if row.get("status") in {"new_pending", "skipped_same_symbol"}:
            continue
        meta = metadata.get(row.get("trade_id") or "") or {}
        for tag in _split_tags(meta.get("strategy_tags") or ""):
            grouped.setdefault(tag, []).append(row)
    rows: list[dict] = []
    for tag, tag_records in grouped.items():
        summary = _summarize_group(tag_records, [])
        rows.append(
            {
                "tag": tag,
                "reviewed_count": summary["reviewed_count"],
                "trigger_rate": summary["trigger_rate"],
                "take_profit_rate": summary["take_profit_rate"],
                "stop_loss_rate": summary["stop_loss_rate"],
                "avg_close_vs_signal_pct": summary["avg_close_vs_signal_pct"],
            }
        )
    rows.sort(key=lambda x: (x.get("reviewed_count") or 0, x.get("take_profit_rate") or 0), reverse=True)
    return rows[:max_rows]


def _regime_breakdown(records: list[dict], metadata: dict[str, dict], archive_rows: list[dict]) -> list[dict]:
    """按市场环境拆分表现。"""
    grouped: dict[str, list[dict]] = {}
    archive_grouped: dict[str, list[dict]] = {}
    for row in records:
        if row.get("status") in {"new_pending", "skipped_same_symbol"}:
            continue
        meta = metadata.get(row.get("trade_id") or "") or {}
        regime = meta.get("market_regime_label") or "未知"
        grouped.setdefault(regime, []).append(row)
    for row in archive_rows:
        regime = row.get("market_regime_label") or "未知"
        archive_grouped.setdefault(regime, []).append(row)
    result = []
    for regime, rows in grouped.items():
        summary = _summarize_group(rows, archive_grouped.get(regime, []))
        result.append({"market_regime_label": regime, **summary})
    result.sort(key=lambda x: x.get("reviewed_count") or 0, reverse=True)
    return result


def _diagnostics(window_summary: dict, min_sample: int) -> tuple[list[str], list[str]]:
    """基于窗口统计生成健康提示和实验假设。"""
    notes: list[str] = []
    experiments: list[str] = []
    reviewed = int(window_summary.get("reviewed_count") or 0)
    if reviewed < min_sample:
        notes.append(f"样本数 {reviewed}，低于 {min_sample}，暂不应基于该窗口单独调参。")
        experiments.append("继续累积样本；参数实验只保留为候选，不替换主策略。")
        return notes, experiments

    trigger_rate = window_summary.get("trigger_rate")
    stop_loss_rate = window_summary.get("stop_loss_rate")
    take_profit_rate = window_summary.get("take_profit_rate")
    avg_adverse = window_summary.get("avg_max_adverse_pct")
    avg_favorable = window_summary.get("avg_max_favorable_pct")
    avg_net = window_summary.get("avg_net_return")

    if trigger_rate is not None and trigger_rate < 0.35:
        notes.append("触发率偏低，候选可能离触发区间较远，或触发区间偏窄。")
        experiments.append("开一个影子版本：略放宽 pullback/chase 区间，但保持仓位不变，观察触发率和止损率是否同步抬升。")
    if stop_loss_rate is not None and stop_loss_rate >= 0.45:
        notes.append("止损率偏高，需检查市场环境过滤、止损宽度或过热过滤。")
        experiments.append("开一个影子版本：防守/中性环境提高最低综合分，或降低单票仓位上限。")
    if take_profit_rate is not None and stop_loss_rate is not None and take_profit_rate > stop_loss_rate:
        notes.append("第一止盈率高于止损率，近期触发后的顺向效率尚可。")
    if avg_adverse is not None and avg_adverse <= -0.06:
        notes.append("平均最大逆向波动较深，触发后回撤压力偏大。")
        experiments.append("开一个影子版本：对高波动标的进一步降低仓位，或增加 volatility_20d 过滤强度。")
    if avg_favorable is not None and avg_favorable >= 0.06 and take_profit_rate is not None and take_profit_rate < 0.3:
        notes.append("最大顺向波动不低但第一止盈率不高，可能存在止盈距离或执行窗口问题。")
        experiments.append("开一个影子版本：测试更近的第一止盈或分批止盈参数。")
    if avg_net is not None and avg_net < 0:
        notes.append("已归档交易平均净收益为负，需优先观察是否由单日极端行情主导。")

    if not notes:
        notes.append("暂未触发硬性健康告警，继续按当前参数观察。")
    if not experiments:
        experiments.append("暂不新增参数实验；继续累积 20/60 日窗口样本。")
    return notes, experiments


def _window_summary(
    records: list[dict],
    archive_rows: list[dict],
    metadata: dict[str, dict],
    as_of: datetime,
    days: int,
    min_sample: int,
    max_tag_rows: int,
) -> dict:
    """生成单个窗口的健康摘要。"""
    latest = _latest_records(records, as_of, days)
    archive_window = _archive_rows_for_window(archive_rows, as_of, days)
    summary = _summarize_group(latest, archive_window)
    notes, experiments = _diagnostics(summary, min_sample=min_sample)
    return {
        "window_days": days,
        **summary,
        "tag_breakdown": _tag_breakdown(latest, metadata, max_rows=max_tag_rows),
        "regime_breakdown": _regime_breakdown(latest, metadata, archive_window),
        "diagnostics": notes,
        "experiment_hypotheses": experiments,
    }


def render_health_report(summary: dict) -> str:
    """渲染策略健康 Markdown 报告。"""
    lines = [
        f"# 策略健康监控 - {summary.get('as_of_date')}",
        "",
        DISCLAIMER,
        "",
        "**字段口径**",
        "",
        "- 触发率：滚动窗口内已确认入场的复盘数量 / 复盘数，包含继续观察、止盈、止损、到期等已入场状态。",
        "- 该触发率不同于复盘日报里的“今日新触发”，后者只统计当日刚从 pending 进入 open 的计划。",
        "- 同标的叠加跳过不计入复盘数。",
        "",
        "**滚动窗口概览**",
        "",
        "| 窗口 | 复盘数 | 触发率 | 第一止盈率 | 止损率 | 平均净收益 | 平均顺向 | 平均逆向 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary.get("windows") or []:
        lines.append(
            "| {days}日 | {reviewed} | {trigger} | {tp} | {sl} | {net} | {mfe} | {mae} |".format(
                days=item.get("window_days"),
                reviewed=item.get("reviewed_count"),
                trigger=_fmt_pct(item.get("trigger_rate")),
                tp=_fmt_pct(item.get("take_profit_rate")),
                sl=_fmt_pct(item.get("stop_loss_rate")),
                net=_fmt_pct(item.get("avg_net_return")),
                mfe=_fmt_pct(item.get("avg_max_favorable_pct")),
                mae=_fmt_pct(item.get("avg_max_adverse_pct")),
            )
        )

    primary = summary.get("primary_window") or {}
    lines.extend(["", "**健康提示**", ""])
    for note in primary.get("diagnostics") or []:
        lines.append(f"- {note}")

    lines.extend(["", "**候选实验假设**", ""])
    for item in primary.get("experiment_hypotheses") or []:
        lines.append(f"- {item}")

    tag_rows = primary.get("tag_breakdown") or []
    if tag_rows:
        lines.extend(
            [
                "",
                "**策略标签表现**",
                "",
                "| 标签 | 样本 | 触发率 | 第一止盈率 | 止损率 | 收盘偏离 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in tag_rows:
            lines.append(
                "| {tag} | {count} | {trigger} | {tp} | {sl} | {close_dev} |".format(
                    tag=row.get("tag"),
                    count=row.get("reviewed_count"),
                    trigger=_fmt_pct(row.get("trigger_rate")),
                    tp=_fmt_pct(row.get("take_profit_rate")),
                    sl=_fmt_pct(row.get("stop_loss_rate")),
                    close_dev=_fmt_pct(row.get("avg_close_vs_signal_pct")),
                )
            )

    lines.extend(["", DISCLAIMER, ""])
    return "\n".join(lines)


def build_strategy_health(
    output_base: Path,
    as_of_date: str,
    output_dir: Path,
    archive_path: Path,
    windows: list[int],
    history_days: int = 120,
    min_sample: int = 10,
    max_tag_rows: int = 12,
) -> dict:
    """
    生成策略健康监控产物。

    Args:
        output_base: 输出根目录
        as_of_date:  统计截止日期 YYYY-MM-DD
        output_dir:  当日 scan 输出目录
        archive_path:归档台账路径
        windows:     滚动窗口天数列表
        history_days:读取历史产物的最大自然日范围
        min_sample:  触发健康告警所需最小样本数
        max_tag_rows:标签表现展示行数
    Returns:
        策略健康汇总字典
    """
    as_of = _parse_date(as_of_date)
    if as_of is None:
        raise ValueError(f"invalid as_of_date: {as_of_date}")
    windows = sorted({int(item) for item in windows if int(item) > 0})
    records = _load_review_records(output_base, as_of_date, history_days=history_days)
    metadata = _load_candidate_metadata(output_base, as_of_date, history_days=history_days)
    archive_rows = _read_csv_rows(archive_path)

    window_summaries = [
        _window_summary(
            records,
            archive_rows,
            metadata,
            as_of,
            days=days,
            min_sample=min_sample,
            max_tag_rows=max_tag_rows,
        )
        for days in windows
    ]
    primary_window = next((item for item in window_summaries if item.get("reviewed_count", 0) >= min_sample), None)
    if primary_window is None and window_summaries:
        primary_window = window_summaries[0]

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "strategy_health_summary.json"
    report_path = output_dir / "strategy_health_report.md"
    tag_path = output_dir / "strategy_health_tags.csv"
    primary_tags = (primary_window or {}).get("tag_breakdown") or []
    _write_csv(
        tag_path,
        primary_tags,
        ["tag", "reviewed_count", "trigger_rate", "take_profit_rate", "stop_loss_rate", "avg_close_vs_signal_pct"],
    )
    summary = {
        "as_of_date": as_of_date,
        "generated_at": datetime.now().isoformat(),
        "history_days": history_days,
        "windows": window_summaries,
        "primary_window": primary_window or {},
        "field_definitions": {
            "triggered_count": "滚动窗口内已确认入场的复盘数量，包含 triggered/active_open/stopped/take_profit/timeout。",
            "trigger_rate": "triggered_count / reviewed_count；不同于 review_summary.triggered_count 的当日新触发口径。",
            "skipped_same_symbol_count": "同标的已有 active/pending 而跳过新增 pending 的数量，不计入 reviewed_count。",
        },
        "data_sources": {
            "review_records": len(records),
            "candidate_metadata": len(metadata),
            "archive_rows": len(archive_rows),
        },
        "note": "健康报告只用于观察策略漂移和设计影子实验，不自动修改主策略。",
        "summary_path": str(summary_path),
        "report_path": str(report_path),
        "tag_breakdown_path": str(tag_path),
    }
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(render_health_report(summary))
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary
