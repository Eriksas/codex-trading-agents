"""离线检查案例与导入回答；不调用模型、不读取行情、不修改策略。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
DEFAULT_CASES = Path(__file__).with_name("cases.json")
ENUMS = {
    "data_status": {"sufficient", "insufficient"},
    "confidence": {"low", "medium", "high"},
    "conclusion_grade": {"Rejected", "Inconclusive", "Promising", "Validated"},
    "recommended_action": {
        "request_data", "observe", "reject", "propose_experiment", "request_review",
        "correct_report", "promote", "modify_main_strategy",
    },
}
STEPS = {
    "data_check", "collect_samples", "independent_experiment", "historical_validation",
    "human_confirmation", "check_confounders", "reconcile_with_python",
}
EXPECTED_KEYS = set(ENUMS) | {"causal_claim", "metrics", "required_steps", "evidence_sources"}
RESPONSE_KEYS = EXPECTED_KEYS | {"case_id", "summary"}


def require(condition: bool, message: str) -> None:
    """校验条件；不依赖可被 python -O 禁用的 assert。"""
    if not condition:
        raise ValueError(message)


def exact_keys(value: Any, keys: set[str], label: str) -> None:
    """检查对象和字段集合，避免拼错字段被静默忽略。"""
    require(isinstance(value, dict), f"{label}: 必须是对象")
    require(set(value) == keys, f"{label}: 字段必须为 {sorted(keys)}")


def text_list(value: Any, label: str, *, nonempty: bool = True) -> None:
    """检查无重复的非空字符串列表。"""
    require(isinstance(value, list), f"{label}: 必须是列表")
    require(not nonempty or bool(value), f"{label}: 不得为空")
    require(all(isinstance(v, str) and bool(v.strip()) for v in value), f"{label}: 需要非空字符串")
    require(len(value) == len(set(value)), f"{label}: 不得重复")


def validate_metrics(value: Any) -> None:
    """只允许有限数值或 null；拒绝 bool 冒充数字。"""
    require(isinstance(value, dict), "metrics: 必须是对象")
    for key, number in value.items():
        require(isinstance(key, str) and bool(key.strip()), "metrics: 名称不得为空")
        require(number is None or (type(number) in (int, float) and math.isfinite(number)),
                "metrics: 只接受有限数值或 null")


def validate_cases(payload: Any) -> list[dict[str, Any]]:
    """校验固定场景、预期约束及来源引用，不执行案例任务。"""
    exact_keys(payload, {"schema_version", "data_kind", "cases"}, "案例集")
    require(type(payload["schema_version"]) is int and payload["schema_version"] == 1,
            "案例 schema_version 必须为 1")
    require(payload["data_kind"] == "synthetic_rule_cases_not_market_experiments",
            "必须标记为合成规则案例")
    cases = payload["cases"]
    require(isinstance(cases, list) and bool(cases), "案例集不得为空")
    ids: set[str] = set()
    for case in cases:
        exact_keys(case, {"id", "title", "input", "expected", "manual_review"}, "案例")
        for key in ("id", "title"):
            require(isinstance(case[key], str) and bool(case[key].strip()), f"{key}: 不得为空")
        require(case["id"] not in ids, "案例 id 重复")
        ids.add(case["id"])
        exact_keys(case["input"], {"task", "sources", "note"}, "input")
        for key in ("task", "note"):
            require(isinstance(case["input"][key], str) and bool(case["input"][key].strip()),
                    f"input.{key}: 不得为空")
        sources = case["input"]["sources"]
        require(isinstance(sources, dict) and bool(sources), "sources 不得为空")
        text_list(list(sources), "source ids")
        expected = case["expected"]
        exact_keys(expected, EXPECTED_KEYS, "expected")
        for key, choices in ENUMS.items():
            text_list(expected[key], key)
            require(set(expected[key]) <= choices, f"expected.{key}: 未知枚举")
        require(type(expected["causal_claim"]) is bool, "causal_claim 必须为布尔值")
        validate_metrics(expected["metrics"])
        text_list(expected["required_steps"], "required_steps")
        require(set(expected["required_steps"]) <= STEPS, "未知验证步骤")
        text_list(expected["evidence_sources"], "evidence_sources")
        require(set(expected["evidence_sources"]) <= set(sources), "预期引用了未知来源")
        text_list(case["manual_review"], "manual_review")
    return cases


def validate_responses(payload: Any, cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """校验导入格式和已声明的采集信息；真实性仍需人工核对。"""
    exact_keys(payload, {"schema_version", "provenance", "responses"}, "回答文件")
    require(type(payload["schema_version"]) is int and payload["schema_version"] == 1,
            "回答 schema_version 必须为 1")
    provenance = payload["provenance"]
    exact_keys(provenance, {"kind", "model", "prompt_version", "captured_at"}, "provenance")
    require(provenance["kind"] in ("agent_capture", "handwritten_fixture"), "未知回答来源类型")
    for key in ("model", "prompt_version", "captured_at"):
        require(isinstance(provenance[key], str) and bool(provenance[key].strip()), f"{key}: 不得为空")
    rows = payload["responses"]
    require(isinstance(rows, list) and bool(rows), "responses 不得为空；没有回答不能当成通过")
    known = {case["id"]: case for case in cases}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        exact_keys(row, RESPONSE_KEYS, "回答")
        case_id = row["case_id"]
        require(isinstance(case_id, str), "case_id 必须为字符串")
        require(case_id in known, "未知 case_id")
        require(case_id not in result, "回答 case_id 重复")
        for key, choices in ENUMS.items():
            require(isinstance(row[key], str) and row[key] in choices, f"{key}: 未知枚举")
        require(type(row["causal_claim"]) is bool, "causal_claim 必须为布尔值")
        require(isinstance(row["summary"], str) and bool(row["summary"].strip()), "summary 不得为空")
        validate_metrics(row["metrics"])
        for key in ("required_steps", "evidence_sources"):
            text_list(row[key], key, nonempty=False)
        require(set(row["required_steps"]) <= STEPS, "回答包含未知步骤")
        require(set(row["evidence_sources"]) <= set(known[case_id]["input"]["sources"]),
                "回答引用未知来源")
        result[case_id] = row
    return result


def check_response(case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """检查结构化字段；不把关键词匹配当作自然语言事实验证。"""
    expected = case["expected"]
    checks = {key: row[key] in expected[key] for key in ENUMS}
    checks["causal_claim"] = row["causal_claim"] == expected["causal_claim"]
    for key in ("required_steps", "evidence_sources"):
        checks[key] = set(expected[key]) <= set(row[key])
    metrics = row["metrics"]
    checks["metric_names"] = set(metrics) == set(expected["metrics"])
    for key, number in expected["metrics"].items():
        actual = metrics.get(key)
        checks[f"metric:{key}"] = key in metrics and (
            actual is None if number is None else
            actual is not None and math.isclose(actual, number, rel_tol=1e-9, abs_tol=1e-12)
        )
    return {
        "case_id": case["id"],
        "status": "auto_pass_manual_pending" if all(checks.values()) else "rule_failed",
        "checks": checks,
        "manual_review": {"status": "pending", "questions": case["manual_review"]},
    }


def evaluate(cases: list[dict[str, Any]], payload: Any = None) -> tuple[dict[str, Any], int]:
    """返回报告及退出码：0 已完成当前自动检查，1 规则失败，2 格式/覆盖错误。"""
    report: dict[str, Any] = {
        "schema_version": 1, "scope": "case_validation", "model_called": False,
        "agent_evaluated": False, "case_count": len(cases), "submitted_count": 0,
        "overall_status": "cases_valid_no_agent_run", "results": [],
    }
    if payload is None:
        return report, 0
    rows = validate_responses(payload, cases)
    results = [check_response(case, rows[case["id"]]) for case in cases if case["id"] in rows]
    missing = [case["id"] for case in cases if case["id"] not in rows]
    failed = sum(result["status"] == "rule_failed" for result in results)
    report.update({
        "scope": "imported_response_rules",
        "agent_evaluated": payload["provenance"]["kind"] == "agent_capture",
        "provenance_declared": payload["provenance"], "provenance_verified": False,
        "submitted_count": len(rows), "missing_case_ids": missing,
        "auto_pass_count": len(results) - failed, "rule_failed_count": failed,
        "manual_pending_count": len(results), "results": results,
        "overall_status": "incomplete" if missing else "rule_failed" if failed else "manual_review_required",
    })
    return report, 2 if missing else 1 if failed else 0


def reject_constant(value: str) -> None:
    """拒绝非标准 JSON 的 NaN/Infinity。"""
    raise ValueError(f"JSON 不允许 {value}")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 键，防止后值覆盖前值隐藏冲突。"""
    obj: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in obj, "JSON 对象包含重复键")
        obj[key] = value
    return obj


def load_json(path: Path) -> tuple[Any, str]:
    """读取 UTF-8 输入，返回解析对象与原文件 SHA256。"""
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8"), parse_constant=reject_constant,
                      object_pairs_hook=unique_object), hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    """命令行入口；默认仅输出 JSON，持久化结果限于忽略的评测输出目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--responses", type=Path, help="人工导入的原始结构化回答 JSON")
    parser.add_argument("--output", type=Path, help="可选，路径须位于 output/agent_eval/ 下")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        output = args.output.resolve() if args.output else None
        if output:
            allowed = DEFAULT_CASES.parent.parent / "output" / "agent_eval"
            require(output.is_relative_to(allowed.resolve()), "输出必须位于 output/agent_eval/ 下")
            inputs = [args.cases.resolve()] + ([args.responses.resolve()] if args.responses else [])
            require(output not in inputs, "不能覆盖案例或回答输入")
        case_payload, cases_hash = load_json(args.cases)
        cases = validate_cases(case_payload)
        response_payload, responses_hash = load_json(args.responses) if args.responses else (None, None)
        if args.responses:
            require(response_payload is not None, "回答文件不能为 null")
        report, code = evaluate(cases, response_payload)
        report.update({"created_at": datetime.now(timezone.utc).isoformat(),
                       "cases_sha256": cases_hash, "responses_sha256": responses_hash})
        rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        LOGGER.info("%s; case_count=%d; model_called=false", report["overall_status"], len(cases))
        return code
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        LOGGER.error("评测输入或输出错误：%s", exc)
        print(json.dumps({"overall_status": "input_error", "model_called": False,
                          "agent_evaluated": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
