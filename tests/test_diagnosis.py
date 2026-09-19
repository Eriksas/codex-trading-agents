"""第一阶段主流程测试；所有输入都是合成材料，不调用模型或网络。"""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.diagnosis import ROOT, run_diagnosis


class DiagnosisTests(unittest.TestCase):
    """检查计算口径、输入保护、失败结果和导入回答边界。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.samples = self.root / "samples"
        shutil.copytree(ROOT / "examples/diagnosis", self.samples)
        self.reviews = self.samples / "reviews"
        self.archive = self.samples / "trade_archive.csv"
        self.review = self.reviews / "2026-01-16/scan/review_details.csv"

    def run_case(self, **kwargs: object) -> tuple[dict, dict, int]:
        """运行临时目录诊断并读取实际文件输出。"""
        options = dict(input_root=self.reviews, archive_path=self.archive, as_of_date="2026-01-16",
                       output_root=self.root / "runs", source_kind="synthetic_demo")
        options.update(kwargs)
        result, code = run_diagnosis(**options)
        facts = json.loads(Path(result["report_path"]).with_name("facts.json").read_text(encoding="utf-8"))
        return result, facts, code

    def test_demo_calculation_and_input_preservation(self) -> None:
        before = {p: p.read_bytes() for p in self.samples.rglob("*") if p.is_file()}
        with patch("socket.socket", side_effect=AssertionError("禁止网络")), patch("subprocess.run", side_effect=AssertionError("禁止模型调用")):
            result, facts, code = self.run_case()
        self.assertEqual(code, 0)
        self.assertEqual(facts["windows"][0]["reviewed_count"], 3)
        self.assertEqual(facts["windows"][0]["trigger_rate"], 0.66667)
        self.assertEqual(facts["windows"][0]["realized_win_rate"], 0.5)
        self.assertEqual(facts["numeric_check"]["value"], -0.01)
        self.assertEqual(facts["numeric_check"]["result"], "Rejected")
        self.assertEqual(facts["strategy_conclusion"], "Inconclusive")
        self.assertFalse(result["model_called"])
        self.assertEqual(result["interpretation"]["status"], "not_requested")
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertIn("合成教学示例", Path(result["report_path"]).read_text(encoding="utf-8"))

    def test_repeat_has_stable_facts_and_separate_outputs(self) -> None:
        first, _, _ = self.run_case()
        second, _, _ = self.run_case()
        self.assertEqual(first["facts_sha256"], second["facts_sha256"])
        self.assertNotEqual(first["report_path"], second["report_path"])
        raw = Path(first["report_path"]).with_name("facts.json").read_bytes()
        self.assertEqual(first["facts_sha256"], hashlib.sha256(raw).hexdigest())

    def test_concise_report_preserves_evidence_and_links_to_full_audit(self) -> None:
        result, facts, _ = self.run_case()
        path = Path(result["report_path"])
        report = path.read_text(encoding="utf-8")
        self.assertLess(report.index("## 结论"), report.index("## 数据与指标"))
        self.assertIn("有效归档 2 条，平均净收益 -1.00%", report)
        self.assertIn("Inconclusive（证据不足）", report)
        self.assertIn("均值不是组合收益", report)
        self.assertIn("本次未调用或导入", report)
        for issue in facts["quality"]["warnings"] + facts["quality"]["errors"]:
            self.assertIn(issue, report)
        for filename in ("facts.json", "result.json"):
            self.assertIn(f"]({filename})", report)
            self.assertTrue(path.with_name(filename).is_file())
        for source in facts["sources"]:
            self.assertEqual(len(source["sha256"]), 64)
            self.assertTrue(Path(source["path"]).is_file())
            self.assertNotIn(source["sha256"], report)

    def test_missing_inputs_produce_insufficient_report(self) -> None:
        result, facts, code = self.run_case(input_root=self.root / "absent", archive_path=self.root / "missing.csv")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(facts["windows"], [])
        self.assertIsNone(facts["numeric_check"]["value"])
        self.assertTrue(facts["quality"]["warnings"])
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertNotIn("| 窗口 |", report)
        self.assertIn("无可用统计，缺失未按零处理", report)

    def test_missing_archive_does_not_turn_return_into_zero(self) -> None:
        _, facts, code = self.run_case(archive_path=self.root / "absent.csv")
        self.assertEqual(code, 0)
        self.assertEqual(facts["windows"][0]["reviewed_count"], 3)
        self.assertIsNone(facts["windows"][0]["avg_net_return"])

    def test_malformed_csv_duplicate_and_nonfinite_block_calculation(self) -> None:
        original = self.review.read_text(encoding="utf-8")
        versions = [original + original.splitlines()[1] + "\n",
                    original.replace("max_adverse_pct", "status"),
                    original.replace(",0.01,", ",NaN,"),
                    original.replace("stopped", "unknown_status"),
                    original.replace("2026-01-12", "2026-01-17"),
                    original.replace("-0.04,0.01,-0.05", "-0.04,0.01"),
                    "trade_id\nonly_id\n"]
        for content in versions:
            with self.subTest(content=content[:60]):
                self.review.write_text(content, encoding="utf-8")
                result, facts, code = self.run_case()
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "input_error")
                self.assertEqual(facts["windows"], [])
                self.assertTrue(facts["quality"]["errors"])

    def test_dates_and_latest_state_match_existing_health_rules(self) -> None:
        # 次日复盘取最新状态；未来目录与未来退出不进入截至当日的统计。
        later = self.reviews / "2026-01-17/scan/review_details.csv"
        later.parent.mkdir(parents=True)
        later.write_text("trade_id,review_date,status,data_quality\nDEMO_A,2026-01-17,active_open,complete\n", encoding="utf-8")
        with self.archive.open("a", encoding="utf-8") as file:
            file.write("FUTURE,2026-01-18,1.0,合成环境\n")
        _, old, _ = self.run_case()
        _, current, _ = self.run_case(as_of_date="2026-01-17")
        self.assertEqual(old["windows"][0]["stop_loss_rate"], 0.33333)
        self.assertEqual(current["windows"][0]["stop_loss_rate"], 0)
        self.assertEqual(current["windows"][0]["reviewed_count"], 3)
        self.assertEqual(current["numeric_check"]["value"], -0.01)

    def make_interpretation(self, result: dict) -> Path:
        """构造明确手写的回答夹具，测试导入流程。"""
        self.answer = {"facts_sha256": result["facts_sha256"], "model": "handwritten",
                       "hypotheses": [{"claim": "少量亏损记录可能集中于同一天。", "evidence_ids": ["python_metrics"],
                                       "counterevidence": "只有两条有效归档，不能判断稳定性。", "next_check": "收集更多日期后按日分组验证。", "confidence": "low"}],
                       "critic": ["同日样本不独立，当前数据不足以判断原因。"]}
        self.answer_path = self.root / "answer.json"
        self.answer_path.write_text(json.dumps(self.answer, ensure_ascii=False), encoding="utf-8")
        return self.answer_path

    def test_imported_answer_cannot_replace_python_conclusion(self) -> None:
        first, _, _ = self.run_case()
        path = self.make_interpretation(first)
        result, facts, code = self.run_case(interpretation_path=path)
        self.assertEqual(code, 0)
        self.assertEqual(result["interpretation"]["status"], "imported_manual_review_pending")
        self.assertEqual(facts["strategy_conclusion"], "Inconclusive")
        self.assertEqual(facts["numeric_check"]["value"], -0.01)
        self.assertIn("handwritten", Path(result["report_path"]).read_text(encoding="utf-8"))

    def test_stale_answer_wrong_source_high_confidence_and_overrides_rejected(self) -> None:
        result, _, _ = self.run_case()
        for change in ("stale", "source", "confidence", "override", "invalid_json"):
            with self.subTest(change=change):
                path = self.make_interpretation(result)
                if change == "stale":
                    self.answer["facts_sha256"] = "expired"
                elif change == "source":
                    self.answer["hypotheses"][0]["evidence_ids"] = ["invented"]
                elif change == "confidence":
                    self.answer["hypotheses"][0]["confidence"] = "high"
                elif change == "override":
                    self.answer["metrics"] = {"avg_net_return": 1.0}
                path.write_text("not json" if change == "invalid_json" else json.dumps(self.answer), encoding="utf-8")
                outcome, facts, code = self.run_case(interpretation_path=path)
                self.assertEqual(code, 2)
                self.assertEqual(outcome["interpretation"]["status"], "rejected")
                self.assertEqual(facts["numeric_check"]["value"], -0.01)

    def test_invalid_date_fails_before_creating_run(self) -> None:
        for value in ("2026-02-30", "20260116", "../scan"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_case(as_of_date=value)
        self.assertFalse((self.root / "runs").exists())

    def test_cli_is_independent_of_current_directory(self) -> None:
        proc = subprocess.run([sys.executable, str(ROOT / "main.py"), "diagnose", "--date", "invalid"],
                              cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
