#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# Hook: PostCompact  (async — pairs with PreCompact synaptory-reanchor.sh)
# Purpose: Log the compact event so the CP session view shows context-window
# cycle boundaries. Provides confirmation that the PreCompact reanchor
# (synaptory-reanchor.sh) ran before compression — a PostCompact with no
# preceding reanchor event in events.jsonl indicates a hook-ordering problem.
#
# PostCompact stdin (Claude Code):
#   {"summary": "…", "session_id": "…", "cwd": "…",
#    "hook_event_name": "PostCompact"}

set -u

_HOOK_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=lib/resolve-python.sh
source "${_HOOK_ROOT}/hooks/lib/resolve-python.sh" 2>/dev/null || exit 0
_py="${SYNAPTORY_PYTHON:-python3}"

SUITE_DIR="${CLAUDE_PROJECT_DIR:-}/.synaptory"
if [ -z "${CLAUDE_PROJECT_DIR:-}" ] || [ ! -d "$SUITE_DIR" ]; then
  exit 0
fi

cli=$("${_HOOK_ROOT}/hooks/_resolve-cli.sh" 2>/dev/null || true)
if [[ -z "$cli" ]] || [[ ! -x "$cli" ]]; then
  exit 0
fi

INPUT="$(cat 2>/dev/null || true)"

SUMMARY_LEN=$(printf '%s' "$INPUT" | "$_py" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(len(d.get('summary') or ''))
except Exception:
    print(0)
" 2>/dev/null || echo "0")

(
  "$cli" telemetry activity \
    --kind compact \
    --tool "PostCompact" \
    --summary "Context compacted (summary ${SUMMARY_LEN} chars)" \
    >/dev/null 2>&1
) &
disown 2>/dev/null || true

exit 0
