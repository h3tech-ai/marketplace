#!/usr/bin/env bash

# --- synaptory: resolve Python interpreter (magic-number safe) ---
# shellcheck source=lib/resolve-python.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/resolve-python.sh"
: "${SYNAPTORY_PYTHON:?synaptory requires a working Python 3 interpreter (set SYNAPTORY_PYTHON to override)}"
# -------------------------------------------------------------
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
# Hook: SessionStart
# Purpose: Flush the telemetry outbox from the previous session, record the
#          start of this one, then inject pipeline state into session context
#          if a .synaptory/ workspace exists in the current project.

_HOOK_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=./_plugin-env.sh
PLUGIN_ROOT="$_HOOK_ROOT" source "${_HOOK_ROOT}/hooks/_plugin-env.sh"
# shellcheck source=lib/resolve-python.sh
source "${_HOOK_ROOT}/hooks/lib/resolve-python.sh"
_py="${SYNAPTORY_PYTHON:-python3}"
# shellcheck source=lib/receipt-paths.sh
source "${_HOOK_ROOT}/hooks/lib/receipt-paths.sh"

cli=$("${_HOOK_ROOT}/hooks/_resolve-cli.sh" 2>/dev/null || true)
CLI_STATE_DIR="${HOME}/.synaptory"
CLI_COMMAND="synaptory"
if [[ -n "$cli" ]]; then
  CLI_COMMAND=$(basename "$cli")
  case "$CLI_COMMAND" in
    synaptory-local) CLI_STATE_DIR="${HOME}/.synaptory-local" ;;
    synaptory-*)     CLI_STATE_DIR="${HOME}/.${CLI_COMMAND}" ;;
  esac
fi
if [[ -n "$cli" ]] && [[ -x "$cli" ]]; then
  # SAD Gap 4 fix: let the CLI resolve project_id from its own config chain
  # (.synaptory.yaml → SYNAPTORY_PROJECT_ID). Previously this hook passed
  # basename(CLAUDE_PROJECT_DIR), which diverged from the CLI's auth/session
  # keying and caused API body-project mismatches.
  # Outbox flush is detached from the hook: ships sequentially one POST per
  # event, so a backlog of N events takes N round-trips. Running it
  # synchronously here exposed it to Claude Code's hook timeout — partial
  # drains kept the queue growing across sessions, and `2>/dev/null` hid the
  # truncation. Detach via setsid/nohup with a rotating log so the hook
  # returns immediately and operators have a place to look when telemetry
  # falls behind.
  _outbox_log="${CLI_STATE_DIR}/.outbox/flush.log"
  mkdir -p "$(dirname "$_outbox_log")" 2>/dev/null || true
  if command -v setsid >/dev/null 2>&1; then
    setsid "$cli" outbox flush >>"$_outbox_log" 2>&1 </dev/null &
  else
    nohup "$cli" outbox flush >>"$_outbox_log" 2>&1 </dev/null &
  fi
  disown 2>/dev/null || true
  "$cli" telemetry session-start >/dev/null 2>&1 || true

  # Re-ship any sealed SPQ manifest that never reached the control plane
  # (issue #334 G8). Emission used to be one attempt inside a bare try/except,
  # so a transient failure left `cycle_manifests` empty and /cycles with no
  # producer at all. The sweep is keyed on manifest_hash and skips what this
  # clone already delivered, so the common case is two directory listings.
  # Detached and logged next to the flush, for the same reason: it makes N
  # round-trips and must not sit inside the hook timeout.
  if [ -d "${CLAUDE_PROJECT_DIR}/.synaptory" ] && [ -n "${_py:-}" ]; then
    _manifest_log="${CLI_STATE_DIR}/.outbox/manifests.log"
    if command -v setsid >/dev/null 2>&1; then
      setsid "$_py" "${_HOOK_ROOT}/hooks/lib/manifest_emitter.py" \
        "$CLAUDE_PROJECT_DIR" >>"$_manifest_log" 2>&1 </dev/null &
    else
      nohup "$_py" "${_HOOK_ROOT}/hooks/lib/manifest_emitter.py" \
        "$CLAUDE_PROJECT_DIR" >>"$_manifest_log" 2>&1 </dev/null &
    fi
    disown 2>/dev/null || true
  fi

  # Re-send gate events a previous session could not deliver (#331 G10). Unlike
  # a sealed manifest, a gate event has no durable on-disk original to sweep,
  # so this one IS a spool. Same failure it recovers from: `no project_id
  # resolved` returns before the CLI reaches its own outbox, so one workstream
  # emitted zero gate events for a whole Cycle while the log promised a retry.
  if [ -d "${CLAUDE_PROJECT_DIR}/.synaptory" ] && [ -n "${_py:-}" ]; then
    (
      "$_py" -c "
import sys
sys.path.insert(0, '${_HOOK_ROOT}/hooks/lib')
import gate_emitter
gate_emitter.retry_pending('${CLAUDE_PROJECT_DIR}')
" >/dev/null 2>&1 || true
    ) &
    disown 2>/dev/null || true
  fi
fi

# Probe the outbox so we can surface a visible warning when telemetry is
# backed up. Cheap: counts queued .json files. The queue is partitioned into
# per-project buckets (issue #117), so scan one level deeper (maxdepth 2) and
# exclude quarantined events, which are not auto-re-flushed.
#
# Three measures, not one (issue #320). The count-only warning below fired at
# >500 events and therefore never fired at all for the failure that mattered:
# 76 project directories of permanently undeliverable telemetry, a handful of
# events each. Age says the queue is not waiting for the network, and the number
# of distinct project buckets says the CLI is addressed at a control plane that
# does not know these projects. Age comes from the `<unix_nanos>-<kind>-<pid>`
# filename Queue writes, so no JSON parsing and no non-portable `find` flags.
OUTBOX_DIR="${CLI_STATE_DIR}/.outbox"
OUTBOX_DEPTH=0
OUTBOX_BUCKETS=0
OUTBOX_OLDEST_DAYS=0
OUTBOX_QUARANTINED=0
if [ -d "$OUTBOX_DIR" ]; then
  OUTBOX_DEPTH=$(find "$OUTBOX_DIR" -maxdepth 2 -name '*.json' -not -path '*/.quarantine/*' 2>/dev/null | wc -l | tr -d ' ')
  OUTBOX_BUCKETS=$(find "$OUTBOX_DIR" -maxdepth 2 -name '*.json' -not -path '*/.quarantine/*' -exec dirname {} \; 2>/dev/null | sort -u | wc -l | tr -d ' ')
  OUTBOX_QUARANTINED=$(find "$OUTBOX_DIR" -maxdepth 3 -name '*.json' -path '*/.quarantine/*' 2>/dev/null | wc -l | tr -d ' ')
  _oldest_ns=$(find "$OUTBOX_DIR" -maxdepth 2 -name '*.json' -not -path '*/.quarantine/*' 2>/dev/null \
    | sed 's#.*/##; s#-.*##' | grep -E '^[0-9]+$' | sort -n | head -1)
  if [ -n "${_oldest_ns:-}" ]; then
    OUTBOX_OLDEST_DAYS=$(( ( $(date +%s) - _oldest_ns / 1000000000 ) / 86400 ))
    if [ "$OUTBOX_OLDEST_DAYS" -lt 0 ]; then
      OUTBOX_OLDEST_DAYS=0
    fi
  fi
fi

# >>> TRANSITIONAL: hiro-crew -> Synaptory legacy detection (remove in vNEXT) >>>
# A downstream repo still on the legacy hiro-crew layout won't be recognized by
# Synaptory. Nudge the operator to run `synaptory migrate` once, then stop.
if [ -f "${CLAUDE_PROJECT_DIR}/.hiro-crew.yaml" ] && [ ! -f "${CLAUDE_PROJECT_DIR}/.synaptory.yaml" ]; then
  LEGACY_MSG="# Legacy hiro-crew project detected

This repo still uses the legacy \`hiro-crew\` layout (\`.hiro-crew.yaml\`, \`.hiro-crew/\`). Synaptory will not track it until you migrate.

**Run once from the project root** (takes a backup automatically; \`--dry-run\` previews):
\`\`\`
synaptory migrate
\`\`\`
Then, in Claude Code:
\`\`\`
/plugin marketplace add https://github.com/h3tech-ai/marketplace
/plugin install synaptory@h3tech-ai
\`\`\`"
  printf '%s' "$LEGACY_MSG" | SYNAPTORY_HOOK_LIB="${_HOOK_ROOT}/hooks/lib" "$_py" -c "
import sys, os
sys.path.insert(0, os.environ['SYNAPTORY_HOOK_LIB'])
from hook_io import emit
emit('SessionStart', additional_context=sys.stdin.read())
" 2>/dev/null || echo "{\"additionalContext\": \"Legacy hiro-crew project detected — run 'synaptory migrate'.\"}"
  exit 0
fi
# <<< TRANSITIONAL <<<

SUITE_DIR="${CLAUDE_PROJECT_DIR}/.synaptory"

# Only fire if the suite directory exists in the current project
if [ ! -d "$SUITE_DIR" ]; then
  exit 0
fi

# Count receipts across every layout (flat, multi-spec, SPQ). An SPQ
# project reported 0 here no matter how much the Cycle had produced (#336),
# so a resumed session was told there was no evidence on disk.
RECEIPT_COUNT=$(synaptory_receipt_count "$SUITE_DIR/.orchestrator")

# Read last session snapshot (first 30 lines)
LAST_SESSION=""
if [ -f "$SUITE_DIR/.orchestrator/last-session.md" ]; then
  LAST_SESSION=$(head -30 "$SUITE_DIR/.orchestrator/last-session.md" 2>/dev/null)
fi

# Read project name from settings
PROJECT_NAME=""
if [ -f "$SUITE_DIR/.orchestrator/settings.md" ]; then
  PROJECT_NAME=$(grep -m1 "^Project:" "$SUITE_DIR/.orchestrator/settings.md" 2>/dev/null | sed 's/^Project: *//')
fi

# Read sprint state from pipeline-state.json
SPRINT_CONTEXT=""
STATE_FILE="$SUITE_DIR/.orchestrator/pipeline-state.json"

if [ -f "$STATE_FILE" ] && [ -s "$STATE_FILE" ]; then
  SPRINT_CONTEXT=$("$_py" -c "
import json, sys
def render_substate(mode, sub, prefix=''):
    lines = []
    lifecycle = sub.get('lifecycle_state', 'INCEPTION')
    lines.append(f'{prefix}Lifecycle State: {lifecycle}')
    if mode == 'scrum':
        current = sub.get('current_sprint', 0)
        goal = sub.get('sprint_goal', '')
        completed = len(sub.get('sprints_completed', []))
        stories = sub.get('current_stories', [])
        done = sum(1 for s in stories if s.get('state') == 'done')
        lines.append(f'{prefix}Current Sprint: {current}')
        if goal: lines.append(f'{prefix}Sprint Goal: {goal}')
        lines.append(f'{prefix}Completed Sprints: {completed}')
        if stories: lines.append(f'{prefix}Stories: {done}/{len(stories)} done')
    elif mode == 'kanban':
        total = sub.get('cumulative_ticket_number', 0)
        current = len(sub.get('current_stories', []))
        completed = len(sub.get('tickets_completed', []))
        lines.append(f'{prefix}Cumulative Tickets: {total}')
        lines.append(f'{prefix}On Board: {current}')
        lines.append(f'{prefix}Completed: {completed}')
    return lines

try:
    state = json.load(open('$STATE_FILE'))
    version = state.get('version')
    mode = state.get('build_mode', 'scrum')
    out = []
    if version == '3.0' and isinstance(state.get('specs'), dict):
        # Multi-spec rollup (design §5.2 / docs/multi-spec-design.md)
        out.append(f'Build Mode: {mode}')
        active = state.get('active_spec', '')
        out.append(f'Active Spec: {active or \"<none>\"}')
        for sid, sub in state['specs'].items():
            out.append('')
            out.append(f'### Spec: {sid}' + ('  (active)' if sid == active else ''))
            out.extend(render_substate(mode, sub, prefix='  '))
    elif version == '2.0' and 'lifecycle_state' in state:
        out.append(f'Build Mode: {mode}')
        out.extend(render_substate(mode, state))
    print('\n'.join(out))
except Exception:
    pass
" 2>/dev/null || echo "")
fi

# Build context message
# Thresholds mirror outbox.StaleAge / StaleBucketCount / StaleEventCount in
# cli/internal/outbox/outbox.go. Keep them in step.
OUTBOX_WARNING=""
if [ "$OUTBOX_DEPTH" -gt 0 ] \
   && { [ "$OUTBOX_DEPTH" -gt 500 ] || [ "$OUTBOX_OLDEST_DAYS" -ge 7 ] || [ "$OUTBOX_BUCKETS" -ge 5 ]; }; then
  OUTBOX_WARNING="
**Telemetry backlog: ${OUTBOX_DEPTH} events queued locally across
${OUTBOX_BUCKETS} project(s), oldest ${OUTBOX_OLDEST_DAYS}d.** Control-plane
dashboards (overview, audit, cost) will lag until the outbox drains. A
background flush has been started; if the queue is not shrinking after a
few minutes, run \`${CLI_COMMAND} outbox flush\` in a terminal and check
\`${CLI_STATE_DIR}/.outbox/flush.log\` for errors.

A queue this old, or spread across this many projects, usually means the
events are addressed to a control plane that does not know those projects,
so they can never ship. Confirm which one this CLI talks to with
\`${CLI_COMMAND} status\`, then retire the dead entries with
\`${CLI_COMMAND} outbox quarantine --older-than 30d --dry-run\`."
fi
# Quarantine is terminal: nothing removes these automatically, so an operator
# who chooses to keep them would otherwise read this line at every SessionStart
# forever. Mention it only once the pile is big enough to be worth acting on,
# and name the verb that makes it go away.
if [ "$OUTBOX_QUARANTINED" -ge 50 ]; then
  OUTBOX_WARNING="${OUTBOX_WARNING}
**${OUTBOX_QUARANTINED} telemetry event(s) were refused** by the control plane
and are held in quarantine (never retried). Inspect with
\`${CLI_COMMAND} outbox list\`, or dispose of them with
\`${CLI_COMMAND} outbox prune --older-than 90d --dry-run\`."
fi

CONTEXT="# Synaptory Pipeline Project Detected

Project: ${PROJECT_NAME:-$(basename "$CLAUDE_PROJECT_DIR")}
Workspace: ${SUITE_DIR}/
Receipts: ${RECEIPT_COUNT} agent completions recorded
${OUTBOX_WARNING}

${SPRINT_CONTEXT:+Sprint State:
${SPRINT_CONTEXT}
}
**IMPORTANT — Before starting work, ask the user how they'd like to proceed using AskUserQuestion:**

Question: \"This project was built with the synaptory pipeline. How would you like to work today?\"
Header: \"Synaptory Pipeline Project\"
Options:
  1. \"Use synaptory (Recommended)\" — \"Route changes through specialized agents — architecture, security, and test baselines stay intact.\"
  2. \"Work directly without the plugin\" — \"Make changes freely. You can always invoke /synaptory later if needed.\"
  3. \"Chat about this\" — \"Let's discuss what I'm planning and figure out the best approach together.\"

If the user chooses option 1: invoke /synaptory for their request.
If the user chooses option 2: proceed normally. Respect the choice fully — no further reminders this session.
If the user chooses option 3: read and follow \${CLAUDE_PLUGIN_ROOT}/agents/research-advisor/SKILL.md."

if [ -n "$LAST_SESSION" ]; then
  CONTEXT="${CONTEXT}

## Last Session State
${LAST_SESSION}"
fi

# Check for context packages (brownfield knowledge layer)
CONTEXT_PKG_DIR="$SUITE_DIR/.orchestrator/context-packages"
if [ -d "$CONTEXT_PKG_DIR" ]; then
  PKG_COUNT=$(find "$CONTEXT_PKG_DIR" -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$PKG_COUNT" -gt 0 ]; then
    PKG_SUMMARY="## Context Packages (Brownfield Knowledge)
- Location: ${CONTEXT_PKG_DIR}/
- Packages: ${PKG_COUNT} context packages loaded"

    if [ -f "$CONTEXT_PKG_DIR/health-assessment.md" ]; then
      HEALTH=$(head -20 "$CONTEXT_PKG_DIR/health-assessment.md" 2>/dev/null)
      PKG_SUMMARY="${PKG_SUMMARY}

### Health Assessment
${HEALTH}"
    fi

    if [ -f "$CONTEXT_PKG_DIR/business-rules-inventory.md" ]; then
      BR_COUNT=$(grep -c "^### Rule:" "$CONTEXT_PKG_DIR/business-rules-inventory.md" 2>/dev/null || echo "0")
      PKG_SUMMARY="${PKG_SUMMARY}
- Business rules: ${BR_COUNT} rules documented"
    fi

    if [ -f "$CONTEXT_PKG_DIR/risk-register.md" ]; then
      RISK_COUNT=$(grep -c "^### RISK-" "$CONTEXT_PKG_DIR/risk-register.md" 2>/dev/null || echo "0")
      PKG_SUMMARY="${PKG_SUMMARY}
- Risk items: ${RISK_COUNT} risks identified"
    fi

    CONTEXT="${CONTEXT}

${PKG_SUMMARY}

**Note:** Context packages from a previous /synaptory run are available. Agents will read relevant packages automatically."
  fi
fi

# Inject path-scoped rules (from plugin dir — rules ship as plaintext in dist now)
RULES_DIR="${CLAUDE_PLUGIN_ROOT}/rules"
for rule in synaptory-freshness.md synaptory-receipt-protocol.md synaptory-guard.md; do
  RULE_FILE="$RULES_DIR/$rule"
  if [ -f "$RULE_FILE" ]; then
    RULE_CONTENT=$(cat "$RULE_FILE" 2>/dev/null)
    if [ -n "$RULE_CONTENT" ]; then
      CONTEXT="${CONTEXT}

---
${RULE_CONTENT}"
    fi
  fi
done

# Derive a session title from project + sprint/kanban state.
SESSION_TITLE_SUFFIX=""
if [ -f "$STATE_FILE" ] && [ -s "$STATE_FILE" ]; then
  SESSION_TITLE_SUFFIX=$("$_py" -c "
import json, sys
try:
    state = json.load(open('$STATE_FILE'))
    mode = state.get('build_mode', 'scrum')
    version = state.get('version', '2.0')
    def sprint_label(sub):
        lc = sub.get('lifecycle_state', 'INCEPTION')
        if mode == 'scrum':
            n = sub.get('current_sprint', 0)
            return f'Sprint {n}' if n else lc.title()
        return lc.replace('_', ' ').title()
    if version == '3.0' and isinstance(state.get('specs'), dict):
        active = state.get('active_spec', '')
        sub = state['specs'].get(active, {}) if active else {}
        lbl = sprint_label(sub) if sub else ''
        print((' · ' + active + (' · ' + lbl if lbl else '')) if active else '')
    else:
        print(' · ' + sprint_label(state))
except Exception:
    print('')
" 2>/dev/null || echo "")
fi
SESSION_TITLE="Synaptory · ${PROJECT_NAME:-$(basename "${CLAUDE_PROJECT_DIR}")}${SESSION_TITLE_SUFFIX}"

# watchPaths: react when the project config or pipeline state is edited out-of-band.
WATCH_PATHS='[".synaptory.yaml", ".synaptory/.orchestrator/pipeline-state.json"]'

printf '%s' "$CONTEXT" | SYNAPTORY_HOOK_LIB="${_HOOK_ROOT}/hooks/lib" SYNAPTORY_SESSION_TITLE="$SESSION_TITLE" SYNAPTORY_WATCH_PATHS="$WATCH_PATHS" "$_py" -c "
import sys, os, json
sys.path.insert(0, os.environ['SYNAPTORY_HOOK_LIB'])
from hook_io import emit
content = sys.stdin.read()
title = os.environ.get('SYNAPTORY_SESSION_TITLE', '')
watch_raw = os.environ.get('SYNAPTORY_WATCH_PATHS', '[]')
try:
    watch = json.loads(watch_raw)
except Exception:
    watch = []
# OSC 0 window title so the terminal tab shows the project.
terminal_seq = f'\033]0;{title}\007' if title else None
emit(
    'SessionStart',
    additional_context=content,
    session_title=title or None,
    watch_paths=watch or None,
    terminal_sequence=terminal_seq,
)
" 2>/dev/null || echo "{\"additionalContext\": \"synaptory workspace detected. ${RECEIPT_COUNT} receipts found.\"}"

exit 0
