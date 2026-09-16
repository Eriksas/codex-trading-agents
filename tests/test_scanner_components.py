"""与拆分前实现的固定输出对照；所有价格和交易均为合成测试数据。"""

from __future__ import annotations

import importlib
import ast
import copy
import hashlib
import inspect
import json
import sys
import tempfile
import unittest
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import market_scanner as scanner
from scanner_scenarios import capture, config_for, offline_clock
import scanner_data as data


class ScannerBaselineTests(unittest.TestCase):
    """比较输出字段、数值、台账和完整扫描产物。"""

    @classmethod
    def setUpClass(cls) -> None:
        """每个测试套件只运行一次完整合成场景。"""
        cls.expected = json.loads((ROOT / "tests/fixtures/scanner_baseline.json").read_text(encoding="utf-8"))["expected"]
        with tempfile.TemporaryDirectory() as temporary:
            cls.actual = capture(scanner, Path(temporary))

    def test_rules_and_trade_execution_match_baseline(self) -> None:
        self.maxDiff = 2000
        for strategy, values in self.expected["pure"].items():
            for key, expected in values.items():
                with self.subTest(strategy=strategy, field=key):
                    self.assertEqual(self.actual["pure"][strategy][key], expected)

    def test_ledger_transitions_and_repeat_match_baseline(self) -> None:
        self.maxDiff = 2000
        for key, expected in self.expected["ledger"].items():
            with self.subTest(field=key):
                self.assertEqual(self.actual["ledger"][key], expected)

    def test_complete_scan_outputs_match_baseline(self) -> None:
        self.maxDiff = 2000
        for mode in ("legacy_pipeline", "v3_pipeline", "empty_pipeline"):
            expected, actual = self.expected[mode], self.actual[mode]
            for key, value in expected["summary"].items():
                with self.subTest(mode=mode, summary=key):
                    self.assertEqual(actual["summary"][key], value)
            self.assertEqual(set(actual["artifact_sha256"]), set(expected["artifact_sha256"]))
            for path, digest in expected["artifact_sha256"].items():
                with self.subTest(mode=mode, artifact=path):
                    self.assertEqual(actual["artifact_sha256"][path], digest)

    def test_fixture_exercises_material_branches(self) -> None:
        for mode in ("legacy_pipeline", "v3_pipeline"):
            summary = self.actual[mode]["summary"]
            self.assertGreater(summary["candidate_count"], 0)
            self.assertGreater(summary["backtest_summary"]["trade_count"], 0)
            self.assertTrue(summary["shadow_experiments_summary"]["enabled"])
        actions = {row["ledger_action"] for row in self.actual["ledger"]["first_updates"]}
        self.assertTrue({"closed_stop_loss", "closed_take_profit", "closed_timeout", "expired_no_entry",
                         "missing_snapshot", "skipped_same_symbol_active"} <= actions)
        self.assertEqual(self.actual["ledger"]["second"]["closed_count"], 0)
        for values in self.actual["pure"].values():
            self.assertEqual(values["trades"]["both"]["exit_reason"], "stop_loss")
            self.assertIsNone(values["trades"]["no_entry"])
            self.assertGreater(values["portfolio"][1]["skipped_trades"], 0)

    def test_research_and_daily_consumers_still_import(self) -> None:
        sys.path.insert(0, str(ROOT / "research"))
        self.addCleanup(sys.path.remove, str(ROOT / "research"))
        for name in ("quant_core", "scheduled_v3_reporter", "forward_paper_trading_v3",
                     "factor_research", "factor_research_round6", "factor_research_freeze_v3"):
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_all_function_signatures_and_defaults_preserved(self) -> None:
        baseline = json.loads((ROOT / "tests/fixtures/scanner_baseline.json").read_text(encoding="utf-8"))
        for name, signature in baseline["function_signatures"].items():
            with self.subTest(function=name):
                self.assertEqual(str(inspect.signature(getattr(scanner, name))), signature)
        current_hash = hashlib.sha256(json.dumps(scanner.DEFAULT_SCANNER_CONFIG, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        self.assertEqual(current_hash, baseline["default_config_sha256"])

    def test_components_do_not_import_facade_or_research(self) -> None:
        graph: dict[str, set[str]] = {}
        for path in (ROOT / "src").glob("scanner_*.py"):
            imports: set[str] = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add(node.module or "")
                    if node.module is None:
                        imports.update(alias.name for alias in node.names)
            self.assertNotIn("market_scanner", imports)
            self.assertFalse(any(item.startswith("research") for item in imports))
            graph[path.stem] = {item for item in imports if item.startswith("scanner_")}
        def visit(name: str, active: set[str]) -> None:
            self.assertNotIn(name, active, f"循环依赖：{name}")
            for dependency in graph[name]:
                visit(dependency, active | {name})
        for name in graph:
            visit(name, set())


class ScannerBoundaryTests(unittest.TestCase):
    """覆盖缓存、数据源降级、异常传播与旧配置入口。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_config = scanner.ACTIVE_SCANNER_CONFIG
        self.addCleanup(scanner._set_active_config, self.old_config)
        scanner._set_active_config(config_for(scanner, "legacy_momentum_v1", self.root))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline_clock(scanner))
        self.bars = [{"date": "2026-03-30", "date_ms": 1774828800000, "open": 10,
                      "high": 11, "low": 9, "close": 10.5, "volume": 100, "turnover": 1000}]

    def test_config_switch_reaches_every_component(self) -> None:
        config = config_for(scanner, "alpha040_v3_risk_controlled", self.root)
        config["market_regimes"]["risk_on"]["min_score"] = 999
        config["trade_plan"]["base_position_pct"] = 3
        config["backtest"]["portfolio_max_positions"] = 1
        scanner._set_active_config(config)
        self.assertIs(scanner.ACTIVE_SCANNER_CONFIG, config)
        self.assertTrue(scanner._is_alpha040_v3_active())
        self.assertNotEqual(scanner._classify_market_regime(90)["regime_label"], "积极")
        self.assertEqual(scanner._run_portfolio_backtest([])[1]["max_positions"], 1)
        self.assertEqual(scanner._ledger_paths()[0].parent, self.root / "ledger")
        config["active_strategy"] = "legacy_momentum_v1"
        self.assertEqual(scanner._build_trade_plan({"latest": 100})["position_pct"], 3)
        self.assertFalse(scanner._is_alpha040_v3_active())

    def test_fresh_cache_and_utf8_roundtrip_avoid_requests(self) -> None:
        data._write_bars_cache("ohlcv", "DEMO.SH", self.bars)
        self.assertTrue(data._cache_path("ohlcv", "DEMO.SH").read_bytes().startswith(b"\xef\xbb\xbf"))
        with patch.object(data, "_fetch_fuyao_bars", side_effect=AssertionError("新鲜缓存不应请求网络")):
            self.assertEqual(data._fetch_fuyao_historical("DEMO.SH"), self.bars)

    def test_stale_cache_fallback_and_fail_closed_setting(self) -> None:
        old = [dict(self.bars[0], date="2026-01-01")]
        for kind, function in (("ohlcv", data._fetch_fuyao_historical), ("index", data._fetch_fuyao_index_historical)):
            data._write_bars_cache(kind, "DEMO", old)
            with self.subTest(kind=kind), patch.object(data, "_fetch_fuyao_bars", side_effect=RuntimeError("合成接口失败")):
                scanner.ACTIVE_SCANNER_CONFIG["data"]["stale_cache_allowed"] = True
                self.assertEqual(function("DEMO"), old)
                scanner.ACTIVE_SCANNER_CONFIG["data"]["stale_cache_allowed"] = False
                with self.assertRaises(RuntimeError):
                    function("DEMO")

    def test_incremental_cache_merge_and_disabled_write(self) -> None:
        old = dict(self.bars[0], date="2026-01-01", date_ms=1)
        fresh = [dict(old, close=12), dict(self.bars[0], date_ms=2)]
        data._write_bars_cache("ohlcv", "DEMO", [old])
        with patch.object(data, "_fetch_fuyao_bars", return_value=fresh):
            self.assertEqual(data._fetch_fuyao_historical("DEMO"), fresh)
        self.assertEqual(data._read_bars_cache("ohlcv", "DEMO"), fresh)
        scanner.ACTIVE_SCANNER_CONFIG["data"]["cache_enabled"] = False
        data._write_bars_cache("ohlcv", "NO_WRITE", self.bars)
        self.assertFalse(data._cache_path("ohlcv", "NO_WRITE").exists())

    def test_cache_skips_malformed_prices(self) -> None:
        path = data._cache_path("ohlcv", "BAD")
        path.parent.mkdir(parents=True)
        path.write_text("date,date_ms,open,high,low,close\n2026-03-30,1,10,11,9,bad\n", encoding="utf-8-sig")
        self.assertEqual(data._read_bars_cache("ohlcv", "BAD"), [])

    def test_provider_fallback_deduplicates_and_reports_index_failure(self) -> None:
        def quotes(order_by: str, pages: int) -> list[dict]:
            return [{"symkey": "DEMO", "latest": pages}]
        def index(subskill: str, args: list[str]) -> dict:
            if "000001.XSHG" in args:
                raise RuntimeError("合成指数缺失")
            return {"name": "合成指数"}
        with patch.object(data, "_fetch_market_snapshot_fuyao", side_effect=RuntimeError("合成主源失败")), \
             patch.object(data, "_fetch_quote_pages", side_effect=quotes), patch.object(data, "_run_ftshare", side_effect=index):
            stocks, indexes, source = data.fetch_market_snapshot()
        self.assertEqual(source, "ftshare")
        self.assertEqual(stocks, [{"symkey": "DEMO", "latest": 2}])
        self.assertEqual(len(indexes), 3)

    def test_ftshare_subprocess_errors_remain_visible(self) -> None:
        with patch.object(data, "_ftshare_runpy_path", return_value=Path("fixture.py")):
            with patch.object(data.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="synthetic failure", stdout="")):
                with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                    data._run_ftshare("fixture", [])
            with patch.object(data.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="", stdout="not json")):
                with self.assertRaises(json.JSONDecodeError):
                    data._run_ftshare("fixture", [])

    def test_review_history_failure_does_not_invent_bars(self) -> None:
        active = [{"symbol": "DEMO", "status": "open"}]
        with patch.object(scanner, "_fetch_fuyao_historical", side_effect=RuntimeError("synthetic failure")):
            self.assertEqual(scanner._ensure_review_histories(active, {}, "fuyao"), {"DEMO": []})
            self.assertEqual(scanner._ensure_review_histories(active, {}, "ftshare"), {})

    def test_steward_timeout_and_dry_push_are_mocked(self) -> None:
        with patch.object(scanner.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            summary = scanner._run_strategy_steward("2026-03-31", str(self.root), "daily", "fixture.sh", timeout_seconds=1)
            self.assertEqual(summary["status"], "failed")
            with self.assertRaises(RuntimeError):
                scanner._run_strategy_steward("2026-03-31", str(self.root), "daily", "fixture.sh", fail_on_error=True, timeout_seconds=1)
        import notifier
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({"market_scanner": scanner.ACTIVE_SCANNER_CONFIG}), encoding="utf-8")
        with patch.object(scanner, "fetch_market_snapshot", return_value=([], [], "ftshare")), \
             patch.object(notifier, "send_report", return_value=[SimpleNamespace(status="dry_run")]) as send:
            result = scanner.run_market_scan(str(self.root / "out"), "2026-03-31", config_path=str(config_path), push=True, push_dry_run=True)
        self.assertTrue(send.call_args.kwargs["dry_run"])
        self.assertEqual(result["push_results"], [{"status": "dry_run"}])

    def test_package_import_supports_legacy_helpers(self) -> None:
        package = importlib.import_module("src.market_scanner")
        previous = package.ACTIVE_SCANNER_CONFIG
        try:
            package._set_active_config(copy.deepcopy(scanner.DEFAULT_SCANNER_CONFIG))
            self.assertEqual(package._build_trade_plan({"latest": 100}), scanner._build_trade_plan({"latest": 100}))
        finally:
            package._set_active_config(previous)


if __name__ == "__main__":
    unittest.main()
