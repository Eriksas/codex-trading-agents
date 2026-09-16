"""模拟台账状态转换与归档；不连接实盘。"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

if __package__:
    from .scanner_backtest import (
        _entry_price_for_bar,
        _parse_trigger_zone,
        _stop_execution_metrics,
    )
    from .scanner_config import (
        _cfg,
    )
    from .scanner_data import (
        _parse_bar_date,
    )
    from .scanner_utils import (
        _read_csv_rows,
        _to_float,
        _write_csv,
    )
else:
    from scanner_backtest import (
        _entry_price_for_bar,
        _parse_trigger_zone,
        _stop_execution_metrics,
    )
    from scanner_config import (
        _cfg,
    )
    from scanner_data import (
        _parse_bar_date,
    )
    from scanner_utils import (
        _read_csv_rows,
        _to_float,
        _write_csv,
    )


def _ledger_paths() -> tuple[Path, Path]:
    """返回 pending 台账与归档台账路径。"""
    ledger_dir = Path(str(_cfg("ledger", "dir", "data/ledger")))
    return (
        ledger_dir / str(_cfg("ledger", "pending_file", "pending_trades.csv")),
        ledger_dir / str(_cfg("ledger", "archive_file", "trade_archive.csv")),
    )


def _trade_id(symbol: str, signal_date: str) -> str:
    """生成模拟交易唯一 ID。"""
    return f"{symbol}_{signal_date}"


def _date_diff_days(start: str, end: str) -> int:
    """计算两个 YYYY-MM-DD 日期相差天数。"""
    start_dt = _parse_bar_date(start)
    end_dt = _parse_bar_date(end)
    if start_dt is None or end_dt is None:
        return 0
    return (end_dt.date() - start_dt.date()).days


def _snapshot_bar(row: dict) -> Optional[dict]:
    """将当日快照转换为简化 K 线，用于 pending 台账复核。"""
    open_price = _to_float(row.get("open"))
    high = _to_float(row.get("high"))
    low = _to_float(row.get("low"))
    close = _to_float(row.get("latest") or row.get("close"))
    if close is None:
        return None
    return {
        "open": open_price if open_price is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
    }


def _close_ledger_trade(
    row: dict,
    exit_date: str,
    exit_price: float,
    exit_reason: str,
    execution_metrics: Optional[dict[str, Any]] = None,
) -> dict:
    """关闭台账记录并计算模拟收益。"""
    entry_price = _to_float(row.get("entry_price"))
    net_return = None
    gross_return = None
    if entry_price:
        gross_return = exit_price / entry_price - 1
        net_return = gross_return - (_cfg("backtest", "cost_bps", 15) / 10000)
    row.update(
        {
            "status": "closed" if exit_reason != "expired_no_entry" else "expired",
            "exit_date": exit_date,
            "exit_price": round(exit_price, 3),
            "exit_reason": exit_reason,
            "gross_return": round(gross_return, 5) if gross_return is not None else "",
            "net_return": round(net_return, 5) if net_return is not None else "",
            "last_update": datetime.now().isoformat(),
        }
    )
    if execution_metrics:
        row.update(execution_metrics)
    return row


def _normalize_position_audit_fields(row: dict) -> dict:
    """为旧台账行补齐仓位审计字段。"""
    base_position = _to_float(row.get("base_position_pct"))
    position = _to_float(row.get("position_pct"))
    multiplier = _to_float(row.get("market_position_multiplier"))
    if base_position is not None and position is not None and not row.get("effective_position_multiplier"):
        row["effective_position_multiplier"] = round(position / base_position, 4) if base_position else ""
    if base_position is not None and multiplier is not None and not row.get("raw_position_pct"):
        row["raw_position_pct"] = round(base_position * multiplier, 2)
    if not row.get("position_adjustment_note") and base_position is not None and position is not None:
        if multiplier is not None:
            row["position_adjustment_note"] = (
                f"基础{base_position:g}% × 环境系数{multiplier:g} = {(base_position * multiplier):.2f}%，"
                f"实际{position:g}%"
            )
        else:
            row["position_adjustment_note"] = f"基础{base_position:g}%，实际{position:g}%"
    return row


def _update_trade_ledger(candidates: list[dict], stocks: list[dict], today: str, output_dir: Path) -> dict:
    """滚动维护 pending/open/closed 模拟交易台账。"""
    pending_path, archive_path = _ledger_paths()
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    active_rows = _read_csv_rows(pending_path)
    archive_rows = _read_csv_rows(archive_path)
    archive_ids = {row.get("trade_id") for row in archive_rows}
    stock_map = {str(row.get("symkey")): row for row in stocks if row.get("symkey")}
    entry_window_days = int(_cfg("backtest", "entry_window_days", 2))
    max_hold_days = int(_cfg("backtest", "max_hold_days", 5))
    stop_slippage_bps = float(_cfg("backtest", "stop_slippage_bps", 30))
    same_symbol_policy = str(_cfg("ledger", "same_symbol_active_policy", "skip")).lower()

    updates: list[dict] = []
    next_active: list[dict] = []
    closed_rows: list[dict] = []
    skipped_same_symbol_count = 0

    for row in active_rows:
        row = _normalize_position_audit_fields(row)
        status = row.get("status") or "pending"
        symbol = row.get("symbol") or ""
        signal_date = row.get("signal_date") or today
        bar = _snapshot_bar(stock_map.get(symbol) or {})
        action = "kept"

        if signal_date == today:
            next_active.append(row)
            updates.append({**row, "ledger_action": action})
            continue

        if status == "pending":
            if _date_diff_days(signal_date, today) > entry_window_days:
                row = _close_ledger_trade(row, today, _to_float(row.get("signal_price")) or 0.0, "expired_no_entry")
                closed_rows.append(row)
                action = "expired_no_entry"
            elif bar:
                trigger_low, trigger_high = _parse_trigger_zone(str(row.get("trigger_zone")))
                entry_price = _entry_price_for_bar(bar, trigger_low, trigger_high)
                if entry_price is not None:
                    row.update(
                        {
                            "status": "open",
                            "entry_date": today,
                            "entry_price": round(entry_price, 3),
                            "holding_days": 1,
                            "last_update": datetime.now().isoformat(),
                        }
                    )
                    status = "open"
                    action = "entered"

        if status == "open":
            if bar:
                stop_loss = _to_float(row.get("stop_loss")) or 0.0
                first_take_profit = _to_float(row.get("first_take_profit")) or 0.0
                holding_days = max(1, _date_diff_days(row.get("entry_date") or today, today) + 1)
                row["holding_days"] = holding_days
                if bar["low"] <= stop_loss:
                    entry_price = _to_float(row.get("entry_price")) or stop_loss
                    stop_metrics = _stop_execution_metrics(
                        entry_price,
                        stop_loss,
                        bar.get("low"),
                        float(_cfg("backtest", "cost_bps", 15)),
                        stop_slippage_bps,
                    )
                    row = _close_ledger_trade(row, today, stop_loss, "stop_loss", stop_metrics)
                    closed_rows.append(row)
                    action = "closed_stop_loss"
                elif bar["high"] >= first_take_profit:
                    row = _close_ledger_trade(row, today, first_take_profit, "take_profit")
                    closed_rows.append(row)
                    action = "closed_take_profit"
                elif holding_days >= max_hold_days:
                    row = _close_ledger_trade(row, today, bar["close"], "timeout")
                    closed_rows.append(row)
                    action = "closed_timeout"
                else:
                    row["last_update"] = datetime.now().isoformat()
                    next_active.append(row)
                    action = "open_kept"
            else:
                row["last_update"] = datetime.now().isoformat()
                next_active.append(row)
                action = "missing_snapshot"
        elif row.get("status") in {"closed", "expired"}:
            pass
        elif action == "kept":
            row["last_update"] = datetime.now().isoformat()
            next_active.append(row)

        updates.append({**row, "ledger_action": action})

    active_ids = {row.get("trade_id") for row in next_active}
    active_by_symbol: dict[str, list[dict]] = {}
    for row in next_active:
        if (row.get("status") or "pending") in {"pending", "open"} and row.get("symbol"):
            active_by_symbol.setdefault(str(row.get("symbol")), []).append(row)
    for item in candidates:
        symbol = item.get("symbol")
        if not symbol:
            continue
        trade_id = _trade_id(symbol, today)
        if trade_id in active_ids or trade_id in archive_ids:
            continue
        overlapping = active_by_symbol.get(str(symbol), [])
        if overlapping and same_symbol_policy == "skip":
            skipped_same_symbol_count += 1
            overlapping_ids = ",".join(str(row.get("trade_id") or "") for row in overlapping if row.get("trade_id"))
            overlap_note = f"同标的已有 active/pending：{overlapping_ids}，本轮不新增 pending"
            item["same_symbol_overlap"] = True
            item["same_symbol_active_trade_ids"] = overlapping_ids
            item["overlap_risk_note"] = overlap_note
            item["plan"] = f"{item.get('plan') or ''}；{overlap_note}"
            skipped_row = {
                "trade_id": trade_id,
                "status": "skipped_same_symbol",
                "symbol": symbol,
                "name": item.get("name"),
                "signal_date": today,
                "entry_date": "",
                "exit_date": "",
                "signal_price": item.get("signal_price"),
                "trigger_zone": item.get("trigger_zone"),
                "entry_price": "",
                "exit_price": "",
                "stop_loss": item.get("stop_loss"),
                "first_take_profit": item.get("first_take_profit"),
                "base_position_pct": item.get("base_position_pct", item.get("position_pct")),
                "raw_position_pct": item.get("raw_position_pct"),
                "position_cap_pct": item.get("position_cap_pct"),
                "position_pct": 0,
                "market_regime_label": item.get("market_regime_label"),
                "market_position_multiplier": item.get("market_position_multiplier"),
                "effective_position_multiplier": 0,
                "position_adjustment_note": item.get("position_adjustment_note"),
                "holding_days": "",
                "exit_reason": "skipped_same_symbol_active",
                "gross_return": "",
                "net_return": "",
                "stop_low_price": "",
                "stop_breach_pct": "",
                "stop_slippage_bps": stop_slippage_bps,
                "slippage_exit_price": "",
                "net_return_worst_intraday": "",
                "net_return_slippage": "",
                "slippage_vs_ideal_return": "",
                "same_symbol_active_trade_ids": overlapping_ids,
                "overlap_risk_note": overlap_note,
                "plan": item.get("plan"),
                "last_update": datetime.now().isoformat(),
            }
            updates.append({**skipped_row, "ledger_action": "skipped_same_symbol_active"})
            continue
        row = {
            "trade_id": trade_id,
            "status": "pending",
            "symbol": symbol,
            "name": item.get("name"),
            "signal_date": today,
            "entry_date": "",
            "exit_date": "",
            "signal_price": item.get("signal_price"),
            "trigger_zone": item.get("trigger_zone"),
            "entry_price": "",
            "exit_price": "",
            "stop_loss": item.get("stop_loss"),
            "first_take_profit": item.get("first_take_profit"),
            "base_position_pct": item.get("base_position_pct", item.get("position_pct")),
            "raw_position_pct": item.get("raw_position_pct"),
            "position_cap_pct": item.get("position_cap_pct"),
            "position_pct": item.get("position_pct"),
            "market_regime_label": item.get("market_regime_label"),
            "market_position_multiplier": item.get("market_position_multiplier"),
            "effective_position_multiplier": item.get("effective_position_multiplier"),
            "position_adjustment_note": item.get("position_adjustment_note"),
            "holding_days": "",
            "exit_reason": "",
            "gross_return": "",
            "net_return": "",
            "stop_low_price": "",
            "stop_breach_pct": "",
            "stop_slippage_bps": stop_slippage_bps,
            "slippage_exit_price": "",
            "net_return_worst_intraday": "",
            "net_return_slippage": "",
            "slippage_vs_ideal_return": "",
            "same_symbol_active_trade_ids": "",
            "overlap_risk_note": "",
            "plan": item.get("plan"),
            "last_update": datetime.now().isoformat(),
        }
        next_active.append(row)
        active_ids.add(trade_id)
        active_by_symbol.setdefault(str(symbol), []).append(row)
        updates.append({**row, "ledger_action": "new_pending"})

    ledger_fields = [
        "trade_id", "status", "symbol", "name", "signal_date", "entry_date", "exit_date",
        "signal_price", "trigger_zone", "entry_price", "exit_price", "stop_loss",
        "first_take_profit", "base_position_pct", "raw_position_pct", "position_cap_pct",
        "position_pct", "market_regime_label", "market_position_multiplier",
        "effective_position_multiplier", "position_adjustment_note", "holding_days",
        "exit_reason", "gross_return", "net_return", "stop_low_price", "stop_breach_pct",
        "stop_slippage_bps", "slippage_exit_price", "net_return_worst_intraday",
        "net_return_slippage", "slippage_vs_ideal_return", "same_symbol_active_trade_ids",
        "overlap_risk_note", "plan", "last_update",
    ]
    _write_csv(pending_path, next_active, ledger_fields)
    archive_combined = list(archive_rows)
    for row in closed_rows:
        if row.get("trade_id") not in archive_ids:
            archive_combined.append(row)
            archive_ids.add(row.get("trade_id"))
    _write_csv(archive_path, archive_combined, ledger_fields)
    _write_csv(output_dir / "ledger_updates.csv", updates, [*ledger_fields, "ledger_action"])

    return {
        "active_count": len(next_active),
        "new_pending_count": sum(1 for row in updates if row.get("ledger_action") == "new_pending"),
        "skipped_same_symbol_count": skipped_same_symbol_count,
        "closed_count": len(closed_rows),
        "pending_path": str(pending_path),
        "archive_path": str(archive_path),
        "updates_path": str(output_dir / "ledger_updates.csv"),
    }
