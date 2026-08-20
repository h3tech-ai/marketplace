#!/usr/bin/env bash
# Thin Cursor hook entry: maps stdin/stdout via hooks/lib/cursor_hook.py.
# PLUGIN_ROOT is derived from this script's location so we do not depend on
# ${PLUGIN_ROOT} expansion in hooks.json (sessionStart has not exported it yet).
set -u
_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "${_ROOT}/hooks/lib/resolve-python.sh" ]; then
  # shellcheck source=lib/resolve-python.sh
  source "${_ROOT}/hooks/lib/resolve-python.sh" 2>/dev/null || true
fi
_py="${SYNAPTORY_PYTHON:-python3}"
export PLUGIN_ROOT="${_ROOT}"
export CLAUDE_PLUGIN_ROOT="${_ROOT}"
exec "$_py" "${_ROOT}/hooks/lib/cursor_hook.py" "$@"
