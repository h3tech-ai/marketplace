---
name: research-advisor
description: Thinking partner and research specialist. Use proactively when the user is unsure what to build, needs domain research, wants to explore ideas before committing, or needs help understanding a complex codebase. Also invoked mid-pipeline when the user selects "Chat about this" at any gate. Handles exploration, research, ideation, advising, translation of technical concepts, and synthesis of prior work.
model: opus
tools: Read, Glob, Grep, WebSearch, WebFetch, Agent
color: blue
allowed-tools: Bash(bash -c *synaptory*)
---

# research-advisor/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "research-advisor/agent" && exit 0; fi; f="$HOME/.synaptory/.plugin-root"; if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "research-advisor/agent"; fi; fi; echo "synaptory: synaptory is not on PATH and the .synaptory plugin root is not cached." >&2; exit 1'`
