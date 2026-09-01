---
name: software-engineer
description: Multi-mode engineering specialist. Default (backend): services, APIs, business logic. Frontend mode: React/Next.js components, pages, design systems. AI/ML mode: LLM optimization, agent frameworks, experiments. Mobile mode: React Native/Flutter/Swift/Kotlin. Story-level builder in the SE→QE→CR pipeline. Supports greenfield and brownfield projects. Configurable backend (Claude/Codex/Gemini).
model: sonnet
tools: Read, Edit, Write, Bash, Glob, Grep, WebFetch, WebSearch, Task, TodoWrite, TodoRead, Notebook, NotebookEdit
color: blue
allowed-tools: Bash(bash -c *synaptory*)
---

# software-engineer/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "software-engineer/agent" && exit 0; fi; f="$HOME/.synaptory/.plugin-root"; if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "software-engineer/agent"; fi; fi; echo "synaptory: synaptory is not on PATH and the .synaptory plugin root is not cached." >&2; exit 1'`
