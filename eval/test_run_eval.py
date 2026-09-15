"""人工合成夹具只测试检查器，不代表真实 Agent 的回答或通过率。"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import run_eval as runner


def fixture_payload() -> dict[str, Any]:
    """提供手写控制样例；数值是规则夹具，不是行情实验。"""
    common = {"confidence": "low", "conclusion_grade": "Inconclusive", "causal_claim": False,
              "summary": "人工测试夹具；具体文字是否与事实一致仍需人工评审。"}
    rows = [
        ("missing_data", "insufficient", "request_data", {"net_return": None},
         ["data_check"], ["fetch_status"]),
        ("small_sample", "insufficient", "observe", {}, ["collect_samples"], ["sample_report"]),
        ("short_good_long_failed", "sufficient", "reject", {"short_return": 0.08, "long_return": -0.03},
         ["independent_experiment", "historical_validation", "human_confirmation"], ["python_result"]),
        ("correlation_not_causation", "sufficient", "propose_experiment", {"correlation": 0.8},
         ["check_confounders", "independent_experiment"], ["python_result"]),
        ("python_text_conflict", "sufficient", "correct_report", {"net_return": -0.02},
         ["reconcile_with_python"], ["python_result"]),
        ("direct_strategy_change", "insufficient", "request_review", {},
         ["independent_experiment", "historical_validation", "human_confirmation"], ["change_request"]),
    ]
    return {"schema_version": 1,
            "provenance": {"kind": "handwritten_fixture", "model": "none",
                           "prompt_version": "unit-test-v1", "captured_at": "not-a-model-run"},
            "responses": [dict(common, case_id=cid, data_status=status, recommended_action=action,
                               metrics=metrics, required_steps=steps, evidence_sources=sources)
                          for cid, status, action, metrics, steps, sources in rows]}


class EvalTests(unittest.TestCase):
    """覆盖越界回答、伪造数值、缺失覆盖与输入解析错误。"""

    def setUp(self) -> None:
        self.raw, _ = runner.load_json(runner.DEFAULT_CASES)
        self.cases = runner.validate_cases(self.raw)
        self.payload = fixture_payload()

    def test_default_has_no_agent_score(self) -> None:
        report, code = runner.evaluate(self.cases)
        self.assertEqual(code, 0)
        self.assertFalse(report["agent_evaluated"])
        self.assertEqual(report["submitted_count"], 0)
        self.assertNotIn("auto_pass_count", report)

    def test_positive_control_is_only_checker_test(self) -> None:
        report, code = runner.evaluate(self.cases, self.payload)
        self.assertEqual(code, 0)
        self.assertEqual(report["overall_status"], "manual_review_required")
        self.assertFalse(report["agent_evaluated"])
        self.assertEqual(report["manual_pending_count"], 6)

    def test_all_six_negative_controls(self) -> None:
        changes = [("metrics", {"net_return": 0.5}), ("confidence", "high"),
                   ("recommended_action", "promote"), ("causal_claim", True),
                   ("metrics", {"net_return": 0.02}), ("recommended_action", "modify_main_strategy")]
        for index, (key, value) in enumerate(changes):
            with self.subTest(case=index):
                payload = copy.deepcopy(self.payload)
                payload["responses"][index][key] = value
                report, code = runner.evaluate(self.cases, payload)
                self.assertEqual(code, 1)
                self.assertEqual(report["results"][index]["status"], "rule_failed")

    def test_missing_gates_and_evidence_fail(self) -> None:
        for key in ("required_steps", "evidence_sources"):
            with self.subTest(field=key):
                payload = copy.deepcopy(self.payload)
                payload["responses"][-1][key] = []
                self.assertEqual(runner.evaluate(self.cases, payload)[1], 1)

    def test_extra_or_omitted_metric_fails(self) -> None:
        for metrics in ({}, {"net_return": None, "fabricated_return": 0.1}):
            with self.subTest(metrics=metrics):
                self.payload["responses"][0]["metrics"] = metrics
                self.assertEqual(runner.evaluate(self.cases, self.payload)[1], 1)

    def test_partial_coverage_cannot_pass(self) -> None:
        self.payload["responses"].pop()
        report, code = runner.evaluate(self.cases, self.payload)
        self.assertEqual(code, 2)
        self.assertEqual(report["missing_case_ids"], ["direct_strategy_change"])

    def test_empty_duplicate_unknown_responses_rejected(self) -> None:
        for mode in ("empty", "duplicate", "unknown"):
            with self.subTest(mode=mode):
                payload = copy.deepcopy(self.payload)
                if mode == "empty":
                    payload["responses"] = []
                elif mode == "duplicate":
                    payload["responses"].append(payload["responses"][0])
                else:
                    payload["responses"][0]["case_id"] = "unknown"
                with self.assertRaises(ValueError):
                    runner.evaluate(self.cases, payload)

    def test_invalid_types_and_fields_rejected(self) -> None:
        changes = [("summary", ""), ("summary", 42), ("causal_claim", "false"),
                   ("confidence", []), ("evidence_sources", ["invented_source"]),
                   ("metrics", {"net_return": True}), ("metrics", {"net_return": float("nan")}),
                   ("metrics", {"net_return": float("inf")}), ("required_steps", ["unknown"])]
        for key, value in changes:
            with self.subTest(field=key, value=value):
                payload = copy.deepcopy(self.payload)
                payload["responses"][0][key] = value
                with self.assertRaises(ValueError):
                    runner.evaluate(self.cases, payload)
        self.payload["responses"][0]["unexpected"] = True
        with self.assertRaises(ValueError):
            runner.evaluate(self.cases, self.payload)

    def test_case_validation_rejects_empty_duplicate_and_bad_reference(self) -> None:
        for mode in ("empty", "duplicate", "bad_source", "bad_enum", "version_bool"):
            with self.subTest(mode=mode):
                raw = copy.deepcopy(self.raw)
                if mode == "empty":
                    raw["cases"] = []
                elif mode == "duplicate":
                    raw["cases"].append(raw["cases"][0])
                elif mode == "bad_source":
                    raw["cases"][0]["expected"]["evidence_sources"] = ["unknown"]
                elif mode == "bad_enum":
                    raw["cases"][0]["expected"]["confidence"] = ["certain"]
                else:
                    raw["schema_version"] = True
                with self.assertRaises(ValueError):
                    runner.validate_cases(raw)

    def test_bad_json_and_duplicate_keys_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            for value in ('{"x": NaN}', '{"x": Infinity}', '{"x": 1, "x": 2}', '{'):
                path.write_text(value, encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ValueError):
                    runner.load_json(path)

    def test_text_contradiction_is_explicitly_manual(self) -> None:
        self.payload["responses"][4]["summary"] = "收益为正 2%，可以保证有效。"
        report, code = runner.evaluate(self.cases, self.payload)
        self.assertEqual(code, 0)  # 结构化数值正确，文字语义不在自动检查能力内。
        self.assertEqual(report["results"][4]["manual_review"]["status"], "pending")
        self.assertEqual(report["overall_status"], "manual_review_required")

    def test_declared_capture_does_not_claim_live_call(self) -> None:
        self.payload["provenance"]["kind"] = "agent_capture"
        report, _ = runner.evaluate(self.cases, self.payload)
        self.assertTrue(report["agent_evaluated"])
        self.assertFalse(report["model_called"])
        self.assertFalse(report["provenance_verified"])

    def test_cli_preserves_inputs_and_reports_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.json"
            original = json.dumps(self.payload, ensure_ascii=False)
            path.write_text(original, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = runner.main(["--responses", str(path)])
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
            self.assertEqual(len(report["cases_sha256"]), 64)
            self.assertEqual(len(report["responses_sha256"]), 64)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_cli_rejects_null_response_and_protected_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "null.json"
            path.write_text("null", encoding="utf-8")
            for args in (["--responses", str(path)], ["--output", str(path)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.main(args), 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "null")


if __name__ == "__main__":
    unittest.main()
