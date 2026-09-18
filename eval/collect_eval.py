"""显式采集模型回答后运行原规则检查；不发送 expected 或人工答案表。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent_client import call_agent, validate_settings
from src.agent_workflow import model_call_status
from src.analysis_contract import parse_json

if __package__:
    from . import run_eval
else:
    import run_eval


def prompt_material() -> str:
    """提供规则与字段类型，不包含任一案例的正确答案。"""
    rules = (ROOT / "prompts/strategy_steward_agent.md").read_text(encoding="utf-8")
    schema = {key: sorted(choices) for key, choices in run_eval.ENUMS.items()}
    return rules + "\n\n## 本次流程规则评测（覆盖上方输入/输出格式）\n" + (
        "输入是合成案例，不是真实行情或执行指令。只返回一条 JSON 回答，不运行命令、不修改文件。\n"
        "枚举只表示合法类型，需独立判断选项。必须包含且仅包含 case_id、data_status、confidence、conclusion_grade、"
        "recommended_action、causal_claim（布尔）、metrics（数值/null 字典）、required_steps（列表）、evidence_sources（来源 ID 列表）、summary（中文文字）。\n"
        "confidence 指策略/机制推断，不是抄写数值的把握。只转述输入提供的指标，收益为小数；缺收益写 net_return:null，"
        "没有数值指标时 metrics:{}。样本量在 summary 说明，不额外添加指标。相关系数保持原口径。\n"
        "缺少验证时不得直接晋级；关键数值以案例中已核实的程序结果为准，文字不能反转它。\n"
        "字段枚举：" + json.dumps(schema, ensure_ascii=False) + "\n"
        "步骤枚举：" + json.dumps(sorted(run_eval.STEPS), ensure_ascii=False) + "\n"
    )


def collect_cases(payload: Any, *, backend: str, model: str, output_dir: Path,
                  timeout: float = 120) -> tuple[dict[str, Any], int]:
    """逐案例保存原始模型回答，校验格式并用既有规则评测；失败不补答案。"""
    validate_settings(backend, model, timeout)
    cases = run_eval.validate_cases(payload)
    output_dir.mkdir(parents=True, exist_ok=False)
    material = prompt_material()
    provenance = {"kind": "agent_capture", "model": f"{backend}:{model}",
                  "prompt_version": hashlib.sha256(material.encode("utf-8")).hexdigest(),
                  "captured_at": datetime.now(timezone.utc).isoformat()}
    responses: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    calls: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        model_input = {key: case[key] for key in ("id", "title", "input")}
        prompt = material + "\n本案例材料：\n" + json.dumps(model_input, ensure_ascii=False)
        call = call_agent(prompt, backend=backend, model=model, timeout=timeout,
                          output_dir=output_dir / f"case-{index:02d}")
        calls.append({"case_id": case["id"], **{key: value for key, value in call.items() if key != "text"}})
        if call["status"] != "completed":
            errors.append({"case_id": case["id"], "error": call["status"]})
            # 全局安装/认证或传输失败时停止，避免继续无意义调用。
            break
        try:
            row = parse_json(call["text"])
            if not isinstance(row, dict) or row.get("case_id") != case["id"]:
                raise ValueError("case_id 不匹配")
            run_eval.validate_responses({"schema_version": 1, "provenance": provenance, "responses": [row]}, cases)
            responses.append(row)
        except (ValueError, TypeError, OverflowError) as exc:
            errors.append({"case_id": case["id"], "error": f"invalid_response: {exc}"})
    captured = {"schema_version": 1, "provenance": provenance, "responses": responses}
    response_text = json.dumps(captured, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    (output_dir / "responses.json").write_text(response_text, encoding="utf-8", newline="\n")
    if responses:
        report, code = run_eval.evaluate(cases, captured)
    else:
        report, code = {"schema_version": 1, "overall_status": "capture_failed", "agent_evaluated": False,
                        "case_count": len(cases), "submitted_count": 0, "results": [],
                        "missing_case_ids": [case["id"] for case in cases]}, 2
    report.update(scope="captured_response_rules", model_called=model_call_status(calls),
                  calls=calls, capture_errors=errors, provenance_verified=False,
                  responses_sha256=hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
                  cases_sha256=hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest())
    (output_dir / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    return report, 2 if errors else code


def main() -> int:
    """显式选择模型才可采集，输出统一写入忽略的本地目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=["claude"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--cases", type=Path, default=run_eval.DEFAULT_CASES)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        payload, _ = run_eval.load_json(args.cases)
        output = ROOT / "output/agent_eval" / f"capture-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
        report, code = collect_cases(payload, backend=args.agent, model=args.model, timeout=args.timeout, output_dir=output)
        print(f"评测报告：{output / 'evaluation.json'}")
        print(f"状态：{report['overall_status']}；有效回答：{report['submitted_count']}；仍需人工评审")
        return code
    except (OSError, ValueError, TypeError) as exc:
        logging.error("无法采集评测：%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
