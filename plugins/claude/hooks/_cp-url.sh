#!/usr/bin/env bash
# Resolve this plugin tree's control-plane URL.
#
# Never reads SYNAPTORY_CONTROL_PLANE_URL or SYNAPTORY_CP_ENV. Those overrides
# mixed a local CLI with prod plugins on the same laptop. Local vs prod is
# selected by which plugin + which CLI binary you installed:
#   hooks/lib/cp-url.local  — gitignored; written by `./synaptory deploy local`
#   hooks/lib/cp-url        — build stamp (marketplace / `./synaptory build`)
#
# Sets _cp_url. Source after PLUGIN_ROOT is set.

_synaptory_read_cp_url() {
  local root="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
  _cp_url=""
  [[ -n "$root" ]] || return 0
  local f candidate
  for f in "${root}/hooks/lib/cp-url.local" "${root}/hooks/lib/cp-url"; do
    if [[ -f "$f" ]]; then
      candidate=$(tr -d '[:space:]' < "$f")
      if [[ -n "$candidate" && "$candidate" != "SYNAPTORY_CP_URL_PLACEHOLDER" ]]; then
        _cp_url="$candidate"
        break
      fi
    fi
  done
  if [[ -z "$_cp_url" || "$_cp_url" == "SYNAPTORY_CP_URL_PLACEHOLDER" ]]; then
    if [[ -n "${CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL:-}" ]]; then
      _cp_url="${CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL}"
    fi
  fi
}

_synaptory_cp_url_is_local() {
  local u
  u=$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')
  case "$u" in
    http://localhost|http://localhost:*|http://localhost/*|\
    https://localhost|https://localhost:*|https://localhost/*|\
    http://127.*|https://127.*|http://\[::1\]|http://\[::1\]:*|\
    http://\[::1\]/*|https://\[::1\]|https://\[::1\]:*|https://\[::1\]/*) return 0 ;;
    *) return 1 ;;
  esac
}

_synaptory_read_cp_url
