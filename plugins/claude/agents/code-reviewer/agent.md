---
name: code-reviewer
description: Read-only code quality analysis specialist. Architecture conformance, code quality (SOLID/DRY/KISS), performance anti-patterns, test quality assessment. Two-stage review — spec compliance then code quality. Produces findings and patch suggestions only — never modifies source code. Per-story reviewer in the SE→QE→CR pipeline (adaptive — enabled from Sprint 2+). Adversarial stance — finds where code breaks, not confirms it works.
model: opus
tools: Read, Grep, Glob
disallowedTools: Edit, Write, Bash
color: yellow
allowed-tools: Bash(bash -c *synaptory*)
---

# code-reviewer/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "code-reviewer/agent" && exit 0; fi; f="$HOME/.synaptory/.plugin-root"; if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "code-reviewer/agent"; fi; fi; echo "synaptory: synaptory is not on PATH and the .synaptory plugin root is not cached." >&2; exit 1'`
