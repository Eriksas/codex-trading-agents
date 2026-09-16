"""事件/组合回放与既有影子对照；保持原成交、成本和仓位口径。"""

from statistics import median, stdev
from typing import Any, Optional

if __package__:
    from .scanner_config import (
        _cfg,
    )
    from .scanner_report import (
        _render_shadow_experiment_report,
    )
    from .scanner_rules import (
        _apply_market_risk_controls,
        _build_trade_plan,
        _market_allows_backtest_signal,
        _passes_enhanced_strategy,
        _score_stock,
        _signal_row_from_bar,
    )
    from .scanner_utils import (
        _to_float,
    )
else:
    from scanner_config import (
        _cfg,
    )
    from scanner_report import (
        _render_shadow_experiment_report,
    )
    from scanner_rules import (
        _apply_market_risk_controls,
        _build_trade_plan,
        _market_allows_backtest_signal,
        _passes_enhanced_strategy,
        _score_stock,
        _signal_row_from_bar,
    )
    from scanner_utils import (
        _to_float,
    )


def _parse_trigger_zone(zone: str) -> tuple[float, float]:
    """解析触发区间字符串。"""
    low_text, high_text = zone.split("-", maxsplit=1)
    return float(low_text), float(high_text)


def _entry_price_for_bar(bar: dict, trigger_low: float, trigger_high: float) -> Optional[float]:
    """判断 K 线是否触发模拟买入区间，并返回保守成交价。"""
    if bar["low"] > trigger_high or bar["high"] < trigger_low:
        return None
    open_price = bar["open"]
    if trigger_low <= open_price <= trigger_high:
        return open_price
    if open_price > trigger_high:
        return trigger_high
    return trigger_low


def _stop_execution_metrics(
    entry_price: float,
    stop_loss: float,
    bar_low: Optional[float],
    cost_bps: float,
    stop_slippage_bps: float,
) -> dict[str, Optional[float]]:
    """计算止损理想成交、滑点成交和日内最低压力测试指标。"""
    if entry_price <= 0 or stop_loss <= 0:
        return {}
    low_price = bar_low if bar_low is not None else stop_loss
    stop_breach_pct = low_price / stop_loss - 1 if low_price < stop_loss else 0.0
    slippage_price = stop_loss * (1 - stop_slippage_bps / 10000)
    if low_price is not None:
        slippage_price = max(low_price, slippage_price)
    ideal_net = stop_loss / entry_price - 1 - cost_bps / 10000
    worst_intraday_net = low_price / entry_price - 1 - cost_bps / 10000 if low_price is not None else ideal_net
    slippage_net = slippage_price / entry_price - 1 - cost_bps / 10000
    return {
        "stop_low_price": round(low_price, 3) if low_price is not None else None,
        "stop_breach_pct": round(stop_breach_pct, 5),
        "stop_slippage_bps": stop_slippage_bps,
        "slippage_exit_price": round(slippage_price, 3),
        "net_return_worst_intraday": round(worst_intraday_net, 5),
        "net_return_slippage": round(slippage_net, 5),
        "slippage_vs_ideal_return": round(slippage_net - ideal_net, 5),
    }


def _simulate_trade(
    signal: dict,
    bars: list[dict],
    signal_idx: int,
    market_profile: Optional[dict] = None,
    max_hold_days: int = 5,
    entry_window_days: int = 2,
    cost_bps: float = 15,
) -> Optional[dict]:
    """按信号后的触发区间、止损、第一止盈做保守回放。"""
    stop_slippage_bps = float(_cfg("backtest", "stop_slippage_bps", 30))
    plan = _build_trade_plan(signal)
    if market_profile:
        plan = _apply_market_risk_controls([plan], market_profile)[0]
    trigger_low, trigger_high = _parse_trigger_zone(plan["trigger_zone"])
    entry_idx: Optional[int] = None
    entry_price: Optional[float] = None
    for idx in range(signal_idx + 1, min(signal_idx + 1 + entry_window_days, len(bars))):
        price = _entry_price_for_bar(bars[idx], trigger_low, trigger_high)
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
    stop_metrics: dict[str, Optional[float]] = {
        "stop_low_price": None,
        "stop_breach_pct": None,
        "stop_slippage_bps": stop_slippage_bps,
        "slippage_exit_price": None,
        "net_return_worst_intraday": None,
        "net_return_slippage": None,
        "slippage_vs_ideal_return": None,
    }

    for idx in range(entry_idx, min(entry_idx + max_hold_days, len(bars))):
        bar = bars[idx]
        if bar["low"] <= stop_loss:
            exit_idx = idx
            exit_price = stop_loss
            exit_reason = "stop_loss"
            stop_metrics.update(
                _stop_execution_metrics(entry_price, stop_loss, bar.get("low"), cost_bps, stop_slippage_bps)
            )
            break
        if bar["high"] >= first_take_profit:
            exit_idx = idx
            exit_price = first_take_profit
            exit_reason = "take_profit"
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
        "_exit_idx": exit_idx,
    }


def _summarize_backtest(trades: list[dict], symbol_count: int, cost_bps: float) -> dict:
    """汇总回测指标。"""
    returns = [trade["net_return"] for trade in trades]
    slippage_returns = [
        trade["net_return_slippage"]
        for trade in trades
        if trade.get("net_return_slippage") is not None
    ]
    worst_intraday_returns = [
        trade["net_return_worst_intraday"]
        for trade in trades
        if trade.get("net_return_worst_intraday") is not None
    ]
    stop_trades = [trade for trade in trades if trade.get("exit_reason") == "stop_loss"]
    stop_breaches = [
        trade.get("stop_breach_pct")
        for trade in stop_trades
        if trade.get("stop_breach_pct") is not None and trade.get("stop_breach_pct") < 0
    ]
    slippage_vs_ideal = [
        trade.get("slippage_vs_ideal_return")
        for trade in stop_trades
        if trade.get("slippage_vs_ideal_return") is not None
    ]
    event_equity = 1.0
    peak = 1.0
    event_sequence_max_drawdown = 0.0
    for ret in returns:
        event_equity *= 1 + ret
        peak = max(peak, event_equity)
        if peak:
            event_sequence_max_drawdown = min(event_sequence_max_drawdown, event_equity / peak - 1)
    reason_counts: dict[str, int] = {}
    for trade in trades:
        reason = trade.get("exit_reason") or "unknown"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "symbol_count": symbol_count,
        "trade_count": len(trades),
        "win_rate": round(sum(1 for ret in returns if ret > 0) / len(returns), 4) if returns else None,
        "average_return": round(sum(returns) / len(returns), 5) if returns else None,
        "median_return": round(median(returns), 5) if returns else None,
        "best_trade_return": round(max(returns), 5) if returns else None,
        "worst_trade_return": round(min(returns), 5) if returns else None,
        "average_slippage_return": round(sum(slippage_returns) / len(slippage_returns), 5) if slippage_returns else None,
        "average_worst_intraday_return": (
            round(sum(worst_intraday_returns) / len(worst_intraday_returns), 5) if worst_intraday_returns else None
        ),
        "event_sequence_max_drawdown": round(event_sequence_max_drawdown, 5) if returns else None,
        "exit_reason_counts": reason_counts,
        "stop_loss_breach_count": len(stop_breaches),
        "stop_loss_breach_rate": round(len(stop_breaches) / len(stop_trades), 5) if stop_trades else None,
        "average_stop_breach_pct": round(sum(stop_breaches) / len(stop_breaches), 5) if stop_breaches else None,
        "worst_stop_breach_pct": round(min(stop_breaches), 5) if stop_breaches else None,
        "average_slippage_vs_ideal_return": (
            round(sum(slippage_vs_ideal) / len(slippage_vs_ideal), 5) if slippage_vs_ideal else None
        ),
        "cost_bps": cost_bps,
        "stop_slippage_bps": _cfg("backtest", "stop_slippage_bps", 30),
        "rule": "信号日收盘生成计划；后2个交易日触发区间有效；持有最多5个交易日；同日先触发止损则按理想止损价计，同时输出滑点和日内最低压力测试。",
        "note": "回测为事件级规则回放，未构建资金占用、同时持仓上限和组合再平衡曲线；止损成交价默认仍为规则价，压力测试指标用于暴露跳空/滑点风险。",
    }


def _run_backtest(
    histories: dict[str, list[dict]],
    stock_rows: dict[str, dict],
    market_timeline: Optional[dict[str, dict]] = None,
    min_score: float = 70,
    max_hold_days: int = 5,
    entry_window_days: int = 2,
    cost_bps: float = 15,
) -> tuple[list[dict], dict]:
    """对已补齐历史 K 的样本执行规则回测。"""
    trades: list[dict] = []
    for symbol, bars in histories.items():
        if len(bars) < 80:
            continue
        base_row = stock_rows.get(symbol)
        if not base_row:
            continue
        idx = 60
        while idx < len(bars) - entry_window_days:
            signal_row = _signal_row_from_bar(base_row, bars, idx)
            scored_signal = _score_stock(signal_row)
            signal_date = bars[idx].get("date")
            signal_market = market_timeline.get(signal_date) if market_timeline and signal_date else None
            if (
                scored_signal["passed"]
                and scored_signal["score"] >= min_score
                and _passes_enhanced_strategy(scored_signal)
                and _market_allows_backtest_signal(signal_market, scored_signal["score"])
            ):
                trade = _simulate_trade(
                    scored_signal,
                    bars,
                    idx,
                    market_profile=signal_market,
                    max_hold_days=max_hold_days,
                    entry_window_days=entry_window_days,
                    cost_bps=cost_bps,
                )
                if trade:
                    trades.append(trade)
                    idx = int(trade.pop("_exit_idx")) + 1
                    continue
            idx += 1
    summary = _summarize_backtest(trades, len(histories), cost_bps)
    summary["market_filter_applied"] = bool(market_timeline)
    if market_timeline:
        summary["note"] = (
            "回测为事件级规则回放，已按历史指数环境过滤新信号；"
            "未构建资金占用、同时持仓上限和组合再平衡曲线；"
            "止损成交价默认仍为规则价，另输出滑点和日内最低压力测试。"
        )
    return trades, summary


def _run_portfolio_backtest(trades: list[dict]) -> tuple[list[dict], dict]:
    """在事件级交易上叠加现金、持仓上限和总敞口约束。"""
    initial_cash = float(_cfg("backtest", "portfolio_initial_cash", 1_000_000))
    max_positions = int(_cfg("backtest", "portfolio_max_positions", 4))
    max_total_exposure = float(_cfg("backtest", "portfolio_max_total_exposure_pct", 0.32))
    default_position_pct = float(_cfg("backtest", "portfolio_default_position_pct", 0.08))

    sorted_trades = sorted(trades, key=lambda x: (x.get("entry_date") or "", -(x.get("score") or 0)))
    active: list[dict] = []
    accepted: list[dict] = []
    skipped: list[dict] = []
    cash = initial_cash
    realized_equity = initial_cash
    peak_equity = initial_cash
    max_drawdown = 0.0

    def close_due_positions(current_date: str) -> None:
        nonlocal cash, realized_equity, peak_equity, max_drawdown, active
        still_active: list[dict] = []
        for pos in active:
            if pos["exit_date"] <= current_date:
                exit_value = pos["position_value"] * (1 + pos["net_return"])
                cash += exit_value
                realized_equity = cash + sum(item["position_value"] for item in still_active)
                peak_equity = max(peak_equity, realized_equity)
                if peak_equity:
                    max_drawdown = min(max_drawdown, realized_equity / peak_equity - 1)
            else:
                still_active.append(pos)
        active = still_active

    for trade in sorted_trades:
        entry_date = trade.get("entry_date") or ""
        exit_date = trade.get("exit_date") or ""
        if not entry_date or not exit_date:
            continue
        close_due_positions(entry_date)
        current_exposure = sum(pos["position_value"] for pos in active)
        position_pct = (_to_float(trade.get("position_pct")) or (default_position_pct * 100)) / 100
        target_value = initial_cash * position_pct
        remaining_exposure = initial_cash * max_total_exposure - current_exposure
        position_value = min(target_value, cash, remaining_exposure)
        if len(active) >= max_positions or position_value <= 0:
            skipped.append({**trade, "portfolio_action": "skipped_capacity"})
            continue
        trade_row = {
            **trade,
            "portfolio_action": "accepted",
            "position_value": round(position_value, 2),
            "portfolio_position_pct": round(position_value / initial_cash, 4),
        }
        cash -= position_value
        active.append(
            {
                "exit_date": exit_date,
                "position_value": position_value,
                "net_return": trade.get("net_return") or 0.0,
            }
        )
        accepted.append(trade_row)

    close_due_positions("9999-12-31")
    returns = [row.get("net_return") for row in accepted if row.get("net_return") is not None]
    summary = {
        "initial_cash": initial_cash,
        "ending_equity": round(cash, 2),
        "total_return": round(cash / initial_cash - 1, 5) if initial_cash else None,
        "accepted_trades": len(accepted),
        "skipped_trades": len(skipped),
        "max_positions": max_positions,
        "max_total_exposure_pct": max_total_exposure,
        "win_rate": round(sum(1 for ret in returns if ret > 0) / len(returns), 4) if returns else None,
        "average_trade_return": round(sum(returns) / len(returns), 5) if returns else None,
        "max_drawdown": round(max_drawdown, 5),
        "note": "组合级回测加入现金、同时持仓上限和总敞口限制；仍使用日 K 事件回放，非实盘成交证明。",
    }
    return [*accepted, *skipped], summary


def _summarize_shadow_returns(trades: list[dict], return_field: str = "net_return") -> dict:
    """汇总影子实验单笔收益。"""
    returns = [
        _to_float(trade.get(return_field))
        for trade in trades
        if _to_float(trade.get(return_field)) is not None
    ]
    reason_counts: dict[str, int] = {}
    for trade in trades:
        reason = str(trade.get("exit_reason") or trade.get("shadow_exit_reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    if not returns:
        return {
            "trade_count": 0,
            "win_rate": None,
            "average_return": None,
            "median_return": None,
            "best_trade_return": None,
            "worst_trade_return": None,
            "exit_reason_counts": reason_counts,
        }
    return {
        "trade_count": len(returns),
        "win_rate": round(sum(1 for ret in returns if ret > 0) / len(returns), 4),
        "average_return": round(sum(returns) / len(returns), 5),
        "median_return": round(median(returns), 5),
        "best_trade_return": round(max(returns), 5),
        "worst_trade_return": round(min(returns), 5),
        "exit_reason_counts": reason_counts,
    }


def _simulate_next_day_open_trade(
    trade: dict,
    histories: dict[str, list[dict]],
    max_hold_days: int,
    cost_bps: float,
) -> Optional[dict]:
    """用既有信号测试“次一交易日开盘直接买入”的影子表现。"""
    symbol = str(trade.get("symbol") or "")
    signal_date = str(trade.get("signal_date") or "")
    bars = histories.get(symbol) or []
    if not symbol or not signal_date or len(bars) < 2:
        return None
    date_to_idx = {str(bar.get("date")): idx for idx, bar in enumerate(bars)}
    signal_idx = date_to_idx.get(signal_date)
    if signal_idx is None or signal_idx + 1 >= len(bars):
        return None
    entry_idx = signal_idx + 1
    entry_bar = bars[entry_idx]
    entry_price = _to_float(entry_bar.get("open"))
    stop_loss = _to_float(trade.get("stop_loss"))
    first_take_profit = _to_float(trade.get("first_take_profit"))
    if entry_price is None or stop_loss is None or first_take_profit is None or entry_price <= 0:
        return None

    exit_idx = min(entry_idx + max_hold_days - 1, len(bars) - 1)
    exit_price = _to_float(bars[exit_idx].get("close")) or entry_price
    exit_reason = "timeout"
    stop_metrics: dict[str, Any] = {
        "stop_low_price": None,
        "stop_breach_pct": None,
        "stop_slippage_bps": float(_cfg("backtest", "stop_slippage_bps", 30)),
        "slippage_exit_price": None,
        "net_return_worst_intraday": None,
        "net_return_slippage": None,
        "slippage_vs_ideal_return": None,
    }
    for idx in range(entry_idx, min(entry_idx + max_hold_days, len(bars))):
        bar = bars[idx]
        low = _to_float(bar.get("low"))
        high = _to_float(bar.get("high"))
        if low is not None and low <= stop_loss:
            exit_idx = idx
            exit_price = stop_loss
            exit_reason = "stop_loss"
            stop_metrics.update(
                _stop_execution_metrics(
                    entry_price,
                    stop_loss,
                    low,
                    cost_bps,
                    float(_cfg("backtest", "stop_slippage_bps", 30)),
                )
            )
            break
        if high is not None and high >= first_take_profit:
            exit_idx = idx
            exit_price = first_take_profit
            exit_reason = "take_profit"
            break

    gross_return = exit_price / entry_price - 1
    net_return = gross_return - cost_bps / 10000
    return {
        "symbol": symbol,
        "name": trade.get("name"),
        "signal_date": signal_date,
        "entry_date": entry_bar.get("date"),
        "exit_date": bars[exit_idx].get("date"),
        "signal_price": trade.get("signal_price"),
        "entry_price": round(entry_price, 3),
        "exit_price": round(exit_price, 3),
        "stop_loss": stop_loss,
        "first_take_profit": first_take_profit,
        "holding_days": exit_idx - entry_idx + 1,
        "exit_reason": exit_reason,
        "gross_return": round(gross_return, 5),
        "net_return": round(net_return, 5),
        "position_pct": trade.get("position_pct"),
        "market_regime_label": trade.get("market_regime_label"),
        "score": trade.get("score"),
        **stop_metrics,
    }


def _compound_position_returns(trades: list[dict], position_field: str = "position_pct") -> dict:
    """按单笔仓位将交易收益折算到账户权益曲线。"""
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    accepted = 0
    exposure_values: list[float] = []
    for trade in sorted(trades, key=lambda item: (str(item.get("entry_date") or ""), str(item.get("symbol") or ""))):
        ret = _to_float(trade.get("net_return"))
        position_pct = _to_float(trade.get(position_field))
        if ret is None or position_pct is None or position_pct <= 0:
            continue
        accepted += 1
        exposure = position_pct / 100
        exposure_values.append(exposure)
        equity *= 1 + ret * exposure
        peak = max(peak, equity)
        if peak:
            max_drawdown = min(max_drawdown, equity / peak - 1)
    return {
        "trade_count": accepted,
        "total_return": round(equity - 1, 5),
        "max_drawdown": round(max_drawdown, 5),
        "average_position_pct": round((sum(exposure_values) / len(exposure_values)) * 100, 2) if exposure_values else None,
    }


def _apply_risk_budget_positions(trades: list[dict]) -> list[dict]:
    """按每笔最大账户风险反推影子仓位。"""
    account_risk_pct = float(_cfg("shadow_experiments", "risk_budget_account_pct", 0.002))
    max_position_pct = float(_cfg("shadow_experiments", "risk_budget_max_position_pct", 8))
    min_stop_pct = float(_cfg("shadow_experiments", "risk_budget_min_stop_pct", 0.005))
    rows: list[dict] = []
    for trade in trades:
        entry_price = _to_float(trade.get("entry_price"))
        stop_loss = _to_float(trade.get("stop_loss"))
        original_position = _to_float(trade.get("position_pct")) or max_position_pct
        if entry_price is None or stop_loss is None or entry_price <= 0:
            budget_position = min(original_position, max_position_pct)
            stop_distance = None
        else:
            stop_distance = max((entry_price - stop_loss) / entry_price, min_stop_pct)
            budget_position = min(max_position_pct, account_risk_pct / stop_distance * 100)
            budget_position = min(budget_position, original_position)
        rows.append(
            {
                **trade,
                "risk_budget_position_pct": round(max(0.0, budget_position), 2),
                "risk_budget_account_pct": account_risk_pct,
                "stop_distance_pct": round(stop_distance, 5) if stop_distance is not None else None,
            }
        )
    return rows


def _run_shadow_experiments(
    backtest_trades: list[dict],
    histories: dict[str, list[dict]],
    cost_bps: float,
    max_hold_days: int,
) -> tuple[list[dict], dict, str]:
    """生成不影响主策略的影子实验评估。"""
    next_open_trades = [
        trade
        for trade in (
            _simulate_next_day_open_trade(item, histories, max_hold_days=max_hold_days, cost_bps=cost_bps)
            for item in backtest_trades
        )
        if trade is not None
    ]
    risk_budget_trades = _apply_risk_budget_positions(backtest_trades)
    current_account = _compound_position_returns(backtest_trades)
    risk_budget_account = _compound_position_returns(risk_budget_trades, position_field="risk_budget_position_pct")

    defensive_trades = [trade for trade in backtest_trades if trade.get("market_regime_label") == "防守"]
    non_defensive_trades = [trade for trade in backtest_trades if trade.get("market_regime_label") != "防守"]
    stop_trades = [trade for trade in backtest_trades if trade.get("exit_reason") == "stop_loss"]
    stop_slippage = [
        _to_float(trade.get("net_return_slippage"))
        for trade in stop_trades
        if _to_float(trade.get("net_return_slippage")) is not None
    ]
    stop_worst = [
        _to_float(trade.get("net_return_worst_intraday"))
        for trade in stop_trades
        if _to_float(trade.get("net_return_worst_intraday")) is not None
    ]
    stop_ideal = [
        _to_float(trade.get("net_return"))
        for trade in stop_trades
        if _to_float(trade.get("net_return")) is not None
    ]

    current_summary = _summarize_shadow_returns(backtest_trades)
    next_open_summary = _summarize_shadow_returns(next_open_trades)
    delta_avg = None
    if current_summary.get("average_return") is not None and next_open_summary.get("average_return") is not None:
        delta_avg = round(next_open_summary["average_return"] - current_summary["average_return"], 5)
    summary = {
        "enabled": True,
        "sample_scope": "基于已触发入场的规则回测样本做影子对照；不改主策略、不写入台账。",
        "current_trigger": current_summary,
        "next_day_open_buy": {
            **next_open_summary,
            "average_return_delta_vs_current": delta_avg,
            "note": "同一批已触发信号，改为次一交易日开盘直接入场，并沿用原止损/第一止盈/持有期。",
        },
        "defensive_skip": {
            "current_defensive_trade_count": len(defensive_trades),
            "non_defensive_trade_count": len(non_defensive_trades),
            "note": "当前历史回测已按市场环境过滤防守期新信号；若这里出现防守样本，需优先复查过滤口径。",
        },
        "risk_budget_position": {
            "account_risk_pct": float(_cfg("shadow_experiments", "risk_budget_account_pct", 0.002)),
            "max_position_pct": float(_cfg("shadow_experiments", "risk_budget_max_position_pct", 8)),
            "current_fixed_position_account": current_account,
            "risk_budget_account": risk_budget_account,
            "average_position_delta_pct": (
                round(
                    (risk_budget_account.get("average_position_pct") or 0)
                    - (current_account.get("average_position_pct") or 0),
                    2,
                )
                if current_account.get("average_position_pct") is not None
                and risk_budget_account.get("average_position_pct") is not None
                else None
            ),
            "note": "每笔按最大账户风险反推仓位，并以上限和原策略仓位封顶；仅作影子评估。",
        },
        "stop_execution_pressure": {
            "stop_loss_count": len(stop_trades),
            "ideal_stop_average": round(sum(stop_ideal) / len(stop_ideal), 5) if stop_ideal else None,
            "slippage_stop_average": round(sum(stop_slippage) / len(stop_slippage), 5) if stop_slippage else None,
            "worst_intraday_average": round(sum(stop_worst) / len(stop_worst), 5) if stop_worst else None,
            "note": "理想止损、滑点止损、日内最低压力三种口径并列，避免只看规则价。"
        },
        "promotion_rule": "影子实验只提供证据；样本不足或未经过 Hermes critic 与回测验证前，不提升为主策略。",
    }
    rows: list[dict] = []
    for trade in next_open_trades:
        rows.append({"experiment": "next_day_open_buy", **trade})
    for trade in risk_budget_trades:
        rows.append(
            {
                "experiment": "risk_budget_position",
                "symbol": trade.get("symbol"),
                "name": trade.get("name"),
                "signal_date": trade.get("signal_date"),
                "entry_date": trade.get("entry_date"),
                "exit_date": trade.get("exit_date"),
                "net_return": trade.get("net_return"),
                "position_pct": trade.get("position_pct"),
                "risk_budget_position_pct": trade.get("risk_budget_position_pct"),
                "stop_distance_pct": trade.get("stop_distance_pct"),
                "exit_reason": trade.get("exit_reason"),
                "score": trade.get("score"),
            }
        )
    report = _render_shadow_experiment_report(summary)
    return rows, summary, report
