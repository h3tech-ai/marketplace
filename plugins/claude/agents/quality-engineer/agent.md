---
name: quality-engineer
description: Testing specialist. Use proactively when the user wants to write or run tests — unit, integration, e2e, performance, or contract tests. Per-story verifier in the SE→QE→CR pipeline. Generates test specs during Sprint Planning, tests each story as SE completes it. Produces tests/ with full test suites. Supports greenfield and brownfield projects. Configurable backend (Claude/Codex/Gemini).
model: opus
tools: Read, Edit, Write, Bash, Glob, Grep, WebFetch, WebSearch, Task, TodoWrite, TodoRead
color: green
allowed-tools: Bash(bash -c *synaptory*)
---

# quality-engineer/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "quality-engineer/agent" && exit 0; fi; f="$HOME/.synaptory/.plugin-root"; if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "quality-engineer/agent"; fi; fi; echo "synaptory: synaptory is not on PATH and the .synaptory plugin root is not cached." >&2; exit 1'`
