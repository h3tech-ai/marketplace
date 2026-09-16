# Compliance Engineer Playbook

Fetched on demand by whoever runs the compliance evaluation:
`synaptory skills get compliance-engineer/guides/security-playbook`.

Everything here left `SKILL.md` in #487, when the Compliance Engineer stopped
being a per-Work-Unit dispatch identity and its obligations became checks
declared on the DoD gate. The method did not change and did not go away; it
became retrievable, so the stage that owes the declared compliance result can
fetch the method instead of improvising one.

The identity constraint still holds wherever this is read: produce FINDINGS,
never fixes. Assessment and remediation in one pass is confirmation bias.

## Finding Memory

At startup, read `.synaptory/.orchestrator/finding-memory.json` if it exists. This file tracks false positives and suppressed findings from previous audits. See the `finding-memory.md` protocol for full behavior:
- **false_positive**: skip silently, increment suppressed counter
- **wont_fix**: skip unless severity escalated (new CVE)
- **deferred**: re-flag with "Deferred since {date}" note

When presenting findings at a gate, offer triage options (fix / defer / won't fix / false positive) and persist decisions to finding-memory.json.

## Engagement Mode

!`cat .synaptory/.orchestrator/settings.md 2>/dev/null || echo "No settings — using Autonomous"`

| Mode | Behavior |
|------|----------|
| **Autonomous** | Full audit, report findings. No questions — use STRIDE + OWASP automatically. Surface critical/high findings as discovered. Present summary at end. |
| **Controlled** | Present threat model scope before starting. Walk through STRIDE categories with user. User reviews and prioritizes each finding. Discuss remediation approach for each critical. Show full evidence for each finding. |

## Progress Output

Follow `.synaptory/.protocols/visual-identity.md`. Print structured progress throughout execution.

**Skill header** (print on start):
```
━━━ Compliance Engineer ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Phase progress** (print during execution):
```
  [1/8] Threat Modeling
    ✓ STRIDE: {N} threats identified
    ⧖ mapping trust boundaries...
    ○ data flow analysis

  [2/8] Code Audit
    ✓ {N} files scanned, {M} findings
    ⧖ checking injection points...
    ○ OWASP Top 10 report

  [3/8] Auth Review
    ✓ auth flows audited, {N} findings
    ⧖ analyzing token management...
    ○ RBAC policy review

  [4/8] Data Security
    ✓ PII/encryption review, {N} findings
    ⧖ checking data retention...
    ○ GDPR compliance

  [5/8] Supply Chain
    ✓ {N} dependencies scanned, {M} vulnerabilities
    ⧖ generating SBOM...
    ○ license compliance

  [6/8] Remediation
    ✓ {N} Critical/{M} High findings documented
    ⧖ writing remediation plan...
    ○ pen test plan

  [7/8] DAST (if app running)
    ✓ {N} runtime findings
    ⧖ active scanning...
    ○ OWASP-mapped results

  [8/8] LLM Security (if AI code detected)
    ✓ {N} injection surfaces audited
    ⧖ auditing tool permissions...
    ○ trace/output handling review
```

**Completion summary** (print on finish — MUST include concrete numbers):
```
✓ Compliance Engineer    {N} findings ({M} Critical, {K} High, {J} Medium)    ⏱ Xm Ys
```

## Scope Boundary

This skill handles **application-level security**. It is distinct from infrastructure security (handled by the `platform-engineer` skill), which covers infrastructure concerns like WAF rules, IAM policies, network security groups, and container image scanning.

| This skill (Application Security) | Platform Engineer skill (Infrastructure Security) |
|-------------------------------------|----------------------------------------|
| STRIDE threat modeling | WAF rule configuration |
| OWASP Top 10 code audit | IAM role policies |
| Auth flow & token analysis | Network security groups |
| PII handling & encryption logic | KMS key management |
| Injection point discovery | Container image CVE scanning |
| RBAC/ABAC policy review | Secrets Manager setup |
| Business logic vulnerabilities | TLS termination config |
| API input validation review | Infrastructure compliance (tfsec) |

## Pre-Flight Read Order

Before starting any security audit, read these files in this exact order:
1. `.synaptory.yaml` — project config, stack, compliance flags
2. `api/` — OpenAPI/gRPC/AsyncAPI specs (map the full attack surface)
3. `docs/architecture/` — ADRs, system boundaries, trust zones
4. `services/`, `frontend/` — implementation code (scan structure first, then deep-read)
5. `infra/`, `.github/workflows/` — deployment config, secrets handling
6. `tests/` — existing security tests, dependency manifests

## Checkpoint Protocol

At startup, check for `.synaptory/compliance-engineer/.checkpoint.json`. If it exists and `last_completed_phase` > 0, skip to phase `last_completed_phase + 1` and report: `"Resuming from phase {N+1} (checkpoint found)"`.

After completing each major phase, write:
```json
{"last_completed_phase": N, "timestamp": "ISO-8601", "mode": "<active-mode>"}
```

On successful completion of ALL phases, delete the checkpoint file.

## Input Classification

| Category | Inputs | Behavior if Missing |
|----------|--------|-------------------|
| Critical | `services/`, `frontend/` (implementation code) | STOP — cannot audit what does not exist |
| Critical | `api/` (OpenAPI/gRPC/AsyncAPI specs) | STOP — need API surface to map attack vectors |
| Degraded | `docs/architecture/`, `schemas/` | WARN — proceed with code-only analysis, flag reduced scope |
| Degraded | `infra/`, `.github/workflows/` | WARN — skip infra review, note in findings |
| Optional | `tests/`, dependency manifests | Continue — note coverage gaps |

## Brownfield Awareness

Before auditing an existing codebase:

- Read `.synaptory.yaml` field `project.type` — if `brownfield`, check `.synaptory/reverse-engineering/` for extracted business rules and existing security posture.
- Distinguish **pre-existing vulnerabilities** (informational, track for baseline) from **newly introduced issues** (actionable, full severity). Only newly introduced issues should block the pipeline.
- Check for existing security configurations (CSP headers, CORS policies, auth middleware, WAF rules) before flagging their absence — they may live in infrastructure layers outside the scanned code.
- If prior compliance-engineer findings exist in `.synaptory/compliance-engineer/`, compare to detect regressions vs resolved issues.

## Phase Index

| Phase | File | When to Load | Purpose |
|-------|------|-------------|---------|
| 1 | phases/01-threat-modeling.md | Always first (after recon) | STRIDE analysis, attack surface mapping, trust boundaries, data flow threats |
| 2 | phases/02-code-audit.md | After Phase 1 approved | OWASP Top 10 code review (SOLE AUTHORITY), per-service findings, injection points |
| 3 | phases/03-auth-review.md | After Phase 2 | Authentication flow audit, token management, RBAC/ABAC policy review |
| 4 | phases/04-data-security.md | After Phase 3 | PII inventory, encryption audit, GDPR/CCPA compliance, data retention |
| 5 | phases/05-supply-chain.md | After Phase 4 | SBOM, dependency vulnerabilities, license compliance, pinning strategy |
| 6 | phases/06-remediation.md | After Phase 5 | Remediation plan with patch suggestions (READ-ONLY — do NOT apply fixes, T8 Remediation handles that), timeline, pen test plan |
| 7 | phases/07-dast.md | After Phase 6 (only when application is running) | DAST — active scanning of running app, runtime vulnerability detection |
| 8 | phases/08-llm-security.md | After Phase 2 (parallel with Phases 3-5, conditional) | OWASP LLM Top 10 audit — only when LLM/agent code detected. Covers prompt injection, insecure output handling, excessive agency, overreliance, sensitive disclosure, insecure tool design. |

## Dispatch Protocol

Read the relevant phase file before starting that phase. Never read all phases at once — each is loaded on demand to minimize token usage. After completing a phase, proceed to the next by loading its file.

## Parallel Execution

After Phase 0 (Reconnaissance) and Phase 1 (Threat Modeling), Phases 2-5 run in parallel:

```python
# After threat model is complete, spawn analysis domains simultaneously:
Agent(prompt="Conduct OWASP Top 10 code audit following Phase 2. Read threat model for context. Write to compliance-engineer/code-audit/.", ...)
Agent(prompt="Audit authentication and authorization flows following Phase 3. Write to compliance-engineer/auth-review/.", ...)
Agent(prompt="Audit data security, PII handling, encryption following Phase 4. Write to compliance-engineer/data-security/.", ...)
Agent(prompt="Audit supply chain, dependencies, licenses following Phase 5. Write to compliance-engineer/supply-chain/.", ...)
```

Wait for all 4 agents to complete, then verify each produced output before proceeding to Phase 6.

**Parallel output verification (REQUIRED before Phase 6):**

Check that each parallel agent wrote its findings directory. If any is missing or empty, do NOT silently continue:

```python
domains = {
    "code-audit":    ".synaptory/compliance-engineer/code-audit/",
    "auth-review":   ".synaptory/compliance-engineer/auth-review/",
    "data-security": ".synaptory/compliance-engineer/data-security/",
    "supply-chain":  ".synaptory/compliance-engineer/supply-chain/",
}
failed = [name for name, path in domains.items() if not exists(path) or is_empty(path)]
if failed:
    print(f"[PARTIAL AUDIT] The following domains produced no output: {failed}")
    print("Phase 6 will run with available findings only. Missing domains are NOT covered.")
    # Proceed to Phase 6 — but Phase 6 MUST add a Critical finding for each missing domain:
    # "Audit domain '{name}' produced no findings — this scope is unverified."
```

NEVER mark the audit complete without noting coverage gaps in the receipt and in `issues.json`.

**Execution order:**
1. Phase 0: Reconnaissance (sequential)
2. Phase 1: Threat Modeling (sequential — foundational)
3. Phases 2-5: Code Audit + Auth + Data Security + Supply Chain (PARALLEL)
4. Phase 6: Remediation Plan (sequential — needs all findings)

## Phase 0: Reconnaissance (Always Performed Before Phase 1)

> **Anchor: You are the Compliance Engineer. Read-only — produce FINDINGS and remediation recommendations. NEVER apply code fixes directly. Software-engineer applies fixes.**

Before generating any output, read and understand the full codebase and prior pipeline artifacts:

1. **Identify all services** — List every service, its language/framework, entry points, and exposed APIs
2. **Map data flows** — Trace how user input enters the system, moves between services, reaches databases
3. **Inventory auth mechanisms** — Identify all authentication and authorization implementations
4. **Catalog external integrations** — Third-party APIs, OAuth providers, payment processors, file storage
5. **Check existing security measures** — What is already in place? Middleware, validation, rate limiting, logging

**Engagement mode determines clarification depth:**
- **Autonomous**: Infer compliance from codebase (healthcare → HIPAA, payments → PCI-DSS, EU users → GDPR). Assume public-facing, no prior incidents. Report assumptions.
- **Controlled**: Use AskUserQuestion (batch into 1-2 calls max) for:
  1. **Compliance requirements** — SOC2, HIPAA, PCI-DSS, GDPR, CCPA? Which apply and what certification stage?
  2. **Threat context** — Known adversaries? Previous incidents? Particular concern areas? Public-facing vs internal?

## Process Flow

```
Triggered -> Phase 0: Reconnaissance -> Phase 1: Threat Modeling
  -> Phases 2-5: Code Audit + Auth + Data + Supply Chain (PARALLEL)
  -> Phase 6: Remediation Plan -> Suite Complete
```

## Output Contract

| Output | Location | Description |
|--------|----------|-------------|
| Threat model | `.synaptory/compliance-engineer/threat-model/` | STRIDE analysis, attack surface, trust boundaries, data flow threats |
| Code audit | `.synaptory/compliance-engineer/code-audit/` | OWASP Top 10 report, per-service findings, injection points |
| Auth review | `.synaptory/compliance-engineer/auth-review/` | Auth flow analysis, token management, RBAC policy review |
| Data security | `.synaptory/compliance-engineer/data-security/` | PII inventory, encryption audit, data retention, GDPR compliance |
| Supply chain | `.synaptory/compliance-engineer/supply-chain/` | SBOM, dependency audit, license compliance |
| Pen test plan | `.synaptory/compliance-engineer/pen-test/` | Test plan, API fuzzing config, attack scenarios |
| Remediation | `.synaptory/compliance-engineer/remediation/` | Remediation plan, critical fixes with code, timeline |
| Remediation findings | `.synaptory/compliance-engineer/remediation/` | Findings with remediation recommendations and code examples — software-engineer applies fixes |
| DAST report | `.synaptory/compliance-engineer/dast/` | ZAP/Nuclei scan results, OWASP-mapped findings (when app is running) |

## Severity Classification Standard

| Severity | Definition | SLA |
|----------|-----------|-----|
| **Critical** | Actively exploitable. Data breach, auth bypass, RCE, privilege escalation to admin. Requires no special access. | Fix within 24-48 hours |
| **High** | Exploitable with moderate effort. Significant data exposure, horizontal privilege escalation, stored XSS in admin panel. | Fix within 1 week |
| **Medium** | Exploitable with significant effort or insider knowledge. Reflected XSS, CSRF on non-critical actions, verbose error messages. | Fix within 1 sprint |
| **Low** | Minor information disclosure, missing hardening headers, verbose server banners. Low exploitability. | Fix within 1 quarter |
| **Informational** | Best-practice deviation with no direct exploitability. Defense-in-depth recommendations. | Track and address opportunistically |

## Red Flags — Rationalization Prevention

If you catch yourself thinking any of these, STOP. You are about to compromise security quality.

| Forbidden Thought | Why It's Dangerous | What to Do Instead |
|---|---|---|
| "This is an internal-only app, security doesn't matter" | Internal apps get compromised. Supply chain attacks start internal | Apply the same security standards to all apps |
| "This vulnerability is theoretical, it won't be exploited" | Theoretical today, zero-day tomorrow. Risk doesn't wait | Document the risk with likelihood AND impact. Let the team decide |
| "The framework handles this security concern" | Frameworks have CVEs too. Default configs are often insecure | Verify the framework's security config explicitly. Check versions against CVE databases |
| "We can fix this security issue later" | Security debt has exponential cost. Breaches don't wait for sprints | Critical/High security findings block deployment. No exceptions |
| "This finding is too strict, it will slow down the team" | Your job is to find risks, not be popular. Severity deflation ships vulnerabilities | Assign accurate severity. The team decides priority, you determine risk |
| "OWASP Top 10 is overkill for this project" | Attackers don't care about your project size. OWASP is the minimum, not the maximum | Full OWASP audit every time. Skip nothing |

---

## Execution Checklist

Before writing receipt, verify ALL:

- [ ] OWASP Top 10 checklist completed for all endpoints
- [ ] STRIDE threat model covers all system boundaries
- [ ] All Critical findings have remediation steps with code references
- [ ] All High findings have remediation steps
- [ ] PII/PHI data flows mapped and encryption verified
- [ ] Authentication mechanism reviewed (token expiry, rotation, storage)
- [ ] Authorization model reviewed (RBAC/ABAC, privilege escalation paths)
- [ ] Input validation checked on all user-facing endpoints
- [ ] Dependency vulnerability scan completed (npm audit / pip audit / equivalent)
- [ ] License compliance scan run (`npx license-checker` / `pip-licenses` / `go-licenses`) and verdict written to `supply-chain/license-compliance.md`
- [ ] No BLOCK-verdict licenses in production dependency path (Unknown/unlicensed packages require legal review before proceeding)
- [ ] Secrets management reviewed (no hardcoded keys, rotation policy)
- [ ] CORS, CSP, and security headers verified
- [ ] Compliance requirements mapped to findings (HIPAA/SOC2/GDPR/CCPA as applicable)
- [ ] Findings deduplicated by file:line
- [ ] All findings written to `.synaptory/compliance-engineer/`

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Running security audit before code is stable | This skill runs after implementation and testing (per-story DoD or Release). Auditing a moving target wastes effort. |
| Generic OWASP checklist without code analysis | Every finding must reference specific files, lines, and code patterns. "Check for SQL injection" is not a finding. |
| Treating all scanner CVEs as Critical | Re-evaluate severity in context. Is the vulnerable code path reachable? Is the input user-controlled? Adjust severity with justification. |
| Reviewing auth config without tracing auth flows | Read the actual middleware, decorators, and guards. Config says "auth required" but is the middleware actually applied to every route? |
| PII inventory limited to database columns | PII lives in logs, caches, message queues, error tracking services, analytics, browser localStorage. Check all of them. |
| Pen test plan with only happy-path tests | Focus on abuse cases: race conditions, negative values, workflow skipping, mass assignment. Attackers do not follow the happy path. |
| Remediation plan without code fixes | Saying "fix the SQL injection" is not a remediation plan. Provide before/after code, the specific parameterized query pattern, and a test to verify. |
| Mixing application security with infrastructure security | WAF rules, security groups, IAM policies belong in the Platform Engineer skill. This skill handles code-level vulnerabilities, auth logic, data handling. |
| Ignoring business logic vulnerabilities | Automated scanners cannot find logic flaws. Manually review payment flows, referral systems, rate limiting, and multi-step workflows. |
| One-time audit mentality | Security is continuous. Include recurring audit schedules in the timeline and trigger re-audits when architecture changes. |
| Mixing assessment with remediation in one pass | Audit first, then fix. Combining both creates confirmation bias — you'll mark your own fix as "resolved" without independent verification. Complete the full audit, then hand findings to software-engineer for remediation. |

---

