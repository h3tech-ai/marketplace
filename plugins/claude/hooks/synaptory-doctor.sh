#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# User-invocable self-service diagnostic. Prints a plain-text report showing
# token source, format, signature status, expiry, backend mode, and session
# dir. SessionStart funnels errors here automatically.

set -euo pipefail

# shellcheck source=lib/resolve-python.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/resolve-python.sh"
: "${SYNAPTORY_PYTHON:?synaptory requires Python 3.9+. Set SYNAPTORY_PYTHON to override.}"

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
exec "$SYNAPTORY_PYTHON" "${PLUGIN_ROOT}/hooks/lib/doctor.py"
