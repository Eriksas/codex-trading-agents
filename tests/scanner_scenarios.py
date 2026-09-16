"""扫描器重构的固定合成输入；不代表行情、实验收益或模型评测。"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator
from unittest.mock import patch


class FixedClock(datetime):
    """固定产物时间，便于跨机器比较。"""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        """返回合成案例时间。"""
        return cls(2026, 3, 31, 18, 0, tzinfo=tz)

    @classmethod
    def today(cls) -> datetime:
        """返回固定日期。"""
        return cls.now()


@contextmanager
def offline_clock(scanner: ModuleType) -> Iterator[None]:
    """只在测试中冻结各函数所属模块时钟，禁止真实联网。"""
    import reviewer
    import strategy_health
    import strategy_learning

    spaces = {id(func.__globals__): func.__globals__ for func in vars(scanner).values()
              if callable(func) and hasattr(func, "__globals__")}
    for module in (reviewer, strategy_health, strategy_learning):
        spaces[id(vars(module))] = vars(module)
    with ExitStack() as stack:
        for space in spaces.values():
            if "datetime" in space:
                stack.enter_context(patch.dict(space, {"datetime": FixedClock}))
        stack.enter_context(patch("socket.socket", side_effect=AssertionError("扫描器回归禁止联网")))
        yield


def sample_market() -> tuple[list[dict], list[dict], dict[str, list[dict]]]:
    """构造含涨跌日的 90 根教学 K 线及三个虚构标的。"""
    histories: dict[str, list[dict]] = {}
    stocks = []
    for number in range(3):
        symbol = f"DEMO{number}.SH"
        bars = []
        for index in range(90):
            close = 100 + index * 0.45 + (index % 5) * 0.25 + number
            bars.append({"date": (datetime(2026, 1, 1) + timedelta(days=index)).strftime("%Y-%m-%d"),
                         "date_ms": 1767225600000 + index * 86400000,
                         "open": close - 0.1, "high": close + 1.0, "low": close - 1.0,
                         "close": close, "volume": 10000 + index * 10, "turnover": 15e9 + index * 1e7})
        histories[symbol] = bars
        stocks.append({"symkey": symbol, "name": f"合成样例{number}", "latest": bars[-1]["close"],
                       "open": bars[-1]["open"], "high": bars[-1]["high"], "low": bars[-1]["low"],
                       "change_rate": 0.01 if number < 2 else 0.3, "turnover": bars[-1]["turnover"],
                       "market_cap_total": 3e11, "turnover_rate": 0.03, "amplitude": 0.025,
                       "trading_status": "NORMAL", "data_source": "fuyao",
                       "industry_sector": {"name": f"合成行业{number}"}})
    indexes = [{"label": "合成指数", "latest": 200, "change_rate": 0.015,
                "change_rate_5d": 0.04, "change_rate_20d": 0.1, "change_rate_60d": 0.2,
                "above_ma20": True, "above_ma60": True, "volatility_20d": 0.01,
                "index_history_enriched": True, "_history_bars": copy.deepcopy(histories["DEMO0.SH"])}]
    return stocks, indexes, histories


def config_for(scanner: ModuleType, strategy: str, root: Path) -> dict:
    """仅为固定输入指定临时路径，不改仓库策略配置。"""
    config = copy.deepcopy(scanner.DEFAULT_SCANNER_CONFIG)
    config["active_strategy"] = strategy
    config["ledger"]["dir"] = str(root / "ledger")
    config["data"]["cache_dir"] = str(root / "cache")
    config["strategy_steward"]["enabled"] = False
    return config


def normalized(value: Any, root: Path) -> Any:
    """仅归一临时目录和路径分隔符；不改数字、字段或时间。"""
    if isinstance(value, dict):
        return {key: normalized(item, root) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalized(item, root) for item in value]
    if isinstance(value, str):
        return value.replace(str(root), "<RUN>").replace("\\", "/")
    return value


def pure_results(scanner: ModuleType, root: Path) -> dict:
    """覆盖两套规则、状态切换、止损优先、未触发和组合容量。"""
    results: dict[str, Any] = {}
    for strategy in ("legacy_momentum_v1", "alpha040_v3_risk_controlled"):
        scanner._set_active_config(config_for(scanner, strategy, root))
        stocks, indexes, histories = sample_market()
        for row in stocks:
            scanner._enrich_with_history(row, histories[row["symkey"]])
        scored = scanner._apply_strategy_overlays([scanner._score_stock(row) for row in stocks], histories)
        profile = scanner._build_market_profile(indexes, scored)
        candidates = scanner._select_candidates(scored, 5, profile)
        signal = dict(scored[0], latest=100, atr14=3)
        plan = scanner._build_trade_plan(signal)
        low, high = scanner._parse_trigger_zone(plan["trigger_zone"])
        trades = {}
        for mode in ("both", "profit", "timeout", "no_entry"):
            next_bar = {"date": "2026-03-30", "open": 100, "close": 100.5,
                        "high": plan["first_take_profit"] + 1 if mode in {"both", "profit"} else high,
                        "low": plan["stop_loss"] - 1 if mode == "both" else low + 0.1}
            if mode == "no_entry":
                next_bar.update(open=200, low=199, high=201, close=200)
            bars = [{"date": "2026-03-29"}, next_bar, dict(next_bar, date="2026-03-31")]
            trades[mode] = scanner._simulate_trade(signal, bars, 0, max_hold_days=2)
        populated = [dict(trade, symbol=f"DEMO{i}.SH") for i, trade in enumerate(trades.values()) if trade]
        # 同日入场超过四个持仓上限，触发容量分支。
        overloaded = populated + [dict(populated[0], symbol=f"EXTRA{i}.SH") for i in range(5)]
        overloaded = [dict(trade, exit_date="2026-04-03") for trade in overloaded]
        shadow_input = [dict(populated[0], signal_date="2026-01-05", symbol="DEMO0.SH")]
        results[strategy] = {"scored": scored, "profile": profile, "candidates": candidates,
                             "defensive_candidates": scanner._select_candidates(scored, 5, dict(profile, regime_label="防守")),
                             "plan": plan, "trades": trades,
                             "summary": scanner._summarize_backtest(populated, 3, 15),
                             "portfolio": scanner._run_portfolio_backtest(overloaded),
                             "shadow": scanner._run_shadow_experiments(shadow_input, histories, 15, 5)}
    return normalized(results, root)


def ledger_results(scanner: ModuleType, root: Path) -> dict:
    """覆盖 pending/open、止损/止盈/到期、同标的跳过与重复运行。"""
    scanner._set_active_config(config_for(scanner, "legacy_momentum_v1", root))
    out = root / "output"
    out.mkdir(parents=True, exist_ok=True)
    candidates = [scanner._build_trade_plan({"symbol": name, "name": name, "latest": 100,
                                            "amplitude": 0.05, "score": 100})
                  for name in ("STOP", "PROFIT", "TIMEOUT", "EXPIRED", "MISSING", "OVERLAP")]
    scanner._update_trade_ledger(candidates, [], "2026-03-29", out)
    pending, archive = scanner._ledger_paths()
    rows = scanner._read_csv_rows(pending)
    for row in rows:
        if row["symbol"] in {"TIMEOUT", "MISSING", "OVERLAP"}:
            row.update(status="open", entry_date="2026-03-29", entry_price="100")
        if row["symbol"] == "TIMEOUT":
            row["entry_date"] = "2026-03-20"
        if row["symbol"] == "EXPIRED":
            row["signal_date"] = "2026-03-20"
    scanner._write_csv(pending, rows, list(rows[0]))
    stocks = [{"symkey": name, "open": 100, "latest": 100.5,
               "low": 90 if name == "STOP" else 99, "high": 110 if name in {"STOP", "PROFIT"} else 101}
              for name in ("STOP", "PROFIT", "TIMEOUT", "OVERLAP")]
    first = scanner._update_trade_ledger([copy.deepcopy(candidates[-1])], stocks, "2026-03-31", out)
    first_updates = scanner._read_csv_rows(out / "ledger_updates.csv")
    second = scanner._update_trade_ledger([], stocks, "2026-03-31", out)
    return normalized({"first": first, "first_updates": first_updates, "second": second,
                       "pending": scanner._read_csv_rows(pending), "archive": scanner._read_csv_rows(archive)}, root)


def pipeline_results(scanner: ModuleType, root: Path, strategy: str, empty: bool = False) -> dict:
    """实际运行完整扫描编排，网络输入由固定样例替代，所有输出在临时目录。"""
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "strategy.json"
    config_path.write_text(json.dumps({"market_scanner": config_for(scanner, strategy, root)}), encoding="utf-8")
    stocks, indexes, histories = sample_market()
    if empty:
        stocks, indexes, histories = [], [], {}
    source = "ftshare" if empty else "fuyao"
    with patch.object(scanner, "fetch_market_snapshot", return_value=(stocks, indexes, source)), \
         patch.object(scanner, "_fetch_fuyao_historical", side_effect=lambda symbol: copy.deepcopy(histories[symbol])), \
         patch("subprocess.run", side_effect=AssertionError("禁止外部程序或模型调用")):
        result = scanner.run_market_scan(output_base=str(root / "output"), date="2026-03-31", config_path=str(config_path))
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == config_path:
            continue
        raw = path.read_text(encoding="utf-8-sig")
        if path.suffix == ".json":
            raw = json.dumps(normalized(json.loads(raw), root), ensure_ascii=False, sort_keys=True)
        else:
            raw = normalized(raw, root)
        files[path.relative_to(root).as_posix()] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {"summary": normalized(result, root), "artifact_sha256": files}


def capture(scanner: ModuleType, root: Path) -> dict:
    """采集全部基线场景，恢复配置，供对照测试使用。"""
    previous = scanner.ACTIVE_SCANNER_CONFIG
    try:
        with offline_clock(scanner):
            return {"pure": pure_results(scanner, root), "ledger": ledger_results(scanner, root / "ledger_case"),
                    "legacy_pipeline": pipeline_results(scanner, root / "legacy", "legacy_momentum_v1"),
                    "v3_pipeline": pipeline_results(scanner, root / "v3", "alpha040_v3_risk_controlled"),
                    "empty_pipeline": pipeline_results(scanner, root / "empty", "alpha040_v3_risk_controlled", empty=True)}
    finally:
        scanner._set_active_config(previous)
