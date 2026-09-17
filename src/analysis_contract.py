"""诊断文字的输入输出契约；校验结构，不替代全文事实评审。"""

import hashlib
import json
from pathlib import Path
from typing import Any


def parse_json(raw: str) -> Any:
    """拒绝重复键和非有限 JSON 常量，不自动修复模型输出。"""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 有重复键")
            result[key] = value
        return result
    def reject(value: str) -> None:
        raise ValueError("JSON 不允许非有限数值")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


def validate_interpretation(value: Any, facts_hash: str, facts: dict[str, Any]) -> dict[str, Any]:
    """检查事实包标识、引用、假设字段和置信程度，保留原文字。"""
    expected = {"facts_sha256", "model", "hypotheses", "critic"}
    if not isinstance(value, dict) or set(value) != expected or value["facts_sha256"] != facts_hash:
        raise ValueError("回答字段不匹配或 facts_sha256 已过期；请使用本次材料重新分析")
    if not isinstance(value["model"], str) or not value["model"].strip():
        raise ValueError("回答必须注明实际模型；手写示例填 handwritten")
    def texts(items: Any) -> bool:
        return isinstance(items, list) and bool(items) and all(isinstance(x, str) and bool(x.strip()) for x in items)
    if not isinstance(value["hypotheses"], list) or not texts(value["critic"]):
        raise ValueError("hypotheses 必须为列表，critic 必须含反方意见或不足说明")
    source_ids = {item["id"] for item in facts["sources"]} | {"python_metrics", "data_quality"}
    for item in value["hypotheses"]:
        if not isinstance(item, dict) or set(item) != {"claim", "evidence_ids", "counterevidence", "next_check", "confidence"}:
            raise ValueError("假设字段错误")
        if not all(isinstance(item[key], str) and item[key].strip() for key in ("claim", "counterevidence", "next_check")):
            raise ValueError("假设、反证、待验证分析不得为空")
        if not texts(item["evidence_ids"]) or not set(item["evidence_ids"]) <= source_ids:
            raise ValueError("假设引用了未知来源")
        if item["confidence"] not in ("low", "medium", "high"):
            raise ValueError("未知置信程度")
        if (facts["small_sample"] or facts["quality"]["errors"] or facts["quality"]["warnings"]) and item["confidence"] != "low":
            raise ValueError("样本或数据不足时，策略假设只允许低置信度")
    return value


def read_interpretation(path: Path, facts_hash: str, facts: dict[str, Any]) -> dict[str, Any]:
    """校验人工导入的原始 JSON，不认证其模型来源。"""
    raw_bytes = path.read_bytes()
    value = validate_interpretation(parse_json(raw_bytes.decode("utf-8")), facts_hash, facts)
    return {"status": "imported_manual_review_pending", "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "provenance_verified": False, "content": value}
