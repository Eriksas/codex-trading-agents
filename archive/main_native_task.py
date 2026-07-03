"""
⚠️ 已弃用：本架构证明不适合纯计算任务，保留用于对比研究。
实验结论：Claude Code Agent 工具（V4）在 ×N 并行时立即触发会话 quota 限制，
失败原因与 sub-agent 冷启动无关，而是 Agent 工具计入主会话 token 配额。
此路径对批量 sub-agent 场景不可用；subprocess 版（V3）更稳定。
正确架构见 main_v2.py。

main_native_task.py - 原生 Agent 工具版调度脚本（阶段 2.2 对比实验）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
使用方式（重要）：
  此脚本 ≠ 可直接 `python3 main_native_task.py` 的独立程序。

  它是为 Claude Code 主会话设计的"结构化执行规范"：
    - 步骤 1、3 用 Bash 工具执行 Python 代码
    - 步骤 2 用 Claude Code 的 Agent 工具派发 sub-agent
      （Agent 工具是 Claude Code 会话内的工具，不可从 Python 进程中直接调用）

  正确用法：
    在 Claude Code 交互式会话中，发送指令：
      "读取 main_native_task.py 并按其中的流程执行今日分析"
    Claude Code 会逐步执行，在步骤 2 处调用 Agent 工具。

与 main.py（subprocess 版）的关键区别：
  ┌─────────────────┬────────────────────────────┬────────────────────────────┐
  │                 │ main.py (subprocess)       │ main_native_task.py (Agent)│
  ├─────────────────┼────────────────────────────┼────────────────────────────┤
  │ sub-agent 启动  │ subprocess claude CLI      │ Claude Code Agent 工具     │
  │ 冷启动开销      │ ~18s（每次新进程）          │ 无（同会话内派发）          │
  │ 上下文共享      │ 无（完全隔离）              │ 可共享部分系统 prompt       │
  │ 可独立运行      │ ✅ 可                       │ ❌ 需 Claude Code 会话      │
  │ 并行化          │ ThreadPoolExecutor         │ 多个 Agent 调用（同轮次）   │
  │ 成本计量        │ 按 claude 进程计费          │ 按主会话 token 计费         │
  └─────────────────┴────────────────────────────┴────────────────────────────┘
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 步骤定义（Claude Code 按此顺序执行）
# ---------------------------------------------------------------------------

WORKING_DIR = Path(__file__).parent.resolve()


def step1_fetch(watchlist_path: str = "watchlist.json", output_base: str = "output") -> dict:
    """
    步骤 1：数据抓取。
    Claude Code 执行方式：直接 import 并调用（或 Bash 执行 python3 -m src.data_fetcher）
    """
    sys.path.insert(0, str(WORKING_DIR / "src"))
    from data_fetcher import run_data_fetch
    return {"results": run_data_fetch(watchlist_path=watchlist_path, output_base=output_base)}


# ---------------------------------------------------------------------------
# 步骤 2 的 Agent 工具调用规范
# ---------------------------------------------------------------------------

def make_agent_prompt(code: str, stock_name: str, raw_dir: Path, analysis_dir: Path) -> str:
    """
    生成单只股票分析的 Agent prompt（与 main.py 共用 prompts/analysis_agent.md 模板）。

    Claude Code 对每只股票调用一次 Agent(prompt=make_agent_prompt(...))。
    并行化方式：在同一轮工具调用中同时发出多个 Agent 调用（Claude Code 原生支持）。
    """
    template_path = WORKING_DIR / "prompts" / "analysis_agent.md"
    template = template_path.read_text(encoding="utf-8")

    ohlcv_path       = raw_dir / f"{code}_ohlcv.csv"
    fundamental_path = raw_dir / f"{code}_fundamental.json"
    output_path      = analysis_dir / f"{code}.json"

    task_params = (
        f"- CODE: `{code}`\n"
        f"- STOCK_NAME: `{stock_name}`\n"
        f"- OHLCV_PATH: `{ohlcv_path}`\n"
        f"- FUNDAMENTAL_PATH: `{fundamental_path}`\n"
        f"- OUTPUT_PATH: `{output_path}`\n"
        f"- ANALYSIS_DIR: `{analysis_dir}`\n"
        f"- RAW_DIR: `{raw_dir}`\n"
        f"- WORKING_DIR: `{WORKING_DIR}`\n"
    )
    return (
        template
        .replace("{TASK_PARAMS}", task_params)
        .replace("{CODE}",             code)
        .replace("{STOCK_NAME}",       stock_name)
        .replace("{OHLCV_PATH}",       str(ohlcv_path))
        .replace("{FUNDAMENTAL_PATH}", str(fundamental_path))
        .replace("{OUTPUT_PATH}",      str(output_path))
        .replace("{ANALYSIS_DIR}",     str(analysis_dir))
        .replace("{RAW_DIR}",          str(raw_dir))
        .replace("{WORKING_DIR}",      str(WORKING_DIR))
    )


# ---------------------------------------------------------------------------
# Claude Code 执行本脚本时应遵循的步骤（注释形式的"执行规范"）
# ---------------------------------------------------------------------------

"""
【Claude Code 执行规范】

步骤 1：调用 step1_fetch() 或直接运行 data_fetcher
    result = step1_fetch()

步骤 2：对 watchlist 中每只股票，调用 Agent 工具（可在同一轮次并行）
    对每只 stock in watchlist:
        prompt = make_agent_prompt(stock.code, stock.name, raw_dir, analysis_dir)
        Agent(description=f"分析 {stock.name} ({stock.code})", prompt=prompt)

    注意：若在同一轮次发送多个 Agent 调用，Claude Code 会并行执行它们，
    无冷启动开销，总时间接近单只股票的处理时间。

步骤 3：验证所有 analysis/*.json 的 schema 后，调用 reporter
    from reporter import generate_report
    generate_report()
"""


def step3_report(watchlist_path: str = "watchlist.json", output_base: str = "output"):
    """步骤 3：生成报告。Claude Code 执行方式：直接 import 并调用。"""
    from reporter import generate_report
    return generate_report(watchlist_path=watchlist_path, output_base=output_base)


# ---------------------------------------------------------------------------
# 最小可运行入口（仅步骤 1 和 3，步骤 2 需由 Claude Code Agent 工具完成）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("main_native_task.py - 原生 Agent 工具版")
    print("步骤 2 需在 Claude Code 会话中通过 Agent 工具执行")
    print("此处仅打印步骤 2 的 Agent prompt 供参考")
    print("=" * 60)

    today = datetime.today().strftime("%Y-%m-%d")
    raw_dir = Path("output") / today / "raw"
    analysis_dir = Path("output") / today / "analysis"

    with open("watchlist.json", encoding="utf-8") as f:
        stocks = json.load(f)["stocks"]

    for stock in stocks:
        prompt = make_agent_prompt(stock["code"], stock["name"], raw_dir, analysis_dir)
        print(f"\n--- Agent prompt for {stock['name']} ({stock['code']}) ---")
        print(f"[{len(prompt)} chars, 预览前 200 字符]")
        print(prompt[:200])
        print("...")
