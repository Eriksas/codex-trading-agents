#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON:-python3}"
OUTPUT_BASE="${OUTPUT_BASE:-output}"
PUSH_MODE="${PUSH_MODE:-digest}"
PUSH_DRY_RUN="${PUSH_DRY_RUN:-false}"
RUN_MARKET_SCAN="${RUN_MARKET_SCAN:-true}"
RUN_FORWARD_PAPER="${RUN_FORWARD_PAPER:-true}"
SCAN_LIMIT="${SCAN_LIMIT:-5}"
SCAN_ENRICH_LIMIT="${SCAN_ENRICH_LIMIT:-120}"
FORWARD_OUTPUT_DIR="${FORWARD_OUTPUT_DIR:-data/ledger/forward_paper_trading_v3}"
SUMMARY_PATH="${OUTPUT_BASE}/github_v3_daily_summary.json"

has_feishu_app=false
if [[ -n "${FEISHU_APP_ID:-}" || -n "${FEISHU_APP_SECRET:-}" || -n "${FEISHU_RECEIVE_ID:-}" ]]; then
  if [[ -n "${FEISHU_APP_ID:-}" && -n "${FEISHU_APP_SECRET:-}" && -n "${FEISHU_RECEIVE_ID:-}" ]]; then
    has_feishu_app=true
  else
    echo "FEISHU_APP_ID, FEISHU_APP_SECRET and FEISHU_RECEIVE_ID must be configured together." >&2
    exit 1
  fi
fi

has_feishu_webhook=false
if [[ -n "${FEISHU_WEBHOOK_URL:-}" ]]; then
  has_feishu_webhook=true
fi

if [[ "$PUSH_DRY_RUN" != "true" && "$has_feishu_app" != "true" && "$has_feishu_webhook" != "true" ]]; then
  echo "No Feishu push channel configured. Set Feishu secrets or run with PUSH_DRY_RUN=true." >&2
  exit 1
fi

active_strategy="$("$PYTHON_BIN" - <<'PY'
import json
from pathlib import Path

print(json.loads(Path("strategy.json").read_text(encoding="utf-8")).get("active_strategy", ""))
PY
)"
if [[ "$active_strategy" != "alpha040_v3_risk_controlled" ]]; then
  echo "Unexpected active_strategy: ${active_strategy}. Expected alpha040_v3_risk_controlled." >&2
  exit 1
fi

if [[ "$RUN_MARKET_SCAN" == "true" ]]; then
  market_scan_cmd=(
    "$PYTHON_BIN" src/market_scanner.py
    --output "$OUTPUT_BASE"
    --limit "$SCAN_LIMIT"
    --enrich-limit "$SCAN_ENRICH_LIMIT"
  )
  if [[ -n "${REPORT_DATE:-}" ]]; then
    market_scan_cmd+=(--date "$REPORT_DATE")
  fi
  "${market_scan_cmd[@]}"
fi

mkdir -p "$OUTPUT_BASE"
report_cmd=("$PYTHON_BIN" src/scheduled_v3_reporter.py --output-base "$OUTPUT_BASE")
if [[ -n "${REPORT_DATE:-}" ]]; then
  report_cmd+=(--date "$REPORT_DATE")
fi
"${report_cmd[@]}" > "$SUMMARY_PATH"

report_path="$("$PYTHON_BIN" - "$SUMMARY_PATH" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(summary["report_path"])
PY
)"

report_date="$("$PYTHON_BIN" - "$SUMMARY_PATH" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(summary["date"])
PY
)"

if [[ "$RUN_FORWARD_PAPER" == "true" ]]; then
  forward_cmd=("$PYTHON_BIN" src/forward_paper_trading_v3.py --output "$FORWARD_OUTPUT_DIR")
  if [[ -n "${REPORT_DATE:-}" ]]; then
    forward_cmd+=(--date "$REPORT_DATE")
  fi
  "${forward_cmd[@]}"
fi

push_args=(--report "$report_path" --mode "$PUSH_MODE")
if [[ "$PUSH_DRY_RUN" == "true" ]]; then
  push_args+=(--dry-run)
fi
PYTHONPATH=src "$PYTHON_BIN" -m notifier "${push_args[@]}"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "report_date=$report_date"
    echo "v3_report_path=$report_path"
    echo "summary_path=$SUMMARY_PATH"
  } >> "$GITHUB_OUTPUT"
fi

echo "V3 daily push completed for ${report_date}: ${report_path}"
