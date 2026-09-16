"""扫描器内部的数值转换、格式化与 CSV I/O；不包含策略判断。"""

from pathlib import Path
from typing import Any, Optional
import csv


def _to_float(value: Any) -> Optional[float]:
    """安全转换为 float。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: Optional[float], digits: int = 2) -> str:
    """将小数涨跌幅格式化为百分比。"""
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%"


def _fmt_yi(value: Optional[float], digits: int = 1) -> str:
    """将元格式化为亿元。"""
    if value is None:
        return "-"
    return f"{value / 1e8:.{digits}f}亿"


def _fmt_source(source: str) -> str:
    """数据源展示名。"""
    mapping = {
        "fuyao": "同花顺扶摇 API / fuyao.aicubes.cn",
        "ftshare": "FTShare-market-data / market.ft.tech",
    }
    return mapping.get(source, source)


def _symbol_cn_suffix(symkey: str) -> str:
    """将 FTShare symkey 转为常见 SH/SZ/BJ 后缀。"""
    return symkey.replace(".XSHG", ".SH").replace(".XSHE", ".SZ").replace(".BJSE", ".BJ")


def _symbol_fuyao_to_scan(thscode: str) -> str:
    """扶摇 thscode 已是常见 SH/SZ/BJ 后缀，保持原样。"""
    return thscode


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    """写 CSV。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _read_csv_rows(path: Path) -> list[dict]:
    """读取 CSV，不存在时返回空列表。"""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))
