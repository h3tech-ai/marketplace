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
  local f
  for f in "${root}/hooks/lib/cp-url.local" "${root}/hooks/lib/cp-url"; do
    if [[ -f "$f" ]]; then
      _cp_url=$(tr -d '[:space:]' < "$f")
      break
    fi
  done
  if [[ -z "$_cp_url" || "$_cp_url" == "SYNAPTORY_CP_URL_PLACEHOLDER" ]]; then
    if [[ -n "${CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL:-}" ]]; then
      _cp_url="${CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL}"
    fi
  fi
}

_synaptory_cp_url_is_local() {
  local u="${1:-}"
  [[ "$u" == *localhost* || "$u" == *127.* || "$u" == *::1* ]]
}

_synaptory_read_cp_url
