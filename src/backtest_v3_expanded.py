"""
backtest_v3_expanded.py - Expanded-data backtest for alpha040_v3_risk_controlled

优先读取 data/expanded/v3/daily_kline；不可用时回落到当前本地缓存。
本模块只输出研究回测，不连接实盘，不自动下单。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

import factor_research as fr
import factor_research_round6 as r6

DEFAULT_EXPANDED_CACHE = Path("data/expanded/v3/daily_kline")
DEFAULT_OUTPUT_DIR = Path("output/main_strategy_upgrade_v3/expanded_backtest")


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    """写 CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def _cache_dir_or_fallback(cache_dir: Path) -> tuple[Path, str]:
    """选择可用缓存目录。"""
    if cache_dir.resolve() == fr.DEFAULT_CACHE_DIR.resolve():
        return cache_dir, "local_cache_explicit"
    if cache_dir.exists() and any(cache_dir.glob("*.csv")):
        return cache_dir, "expanded"
    return fr.DEFAULT_CACHE_DIR, "local_cache_fallback"


def _summarize_group(trades: pd.DataFrame, field: str) -> pd.DataFrame:
    """按字段汇总交易表现。"""
    rows: list[dict[str, Any]] = []
    if trades.empty or field not in trades.columns:
        return pd.DataFrame()
    for value, group in trades.groupby(field, dropna=False):
        returns = pd.to_numeric(group["net_return"], errors="coerce").dropna()
        rows.append(
            {
                field: value if value else "unknown",
                "trade_count": int(len(group)),
                "win_rate": round(float((returns > 0).mean()), 6) if len(returns) else None,
                "average_return": round(float(returns.mean()), 6) if len(returns) else None,
                "median_return": round(float(returns.median()), 6) if len(returns) else None,
                "stop_loss_count": int((group.get("exit_reason") == "stop_loss").sum()),
                "limit_down_blocked_exit_count": int((group.get("exit_reason") == "limit_down_blocked_exit").sum()),
            }
        )
    return pd.DataFrame(rows)


def run_expanded_backtest(
    *,
    cache_dir: Path = DEFAULT_EXPANDED_CACHE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    """运行扩展数据回测。"""
    selected_cache, source = _cache_dir_or_fallback(cache_dir)
    summary = r6.run_factor_research_round6(output_dir=output_dir, cache_dir=selected_cache)
    v3_path = output_dir / "v3_atr_risk_budget_hot5_vol_risk_on_trades.csv"
    trades = pd.read_csv(v3_path, encoding="utf-8-sig") if v3_path.exists() else pd.DataFrame()
    accepted_path = output_dir / "v3_atr_risk_budget_hot5_vol_risk_on_accepted_trades.csv"
    accepted = pd.read_csv(accepted_path, encoding="utf-8-sig") if accepted_path.exists() else pd.DataFrame()
    if not trades.empty:
        trades["year"] = pd.to_datetime(trades["signal_date"]).dt.year
        trades["month"] = pd.to_datetime(trades["signal_date"]).dt.strftime("%Y-%m")
    _write_csv(output_dir / "yearly_performance.csv", _summarize_group(trades, "year"))
    _write_csv(output_dir / "monthly_trade_performance.csv", _summarize_group(trades, "month"))
    _write_csv(output_dir / "market_regime_performance.csv", _summarize_group(trades, "market_regime_label"))
    _write_csv(output_dir / "industry_performance.csv", _summarize_group(trades, "industry"))
    _write_csv(output_dir / "exit_reason_performance.csv", _summarize_group(trades, "exit_reason"))
    cost_summary = pd.read_csv(output_dir / "strategy_comparison.csv", encoding="utf-8-sig") if (output_dir / "strategy_comparison.csv").exists() else pd.DataFrame()
    _write_csv(output_dir / "risk_budget_audit.csv", accepted)
    report = [
        "# Expanded V3 Backtest Report",
        "",
        f"- Data source: {source}",
        f"- Cache dir: `{selected_cache}`",
        f"- Strategy: alpha040_v3_risk_controlled",
        "",
        "Outputs include全区间表现、按年份/月度/市场环境/行业/退出原因表现、成本和滑点压力测试占位、风险预算口径审计。",
        "",
        "If expanded provider data is unavailable, this report explicitly falls back to local cache.",
    ]
    (output_dir / "expanded_backtest_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    result = {
        **summary,
        "data_source": source,
        "selected_cache": str(selected_cache),
        "accepted_trades": int(len(accepted)),
    }
    _write_json(output_dir / "expanded_backtest_summary.json", result)
    return result


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行 V3 扩展数据回测")
    parser.add_argument("--cache-dir", default=str(DEFAULT_EXPANDED_CACHE), help="扩展 K 线目录")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    args = parser.parse_args()
    summary = run_expanded_backtest(cache_dir=Path(args.cache_dir), output_dir=Path(args.output))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
