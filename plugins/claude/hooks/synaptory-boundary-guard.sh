#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# Hook: PreToolUse  (blocking — exit 0 = allow; emit deny JSON = block)
# Purpose: Promote key iron-law and tracker-boundary rules from prompt text
# to deterministic enforcement at the harness layer.
#
# Active in structured (autonomous) mode only, or when SYNAPTORY_GUARDRAILS=1
# is set. Disable with SYNAPTORY_GUARDRAILS=0.
#
# Guards enforced:
#   G1. Block direct write-tool access to .synaptory/tracker/ — all story/epic
#       operations must go through tracker_cli.py (iron law §1).
#   G2. Block direct writes to pipeline-state.json — transitions must flow
#       through story_pipeline.py / synaptory-pipeline-snapshot.sh.
#   G3. GONE, with the directory it guarded (ADR-035 / `SPD-194`). It denied
#       writes to `.synaptory/sync/`, the per-lane readiness record that the
#       Sync-stage barrier collected a quorum of. The barrier is at Checkpoint
#       now and evaluates the admitted set rather than a quorum of lane
#       records, so nothing writes that directory and nothing reads it. A
#       guard aimed at a path the product no longer creates is not defence in
#       depth: it reads as coverage in this header while enforcing nothing,
#       and the inputs that replaced the readiness record — the sealed
#       declaration, the cut record, the dependency events — are all under a
#       root G4 already covers.
#   G4. Block direct write-tool access to the SPQ Cycle store, on both of its
#       roots: `.synaptory/.orchestrator/spq/` (gitignored, local) and
#       `.synaptory/cycles/` (committed transport). TWO ROOTS, and neither is
#       redundant. The first holds this clone's board and its ledger cache.
#       The second holds the sealed declaration, the dependency events and the
#       append-only cut record — the things OTHER clones read, which is why
#       `open_cycle` refuses to write them when they are gitignored.
#       Everything under both must be DERIVED — by `open_cycle` /
#       `hydrate_cycle` / `cut_work_unit` / the ledger verbs — never
#       hand-authored. Hand-writing a dependency event would let a Work Unit
#       be unblocked by a claim nothing verified; hand-writing a cut would let
#       an agent shrink the admitted set it has to clear at the barrier. G2
#       does not cover this: it matches only the literal string
#       `pipeline-state.json`, so before G4 the entire SPQ store was
#       unguarded.
#       Honest scope: because this guard is active in structured mode only (or
#       with SYNAPTORY_GUARDRAILS=1) and is switched off entirely by
#       SYNAPTORY_GUARDRAILS=0, G4 is defence in depth — NOT a security
#       boundary. The declaration HASH is what actually makes tampering
#       detectable. G4 makes accidental and casual tampering unlikely; the
#       hash is what makes deliberate tampering visible.
#
# PreToolUse stdin (Claude Code):
#   {"tool_name": "…", "tool_input": {…}, "session_id": "…",
#    "cwd": "…", "hook_event_name": "PreToolUse"}

set -u

_HOOK_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=lib/resolve-python.sh
source "${_HOOK_ROOT}/hooks/lib/resolve-python.sh" 2>/dev/null || exit 0
_py="${SYNAPTORY_PYTHON:-python3}"

# Only active inside a synaptory project.
if [ -z "${CLAUDE_PROJECT_DIR:-}" ] || [ ! -d "${CLAUDE_PROJECT_DIR}/.synaptory" ]; then
  exit 0
fi

# Suppression override.
if [ "${SYNAPTORY_GUARDRAILS:-}" = "0" ]; then
  exit 0
fi

# Default: activate only in structured (autonomous) mode.
if [ "${SYNAPTORY_GUARDRAILS:-}" != "1" ]; then
  MODE_OUT=$("$_py" "${_HOOK_ROOT}/hooks/lib/mode_reader.py" "${CLAUDE_PROJECT_DIR}" 2>/dev/null \
    || echo "mode=interactive")
  MODE=$(printf '%s' "$MODE_OUT" | grep -oE 'mode=[a-z_-]+' | cut -d= -f2 | head -1 || echo "interactive")
  if [ "$MODE" != "autonomous" ] && [ "$MODE" != "structured" ]; then
    exit 0
  fi
fi

INPUT="$(cat 2>/dev/null || true)"

# Pass input via stdin and project_dir via env to avoid shell quoting hazards.
GUARD_OUT=$(printf '%s' "$INPUT" | SYNAPTORY_PROJECT_DIR="${CLAUDE_PROJECT_DIR}" \
  "$_py" -c "
import sys, json, os, re

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = (data.get('tool_name') or '').strip()
inp  = data.get('tool_input') or {}
if not isinstance(inp, dict):
    sys.exit(0)

project_dir = os.environ.get('SYNAPTORY_PROJECT_DIR', data.get('cwd') or '').rstrip('/')
WRITE_TOOLS = {'Edit', 'Write', 'Bash', 'NotebookEdit'}

if tool not in WRITE_TOOLS:
    sys.exit(0)

file_path = (inp.get('file_path') or inp.get('notebook_path') or '').strip()

# ---- G1: tracker directory ----
tracker_dir = os.path.join(project_dir, '.synaptory', 'tracker')
if tool == 'Bash':
    cmd = inp.get('command', '')
    if re.search(r'[>|].*\\.synaptory/tracker/', cmd):
        print('deny')
        print('Direct writes to .synaptory/tracker/ are blocked — use tracker_cli.py. Iron law: all story/epic operations must flow through the tracker adapter.')
        sys.exit(0)
elif file_path:
    abs_path = file_path if os.path.isabs(file_path) else os.path.join(project_dir, file_path)
    try:
        real_file    = os.path.realpath(abs_path)
        real_tracker = os.path.realpath(tracker_dir)
        if real_file.startswith(real_tracker + os.sep) or real_file == real_tracker:
            print('deny')
            print('Direct writes to .synaptory/tracker/ are blocked — use tracker_cli.py.')
            sys.exit(0)
    except Exception:
        pass

# ---- G2: pipeline-state.json ----
if tool == 'Bash':
    cmd = inp.get('command', '')
    if 'pipeline-state.json' in cmd and re.search(r'[>|].*pipeline-state', cmd):
        print('deny')
        print('Direct writes to pipeline-state.json are blocked — pipeline transitions must go through advance_kernel.py (the only legal writer) or synaptory-pipeline-snapshot.sh for state restore.')
        sys.exit(0)
elif file_path and 'pipeline-state.json' in file_path:
    print('deny')
    print('Direct writes to pipeline-state.json are blocked — pipeline transitions must go through advance_kernel.py (the only legal writer) or synaptory-pipeline-snapshot.sh for state restore.')
    sys.exit(0)

# ---- G3: retired with .synaptory/sync/ (ADR-035). See the header. ----

# ---- G4: the SPQ store and the committed Cycle transport ----
_SPQ_DENY = (
    'Direct writes to the SPQ Cycle store are blocked — the sealed '
    'declaration, the dependency events, the cut record and Work Unit state '
    'must be DERIVED by spq_state_machine.py (open_cycle / hydrate_cycle / '
    'cut_work_unit) and the ledger verbs, never hand-authored. Hand-writing a '
    'dependency event would let a Work Unit be unblocked by a claim nothing '
    'verified, and hand-writing a cut would shrink the admitted set the '
    'Checkpoint barrier has to clear.'
)
spq_roots = [
    os.path.join(project_dir, '.synaptory', '.orchestrator', 'spq'),
    os.path.join(project_dir, '.synaptory', 'cycles'),
]
if tool == 'Bash':
    cmd = inp.get('command', '')
    if re.search(r'[>|].*\.synaptory/(\.orchestrator/spq|cycles)/', cmd):
        print('deny')
        print(_SPQ_DENY)
        sys.exit(0)
elif file_path:
    abs_path = file_path if os.path.isabs(file_path) else os.path.join(project_dir, file_path)
    try:
        real_file = os.path.realpath(abs_path)
        for root in spq_roots:
            real_root = os.path.realpath(root)
            if real_file.startswith(real_root + os.sep) or real_file == real_root:
                print('deny')
                print(_SPQ_DENY)
                sys.exit(0)
    except Exception:
        pass
" 2>/dev/null || true)

GUARD_DECISION=$(printf '%s' "$GUARD_OUT" | sed -n '1p')
GUARD_REASON=$(printf '%s' "$GUARD_OUT" | sed -n '2p')

if [ "$GUARD_DECISION" = "deny" ]; then
  printf '%s' "$GUARD_REASON" | SYNAPTORY_GUARD_EVENT="PreToolUse" \
    "$_py" -c "
import sys, json, os
reason = sys.stdin.read().strip()
out = {
    'hookSpecificOutput': {
        'permissionDecision': 'deny',
        'permissionDecisionReason': reason,
    }
}
print(json.dumps(out))
" 2>/dev/null || printf '{"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"Blocked by synaptory-boundary-guard"}}\n'
  exit 0
fi

exit 0
