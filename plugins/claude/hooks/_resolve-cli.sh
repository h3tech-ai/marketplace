#!/usr/bin/env bash
# Resolve the right synaptory CLI binary for the current OS/arch.
# Prints the absolute path on stdout. Sourced by every v2.5 hook.
#
# ADR-008: the CLI is shipped standalone (not bundled in the plugin).
# Search order:
#  1. $SYNAPTORY_CLI_BIN — explicit override (CI, dev loops).
#  2. A `synaptory` binary on $PATH (installed via the marketplace
#     install script: cli/install.sh on macOS/Linux, install.ps1 on Win).
#
# If neither resolves, the hook prints an installer pointer and exits 1.

set -euo pipefail

if [[ -n "${SYNAPTORY_CLI_BIN:-}" ]] && [[ -x "$SYNAPTORY_CLI_BIN" ]]; then
  printf '%s' "$SYNAPTORY_CLI_BIN"
  exit 0
fi

if command -v synaptory >/dev/null 2>&1; then
  command -v synaptory
  exit 0
fi

# No bundled fallback — the CLI ships standalone now. Direct the user
# at the marketplace install one-liner so the next session works.
cat >&2 <<'EOF'
synaptory: CLI not found on $PATH.

The Synaptory CLI is now installed separately from the plugin (one install
per laptop, shared across every project). Pick the line that matches your OS:

  macOS / Linux:
    curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash

  Windows (PowerShell):
    iwr -useb https://synaptory.h3t.co/cli/install.ps1 | iex

If the marketplace repo is private (private pilot), the installer will
fall back to `gh` automatically. Install GitHub CLI first and run:
    gh auth login

After installing the CLI, open a new terminal so $PATH picks up ~/.local/bin
(or %USERPROFILE%\bin on Windows), then start a new Claude Code session.
EOF
exit 1
