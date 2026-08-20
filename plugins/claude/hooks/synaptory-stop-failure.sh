#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# Hook: StopFailure  (async — must not block the session)
# Purpose: Ship API-error events to the control-plane outbox so they
# surface in /reliability/errors and /reliability/summary analytics.
#
# StopFailure stdin (Claude Code v2.1.196+):
#   {"stop_reason": "rate_limit|overloaded|model_not_found|…",
#    "stop_message": "…", "session_id": "…", "cwd": "…",
#    "hook_event_name": "StopFailure"}
#
# We only record stop_reason values that indicate a platform / model
# error — not normal completions like "end_turn" or "stop_sequence".

set -u  # do NOT set -e — every failure path is deliberately tolerated.

_HOOK_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=lib/resolve-python.sh
source "${_HOOK_ROOT}/hooks/lib/resolve-python.sh" 2>/dev/null || exit 0
_py="${SYNAPTORY_PYTHON:-python3}"

# Only fire inside a synaptory project.
SUITE_DIR="${CLAUDE_PROJECT_DIR:-}/.synaptory"
if [ -z "${CLAUDE_PROJECT_DIR:-}" ] || [ ! -d "$SUITE_DIR" ]; then
  exit 0
fi

cli=$("${_HOOK_ROOT}/hooks/_resolve-cli.sh" 2>/dev/null || true)
if [[ -z "$cli" ]] || [[ ! -x "$cli" ]]; then
  exit 0
fi

INPUT="$(cat 2>/dev/null || true)"

# Extract stop_reason and filter to API-error matchers only.
PARSED=$(printf '%s' "$INPUT" | "$_py" -c "
import sys, json, re

MATCHERS = [
    'rate_limit', 'overloaded', 'model_not_found',
    'authentication_failed', 'server_error', 'max_output_tokens',
]
try:
    d = json.load(sys.stdin)
    reason = (d.get('stop_reason') or '').strip().lower()
    msg    = str(d.get('stop_message') or '').strip()[:120]
except Exception:
    sys.exit(0)

if not any(m in reason for m in MATCHERS):
    sys.exit(0)

# Output: reason TAB message
print(reason + '\t' + msg)
" 2>/dev/null || true)

if [ -z "$PARSED" ]; then
  exit 0
fi

STOP_REASON="${PARSED%%	*}"
STOP_MSG="${PARSED##*	}"
SUMMARY="${STOP_REASON}"
if [ -n "$STOP_MSG" ]; then
  SUMMARY="${SUMMARY} — ${STOP_MSG}"
fi

(
  "$cli" telemetry activity \
    --kind api_error \
    --tool "StopFailure" \
    --summary "$SUMMARY" \
    >/dev/null 2>&1
) &
disown 2>/dev/null || true

exit 0
