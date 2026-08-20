#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# Hook: SubagentStart
# Purpose: Inject the core protocol files into every subagent at startup.
# This eliminates the need for each skill to individually cat protocol files.

_HOOK_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=./_plugin-env.sh
PLUGIN_ROOT="$_HOOK_ROOT" source "${_HOOK_ROOT}/hooks/_plugin-env.sh"
# shellcheck source=lib/resolve-python.sh
source "${_HOOK_ROOT}/hooks/lib/resolve-python.sh"
_py="${SYNAPTORY_PYTHON:-python3}"

# Telemetry — record the subagent span. Fire before the depth guard so even
# blocked spawns are visible in the control-plane dashboard.
cli=$("${_HOOK_ROOT}/hooks/_resolve-cli.sh" 2>/dev/null || true)
if [[ -n "$cli" ]] && [[ -x "$cli" ]]; then
  SYNAPTORY_STDIN=$(cat)
  # Claude Code's SubagentStart hook stdin is JSON shaped like:
  #   {"agent_id": "...", "agent_type": "synaptory:software-engineer",
  #    "session_id": "...", "transcript_path": "...", "cwd": "...",
  #    "hook_event_name": "SubagentStart"}
  # Claude Code passes the plugin-namespaced subagent type, so synaptory's own
  # agents arrive as "synaptory:<role>". We capture agent_id so SubagentStop
  # can correlate the start span with the receipt that follows.
  span_meta=$(printf '%s' "$SYNAPTORY_STDIN" | "$_py" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get('agent_id') or '') + '\t' + (d.get('agent_type') or 'unknown'))
except Exception:
    print('\tunknown')
" 2>/dev/null || printf '\tunknown')
  agent_id="${span_meta%%	*}"
  agent_type="${span_meta##*	}"

  # Issue #130: only synaptory-plugin subagents are pipeline agents that emit a
  # span + owe a receipt. Other subagents in the session — the Workflow tool's
  # workers, `Explore`/`general-purpose`, agents from other plugins — never
  # write a synaptory receipt, so emitting spans for them made SubagentStop mark
  # every one `receipt_invalid`, drowning the reliability error rate (~97% noise
  # in prod). Gate span + receipt emission on the `synaptory:` namespace, and
  # drop a per-agent marker so SubagentStop (whose payload carries no
  # agent_type) can make the same call.
  case "$agent_type" in
    synaptory:*) _synaptory_agent=1 ;;
    *)           _synaptory_agent=0 ;;
  esac

  if [ "$_synaptory_agent" = "1" ]; then
    if [ -n "${agent_id:-}" ] && [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
      _marker_dir="${CLAUDE_PROJECT_DIR}/.synaptory/.orchestrator/subagent-markers"
      mkdir -p "$_marker_dir" 2>/dev/null || true
      _safe_id=$(printf '%s' "$agent_id" | tr -c 'A-Za-z0-9_.-' '_')
      : > "${_marker_dir}/${_safe_id}" 2>/dev/null || true
    fi

    "$cli" telemetry subagent-start \
      --role "${agent_type:-unknown}" \
      ${agent_id:+--subagent-id "$agent_id"} \
      --backend claude \
      --model "${SYNAPTORY_AGENT_MODEL:-claude-sonnet-4-5}" \
      >/dev/null 2>&1 || true

    # Phase 5 — open an OTLP span for this subagent. The span_id is
    # derived deterministically from agent_id, so SubagentStop can close
    # the same span without state plumbing. Best-effort; telemetry must
    # never break a hook.
    # Extract session_id and prompt_id for OTel span correlation.
    # prompt_id is a per-turn UUID available in Claude Code v2.1.196+ stdin.
    otel_meta=$(printf '%s' "$SYNAPTORY_STDIN" | "$_py" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get('session_id') or '') + '\t' + (d.get('prompt_id') or ''))
except Exception:
    print('\t')
" 2>/dev/null || printf '\t')
    session_id="${otel_meta%%	*}"
    prompt_id="${otel_meta##*	}"
    OTEL_WRITER="${_HOOK_ROOT}/hooks/lib/otel_writer.py"
    if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -f "$OTEL_WRITER" ]; then
      "$_py" "$OTEL_WRITER" start \
        --suite-dir "${CLAUDE_PROJECT_DIR}/.synaptory" \
        --session-id "${session_id}" \
        --agent-id "${agent_id}" \
        --role "${agent_type:-unknown}" \
        --backend claude \
        --model "${SYNAPTORY_AGENT_MODEL:-claude-sonnet-4-5}" \
        ${prompt_id:+--prompt-id "$prompt_id"} \
        >/dev/null 2>&1 || true
    fi
  fi

  # Re-feed stdin to the rest of the hook (protocol injection runs for every
  # subagent, synaptory or not) — keep this OUTSIDE the gate above.
  printf '%s' "$SYNAPTORY_STDIN" > /tmp/.synaptory-subagent-input.$$
  exec < /tmp/.synaptory-subagent-input.$$
  trap 'rm -f /tmp/.synaptory-subagent-input.$$' EXIT
fi

# --- Mid-session auth gate (fail-closed) ---
# The SessionStart hook gates auth at startup, but a session can lapse during
# a long-running Claude Code session (24h token TTL). Re-check here so an
# expired session that can't be silently renewed BLOCKS the next agent
# dispatch — "nothing runs while expired". `whoami --check` renews via the
# refresh token when possible and never opens a browser. Skippable with
# SYNAPTORY_AUTH_NO_GATE=1 (CI / offline).
if [[ -n "$cli" ]] && [[ -x "$cli" ]] && [[ "${SYNAPTORY_AUTH_NO_GATE:-}" != "1" ]]; then
  if ! "$cli" whoami --check >/dev/null 2>&1; then
    printf '{"additionalContext": "🔒 BLOCKED — synaptory session expired. Your session lapsed mid-session and could not be auto-renewed, so this agent dispatch is refused. Run `synaptory login` in a terminal, then retry. A new Claude Code session will also re-prompt at startup."}\n'
    exit 1
  fi
fi

# --- Recursive-fork guard (BEA4-F1 / H7-F1) ---
MAX_AGENT_DEPTH=3
LOGGER="${CLAUDE_PLUGIN_ROOT:-}/hooks/lib/synaptory_logger.py"
if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -f "$LOGGER" ]; then
  NEW_DEPTH=$("$_py" "$LOGGER" "$CLAUDE_PROJECT_DIR" --depth-inc 2>/dev/null || echo 0)
  if [ -n "$NEW_DEPTH" ] && [ "$NEW_DEPTH" -gt "$MAX_AGENT_DEPTH" ] 2>/dev/null; then
    "$_py" "$LOGGER" "$CLAUDE_PROJECT_DIR" --depth-dec >/dev/null 2>&1 || true
    "$_py" "$LOGGER" "$CLAUDE_PROJECT_DIR" depth_guard_blocked \
      "attempted_depth=$NEW_DEPTH" "max=$MAX_AGENT_DEPTH" >/dev/null 2>&1 || true
    printf '{"additionalContext": "BLOCKED — agent delegation depth %s exceeds MAX_AGENT_DEPTH=%s. Refusing to start. If this is legitimate, audit the call chain in .synaptory/.orchestrator/events.jsonl, then raise MAX_AGENT_DEPTH in plugin/hooks/synaptory-inject-protocols.sh."}\n' \
      "$NEW_DEPTH" "$MAX_AGENT_DEPTH"
    exit 1
  fi
  "$_py" "$LOGGER" "$CLAUDE_PROJECT_DIR" subagent_start "depth=$NEW_DEPTH" >/dev/null 2>&1 || true
fi

# Protocol bodies live on the control plane (envelope-encrypted, per-UPN
# watermarked at fetch). The CLI's local cache — populated by SessionStart's
# `skills sync` — serves them offline-friendly after the first session of the
# day. We fall back to the on-disk source tree when the CLI is unavailable
# so `claude --plugin-dir ./plugin` keeps working in dev without a session.
#
# In a distributed build the source tree carries no protocol bodies, so the
# CLI path is the only one that returns content. That is the design — the
# previous build encrypted these files and `cat` silently failed.
PROTOCOLS_DIR="${CLAUDE_PLUGIN_ROOT}/skills/_shared/protocols"
CONTEXT=""

# Order and membership matter — these shape the system prompt every subagent
# sees. Keep this list in sync with api/synaptory_api/seed.py's protocol-seeding scope.
protocol_names=(
  receipt-protocol
  input-validation
  tool-efficiency
  freshness-protocol
  iron-laws
  verification-discipline
  socratic-gate
  anti-safe-harbor
  script-output-handling
  clean-code-self-check
  scope-challenge
  finding-memory
  tdd-discipline
  code-review-response
  subagent-isolation
  source-attribution
  open-decision-registry
)

# Fetch protocols in parallel — 17 sequential CLI calls add visible latency
# to every SubagentStart even on warm-cache hits (each call is a fresh Go
# process). Background-fan-out drops the cold path from O(N) to O(1) and the
# warm path from N×proc-spawn to ~1×proc-spawn. Output files are
# index-prefixed so we can concatenate in deterministic prompt order — the
# 17 protocols are NOT order-independent.
_tmpdir=$(mktemp -d -t synaptory-protocols.XXXXXX)
# An earlier EXIT trap may already be set (telemetry stdin tee). `bash` traps
# don't stack — the last `trap` wins — so re-establish a combined cleanup.
trap 'rm -rf "$_tmpdir"; rm -f /tmp/.synaptory-subagent-input.$$' EXIT

_fetch_one() {
  local idx="$1" name="$2" out="$3"
  local body=""
  if [[ -n "$cli" ]] && [[ -x "$cli" ]]; then
    body=$("$cli" skills get "protocols/$name" 2>/dev/null || true)
  fi
  if [[ -z "$body" ]] && [ -f "$PROTOCOLS_DIR/$name.md" ]; then
    body=$(cat "$PROTOCOLS_DIR/$name.md" 2>/dev/null || true)
  fi
  if [[ -n "$body" ]]; then
    printf '%s\n\n---\n' "$body" > "$out"
  fi
}

idx=0
for name in "${protocol_names[@]}"; do
  printf -v padded '%02d' "$idx"
  _fetch_one "$idx" "$name" "${_tmpdir}/${padded}-${name}" &
  idx=$((idx + 1))
done
wait

# Concatenate in index order. find -print0 + sort -z keeps newlines in bodies
# safe and gives us a stable order independent of filesystem listing quirks.
while IFS= read -r -d '' part; do
  CONTEXT="${CONTEXT}$(cat "$part")
"
done < <(find "$_tmpdir" -maxdepth 1 -type f -print0 | sort -z)

# --- #30: project risk-checklist injection ---
# A healthcare / security project points `risk_checklist:` in .synaptory.yaml at
# a markdown file of review patterns (PHI in logs/URLs, error_meta redaction,
# MRN in logger.*, …). Inject it into every subagent so the patterns that catch
# runtime-only leaks aren't invisible to the delivery agents meant to prevent
# them. Same delivery model as protocols above; role is unknowable here, so we
# inject for all (harmless for non-delivery roles).
if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -f "${CLAUDE_PROJECT_DIR}/.synaptory.yaml" ]; then
  _rc_rel=$("$_py" - "${CLAUDE_PROJECT_DIR}/.synaptory.yaml" <<'PYRC' 2>/dev/null || true
import sys
try:
    for raw in open(sys.argv[1], encoding="utf-8"):
        line = raw.split("#", 1)[0].rstrip()
        if not line or line[0] in " \t":
            continue
        if line.startswith("risk_checklist:"):
            print(line.split(":", 1)[1].strip().strip('"').strip("'"))
            break
except Exception:
    pass
PYRC
)
  if [ -n "$_rc_rel" ]; then
    _rc_path="${CLAUDE_PROJECT_DIR}/${_rc_rel}"
    case "$_rc_rel" in /*) _rc_path="$_rc_rel" ;; esac
    if [ -f "$_rc_path" ]; then
      _rc_body=$(cat "$_rc_path" 2>/dev/null || true)
      if [ -n "$_rc_body" ]; then
        # Held in its OWN variable, NOT appended to CONTEXT: the compact-
        # protocol load below resets CONTEXT, so anything appended here
        # would be silently dropped. Re-appended after the compact payload
        # is selected so healthcare/security projects' mandatory review
        # patterns actually reach subagents.
        RISK_CHECKLIST="
# Project Risk Checklist (${_rc_rel})

Project-supplied review checklist — MANDATORY context. Apply every applicable
item to the code you produce or verify in this story; treat an unaddressed
applicable item as a blocking finding.

${_rc_body}

---
"
      fi
    fi
  fi
fi

# Load the compact protocol index for additionalContext emission. CP-seeded
# as "hooks/data/compacted-protocols" (api/synaptory_api/seed.py), delivered
# via the same CLI-fetch-then-disk-fallback model as the individual
# protocols above. The distributed build purges hooks/data/*.md (ADR-016),
# so the on-disk copy only exists in source-tree dev — a real install MUST
# get this via the CLI or every subagent dispatch loses protocol content.
COMPACT_PROTO="${_HOOK_ROOT}/hooks/data/compacted-protocols.md"
CONTEXT=""
if [[ -n "$cli" ]] && [[ -x "$cli" ]]; then
  CONTEXT=$("$cli" skills get "hooks/data/compacted-protocols" 2>/dev/null || true)
fi
if [[ -z "$CONTEXT" ]] && [ -f "$COMPACT_PROTO" ]; then
  CONTEXT=$(cat "$COMPACT_PROTO" 2>/dev/null || true)
fi
if [[ -z "$CONTEXT" ]]; then
  CONTEXT="# Synaptory Protocols (unavailable — CLI unreachable and no on-disk fallback; see .synaptory/.protocols/ for full versions)"
fi

# Re-append the project risk checklist (captured above before the CONTEXT
# reset) so it survives into additionalContext for healthcare/security
# projects. Empty for projects without a `risk_checklist:` entry.
CONTEXT="${CONTEXT}${RISK_CHECKLIST:-}"

# --- #163: Execution Envelope injection ---
# Render the role-scoped evidence-contract envelope (receipt path + required
# fields, the two verification_commands forms, replay rules, per-role
# evidence obligations) so every synaptory delivery agent sees the exact
# machine-checked contract SubagentStop will enforce. Non-synaptory
# agent_types (and the CLI-unavailable path, where agent_type is never
# parsed) render to "" and nothing is appended. Best-effort: the envelope
# must never break a dispatch.
ENVELOPE=$("$_py" "${_HOOK_ROOT}/hooks/lib/evidence_contract.py" envelope \
  --agent-type "${agent_type:-}" 2>/dev/null || true)
if [ -n "$ENVELOPE" ]; then
  CONTEXT="${CONTEXT}

${ENVELOPE}
"
fi

printf '%s' "$CONTEXT" | SYNAPTORY_HOOK_LIB="${_HOOK_ROOT}/hooks/lib" "$_py" -c "
import sys, os
sys.path.insert(0, os.environ['SYNAPTORY_HOOK_LIB'])
from hook_io import emit
emit('SubagentStart', additional_context=sys.stdin.read())
" 2>/dev/null || exit 0

exit 0
