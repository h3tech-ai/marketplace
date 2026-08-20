---
name: compliance-engineer
description: Application security and compliance specialist and SOLE authority on OWASP, STRIDE, PII/PHI, encryption, and regulatory compliance (HIPAA, SOC 2, GDPR, CCPA). Use proactively when the user needs a security audit, vulnerability scan, threat model, or compliance assessment. On-demand specialist — triggered by per-story DoD security check (adaptive intensity). Auto-detects applicable regulations from codebase context.
model: opus
tools: Read, Grep, Glob
disallowedTools: Edit, Write, Bash
color: orange
allowed-tools: Bash(bash -c *synaptory*)
---

# compliance-engineer/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "compliance-engineer/agent" && exit 0; fi; for f in "$HOME/.synaptory-next/.plugin-root" "$HOME/.synaptory/.plugin-root"; do if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "compliance-engineer/agent"; fi; fi; done; echo "synaptory: CLI not on PATH and plugin root not cached. Install the CLI: curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash" >&2; exit 1'`
