#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# Purpose: Runtime skill resolver for stub SKILL.md files.
# Called via: !`bash "${CLAUDE_PLUGIN_ROOT}/hooks/synaptory-decrypt-skill.sh" "<skill-name>"`
#
# Skill bodies are served by the CLI from its local cache (populated by
# Synaptory-skills-fetch.sh on SessionStart). A live fetch is attempted on cache miss.
# The control-plane URL is stamped at build time into hooks/lib/cp-url.

set -euo pipefail

SKILL_NAME="${1:-}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=./_plugin-env.sh
source "${PLUGIN_ROOT}/hooks/_plugin-env.sh"

if [[ -z "$SKILL_NAME" ]]; then
  echo "Error: No skill name provided to synaptory-decrypt-skill.sh" >&2
  exit 1
fi

# Resolve the control-plane URL.
_cp_url_file="${PLUGIN_ROOT}/hooks/lib/cp-url"
_cp_url=""
if [[ -f "$_cp_url_file" ]]; then
  _cp_url=$(tr -d '[:space:]' < "$_cp_url_file")
fi
if [[ "${SYNAPTORY_CP_ENV:-}" == "dev" ]] && [[ -n "${SYNAPTORY_CONTROL_PLANE_URL:-}" ]]; then
  _cp_url="${SYNAPTORY_CONTROL_PLANE_URL}"
fi
if [[ -z "$_cp_url" ]] || [[ "$_cp_url" == "SYNAPTORY_CP_URL_PLACEHOLDER" ]]; then
  if [[ -n "${CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL:-}" ]]; then
    _cp_url="${CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL}"
  fi
fi
if [[ -z "$_cp_url" ]] || [[ "$_cp_url" == "SYNAPTORY_CP_URL_PLACEHOLDER" ]]; then
  cat <<'MSG'

## synaptory: Control Plane Not Configured

This plugin build does not have a control-plane URL stamped into it.
Skills cannot be loaded.

To fix: set the control-plane URL in plugin settings (field: control_plane_url),
or contact your H3Tech operator for a correctly built distribution.

MSG
  exit 1
fi
export SYNAPTORY_CONTROL_PLANE_URL="$_cp_url"

cli=$("${PLUGIN_ROOT}/hooks/_resolve-cli.sh" 2>/dev/null || true)
if [[ -z "$cli" ]] || [[ ! -x "$cli" ]]; then
  echo "synaptory: CLI binary not found; cannot resolve skill '${SKILL_NAME}'." >&2
  exit 1
fi

# Try the CLI cache first. Capture stderr so we can distinguish the two
# interesting failure modes: (a) no session — show onboarding; (b) other —
# fall back to plaintext if present, else show a generic error.
fetch_err=$(mktemp)
if body=$("$cli" skills get "$SKILL_NAME" 2>"$fetch_err"); then
  rm -f "$fetch_err"
  printf '%s' "$body"
  exit 0
fi

# "No session in keychain" is the dominant first-run case. Show the user a
# self-contained onboarding block instead of silently falling through to the
# plaintext stub or a cryptic error — the orchestrator paraphrases
# additional_context but displays tool output verbatim, so this message lands
# in the chat where the user can act on it.
if grep -q "no synaptory session in keychain" "$fetch_err" 2>/dev/null; then
  rm -f "$fetch_err"
  cat <<ONBOARD
# Synaptory — sign-in required

You are not signed in to the Synaptory Control Plane, so the \`${SKILL_NAME}\` skill cannot load its full instructions.

## One-time setup

**1. Install the \`synaptory\` CLI on your PATH** (skip if you already have it):
\`\`\`bash
bash "${PLUGIN_ROOT}/hooks/install-cli.sh"
\`\`\`
This symlinks the bundled CLI into \`~/.local/bin/synaptory\`. Add \`~/.local/bin\` to your \`\$PATH\` if it is not already.

**2. Connect this repo to your control-plane project.** Your H3Tech operator created a project slug for your team (e.g. \`taskflow-pilot\`). From the repo root:
\`\`\`bash
echo "project_id: <your-project-slug>" > .synaptory.yaml
\`\`\`
If you do not have a project slug yet, email **ops@h3t.co** with your UPN and project name.

**3. Sign in** — this opens your browser for Entra sign-in:
\`\`\`bash
synaptory login --project <your-project-slug>
\`\`\`

**4. Start a new Claude Code session in this repo**, then re-invoke \`/${SKILL_NAME}\`. SessionStart will pick up the cached session automatically.

---

*Orchestrator note: do NOT proceed with synaptory workflows until the user has completed the steps above. If the user wants help on something else in the meantime, handle it with standard tools.*
ONBOARD
  exit 0
fi

# Any other failure (network, server error, etc.): show stderr + fall back to
# plaintext if available.
err_content=$(cat "$fetch_err" 2>/dev/null || true)
rm -f "$fetch_err"

PLAINTEXT_SKILL="${PLUGIN_ROOT}/skills/${SKILL_NAME}/SKILL.md"
if [[ -f "$PLAINTEXT_SKILL" ]] && ! head -5 "$PLAINTEXT_SKILL" | grep -q "synaptory-decrypt-skill"; then
  # Plaintext body is a dev-mode convenience; in a distributed build the stub
  # matches this grep and we fall through.
  cat "$PLAINTEXT_SKILL"
  exit 0
fi

cat <<ERR
# Synaptory — skill load failed

Could not load skill \`${SKILL_NAME}\`. The CLI returned:

\`\`\`
${err_content:-(no error output)}
\`\`\`

Troubleshooting:
- Check that the control plane is reachable: \`curl ${SYNAPTORY_CONTROL_PLANE_URL}/healthz\`
- Refresh the local skill cache: \`synaptory skills sync\`
- If the issue persists, contact your H3Tech operator.
ERR
exit 1
