#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ENV="${CLAUDE_TRADING_AGENTS_ENV:-$HOME/.secrets/claude_trading_agents.env}"

if [[ -f "$LOCAL_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$LOCAL_ENV"
  set +a
fi

export FUYAO_API_KEY_FILE="${FUYAO_API_KEY_FILE:-$HOME/.secrets/fuyao_api_key}"

cd "$ROOT_DIR"
"${PYTHON:-python3}" src/market_scanner.py --push "$@"
