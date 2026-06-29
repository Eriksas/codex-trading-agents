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
    printf '%s' "$value" | gh secret set "$name"
    echo "set $name"
  else
    echo "skip $name (empty)"
  fi
}

set_variable() {
  local name="$1"
  local value="${!name:-}"
  if [[ -n "$value" ]]; then
    printf '%s' "$value" | gh variable set "$name"
    echo "set variable $name"
  else
    echo "skip variable $name (empty)"
  fi
}

: "${FEISHU_RECEIVE_ID_TYPE:=chat_id}"
: "${FEISHU_APP_MESSAGE_FORMAT:=post}"
: "${FEISHU_WEBHOOK_MESSAGE_FORMAT:=post}"
: "${SCAN_ENRICH_LIMIT:=120}"

set_secret FUYAO_API_KEY
set_secret FEISHU_APP_ID
set_secret FEISHU_APP_SECRET
set_secret FEISHU_RECEIVE_ID
set_secret FEISHU_WEBHOOK_URL
set_secret FEISHU_WEBHOOK_SECRET

set_variable FEISHU_RECEIVE_ID_TYPE
set_variable FEISHU_APP_MESSAGE_FORMAT
set_variable FEISHU_WEBHOOK_MESSAGE_FORMAT
set_variable SCAN_ENRICH_LIMIT
