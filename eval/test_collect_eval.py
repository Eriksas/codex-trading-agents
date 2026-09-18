"""采集器合成控制测试，不产生真实模型通过率。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collect_eval
from test_run_eval import fixture_payload


class CollectorTests(unittest.TestCase):
    """确认答案不泄露、失败不补造，原规则检查确实被执行。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "capture"
        self.payload, _ = collect_eval.run_eval.load_json(collect_eval.run_eval.DEFAULT_CASES)
        self.rows = fixture_payload()["responses"]
        self.prompts: list[str] = []

    def reply(self, prompt: str, **kwargs: object) -> dict:
        index = len(self.prompts)
        self.prompts.append(prompt)
        return {"status": "completed", "text": json.dumps(self.rows[index], ensure_ascii=False), "model_called": True}

    def collect(self) -> tuple[dict, int]:
        return collect_eval.collect_cases(self.payload, backend="claude", model="fixture-model", output_dir=self.output)

    def test_captured_rows_are_evaluated_without_answer_leakage(self) -> None:
        self.payload["cases"][0]["manual_review"].append("HIDDEN_REVIEW_CANARY")
        with patch.object(collect_eval, "call_agent", side_effect=self.reply):
            result, code = self.collect()
        self.assertEqual(code, 0)
        self.assertEqual(result["submitted_count"], 6)
        self.assertEqual(result["overall_status"], "manual_review_required")
        self.assertEqual(result["manual_pending_count"], 6)
        for prompt in self.prompts:
            self.assertNotIn("HIDDEN_REVIEW_CANARY", prompt)
            model_input = json.loads(prompt.split("本案例材料：\n", 1)[1])
            self.assertEqual(set(model_input), {"id", "title", "input"})
        captured = json.loads((self.output / "responses.json").read_text(encoding="utf-8"))
        self.assertEqual(captured["responses"], self.rows)

    def test_rule_violation_is_not_repaired_by_collector(self) -> None:
        self.rows[4]["metrics"]["net_return"] = 0.02
        with patch.object(collect_eval, "call_agent", side_effect=self.reply):
            result, code = self.collect()
        self.assertEqual(code, 1)
        self.assertEqual(result["rule_failed_count"], 1)
        self.assertFalse(result["results"][4]["checks"]["metric:net_return"])

    def test_unavailable_backend_stops_without_fake_answers(self) -> None:
        with patch.object(collect_eval, "call_agent", return_value={"status": "unavailable", "text": None, "model_called": False}) as call:
            result, code = self.collect()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(code, 2)
        self.assertEqual(result["submitted_count"], 0)
        self.assertFalse(result["agent_evaluated"])
        self.assertFalse(result["model_called"])
        self.assertNotIn("auto_pass_count", result)

    def test_wrong_case_and_invalid_json_remain_incomplete(self) -> None:
        self.rows[0]["case_id"] = "small_sample"
        original_reply = self.reply
        def response(prompt: str, **kwargs: object) -> dict:
            value = original_reply(prompt, **kwargs)
            if len(self.prompts) == 2:
                value["text"] = "```json\n{}\n```"
            return value
        with patch.object(collect_eval, "call_agent", side_effect=response):
            result, code = self.collect()
        self.assertEqual(code, 2)
        self.assertEqual(result["submitted_count"], 4)
        self.assertEqual(result["overall_status"], "incomplete")
        self.assertEqual(len(result["capture_errors"]), 2)
        self.assertEqual(result["missing_case_ids"], ["missing_data", "small_sample"])


if __name__ == "__main__":
    unittest.main()
