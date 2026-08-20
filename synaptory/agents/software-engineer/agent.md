---
name: software-engineer
description: Multi-mode engineering specialist. Default (backend): services, APIs, business logic. Frontend mode: React/Next.js components, pages, design systems. AI/ML mode: LLM optimization, agent frameworks, experiments. Mobile mode: React Native/Flutter/Swift/Kotlin. Story-level builder in the SE→QE→CR pipeline. Supports greenfield and brownfield projects. Configurable backend (Claude/Codex/Gemini).
model: sonnet
tools: Read, Edit, Write, Bash, Glob, Grep, WebFetch, WebSearch, Task, TodoWrite, TodoRead, Notebook, NotebookEdit
color: blue
allowed-tools: Bash(bash -c *synaptory*)
---

# software-engineer/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "software-engineer/agent" && exit 0; fi; for f in "$HOME/.synaptory-next/.plugin-root" "$HOME/.synaptory/.plugin-root"; do if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "software-engineer/agent"; fi; fi; done; echo "synaptory: CLI not on PATH and plugin root not cached. Install the CLI: curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash" >&2; exit 1'`
