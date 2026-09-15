"""项目主展示入口：离线示例与已有数据的策略健康诊断。"""

import argparse
import logging
from pathlib import Path

from src.diagnosis import ROOT, run_diagnosis


def main() -> int:
    """解析任务参数，输出报告路径和明确的退出状态。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="用仓库合成样例离线生成一份诊断报告，无需安装依赖或密钥")
    demo.add_argument("--interpretation", type=Path, help="可选：导入针对相同示例事实包的回答 JSON")
    diagnose = commands.add_parser("diagnose", help="读取已有复盘 CSV 和归档，不抓行情、不推送")
    diagnose.add_argument("--date", required=True, help="统计截止日期 YYYY-MM-DD")
    diagnose.add_argument("--input-root", type=Path, default=ROOT / "output", help="已有 YYYY-MM-DD/scan 目录的父目录")
    diagnose.add_argument("--archive", type=Path, default=ROOT / "data/ledger/trade_archive.csv", help="已有模拟归档 CSV")
    diagnose.add_argument("--question", default="现有记录是否足以支持策略表现稳定？", help="要分析的问题")
    diagnose.add_argument("--interpretation", type=Path, help="可选：导入与本次 facts_sha256 一致的 Agent 回答 JSON")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "demo":
            sample = ROOT / "examples/diagnosis"
            result, code = run_diagnosis(
                input_root=sample / "reviews", archive_path=sample / "trade_archive.csv",
                as_of_date="2026-01-16", source_kind="synthetic_demo",
                question="已有归档的平均净收益为正吗？这些样本能证明策略稳定吗？",
                interpretation_path=args.interpretation,
            )
        else:
            result, code = run_diagnosis(
                input_root=args.input_root, archive_path=args.archive, as_of_date=args.date,
                question=args.question, interpretation_path=args.interpretation,
            )
        print(f"报告：{result['report_path']}")
        print(f"状态：{result['status']}；模型调用：否；人工决定：待确认")
        return code
    except (OSError, ValueError) as exc:
        logging.error("诊断未完成：%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
