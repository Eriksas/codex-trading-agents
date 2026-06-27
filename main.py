"""
⚠️ 已弃用：本架构证明不适合纯计算任务，保留用于对比研究。
实验结论：sub-agent 冷启动 ~18s vs analyzer 直接调用 ~0.3s，overhead 60×。
纯计算任务（技术指标、数据聚合）不应拆分为 sub-agent；LLM 推理任务才值得。
正确架构见 main_v2.py。

main.py - 主调度脚本（阶段 2.1 / 2.2）

流程：
  步骤 1：data_fetcher.run_data_fetch()      — 直接函数调用
  步骤 2：analysis sub-agent × N 只股票      — claude CLI 子进程（串行 or 并行）
  步骤 3：reporter.generate_report()         — 直接函数调用

Sub-agent 通过 claude -p 调用，传入自包含的 prompt（含股票代码、文件路径、输出路径）。
主进程等待每个 sub-agent 结束，解析最后一行 JSON 作为状态报告。

使用方式：
  python3 main.py               # 默认并行（max_workers=5）
  python3 main.py --serial      # 串行模式（阶段 2.1 兼容）
"""

import json
import logging
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

WORKING_DIR = Path(__file__).parent.resolve()
PROMPT_TEMPLATE_PATH = WORKING_DIR / "prompts" / "analysis_agent.md"
AGENT_TIMEOUT_SECONDS = 120  # 单只股票分析的最长等待时间


# ---------------------------------------------------------------------------
# Sub-agent 调度
# ---------------------------------------------------------------------------

def _build_agent_prompt(
    code: str,
    stock_name: str,
    raw_dir: Path,
    analysis_dir: Path,
) -> str:
    """
    将 analysis_agent.md 模板填充为具体股票的自包含 prompt。

    Args:
        code:         6 位股票代码
        stock_name:   股票中文名
        raw_dir:      output/YYYY-MM-DD/raw/ 绝对路径
        analysis_dir: output/YYYY-MM-DD/analysis/ 绝对路径
    Returns:
        填充后的 prompt 字符串
    """
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")

    ohlcv_path      = raw_dir / f"{code}_ohlcv.csv"
    fundamental_path = raw_dir / f"{code}_fundamental.json"
    output_path     = analysis_dir / f"{code}.json"

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


def _parse_agent_status(output: str, code: str) -> dict:
    """
    从 sub-agent stdout 中提取最后一行有效 JSON。

    Sub-agent 被要求最后一行输出状态 JSON。
    如果解析失败，返回 failed 状态并附带原始末尾输出。
    """
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if "code" in data and "status" in data:
                    return data
            except json.JSONDecodeError:
                continue

    # 没找到合法 JSON
    tail = "\n".join(lines[-5:]) if lines else "(empty output)"
    return {
        "code": code,
        "status": "failed",
        "error": f"sub-agent 未返回合法状态 JSON，末尾输出：{tail[:200]}",
    }


def run_analysis_agent(
    code: str,
    stock_name: str,
    raw_dir: Path,
    analysis_dir: Path,
) -> dict:
    """
    通过 claude CLI 启动单只股票的分析 sub-agent，等待完成并返回状态。

    Args:
        code:         6 位股票代码
        stock_name:   股票中文名
        raw_dir:      raw 数据目录
        analysis_dir: analysis 输出目录
    Returns:
        状态字典：{"code", "status", "output_path", "data_quality"/"error", ...}
    """
    prompt = _build_agent_prompt(code, stock_name, raw_dir, analysis_dir)

    logger.info(f"[{code}] 启动 analysis sub-agent（{stock_name}）")
    logger.debug(f"[{code}] Prompt 长度：{len(prompt)} 字符")

    t_start = time.time()
    try:
        proc = subprocess.run(
            [
                "claude",
                "--print",
                "--dangerously-skip-permissions",   # sub-agent 需要写文件
                "--output-format", "text",
                "--model", "haiku",                 # 纯计算任务，用 haiku 控制成本
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT_SECONDS,
            cwd=str(WORKING_DIR),
        )
        elapsed = time.time() - t_start
        stdout  = proc.stdout or ""
        stderr  = proc.stderr or ""

        logger.info(f"[{code}] sub-agent 完成，耗时 {elapsed:.1f}s，returncode={proc.returncode}")
        if stderr:
            logger.debug(f"[{code}] sub-agent stderr: {stderr[:300]}")

        # 记录完整 stdout 用于 sop.md 分析
        log_path = Path("logs") / f"agent_{code}_{datetime.today().strftime('%Y%m%d')}.log"
        log_path.parent.mkdir(exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"=== PROMPT ({len(prompt)} chars) ===\n{prompt}\n\n")
            f.write(f"=== STDOUT ===\n{stdout}\n\n")
            f.write(f"=== STDERR ===\n{stderr}\n")
            f.write(f"=== returncode={proc.returncode}, elapsed={elapsed:.1f}s ===\n")

        status = _parse_agent_status(stdout, code)
        status["elapsed_seconds"] = round(elapsed, 1)

    except subprocess.TimeoutExpired:
        elapsed = time.time() - t_start
        logger.error(f"[{code}] sub-agent 超时（>{AGENT_TIMEOUT_SECONDS}s）")
        status = {"code": code, "status": "failed",
                  "error": f"timeout after {elapsed:.0f}s"}

    except FileNotFoundError:
        logger.error("[ERROR] `claude` CLI 未找到，请确认 Claude Code 已安装且在 PATH 中")
        status = {"code": code, "status": "failed", "error": "claude CLI not found"}

    return status


def _validate_analysis_output(code: str, analysis_dir: Path) -> tuple[bool, str]:
    """
    验证 sub-agent 产出的 JSON 文件存在且 schema 合规。

    Returns:
        (ok: bool, reason: str)
    """
    path = analysis_dir / f"{code}.json"
    if not path.exists():
        return False, f"文件不存在: {path}"

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, f"JSON 解析失败: {e}"

    required = {"stock_code", "technical", "fundamental", "data_quality"}
    missing = required - set(data.keys())
    if missing:
        return False, f"缺少必要字段: {missing}"

    return True, "ok"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_daily_pipeline(
    date: Optional[str] = None,
    watchlist_path: str = "watchlist.json",
    output_base: str = "output",
) -> dict:
    """
    执行每日完整数据管道。

    步骤 1：data_fetcher.run_data_fetch()     — 直接函数调用
    步骤 2：analysis sub-agent × N             — claude -p 子进程
    步骤 3：reporter.generate_report()         — 直接函数调用

    Args:
        date:           指定日期（YYYY-MM-DD），None 时取今日
        watchlist_path: watchlist.json 路径
        output_base:    输出根目录
    Returns:
        管道运行摘要字典
    """
    if date is None:
        date = datetime.today().strftime("%Y-%m-%d")

    raw_dir      = Path(output_base) / date / "raw"
    analysis_dir = Path(output_base) / date / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(exist_ok=True)

    with open(watchlist_path, encoding="utf-8") as f:
        stocks = json.load(f)["stocks"]

    pipeline_start = time.time()
    summary = {
        "date": date,
        "pipeline_start": datetime.now().isoformat(),
        "step1_fetch": {},
        "step2_analysis": [],
        "step3_report": {},
    }

    # ------------------------------------------------------------------ #
    # 步骤 1：数据抓取（直接调用，不走 sub-agent）
    # ------------------------------------------------------------------ #
    logger.info("=" * 60)
    logger.info("步骤 1：数据抓取")
    logger.info("=" * 60)
    sys.path.insert(0, str(WORKING_DIR / "src"))
    from data_fetcher import run_data_fetch  # noqa: E402
    fetch_results = run_data_fetch(watchlist_path=watchlist_path, output_base=output_base)
    summary["step1_fetch"] = {
        "total": len(fetch_results),
        "complete": sum(1 for r in fetch_results if r["data_quality"] == "complete"),
    }
    logger.info(f"步骤 1 完成：{summary['step1_fetch']}")

    # ------------------------------------------------------------------ #
    # 步骤 2：分析（每只股票独立 sub-agent，支持串行/并行）
    # ------------------------------------------------------------------ #
    parallel = not ("--serial" in sys.argv)
    mode_label = "并行" if parallel else "串行"
    max_workers = len(stocks) if parallel else 1
    logger.info("=" * 60)
    logger.info(f"步骤 2：Analysis sub-agents（{mode_label}，max_workers={max_workers}）")
    logger.info("=" * 60)

    agent_results: list[dict] = []

    def _run_and_validate(stock: dict) -> dict:
        """在线程中调用 sub-agent，返回带 schema 验证的状态（线程安全：每只写独立文件）。"""
        code = stock["code"]
        name = stock["name"]
        status = run_analysis_agent(code, name, raw_dir, analysis_dir)
        # schema 验证在各线程内执行（只读 analysis/{code}.json，无写冲突）
        ok, reason = _validate_analysis_output(code, analysis_dir)
        status["schema_valid"] = ok
        if not ok:
            logger.warning(f"[{code}] schema 验证失败: {reason}")
            status["schema_error"] = reason
        else:
            logger.info(f"[{code}] schema 验证通过")
        return status

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_and_validate, s): s for s in stocks}
        for fut in as_completed(futures):
            try:
                agent_results.append(fut.result())
            except Exception as e:
                stock = futures[fut]
                logger.error(f"[{stock['code']}] future 异常: {e}")
                agent_results.append({"code": stock["code"], "status": "failed",
                                       "schema_valid": False, "error": str(e)})

    summary["step2_analysis"] = agent_results
    summary["step2_mode"] = mode_label
    success_count = sum(1 for r in agent_results if r.get("status") == "success")
    logger.info(f"步骤 2 完成（{mode_label}）：{success_count}/{len(stocks)} 只成功")

    # ------------------------------------------------------------------ #
    # 步骤 3：报告生成（直接调用）
    # ------------------------------------------------------------------ #
    logger.info("=" * 60)
    logger.info("步骤 3：报告生成")
    logger.info("=" * 60)
    from reporter import generate_report  # noqa: E402
    report_path = generate_report(watchlist_path=watchlist_path, output_base=output_base)
    summary["step3_report"] = {
        "path": str(report_path) if report_path else None,
        "success": report_path is not None,
    }

    # ------------------------------------------------------------------ #
    # 最终摘要
    # ------------------------------------------------------------------ #
    total_elapsed = time.time() - pipeline_start
    summary["total_elapsed_seconds"] = round(total_elapsed, 1)
    logger.info("=" * 60)
    logger.info(f"管道完成，总耗时 {total_elapsed:.1f}s")
    logger.info(f"报告路径: {report_path}")
    logger.info("=" * 60)

    return summary


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run_daily_pipeline()
    print(f"\n=== 管道摘要（{result.get('step2_mode','?')}）===")
    print(f"总耗时: {result['total_elapsed_seconds']}s")
    print(f"分析成功: {sum(1 for r in result['step2_analysis'] if r.get('status')=='success')}/5")
    print(f"报告: {result['step3_report']['path']}")
