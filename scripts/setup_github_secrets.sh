#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ENV="${CODEX_TRADING_AGENTS_ENV:-${CLAUDE_TRADING_AGENTS_ENV:-$HOME/.secrets/codex_trading_agents.env}}"
if [[ ! -f "$LOCAL_ENV" && -f "$HOME/.secrets/claude_trading_agents.env" ]]; then
  LOCAL_ENV="$HOME/.secrets/claude_trading_agents.env"
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI is required." >&2
  exit 1
fi

cd "$ROOT_DIR"
if ! gh repo view >/dev/null 2>&1; then
  echo "No GitHub remote found for this repository. Add origin first, then rerun." >&2
  exit 1
fi

if [[ -f "$LOCAL_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$LOCAL_ENV"
  set +a
fi

if [[ -z "${FUYAO_API_KEY:-}" && -f "${FUYAO_API_KEY_FILE:-$HOME/.secrets/fuyao_api_key}" ]]; then
  FUYAO_API_KEY="$(<"${FUYAO_API_KEY_FILE:-$HOME/.secrets/fuyao_api_key}")"
fi

set_secret() {
  local name="$1"
  local value="${!name:-}"
  if [[ -n "$value" ]]; then
    printf '%s' "$value" | gh secret set "$name" --body-file -
    echo "set $name"
  else
    echo "skip $name (empty)"
  fi
}

set_secret FUYAO_API_KEY
set_secret FEISHU_APP_ID
set_secret FEISHU_APP_SECRET
set_secret FEISHU_RECEIVE_ID
set_secret FEISHU_WEBHOOK_URL
set_secret FEISHU_WEBHOOK_SECRET
