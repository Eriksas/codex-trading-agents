"""受控调用的合成测试：mock/本地假 CLI 均不是实际模型结果。"""

import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import agent_client, agent_workflow
from src.diagnosis import ROOT, run_diagnosis


class AgentClientTests(unittest.TestCase):
    """检查命令边界、stdin、原始记录和失败状态。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, **kwargs: object) -> dict:
        """调用客户端并让每项测试使用独立日志目录。"""
        return agent_client.call_agent("中文材料，$(not_a_command) `literal`", backend="claude", model="haiku",
                                       output_dir=self.root / "call", **kwargs)

    def available(self) -> object:
        """只模拟 CLI 的存在，不读取本机认证。"""
        return patch.object(agent_client, "executable_status", return_value={"status": "available", "executable": "fixture.exe"})

    def completed(self, command: list[str], **kwargs: object) -> SimpleNamespace:
        """输出明确标记为合成测试的成功信封。"""
        self.command, self.arguments = command, kwargs
        kwargs["stdout"].write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                            "result": '{"fixture": true}', "usage": {"output_tokens": 1}}))
        return SimpleNamespace(returncode=0)

    def test_fixed_flags_stdin_and_temporary_cwd(self) -> None:
        with self.available(), patch.object(agent_client.subprocess, "run", side_effect=self.completed):
            result = self.invoke()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["model_called"])
        self.assertFalse(self.arguments["shell"])
        self.assertNotIn(self.arguments["input"], self.command)
        self.assertEqual(self.command[self.command.index("--tools") + 1], "")
        self.assertEqual(self.command[self.command.index("--disallowed-tools") + 1], "*")
        self.assertIn("--safe-mode", self.command)
        self.assertIn("--strict-mcp-config", self.command)
        self.assertIn("--no-session-persistence", self.command)
        self.assertNotIn("--dangerously-skip-permissions", self.command)
        self.assertNotEqual(Path(self.arguments["cwd"]), ROOT)
        self.assertFalse(Path(self.arguments["cwd"]).exists())
        self.assertEqual((self.root / "call/response.txt").read_text(encoding="utf-8"), '{"fixture": true}')

    def test_missing_cli_does_not_start_subprocess(self) -> None:
        with patch.object(agent_client, "executable_status", return_value={"status": "unavailable", "reason": "fixture missing"}), \
             patch.object(agent_client.subprocess, "run") as run:
            result = self.invoke()
        run.assert_not_called()
        self.assertFalse(result["model_called"])
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue((self.root / "call/call.json").exists())

    def test_timeout_is_unknown_and_never_retried(self) -> None:
        with self.available(), patch.object(agent_client.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 1)) as run:
            result = self.invoke(timeout=1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(result["model_called"])

    def test_cli_error_does_not_echo_raw_stderr(self) -> None:
        def fail(command: list[str], **kwargs: object) -> SimpleNamespace:
            kwargs["stderr"].write("SYNTHETIC_PRIVATE_STDERR_MARKER")
            return SimpleNamespace(returncode=1)
        with self.available(), patch.object(agent_client.subprocess, "run", side_effect=fail):
            result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("SYNTHETIC_PRIVATE_STDERR_MARKER", json.dumps(result))
        self.assertIsNone(result["model_called"])

    def test_input_limit_and_invalid_model_fail_before_launch(self) -> None:
        with patch.object(agent_client, "MAX_PROMPT_BYTES", 1), patch.object(agent_client.subprocess, "run") as run:
            self.assertEqual(self.invoke()["status"], "input_too_large")
            run.assert_not_called()
        for model in ("--dangerously-skip-permissions", "a;echo b", "", None):
            with self.subTest(model=model), self.assertRaises(ValueError):
                agent_client.validate_settings("claude", model, 120)
        for timeout in (0, -1, 601, float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                agent_client.validate_settings("claude", "haiku", timeout)

    def test_invalid_envelope_is_not_a_success(self) -> None:
        def wrong(command: list[str], **kwargs: object) -> SimpleNamespace:
            kwargs["stdout"].write('{"result":"guessed output"}')
            return SimpleNamespace(returncode=0)
        with self.available(), patch.object(agent_client.subprocess, "run", side_effect=wrong):
            self.assertEqual(self.invoke()["status"], "invalid_envelope")

    def test_output_limit_does_not_accept_an_oversized_result(self) -> None:
        with self.available(), patch.object(agent_client, "MAX_OUTPUT_BYTES", 10), \
             patch.object(agent_client.subprocess, "run", side_effect=self.completed):
            result = self.invoke()
        self.assertEqual(result["status"], "output_too_large")
        self.assertIsNone(result["text"])

    def test_windows_shell_launcher_is_not_started(self) -> None:
        with patch.object(agent_client, "os", SimpleNamespace(name="nt", environ={})), \
             patch.object(agent_client.shutil, "which", return_value="claude.cmd"):
            self.assertEqual(agent_client.executable_status()["status"], "unsupported_launcher")

    def test_real_local_stub_process_roundtrips_unicode_without_shell(self) -> None:
        stub = self.root / "stub.py"
        stub.write_text('import sys,json\nsys.stdin.reconfigure(encoding="utf-8")\nsys.stdout.reconfigure(encoding="utf-8")\nprint(json.dumps({"type":"result","subtype":"success","is_error":False,"result":sys.stdin.read()},ensure_ascii=False))\n', encoding="utf-8")
        original_builder = agent_client.build_command
        def command(executable: str, model: str, path: Path) -> list[str]:
            return [sys.executable, str(stub), *original_builder("unused", model, path)[1:]]
        with self.available(), patch.object(agent_client, "build_command", side_effect=command):
            result = self.invoke()
        self.assertEqual(result["text"], "中文材料，$(not_a_command) `literal`")
        self.assertEqual(result["prompt_sha256"], hashlib.sha256(result["text"].encode("utf-8")).hexdigest())


class AnalysisAgentTests(unittest.TestCase):
    """验证两阶段依赖与 Python 数值保护。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.options = dict(input_root=ROOT / "examples/diagnosis/reviews", archive_path=ROOT / "examples/diagnosis/trade_archive.csv",
                            as_of_date="2026-01-16", source_kind="synthetic_demo", output_root=self.root / "runs")
        self.base, _ = run_diagnosis(**self.options)
        self.facts = json.loads(Path(self.base["report_path"]).with_name("facts.json").read_text(encoding="utf-8"))
        self.phase = 0

    def response(self, prompt: str, **kwargs: object) -> dict:
        self.phase += 1
        self.assertNotIn(str(ROOT), prompt)
        if self.phase == 1:
            value = {"facts_sha256": self.base["facts_sha256"], "hypotheses": [{"claim": "样本可能不足以反映其他日期。",
                     "evidence_ids": ["python_metrics"], "counterevidence": "只有两条归档", "next_check": "补充更多日期再分组", "confidence": "low"}]}
        else:
            self.assertIn("样本可能不足以反映其他日期", prompt)
            value = {"facts_sha256": self.base["facts_sha256"], "critic": ["合成样例不构成市场证据，当前无法验证机制。"]}
        return {"status": "completed", "text": json.dumps(value, ensure_ascii=False), "model_called": True}

    def test_two_calls_finish_but_strategy_stays_inconclusive(self) -> None:
        with patch.object(agent_workflow, "call_agent", side_effect=self.response) as call:
            result, code = run_diagnosis(**self.options, agent_backend="claude", agent_model="haiku")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(code, 0)
        self.assertEqual(result["interpretation"]["status"], "generated_manual_review_pending")
        actual = json.loads(Path(result["report_path"]).with_name("facts.json").read_text(encoding="utf-8"))
        self.assertEqual(actual, self.facts)
        self.assertEqual(actual["strategy_conclusion"], "Inconclusive")
        self.assertEqual(result["human_decision"], "pending")

    def test_bad_first_answer_stops_before_critic(self) -> None:
        with patch.object(agent_workflow, "call_agent", return_value={"status": "completed", "text": '{"bad":"response"}', "model_called": True}) as call:
            result, code = run_diagnosis(**self.options, agent_backend="claude", agent_model="haiku")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(code, 2)
        self.assertEqual(result["interpretation"]["status"], "rejected")

    def test_critic_failure_keeps_raw_python_results(self) -> None:
        def reply(prompt: str, **kwargs: object) -> dict:
            return self.response(prompt, **kwargs) if self.phase == 0 else {"status": "timeout", "text": None, "model_called": None}
        with patch.object(agent_workflow, "call_agent", side_effect=reply):
            result, code = run_diagnosis(**self.options, agent_backend="claude", agent_model="haiku")
        self.assertEqual(code, 2)
        self.assertTrue(result["model_called"])
        self.assertEqual(len(result["agent_calls"]), 2)
        self.assertIn("-1.00%", Path(result["report_path"]).read_text(encoding="utf-8"))

    def test_empty_data_and_default_path_do_not_call_model(self) -> None:
        with patch.object(agent_workflow, "call_agent") as call:
            run_diagnosis(**self.options)
            options = dict(self.options, input_root=self.root / "missing", archive_path=self.root / "absent.csv")
            result, code = run_diagnosis(**options, agent_backend="claude", agent_model="haiku")
        call.assert_not_called()
        self.assertEqual(code, 0)
        self.assertFalse(result["model_called"])
        self.assertEqual(result["interpretation"]["status"], "skipped_data_unavailable")

    def test_explicit_model_and_exclusive_mode_required(self) -> None:
        for options in ({"agent_backend": "claude"}, {"agent_model": "haiku"},
                        {"agent_backend": "claude", "agent_model": "haiku", "interpretation_path": Path("unused")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                run_diagnosis(**self.options, **options)


class SynthesisAgentTests(unittest.TestCase):
    """原日报可选 LLM 只改文字，调用失败仍交原模板兜底。"""

    def setUp(self) -> None:
        root_logger = logging.getLogger()
        previous_handlers, previous_level = list(root_logger.handlers), root_logger.level
        import main_v2
        for handler in list(root_logger.handlers):
            if handler not in previous_handlers:
                root_logger.removeHandler(handler)
                handler.close()
        root_logger.setLevel(previous_level)
        self.main = main_v2
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "DEMO.json"
        self.original = {"stock_code": "DEMO", "data_quality": "partial", "technical": {"indicators": {"ma20": 100}},
                         "fundamental": {}, "synthesis": "pending_llm_interpretation"}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.words = "数据显示，当前材料包含部分技术指标，基本面信息存在缺口。描述范围限于已有字段，仍需核对完整日期与数据来源。单次观察无法判断策略稳定性，后续需要积累更多样本并进行独立验证。"

    def test_program_only_writes_synthesis(self) -> None:
        with patch.object(agent_client, "call_agent", return_value={"status": "completed", "model_called": True,
             "text": json.dumps({"code": "DEMO", "synthesis": self.words}, ensure_ascii=False)}):
            result = self.main.run_synthesis_agent("DEMO", "教学样例", self.path)
        self.assertEqual(result["status"], "success")
        actual = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(actual, {**self.original, "synthesis": self.words})

    def test_model_cannot_return_numeric_overrides(self) -> None:
        before = self.path.read_bytes()
        with patch.object(agent_client, "call_agent", return_value={"status": "completed", "model_called": True,
             "text": json.dumps({"code": "DEMO", "synthesis": self.words, "technical": {"ma20": 200}})}):
            result = self.main.run_synthesis_agent("DEMO", "教学样例", self.path)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_input_change_is_not_overwritten(self) -> None:
        def response(*args: object, **kwargs: object) -> dict:
            self.path.write_text('{"changed_by_owner":true}', encoding="utf-8")
            return {"status": "completed", "model_called": True, "text": json.dumps({"code": "DEMO", "synthesis": self.words})}
        with patch.object(agent_client, "call_agent", side_effect=response):
            result = self.main.run_synthesis_agent("DEMO", "教学样例", self.path)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"changed_by_owner": True})

    def test_failed_data_uses_template_without_cli(self) -> None:
        self.original["data_quality"] = "failed"
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        with patch.object(agent_client, "call_agent") as call:
            result = self.main.run_synthesis_agent("DEMO", "教学样例", self.path)
        call.assert_not_called()
        self.assertEqual(result["status"], "success_template_fallback")
        self.assertFalse(result["model_called"])

    def test_pipeline_does_not_fallback_over_concurrently_changed_input(self) -> None:
        base = Path(self.temp.name)
        watchlist = base / "watchlist.json"
        watchlist.write_text('{"stocks":[{"code":"DEMO","name":"教学样例"}]}', encoding="utf-8")
        self.path = base / "output/2026-01-16/analysis/DEMO.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        def response(*args: object, **kwargs: object) -> dict:
            self.path.write_text('{"changed_by_owner":true}', encoding="utf-8")
            return {"status": "completed", "model_called": True, "text": json.dumps({"code": "DEMO", "synthesis": self.words})}
        fallback = Mock(side_effect=AssertionError("不能由模板覆盖已变化的输入"))
        modules = {
            "data_fetcher": SimpleNamespace(run_data_fetch=lambda **kwargs: [{"data_quality": "partial"}]),
            "analyzer": SimpleNamespace(run_analysis=lambda **kwargs: [{"data_quality": "partial"}]),
            "selector": SimpleNamespace(run_selection=lambda **kwargs: {"total": 1, "passed": 0, "path": "fixture", "markdown_path": "fixture"}),
            "reporter": SimpleNamespace(generate_report=lambda **kwargs: None),
            "synthesizer": SimpleNamespace(synthesize_file=fallback),
        }
        with patch.dict(sys.modules, modules), patch.object(sys, "path", list(sys.path)), \
             patch.object(agent_client, "call_agent", side_effect=response):
            result = self.main.run_daily_pipeline(date="2026-01-16", watchlist_path=str(watchlist),
                                                 output_base=str(base / "output"), use_llm_synthesis=True, serial=True)
        fallback.assert_not_called()
        self.assertTrue(result["step3_synthesis"][0]["skip_template_fallback"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"changed_by_owner": True})


if __name__ == "__main__":
    unittest.main()
