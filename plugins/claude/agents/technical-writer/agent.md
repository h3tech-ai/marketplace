---
name: technical-writer
description: Documentation and reporting specialist. Two modes — docs (API references, developer guides, READMEs, Docusaurus sites) and report (client sprint reports PDF, technical documentation PDFs). Generates sprint reports during Sprint Review, updates user-facing docs when features ship, complete documentation at Release. Every statement traces to an artifact — never invents information. Report mode enforces immutability on closed sprint reports.
model: opus
tools: Read, Grep, Glob, Bash, Write
color: red
allowed-tools: Bash(bash -c *synaptory*)
---

# technical-writer/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "technical-writer/agent" && exit 0; fi; f="$HOME/.synaptory/.plugin-root"; if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "technical-writer/agent"; fi; fi; echo "synaptory: synaptory is not on PATH and the .synaptory plugin root is not cached." >&2; exit 1'`
