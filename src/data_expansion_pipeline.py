"""
data_expansion_pipeline.py - Expand historical data for V3 validation

优先尝试 Fuyao，失败时尝试 Tushare；两者不可用时降级为本地缓存。
本模块只写研究缓存和数据质量报告，不连接实盘，不修改主策略。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import market_scanner as scanner

DEFAULT_OUTPUT_DIR = Path("output/main_strategy_upgrade_v3/data_expansion")
DEFAULT_EXPANDED_DIR = Path("data/expanded/v3")


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    """写 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def _date_ms(date_text: str) -> int:
    """日期转毫秒。"""
    return int(datetime.strptime(date_text, "%Y-%m-%d").timestamp() * 1000)


def _symbol_to_cache_name(symbol: str) -> str:
    """转换缓存文件名。"""
    return "".join(ch if ch.isalnum() else "_" for ch in symbol)


def _save_bars(directory: Path, symbol: str, bars: list[dict]) -> None:
    """保存 K 线。"""
    fields = ["date", "date_ms", "open", "high", "low", "close", "volume", "turnover", "source"]
    rows = [{**bar, "source": bar.get("source") or "unknown"} for bar in bars]
    _write_csv(directory / f"{_symbol_to_cache_name(symbol)}.csv", rows, fields)


def _merge_bars(*bar_sets: list[dict]) -> list[dict]:
    """按 date 合并去重，后来的来源覆盖前者。"""
    merged: dict[str, dict] = {}
    for bars in bar_sets:
        for bar in bars:
            date = str(bar.get("date") or "")
            if date:
                merged[date] = bar
    return sorted(merged.values(), key=lambda item: str(item.get("date") or ""))


def _load_local_histories() -> dict[str, list[dict]]:
    """读取当前本地缓存。"""
    return fr.load_cached_histories(cache_dir=fr.DEFAULT_CACHE_DIR, min_bars=1)


def _fuyao_fetch(symbol: str, start: str, end: str) -> tuple[list[dict], Optional[str]]:
    """尝试用 Fuyao 拉取 K 线。"""
    try:
        bars = scanner._fetch_fuyao_bars(
            "/api/a-share/prices/historical",
            {
                "thscode": symbol,
                "interval": "1d",
                "start": _date_ms(start),
                "end": _date_ms(end),
                "adjust": "none",
            },
        )
        return [{**bar, "source": "fuyao"} for bar in bars], None
    except Exception as exc:
        return [], str(exc)


def _tushare_fetch(symbol: str, start: str, end: str) -> tuple[list[dict], Optional[str]]:
    """尝试用 Tushare 拉取 K 线。"""
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        return [], "missing TUSHARE_TOKEN"
    try:
        import tushare as ts  # type: ignore

        pro = ts.pro_api(token)
        ts_code = symbol
        df = pro.daily(ts_code=ts_code, start_date=start.replace("-", ""), end_date=end.replace("-", ""))
        rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            date = datetime.strptime(str(row["trade_date"]), "%Y%m%d").strftime("%Y-%m-%d")
            rows.append(
                {
                    "date": date,
                    "date_ms": _date_ms(date),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("vol"),
                    "turnover": row.get("amount"),
                    "source": "tushare",
                }
            )
        return sorted(rows, key=lambda item: item["date"]), None
    except Exception as exc:
        return [], str(exc)


def _build_trade_calendar(histories: dict[str, list[dict]]) -> list[dict]:
    """从已取得数据生成交易日历。"""
    dates = sorted({str(bar.get("date")) for bars in histories.values() for bar in bars if bar.get("date")})
    return [{"date": date, "is_open": 1} for date in dates]


def _build_daily_universe(histories: dict[str, list[dict]], min_history: int = 60) -> list[dict]:
    """逐日动态 universe 统计，禁止用未来信息。"""
    dates = sorted({str(bar.get("date")) for bars in histories.values() for bar in bars if bar.get("date")})
    rows: list[dict[str, Any]] = []
    for date in dates:
        count = 0
        insufficient = 0
        for bars in histories.values():
            past = [bar for bar in bars if str(bar.get("date")) <= date]
            if not past or str(past[-1].get("date")) != date:
                continue
            if len(past) < min_history:
                insufficient += 1
                continue
            count += 1
        rows.append({"date": date, "stock_count": count, "insufficient_history": insufficient})
    return rows


def run_data_expansion(
    *,
    start: str = "2024-01-01",
    end: Optional[str] = None,
    expanded_dir: Path = DEFAULT_EXPANDED_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    max_symbols: Optional[int] = None,
) -> dict[str, Any]:
    """执行数据扩展，失败时降级到当前本地缓存。"""
    end = end or datetime.now().strftime("%Y-%m-%d")
    scanner._set_active_config(scanner._load_scanner_config("strategy.json"))
    local_histories = _load_local_histories()
    symbols = sorted(local_histories)
    if max_symbols is not None:
        symbols = symbols[:max_symbols]
    expanded: dict[str, list[dict]] = {}
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        local = [{**bar, "source": "local_cache"} for bar in local_histories.get(symbol, [])]
        fuyao_bars, fuyao_error = _fuyao_fetch(symbol, start, end)
        tushare_bars: list[dict] = []
        tushare_error: Optional[str] = None
        if not fuyao_bars:
            tushare_bars, tushare_error = _tushare_fetch(symbol, start, end)
        merged = _merge_bars(local, tushare_bars, fuyao_bars)
        expanded[symbol] = merged
        _save_bars(expanded_dir / "daily_kline", symbol, merged)
        _save_bars(expanded_dir / "adj_kline", symbol, merged)
        rows.append(
            {
                "symbol": symbol,
                "local_bars": len(local),
                "fuyao_bars": len(fuyao_bars),
                "tushare_bars": len(tushare_bars),
                "merged_bars": len(merged),
                "start_date": merged[0].get("date") if merged else "",
                "end_date": merged[-1].get("date") if merged else "",
                "fuyao_error": fuyao_error or "",
                "tushare_error": tushare_error or "",
                "final_source": "fuyao" if fuyao_bars else ("tushare" if tushare_bars else "local_cache"),
            }
        )
    _write_csv(output_dir / "data_expansion_sources.csv", rows, list(rows[0].keys()) if rows else ["symbol"])
    trade_calendar = _build_trade_calendar(expanded)
    universe = _build_daily_universe(expanded)
    _write_csv(expanded_dir / "trade_calendar.csv", trade_calendar, ["date", "is_open"])
    _write_csv(expanded_dir / "daily_universe.csv", universe, ["date", "stock_count", "insufficient_history"])
    stock_basic = [{"symbol": symbol, "source": "local_cache_or_provider"} for symbol in symbols]
    _write_csv(expanded_dir / "stock_basic.csv", stock_basic, ["symbol", "source"])
    _write_csv(expanded_dir / "daily_basic.csv", [{"symbol": symbol} for symbol in symbols], ["symbol"])
    _write_csv(expanded_dir / "industry_or_sector.csv", [{"symbol": symbol, "sector": "unknown"} for symbol in symbols], ["symbol", "sector"])
    index_dir = Path("data/cache/market_scanner/index")
    index_rows: list[dict[str, Any]] = []
    for path in sorted(index_dir.glob("*.csv")):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row["index_code"] = path.stem
                index_rows.append(row)
    if index_rows:
        _write_csv(expanded_dir / "index_daily.csv", index_rows, list(index_rows[0].keys()))
    coverage_text = [
        "# Universe Coverage Report",
        "",
        f"- Requested range: {start} to {end}",
        f"- Symbols attempted: {len(symbols)}",
        f"- Trade dates: {len(trade_calendar)}",
        f"- Expanded directory: `{expanded_dir}`",
        "",
        "This universe is generated per date from data available up to that date; it does not backfill history from the current stock pool beyond available local/provider data.",
    ]
    (output_dir / "universe_coverage_report.md").write_text("\n".join(coverage_text) + "\n", encoding="utf-8")
    quality_text = [
        "# Data Quality Report",
        "",
        f"- Fuyao successes: {sum(1 for row in rows if row['fuyao_bars'])}",
        f"- Tushare successes: {sum(1 for row in rows if row['tushare_bars'])}",
        f"- Local cache fallback: {sum(1 for row in rows if row['final_source'] == 'local_cache')}",
        "",
        "Provider token, permission, or rate-limit failures are recorded in `data_expansion_sources.csv`; fallback to local cache does not interrupt the main workflow.",
    ]
    (output_dir / "data_quality_report.md").write_text("\n".join(quality_text) + "\n", encoding="utf-8")
    summary = {
        "start": start,
        "end": end,
        "symbol_count": len(symbols),
        "trade_date_count": len(trade_calendar),
        "expanded_dir": str(expanded_dir),
        "output_dir": str(output_dir),
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="扩展 V3 回测数据，失败时降级本地缓存")
    parser.add_argument("--start", default="2024-01-01", help="起始日期")
    parser.add_argument("--end", help="结束日期，默认今日")
    parser.add_argument("--expanded-dir", default=str(DEFAULT_EXPANDED_DIR), help="扩展数据目录")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="报告输出目录")
    parser.add_argument("--max-symbols", type=int, help="最多扩展多少只股票")
    args = parser.parse_args()
    summary = run_data_expansion(
        start=args.start,
        end=args.end,
        expanded_dir=Path(args.expanded_dir),
        output_dir=Path(args.output),
        max_symbols=args.max_symbols,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
