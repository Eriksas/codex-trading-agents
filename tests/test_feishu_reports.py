"""飞书报告可读性与证据边界测试；数据完全合成，不发送消息。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from research import forward_daily_report as observation
from src import notifier
from src import scheduled_v3_reporter as v3


def write_fixture(root: Path, relative: str, rows: list[dict]) -> None:
    """写入独立临时目录的合成台账。"""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def sample_state(root: Path) -> None:
    """两个记录日，用来区分今日、上一记录日与季度频率。"""
    write_fixture(root, "bounce/events_log.csv", [
        {"date": "2026-09-14", "ret5": -0.06, "ret1": 0.01, "ret60": 0.03, "fired": True},
        {"date": "2026-09-16", "ret5": -0.01, "ret1": 0.01, "ret60": 0.03, "fired": False},
    ])
    write_fixture(root, "bounce/signals_ledger.csv", [
        {"signal_date": "2026-09-14", "status": "holding"},
        {"signal_date": "2026-09-14", "status": "pending_entry_next_open"},
    ])
    write_fixture(root, "gates/gate_states.csv", [
        {"date": "2026-09-14", "g2_trend_vote": 0.25, "g3_dvol_trend": 0.2, "g4_dd_ladder": 0.5},
        {"date": "2026-09-16", "g2_trend_vote": 0.5, "g3_dvol_trend": 0.3, "g4_dd_ladder": 0.5},
    ])
    write_fixture(root, "thermometer/thermometer_ledger.csv", [
        {"date": "2026-09-16", "limit_up_pool_n": 30, "max_streak": 3, "streak2_n": 5, "seal_money_sum": 12.3, "late_seal_n": 2},
    ])
    write_fixture(root, "cb/double_low_ledger.csv", [
        {"date": "2026-09-16", "rank": 1, "名称": "合成转债", "双低": 120.5, "cb_below_100_n": 0, "cb_dlow_median": 125.0},
    ])
    write_fixture(root, "divlv/divlv_ledger.csv", [
        {"date": "2026-07-01", "quarter": "2026Q3", "code": "DEMO"},
    ])


class FeishuReportTests(unittest.TestCase):
    """覆盖事实与解释分层、日期/分母、缺失值和最终推送转换。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        sample_state(self.root)

    def test_forward_report_explains_numbers_without_causal_claims(self) -> None:
        before = {path: path.read_bytes() for path in self.root.rglob("*.csv")}
        with patch("socket.socket", side_effect=AssertionError("预览不得联网")):
            report = observation.build_report(self.root, "2026-09-16")
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertIn("4.00 个百分点", report)
        self.assertIn("2026-09-14 的 25.0% 变化 +25.0 个百分点", report)
        self.assertIn("累计记录 1 个触发日期", report)
        self.assertIn("模拟持有 1 条，等待次日入场 1 条", report)
        self.assertIn("不是实际账户持仓", report)
        self.assertIn("低于 100 元的有 0 只", report)
        self.assertIn("不计算低价券占比", report)
        self.assertIn("不能直接写成因果关系", report)
        self.assertLess(len(report), 3500)
        for header in ("今日结论", "事实依据", "如何理解", "还不能判断", "下一步", "数据来源"):
            self.assertIn("## " + header, report)

    def test_stale_records_are_not_reported_as_today(self) -> None:
        report = observation.build_report(self.root, "2026-09-17")
        self.assertIn("4 条日频观察线未提供 2026-09-17", report)
        self.assertIn("不把旧记录当成今日新信号", report)
        self.assertIn("季度更新，不要求每天换名单", report)
        self.assertNotIn("前日", report)

    def test_future_and_duplicate_dates_do_not_inflate_samples(self) -> None:
        path = self.root / "gates/gate_states.csv"
        frame = pd.read_csv(path, encoding="utf-8-sig")
        extra = pd.DataFrame([
            {"date": "2026-09-16", "g2_trend_vote": 0.7, "g3_dvol_trend": 0.3, "g4_dd_ladder": 0.5},
            {"date": "2026-09-20", "g2_trend_vote": 0.99, "g3_dvol_trend": 0.3, "g4_dd_ladder": 0.5},
        ])
        pd.concat([frame, extra]).to_csv(path, index=False, encoding="utf-8-sig")
        report = observation.build_report(self.root, "2026-09-16")
        self.assertIn("已有 2 个不同日期的记录", report)
        self.assertIn("还差 58 个记录日", report)
        self.assertIn("趋势投票（G2）：70.0%", report)
        self.assertNotIn("99.0%", report)
        self.assertIn("存在晚于截止日的记录", report)
        self.assertIn("存在重复日期", report)

    def test_missing_data_and_nonfinite_values_are_not_zero(self) -> None:
        write_fixture(self.root, "thermometer/thermometer_ledger.csv", [{"date": "2026-09-16", "limit_up_pool_n": float("nan")}])
        report = observation.build_report(self.root, "2026-09-16")
        self.assertIn("涨停池有 缺失 只", report)
        self.assertNotIn("nan", report.lower())
        empty = observation.build_report(self.root / "missing", "2026-09-16")
        self.assertIn("未取得可读台账", empty)
        self.assertIn("无记录", empty)
        self.assertNotIn("累计记录 0 个触发日期", empty)

    def test_v3_non_active_environment_explains_rule_not_prediction(self) -> None:
        report = v3.render_v3_report("2026-09-16", {"regime_label": "谨慎", "score": 40, "above_ma20_ratio": 0.25, "avg_index_change": -0.01}, [], {"market_not_risk_on": 7}, 12, 7, 70)
        self.assertIn("尚未满足", report)
        self.assertIn("环境条件未满足 7 只", report)
        self.assertIn("首个未通过条件", report)
        self.assertIn("不是对明日涨跌的预测", report)
        self.assertNotIn("market_not_risk_on", report)
        self.assertNotIn("No Trade Report", report)
        self.assertIn("环境规则得分 40.00", report)
        self.assertIn("原定门槛为 70.00 分", report)
        self.assertIn("占 25.00%", report)

    def test_v3_unknown_environment_is_data_insufficiency(self) -> None:
        report = v3.render_v3_report("2026-09-16", {}, [], {}, 0, 0)
        self.assertIn("市场环境数据不足", report)
        self.assertNotIn("环境被固定规则归为", report)

    def test_v3_candidate_parameters_are_presented_as_simulation(self) -> None:
        item = {"name": "合成股票", "symbol": "DEMO", "alpha040": 1.234, "rps60": 0.8, "close_to_20d_high": -0.02}
        plan = {"trigger_zone": "98.00-101.00", "stop_loss": 95, "first_take_profit": 108, "position_pct": 4}
        with patch.object(v3.scanner, "_build_trade_plan", return_value=plan) as build:
            report = v3.render_v3_report("2026-09-16", {"regime_label": "积极"}, [item], {"passed": 1}, 10, 5)
        build.assert_called_once_with(item)
        self.assertIn("98.00-101.00", report)
        self.assertIn("模拟仓位 4.00%", report)
        self.assertIn("不代表账户已持有", report)
        self.assertIn("距近 20 日高点 -2.00%", report)
        self.assertIn("相对强弱分位 80.00%（不是胜率）", report)

    def test_v3_generation_keeps_existing_summary_contract(self) -> None:
        with patch.object(v3.scanner, "_load_scanner_config", return_value={}), \
             patch.object(v3.scanner, "_set_active_config"), \
             patch.object(v3.forward, "_load_freeze_config", return_value={}), \
             patch.object(v3.forward, "_freeze_thresholds", return_value={}), \
             patch.object(v3.qc, "load_cached_histories", return_value={}), \
             patch.object(v3.qc, "_load_scan_metadata", return_value={}), \
             patch.object(v3.qc, "_build_alpha040_map", return_value={}), \
             patch.object(v3.qc, "_build_dynamic_universe", return_value=({"2026-09-16": [{}]}, [])), \
             patch.object(v3.qc, "_market_timeline", return_value={"2026-09-16": {"regime_label": "谨慎"}}), \
             patch.object(v3.qc, "_eligible_for_strategy", return_value=([], {"market_not_risk_on": 1})):
            summary = v3.generate_v3_daily_report(date="2026-09-16", output_base=self.root / "out")
        self.assertEqual(set(summary), {"date", "report_path", "market_regime_label", "candidate_count", "no_trade_report"})
        self.assertEqual(summary["candidate_count"], 0)
        self.assertTrue(summary["no_trade_report"])
        self.assertTrue(Path(summary["report_path"]).is_file())

    def test_digest_and_feishu_post_preserve_reasoning_sections(self) -> None:
        reports = [observation.build_report(self.root, "2026-09-16"),
                   v3.render_v3_report("2026-09-16", {"regime_label": "谨慎"}, [], {"market_not_risk_on": 1}, 2, 1)]
        for index, report in enumerate(reports):
            path = self.root / f"report{index}.md"
            path.write_text(report, encoding="utf-8")
            with patch.object(notifier, "_post_json", side_effect=AssertionError("不得发送消息")):
                title, content = notifier.build_push_content(path, mode="digest")
                payload = notifier._build_feishu_post(title, content)
            flat = json.dumps(payload, ensure_ascii=False)
            for section in ("今日结论", "事实依据", "还不能判断", "下一步"):
                self.assertIn(section, flat)
            self.assertNotIn("完整报告路径", content)
            self.assertNotIn("<RUN>", flat)


if __name__ == "__main__":
    unittest.main()
