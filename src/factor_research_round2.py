"""
factor_research_round2.py - 第二轮因子研究审计与影子实验

输出：output/factor_research_round2/
本模块只读历史缓存和策略配置，不修改 market scanner 主策略、不写台账、不连接实盘。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import market_scanner as scanner

DEFAULT_OUTPUT_DIR = Path("output/factor_research_round2")


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
    """写 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)


def _build_research_context(
    *,
    cache_dir: Path,
    index_cache_dir: Path,
    max_symbols: Optional[int],
    min_bars: int,
    min_history: int,
    label_horizon: int,
    min_cross_section: int,
    groups: int,
    train_ratio: float,
    strategy_path: str,
) -> dict[str, Any]:
    """构建第二轮研究所需上下文。"""
    config = scanner._load_scanner_config(strategy_path)
    scanner._set_active_config(config)
    histories = fr.load_cached_histories(cache_dir=cache_dir, max_symbols=max_symbols, min_bars=min_bars)
    if not histories:
        raise RuntimeError(f"未找到可用历史 K 线缓存：{cache_dir}")

    max_hold_days = int(scanner._cfg("backtest", "max_hold_days", 5))
    entry_window_days = int(scanner._cfg("backtest", "entry_window_days", 2))
    cost_bps = float(scanner._cfg("backtest", "cost_bps", 15))
    dataset, rows_by_date = fr._build_factor_dataset(
        histories,
        label_horizon=label_horizon,
        min_history=min_history,
        max_hold_days=max_hold_days,
        entry_window_days=entry_window_days,
    )
    if dataset.empty:
        raise RuntimeError("因子数据集为空，无法进行第二轮审计")

    dataset["strength_zone_proximity"] = -(
        pd.to_numeric(dataset["close_to_20d_high"], errors="coerce") + 0.015
    ).abs()
    dataset = fr._cross_section_preprocess(dataset, [*fr.FACTOR_COLUMNS, "strength_zone_proximity"])
    train_end, test_start = fr._split_dates(dataset["date"], train_ratio=train_ratio)
    ic_daily, ic_summary = fr._compute_ic_tables(dataset, fr.FACTOR_COLUMNS, min_cross_section, train_end)
    group_returns, group_summary = fr._compute_group_returns(
        dataset,
        fr.FACTOR_COLUMNS,
        groups,
        min_cross_section,
        train_end,
    )
    corr_matrix = fr._factor_correlation_matrix(dataset, fr.FACTOR_COLUMNS)
    market_timeline = fr._market_timeline_from_cache(index_cache_dir)
    return {
        "config": config,
        "histories": histories,
        "dataset": dataset,
        "rows_by_date": rows_by_date,
        "train_end": train_end,
        "test_start": test_start,
        "ic_daily": ic_daily,
        "ic_summary": ic_summary,
        "group_returns": group_returns,
        "group_summary": group_summary,
        "corr_matrix": corr_matrix,
        "market_timeline": market_timeline,
        "max_hold_days": max_hold_days,
        "entry_window_days": entry_window_days,
        "cost_bps": cost_bps,
    }


def _run_universe_audit(
    histories: dict[str, list[dict]],
    dataset: pd.DataFrame,
    rows_by_date: dict[str, list[dict]],
    output_dir: Path,
) -> tuple[pd.DataFrame, str]:
    """审计研究股票池是否存在后验偏差。"""
    panel = fr._bars_to_panel(histories)
    first_last_rows = []
    for symbol, bars in histories.items():
        first_last_rows.append(
            {
                "symbol": symbol,
                "first_bar_date": bars[0].get("date") if bars else "",
                "last_bar_date": bars[-1].get("date") if bars else "",
                "bar_count": len(bars),
            }
        )
    symbol_ranges = pd.DataFrame(first_last_rows)

    available_counts = panel.groupby("date")["symbol"].nunique().rename("symbols_with_bar").reset_index()
    factor_counts = dataset.groupby("date")["symbol"].nunique().rename("symbols_with_valid_factor_label").reset_index()
    signal_counts = pd.DataFrame(
        [{"date": pd.to_datetime(date), "signal_row_count": len(items)} for date, items in rows_by_date.items()]
    )
    daily = available_counts.merge(factor_counts, on="date", how="left").merge(signal_counts, on="date", how="left")
    daily["symbols_with_valid_factor_label"] = daily["symbols_with_valid_factor_label"].fillna(0).astype(int)
    daily["signal_row_count"] = daily["signal_row_count"].fillna(0).astype(int)
    daily["factor_values_asof_only"] = True
    daily["future_label_used_only_for_evaluation"] = True
    daily["universe_membership_asof_only"] = False
    daily["post_hoc_universe_bias_risk"] = True
    daily["reason"] = "股票池来自当前本地历史缓存文件集合，缓存文件由近期扫描补齐形成，不是逐日历史全市场成分。"
    daily = daily.sort_values("date")
    daily["date"] = daily["date"].dt.strftime("%Y-%m-%d")
    _write_csv(output_dir / "universe_daily_audit.csv", daily)
    _write_csv(output_dir / "universe_symbol_ranges.csv", symbol_ranges)

    earliest = daily["date"].min()
    latest = daily["date"].max()
    min_count = int(daily["symbols_with_bar"].min()) if not daily.empty else 0
    max_count = int(daily["symbols_with_bar"].max()) if not daily.empty else 0
    stable_full_count_days = int((daily["symbols_with_bar"] == len(histories)).sum()) if not daily.empty else 0
    md = "\n".join(
        [
            "# Universe Audit",
            "",
            f"生成日期：{datetime.now().strftime('%Y-%m-%d')}",
            "",
            "## 结论",
            "",
            "- **存在后验股票池偏差风险。** 当前 222 只股票来自 `data/cache/market_scanner/ohlcv/` 中已经存在的缓存文件。",
            "- 因子计算本身只使用信号日及以前的 OHLCV；未来 5 日收益只作为标签，不参与因子值。",
            "- 但股票池成员资格不是按每个历史交易日当时可获得的全市场列表重建，而是由当前缓存中有哪些股票决定。",
            "- 因此，本轮 IC、RankIC、分组收益和影子回测应理解为“当前缓存研究池”的证据，不能直接外推为全 A 历史全市场证据。",
            "",
            "## 每日可用性",
            "",
            f"- 审计区间：{earliest} 至 {latest}",
            f"- 缓存股票数：{len(histories)}",
            f"- 每日有 K 线的股票数范围：{min_count} 至 {max_count}",
            f"- 覆盖全部缓存股票的交易日数：{stable_full_count_days}",
            f"- 每日明细：`output/factor_research_round2/universe_daily_audit.csv`",
            "",
            "## 是否只使用当时可获得信息",
            "",
            "| 项目 | 结论 | 说明 |",
            "|---|---|---|",
            "| 因子值 | 是 | `_signal_row_from_bar` 只截取 `bars[:idx+1]`，滚动均线/波动/量能使用信号日及以前数据。 |",
            "| Alpha191 子集 | 是 | 每只股票按时间序列滚动计算，未使用未来价格。 |",
            "| 标签 | 否，但仅用于评估 | 标签是未来 5 日超额收益，只在 IC/分组收益/报告阶段使用。 |",
            "| 股票池成员 | 否 | 成员来自当前缓存文件集合，缓存生成过程受近期扫描/补齐影响，不是历史逐日全市场可交易列表。 |",
            "",
            "## 改进建议",
            "",
            "- 下一阶段应保存每日全市场快照或至少保存每日 scanner 初筛 universe，避免用当前缓存倒推历史股票池。",
            "- IC 与分组收益报告必须继续标注“研究池横截面”，直到每日全市场 universe 可回放。",
            "- 不应把本轮结果直接提升为主策略参数；只能作为 shadow evidence。",
        ]
    ) + "\n"
    audit_path = output_dir / "universe_audit.md"
    audit_path.write_text(md, encoding="utf-8")
    return daily, md


def _top_abs_correlations(corr_matrix: pd.DataFrame, output_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """输出绝对相关性最高的因子对和重点重复因子检查。"""
    rows: list[dict] = []
    factors = list(corr_matrix.columns)
    for idx, left in enumerate(factors):
        for right in factors[idx + 1:]:
            corr = _safe_float(corr_matrix.loc[left, right])
            if corr is None:
                continue
            rows.append(
                {
                    "factor_left": left,
                    "factor_right": right,
                    "correlation": corr,
                    "abs_correlation": abs(corr),
                }
            )
    top = pd.DataFrame(rows).sort_values("abs_correlation", ascending=False).head(20)
    _write_csv(output_dir / "top_abs_correlations.csv", top)

    watch_pairs = [
        ("rps60", "change_rate_60d"),
        ("rps20", "change_rate_20d"),
        ("volatility_20d", "amplitude"),
        ("log_turnover", "market_cap"),
        ("trend_ma5_ma10", "close_to_20d_high"),
        ("trend_close_ma20", "close_to_20d_high"),
    ]
    watch_rows = []
    for left, right in watch_pairs:
        if left not in corr_matrix.index or right not in corr_matrix.columns:
            watch_rows.append(
                {
                    "factor_left": left,
                    "factor_right": right,
                    "correlation": None,
                    "abs_correlation": None,
                    "status": "missing",
                    "note": "因子不在当前研究数据集中，无法计算；market_cap 目前未随历史缓存保存。",
                }
            )
            continue
        corr = _safe_float(corr_matrix.loc[left, right])
        watch_rows.append(
            {
                "factor_left": left,
                "factor_right": right,
                "correlation": corr,
                "abs_correlation": abs(corr) if corr is not None else None,
                "status": "high_duplicate" if corr is not None and abs(corr) >= 0.85 else "ok",
                "note": "绝对相关 >= 0.85 视为高度重复，需要避免重复加权。" if corr is not None and abs(corr) >= 0.85 else "",
            }
        )
    watch = pd.DataFrame(watch_rows)
    _write_csv(output_dir / "watched_factor_correlations.csv", watch)
    return top, watch


def _metric_lookup(summary: pd.DataFrame, metric: str, split: str) -> dict[str, dict]:
    """把 IC 汇总转为按因子索引的字典。"""
    view = summary[(summary["metric"] == metric) & (summary["split"] == split)] if not summary.empty else pd.DataFrame()
    return {str(row["factor"]): row.to_dict() for _, row in view.iterrows()}


def _group_lookup(group_summary: pd.DataFrame, split: str) -> dict[str, dict]:
    """把分组收益汇总转为按因子索引的字典。"""
    view = group_summary[group_summary["split"] == split] if not group_summary.empty else pd.DataFrame()
    return {str(row["factor"]): row.to_dict() for _, row in view.iterrows()}


def _classify_factor(train_rankic: Optional[float], test_rankic: Optional[float], train_group: Optional[float], test_group: Optional[float]) -> str:
    """基于 train/test RankIC 和分组收益粗分类。"""
    train_rankic = train_rankic if train_rankic is not None else 0.0
    test_rankic = test_rankic if test_rankic is not None else 0.0
    train_group = train_group if train_group is not None else 0.0
    test_group = test_group if test_group is not None else 0.0
    same_sign = train_rankic == 0 or test_rankic == 0 or (train_rankic > 0) == (test_rankic > 0)
    group_same_sign = train_group == 0 or test_group == 0 or (train_group > 0) == (test_group > 0)
    if abs(train_rankic) >= 0.05 and abs(test_rankic) >= 0.05 and same_sign and group_same_sign:
        return "stable_factor"
    if abs(train_rankic) >= 0.05 and (abs(test_rankic) < 0.02 or not same_sign):
        return "train_effective_test_failed"
    if abs(test_rankic) >= 0.05 and (abs(train_rankic) < 0.02 or abs(test_rankic) > max(abs(train_rankic) * 2.0, 0.02)):
        return "test_accidental_enhanced"
    return "mixed_or_weak"


def _train_test_factor_comparison(ic_summary: pd.DataFrame, group_summary: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """输出 train/test IC、RankIC、分组收益对比表。"""
    train_ic = _metric_lookup(ic_summary, "ic", "train")
    test_ic = _metric_lookup(ic_summary, "ic", "test")
    train_rank = _metric_lookup(ic_summary, "rank_ic", "train")
    test_rank = _metric_lookup(ic_summary, "rank_ic", "test")
    train_group = _group_lookup(group_summary, "train")
    test_group = _group_lookup(group_summary, "test")

    rows = []
    for factor in fr.FACTOR_COLUMNS:
        train_rankic = _safe_float((train_rank.get(factor) or {}).get("mean"))
        test_rankic = _safe_float((test_rank.get(factor) or {}).get("mean"))
        train_group_ls = _safe_float((train_group.get(factor) or {}).get("long_short_mean"))
        test_group_ls = _safe_float((test_group.get(factor) or {}).get("long_short_mean"))
        rows.append(
            {
                "factor": factor,
                "train_ic": _safe_float((train_ic.get(factor) or {}).get("mean")),
                "test_ic": _safe_float((test_ic.get(factor) or {}).get("mean")),
                "train_rank_ic": train_rankic,
                "test_rank_ic": test_rankic,
                "train_group_q5_q1": train_group_ls,
                "test_group_q5_q1": test_group_ls,
                "train_rank_ic_positive_rate": _safe_float((train_rank.get(factor) or {}).get("positive_rate")),
                "test_rank_ic_positive_rate": _safe_float((test_rank.get(factor) or {}).get("positive_rate")),
                "classification": _classify_factor(train_rankic, test_rankic, train_group_ls, test_group_ls),
            }
        )
    comparison = pd.DataFrame(rows)
    comparison["abs_test_rank_ic"] = comparison["test_rank_ic"].abs()
    comparison = comparison.sort_values(["classification", "abs_test_rank_ic"], ascending=[True, False])
    _write_csv(output_dir / "train_test_factor_comparison.csv", comparison)
    return comparison


def _label_map(dataset: pd.DataFrame, column: str = "future_5d_excess_return") -> dict[tuple[str, str], float]:
    """生成 date/symbol 到标签或因子值的映射。"""
    result: dict[tuple[str, str], float] = {}
    for _, row in dataset[["date", "symbol", column]].dropna().iterrows():
        result[(row["date"].strftime("%Y-%m-%d"), str(row["symbol"]))] = float(row[column])
    return result


def _factor_value_map(dataset: pd.DataFrame, columns: list[str]) -> dict[str, dict[tuple[str, str], float]]:
    """生成多个标准化因子值映射。"""
    result: dict[str, dict[tuple[str, str], float]] = {}
    for column in columns:
        result[column] = _label_map(dataset, column=column)
    return result


def _split_for_date(date: str, train_end: pd.Timestamp) -> str:
    """根据日期返回 train/test。"""
    return "train" if pd.to_datetime(date) <= train_end else "test"


def _summarize_by_split(trades: list[dict], experiment: str, label: str) -> list[dict]:
    """按 all/train/test 汇总交易。"""
    rows = []
    for split in ["all", "train", "test"]:
        split_trades = trades if split == "all" else [trade for trade in trades if trade.get("split") == split]
        rows.append({"experiment": experiment, "experiment_label": label, "split": split, **fr._summarize_trades(split_trades)})
    return rows


def _current_component_eligible(item: dict) -> bool:
    """当前因子组合影子基线的入选条件。"""
    flags = item.get("_component_flags") or {}
    return fr._hard_research_pass(item) and all(flags.get(key) for key in fr.ABLATION_COMPONENTS)


def _ranked_backtest(
    *,
    histories: dict[str, list[dict]],
    rows_by_date: dict[str, list[dict]],
    market_timeline: dict[str, dict],
    train_end: pd.Timestamp,
    experiment: str,
    label: str,
    score_func: Callable[[dict], Optional[float]],
    eligible_func: Callable[[dict], bool],
    min_base_score: float,
    max_hold_days: int,
    entry_window_days: int,
    cost_bps: float,
    simulate_func: Optional[Callable[[dict, list[dict], int, Optional[dict]], Optional[dict]]] = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """按每日横截面排序并回放影子交易。"""
    trades: list[dict] = []
    selected_rows: list[dict] = []
    next_available_by_symbol: dict[str, str] = {}
    for date in sorted(rows_by_date):
        market_profile = market_timeline.get(date) if market_timeline else None
        candidate_limit = int((market_profile or {}).get("candidate_limit") or 5)
        eligible = []
        for item in rows_by_date[date]:
            base_score = _safe_float(item.get("score")) or 0.0
            if base_score < min_base_score or not eligible_func(item):
                continue
            score = score_func(item)
            if score is None or math.isnan(score):
                continue
            eligible.append({**item, "_round2_rank_score": score})
        eligible.sort(key=lambda item: item["_round2_rank_score"], reverse=True)
        for item in eligible[:candidate_limit]:
            symbol = str(item.get("_symbol_key") or item.get("symbol") or "")
            if not symbol or next_available_by_symbol.get(symbol, "") >= date:
                continue
            bars = histories.get(symbol) or []
            signal_idx = int(item.get("_signal_idx"))
            selected_rows.append(
                {
                    "experiment": experiment,
                    "experiment_label": label,
                    "signal_date": date,
                    "symbol": item.get("symbol"),
                    "rank_score": item.get("_round2_rank_score"),
                    "base_score": item.get("score"),
                    "final_score": item.get("final_score"),
                    "market_regime": (market_profile or {}).get("regime"),
                    "market_regime_label": (market_profile or {}).get("regime_label"),
                    "upper_shadow_ratio": item.get("_upper_shadow_ratio"),
                }
            )
            if simulate_func:
                trade = simulate_func(item, bars, signal_idx, market_profile)
            else:
                trade = scanner._simulate_trade(
                    item,
                    bars,
                    signal_idx,
                    market_profile=market_profile,
                    max_hold_days=max_hold_days,
                    entry_window_days=entry_window_days,
                    cost_bps=cost_bps,
                )
            if trade:
                if trade.get("exit_date"):
                    next_available_by_symbol[symbol] = str(trade["exit_date"])
                split = _split_for_date(str(trade.get("signal_date")), train_end)
                trades.append(
                    {
                        "experiment": experiment,
                        "experiment_label": label,
                        "split": split,
                        "rank_score": item.get("_round2_rank_score"),
                        **{key: value for key, value in trade.items() if not str(key).startswith("_")},
                    }
                )
    return trades, selected_rows, _summarize_by_split(trades, experiment, label)


def _simulate_with_time_stop(
    *,
    signal: dict,
    bars: list[dict],
    signal_idx: int,
    market_profile: Optional[dict],
    max_hold_days: int,
    entry_window_days: int,
    cost_bps: float,
    rule: str,
) -> Optional[dict]:
    """带条件时间止损的交易回放。"""
    stop_slippage_bps = float(scanner._cfg("backtest", "stop_slippage_bps", 30))
    plan = scanner._build_trade_plan(signal)
    if market_profile:
        plan = scanner._apply_market_risk_controls([plan], market_profile)[0]
    trigger_low, trigger_high = scanner._parse_trigger_zone(plan["trigger_zone"])
    entry_idx: Optional[int] = None
    entry_price: Optional[float] = None
    for idx in range(signal_idx + 1, min(signal_idx + 1 + entry_window_days, len(bars))):
        price = scanner._entry_price_for_bar(bars[idx], trigger_low, trigger_high)
        if price is not None:
            entry_idx = idx
            entry_price = price
            break
    if entry_idx is None or entry_price is None:
        return None

    stop_loss = float(plan["stop_loss"])
    first_take_profit = float(plan["first_take_profit"])
    exit_idx = min(entry_idx + max_hold_days - 1, len(bars) - 1)
    exit_price = bars[exit_idx]["close"]
    exit_reason = "timeout"
    stop_metrics: dict[str, Any] = {
        "stop_low_price": None,
        "stop_breach_pct": None,
        "stop_slippage_bps": stop_slippage_bps,
        "slippage_exit_price": None,
        "net_return_worst_intraday": None,
        "net_return_slippage": None,
        "slippage_vs_ideal_return": None,
    }
    max_gain = -1.0
    for idx in range(entry_idx, min(entry_idx + max_hold_days, len(bars))):
        bar = bars[idx]
        high = _safe_float(bar.get("high")) or entry_price
        low = _safe_float(bar.get("low")) or entry_price
        close = _safe_float(bar.get("close")) or entry_price
        max_gain = max(max_gain, high / entry_price - 1)
        if low <= stop_loss:
            exit_idx = idx
            exit_price = stop_loss
            exit_reason = "stop_loss"
            stop_metrics.update(scanner._stop_execution_metrics(entry_price, stop_loss, low, cost_bps, stop_slippage_bps))
            break
        if high >= first_take_profit:
            exit_idx = idx
            exit_price = first_take_profit
            exit_reason = "take_profit"
            break

        holding_days = idx - entry_idx + 1
        closes = [item["close"] for item in bars[: idx + 1] if _safe_float(item.get("close")) is not None]
        ma5 = scanner._moving_average(closes, 5)
        ma10 = scanner._moving_average(closes, 10)
        if rule == "A" and holding_days >= 3 and max_gain < 0.015 and ma5 is not None and close < ma5:
            exit_idx = idx
            exit_price = close
            exit_reason = "time_stop_A_3d_no_1p5_close_below_ma5"
            break
        if rule == "B" and holding_days >= 4 and max_gain < 0.02 and ma10 is not None and close < ma10:
            exit_idx = idx
            exit_price = close
            exit_reason = "time_stop_B_4d_no_2p_close_below_ma10"
            break
        if rule == "C" and holding_days >= 5 and max_gain < 0.02:
            exit_idx = idx
            exit_price = close
            exit_reason = "time_stop_C_5d_no_2p"
            break

    gross_return = exit_price / entry_price - 1
    net_return = gross_return - cost_bps / 10000
    return {
        "symbol": signal.get("symbol"),
        "name": signal.get("name"),
        "signal_date": bars[signal_idx].get("date"),
        "entry_date": bars[entry_idx].get("date"),
        "exit_date": bars[exit_idx].get("date"),
        "signal_price": plan.get("signal_price"),
        "entry_price": round(entry_price, 3),
        "exit_price": round(exit_price, 3),
        "stop_loss": plan.get("stop_loss"),
        "first_take_profit": plan.get("first_take_profit"),
        "position_pct": plan.get("position_pct"),
        "base_position_pct": plan.get("base_position_pct", plan.get("position_pct")),
        "market_regime_label": plan.get("market_regime_label"),
        "holding_days": exit_idx - entry_idx + 1,
        "exit_reason": exit_reason,
        "gross_return": round(gross_return, 5),
        "net_return": round(net_return, 5),
        **stop_metrics,
        "score": signal.get("score"),
        "filter_reasons": signal.get("filter_reasons"),
    }


def _trend_weak_score(item: dict) -> Optional[float]:
    """趋势弱过滤版本：站上 MA20 即可，MA 多头小权重，远离 MA20 扣分。"""
    latest = _safe_float(item.get("latest"))
    ma5 = _safe_float(item.get("ma5"))
    ma10 = _safe_float(item.get("ma10"))
    ma20 = _safe_float(item.get("ma20"))
    if latest is None or ma20 is None or latest < ma20:
        return None
    strategy_score = (_safe_float(item.get("strategy_score")) or 0.0) - (item.get("_component_scores") or {}).get("trend", 0.0)
    if ma5 is not None and ma10 is not None and ma5 >= ma10 >= ma20:
        strategy_score += 5.0
    elif ma5 is not None and ma5 >= ma20:
        strategy_score += 3.0
    distance = latest / ma20 - 1 if ma20 else 0.0
    if distance > 0.12:
        strategy_score -= min(12.0, (distance - 0.12) * 100)
    return round((_safe_float(item.get("score")) or 0.0) * 0.5 + max(strategy_score, 0.0) * 0.5, 6)


def _run_alpha040_and_shadow_experiments(
    *,
    histories: dict[str, list[dict]],
    rows_by_date: dict[str, list[dict]],
    dataset: pd.DataFrame,
    market_timeline: dict[str, dict],
    train_end: pd.Timestamp,
    max_hold_days: int,
    entry_window_days: int,
    cost_bps: float,
    output_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """运行 alpha040、趋势弱过滤和时间止损等 round2 影子实验。"""
    min_base_score = float(scanner._cfg("backtest", "min_score", 70))
    value_maps = _factor_value_map(
        dataset,
        ["alpha040_z", "rps60_z", "strength_zone_proximity_z"],
    )

    def factor_value(item: dict, column: str) -> Optional[float]:
        date = ""
        symbol = str(item.get("symbol") or "")
        signal_idx = item.get("_signal_idx")
        key_symbol = str(item.get("_symbol_key") or symbol)
        if signal_idx is not None:
            # rows_by_date already gives us the date in outer loop, but score_func only sees item.
            # Store it lazily from the bars when needed.
            pass
        date = str(item.get("_signal_date") or "")
        primary_key = (date, symbol)
        fallback_key = (date, key_symbol)
        if primary_key in value_maps[column]:
            return value_maps[column][primary_key]
        return value_maps[column].get(fallback_key)

    # Attach signal_date to each item so score functions can use date/symbol maps without changing first-round code.
    for date, items in rows_by_date.items():
        for item in items:
            item["_signal_date"] = date

    experiments: list[tuple[str, str, Callable[[dict], Optional[float]], Callable[[dict], bool], Optional[Callable[[dict, list[dict], int, Optional[dict]], Optional[dict]]]]] = []
    experiments.append(
        (
            "current_shadow_baseline_round2",
            "当前影子基线",
            lambda item: _safe_float(item.get("final_score")),
            _current_component_eligible,
            None,
        )
    )
    experiments.append(
        (
            "alpha040_only",
            "只用 alpha040 排序",
            lambda item: factor_value(item, "alpha040_z"),
            lambda item: fr._hard_research_pass(item),
            None,
        )
    )
    experiments.append(
        (
            "alpha040_rps60",
            "alpha040 + rps60",
            lambda item: (
                None
                if factor_value(item, "alpha040_z") is None or factor_value(item, "rps60_z") is None
                else 0.5 * factor_value(item, "alpha040_z") + 0.5 * factor_value(item, "rps60_z")
            ),
            lambda item: fr._hard_research_pass(item),
            None,
        )
    )
    experiments.append(
        (
            "alpha040_rps60_strength_zone",
            "alpha040 + rps60 + 强势区位置",
            lambda item: (
                None
                if factor_value(item, "alpha040_z") is None
                or factor_value(item, "rps60_z") is None
                or factor_value(item, "strength_zone_proximity_z") is None
                else 0.4 * factor_value(item, "alpha040_z")
                + 0.4 * factor_value(item, "rps60_z")
                + 0.2 * factor_value(item, "strength_zone_proximity_z")
            ),
            lambda item: fr._hard_research_pass(item),
            None,
        )
    )
    experiments.append(
        (
            "trend_weak_filter",
            "趋势弱过滤版本",
            _trend_weak_score,
            lambda item: fr._hard_research_pass(item)
            and (item.get("_component_flags") or {}).get("rps")
            and (item.get("_component_flags") or {}).get("volume")
            and (item.get("_component_flags") or {}).get("volatility")
            and (item.get("_component_flags") or {}).get("strength_zone"),
            None,
        )
    )
    for rule, label in [
        ("A", "时间止损 A：3日未+1.5%且跌破MA5"),
        ("B", "时间止损 B：4日未+2%且跌破MA10"),
        ("C", "时间止损 C：5日仍未+2%直接退出"),
    ]:
        experiments.append(
            (
                f"time_stop_{rule}",
                label,
                lambda item: _safe_float(item.get("final_score")),
                _current_component_eligible,
                lambda item, bars, signal_idx, market_profile, rule=rule: _simulate_with_time_stop(
                    signal=item,
                    bars=bars,
                    signal_idx=signal_idx,
                    market_profile=market_profile,
                    max_hold_days=max_hold_days,
                    entry_window_days=entry_window_days,
                    cost_bps=cost_bps,
                    rule=rule,
                ),
            )
        )

    all_trades: list[dict] = []
    all_selected: list[dict] = []
    all_summary: list[dict] = []
    for experiment, label, score_func, eligible_func, simulate_func in experiments:
        trades, selected, summary = _ranked_backtest(
            histories=histories,
            rows_by_date=rows_by_date,
            market_timeline=market_timeline,
            train_end=train_end,
            experiment=experiment,
            label=label,
            score_func=score_func,
            eligible_func=eligible_func,
            min_base_score=min_base_score,
            max_hold_days=max_hold_days,
            entry_window_days=entry_window_days,
            cost_bps=cost_bps,
            simulate_func=simulate_func,
        )
        all_trades.extend(trades)
        all_selected.extend(selected)
        all_summary.extend(summary)
    trades_df = pd.DataFrame(all_trades)
    selected_df = pd.DataFrame(all_selected)
    summary_df = pd.DataFrame(all_summary)
    _write_csv(output_dir / "round2_shadow_trades.csv", trades_df)
    _write_csv(output_dir / "round2_shadow_selected_signals.csv", selected_df)
    _write_csv(output_dir / "round2_shadow_summary.csv", summary_df)
    return trades_df, selected_df, summary_df


def _audit_and_run_reversal_filter(
    *,
    histories: dict[str, list[dict]],
    rows_by_date: dict[str, list[dict]],
    dataset: pd.DataFrame,
    market_timeline: dict[str, dict],
    train_end: pd.Timestamp,
    max_hold_days: int,
    entry_window_days: int,
    cost_bps: float,
    output_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """审计并运行冲高回落过滤实验。"""
    min_base_score = float(scanner._cfg("backtest", "min_score", 70))
    label_values = _label_map(dataset)
    eligible_rows: list[dict] = []
    for date, items in rows_by_date.items():
        for item in items:
            if (_safe_float(item.get("score")) or 0.0) < min_base_score or not _current_component_eligible(item):
                continue
            symbol = str(item.get("symbol") or "")
            label_value = label_values.get((date, symbol))
            eligible_rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "upper_shadow_ratio": item.get("_upper_shadow_ratio"),
                    "amplitude": item.get("amplitude"),
                    "future_5d_excess_return": label_value,
                }
            )
    eligible_df = pd.DataFrame(eligible_rows)
    threshold_rows = []
    selected_threshold = 0.45
    selected_reason = "default"
    for threshold in [0.45, 0.40, 0.35, 0.30, 0.25]:
        trigger = eligible_df[
            (pd.to_numeric(eligible_df["upper_shadow_ratio"], errors="coerce") >= threshold)
            & (pd.to_numeric(eligible_df["amplitude"], errors="coerce") >= 0.025)
        ] if not eligible_df.empty else pd.DataFrame()
        threshold_rows.append(
            {
                "threshold": threshold,
                "eligible_count": int(len(eligible_df)),
                "trigger_count": int(len(trigger)),
                "trigger_rate": round(len(trigger) / len(eligible_df), 6) if len(eligible_df) else None,
                "trigger_future_5d_excess_mean": round(float(trigger["future_5d_excess_return"].mean()), 6) if not trigger.empty else None,
            }
        )
        if len(trigger) >= 5 and selected_reason == "default":
            selected_threshold = threshold
            selected_reason = "default 0.45 had enough triggers" if threshold == 0.45 else "default threshold had too few triggers; lowered threshold for audit power"
            break
    if selected_reason == "default" and not eligible_df.empty:
        selected_threshold = float(pd.to_numeric(eligible_df["upper_shadow_ratio"], errors="coerce").quantile(0.75))
        selected_reason = "all fixed thresholds had too few triggers; used upper-shadow 75th percentile"
    audit_df = pd.DataFrame(threshold_rows)

    def eligible_with_filter(item: dict) -> bool:
        upper = _safe_float(item.get("_upper_shadow_ratio"))
        amplitude = _safe_float(item.get("amplitude"))
        if not _current_component_eligible(item):
            return False
        if upper is not None and amplitude is not None and amplitude >= 0.025 and upper >= selected_threshold:
            return False
        return True

    trades, selected, summary = _ranked_backtest(
        histories=histories,
        rows_by_date=rows_by_date,
        market_timeline=market_timeline,
        train_end=train_end,
        experiment="reversal_filter_round2",
        label=f"冲高回落过滤 round2，阈值 {selected_threshold:.3f}",
        score_func=lambda item: _safe_float(item.get("final_score")),
        eligible_func=eligible_with_filter,
        min_base_score=min_base_score,
        max_hold_days=max_hold_days,
        entry_window_days=entry_window_days,
        cost_bps=cost_bps,
    )
    removed = eligible_df[
        (pd.to_numeric(eligible_df["upper_shadow_ratio"], errors="coerce") >= selected_threshold)
        & (pd.to_numeric(eligible_df["amplitude"], errors="coerce") >= 0.025)
    ] if not eligible_df.empty else pd.DataFrame()
    audit_payload = {
        "selected_threshold": selected_threshold,
        "selected_reason": selected_reason,
        "eligible_count": int(len(eligible_df)),
        "removed_count": int(len(removed)),
        "removed_future_5d_excess_mean": round(float(removed["future_5d_excess_return"].mean()), 6) if not removed.empty else None,
        "removed_future_5d_excess_median": round(float(removed["future_5d_excess_return"].median()), 6) if not removed.empty else None,
        "experiment_summary": summary,
    }
    _write_csv(output_dir / "reversal_filter_threshold_audit.csv", audit_df)
    _write_csv(output_dir / "reversal_filter_removed_signals.csv", removed)
    _write_csv(output_dir / "reversal_filter_trades.csv", pd.DataFrame(trades))
    _write_csv(output_dir / "reversal_filter_selected_signals.csv", pd.DataFrame(selected))
    _write_csv(output_dir / "reversal_filter_summary.csv", pd.DataFrame(summary))
    _write_json(output_dir / "reversal_filter_audit.json", audit_payload)
    return audit_df, pd.DataFrame(summary)


def _summary_lookup(summary: pd.DataFrame, experiment: str, split: str = "test") -> dict:
    """读取实验汇总行。"""
    if summary.empty:
        return {}
    rows = summary[(summary["experiment"] == experiment) & (summary["split"] == split)]
    return rows.iloc[0].to_dict() if not rows.empty else {}


def _exit_reason_count(row: dict, reason_prefix: str) -> int:
    """读取某类退出原因数量。"""
    counts = row.get("exit_reason_counts") or {}
    if not isinstance(counts, dict):
        return 0
    return int(sum(value for reason, value in counts.items() if str(reason).startswith(reason_prefix)))


def _render_round2_report(
    *,
    output_dir: Path,
    histories: dict[str, list[dict]],
    dataset: pd.DataFrame,
    train_end: pd.Timestamp,
    test_start: pd.Timestamp,
    universe_daily: pd.DataFrame,
    top_corr: pd.DataFrame,
    watch_corr: pd.DataFrame,
    factor_comparison: pd.DataFrame,
    shadow_summary: pd.DataFrame,
    reversal_audit: pd.DataFrame,
    reversal_summary: pd.DataFrame,
) -> str:
    """渲染 round2 报告。"""
    lines = [
        "# Factor Research Round2 Report",
        "",
        f"生成日期：{datetime.now().strftime('%Y-%m-%d')}",
        f"样本股票：{len(histories)} 只",
        f"有效因子样本：{len(dataset)} 行",
        f"时间切分：train <= {train_end.strftime('%Y-%m-%d')}，test >= {test_start.strftime('%Y-%m-%d')}",
        "",
        "免责声明：本报告仅用于个人模拟交易、复盘和策略研究；不构成投资建议，不修改主策略，不连接实盘接口。",
        "",
        "## 1. Universe 审计",
        "",
        "- 结论：当前研究池存在后验股票池偏差风险。",
        "- 因子值计算使用信号日及以前数据；标签未来 5 日收益只用于评估。",
        "- 股票池成员来自当前本地缓存文件集合，不是逐日历史全市场 universe。",
        f"- 每日股票数范围：{int(universe_daily['symbols_with_bar'].min())} 至 {int(universe_daily['symbols_with_bar'].max())}。",
        "- 详情：`output/factor_research_round2/universe_audit.md` / `universe_daily_audit.csv`。",
        "",
        "## 2. 高相关因子",
        "",
        "| 因子 A | 因子 B | 相关系数 | 绝对值 |",
        "|---|---|---:|---:|",
    ]
    for _, row in top_corr.head(10).iterrows():
        lines.append(
            f"| `{row['factor_left']}` | `{row['factor_right']}` | {row['correlation']:.4f} | {row['abs_correlation']:.4f} |"
        )
    lines.extend(["", "### 指定重复因子检查", "", "| 因子 A | 因子 B | 状态 | 说明 |", "|---|---|---|---|"])
    for _, row in watch_corr.iterrows():
        corr_text = "-" if pd.isna(row.get("correlation")) else f"{row['correlation']:.4f}"
        lines.append(f"| `{row['factor_left']}` | `{row['factor_right']}` | {row['status']} | corr={corr_text} {row.get('note') or ''} |")

    lines.extend(["", "## 3. Train/Test 稳定性", ""])
    for label, title in [
        ("stable_factor", "稳定因子"),
        ("train_effective_test_failed", "训练有效测试失效"),
        ("test_accidental_enhanced", "测试段偶然增强"),
    ]:
        view = factor_comparison[factor_comparison["classification"] == label].head(8)
        lines.extend([f"### {title}", ""])
        if view.empty:
            lines.append("- 暂无。")
        else:
            for _, row in view.iterrows():
                lines.append(
                    f"- `{row['factor']}`：train RankIC {row['train_rank_ic']:.4f}，"
                    f"test RankIC {row['test_rank_ic']:.4f}，test Q5-Q1 {_fmt_pct(row['test_group_q5_q1'])}"
                )
        lines.append("")

    lines.extend(
        [
            "## 4. alpha040 专项验证",
            "",
            "| 实验 | test交易数 | test胜率 | test平均单笔 | test中位数 | test回撤 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for experiment in [
        "current_shadow_baseline_round2",
        "alpha040_only",
        "alpha040_rps60",
        "alpha040_rps60_strength_zone",
    ]:
        row = _summary_lookup(shadow_summary, experiment, "test")
        lines.append(
            f"| {row.get('experiment_label', experiment)} | {int(row.get('trade_count') or 0)} | "
            f"{_fmt_pct(row.get('win_rate'))} | {_fmt_pct(row.get('average_return'))} | "
            f"{_fmt_pct(row.get('median_return'))} | {_fmt_pct(row.get('event_sequence_max_drawdown'))} |"
        )

    lines.extend(
        [
            "",
            "## 5. 趋势弱过滤与时间止损",
            "",
            "| 实验 | test交易数 | test胜率 | test平均单笔 | test中位数 | test回撤 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for experiment in ["trend_weak_filter", "time_stop_A", "time_stop_B", "time_stop_C"]:
        row = _summary_lookup(shadow_summary, experiment, "test")
        lines.append(
            f"| {row.get('experiment_label', experiment)} | {int(row.get('trade_count') or 0)} | "
            f"{_fmt_pct(row.get('win_rate'))} | {_fmt_pct(row.get('average_return'))} | "
            f"{_fmt_pct(row.get('median_return'))} | {_fmt_pct(row.get('event_sequence_max_drawdown'))} |"
        )
    lines.extend(["", "### 时间止损实际触发次数", ""])
    for experiment, prefix in [
        ("time_stop_A", "time_stop_A"),
        ("time_stop_B", "time_stop_B"),
        ("time_stop_C", "time_stop_C"),
    ]:
        row = _summary_lookup(shadow_summary, experiment, "test")
        lines.append(f"- {row.get('experiment_label', experiment)}：test 触发 {_exit_reason_count(row, prefix)} 笔。")

    lines.extend(["", "## 6. 冲高回落过滤审计", ""])
    if not reversal_audit.empty:
        with open(output_dir / "reversal_filter_audit.json", "r", encoding="utf-8") as f:
            chosen = json.load(f)
        removed_mean = _safe_float(chosen.get("removed_future_5d_excess_mean"))
        lines.extend(
            [
                f"- 选用阈值：{float(chosen.get('selected_threshold')):.3f}",
                f"- 选择原因：{chosen.get('selected_reason')}",
                f"- eligible 数量：{int(chosen.get('eligible_count') or 0)}",
                f"- 剔除数量：{int(chosen.get('removed_count') or 0)}",
                f"- 被剔除信号未来 5 日超额平均：{_fmt_pct(chosen.get('removed_future_5d_excess_mean'))}",
            ]
        )
        if removed_mean is not None and removed_mean > 0:
            lines.append("- 注意：被剔除样本未来 5 日超额收益为正，说明该过滤并非单纯剔除弱票，不能直接提升为主策略。")
    row = _summary_lookup(reversal_summary, "reversal_filter_round2", "test")
    lines.extend(
        [
            f"- 过滤后 test 平均单笔：{_fmt_pct(row.get('average_return'))}，胜率：{_fmt_pct(row.get('win_rate'))}，交易数：{int(row.get('trade_count') or 0)}。",
            "- 详情：`reversal_filter_threshold_audit.csv` / `reversal_filter_removed_signals.csv`。",
            "",
            "## 7. 文件清单",
            "",
            "- `universe_audit.md`：股票池偏差审计。",
            "- `top_abs_correlations.csv`：绝对相关最高前 20 组。",
            "- `watched_factor_correlations.csv`：指定重复因子对检查。",
            "- `train_test_factor_comparison.csv`：train/test IC、RankIC、分组收益与稳定性分类。",
            "- `round2_shadow_summary.csv`：alpha040、趋势弱过滤、时间止损实验汇总。",
            "- `reversal_filter_audit.json`：冲高回落过滤阈值和剔除收益审计。",
            "",
            "## 8. 结论边界",
            "",
            "- 因 universe 不是逐日全市场重建，本轮结果只能作为 shadow evidence。",
            "- 高相关因子不应重复加权，尤其 RPS 与对应窗口涨跌幅天然重复。",
            "- 任何改善项都不能自动并入主策略，需要更长窗口、每日 universe 修正和人工确认。",
        ]
    )
    report = "\n".join(lines) + "\n"
    (output_dir / "factor_research_round2_report.md").write_text(report, encoding="utf-8")
    return report


def run_factor_research_round2(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = fr.DEFAULT_CACHE_DIR,
    index_cache_dir: Path = fr.DEFAULT_INDEX_CACHE_DIR,
    max_symbols: Optional[int] = None,
    min_bars: int = 80,
    min_history: int = 60,
    label_horizon: int = fr.DEFAULT_LABEL_HORIZON,
    min_cross_section: int = fr.DEFAULT_MIN_CROSS_SECTION,
    groups: int = fr.DEFAULT_GROUPS,
    train_ratio: float = 0.7,
    strategy_path: str = "strategy.json",
) -> dict[str, Any]:
    """执行第二轮因子研究审计与影子实验。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    ctx = _build_research_context(
        cache_dir=cache_dir,
        index_cache_dir=index_cache_dir,
        max_symbols=max_symbols,
        min_bars=min_bars,
        min_history=min_history,
        label_horizon=label_horizon,
        min_cross_section=min_cross_section,
        groups=groups,
        train_ratio=train_ratio,
        strategy_path=strategy_path,
    )
    histories = ctx["histories"]
    dataset = ctx["dataset"]
    rows_by_date = ctx["rows_by_date"]
    train_end = ctx["train_end"]
    test_start = ctx["test_start"]
    universe_daily, _universe_md = _run_universe_audit(histories, dataset, rows_by_date, output_dir)
    top_corr, watch_corr = _top_abs_correlations(ctx["corr_matrix"], output_dir)
    factor_comparison = _train_test_factor_comparison(ctx["ic_summary"], ctx["group_summary"], output_dir)
    shadow_trades, shadow_selected, shadow_summary = _run_alpha040_and_shadow_experiments(
        histories=histories,
        rows_by_date=rows_by_date,
        dataset=dataset,
        market_timeline=ctx["market_timeline"],
        train_end=train_end,
        max_hold_days=ctx["max_hold_days"],
        entry_window_days=ctx["entry_window_days"],
        cost_bps=ctx["cost_bps"],
        output_dir=output_dir,
    )
    reversal_audit, reversal_summary = _audit_and_run_reversal_filter(
        histories=histories,
        rows_by_date=rows_by_date,
        dataset=dataset,
        market_timeline=ctx["market_timeline"],
        train_end=train_end,
        max_hold_days=ctx["max_hold_days"],
        entry_window_days=ctx["entry_window_days"],
        cost_bps=ctx["cost_bps"],
        output_dir=output_dir,
    )
    report = _render_round2_report(
        output_dir=output_dir,
        histories=histories,
        dataset=dataset,
        train_end=train_end,
        test_start=test_start,
        universe_daily=universe_daily,
        top_corr=top_corr,
        watch_corr=watch_corr,
        factor_comparison=factor_comparison,
        shadow_summary=shadow_summary,
        reversal_audit=reversal_audit,
        reversal_summary=reversal_summary,
    )
    summary = {
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "module": "factor_research_round2_shadow",
        "safety": "不修改主 market_scanner 策略、不写台账、不连接实盘接口。",
        "history_symbol_count": len(histories),
        "dataset_rows": int(len(dataset)),
        "date_start": dataset["date"].min().strftime("%Y-%m-%d"),
        "date_end": dataset["date"].max().strftime("%Y-%m-%d"),
        "train_end": train_end.strftime("%Y-%m-%d"),
        "test_start": test_start.strftime("%Y-%m-%d"),
        "output_dir": str(output_dir),
        "report_path": str(output_dir / "factor_research_round2_report.md"),
        "universe_audit_path": str(output_dir / "universe_audit.md"),
        "top_correlation_path": str(output_dir / "top_abs_correlations.csv"),
        "factor_comparison_path": str(output_dir / "train_test_factor_comparison.csv"),
        "shadow_summary_path": str(output_dir / "round2_shadow_summary.csv"),
        "reversal_audit_path": str(output_dir / "reversal_filter_audit.json"),
        "report_characters": len(report),
        "shadow_trade_count": int(len(shadow_trades)),
        "shadow_selected_count": int(len(shadow_selected)),
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行因子研究第二轮审计与影子实验")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录，默认 output/factor_research_round2")
    parser.add_argument("--cache-dir", default=str(fr.DEFAULT_CACHE_DIR), help="股票历史 K 缓存目录")
    parser.add_argument("--index-cache-dir", default=str(fr.DEFAULT_INDEX_CACHE_DIR), help="指数历史 K 缓存目录")
    parser.add_argument("--max-symbols", type=int, help="最多读取多少只股票缓存，默认全部")
    parser.add_argument("--min-bars", type=int, default=80, help="单票最少 K 线数量")
    parser.add_argument("--min-history", type=int, default=60, help="开始生成信号前的最少历史长度")
    parser.add_argument("--label-horizon", type=int, default=fr.DEFAULT_LABEL_HORIZON, help="未来收益标签窗口")
    parser.add_argument("--min-cross-section", type=int, default=fr.DEFAULT_MIN_CROSS_SECTION, help="每日最小横截面样本")
    parser.add_argument("--groups", type=int, default=fr.DEFAULT_GROUPS, help="分组收益组数")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="按时间顺序训练段比例")
    parser.add_argument("--strategy", default="strategy.json", help="读取 market scanner 参数的策略配置")
    args = parser.parse_args()
    summary = run_factor_research_round2(
        output_dir=Path(args.output),
        cache_dir=Path(args.cache_dir),
        index_cache_dir=Path(args.index_cache_dir),
        max_symbols=args.max_symbols,
        min_bars=args.min_bars,
        min_history=args.min_history,
        label_horizon=args.label_horizon,
        min_cross_section=args.min_cross_section,
        groups=args.groups,
        train_ratio=args.train_ratio,
        strategy_path=args.strategy,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
