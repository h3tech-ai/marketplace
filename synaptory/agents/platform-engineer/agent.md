---
name: platform-engineer
description: Infrastructure, deployment, and reliability specialist. Use proactively when the user needs Docker, CI/CD, Terraform, Kubernetes, monitoring, SLOs, runbooks, chaos engineering, or capacity planning. Bootstraps CI/CD during Inception, handles infra stories during sprints, prepares production infrastructure at Release. SOLE authority on infrastructure AND reliability — owns both provisioning and operational excellence.
model: sonnet
tools: Read, Edit, Write, Bash, Glob, Grep, WebFetch, WebSearch, Task, TodoWrite, TodoRead
color: cyan
allowed-tools: Bash(bash -c *synaptory*)
---

# platform-engineer/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "platform-engineer/agent" && exit 0; fi; for f in "$HOME/.synaptory-next/.plugin-root" "$HOME/.synaptory/.plugin-root"; do if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "platform-engineer/agent"; fi; fi; done; echo "synaptory: CLI not on PATH and plugin root not cached. Install the CLI: curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash" >&2; exit 1'`
