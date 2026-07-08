"""
fuyao_client.py - 同花顺扶摇 API 客户端（新端点族接入，2026-07-08）

能力勘探见 docs/fuyao_api_capability_2026-07.md。路径按官方约定
`/api/<标的宇宙>/<数据类型>/<动作>`，由 MCP 工具名反推并经冒烟验证；
`smoke()` 会逐端点实测并打印可用性。

使用红线（AGENTS.md）：
1. fuyao 指数历史失真（已知）——禁用；
2. **fuyao 复权价失真（2026-07-08 实测）**：adjust=backward 的日收益率相对
   BaoStock/交易所口径被恒定阻尼约 0.78 倍——prices 端点只允许 adjust="none"，
   复权一律用 BaoStock hfq 或由 corporate-actions 事件流自行推导；
3. 原始不复权价已验证干净（与 BaoStock 逐日一致）；
4. 热榜/龙虎榜等特色数据仅保留近一年（date 超期返回 1003），启用前逐族交叉验证。

待办：market-dumps（全市场 10 年日K + 复权因子 Parquet 预签名下载）端点
未在聚合文档中，需从浏览器文档页确认后补充。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import requests

BASE_URL = "https://fuyao.aicubes.cn"
logger = logging.getLogger(__name__)


def _api_key() -> str:
    key = os.environ.get("FUYAO_API_KEY")
    if key:
        return key.strip()
    path = Path.home() / ".secrets" / "fuyao_api_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise RuntimeError("缺少 FUYAO_API_KEY（环境变量或 ~/.secrets/fuyao_api_key）")


def get(path: str, params: Optional[dict[str, Any]] = None, timeout: int = 25) -> Any:
    """统一 GET；返回 data 字段，业务错误抛异常（含 code）。"""
    resp = requests.get(
        f"{BASE_URL}{path}", params=params or {},
        headers={"X-api-key": _api_key(), "User-Agent": "codex-trading-agents/1.0"},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"fuyao {path} code={payload.get('code')}: {payload.get('message')}")
    return payload.get("data")


# --- 行情（支持复权）---

def prices_historical(thscode: str, start_ms: int, end_ms: int, interval: str = "1d") -> Any:
    """日K（毫秒时间戳窗口）。只取原始价：adjust 固定 none——
    fuyao 复权层已实测失真（收益阻尼 ~0.78），禁止使用。"""
    return get("/api/a-share/prices/historical",
               {"thscode": thscode, "start": start_ms, "end": end_ms,
                "adjust": "none", "interval": interval})


# --- 除复权事件流 ---

def adjustment_factors(thscode: str) -> Any:
    """分红/送股/配股原始事件流。"""
    return get("/api/a-share/corporate-actions/adjustment-factors", {"thscode": thscode})


# --- 财务 ---

def financial_indicators(thscode: str, report: str) -> Any:
    """五类财务指标；report 格式 {yyyy}-{1|2|3|4}（年-季），如 "2025-4"。"""
    return get("/api/a-share/financials/indicators", {"thscode": thscode, "report": report})


def income_statements(thscode: str, period: str = "annual") -> Any:
    """利润表多期序列；period 如 annual。"""
    return get("/api/a-share/financials/income-statements", {"thscode": thscode, "period": period})


# --- 特色数据（近一年）---

def limit_up_pool(date: str) -> Any:
    """涨停股票池（已验证 2024-01 起历史可查）。"""
    return get("/api/a-share/special-data/limit-up-pool", {"date": date})


def limit_up_ladder(date: str) -> Any:
    """连板天梯。"""
    return get("/api/a-share/special-data/limit-up-ladder", {"date": date})


def hot_stock_list(date: Optional[str] = None) -> Any:
    """同花顺热榜（当日）。"""
    return get("/api/a-share/special-data/hot-stock-list", {"date": date} if date else {})


def hot_stock_list_history(date: str) -> Any:
    """热榜历史（仅近一年，超期 code=1003）。"""
    return get("/api/a-share/special-data/hot-stock-list-history", {"date": date})


def hot_stock_rank_trend(thscode: str, start_date: str, end_date: str) -> Any:
    """单股热度排名走势。"""
    return get("/api/a-share/special-data/hot-stock-rank-trend",
               {"thscode": thscode, "start_date": start_date, "end_date": end_date})


def dragon_tiger_list(date: str) -> Any:
    """龙虎榜（近一年）。"""
    return get("/api/a-share/special-data/dragon-tiger-list", {"date": date})


# --- 同花顺指数/板块 ---

def ths_index_list(**params: Any) -> Any:
    return get("/api/a-share-index/catalog/ths-index-list", params)


def ths_index_constituents(thscode: str) -> Any:
    return get("/api/a-share-index/constituents/ths-stock-list", {"thscode": thscode})


def trading_days() -> Any:
    """近一年交易日。"""
    return get("/api/a-share/calendar/trading-days")


# --- 全市场 Parquet 导出（预签名下载链接）---

def dump_download_url(kind: str) -> Any:
    """kind: daily-k（10年全量）| daily-k-10d（近10交易日增量）| adjustment-factors。

    已验证（2026-07-08）：10y 日K 1,013 万行 5,523 只 2016~今日、茅台已知值命中，
    但不含退市股（幸存者补丁仍走 BaoStock）；复权因子事件流与 BaoStock hfq
    跳变对账偏差 0.00036%。数据落地目录：data/expanded/fuyao_dumps/。
    """
    assert kind in ("daily-k", "daily-k-10d", "adjustment-factors"), kind
    return get(f"/api/dump/market-dumps/{kind}/download-url", {})


def smoke() -> None:
    """逐端点冒烟：打印可用性与样本字段。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cases = [
        ("prices_raw", lambda: prices_historical("000001.SZ", 1780272000000, 1781049600000)),
        ("adjustment_factors", lambda: adjustment_factors("600519.SH")),
        ("financial_indicators", lambda: financial_indicators("600519.SH", "2025-4")),
        ("income_statements", lambda: income_statements("600519.SH")),
        ("limit_up_pool", lambda: limit_up_pool("2026-07-03")),
        ("limit_up_ladder", lambda: limit_up_ladder("2026-07-03")),
        ("hot_stock_list", lambda: hot_stock_list()),
        ("hot_history(近1年)", lambda: hot_stock_list_history("2026-06-20")),
        ("rank_trend", lambda: hot_stock_rank_trend("000001.SZ", "2026-06-01", "2026-07-01")),
        ("dragon_tiger", lambda: dragon_tiger_list("2026-07-03")),
        ("ths_index_list", lambda: ths_index_list()),
        ("trading_days", lambda: trading_days()),
    ]
    for name, fn in cases:
        try:
            data = fn()
            if isinstance(data, dict):
                inner = data.get("item") or data.get("items") or data
                n = len(inner) if hasattr(inner, "__len__") else "?"
                keys = list(inner[0].keys())[:6] if isinstance(inner, list) and inner else list(data.keys())[:6]
            elif isinstance(data, list):
                n, keys = len(data), (list(data[0].keys())[:6] if data else [])
            else:
                n, keys = "?", []
            logger.info("✓ %-22s 条数=%s 字段样本=%s", name, n, keys)
        except Exception as exc:  # noqa: BLE001 - 冒烟即为暴露失败
            logger.info("✗ %-22s %s", name, str(exc)[:110])


if __name__ == "__main__":
    smoke()
