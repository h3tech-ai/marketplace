#!/usr/bin/env bash

# --- synaptory: resolve Python interpreter (magic-number safe) ---
# shellcheck source=lib/resolve-python.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/resolve-python.sh"
: "${SYNAPTORY_PYTHON:?synaptory requires a working Python 3 interpreter (set SYNAPTORY_PYTHON to override)}"
# -------------------------------------------------------------
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
# Hook: SubagentStop
# Purpose: After a subagent completes, verify it wrote a valid receipt to
# .synaptory/.orchestrator/receipts/. Mode-aware enforcement:
#   Autonomous: exit 1 on invalid/missing receipt (BLOCK pipeline)
#   Controlled: exit 0 with warnings (inform but don't block)

_HOOK_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=./_plugin-env.sh
PLUGIN_ROOT="$_HOOK_ROOT" source "${_HOOK_ROOT}/hooks/_plugin-env.sh"
# shellcheck source=lib/resolve-python.sh
source "${_HOOK_ROOT}/hooks/lib/resolve-python.sh"
_py="${SYNAPTORY_PYTHON:-python3}"

SUITE_DIR="${CLAUDE_PROJECT_DIR}/.synaptory"
# Receipts dir: per-spec when SYNAPTORY_ACTIVE_SPEC is set (or recoverable from
# pipeline-state.json) and the per-spec directory exists
# (docs/multi-spec-design.md §5.3); falls back to the legacy flat path
# otherwise.
#
# Belt-and-suspenders: in multi-spec projects, _plugin-env.sh exports
# SYNAPTORY_ACTIVE_SPEC from pipeline-state.json's `active_spec`. We repeat the
# lookup here for hooks that bypass _plugin-env.sh (e.g. direct test
# invocations) and for sessions that pre-date the export. Without this
# fallback, every multi-spec receipt landed in the legacy flat dir and
# produced spurious receipt_invalid spans.
if [ -z "${SYNAPTORY_ACTIVE_SPEC:-}" ] \
   && [ -r "$SUITE_DIR/.orchestrator/pipeline-state.json" ]; then
  SYNAPTORY_ACTIVE_SPEC=$(
    sed -n 's/.*"active_spec"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      "$SUITE_DIR/.orchestrator/pipeline-state.json" | head -n1
  )
  [ -n "$SYNAPTORY_ACTIVE_SPEC" ] && export SYNAPTORY_ACTIVE_SPEC
fi
if [ -n "${SYNAPTORY_ACTIVE_SPEC:-}" ] \
   && [ -d "$SUITE_DIR/.orchestrator/specs/$SYNAPTORY_ACTIVE_SPEC" ]; then
  RECEIPTS_DIR="$SUITE_DIR/.orchestrator/specs/$SYNAPTORY_ACTIVE_SPEC/receipts"
else
  RECEIPTS_DIR="$SUITE_DIR/.orchestrator/receipts"
fi
HOOK_LIB_DIR="${CLAUDE_PLUGIN_ROOT}/hooks/lib"
LOGGER="$HOOK_LIB_DIR/synaptory_logger.py"

# Always decrement the active-depth counter on exit — we incremented it in
# SubagentStart, so this keeps the counter in sync whether the subagent
# finishes cleanly, fails validation, or times out. See BEA4-F1 / H7-F1.
_decrement_depth_on_exit() {
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -f "$LOGGER" ]; then
    "$_py" "$LOGGER" "$CLAUDE_PROJECT_DIR" --depth-dec >/dev/null 2>&1 || true
    "$_py" "$LOGGER" "$CLAUDE_PROJECT_DIR" subagent_stop >/dev/null 2>&1 || true
  fi
}
trap _decrement_depth_on_exit EXIT

# Exit silently if no workspace (non-crew project)
if [ ! -d "$SUITE_DIR" ]; then
  exit 0
fi

# Read subagent info from stdin
INPUT=$(cat)
SUBAGENT_DESC="unknown"
TRANSCRIPT_PATH=""
SUBAGENT_AGENT_ID=""
SUBAGENT_SESSION_ID=""
if [ -n "${_py:-}" ]; then
  PARSED=$(echo "$INPUT" | "$_py" -c "
import sys, json
try:
  d = json.load(sys.stdin)
  print(d.get('description', d.get('name', 'unknown')))
  print(d.get('transcript_path', ''))
  print(d.get('agent_id', '') or '')
  print(d.get('session_id', '') or '')
except:
  print('unknown')
  print('')
  print('')
  print('')
" 2>/dev/null)
  SUBAGENT_DESC=$(echo "$PARSED" | sed -n '1p')
  TRANSCRIPT_PATH=$(echo "$PARSED" | sed -n '2p')
  SUBAGENT_AGENT_ID=$(echo "$PARSED" | sed -n '3p')
  SUBAGENT_SESSION_ID=$(echo "$PARSED" | sed -n '4p')
fi

# Issue #130: only enforce receipts + emit spans for synaptory-plugin
# subagents. The SubagentStop payload carries no agent_type, so we rely on the
# marker synaptory-inject-protocols.sh dropped at SubagentStart for
# `synaptory:*` dispatches. No marker → this is some other subagent (Workflow
# worker, Explore/general-purpose, another plugin's agent) that never owed a
# synaptory receipt; skip silently so it doesn't emit a false receipt_invalid
# span or trip the fail-closed exit-2. The depth-counter trap (registered
# above) still fires on this early exit; the span-end trap is not yet set, so
# no span is emitted. Consume the marker so it can't leak.
if [ -n "${SUBAGENT_AGENT_ID:-}" ]; then
  _marker_dir="${SUITE_DIR}/.orchestrator/subagent-markers"
  _safe_id=$(printf '%s' "$SUBAGENT_AGENT_ID" | tr -c 'A-Za-z0-9_.-' '_')
  if [ -e "${_marker_dir}/${_safe_id}" ]; then
    rm -f "${_marker_dir}/${_safe_id}" 2>/dev/null || true
  else
    exit 0
  fi
else
  # No agent_id at all → can't correlate to a synaptory dispatch; don't enforce.
  exit 0
fi

# Phase 5 — close the OTLP span opened by synaptory-inject-protocols.sh.
# Deterministic span_id derivation matches the start side; no state
# plumbing needed. The status flips to "error" on the failure paths
# below by re-assigning SYNAPTORY_SPAN_STATUS / SYNAPTORY_SPAN_MESSAGE; the
# trap fires once on exit with the latest values.
OTEL_WRITER="${CLAUDE_PLUGIN_ROOT}/hooks/lib/otel_writer.py"
SYNAPTORY_SPAN_STATUS="ok"
SYNAPTORY_SPAN_MESSAGE=""
_emit_span_end_on_exit() {
  if [ -f "$OTEL_WRITER" ] && [ -n "${SUBAGENT_AGENT_ID:-}" ]; then
    "$_py" "$OTEL_WRITER" end \
      --suite-dir "$SUITE_DIR" \
      --session-id "${SUBAGENT_SESSION_ID:-}" \
      --agent-id "$SUBAGENT_AGENT_ID" \
      --status "$SYNAPTORY_SPAN_STATUS" \
      ${SYNAPTORY_SPAN_MESSAGE:+--message "$SYNAPTORY_SPAN_MESSAGE"} \
      >/dev/null 2>&1 || true
  fi
}
# Stack this trap on top of the existing depth-counter trap. Both fire
# on exit; order is registration order.
_prev_exit_trap=$(trap -p EXIT 2>/dev/null | sed -E "s/^trap -- '(.*)' EXIT$/\1/")
trap "$_prev_exit_trap; _emit_span_end_on_exit" EXIT

# Read engagement mode
AUTONOMOUS="true"
if [ -f "$HOOK_LIB_DIR/mode_reader.py" ] && [ -n "${_py:-}" ]; then
  MODE_INFO=$("$_py" "$HOOK_LIB_DIR/mode_reader.py" "$CLAUDE_PROJECT_DIR" 2>/dev/null)
  if echo "$MODE_INFO" | grep -q "autonomous=False"; then
    AUTONOMOUS="false"
  fi
fi

# Find every receipt that has not yet been shipped, ship it, and mark it
# shipped via a sibling sentinel file. Replaces the previous time-marker
# race (find -newer /tmp/.crew-check-base) which lost receipts when the
# marker advanced past a receipt's mtime between when the agent finished
# writing and when the hook ran. Per-receipt sentinels are immune to
# clock skew, hook-fire timing, and concurrent SubagentStop fires.
#
# Sentinel format: <receipt>.shipped (zero-byte file, mtime = ship time).
# Idempotent: re-shipping after an agent rewrites a receipt (mtime
# advances past the sentinel) ships once. The CLI's outbox handles
# transient failures; touching the sentinel after a failed call would
# permanently lose the event, so we ONLY touch on success.
RECEIPTS_TO_SHIP=()
if [ -d "$RECEIPTS_DIR" ]; then
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    sentinel="${f}.shipped"
    if [ ! -e "$sentinel" ] || [ "$f" -nt "$sentinel" ]; then
      RECEIPTS_TO_SHIP+=("$f")
    fi
  done < <(find "$RECEIPTS_DIR" -name "*.json" 2>/dev/null)
fi

# Backfill token_usage from the parent transcript before shipping. Subagents
# can't see their own final Usage — that lives only in the parent's result
# message — so receipts written by the subagent ship with no token counts
# and /cost rolls them up at $0. We run in the parent context here, with
# `transcript_path` pointing at the parent's JSONL, so we can parse the
# usage block out and patch any receipt missing it. Best-effort: a parse
# miss leaves the receipt untouched and the validator's existing warning
# still fires. See transcript_usage.py.
if [ "${#RECEIPTS_TO_SHIP[@]}" -gt 0 ] \
   && [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ] \
   && [ -f "$HOOK_LIB_DIR/transcript_usage.py" ] && [ -n "${_py:-}" ]; then
  for receipt in "${RECEIPTS_TO_SHIP[@]}"; do
    "$_py" "$HOOK_LIB_DIR/transcript_usage.py" "$receipt" "$TRANSCRIPT_PATH" \
      >/dev/null 2>&1 || true
  done
fi

# Ship every unshipped receipt.
if [ "${#RECEIPTS_TO_SHIP[@]}" -gt 0 ]; then
  cli=$("${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}/hooks/_resolve-cli.sh" 2>/dev/null || true)
  if [[ -n "$cli" ]] && [[ -x "$cli" ]]; then
    for receipt in "${RECEIPTS_TO_SHIP[@]}"; do
      if "$cli" telemetry receipt --file "$receipt" >/dev/null 2>&1; then
        touch "${receipt}.shipped" 2>/dev/null || true
      fi
    done
  fi
fi

# Pick the newest receipt for the validation logging below, regardless
# of ship status — we still want to flag receipt_missing even if the
# only receipt failed to ship.
RECENT_RECEIPT=""
if [ -d "$RECEIPTS_DIR" ]; then
  # `stat -f "%m %N"` is BSD/macOS syntax. On GNU/Linux `stat -f` means
  # "display filesystem status" and emits multi-line output including
  # a "Total: " label that sorts to the top — receipt path resolves to
  # the literal string "Total:" and the hook fails with "Receipt file
  # not found: Total:". Surfaced by the e2e CI runner.
  #
  # `ls -1t` is portable: sorts by mtime descending across BSD + GNU
  # coreutils. -print0/-0 keeps spaces in receipt names safe.
  RECENT_RECEIPT=$(find "$RECEIPTS_DIR" -name "*.json" -not -name "*.shipped" -print0 2>/dev/null \
    | xargs -0 ls -1t 2>/dev/null \
    | head -1)
fi

# No receipt on disk — try recovering one from the transcript before
# logging `receipt_missing`. Some subagents emit a structurally-valid
# receipt JSON in their response text but forget to call Write. We
# rebuild the file from the parent transcript so the pipeline keeps
# moving and the validator can still flag any schema issues. See
# plugin-claude/hooks/lib/receipt_recovery.py.
if [ -z "$RECENT_RECEIPT" ] \
   && [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ] \
   && [ -f "$HOOK_LIB_DIR/receipt_recovery.py" ] && [ -n "${_py:-}" ]; then
  STORY_HINT=""
  if [ -f "$SUITE_DIR/.orchestrator/pipeline-state.json" ]; then
    STORY_HINT=$("$_py" -c "
import json, sys
try:
  d = json.load(open('$SUITE_DIR/.orchestrator/pipeline-state.json'))
  print(d.get('current_story_id') or d.get('current_story') or '')
except Exception:
  print('')
" 2>/dev/null)
  fi
  RECOVERED=$("$_py" "$HOOK_LIB_DIR/receipt_recovery.py" \
    "$TRANSCRIPT_PATH" "$RECEIPTS_DIR" "$STORY_HINT" 2>/dev/null || true)
  if [ -n "$RECOVERED" ] && [ -f "$RECOVERED" ]; then
    RECENT_RECEIPT="$RECOVERED"
    if [ -f "$LOGGER" ]; then
      "$_py" "$LOGGER" "$CLAUDE_PROJECT_DIR" receipt_recovered_from_transcript \
        "subagent=$SUBAGENT_DESC" "path=$RECOVERED" >/dev/null 2>&1 || true
    fi
    echo "[synaptory] Recovered receipt from transcript: $RECOVERED" >&2
  fi
fi

# No receipt found (and recovery either skipped or yielded nothing)
if [ -z "$RECENT_RECEIPT" ]; then
  if [ -f "$LOGGER" ]; then
    "$_py" "$LOGGER" "$CLAUDE_PROJECT_DIR" receipt_missing \
      "subagent=$SUBAGENT_DESC" "autonomous=$AUTONOMOUS" >/dev/null 2>&1 || true
  fi
  SYNAPTORY_SPAN_STATUS="error"
  SYNAPTORY_SPAN_MESSAGE="receipt_missing"
  if [ "$AUTONOMOUS" = "true" ]; then
    echo "[synaptory] ERROR: Subagent '${SUBAGENT_DESC}' completed without writing a receipt to ${RECEIPTS_DIR}/" >&2
    echo "[synaptory] Every completed task must produce a receipt. See receipt-protocol.md." >&2
    # exit 2 (not 1): the hook contract treats exit 2 as a BLOCKING error that
    # force-feeds stderr to Claude. exit 1 is non-blocking (first stderr line
    # only) — with the P2 loop engine driving continuation, a non-blocking
    # failure could be steamrolled. exit 2 makes the fail-closed guarantee
    # harness-enforced, not prompt-hoped. (epic #75)
    exit 2
  else
    echo "[synaptory] WARNING: Subagent '${SUBAGENT_DESC}' completed without writing a receipt to ${RECEIPTS_DIR}/" >&2
    exit 0
  fi
fi

# Receipt found — validate with receipt_validator.py
if [ -f "$HOOK_LIB_DIR/receipt_validator.py" ] && [ -n "${_py:-}" ]; then
  VALIDATION_OUTPUT=$("$_py" "$HOOK_LIB_DIR/receipt_validator.py" "$RECENT_RECEIPT" "$CLAUDE_PROJECT_DIR" 2>/dev/null)
  VALIDATION_EXIT=$?

  if [ $VALIDATION_EXIT -ne 0 ]; then
    SYNAPTORY_SPAN_STATUS="error"
    SYNAPTORY_SPAN_MESSAGE="receipt_invalid"
    # Validation failed — extract errors
    ERRORS=$(echo "$VALIDATION_OUTPUT" | "$_py" -c "
import sys, json
try:
  r = json.load(sys.stdin)
  for e in r.get('errors', []):
    print(f'  - {e}')
  for w in r.get('warnings', []):
    print(f'  [warn] {w}')
except:
  print('  - Could not parse validation output')
" 2>/dev/null)

    if [ "$AUTONOMOUS" = "true" ]; then
      echo "[synaptory] ERROR: Receipt validation failed for '${SUBAGENT_DESC}':" >&2
      echo "$ERRORS" >&2
      echo "[synaptory] Fix receipt issues before proceeding. See receipt-protocol.md." >&2
      exit 2  # blocking (see the receipt_missing note above) — epic #75
    else
      echo "[synaptory] WARNING: Receipt validation issues for '${SUBAGENT_DESC}':" >&2
      echo "$ERRORS" >&2
      # Inject validation report as additional context for thorough/meticulous
      echo "$VALIDATION_OUTPUT" | SYNAPTORY_HOOK_LIB="${CLAUDE_PLUGIN_ROOT}/hooks/lib" "$_py" -c "
import sys, os, json
sys.path.insert(0, os.environ['SYNAPTORY_HOOK_LIB'])
from hook_io import emit
try:
  r = json.load(sys.stdin)
  report = 'Receipt Validation Report:\n'
  for e in r.get('errors', []):
    report += f'ERROR: {e}\n'
  for w in r.get('warnings', []):
    report += f'WARNING: {w}\n'
  emit('SubagentStop', additional_context=report)
except Exception:
  pass
" 2>/dev/null
      exit 0
    fi
  fi

  # Validation passed — run verification commands (required since SDD enforcement)
  if [ -f "$HOOK_LIB_DIR/verification_runner.py" ]; then
    VERIFY_OUTPUT=$("$_py" "$HOOK_LIB_DIR/verification_runner.py" "$RECENT_RECEIPT" "$CLAUDE_PROJECT_DIR" 2>/dev/null)
    VERIFY_EXIT=$?

    # Check if verification was skipped (missing verification_commands)
    SKIPPED=$(echo "$VERIFY_OUTPUT" | "$_py" -c "
import sys, json
try:
  r = json.load(sys.stdin)
  print('true' if r.get('skipped', False) else 'false')
except:
  print('false')
" 2>/dev/null)

    if [ "$SKIPPED" = "true" ]; then
      if [ "$AUTONOMOUS" = "true" ]; then
        echo "[synaptory] ERROR: Receipt for '${SUBAGENT_DESC}' is missing verification_commands." >&2
        echo "[synaptory] Every receipt must include at least one verification command. See receipt-protocol.md." >&2
        exit 2  # blocking (see the receipt_missing note above) — epic #75
      else
        echo "[synaptory] WARNING: Receipt for '${SUBAGENT_DESC}' is missing verification_commands." >&2
        exit 0
      fi
    fi

    # Surface unreplayable executed-proof entries (REQ-E-003: marked, not
    # silently accepted). These are warning-level — the recorded exit_code
    # remains the DoD evidence — but the agent/orchestrator must SEE the
    # contract explanation so the next receipt records replayable proof.
    UNREPLAYED=$(echo "$VERIFY_OUTPUT" | "$_py" -c "
import sys, json
try:
  r = json.load(sys.stdin)
  for e in r.get('results', []):
    if e.get('replayed') is False:
      print(f'  - {e.get(\"command\", \"?\")}: {e.get(\"reason\", \"unreplayable\")}')
except:
  pass
" 2>/dev/null)
    if [ -n "$UNREPLAYED" ]; then
      echo "[synaptory] WARNING: verification command(s) for '${SUBAGENT_DESC}' could not be replayed (recorded exit codes accepted as evidence):" >&2
      echo "$UNREPLAYED" >&2
    fi

    if [ $VERIFY_EXIT -ne 0 ]; then
      SYNAPTORY_SPAN_STATUS="error"
      SYNAPTORY_SPAN_MESSAGE="verification_failed"
      VERIFY_ERRORS=$(echo "$VERIFY_OUTPUT" | "$_py" -c "
import sys, json
try:
  r = json.load(sys.stdin)
  for f in r.get('failures', []):
    print(f'  - {f[\"command\"]}: expected exit {f[\"expected_exit_code\"]}, got {f[\"actual_exit_code\"]}')
    # Rejected commands carry the contract's self-explaining reason (ending
    # with the concrete fix) in stderr_snippet — feed it to the agent.
    snippet = f.get('stderr_snippet') or ''
    if snippet.startswith('verification command rejected:'):
      print(f'    {snippet}')
except:
  pass
" 2>/dev/null)

      if [ "$AUTONOMOUS" = "true" ]; then
        echo "[synaptory] ERROR: Verification commands failed for '${SUBAGENT_DESC}':" >&2
        echo "$VERIFY_ERRORS" >&2
        exit 2  # blocking (see the receipt_missing note above) — epic #75
      else
        echo "[synaptory] WARNING: Verification commands had failures for '${SUBAGENT_DESC}':" >&2
        echo "$VERIFY_ERRORS" >&2
        exit 0
      fi
    fi
  fi

  # All checks passed
  exit 0
else
  # No validator available — fall back to basic existence check (receipt was found)
  exit 0
fi
