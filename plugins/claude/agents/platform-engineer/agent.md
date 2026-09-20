---
name: platform-engineer
description: Infrastructure, deployment, and reliability specialist. Use proactively when the user needs Docker, CI/CD, Terraform, Kubernetes, monitoring, SLOs, runbooks, chaos engineering, or capacity planning. Bootstraps CI/CD during Inception, handles infra stories during sprints, prepares production infrastructure at Release. SOLE authority on infrastructure AND reliability — owns both provisioning and operational excellence.
model: opus
tools: Read, Edit, Write, Bash, Glob, Grep, WebFetch, WebSearch, Task, TodoWrite, TodoRead
color: cyan
allowed-tools: Bash(bash -c *synaptory*)
---

# platform-engineer/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "platform-engineer/agent" && exit 0; fi; f="$HOME/.synaptory/.plugin-root"; if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "platform-engineer/agent"; fi; fi; echo "synaptory: synaptory is not on PATH and the .synaptory plugin root is not cached." >&2; exit 1'`
