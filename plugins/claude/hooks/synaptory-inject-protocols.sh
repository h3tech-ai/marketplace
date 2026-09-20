#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# Hook: SubagentStart
# Purpose: Inject the compact protocol index into every subagent at dispatch.
# This eliminates the need for each skill to individually cat protocol files.

_HOOK_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=./_plugin-env.sh
PLUGIN_ROOT="$_HOOK_ROOT" source "${_HOOK_ROOT}/hooks/_plugin-env.sh"
# shellcheck source=lib/resolve-python.sh
source "${_HOOK_ROOT}/hooks/lib/resolve-python.sh"
_py="${SYNAPTORY_PYTHON:-python3}"
# The dispatch resolver lives beside the other shared runtime modules.
# This was referenced below before it was ever assigned, so the test on
# `$HOOK_LIB_DIR/dispatched_receipts.py` read `/dispatched_receipts.py`,
# never matched, and the marker's correlation line came out empty on
# every dispatch. The suite missed it because those tests wrote the
# marker themselves instead of running this hook (#396).
HOOK_LIB_DIR="${_HOOK_ROOT}/hooks/lib"

# --- Dispatch identity: read stdin ONCE, unconditionally (#512) -------------
# Claude Code's SubagentStart hook stdin is JSON shaped like:
#   {"agent_id": "...", "agent_type": "synaptory:software-engineer",
#    "session_id": "...", "transcript_path": "...", "cwd": "...",
#    "hook_event_name": "SubagentStart"}
# Claude Code passes the plugin-namespaced subagent type, so synaptory's own
# agents arrive as "synaptory:<role>". agent_id is what lets SubagentStop
# correlate this dispatch with the receipt that follows.
#
# This parse used to live inside the CLI gate below, because the first thing
# built on top of it was telemetry. Everything derived from it inherited that
# dependency by proximity, not by need: the marker, the `synaptory:` namespace
# classification, and #163's execution envelope are all local facts about the
# dispatch and none of them addresses a control plane. An unstamped tree
# resolves NO CLI by design (#320, #425), so on the source-tree sideload all
# three silently vanished — and that is exactly the run whose catalog
# retrieval takes the disk fallback and most needs attribution (#446, #512).
SYNAPTORY_STDIN=$(cat)
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

# The dispatching SESSION, read once here rather than inside the CLI gate
# below. It is split separately from span_meta above because that value is cut
# with ${..%%} / ${..##}, which a third field would silently break.
#
# Two consumers, and only one of them is telemetry: the OTel span correlation
# (CLI-gated, further down) and the Cycle-ownership claim (#791, never gated).
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

# Issue #130: only synaptory-plugin subagents are pipeline agents that emit a
# span + owe a receipt. Other subagents in the session — the Workflow tool's
# workers, `Explore`/`general-purpose`, agents from other plugins — never
# write a synaptory receipt, so emitting spans for them made SubagentStop mark
# every one `receipt_invalid`, drowning the reliability error rate (~97% noise
# in prod). Gate span + receipt emission on the `synaptory:` namespace, and
# drop a per-agent marker so SubagentStop (whose payload carries no
# agent_type) can make the same call. The namespace gate is what #130 bought;
# hoisting the marker out of the CLI gate does not widen it.
case "$agent_type" in
  synaptory:*) _synaptory_agent=1 ;;
  *)           _synaptory_agent=0 ;;
esac
_synaptory_role="${agent_type#synaptory:}"

# Re-feed stdin to the rest of the hook (protocol injection runs for every
# subagent, synaptory or not).
printf '%s' "$SYNAPTORY_STDIN" > /tmp/.synaptory-subagent-input.$$
exec < /tmp/.synaptory-subagent-input.$$
trap 'rm -f /tmp/.synaptory-subagent-input.$$' EXIT

# --- The per-dispatch marker: local, and never CLI-gated (#512) -------------
# Two readers, neither of which involves a control plane:
#   synaptory-verify-receipt.sh  which subagents owe a receipt, for which role,
#                                bound to which dispatch (#130, #340, #396)
#   core/lib/skill_fetch_log.py  which role a catalog retrieval belongs to,
#                                observed rather than inferred (#446)
# Writing it only when a CLI resolved made receipt enforcement and role
# attribution a side effect of telemetry reachability. They are not the same
# question.
if [ "$_synaptory_agent" = "1" ] \
   && [ -n "${agent_id:-}" ] && [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  _marker_dir="${CLAUDE_PROJECT_DIR}/.synaptory/.orchestrator/subagent-markers"
  mkdir -p "$_marker_dir" 2>/dev/null || true
  _safe_id=$(printf '%s' "$agent_id" | tr -c 'A-Za-z0-9_.-' '_')
  # The marker carries the ROLE, not just existence (#396). SubagentStop's
  # payload has no agent_type, and without it selection could only scope to
  # "the current work across all roles", which let a QE stop that wrote no
  # receipt be satisfied by the SE receipt sitting beside it. This is the
  # only place the exact dispatched role is observable, so it is recorded
  # here and consumed there.
  # Line 1 is the role. Line 2 is "story<TAB>dispatch_id" when exactly one
  # story holds an active dispatch for that role RIGHT NOW, which is the
  # only moment the correlation is unambiguous. Two simultaneous same-role
  # dispatches record nothing here, and selection falls back to the role.
  _marker_role="$_synaptory_role"
  _marker_link=""
  if [ -f "$HOOK_LIB_DIR/dispatched_receipts.py" ] && [ -n "${_py:-}" ]; then
    _marker_link=$("$_py" "$HOOK_LIB_DIR/dispatched_receipts.py" \
      --resolve-dispatch "$CLAUDE_PROJECT_DIR" "$_marker_role" 2>/dev/null || true)
  fi
  printf '%s\n%s\n' "$agent_type" "$_marker_link" \
    > "${_marker_dir}/${_safe_id}" 2>/dev/null || true

  # --- Cycle ownership: this session is the one driving the Cycle (#791) ----
  # Dispatching a delivery agent is the ACT that makes a session the holder,
  # and this is the only event that carries both the host session id and the
  # proof that the session writes into the Cycle. The Stop-hook loop then
  # speaks only to the holder, instead of to whatever session happens to be
  # pointed at the project directory.
  #
  # Ungated by the CLI for the same reason as the marker above (#512): who
  # holds a Cycle is a local fact about this checkout and addresses no control
  # plane. Best-effort — a failure here must never fail a dispatch.
  if [ -f "$HOOK_LIB_DIR/loop_engine.py" ] && [ -n "${session_id:-}" ]; then
    "$_py" "$HOOK_LIB_DIR/loop_engine.py" --claim \
      "$CLAUDE_PROJECT_DIR" "$session_id" "$_synaptory_role" \
      >/dev/null 2>&1 || true
  fi
fi

# --- Telemetry: this is the part that genuinely needs a CLI -----------------
# Fires before the depth guard so even blocked spawns are visible in the
# control-plane dashboard. An unstamped tree resolves no CLI and ships
# nothing, which is #320's rule and is left exactly as it was.
cli=$("${_HOOK_ROOT}/hooks/_resolve-cli.sh" 2>/dev/null || true)
if [[ -n "$cli" ]] && [[ -x "$cli" ]] && [ "$_synaptory_agent" = "1" ]; then
  _telemetry_story=""
  case "${_marker_link:-}" in
    ""|\?ambiguous*) ;;
    *) _telemetry_story="${_marker_link%%	*}" ;;
  esac
  # #577 -- `--model` is OMITTED when SYNAPTORY_AGENT_MODEL is unset, which is
  # the normal case: nothing on this path observes the model a dispatch
  # actually ran on. Claude Code's SubagentStart stdin carries no model id, and
  # the host's `Agent()` tool takes a tier alias rather than an exact id
  # (#519), so the hook has nothing to report.
  #
  # This used to default to the literal `claude-sonnet-4-5`, an id
  # `model-pins.json` has named under no tier since the pins rolled to
  # `claude-sonnet-5` / `claude-opus-4-8`. Every span therefore carried a
  # confident, uniform, wrong model. `model_pin_check` treats an ABSENT model
  # as "no recorded provenance at all", and #493's finding is that a
  # confidently wrong provenance value is worse than an absent one: an absent
  # one is legible as a gap, a wrong one is indistinguishable from a
  # measurement.
  #
  # Absence is carried end to end: the CLI flag defaults to "",
  # `SubagentStartReq.Model` is `omitempty`, and `subagent_spans.model` is
  # nullable. `/by-model` reads `Receipt.model` (agent-authored) and already
  # excludes nulls, so a NULL span model narrows nothing.
  "$cli" telemetry subagent-start \
    --role "${_synaptory_role:-unknown}" \
    ${agent_id:+--subagent-id "$agent_id"} \
    --backend claude \
    ${SYNAPTORY_AGENT_MODEL:+--model "$SYNAPTORY_AGENT_MODEL"} \
    ${_telemetry_story:+--story-id "$_telemetry_story"} \
    >/dev/null 2>&1 || true

  # Phase 5 — open an OTLP span for this subagent. The span_id is
  # derived deterministically from agent_id, so SubagentStop can close
  # the same span without state plumbing. Best-effort; telemetry must
  # never break a hook.
  #
  # Deliberately left inside the CLI gate rather than hoisted with the
  # marker: a span is telemetry, and the only thing that ever ships
  # spans-<session>.jsonl is `synaptory telemetry traces`. On an unstamped
  # tree SubagentStop will now write an `end` with no matching `start`,
  # which costs one JSONL line and nothing else — `buildOTLPPayload`
  # (cli/internal/cli/traces.go) builds its span list from `start` events
  # only, so an unpaired `end` is dropped rather than shipped as a
  # zero-duration span.
  #
  # session_id / prompt_id come from the single hoisted parse near the top.
  # They used to be re-read here, which was the only place that needed them
  # until the Cycle-ownership claim did too (#791) — and a second copy of the
  # same parse is how two readers of one stdin come to disagree.
  OTEL_WRITER="${_HOOK_ROOT}/hooks/lib/otel_writer.py"
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -f "$OTEL_WRITER" ]; then
    "$_py" "$OTEL_WRITER" start \
      --suite-dir "${CLAUDE_PROJECT_DIR}/.synaptory" \
      --session-id "${session_id}" \
      --agent-id "${agent_id}" \
      --role "${agent_type:-synaptory:unknown}" \
      --backend claude \
      ${SYNAPTORY_AGENT_MODEL:+--model "$SYNAPTORY_AGENT_MODEL"} \
      ${_telemetry_story:+--story-id "$_telemetry_story"} \
      ${prompt_id:+--prompt-id "$prompt_id"} \
      >/dev/null 2>&1 || true
  fi
fi

# --- Mid-session auth gate (fail-closed) ---
# The SessionStart hook gates auth at startup, but a session can lapse during
# a long-running Claude Code session (24h token TTL). Re-check here so an
# expired session that can't be silently renewed BLOCKS the next agent
# dispatch — "nothing runs while expired". `whoami --check` renews via the
# refresh token when possible and never opens a browser. Skippable with
# SYNAPTORY_AUTH_NO_GATE=1 (CI / offline).
#
# There is NO cache. Every dispatch re-runs the check, which is what ADR-011
# asks for and what the block below costs almost nothing to do.
#
# A 300s sentinel used to skip it, justified as "a burst of dispatches costs
# one network round-trip, not one per dispatch". That justification was wrong:
# `whoami --check` goes through `clientForSession`, which loads the keychain
# record and returns. It contacts the control plane ONLY inside the 5 minute
# expiry skew, when it renews. For a healthy session the check is already a
# local read, so the sentinel was saving a process spawn while opening a
# window in which a session that stopped being usable still dispatched agents.
#
# Two rounds of #396 review closed narrower versions of that window (binding
# the sentinel to a UPN, refusing a locally expired session). Removing the
# cache closes the class instead of the instance, and leaves the gate exactly
# as strong as the check it runs.
#
# What this gate does NOT prove, and no version of it ever did: that the
# session is still valid SERVER-side. `whoami --check` never asks the control
# plane about a healthy token, so remote revocation is invisible here and is
# caught at the next authenticated call instead. Do not read this block as a
# revocation check.
if [[ -n "$cli" ]] && [[ -x "$cli" ]] && [[ "${SYNAPTORY_AUTH_NO_GATE:-}" != "1" ]]; then
  if ! "$cli" whoami --check >/dev/null 2>&1; then
    # A stale sentinel from the cached era must not survive as a live file.
    if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
      rm -f "${CLAUDE_PROJECT_DIR}/.synaptory/.orchestrator/.auth-check-ok" 2>/dev/null || true
    fi
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
    printf '{"additionalContext": "BLOCKED — agent delegation depth %s exceeds MAX_AGENT_DEPTH=%s. Refusing to start. If this is legitimate, audit the call chain in .synaptory/.orchestrator/events.jsonl, then raise MAX_AGENT_DEPTH in plugin-claude/hooks/synaptory-inject-protocols.sh."}\n' \
      "$NEW_DEPTH" "$MAX_AGENT_DEPTH"
    exit 1
  fi
  "$_py" "$LOGGER" "$CLAUDE_PROJECT_DIR" subagent_start "depth=$NEW_DEPTH" >/dev/null 2>&1 || true
fi

# --- #30: project risk-checklist injection ---
# A healthcare / security project points `risk_checklist:` in .synaptory.yaml at
# a markdown file of review patterns (PHI in logs/URLs, error_meta redaction,
# MRN in logger.*, …). Inject it into every subagent so the patterns that catch
# runtime-only leaks aren't invisible to the delivery agents meant to prevent
# them. Role is unknowable here, so we inject for all (harmless for
# non-delivery roles).
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
        # Held in its own variable and appended AFTER the compact payload
        # below, so healthcare/security projects' mandatory review patterns
        # land behind the protocol index rather than replacing it.
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

# Load the compact protocol index for additionalContext emission. This is
# the ONLY protocol body injected here — full per-protocol bodies land in
# .synaptory/.protocols/ via the SessionStart skills-fetch, not per dispatch.
# CP-seeded as "hooks/data/compacted-protocols" (api/synaptory_api/seed.py):
# fetched via the CLI (envelope-encrypted at rest, per-UPN watermarked, and
# served offline-friendly by the CLI's local cache after SessionStart's
# `skills sync`), falling back to the on-disk copy so
# `claude --plugin-dir ./plugin` keeps working in dev without a session.
# The distributed build purges hooks/data/*.md (ADR-016), so the on-disk
# copy only exists in source-tree dev — a real install MUST get this via
# the CLI or every subagent dispatch loses protocol content.
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

# Append the project risk checklist (captured above) after the compact
# payload so it reaches additionalContext for healthcare/security projects.
# Empty for projects without a `risk_checklist:` entry.
CONTEXT="${CONTEXT}${RISK_CHECKLIST:-}"

# --- #163: Execution Envelope injection ---
# Render the role-scoped evidence-contract envelope (receipt path + required
# fields, the two verification_commands forms, replay rules, per-role
# evidence obligations) so every synaptory delivery agent sees the exact
# machine-checked contract SubagentStop will enforce. Non-synaptory
# agent_types render to "" and nothing is appended. Best-effort: the envelope
# must never break a dispatch.
#
# #501 — the envelope also states the story's DoD tier and active checks, so
# the contract an agent is GRADED on is the contract it was SHOWN. The
# renderer used to accept `--active-checks`/`--tier` and nothing ever passed
# them, because the host tells SubagentStart the ROLE and not the story.
# `dispatched_receipts.py --resolve-dispatch` is the resolver that closed the
# same gap for SubagentStop (#396): it reads the kernel's dispatch binding,
# falls back to the single in-flight story, and returns the literal
# `?ambiguous` when two stories hold this role at once. Passing that answer
# through verbatim is deliberate — the envelope then NAMES the ambiguity
# instead of rendering a check list for the wrong story.
_ENVELOPE_STORY=""
if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  case "${agent_type:-}" in
    synaptory:*)
      if [ -n "${_marker_link:-}" ]; then
        # Already resolved for the SubagentStop marker above; reuse rather
        # than spawn a second interpreter on every dispatch.
        _ENVELOPE_STORY="${_marker_link%%	*}"
      elif [ -f "$HOOK_LIB_DIR/dispatched_receipts.py" ]; then
        _ENVELOPE_STORY=$("$_py" "$HOOK_LIB_DIR/dispatched_receipts.py" \
          --resolve-dispatch "$CLAUDE_PROJECT_DIR" "${agent_type#synaptory:}" \
          2>/dev/null | head -1 || true)
        _ENVELOPE_STORY="${_ENVELOPE_STORY%%	*}"
      fi
      ;;
  esac
fi
# #512: this used to render to "" on the CLI-unavailable path too, because
# agent_type was parsed inside the telemetry gate and so was empty there. An
# unstamped tree is precisely where SubagentStop still enforces the contract
# locally, so withholding the contract text from the agent while enforcing it
# on the agent was the wrong half to drop. agent_type is parsed above the gate
# now, and the story resolution below is guarded on CLAUDE_PROJECT_DIR rather
# than on a CLI, so both halves reach an unstamped tree.
ENVELOPE=$("$_py" "${_HOOK_ROOT}/hooks/lib/evidence_contract.py" envelope \
  --agent-type "${agent_type:-}" \
  --project-dir "${CLAUDE_PROJECT_DIR:-}" \
  --story-id "${_ENVELOPE_STORY:-}" 2>/dev/null || true)
# The envelope goes to `emit` as PROTECTED context, not concatenated onto
# CONTEXT (#501). additionalContext is capped at 10 KB and the cap is a blind
# head-truncation, so appending the envelope last meant the compacted protocol
# index plus the envelope overran the cap on a real dispatch and the
# envelope's TAIL — its per-role evidence obligations, and now the story's DoD
# contract — was cut off with no signal at all. Reserving the envelope's bytes
# trims the protocol index instead, and says so where the trim happens: the
# index names where the full protocols live, while the envelope is the
# contract SubagentStop mechanically enforces.
printf '%s' "$CONTEXT" | SYNAPTORY_HOOK_LIB="${_HOOK_ROOT}/hooks/lib" \
  SYNAPTORY_ENVELOPE="${ENVELOPE:-}" "$_py" -c "
import sys, os
sys.path.insert(0, os.environ['SYNAPTORY_HOOK_LIB'])
from hook_io import emit
envelope = os.environ.get('SYNAPTORY_ENVELOPE') or ''
emit('SubagentStart', additional_context=sys.stdin.read(),
     protected_context=('\n\n' + envelope + '\n') if envelope else None)
" 2>/dev/null || exit 0

exit 0
