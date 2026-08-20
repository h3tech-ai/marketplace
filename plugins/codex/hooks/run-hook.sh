#!/usr/bin/env bash
set -u

event="${1:-${SYNAPTORY_HOOK_EVENT:-}}"
if [ -z "$event" ]; then
  printf '%s\n' 'synaptory hook event is required' >&2
  exit 1
fi

plugin_root="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -z "$plugin_root" ]; then
  plugin_root="$(cd "$(dirname "$0")/.." && pwd)"
fi

python_bin="${SYNAPTORY_PYTHON:-python3}"
exec "$python_bin" "$plugin_root/hooks/codex_hook.py" "$event"
