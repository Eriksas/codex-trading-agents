"""
main_v2.py - 正确架构版主调度脚本（阶段 2.3）

架构原则（从阶段 2.2 实验总结）：
  纯计算任务（技术指标、数据聚合）→ 直接函数调用，无 sub-agent
  synthesis 生成                  → 默认模板直调；可选 Claude 无工具文字调用，由 Python 验证回写

流程：
  步骤 1：data_fetcher.run_data_fetch()    — 直接函数调用（数据抓取）
  步骤 2：analyzer.run_analysis()          — 直接函数调用（技术面 + 基本面计算）
  步骤 3：synthesis 生成                   — 默认模板直调；--llm 时使用 claude CLI 子进程
  步骤 4：reporter.generate_report()       — 直接函数调用（报告生成）
  步骤 5：notifier.send_report()            — 可选，每日推送

使用方式：
  python3 main_v2.py               # 默认模板 synthesis（不依赖 Claude CLI）
  python3 main_v2.py --llm          # 使用 Claude CLI 并行生成 synthesis
  python3 main_v2.py --llm --serial # 使用 Claude CLI 串行生成 synthesis（调试用）
  python3 main_v2.py --push         # 生成报告后推送到已配置通道
"""

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/pipeline_v2.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main_v2")

WORKING_DIR = Path(__file__).parent.resolve()
SYNTHESIS_PROMPT_PATH = WORKING_DIR / "prompts" / "synthesis_agent.md"
SYNTHESIS_TIMEOUT_SECONDS = 120


# ---------------------------------------------------------------------------
# Synthesis sub-agent
# ---------------------------------------------------------------------------

def _build_synthesis_prompt(code: str, stock_name: str, analysis_path: Path) -> str:
    template = SYNTHESIS_PROMPT_PATH.read_text(encoding="utf-8")
    task_params = (
        f"- CODE: `{code}`\n"
        f"- STOCK_NAME: `{stock_name}`\n"
    )
    return (
        template
        .replace("{TASK_PARAMS}", task_params)
        .replace("{CODE}", code)
        .replace("{STOCK_NAME}", stock_name)
        .replace("{ANALYSIS_PATH}", str(analysis_path))
        .replace("{WORKING_DIR}", str(WORKING_DIR))
        .replace("{ANALYSIS_JSON}", analysis_path.read_text(encoding="utf-8"))
    )


def _parse_synthesis_status(output: str, code: str) -> dict:
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if "code" in data and "status" in data:
                    return data
            except json.JSONDecodeError:
                continue
    tail = "\n".join(lines[-5:]) if lines else "(empty output)"
    return {
        "code": code,
        "status": "failed",
        "error": f"synthesis sub-agent 未返回合法状态 JSON，末尾输出：{tail[:200]}",
    }


def run_synthesis_agent(code: str, stock_name: str, analysis_path: Path) -> dict:
    """请求无工具文字结果，由 Python 验证并仅更新 synthesis 字段。"""
    from src.agent_client import call_agent
    from src.analysis_contract import parse_json
    from src.synthesizer import FORBIDDEN_TERMS, build_synthesis
    from uuid import uuid4

    started = time.monotonic()
    called = False
    skip_template_fallback = False
    try:
        original = analysis_path.read_bytes()
        data = parse_json(original.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("analysis 必须为 JSON 对象")
        if data.get("data_quality") == "failed":
            synthesis = build_synthesis(data)
            status = "success_template_fallback"
        else:
            prompt = _build_synthesis_prompt(code, stock_name, analysis_path)
            call = call_agent(
                prompt, backend="claude", model="haiku", timeout=SYNTHESIS_TIMEOUT_SECONDS,
                output_dir=WORKING_DIR / "logs" / "agent_calls" / f"synthesis-{uuid4().hex}",
            )
            called = call["model_called"]
            if call["status"] != "completed":
                raise ValueError(f"受控综合观察调用失败：{call['status']}")
            value = parse_json(call["text"])
            if not isinstance(value, dict) or set(value) != {"code", "synthesis"} or value["code"] != code:
                raise ValueError("综合观察结果字段或 code 不匹配")
            synthesis = value["synthesis"]
            if not isinstance(synthesis, str) or not 80 <= len(synthesis) <= 200:
                raise ValueError("综合观察必须为 80 至 200 字")
            if any(term in synthesis for term in FORBIDDEN_TERMS):
                raise ValueError("综合观察含不允许的操作性用语")
            status = "success"
        if analysis_path.read_bytes() != original:
            skip_template_fallback = True
            raise ValueError("analysis 在调用期间发生变化，拒绝覆盖")
        data["synthesis"] = synthesis
        analysis_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        return {"code": code, "status": status, "synthesis_chars": len(synthesis),
                "model_called": called, "elapsed_seconds": round(time.monotonic() - started, 1)}
    except (OSError, ValueError, TypeError) as exc:
        logger.error("[%s] synthesis 失败：%s", code, exc)
        return {"code": code, "status": "failed", "error": str(exc), "model_called": called,
                "skip_template_fallback": skip_template_fallback,
                "elapsed_seconds": round(time.monotonic() - started, 1)}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_daily_pipeline(
    date: Optional[str] = None,
    watchlist_path: str = "watchlist.json",
    output_base: str = "output",
    strategy_path: str = "strategy.json",
    use_llm_synthesis: bool = False,
    serial: bool = False,
    push: bool = False,
    push_mode: str = "digest",
    push_dry_run: bool = False,
) -> dict:
    """
    执行每日完整数据管道（正确架构版）。

    步骤 1：data_fetcher.run_data_fetch()    — 直接函数调用
    步骤 2：analyzer.run_analysis()          — 直接函数调用
    步骤 3：synthesis 生成                   — 默认模板；可选 claude -p 子进程（并行）
    步骤 4：reporter.generate_report()       — 直接函数调用
    步骤 5：notifier.send_report()           — 可选推送

    Args:
        date:           指定日期（YYYY-MM-DD），None 时取今日
        watchlist_path: watchlist.json 路径
        output_base:    输出根目录
        strategy_path:  策略配置路径
        use_llm_synthesis: True 时调用 claude CLI 生成 synthesis，否则使用确定性模板
        serial:          LLM synthesis 串行模式
        push:            True 时生成报告后推送
        push_mode:       digest 推摘要，full 推完整报告
        push_dry_run:    True 时只检查推送配置，不发送
    Returns:
        管道运行摘要字典
    """
    if date is None:
        date = datetime.today().strftime("%Y-%m-%d")

    raw_dir = Path(output_base) / date / "raw"
    analysis_dir = Path(output_base) / date / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(exist_ok=True)

    with open(watchlist_path, encoding="utf-8") as f:
        stocks = json.load(f)["stocks"]

    parallel = not serial
    mode_label = "并行" if parallel else "串行"
    max_workers = len(stocks) if parallel else 1

    pipeline_start = time.time()
    summary = {
        "date": date,
        "pipeline_start": datetime.now().isoformat(),
        "architecture": "v2 (compute=direct, synthesis=llm-sub-agent|template)",
        "step1_fetch": {},
        "step2_analysis": {},
        "step2_selection": {},
        "step3_synthesis": [],
        "step3_mode": mode_label,
        "step4_report": {},
        "step5_push": [],
    }

    # ------------------------------------------------------------------ #
    # 步骤 1：数据抓取（直接函数调用）
    # ------------------------------------------------------------------ #
    logger.info("=" * 60)
    logger.info("步骤 1：数据抓取（直接调用）")
    logger.info("=" * 60)
    sys.path.insert(0, str(WORKING_DIR / "src"))
    from data_fetcher import run_data_fetch
    fetch_results = run_data_fetch(watchlist_path=watchlist_path, output_base=output_base, date=date)
    summary["step1_fetch"] = {
        "total": len(fetch_results),
        "complete": sum(1 for r in fetch_results if r["data_quality"] == "complete"),
        "partial": sum(1 for r in fetch_results if r["data_quality"] == "partial"),
        "failed": sum(1 for r in fetch_results if r["data_quality"] == "failed"),
    }
    logger.info(f"步骤 1 完成：{summary['step1_fetch']}")

    # ------------------------------------------------------------------ #
    # 步骤 2：技术面 + 基本面计算（直接函数调用，无 sub-agent）
    # ------------------------------------------------------------------ #
    logger.info("=" * 60)
    logger.info("步骤 2：技术面 + 基本面计算（直接调用，无 sub-agent）")
    logger.info("=" * 60)
    t2_start = time.time()
    from analyzer import run_analysis
    analysis_results = run_analysis(
        watchlist_path=watchlist_path,
        output_base=output_base,
        date=date,
    )
    t2_elapsed = time.time() - t2_start
    summary["step2_analysis"] = {
        "total": len(analysis_results),
        "complete": sum(1 for r in analysis_results if r.get("data_quality") == "complete"),
        "partial": sum(1 for r in analysis_results if r.get("data_quality") == "partial"),
        "failed": sum(1 for r in analysis_results if r.get("data_quality") == "failed"),
        "elapsed_seconds": round(t2_elapsed, 1),
    }
    logger.info(f"步骤 2 完成：{summary['step2_analysis']} （{t2_elapsed:.1f}s）")

    # ------------------------------------------------------------------ #
    # 步骤 2.5：策略筛选（观察池排序，直接函数调用）
    # ------------------------------------------------------------------ #
    logger.info("=" * 60)
    logger.info("步骤 2.5：策略筛选（观察池排序）")
    logger.info("=" * 60)
    from selector import run_selection
    selection_result = run_selection(
        strategy_path=strategy_path,
        output_base=output_base,
        date=date,
    )
    summary["step2_selection"] = {
        "total": selection_result["total"],
        "passed": selection_result["passed"],
        "path": selection_result["path"],
        "markdown_path": selection_result["markdown_path"],
    }
    logger.info(f"步骤 2.5 完成：{summary['step2_selection']}")

    # ------------------------------------------------------------------ #
    # 步骤 3：synthesis 生成（LLM sub-agent，并行/串行）
    # ------------------------------------------------------------------ #
    logger.info("=" * 60)
    if use_llm_synthesis:
        logger.info(f"步骤 3：synthesis sub-agents（{mode_label}，max_workers={max_workers}）")
    else:
        logger.info("步骤 3：synthesis 模板生成（直接调用）")
    logger.info("=" * 60)
    t3_start = time.time()

    synthesis_results: list[dict] = []

    def _run_synthesis(stock: dict) -> dict:
        code = stock["code"]
        name = stock["name"]
        analysis_path = analysis_dir / f"{code}.json"
        if not analysis_path.exists():
            logger.warning(f"[{code}] analysis JSON 不存在，跳过 synthesis")
            return {"code": code, "status": "skipped", "error": "analysis JSON missing"}
        return run_synthesis_agent(code, name, analysis_path)

    if use_llm_synthesis:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_run_synthesis, s): s for s in stocks}
            for fut in as_completed(futures):
                try:
                    synthesis_results.append(fut.result())
                except Exception as e:
                    stock = futures[fut]
                    logger.error(f"[{stock['code']}] synthesis future 异常: {e}")
                    synthesis_results.append({
                        "code": stock["code"], "status": "failed",
                        "error": str(e),
                    })
        fallback_targets = [r["code"] for r in synthesis_results if r.get("status") != "success" and not r.get("skip_template_fallback")]
        if fallback_targets:
            logger.warning(f"synthesis LLM 失败 {len(fallback_targets)} 只，使用模板兜底: {fallback_targets}")
            from synthesizer import synthesize_file
            for code in fallback_targets:
                analysis_path = analysis_dir / f"{code}.json"
                if not analysis_path.exists():
                    continue
                try:
                    fallback_status = synthesize_file(analysis_path)
                    fallback_status["status"] = "success_template_fallback"
                    synthesis_results.append(fallback_status)
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    logger.error(f"[{code}] synthesis 模板兜底失败: {exc}")
    else:
        from synthesizer import run_synthesis
        synthesis_results = run_synthesis(
            watchlist_path=watchlist_path,
            output_base=output_base,
            date=date,
        )

    t3_elapsed = time.time() - t3_start
    success_count = sum(1 for r in synthesis_results if str(r.get("status", "")).startswith("success"))
    summary["step3_synthesis"] = synthesis_results
    summary["step3_elapsed_seconds"] = round(t3_elapsed, 1)
    synthesis_label = f"LLM {mode_label}" if use_llm_synthesis else "模板"
    logger.info(f"步骤 3 完成（{synthesis_label}）：{success_count}/{len(stocks)} 只成功，{t3_elapsed:.1f}s")

    # ------------------------------------------------------------------ #
    # 步骤 4：报告生成（直接函数调用）
    # ------------------------------------------------------------------ #
    logger.info("=" * 60)
    logger.info("步骤 4：报告生成（直接调用）")
    logger.info("=" * 60)
    from reporter import generate_report
    report_path = generate_report(watchlist_path=watchlist_path, output_base=output_base, date=date)
    summary["step4_report"] = {
        "path": str(report_path) if report_path else None,
        "success": report_path is not None,
    }

    # ------------------------------------------------------------------ #
    # 步骤 5：报告推送（可选）
    # ------------------------------------------------------------------ #
    if push and report_path:
        logger.info("=" * 60)
        logger.info(f"步骤 5：报告推送（mode={push_mode}, dry_run={push_dry_run}）")
        logger.info("=" * 60)
        from notifier import send_report
        push_results = send_report(report_path, mode=push_mode, dry_run=push_dry_run)
        summary["step5_push"] = [item.__dict__ for item in push_results]
        push_summary_path = Path(output_base) / date / "push_summary.json"
        with open(push_summary_path, "w", encoding="utf-8") as f:
            json.dump({
                "date": date,
                "push_time": datetime.now().isoformat(),
                "report_path": str(report_path),
                "mode": push_mode,
                "dry_run": push_dry_run,
                "results": summary["step5_push"],
            }, f, ensure_ascii=False, indent=2)
        logger.info(f"推送结果写入: {push_summary_path}")

    # ------------------------------------------------------------------ #
    # 最终摘要
    # ------------------------------------------------------------------ #
    total_elapsed = time.time() - pipeline_start
    summary["total_elapsed_seconds"] = round(total_elapsed, 1)
    logger.info("=" * 60)
    logger.info(f"管道完成（v2），总耗时 {total_elapsed:.1f}s")
    logger.info(f"  步骤 2（计算）: {t2_elapsed:.1f}s")
    logger.info(f"  步骤 3（synthesis）: {t3_elapsed:.1f}s（{synthesis_label}）")
    logger.info(f"  报告路径: {report_path}")
    logger.info("=" * 60)

    return summary


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行每日 A 股研究性数据分析工作流")
    parser.add_argument("--date", help="输出日期（YYYY-MM-DD），默认今日")
    parser.add_argument("--watchlist", default="watchlist.json", help="watchlist.json 路径")
    parser.add_argument("--output", default="output", help="输出根目录")
    parser.add_argument("--strategy", default="strategy.json", help="策略配置 JSON 路径")
    parser.add_argument("--llm", action="store_true", help="使用 claude CLI sub-agent 生成 synthesis")
    parser.add_argument("--serial", action="store_true", help="LLM synthesis 串行运行")
    parser.add_argument("--push", action="store_true", help="生成报告后推送到已配置通道")
    parser.add_argument("--push-mode", choices=["digest", "full"], default="digest", help="推送摘要或完整报告")
    parser.add_argument("--push-dry-run", action="store_true", help="只检查推送配置，不发送")
    parser.add_argument("--push-fail-on-error", action="store_true", help="任一推送通道失败时以非零状态退出")
    args = parser.parse_args()

    result = run_daily_pipeline(
        date=args.date,
        watchlist_path=args.watchlist,
        output_base=args.output,
        strategy_path=args.strategy,
        use_llm_synthesis=args.llm,
        serial=args.serial,
        push=args.push,
        push_mode=args.push_mode,
        push_dry_run=args.push_dry_run,
    )
    mode = "LLM " + result.get("step3_mode", "?") if args.llm else "模板"
    print(f"\n=== 管道摘要（v2 架构，synthesis={mode}）===")
    print(f"总耗时: {result['total_elapsed_seconds']}s")
    print(f"  步骤 2（计算）: {result['step2_analysis']['elapsed_seconds']}s（直接调用）")
    print(f"  策略筛选: {result['step2_selection']['passed']}/{result['step2_selection']['total']} 只通过硬过滤")
    print(f"  步骤 3（synthesis）: {result['step3_elapsed_seconds']}s（{mode}）")
    s3_ok = sum(1 for r in result['step3_synthesis'] if str(r.get('status', '')).startswith('success'))
    print(f"  synthesis 成功: {s3_ok}/5")
    print(f"报告: {result['step4_report']['path']}")
    if args.push:
        ok = sum(1 for r in result["step5_push"] if r.get("status") in {"success", "dry_run"})
        print(f"推送: {ok}/{len(result['step5_push'])}")
        failed = [r for r in result["step5_push"] if r.get("status") == "failed"]
        if args.push_fail_on_error and failed:
            raise SystemExit(1)
