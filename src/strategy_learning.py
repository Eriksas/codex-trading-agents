"""
strategy_learning.py - 策略学习记忆模块

将每日复盘、健康监控、Hermes critic 与实验草案沉淀为可审计的长期经验。
本模块只更新学习记忆，不修改主策略或交易台账。
"""

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_MEMORY_DIR = Path("data/strategy_learning")
DISCLAIMER = "策略学习记忆仅用于个人模拟复盘和工程改进，不构成投资建议。"


def _read_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件，不存在或解析失败时返回空字典。"""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    """读取 CSV 文件，不存在时返回空列表。"""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _to_float(value: Any) -> Optional[float]:
    """安全转换为 float。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_memory(path: Path) -> dict[str, Any]:
    """读取或初始化学习记忆。"""
    payload = _read_json(path)
    if payload:
        payload.setdefault("version", 1)
        payload.setdefault("lessons", [])
        return payload
    return {
        "version": 1,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "disclaimer": DISCLAIMER,
        "lessons": [],
    }


def _compact_evidence(evidence: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """保留最近若干条证据，避免记忆无限膨胀。"""
    deduped: dict[tuple[Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any]] = []
    for item in evidence:
        key = (item.get("date"), item.get("source"))
        if key not in deduped:
            order.append(key)
        deduped[key] = item
    compacted = []
    for key in order[-limit:]:
        item = deduped[key]
        next_item = dict(item)
        if "sample_warning" in next_item:
            next_item["sample_warning"] = _shorten(next_item.get("sample_warning"), 500)
        compacted.append(next_item)
    return compacted


def _shorten(text: Any, limit: int = 500) -> str:
    """裁剪长文本，避免学习记忆膨胀。"""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 20].rstrip() + "...（已截断）"


def _upsert_lesson(memory: dict[str, Any], lesson: dict[str, Any]) -> dict[str, Any]:
    """按 lesson_id 更新学习经验。"""
    lessons = memory.setdefault("lessons", [])
    existing = next((item for item in lessons if item.get("id") == lesson["id"]), None)
    evidence = lesson.pop("evidence")
    if existing is None:
        lesson["first_seen"] = evidence.get("date")
        lesson["last_seen"] = evidence.get("date")
        lesson["seen_count"] = 1
        lesson["evidence"] = [evidence]
        lessons.append(lesson)
        return lesson

    existing_evidence = list(existing.get("evidence") or [])
    evidence_key = (evidence.get("date"), evidence.get("source"))
    replace_idx = next(
        (
            idx
            for idx, item in enumerate(existing_evidence)
            if (item.get("date"), item.get("source")) == evidence_key
        ),
        None,
    )
    existing["last_seen"] = evidence.get("date")
    existing["status"] = lesson.get("status", existing.get("status"))
    existing["severity"] = lesson.get("severity", existing.get("severity"))
    existing["summary"] = lesson.get("summary", existing.get("summary"))
    existing["rule"] = lesson.get("rule", existing.get("rule"))
    existing["next_gate"] = lesson.get("next_gate", existing.get("next_gate"))
    if replace_idx is None:
        existing_evidence.append(evidence)
    else:
        existing_evidence[replace_idx] = evidence
    existing["evidence"] = _compact_evidence(existing_evidence)
    existing["seen_count"] = len(existing["evidence"])
    return existing


def _windows_are_identical(windows: list[dict[str, Any]]) -> bool:
    """判断 5/20/60 等窗口核心指标是否完全相同。"""
    if len(windows) < 2:
        return False
    keys = [
        "reviewed_count",
        "triggered_count",
        "take_profit_count",
        "stopped_count",
        "closed_count",
        "active_open_count",
        "avg_close_vs_signal_pct",
        "avg_max_favorable_pct",
        "avg_max_adverse_pct",
        "realized_trade_count",
        "realized_win_rate",
        "avg_net_return",
    ]
    baseline = {key: windows[0].get(key) for key in keys}
    return all({key: window.get(key) for key in keys} == baseline for window in windows[1:])


def _tag_rows_are_redundant(rows: list[dict[str, Any]]) -> bool:
    """判断多个标签表现是否完全重叠，无法区分边际贡献。"""
    if len(rows) < 2:
        return False
    comparable = [
        (
            row.get("reviewed_count"),
            row.get("trigger_rate"),
            row.get("take_profit_rate"),
            row.get("stop_loss_rate"),
            row.get("avg_close_vs_signal_pct"),
        )
        for row in rows
    ]
    first = comparable[0]
    same_count = sum(1 for item in comparable if item == first)
    return same_count >= min(4, len(comparable))


def _stopped_gap_stats(review_rows: list[dict[str, str]]) -> dict[str, Any]:
    """统计止损后继续下穿 stop 的观察样本。"""
    gaps = []
    for row in review_rows:
        if row.get("status") != "stopped":
            continue
        stop = _to_float(row.get("stop_loss"))
        low = _to_float(row.get("period_low"))
        if stop is None or low is None or stop <= 0 or low >= stop:
            continue
        gaps.append(
            {
                "trade_id": row.get("trade_id"),
                "symbol": row.get("symbol"),
                "breach_pct": round(low / stop - 1, 5),
            }
        )
    return {
        "count": len(gaps),
        "worst_breach_pct": min((item["breach_pct"] for item in gaps), default=None),
        "examples": gaps[:3],
    }


def _overlap_stats(review_rows: list[dict[str, str]]) -> dict[str, Any]:
    """检查同标的 active_open 与 new_pending 是否叠加。"""
    active_symbols = {row.get("symbol") for row in review_rows if row.get("status") == "active_open"}
    new_symbols = {row.get("symbol") for row in review_rows if row.get("status") == "new_pending"}
    overlap = sorted(symbol for symbol in active_symbols & new_symbols if symbol)
    return {"count": len(overlap), "symbols": overlap}


def update_strategy_learning(
    output_base: Path,
    as_of_date: str,
    memory_dir: Path = DEFAULT_MEMORY_DIR,
) -> dict[str, Any]:
    """
    更新策略学习记忆。

    Args:
        output_base: 输出根目录
        as_of_date:  日期 YYYY-MM-DD
        memory_dir:  长期学习记忆目录
    Returns:
        本次学习摘要
    """
    scan_dir = output_base / as_of_date / "scan"
    health = _read_json(scan_dir / "strategy_health_summary.json")
    market = _read_json(scan_dir / "market_profile.json")
    review_summary = _read_json(scan_dir / "review_summary.json")
    experiments = _read_json(Path("strategy_experiments") / as_of_date / "hermes_experiments.json")
    review_rows = _read_csv_rows(scan_dir / "review_details.csv")
    tag_rows = _read_csv_rows(scan_dir / "strategy_health_tags.csv")
    critic_path = scan_dir / "strategy_steward_critic.md"

    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_path = memory_dir / "learning_memory.json"
    memory = _load_memory(memory_path)

    primary = health.get("primary_window") or {}
    reviewed = int(primary.get("reviewed_count") or 0)
    closed = int(primary.get("closed_count") or 0)
    min_sample = 10
    changed: list[dict[str, Any]] = []

    if reviewed < min_sample:
        changed.append(
            _upsert_lesson(
                memory,
                {
                    "id": "small_sample_no_tuning",
                    "title": "小样本优先不调参",
                    "status": "active",
                    "severity": "high",
                    "summary": "复盘样本低于门槛时，只能继续观察或生成影子草案，不能修改主策略。",
                    "rule": "reviewed_count < 10 或 closed_count < 10 时，不提出主策略参数升级。",
                    "next_gate": "closed_count >= 30 且覆盖多个交易日后再讨论回测验证。",
                    "evidence": {
                        "date": as_of_date,
                        "source": "strategy_health_summary.json",
                        "reviewed_count": reviewed,
                        "closed_count": closed,
                    },
                },
            )
        )

    windows = health.get("windows") or []
    if _windows_are_identical(windows):
        changed.append(
            _upsert_lesson(
                memory,
                {
                    "id": "identical_windows_are_not_trend",
                    "title": "相同滚动窗口不是趋势稳定",
                    "status": "active",
                    "severity": "medium",
                    "summary": "5/20/60 日窗口完全相同通常说明样本过薄，不能解读为短中长期稳定。",
                    "rule": "多个窗口核心指标完全相同时，优先标记为样本不足或历史跨度不足。",
                    "next_gate": "窗口之间出现独立样本差异后再做漂移判断。",
                    "evidence": {
                        "date": as_of_date,
                        "source": "strategy_health_summary.json",
                        "window_count": len(windows),
                        "reviewed_count": reviewed,
                    },
                },
            )
        )

    if _tag_rows_are_redundant(tag_rows):
        changed.append(
            _upsert_lesson(
                memory,
                {
                    "id": "redundant_tags_no_edge_claim",
                    "title": "标签完全重叠时不能声称边际优势",
                    "status": "active",
                    "severity": "medium",
                    "summary": "多个策略标签样本和表现完全一致时，只能说明条件共现，不能比较标签优劣。",
                    "rule": "标签 reviewed_count 和表现指标重叠时，不提出标签权重优化。",
                    "next_gate": "标签之间至少有 30 笔非重叠样本后再评估边际贡献。",
                    "evidence": {
                        "date": as_of_date,
                        "source": "strategy_health_tags.csv",
                        "tag_rows": len(tag_rows),
                    },
                },
            )
        )

    current_regime = market.get("regime_label")
    regime_rows = primary.get("regime_breakdown") or []
    historical_regimes = sorted({row.get("market_regime_label") for row in regime_rows if row.get("market_regime_label")})
    if current_regime and historical_regimes and current_regime not in historical_regimes:
        changed.append(
            _upsert_lesson(
                memory,
                {
                    "id": "regime_mismatch_needs_separate_sample",
                    "title": "当前市场环境与已平仓样本不一致",
                    "status": "active",
                    "severity": "medium",
                    "summary": "不能用上一环境生成的样本评估当前环境下的参数效果。",
                    "rule": "市场环境切换后，需等待该环境下的新 closed 样本再讨论参数。",
                    "next_gate": "同一 regime 下 closed_count >= 30。",
                    "evidence": {
                        "date": as_of_date,
                        "source": "market_profile.json + strategy_health_summary.json",
                        "current_regime": current_regime,
                        "historical_regimes": historical_regimes,
                    },
                },
            )
        )

    gap_stats = _stopped_gap_stats(review_rows)
    if gap_stats["count"]:
        changed.append(
            _upsert_lesson(
                memory,
                {
                    "id": "stop_gap_is_execution_issue",
                    "title": "跳空穿止损优先归为执行假设问题",
                    "status": "active",
                    "severity": "medium",
                    "summary": "价格低于规则止损位较多时，不能简单归因为止损宽度参数错误。",
                    "rule": "先记录 stop breach，再判断是否需要执行模型或跳空保护实验。",
                    "next_gate": "stopped 样本中跳空穿 stop 累计 >= 30 后再设计实验。",
                    "evidence": {
                        "date": as_of_date,
                        "source": "review_details.csv",
                        **gap_stats,
                    },
                },
            )
        )

    overlap = _overlap_stats(review_rows)
    if overlap["count"]:
        changed.append(
            _upsert_lesson(
                memory,
                {
                    "id": "same_symbol_overlap_watch",
                    "title": "同标的 active 与 new_pending 需要叠加风险提示",
                    "status": "active",
                    "severity": "medium",
                    "summary": "同一标的同时存在 active_open 与 new_pending 时，需提示潜在单票敞口叠加。",
                    "rule": "每日生成 pending 前检查同标的 active_open，不自动加仓。",
                    "next_gate": "累计足够同标的叠加事件后再设计单票敞口上限实验。",
                    "evidence": {
                        "date": as_of_date,
                        "source": "review_details.csv",
                        **overlap,
                    },
                },
            )
        )

    if experiments and not (experiments.get("experiments") or []):
        changed.append(
            _upsert_lesson(
                memory,
                {
                    "id": "empty_experiments_can_be_correct",
                    "title": "空实验草案是有效的风险控制输出",
                    "status": "active",
                    "severity": "low",
                    "summary": "当样本不足或 critic 全部否决时，experiments=[] 是符合边界的结果。",
                    "rule": "没有合格证据时，宁可不生成参数实验。",
                    "next_gate": "样本达标且 critic 未否决后再生成实验。",
                    "evidence": {
                        "date": as_of_date,
                        "source": "strategy_experiments/hermes_experiments.json",
                        "sample_warning": _shorten(experiments.get("sample_warning"), 500),
                        "critic_exists": critic_path.exists(),
                    },
                },
            )
        )

    memory["updated_at"] = datetime.now().isoformat()
    memory["lesson_count"] = len(memory.get("lessons") or [])
    memory_path.write_text(json.dumps(memory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    markdown_path = memory_dir / "learning_memory.md"
    markdown_path.write_text(_render_learning_markdown(memory), encoding="utf-8")
    digest_path = scan_dir / "hermes_learning_digest.md"

    summary = {
        "date": as_of_date,
        "generated_at": datetime.now().isoformat(),
        "updated_lessons": [item.get("id") for item in changed],
        "lesson_count": memory["lesson_count"],
        "memory_path": str(memory_path),
        "memory_markdown_path": str(markdown_path),
        "learning_digest_path": str(digest_path),
        "source_files": {
            "strategy_health_summary": str(scan_dir / "strategy_health_summary.json"),
            "review_details": str(scan_dir / "review_details.csv"),
            "market_profile": str(scan_dir / "market_profile.json"),
            "critic_report": str(critic_path) if critic_path.exists() else None,
            "experiments": str(Path("strategy_experiments") / as_of_date / "hermes_experiments.json"),
        },
        "note": "学习记忆由确定性脚本更新；Hermes 只读取并引用，不直接修改。",
    }
    digest_path.write_text(_render_learning_digest(memory, summary, changed), encoding="utf-8")
    summary_path = scan_dir / "strategy_learning_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _render_learning_markdown(memory: dict[str, Any]) -> str:
    """渲染人类可读学习记忆。"""
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    lines = [
        "# Strategy Learning Memory",
        "",
        DISCLAIMER,
        "",
        f"- Updated at: {memory.get('updated_at')}",
        f"- Lesson count: {memory.get('lesson_count', len(memory.get('lessons') or []))}",
        "",
        "## Active Lessons",
        "",
    ]
    lessons = sorted(
        memory.get("lessons") or [],
        key=lambda x: (severity_rank.get(str(x.get("severity")), 9), x.get("id") or ""),
    )
    for lesson in lessons:
        latest = (lesson.get("evidence") or [{}])[-1]
        lines.extend(
            [
                f"### {lesson.get('title')}",
                "",
                f"- ID: `{lesson.get('id')}`",
                f"- Status: {lesson.get('status')} / Severity: {lesson.get('severity')}",
                f"- Seen: {lesson.get('seen_count')} times, {lesson.get('first_seen')} -> {lesson.get('last_seen')}",
                f"- Summary: {lesson.get('summary')}",
                f"- Rule: {lesson.get('rule')}",
                f"- Next gate: {lesson.get('next_gate')}",
                f"- Latest evidence: {json.dumps(latest, ensure_ascii=False)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_learning_digest(
    memory: dict[str, Any],
    summary: dict[str, Any],
    changed_lessons: list[dict[str, Any]],
) -> str:
    """渲染适合飞书推送的学习摘要。"""
    changed_ids = [item.get("id") for item in changed_lessons]
    lesson_by_id = {item.get("id"): item for item in memory.get("lessons") or []}
    lines = [
        f"# Hermes 学习进化摘要 - {summary.get('date')}",
        "",
        DISCLAIMER,
        "",
        "## 本次学习结论",
        "",
        f"- 更新经验：{len(changed_ids)} 条",
        f"- 累计经验：{memory.get('lesson_count', len(memory.get('lessons') or []))} 条",
        "- 主策略：未修改",
        "- 台账：未由 Hermes 修改",
        "",
    ]
    if changed_ids:
        lines.extend(["## 强化的经验", ""])
        for lesson_id in changed_ids[:6]:
            lesson = lesson_by_id.get(lesson_id) or {}
            lines.append(f"- {lesson.get('title') or lesson_id}：{lesson.get('summary') or '-'}")
    else:
        lines.extend(["## 强化的经验", "", "- 本次没有新增或强化的经验。"])

    top_lessons = sorted(
        memory.get("lessons") or [],
        key=lambda item: ({"high": 0, "medium": 1, "low": 2}.get(str(item.get("severity")), 9), item.get("id") or ""),
    )
    lines.extend(["", "## 当前最高优先级", ""])
    for lesson in top_lessons[:3]:
        lines.append(
            f"- {lesson.get('title')}：{lesson.get('rule')} 下一门槛：{lesson.get('next_gate')}"
        )

    source_files = summary.get("source_files") or {}
    lines.extend(
        [
            "",
            "## 文件路径",
            "",
            f"- 学习记忆：`{summary.get('memory_markdown_path')}`",
            f"- Hermes 日报：`output/{summary.get('date')}/scan/strategy_steward_report.md`",
            f"- Hermes 反方审查：`{source_files.get('critic_report')}`",
            f"- 实验草案：`{source_files.get('experiments')}`",
            f"- 学习摘要：`{summary.get('learning_digest_path')}`",
            "",
            "## 边界",
            "",
            "- 本次学习只更新经验记忆和摘要，不代表买卖建议。",
            "- 样本不足时，Hermes 必须优先学习“不调参”。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="更新策略学习记忆")
    parser.add_argument("--date", required=True, help="日期 YYYY-MM-DD")
    parser.add_argument("--output", default="output", help="输出根目录")
    parser.add_argument("--memory-dir", default=str(DEFAULT_MEMORY_DIR), help="学习记忆目录")
    args = parser.parse_args()

    summary = update_strategy_learning(Path(args.output), args.date, Path(args.memory_dir))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
