#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# Hook: SessionStart (runs FIRST, before other hooks)
# Purpose: Authenticate with the Synaptory Control Plane via the CLI.
#
# The control-plane URL is stamped at build time into hooks/lib/cp-url.
# In dev mode (SYNAPTORY_CP_ENV=dev), SYNAPTORY_CONTROL_PLANE_URL env overrides it.

set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=./_plugin-env.sh
source "${PLUGIN_ROOT}/hooks/_plugin-env.sh"

# Cache the plugin root so skill stubs can find the decrypt hook without
# needing ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_SKILL_DIR} expansion in skill
# markdown (Claude Code does not reliably substitute those in !bash inline
# commands). Derive the channel from plugin.json.name so the cache path
# matches the CLI's own channel layout (~/.synaptory-next/ vs ~/.synaptory/).
_plugin_name=$(python3 -c "import json,sys;print(json.load(open('${PLUGIN_ROOT}/.claude-plugin/plugin.json'))['name'])" 2>/dev/null || echo "synaptory")
case "$_plugin_name" in
  synaptory)      _root_cache_dir="$HOME/.synaptory" ;;
  synaptory-*)    _root_cache_dir="$HOME/.${_plugin_name}" ;;
  *)              _root_cache_dir="$HOME/.${_plugin_name}" ;;
esac
mkdir -p "$_root_cache_dir" 2>/dev/null || true
printf '%s' "$PLUGIN_ROOT" > "$_root_cache_dir/.plugin-root" 2>/dev/null || true

# Resolve the control-plane URL: build-stamped value, with dev-only env override.
_cp_url_file="${PLUGIN_ROOT}/hooks/lib/cp-url"
_cp_url=""
if [[ -f "$_cp_url_file" ]]; then
  _cp_url=$(tr -d '[:space:]' < "$_cp_url_file")
fi
# Dev override: only honoured when SYNAPTORY_CP_ENV=dev.
if [[ "${SYNAPTORY_CP_ENV:-}" == "dev" ]] && [[ -n "${SYNAPTORY_CONTROL_PLANE_URL:-}" ]]; then
  _cp_url="${SYNAPTORY_CONTROL_PLANE_URL}"
fi
# userConfig fallback: Claude Code injects CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL
# when the user set a custom URL during plugin onboarding. Takes effect whenever
# the build-stamped cp-url is still a placeholder (custom deployment or fresh source build).
if [[ -z "$_cp_url" ]] || [[ "$_cp_url" == "SYNAPTORY_CP_URL_PLACEHOLDER" ]]; then
  if [[ -n "${CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL:-}" ]]; then
    _cp_url="${CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL}"
  fi
fi
if [[ -z "$_cp_url" ]] || [[ "$_cp_url" == "SYNAPTORY_CP_URL_PLACEHOLDER" ]]; then
  SYNAPTORY_HOOK_LIB="${PLUGIN_ROOT}/hooks/lib" python3 -c "
import sys, os
sys.path.insert(0, os.environ['SYNAPTORY_HOOK_LIB'])
from hook_io import emit
msg = '''## synaptory: Control Plane Not Configured

This plugin build does not have a control-plane URL stamped into it.
Authentication and skill loading will not work.

**To fix:** set the control-plane URL when enabling the plugin — the plugin
settings prompt asks for it (field: control_plane_url). Alternatively, contact
your H3Tech operator to obtain a correctly built plugin distribution.'''
emit('SessionStart', additional_context=msg)
" 2>/dev/null
  exit 1
fi
export SYNAPTORY_CONTROL_PLANE_URL="$_cp_url"

cli=$("${PLUGIN_ROOT}/hooks/_resolve-cli.sh" 2>/dev/null || true)
if [[ -z "$cli" ]] || [[ ! -x "$cli" ]]; then
  SYNAPTORY_HOOK_LIB="${PLUGIN_ROOT}/hooks/lib" python3 -c "
import sys, os
sys.path.insert(0, os.environ['SYNAPTORY_HOOK_LIB'])
from hook_io import emit
msg = '''## synaptory: CLI Binary Not Found

The Synaptory CLI binary could not be found. Skills cannot be loaded.

Reinstall the plugin or rebuild the CLI binary:
  ./synaptory build

Then restart your Claude Code session.'''
emit('SessionStart', additional_context=msg)
" 2>/dev/null
  exit 1
fi

# Enforce cli_min_version from plugin.json (ADR-018 §C.1). The plugin
# was built against a specific CLI; older CLIs may miss subcommands or
# wire-protocol bits this plugin requires.
_plugin_json="${PLUGIN_ROOT}/.claude-plugin/plugin.json"
_min_cli=$(sed -n 's/.*"cli_min_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$_plugin_json" 2>/dev/null | head -n1)
if [[ -n "$_min_cli" ]]; then
  _cli_ver=$("$cli" --version 2>/dev/null | sed -n 's/.*version \([0-9][^[:space:]]*\).*/\1/p' | head -n1)
  if [[ -z "$_cli_ver" ]]; then
    _cli_ver=$("$cli" version 2>/dev/null | head -n1 | tr -d '[:space:]')
  fi
  if [[ -n "$_cli_ver" ]]; then
    # Pure-bash semver compare: returns 0 if $1 >= $2, else 1.
    _semver_ge() {
      local IFS=. a b
      read -r -a a <<< "$1"
      read -r -a b <<< "$2"
      for i in 0 1 2; do
        local av="${a[$i]:-0}" bv="${b[$i]:-0}"
        # Strip non-numeric suffixes so e.g. 2.5.27-dev compares as 27.
        av="${av%%[!0-9]*}"; bv="${bv%%[!0-9]*}"
        av="${av:-0}"; bv="${bv:-0}"
        if (( av > bv )); then return 0; fi
        if (( av < bv )); then return 1; fi
      done
      return 0
    }
    if ! _semver_ge "$_cli_ver" "$_min_cli"; then
      SYNAPTORY_HOOK_LIB="${PLUGIN_ROOT}/hooks/lib" python3 -c "
import sys, os
sys.path.insert(0, os.environ['SYNAPTORY_HOOK_LIB'])
from hook_io import emit
msg = '''## synaptory: CLI Too Old

This plugin requires synaptory CLI ≥ ${_min_cli}, but the installed CLI is ${_cli_ver}.
Skills, login, and telemetry will not work until the CLI is updated.

Update with:
  synaptory update           # auto-fetches the latest version

Or reinstall:
  curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash   (macOS / Linux)
  iwr -useb https://synaptory.h3t.co/cli/install.ps1 | iex    (Windows)

Then start a new Claude Code session.'''
emit('SessionStart', additional_context=msg)
" 2>/dev/null
      exit 1
    fi
  fi
fi
unset _plugin_json _min_cli _cli_ver

# ADR-018 §C.5: proactively check the CLI mirror for a newer plugin / CLI
# and surface an update banner. The old §C.4 path compared the LOCAL
# marketplace cache, which Claude Code only refreshes on the very
# `/plugin marketplace update` command the nag exists to prompt — so it
# never fired proactively. This instead asks the CLI to fetch the
# authoritative `latest.json` (`synaptory version --check --json`) and
# render plugin + CLI freshness. Fully failure-tolerant, throttled, and
# bounded so a slow/unreachable mirror can never stall or noise a session.
# Update policy (#96): precedence is SYNAPTORY_UPDATE_POLICY env var > the
# CP-provided fleet default (admin_locked.update_policy in the cached signed
# policy) > built-in "prompt". The CP value lets an org flip the whole fleet to
# `auto` without setting an env var on every laptop; the env var stays an
# escape hatch. `config value` reads the local policy cache offline (no network
# on the session-start path) and prints --default when it's absent/stale.
_policy="${SYNAPTORY_UPDATE_POLICY:-}"
if [[ -z "$_policy" ]]; then
  _policy=$("$cli" config value admin_locked.update_policy --default prompt 2>/dev/null || echo prompt)
fi
case "$_policy" in prompt|auto|off) ;; *) _policy="prompt" ;; esac   # sanitise unknowns
_now=$(date +%s 2>/dev/null || echo 0)
_stamp_dir="${HOME}/.synaptory"
_stamp="${_stamp_dir}/.version-check-stamp"
_interval="${SYNAPTORY_VERSION_CHECK_INTERVAL:-21600}"   # 6h between checks
_last=0; [[ -r "$_stamp" ]] && _last=$(cat "$_stamp" 2>/dev/null || echo 0)
if [[ "$_policy" != "off" && "$_now" -gt 0 ]] && (( _now - _last >= _interval )); then
  mkdir -p "$_stamp_dir" 2>/dev/null || true
  # A1 (#93): stamp AFTER the check, not before. Writing the stamp up-front
  # meant a timed-out or failed fetch silenced the version check for the full
  # interval (~6h) with no retry — a release could go unnoticed for a workday.
  # Bound the network call (the CLI's own HTTP timeout is 30s — too long for
  # session start). Prefer coreutils `timeout`/`gtimeout` when present.
  if command -v timeout >/dev/null 2>&1; then
    _vc=$(timeout 6 "$cli" version --check --json 2>/dev/null)
  elif command -v gtimeout >/dev/null 2>&1; then
    _vc=$(gtimeout 6 "$cli" version --check --json 2>/dev/null)
  else
    _vc=$("$cli" version --check --json 2>/dev/null)
  fi
  # A1: only a successful check (checked:true) earns the full-interval stamp.
  # Anything else — empty output, or a reachable-but-errored `checked:false`
  # — writes a short backoff so the next session retries in ~10 min instead
  # of being silenced for the full interval. Flatten whitespace first so the
  # match holds for both compact (Go) and pretty-printed JSON.
  _vcflat="${_vc//[[:space:]]/}"
  if [[ "$_vcflat" == *'"checked":true'* ]]; then
    echo "$_now" > "$_stamp" 2>/dev/null || true
  else
    _bk=$(( _now - _interval + 600 )); (( _bk < 0 )) && _bk=0
    echo "$_bk" > "$_stamp" 2>/dev/null || true
  fi
  if [[ -n "$_vc" ]]; then
    VC_JSON="$_vc" POLICY="$_policy" CLI_BIN="$cli" SYNAPTORY_HOOK_LIB="${PLUGIN_ROOT}/hooks/lib" python3 <<'PY' 2>/dev/null
import json, os, subprocess, sys
sys.path.insert(0, os.environ["SYNAPTORY_HOOK_LIB"])
from hook_io import emit
try:
    st = json.loads(os.environ["VC_JSON"])
except Exception:
    raise SystemExit(0)
if not st.get("checked"):
    raise SystemExit(0)   # mirror unreachable / no data → stay silent
policy = os.environ.get("POLICY", "prompt")
cli = st.get("cli", {}) or {}
plugin = st.get("plugin", {}) or {}
latest = st.get("latest", "")
critical = bool(st.get("critical"))
notes = st.get("notes_url", "")
sections = []

cli_needs = bool(cli.get("update_available") or cli.get("below_min"))
if cli_needs:
    # A3 (#95): a `critical` release forces the background CLI self-update even
    # under the default `prompt` policy — a security/forced update shouldn't
    # wait on each user to run `synaptory update`. (`off` never reaches here:
    # the whole block is gated on policy != off upstream.) `auto` always
    # self-updates as before.
    force_auto = policy == "auto" or critical
    if force_auto:
        # Detached, non-blocking self-update (atomic binary replace; the
        # running process keeps the old inode). Never waits on session start.
        try:
            subprocess.Popen(
                [os.environ["CLI_BIN"], "update", "--yes"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            why = "critical — auto-update forced" if critical and policy != "auto" else "auto-update started"
            sections.append(f"- **CLI** {cli.get('installed','')} → **{latest}**: {why} in the background.")
        except Exception:
            sections.append(f"- **CLI** {cli.get('installed','')} → **{latest}**: run `synaptory update`.")
    else:
        sections.append(f"- **CLI** {cli.get('installed','')} → **{latest}**: run `synaptory update`.")

# A2 (#94): plugin updates can't be self-applied (a running session doesn't
# hot-swap plugins), but Claude Code CAN auto-update the marketplace at
# startup once it's enabled — so steer the user to enable it ONCE rather than
# nag the manual command every release. Falls back to the manual update inline.
if plugin.get("update_available"):
    sections.append(
        f"- **Plugin** {plugin.get('installed','')} → **{latest}**. To receive "
        f"this and every future release automatically, enable auto-update for "
        f"the marketplace **once**: `/plugin` → Marketplaces → h3tech-ai → turn "
        f"on Auto-update. (Update now instead: `/plugin marketplace update "
        f"h3tech-ai`.) Either way, reload afterward — a running session won't "
        f"hot-swap the plugin (Cmd-Shift-P → \"Reload Window\", or restart `claude`)."
    )

if sections:
    head = "## ⚠ synaptory CRITICAL update available" if critical else "## synaptory update available"
    body = head + "\n\n" + "\n".join(sections)
    if notes:
        body += f"\n\nRelease notes: {notes}"
    if critical:
        body += "\n\nThis release is flagged **critical** — update before continuing."
    emit("SessionStart", additional_context=body)
PY
  fi
fi
unset _policy _now _stamp_dir _stamp _interval _last _vc _vcflat _bk

# Auth gate (fail-closed). ADR-008: a single user-scoped session works across
# every project the user is a member of. `whoami --check` validates the cached
# session locally — silently renewing an expired-but-refreshable token via the
# refresh token — and exits non-zero only when NO valid session can be
# established. It never opens a browser, so it's safe to run synchronously in
# the hook.
if "$cli" whoami --check >/dev/null 2>&1; then
  exit 0   # authed (possibly just renewed) → proceed
fi

# No valid session and it could not be silently renewed. Per the auth policy
# this is a HARD BLOCK (exit 1) — an expired/missing session must not let the
# Synaptory workflow run unauthenticated. We also AUTO-LAUNCH the browser
# sign-in (detached so it can't stall session start), throttled so repeated
# blocked starts don't storm browser tabs. The CLI's local OAuth callback
# server survives the hook exit via nohup/setsid.
_auth_stamp="${HOME}/.synaptory/.login-launch-stamp"
_now=$(date +%s 2>/dev/null || echo 0)
_last=0; [[ -r "$_auth_stamp" ]] && _last=$(cat "$_auth_stamp" 2>/dev/null || echo 0)
_launched=0
if [[ "${SYNAPTORY_AUTH_NO_AUTOLAUNCH:-}" != "1" ]] && [[ "$_now" -gt 0 ]] && (( _now - _last >= 90 )); then
  mkdir -p "${HOME}/.synaptory" 2>/dev/null || true
  echo "$_now" > "$_auth_stamp" 2>/dev/null || true
  if command -v setsid >/dev/null 2>&1; then
    setsid "$cli" login >/dev/null 2>&1 < /dev/null &
  elif command -v nohup >/dev/null 2>&1; then
    nohup "$cli" login >/dev/null 2>&1 < /dev/null &
  else
    ( "$cli" login >/dev/null 2>&1 < /dev/null & )
  fi
  _launched=1
fi

CLI_BIN="$cli" PLUGIN_ROOT="$PLUGIN_ROOT" LAUNCHED="$_launched" SYNAPTORY_HOOK_LIB="${PLUGIN_ROOT}/hooks/lib" python3 <<'PY' 2>/dev/null
import json, os, sys
sys.path.insert(0, os.environ["SYNAPTORY_HOOK_LIB"])
from hook_io import emit
plugin_root = os.environ["PLUGIN_ROOT"]
launched = os.environ.get("LAUNCHED") == "1"
opened = ("A browser window is opening for Entra sign-in — complete it, then start a **new Claude Code session**."
          if launched else
          "Sign in to continue:")
msg = f"""## 🔒 synaptory — session expired, sign-in required

Synaptory is blocked until you re-authenticate (your session lapsed or was never established).

{opened}

```bash
synaptory login
```

**If the CLI isn't installed yet:**
```bash
bash "{plugin_root}/hooks/install-cli.sh"
```
Open a new terminal so `$PATH` picks up `~/.local/bin`, then run `synaptory login`.

After sign-in, **start a new Claude Code session**. (Set `SYNAPTORY_AUTH_NO_AUTOLAUNCH=1` to stop the browser from opening automatically.)"""
emit("SessionStart", additional_context=msg)
PY
unset _auth_stamp _now _last _launched
exit 1
