#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_BIN="${HERMES_BIN:-$(command -v hermes || true)}"
DATE=""
DRY_RUN=0
OUTPUT_BASE="output"
MODE="daily"
PYTHON_BIN="${PYTHON:-python3}"

usage() {
  cat <<'USAGE'
Usage: scripts/run_strategy_steward.sh [--date YYYY-MM-DD] [--mode daily|deep|critic|experiment] [--dry-run] [--output output]

Generate a read-only Strategy Steward prompt from strategy health/review files,
then ask Hermes Agent to produce one of:

  daily       Regular steward diagnosis report.
  deep        Longer drift diagnosis with more historical context.
  critic      Red-team review of the previous Hermes report.
  experiment  Draft JSON experiment proposals under strategy_experiments/.

Environment:
  HERMES_BIN       Hermes executable path. Default: first hermes in PATH.
  HERMES_MODEL     Optional Hermes model override.
  HERMES_PROVIDER  Optional Hermes provider override.
  PYTHON           Python executable used for JSON validation.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --date)
      DATE="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --output)
      OUTPUT_BASE="${2:-output}"
      shift 2
      ;;
    --mode)
      MODE="${2:-daily}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$MODE" in
  daily|deep|critic|experiment) ;;
  *)
    echo "Unsupported mode: $MODE" >&2
    usage >&2
    exit 2
    ;;
esac

if [[ -z "$DATE" ]]; then
  while IFS= read -r path; do
    DATE="$(basename "$(dirname "$(dirname "$path")")")"
  done < <(find "$ROOT_DIR/$OUTPUT_BASE" -maxdepth 3 -path '*/scan/strategy_health_summary.json' -print 2>/dev/null | sort)
fi

if [[ -z "$DATE" ]]; then
  echo "No strategy_health_summary.json found under $OUTPUT_BASE/*/scan/." >&2
  exit 1
fi

SCAN_DIR="$ROOT_DIR/$OUTPUT_BASE/$DATE/scan"
EXPERIMENT_DIR="$ROOT_DIR/strategy_experiments/$DATE"

case "$MODE" in
  daily)
    PROMPT_PATH="$SCAN_DIR/strategy_steward_prompt.md"
    OUTPUT_PATH="$SCAN_DIR/strategy_steward_report.md"
    ;;
  deep)
    PROMPT_PATH="$SCAN_DIR/strategy_steward_deep_prompt.md"
    OUTPUT_PATH="$SCAN_DIR/strategy_steward_deep_report.md"
    ;;
  critic)
    PROMPT_PATH="$SCAN_DIR/strategy_steward_critic_prompt.md"
    OUTPUT_PATH="$SCAN_DIR/strategy_steward_critic.md"
    ;;
  experiment)
    mkdir -p "$EXPERIMENT_DIR"
    PROMPT_PATH="$SCAN_DIR/strategy_steward_experiment_prompt.md"
    OUTPUT_PATH="$EXPERIMENT_DIR/hermes_experiments.json"
    ;;
esac

required_files=(
  "$ROOT_DIR/prompts/strategy_steward_agent.md"
  "$SCAN_DIR/strategy_health_summary.json"
  "$SCAN_DIR/strategy_health_report.md"
  "$SCAN_DIR/review_summary.json"
  "$SCAN_DIR/review_details.csv"
  "$SCAN_DIR/market_profile.json"
  "$SCAN_DIR/backtest_summary.json"
  "$ROOT_DIR/data/ledger/trade_archive.csv"
)

for path in "${required_files[@]}"; do
  if [[ ! -f "$path" ]]; then
    echo "Required file missing: $path" >&2
    exit 1
  fi
done

append_file() {
  local title="$1"
  local path="$2"
  local lang="$3"
  echo
  echo "## $title"
  echo
  echo '```'"$lang"
  cat "$path"
  echo
  echo '```'
}

append_optional_file() {
  local title="$1"
  local path="$2"
  local lang="$3"
  if [[ -f "$path" ]]; then
    append_file "$title" "$path" "$lang"
  fi
}

append_file_limited_lines() {
  local title="$1"
  local path="$2"
  local lang="$3"
  local max_lines="$4"
  if [[ ! -f "$path" ]]; then
    return
  fi
  local total_lines
  total_lines="$(wc -l < "$path" | tr -d ' ')"
  echo
  echo "## $title"
  echo
  if [[ "$total_lines" -gt "$max_lines" ]]; then
    echo "_Transport note: $path has $total_lines lines; included first $max_lines lines to stay below CLI argument limits._"
    echo
  fi
  echo '```'"$lang"
  head -n "$max_lines" "$path"
  echo
  echo '```'
}

append_recent_files() {
  local title="$1"
  local path_pattern="$2"
  local lang="$3"
  local limit="$4"
  local wrote=0
  while IFS= read -r path; do
    if [[ "$wrote" -eq 0 ]]; then
      echo
      echo "## $title"
    fi
    wrote=$((wrote + 1))
    echo
    echo "### $path"
    echo
    echo '```'"$lang"
    cat "$path"
    echo
    echo '```'
  done < <(find "$ROOT_DIR/$OUTPUT_BASE" -maxdepth 3 -path "$path_pattern" -type f -print 2>/dev/null | sort | tail -n "$limit")
}

find_latest_existing_report() {
  local candidate=""
  for path in \
    "$SCAN_DIR/strategy_steward_report.md" \
    "$SCAN_DIR/strategy_steward_deep_report.md" \
    "$SCAN_DIR/scan_report.md"; do
    if [[ -f "$path" ]]; then
      echo "$path"
      return 0
    fi
  done
  while IFS= read -r path; do
    candidate="$path"
  done < <(find "$ROOT_DIR/$OUTPUT_BASE" -maxdepth 3 -path '*/scan/strategy_steward_report.md' -type f -print 2>/dev/null | sort)
  if [[ -n "$candidate" ]]; then
    echo "$candidate"
  fi
}

SOURCE_REPORT_PATH="$(find_latest_existing_report || true)"

{
  cat "$ROOT_DIR/prompts/strategy_steward_agent.md"
  cat <<EOF

## Runtime Guardrails

- 当前日期：$DATE
- 当前模式：$MODE
- 你是只读诊断 agent，不得调用工具修改文件。
- 不要改写任何输入事实，不要给出买卖建议。
- 如果样本不足，必须把“继续累积样本”放在结论前面。
- 每个判断必须绑定数据来源文件和样本量；没有样本量时必须说明“样本量缺失，不能下策略结论”。
EOF

  case "$MODE" in
    daily)
      cat <<'EOF'
- 输出 Markdown 正文，不要输出代码块包裹全文。
- 只做常规策略管家诊断：事实核对、策略漂移、候选实验、暂不调整原因。
EOF
      ;;
    deep)
      cat <<'EOF'
- 输出 Markdown 正文，不要输出代码块包裹全文。
- token 充足，请展开关键证据，不要为了省 token 省略重要样本、异常或反例。
- 重点比较 5/20/60 日窗口、历史归档、市场环境、策略标签表现和回测摘要，判断是否存在策略漂移。
EOF
      ;;
    critic)
      cat <<'EOF'
- 输出 Markdown 正文，不要输出代码块包裹全文。
- 你是反方审查员，请专门反驳前一份 Hermes 报告：找样本不足、过拟合、错误归因、单日噪音、未证实假设。
- 如果前一份报告提出实验，逐条判断哪些值得进入回测，哪些只是噪音。
EOF
      ;;
    experiment)
      cat <<'EOF'
- 只输出合法 JSON，不要 Markdown，不要代码块。
- 不得改主策略，不得改台账；只生成实验草案。
- 忽略上方 Markdown 输出格式模板；本模式只接受 JSON。
- JSON 字符串内部不要使用未转义的英文双引号；需要引用术语时使用中文引号「」或单引号，确保 json.loads 可以解析。
- 顶层结构必须是：
  {
    "date": "YYYY-MM-DD",
    "mode": "experiment",
    "source_report": "path",
    "sample_warning": "...",
    "experiments": [
      {
        "name": "...",
        "status": "draft",
        "trigger_evidence": "...",
        "evidence_source": "...",
        "sample_size": 0,
        "parameter_direction": "...",
        "expected_improvement": "...",
        "side_effects": "...",
        "validation_metrics": ["..."],
        "rollback_conditions": ["..."],
        "promotion_gate": "..."
      }
    ]
  }
- 样本不足时 experiments 可以为空，但必须写 sample_warning。
EOF
      ;;
  esac

  append_optional_file "hermes_market_learning_skill.md" "$ROOT_DIR/prompts/hermes_market_learning_skill.md" "markdown"
  append_optional_file "learning_memory.md" "$ROOT_DIR/data/strategy_learning/learning_memory.md" "markdown"
  append_optional_file "learning_memory.json" "$ROOT_DIR/data/strategy_learning/learning_memory.json" "json"

  append_file "strategy_health_summary.json" "$SCAN_DIR/strategy_health_summary.json" "json"
  append_file "strategy_health_report.md" "$SCAN_DIR/strategy_health_report.md" "markdown"
  append_file "review_summary.json" "$SCAN_DIR/review_summary.json" "json"
  append_file "review_details.csv" "$SCAN_DIR/review_details.csv" "csv"
  append_file "market_profile.json" "$SCAN_DIR/market_profile.json" "json"
  append_file "backtest_summary.json" "$SCAN_DIR/backtest_summary.json" "json"
  append_optional_file "portfolio_backtest_summary.json" "$SCAN_DIR/portfolio_backtest_summary.json" "json"
  append_optional_file "strategy_health_tags.csv" "$SCAN_DIR/strategy_health_tags.csv" "csv"
  append_file "trade_archive.csv" "$ROOT_DIR/data/ledger/trade_archive.csv" "csv"

  if [[ "$MODE" == "deep" ]]; then
    append_optional_file "backtest_trades.csv" "$SCAN_DIR/backtest_trades.csv" "csv"
    append_optional_file "portfolio_backtest_trades.csv" "$SCAN_DIR/portfolio_backtest_trades.csv" "csv"
    append_file_limited_lines "daily_scans.csv sample" "$SCAN_DIR/daily_scans.csv" "csv" 240
    append_optional_file "research_candidates.csv" "$SCAN_DIR/research_candidates.csv" "csv"
    append_recent_files "recent review_summary.json files" "*/scan/review_summary.json" "json" 20
    append_recent_files "recent strategy_health_summary.json files" "*/scan/strategy_health_summary.json" "json" 20
  fi

  if [[ "$MODE" == "critic" || "$MODE" == "experiment" ]]; then
    if [[ -n "$SOURCE_REPORT_PATH" && -f "$SOURCE_REPORT_PATH" ]]; then
      append_file "previous Hermes or scan report for review" "$SOURCE_REPORT_PATH" "markdown"
    fi
    append_optional_file "strategy_steward_critic.md" "$SCAN_DIR/strategy_steward_critic.md" "markdown"
  fi
} > "$PROMPT_PATH"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Strategy Steward mode: $MODE"
  echo "Strategy Steward prompt written: $PROMPT_PATH"
  echo "Expected output: $OUTPUT_PATH"
  echo "Dry-run enabled; Hermes was not called."
  exit 0
fi

if [[ -z "$HERMES_BIN" || ! -x "$HERMES_BIN" ]]; then
  echo "Hermes executable not found. Set HERMES_BIN or ensure hermes is in PATH." >&2
  exit 1
fi

hermes_args=(chat -q "$(cat "$PROMPT_PATH")" -Q --max-turns 1 --ignore-rules --source tool)
if [[ -n "${HERMES_MODEL:-}" ]]; then
  hermes_args+=(--model "$HERMES_MODEL")
fi
if [[ -n "${HERMES_PROVIDER:-}" ]]; then
  hermes_args+=(--provider "$HERMES_PROVIDER")
fi

RAW_REPORT_PATH="$OUTPUT_PATH.raw"
if [[ "$MODE" == "experiment" ]]; then
  RAW_REPORT_PATH="$SCAN_DIR/strategy_steward_experiment_raw.md"
fi
"$HERMES_BIN" "${hermes_args[@]}" > "$RAW_REPORT_PATH"

if [[ "$MODE" == "experiment" ]]; then
  "$PYTHON_BIN" - "$RAW_REPORT_PATH" "$OUTPUT_PATH" "$DATE" "${SOURCE_REPORT_PATH:-}" <<'PY'
import json
import re
import sys
from pathlib import Path

raw_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
date = sys.argv[3]
source_report = sys.argv[4]
raw = raw_path.read_text(encoding="utf-8")
raw = "\n".join(line for line in raw.splitlines() if "tirith security scanner enabled" not in line).strip()

candidates = [raw]
fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
if fenced:
    candidates.append(fenced.group(1).strip())
start = raw.find("{")
end = raw.rfind("}")
if start != -1 and end != -1 and end > start:
    candidates.append(raw[start : end + 1])

payload = None
for candidate in candidates:
    try:
        payload = json.loads(candidate)
        break
    except json.JSONDecodeError:
        continue

if not isinstance(payload, dict):
    warning_match = re.search(
        r'"sample_warning"\s*:\s*"(.*?)"\s*,\s*"experiments"',
        raw,
        flags=re.DOTALL,
    )
    extracted_warning = ""
    if warning_match:
        extracted_warning = warning_match.group(1).strip()
    payload = {
        "date": date,
        "mode": "experiment",
        "source_report": source_report,
        "parse_status": "raw_markdown_fallback",
        "sample_warning": extracted_warning
        or "Hermes output was not valid JSON. Review raw_output manually; do not promote experiments.",
        "experiments": [],
        "raw_output": raw,
    }
else:
    payload.setdefault("date", date)
    payload.setdefault("mode", "experiment")
    payload.setdefault("source_report", source_report)
    payload.setdefault("experiments", [])
    if not isinstance(payload.get("experiments"), list):
        payload["experiments"] = []
        payload["sample_warning"] = (
            str(payload.get("sample_warning") or "")
            + " experiments field was not a list; reset to empty for safety."
        ).strip()

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  echo "Strategy Steward experiment draft written: $OUTPUT_PATH"
  echo "Raw Hermes output kept: $RAW_REPORT_PATH"
else
  awk '!/tirith security scanner enabled/' "$RAW_REPORT_PATH" > "$OUTPUT_PATH.tmp"
  rm -f "$RAW_REPORT_PATH"
  mv "$OUTPUT_PATH.tmp" "$OUTPUT_PATH"
  echo "Strategy Steward $MODE report written: $OUTPUT_PATH"
fi
