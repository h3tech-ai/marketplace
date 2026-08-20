#!/usr/bin/env bash
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
#
# Hook: Stop (async).
# Purpose: Flush any per-session OTLP span JSONL files written by the
# SubagentStart/SubagentStop hooks during this Claude Code session.
# Calls `synaptory telemetry traces --file <path>` for each pending
# file; the CLI ships the batch (outbox-first) and deletes the file on
# success.
#
# Best-effort by design — never blocks shutdown. The CLI's outbox is
# the persistence layer; if the CLI itself isn't available we leave the
# JSONL on disk for the next session to pick up.

set -u

_HOOK_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

# Only fire inside a synaptory project.
SUITE_DIR="${CLAUDE_PROJECT_DIR:-}/.synaptory"
OTEL_DIR="$SUITE_DIR/.orchestrator/otel"
if [ -z "${CLAUDE_PROJECT_DIR:-}" ] || [ ! -d "$OTEL_DIR" ]; then
  exit 0
fi

cli=$("${_HOOK_ROOT}/hooks/_resolve-cli.sh" 2>/dev/null || true)
if [[ -z "$cli" ]] || [[ ! -x "$cli" ]]; then
  exit 0
fi

# Ship every pending span batch. The CLI deletes the file on a
# successful POST; we just leave files behind otherwise so the
# next-session sweep picks them up.
shopt -s nullglob 2>/dev/null || true
for f in "$OTEL_DIR"/spans-*.jsonl; do
  [ -s "$f" ] || continue
  "$cli" telemetry traces --file "$f" >/dev/null 2>&1 || true
done

exit 0
