#!/bin/bash
set -euo pipefail

JERRY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$HOME/.claude/jerry/run.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

mkdir -p "$HOME/.claude/jerry"

git -C "$JERRY" pull --quiet 2>/dev/null || true

RAW=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null || true)
TOKEN=$(echo "$RAW" | jq -r '.claudeAiOauth.accessToken // .accessToken // .access_token // empty' 2>/dev/null || true)
[ -z "$TOKEN" ] && TOKEN=$(jq -r '.claudeAiOauth.accessToken // .accessToken // .access_token // empty' \
  "$HOME/.claude/credentials.json" 2>/dev/null || true)
[ -z "$TOKEN" ] && { log "ERROR: no credentials"; exit 1; }

USAGE=$(curl -sf --config - https://api.anthropic.com/api/oauth/usage <<EOF
header = "Authorization: Bearer $TOKEN"
header = "anthropic-beta: oauth-2025-04-20"
EOF
) || { log "ERROR: usage API failed"; exit 1; }

PCT=$(echo "$USAGE" | jq -r '.seven_day.utilization')
RESET_AT=$(echo "$USAGE" | jq -r '.seven_day.resets_at')
RESET_EPOCH=$(date -jf "%Y-%m-%dT%H:%M:%SZ" "${RESET_AT%%.*}Z" "+%s" 2>/dev/null \
  || date -d "$RESET_AT" +%s)
HOURS_LEFT=$(( (RESET_EPOCH - $(date +%s)) / 3600 ))

log "Week: ${PCT}%  |  Reset in ${HOURS_LEFT}h"

(( $(echo "$PCT >= 100" | bc -l) )) && { log "SKIP: usage at 100%"; exit 0; }
[ "$HOURS_LEFT" -ge 24 ] && { log "SKIP: ${HOURS_LEFT}h to reset"; exit 0; }

log "Gate PASSED — starting run loop"
exec python3 "$JERRY/bin/run_loop.py"
