#!/usr/bin/env bash
# Compose plugin-cursor/ from plugin-claude/ + the Cursor overlay.
# Also invoked by ./synaptory build. See compose.py docstring for the
# authored-vs-GENERATED split — do not hand-edit GENERATED copies.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "${ROOT}/plugin-cursor/scripts/compose.py" "$@"
