"""
factor_research_freeze_v3.py - Freeze Validation for Alpha040 Risk-Controlled v3

输出：output/factor_research_freeze_v3/
本模块只读本地历史缓存、Round6 输出和策略配置，不修改 market_scanner 主策略、不写台账、不连接实盘。
Freeze 阶段禁止根据结果继续优化阈值；本模块只复跑、对照和报告稳定性。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import factor_research_round3 as r3
import factor_research_round4 as r4
import factor_research_round5 as r5
import factor_research_round6 as r6
import market_scanner as scanner

DEFAULT_OUTPUT_DIR = Path("output/factor_research_freeze_v3")
DEFAULT_ROUND5_DIR = Path("output/factor_research_round5")
DEFAULT_ROUND6_DIR = Path("output/factor_research_round6")
FREEZE_VERSION = "v3_atr_risk_budget_hot5_vol_risk_on"
FIXED_HOT_5D_THRESHOLD = 0.2039
FIXED_VOLATILITY_THRESHOLD = 0.049826
REPRO_METRICS = [
    "event_trade_count",
    "accepted_trade_count",
    "ending_equity",
    "total_return",
    "max_drawdown",
    "sharpe",
    "calmar",
    "win_rate",
    "average_trade_return",
    "median_trade_return",
    "max_consecutive_losses",
    "stop_loss_count",
    "limit_down_blocked_exit_count",
    "positive_month_rate",
]


@dataclass(frozen=True)
class FreezeScenario:
    """Freeze 稳定性测试场景。"""

    key: str
    label: str
    fee_bps: float
    slippage_bps: float
    hot_5d_threshold: float
    volatility_threshold: float
    max_positions: int
    risk_budget_pct: float
    scenario_group: str


def _safe_float(value: Any) -> Optional[float]:
    """安全转换 float。"""
    return fr._safe_float(value)


def _fmt_pct(value: Any, digits: int = 2) -> str:
    """百分比格式化。"""
    return fr._fmt_pct(value, digits=digits)


def _json_default(value: Any) -> Any:
    """JSON 序列化辅助。"""
    return fr._json_default(value)


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    """写 CSV 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)


def _fixed_thresholds() -> dict[str, float]:
    """固定 v3 使用的 Round5 风险阈值。"""
    return {
        "change_rate_5d_q75": FIXED_HOT_5D_THRESHOLD,
        "volatility_20d_q75": FIXED_VOLATILITY_THRESHOLD,
    }


def _freeze_strategies() -> list[r6.ShadowStrategy]:
    """Freeze 复跑的三个固定对照版本。"""
    return [
        r6.ShadowStrategy(
            key="baseline_current_stop",
            label="Baseline：Alpha040 Core 当前止损",
            stop_variant="current_stop",
            only_risk_on=False,
            filter_hot_5d=False,
            filter_high_volatility=False,
        ),
        r6.ShadowStrategy(
            key="v2_atr_risk_budget_hot5_risk_on",
            label="版本2：ATR风控仓位 + 5日过热过滤 + 只开积极环境",
            stop_variant="atr_stop_risk_budget",
            only_risk_on=True,
            filter_hot_5d=True,
            filter_high_volatility=False,
        ),
        r6.ShadowStrategy(
            key=FREEZE_VERSION,
            label="冻结版本3：ATR风控仓位 + 5日过热过滤 + volatility_20d过滤 + 只开积极环境",
            stop_variant="atr_stop_risk_budget",
            only_risk_on=True,
            filter_hot_5d=True,
            filter_high_volatility=True,
        ),
    ]


def _freeze_config(output_dir: Path, strategy_path: str, round6_dir: Path) -> dict[str, Any]:
    """输出冻结规则配置。"""
    config = {
        "freeze_version": FREEZE_VERSION,
        "freeze_date": datetime.now().strftime("%Y-%m-%d"),
        "stage": "freeze_validation",
        "source_round6_report": str(round6_dir / "factor_research_round6_report.md"),
        "source_strategy_config": strategy_path,
        "rules": {
            "ranking": {
                "primary_factor": "alpha040",
                "auxiliary_factors": ["rps60", "close_to_20d_high"],
                "duplicate_weighting_forbidden": ["rps60/change_rate_60d", "rps20/change_rate_20d"],
            },
            "market_environment": {
                "open_only_when": "积极",
                "forbid_open_when": ["中性", "谨慎", "防守"],
            },
            "risk_filters": {
                "change_rate_5d_max": FIXED_HOT_5D_THRESHOLD,
                "volatility_20d_max": FIXED_VOLATILITY_THRESHOLD,
            },
            "risk_control": {
                "stop_type": "ATR stop",
                "position_sizing": "risk budget position",
                "risk_budget_account_pct_current_implementation": r4.DEFAULT_RISK_BUDGET_PCT,
                "note": "Freeze baseline follows the current Round6 implementation; sensitivity tests vary risk budget but do not change frozen rules.",
            },
            "trend": "weak filter only: close >= MA20, MA alignment small weight, distance penalty",
            "upper_shadow": "label only, not exclusion",
            "forbidden": ["no live trading connection", "no automatic order placement", "no threshold optimization during freeze"],
        },
        "data_boundary": "当前 universe 仍受本地缓存覆盖限制，不能视为完整全市场回测。",
    }
    _write_json(output_dir / "freeze_v3_strategy.json", config)
    return config


def _load_context(
    *,
    output_dir: Path,
    cache_dir: Path,
    max_symbols: Optional[int],
    min_bars: int,
    min_history: int,
    train_ratio: float,
    strategy_path: str,
) -> dict[str, Any]:
    """加载 Freeze Validation 上下文。"""
    scanner._set_active_config(scanner._load_scanner_config(strategy_path))
    histories = fr.load_cached_histories(cache_dir=cache_dir, max_symbols=max_symbols, min_bars=min_bars)
    if not histories:
        raise RuntimeError(f"未找到可用历史 K 线缓存：{cache_dir}")
    metadata = r3._load_scan_metadata()
    alpha040_map = r3._build_alpha040_map(histories)
    universe_by_date, daily_universe = r3._build_dynamic_universe(histories, metadata, alpha040_map, min_history, output_dir)
    active_dates = [date for date, rows in universe_by_date.items() if rows]
    train_end, test_start = fr._split_dates(pd.Series(pd.to_datetime(active_dates)), train_ratio=train_ratio)
    return {
        "histories": histories,
        "metadata": metadata,
        "universe_by_date": universe_by_date,
        "daily_universe": daily_universe,
        "active_dates": active_dates,
        "train_end": train_end,
        "test_start": test_start,
        "market_timeline": r3._market_timeline(),
        "max_hold_days": int(scanner._cfg("backtest", "max_hold_days", 5)),
        "entry_window_days": int(scanner._cfg("backtest", "entry_window_days", 2)),
        "initial_cash": float(scanner._cfg("backtest", "portfolio_initial_cash", 1_000_000)),
        "max_total_exposure": float(scanner._cfg("backtest", "portfolio_max_total_exposure_pct", 0.32)),
    }


def _accept_portfolio_trades(
    trades: pd.DataFrame,
    initial_cash: float,
    *,
    max_positions: int,
    max_total_exposure: float,
) -> pd.DataFrame:
    """按指定持仓数和总敞口接受事件交易。"""
    if trades.empty:
        return trades.copy()
    active: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for _, row in trades.sort_values(["entry_date", "rank_score"], ascending=[True, False]).iterrows():
        entry_date = str(row["entry_date"])
        active = [item for item in active if item["exit_date"] >= entry_date]
        current_exposure = sum(item["portfolio_position_pct"] for item in active)
        requested = (_safe_float(row.get("position_pct")) or 8.0) / 100
        actual = min(requested, max_total_exposure - current_exposure)
        if len(active) >= max_positions or actual <= 0:
            rows.append({**row.to_dict(), "portfolio_action": "skipped_capacity", "portfolio_position_pct": 0.0, "position_value": 0.0})
            continue
        accepted = {
            **row.to_dict(),
            "portfolio_action": "accepted",
            "portfolio_position_pct": round(actual, 6),
            "position_value": round(initial_cash * actual, 2),
        }
        rows.append(accepted)
        active.append({"exit_date": str(row["exit_date"]), "portfolio_position_pct": actual})
    return pd.DataFrame(rows)


def _run_strategy(
    strategy: r6.ShadowStrategy,
    *,
    context: dict[str, Any],
    thresholds: dict[str, float],
    fee_bps: float,
    slippage_bps: float,
    max_positions: int,
    risk_budget_pct: float,
    output_dir: Path,
    prefix: str,
    write_files: bool = True,
) -> dict[str, Any]:
    """运行单个策略或稳定性场景。"""
    daily_counts, candidates_by_strategy = r6._build_daily_candidate_counts(
        context["universe_by_date"],
        context["market_timeline"],
        thresholds,
    )
    old_risk_budget = r4.DEFAULT_RISK_BUDGET_PCT
    try:
        r4.DEFAULT_RISK_BUDGET_PCT = risk_budget_pct
        trades = r6._run_strategy_events(
            strategy,
            candidates_by_strategy[strategy.key],
            context["histories"],
            context["market_timeline"],
            context["train_end"],
            context["max_hold_days"],
            context["entry_window_days"],
            fee_bps,
            slippage_bps,
            r3.DEFAULT_LIMIT_THRESHOLD,
        )
    finally:
        r4.DEFAULT_RISK_BUDGET_PCT = old_risk_budget
    portfolio = _accept_portfolio_trades(
        trades,
        context["initial_cash"],
        max_positions=max_positions,
        max_total_exposure=context["max_total_exposure"],
    )
    equity, monthly, metrics = r4._portfolio_equity_curve(
        portfolio,
        context["histories"],
        context["active_dates"],
        context["initial_cash"],
    )
    accepted = portfolio[portfolio["portfolio_action"] == "accepted"].copy() if not portfolio.empty else pd.DataFrame()
    if write_files:
        _write_csv(output_dir / f"{prefix}_daily_candidate_counts.csv", daily_counts)
        _write_csv(output_dir / f"{prefix}_trades.csv", trades)
        _write_csv(output_dir / f"{prefix}_portfolio_trades.csv", portfolio)
        _write_csv(output_dir / f"{prefix}_accepted_trades.csv", accepted)
        _write_csv(output_dir / f"{prefix}_daily_equity.csv", equity)
        _write_csv(output_dir / f"{prefix}_monthly_returns.csv", monthly)
    return {
        "strategy": strategy,
        "daily_counts": daily_counts,
        "trades": trades,
        "portfolio": portfolio,
        "accepted": accepted,
        "equity": equity,
        "monthly": monthly,
        "metrics": metrics,
    }


def _summary_row(result: dict[str, Any], *, key: str, label: str, group: str) -> dict[str, Any]:
    """生成汇总行。"""
    metrics = result["metrics"]
    accepted = result["accepted"]
    stats = r6._trade_stats(accepted)
    monthly_stats = r6._monthly_stability(result["monthly"])
    return {
        "key": key,
        "label": label,
        "scenario_group": group,
        "event_trade_count": int(len(result["trades"])),
        "accepted_trade_count": int(metrics.get("accepted_trades") or 0),
        "ending_equity": metrics.get("ending_equity"),
        "total_return": metrics.get("total_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
        "calmar": metrics.get("calmar"),
        **stats,
        "positive_month_rate": monthly_stats.get("positive_month_rate"),
        "negative_month_count": monthly_stats.get("negative_month_count"),
        "worst_month": monthly_stats.get("worst_month"),
        "worst_month_return": monthly_stats.get("worst_month_return"),
        "monthly_return_std": monthly_stats.get("monthly_return_std"),
    }


def _rerun_baseline_v2_v3(context: dict[str, Any], output_dir: Path) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    """复跑 baseline、版本2、版本3。"""
    results: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for strategy in _freeze_strategies():
        result = _run_strategy(
            strategy,
            context=context,
            thresholds=_fixed_thresholds(),
            fee_bps=5.0,
            slippage_bps=10.0,
            max_positions=4,
            risk_budget_pct=r4.DEFAULT_RISK_BUDGET_PCT,
            output_dir=output_dir,
            prefix=f"rerun_{strategy.key}",
        )
        results[strategy.key] = result
        rows.append(_summary_row(result, key=strategy.key, label=strategy.label, group="freeze_rerun"))
    summary = pd.DataFrame(rows)
    _write_csv(output_dir / "freeze_rerun_summary.csv", summary)
    return results, summary


def _reproducibility_check(rerun_summary: pd.DataFrame, round6_dir: Path, output_dir: Path) -> pd.DataFrame:
    """与 Round6 结果比对，确认可复现。"""
    path = round6_dir / "strategy_comparison.csv"
    if not path.exists():
        result = pd.DataFrame([{"status": "missing_round6_comparison", "path": str(path)}])
        _write_csv(output_dir / "reproducibility_check.csv", result)
        return result
    old = pd.read_csv(path, encoding="utf-8-sig").rename(columns={"strategy_key": "key"})
    rows: list[dict[str, Any]] = []
    for _, new_row in rerun_summary.iterrows():
        key = new_row["key"]
        match = old[old["key"] == key]
        if match.empty:
            rows.append({"key": key, "metric": "all", "old_value": None, "new_value": None, "abs_diff": None, "match": False})
            continue
        old_row = match.iloc[0]
        for metric in REPRO_METRICS:
            old_value = _safe_float(old_row.get(metric))
            new_value = _safe_float(new_row.get(metric))
            if old_value is None and new_value is None:
                abs_diff = 0.0
                ok = True
            elif old_value is None or new_value is None:
                abs_diff = None
                ok = False
            else:
                abs_diff = abs(old_value - new_value)
                tolerance = 0.02 if metric == "ending_equity" else 1e-6
                ok = abs_diff <= tolerance
            rows.append(
                {
                    "key": key,
                    "metric": metric,
                    "old_value": old_value,
                    "new_value": new_value,
                    "abs_diff": abs_diff,
                    "match": bool(ok),
                }
            )
    check = pd.DataFrame(rows)
    _write_csv(output_dir / "reproducibility_check.csv", check)
    return check


def _stability_scenarios() -> list[FreezeScenario]:
    """Freeze 稳定性测试矩阵。"""
    base = {
        "fee_bps": 5.0,
        "slippage_bps": 10.0,
        "hot_5d_threshold": FIXED_HOT_5D_THRESHOLD,
        "volatility_threshold": FIXED_VOLATILITY_THRESHOLD,
        "max_positions": 4,
        "risk_budget_pct": r4.DEFAULT_RISK_BUDGET_PCT,
    }
    scenarios = [
        FreezeScenario("v3_frozen_base", "冻结 v3 基准", **base, scenario_group="base"),
        FreezeScenario("fee_double", "交易成本加倍", **{**base, "fee_bps": 10.0}, scenario_group="cost"),
        FreezeScenario("slippage_double", "滑点加倍", **{**base, "slippage_bps": 20.0}, scenario_group="slippage"),
    ]
    for value in [0.18, FIXED_HOT_5D_THRESHOLD, 0.22]:
        scenarios.append(
            FreezeScenario(
                f"hot5_{str(value).replace('.', '_')}",
                f"5日涨幅阈值 {value:.2%}",
                **{**base, "hot_5d_threshold": value},
                scenario_group="hot_5d_threshold",
            )
        )
    for value in [0.045, FIXED_VOLATILITY_THRESHOLD, 0.055]:
        scenarios.append(
            FreezeScenario(
                f"vol_{str(value).replace('.', '_')}",
                f"volatility_20d 阈值 {value:.2%}",
                **{**base, "volatility_threshold": value},
                scenario_group="volatility_threshold",
            )
        )
    for value in [2, 3, 4]:
        scenarios.append(
            FreezeScenario(
                f"max_positions_{value}",
                f"最大持仓数 {value}",
                **{**base, "max_positions": value},
                scenario_group="max_positions",
            )
        )
    for value in [0.005, 0.0075, 0.01]:
        scenarios.append(
            FreezeScenario(
                f"risk_budget_{str(value).replace('.', '_')}",
                f"单票风险预算 {value:.2%}",
                **{**base, "risk_budget_pct": value},
                scenario_group="risk_budget",
            )
        )
    return scenarios


def _run_stability_tests(context: dict[str, Any], output_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """运行固定 v3 稳定性测试。"""
    strategy = [item for item in _freeze_strategies() if item.key == FREEZE_VERSION][0]
    rows: list[dict[str, Any]] = []
    monthly_rows: list[dict[str, Any]] = []
    for scenario in _stability_scenarios():
        thresholds = {
            "change_rate_5d_q75": scenario.hot_5d_threshold,
            "volatility_20d_q75": scenario.volatility_threshold,
        }
        result = _run_strategy(
            strategy,
            context=context,
            thresholds=thresholds,
            fee_bps=scenario.fee_bps,
            slippage_bps=scenario.slippage_bps,
            max_positions=scenario.max_positions,
            risk_budget_pct=scenario.risk_budget_pct,
            output_dir=output_dir,
            prefix=f"stability_{scenario.key}",
            write_files=False,
        )
        row = _summary_row(result, key=scenario.key, label=scenario.label, group=scenario.scenario_group)
        row.update(
            {
                "fee_bps": scenario.fee_bps,
                "slippage_bps": scenario.slippage_bps,
                "hot_5d_threshold": scenario.hot_5d_threshold,
                "volatility_threshold": scenario.volatility_threshold,
                "max_positions": scenario.max_positions,
                "risk_budget_pct": scenario.risk_budget_pct,
            }
        )
        rows.append(row)
        monthly = result["monthly"].copy()
        if not monthly.empty:
            monthly["scenario_key"] = scenario.key
            monthly["scenario_label"] = scenario.label
            monthly["scenario_group"] = scenario.scenario_group
            monthly_rows.extend(monthly.to_dict("records"))
    summary = pd.DataFrame(rows)
    monthly_df = pd.DataFrame(monthly_rows)
    _write_csv(output_dir / "stability_test_summary.csv", summary)
    _write_csv(output_dir / "stability_monthly_returns.csv", monthly_df)
    return summary, monthly_df


def _history_coverage(histories: dict[str, list[dict]], output_dir: Path) -> dict[str, Any]:
    """判断是否具备按年份样本外切分条件。"""
    rows: list[dict[str, Any]] = []
    all_dates: list[str] = []
    for symbol, bars in histories.items():
        dates = [str(bar.get("date")) for bar in bars if bar.get("date")]
        if not dates:
            continue
        all_dates.extend(dates)
        rows.append({"symbol": symbol, "start_date": min(dates), "end_date": max(dates), "bar_count": len(dates)})
    coverage = pd.DataFrame(rows)
    _write_csv(output_dir / "history_coverage.csv", coverage)
    if not all_dates:
        payload = {"has_longer_history": False, "reason": "no_cached_dates"}
        _write_json(output_dir / "sample_out_validation_preparation.json", payload)
        return payload
    years = sorted({date[:4] for date in all_dates})
    span_days = (pd.to_datetime(max(all_dates)) - pd.to_datetime(min(all_dates))).days
    has_longer = len(years) >= 2 and span_days >= 365
    payload = {
        "has_longer_history": bool(has_longer),
        "min_date": min(all_dates),
        "max_date": max(all_dates),
        "span_days": int(span_days),
        "years": years,
        "decision": "year_split_available" if has_longer else "generate_forward_paper_trading_module",
    }
    _write_json(output_dir / "sample_out_validation_preparation.json", payload)
    return payload


def _forward_module_plan(output_dir: Path, coverage: dict[str, Any]) -> None:
    """输出 forward paper trading 准备说明。"""
    lines = [
        "# Forward Paper Trading Plan",
        "",
        "由于当前缓存历史不足以形成可靠按年份样本外验证，已生成 `src/forward_paper_trading_v3.py`。",
        "",
        "执行边界：",
        "- 从下一交易日起，只记录冻结 v3 信号、模拟触发区间和后续模拟成交状态。",
        "- 不根据 forward 结果修改阈值、因子、仓位或止损规则。",
        "- 不连接实盘接口，不自动下单。",
        "",
        "历史覆盖：",
        f"- 起始日期：{coverage.get('min_date')}",
        f"- 截止日期：{coverage.get('max_date')}",
        f"- 覆盖天数：{coverage.get('span_days')}",
    ]
    (output_dir / "forward_paper_trading_plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_report(
    output_dir: Path,
    *,
    freeze_config: dict[str, Any],
    rerun_summary: pd.DataFrame,
    repro_check: pd.DataFrame,
    stability: pd.DataFrame,
    coverage: dict[str, Any],
) -> str:
    """渲染 Freeze Validation 报告。"""
    repro_ok = bool(not repro_check.empty and repro_check["match"].fillna(False).all()) if "match" in repro_check.columns else False
    lines = [
        "# Freeze V3 Validation Report",
        "",
        f"生成日期：{datetime.now().strftime('%Y-%m-%d')}",
        "",
        "免责声明：本报告仅用于个人模拟交易、复盘和策略研究；不构成投资建议，不修改主 market_scanner 策略，不连接实盘接口。",
        "",
        "## 1. 冻结版本",
        "",
        f"- 固定策略版本：`{freeze_config['freeze_version']}`",
        "- 本阶段禁止继续调参、禁止新增因子、禁止修改版本3规则。",
        f"- 冻结配置：`freeze_v3_strategy.json`",
        "",
        "固定规则：alpha040 主排序；rps60、close_to_20d_high 辅助；只在积极环境开仓；5日涨幅 > 20.39% 剔除；volatility_20d > 4.98% 剔除；ATR 止损 + 风险预算仓位；趋势弱过滤；冲高回落只打标签。",
        "",
        "## 2. 复跑可复现性",
        "",
        f"- 与 Round6 对比结果：{'全部匹配' if repro_ok else '存在差异，见 reproducibility_check.csv'}",
        "",
        "| 版本 | 事件交易 | 接受交易 | 累计收益 | 最大回撤 | 夏普 | 卡玛 | 止损 | 跌停无法卖出 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in rerun_summary.iterrows():
        lines.append(
            f"| {row['label']} | {int(row['event_trade_count'])} | {int(row['accepted_trade_count'])} | "
            f"{_fmt_pct(row.get('total_return'))} | {_fmt_pct(row.get('max_drawdown'))} | "
            f"{row.get('sharpe')} | {row.get('calmar')} | {int(row.get('stop_loss_count') or 0)} | "
            f"{int(row.get('limit_down_blocked_exit_count') or 0)} |"
        )
    lines.extend(
        [
            "",
            "## 3. 稳定性测试",
            "",
            "以下为固定 v3 的压力测试，不用于选择新阈值。",
            "",
            "| 场景 | 分组 | 累计收益 | 最大回撤 | 夏普 | 卡玛 | 接受交易 | 最大连亏 | 止损 | 跌停无法卖出 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for _, row in stability.iterrows():
        lines.append(
            f"| {row['label']} | {row['scenario_group']} | {_fmt_pct(row.get('total_return'))} | "
            f"{_fmt_pct(row.get('max_drawdown'))} | {row.get('sharpe')} | {row.get('calmar')} | "
            f"{int(row.get('accepted_trade_count') or 0)} | {int(row.get('max_consecutive_losses') or 0)} | "
            f"{int(row.get('stop_loss_count') or 0)} | {int(row.get('limit_down_blocked_exit_count') or 0)} |"
        )
    lines.extend(
        [
            "",
            "## 4. 样本外验证准备",
            "",
            f"- 历史覆盖：{coverage.get('min_date')} 至 {coverage.get('max_date')}，约 {coverage.get('span_days')} 天。",
            f"- 判断：{coverage.get('decision')}",
        ]
    )
    if not coverage.get("has_longer_history"):
        lines.append("- 当前没有足够长历史做可靠年份切分，已生成 forward paper trading 模块：`src/forward_paper_trading_v3.py`。")
    lines.extend(
        [
            "",
            "## 5. 数据边界",
            "",
            "- 当前 universe 仍受本地缓存覆盖限制，不能视为完整全市场回测。",
            "- Freeze 结论只能说明当前缓存样本内的复现性与压力测试表现。",
            "",
            "## 6. 输出文件",
            "",
            "- `freeze_v3_strategy.json`",
            "- `freeze_rerun_summary.csv` / `reproducibility_check.csv`",
            "- `stability_test_summary.csv` / `stability_monthly_returns.csv`",
            "- `sample_out_validation_preparation.json` / `forward_paper_trading_plan.md`",
            "- `history_coverage.csv`",
        ]
    )
    report = "\n".join(lines) + "\n"
    (output_dir / "freeze_v3_validation_report.md").write_text(report, encoding="utf-8")
    return report


def run_freeze_v3_validation(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    round5_dir: Path = DEFAULT_ROUND5_DIR,
    round6_dir: Path = DEFAULT_ROUND6_DIR,
    cache_dir: Path = fr.DEFAULT_CACHE_DIR,
    max_symbols: Optional[int] = None,
    min_bars: int = 80,
    min_history: int = 60,
    train_ratio: float = 0.7,
    strategy_path: str = "strategy.json",
) -> dict[str, Any]:
    """运行 Freeze V3 Validation。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    freeze_config = _freeze_config(output_dir, strategy_path, round6_dir)
    context = _load_context(
        output_dir=output_dir,
        cache_dir=cache_dir,
        max_symbols=max_symbols,
        min_bars=min_bars,
        min_history=min_history,
        train_ratio=train_ratio,
        strategy_path=strategy_path,
    )
    _, rerun_summary = _rerun_baseline_v2_v3(context, output_dir)
    repro_check = _reproducibility_check(rerun_summary, round6_dir, output_dir)
    stability, stability_monthly = _run_stability_tests(context, output_dir)
    coverage = _history_coverage(context["histories"], output_dir)
    if not coverage.get("has_longer_history"):
        _forward_module_plan(output_dir, coverage)
    report = _render_report(
        output_dir,
        freeze_config=freeze_config,
        rerun_summary=rerun_summary,
        repro_check=repro_check,
        stability=stability,
        coverage=coverage,
    )
    summary = {
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "module": "freeze_v3_validation",
        "safety": "不修改主 market_scanner 策略、不写台账、不连接实盘接口。",
        "freeze_version": FREEZE_VERSION,
        "reproducible": bool("match" in repro_check.columns and repro_check["match"].fillna(False).all()),
        "has_longer_history": bool(coverage.get("has_longer_history")),
        "output_dir": str(output_dir),
        "report_path": str(output_dir / "freeze_v3_validation_report.md"),
        "report_characters": len(report),
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行 Freeze V3 Validation")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument("--round5-dir", default=str(DEFAULT_ROUND5_DIR), help="Round5 输出目录")
    parser.add_argument("--round6-dir", default=str(DEFAULT_ROUND6_DIR), help="Round6 输出目录")
    parser.add_argument("--cache-dir", default=str(fr.DEFAULT_CACHE_DIR), help="股票历史 K 缓存目录")
    parser.add_argument("--max-symbols", type=int, help="最多读取多少只股票缓存")
    parser.add_argument("--min-bars", type=int, default=80, help="单票最少 K 线数量")
    parser.add_argument("--min-history", type=int, default=60, help="动态 universe 最少历史长度")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="按时间顺序训练段比例")
    parser.add_argument("--strategy", default="strategy.json", help="策略配置文件")
    args = parser.parse_args()
    summary = run_freeze_v3_validation(
        output_dir=Path(args.output),
        round5_dir=Path(args.round5_dir),
        round6_dir=Path(args.round6_dir),
        cache_dir=Path(args.cache_dir),
        max_symbols=args.max_symbols,
        min_bars=args.min_bars,
        min_history=args.min_history,
        train_ratio=args.train_ratio,
        strategy_path=args.strategy,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
