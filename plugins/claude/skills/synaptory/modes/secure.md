# Secure Mode

Standalone security review. Runs the Compliance Engineer's STRIDE + OWASP audit on demand — outside any sprint ceremony, without a Release transition, scoped to what the user actually asked about.

**This is the only user-invocable path to the Compliance Engineer.** Every other CE dispatch is conditional: the PHI gate inside a story's DoD (`modes/sprint.md`), gate remediation, Release Activity 2, or an SPQ barrier.

## Trigger Signals

"security review", "security audit", "audit my code", "harden", "is this secure", "vulnerability scan", "check for vulnerabilities", "OWASP", "STRIDE", "threat model", "pentest"

## Secure vs Review vs Release

| Mode | Agent | Scope | When |
|------|-------|-------|------|
| **Review** | Code Reviewer | Quality: architecture conformance, SOLID/DRY, perf anti-patterns, test quality | "review my code" — no audit or hardening intent |
| **Secure** | Compliance Engineer | Security: STRIDE, OWASP Top 10, auth, data handling, supply chain | "audit this for security", "harden this", any time |
| **Release** | QE + CE + PE + TW + CR | Full pre-launch readiness, maximum depth, all agents | "ship it", "production ready" — requires a delivery lifecycle in `RELEASE` |

Secure is the scoped one. It needs no lifecycle state, writes no state-machine transition, and works on a bare repo.

---

## Step 1 — Resolve Scope

Default to **diff scope**. A standalone security review usually means "what I just wrote", and a whole-codebase 8-phase audit on every ask is how the capability became unusable.

```bash
BASE=$(git merge-base HEAD "$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)" 2>/dev/null || true)
[ -n "$BASE" ] && git diff --name-only "$BASE"...HEAD
git diff --name-only HEAD                    # unstaged
git diff --name-only --cached                # staged
git ls-files --others --exclude-standard     # untracked
```

The union of those lists is the **changed set**.

| Situation | Scope |
|---|---|
| Changed set is non-empty | `diff` — audit the changed set plus its 1-hop callers |
| Changed set is empty | `full` — say so ("no pending changes; auditing the whole codebase") |
| User said "the whole codebase", "everything", "full audit", or named a directory | `full` (or `path`, scoped to what they named) |
| User named specific files | `path`, scoped to those files |

State the resolved scope and the file count before dispatching. Never silently widen it.

## Step 2 — Deterministic Pre-Pass

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/security_scan.py" . --format json
```

Fast stdlib pattern scan (hardcoded secrets, injection shapes, insecure crypto). It exits non-zero when it finds a Critical.

These are **leads, not findings**. Pass them to the Compliance Engineer as input; the CE triages them with real context. Do not report a scanner hit to the user as a confirmed vulnerability.

## Step 3 — Load Finding Memory

```bash
cat .synaptory/.orchestrator/finding-memory.json 2>/dev/null || echo "{}"
```

Pass the suppression list into the CE prompt verbatim. Findings already triaged as `false_positive` stay silent, `wont_fix` stays silent unless severity escalated, `deferred` re-surfaces with its age. This is the only suppression mechanism — never invent a second one. See `finding-memory.md` in the shared protocols.

## Step 4 — Dispatch the Compliance Engineer

```bash
CE_BACKEND=$(python3 "${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "compliance-engineer")
```

Dispatch per the backend wrapper (`skills/_shared/backends/{backend}.md`). The prompt MUST carry a `## Scope` block — that block is what tells the CE to read `modes/scoped-audit.md` and replaces its directory-oriented pre-flight:

```
## Scope

scope: {diff|full|path}
base: {BASE commit or "n/a"}
files:
{one changed-file path per line — the resolved changed set}

Read `modes/scoped-audit.md` before Phase 0 and apply it. Audit the files
listed above plus their direct callers. Findings outside that set are
informational baseline, not blockers.

## Scanner Leads

{security_scan.py JSON, or "none"}

## Suppressed Findings

{finding-memory.json entries, or "none"}
```

Everything else follows the CE's standard pipeline — Phase 0 recon, Phase 1 threat model, Phases 2-5 in parallel, Phase 6 remediation plan. Phase 7 (DAST) only if the app is already running; Phase 8 (LLM security) only if the changed set contains agent/prompt code.

**The Compliance Engineer never applies fixes.** That is a constitutional constraint on the agent — it produces findings, the Software Engineer remediates.

## Step 5 — Triage Gate

**1 gate.** Present the findings grouped by severity with the counts, then triage with `AskUserQuestion`:

| Option | Effect |
|---|---|
| Fix the criticals now | Go to Step 6 |
| Defer | Persist as `deferred` in finding-memory.json with today's date |
| Won't fix | Persist as `wont_fix` with the user's reason |
| False positive | Persist as `false_positive` — silent on every future audit |

Persist every decision to `.synaptory/.orchestrator/finding-memory.json` before moving on. A triage decision that is not written down gets re-litigated next audit.

## Step 6 — Remediation Handoff (optional)

Only when the user asked for fixes. Dispatch the Software Engineer with the CE's remediation findings (`.synaptory/compliance-engineer/remediation/`) as the task, one dispatch per critical. Then re-run Step 2's scanner to confirm the pattern is gone, and note in the receipt that remediation ran.

If the user declined fixes, stop after Step 5. Secure is read-only by default.

## Receipt

The Compliance Engineer writes its own receipt. Standalone runs have no story, so use a date-stamped id (same shape as Retro mode's):

```
.synaptory/.orchestrator/receipts/SECURE-{YYYYMMDD}-ce.json
```

with `story_id: "SECURE-{YYYYMMDD}"`, `role: "compliance-engineer"`, `token_usage.stage: "ce-compliance"`, and `metrics` carrying `findings_critical`, `findings_high`, `findings_medium`, `findings_low`. `findings_critical` is the count of **unremediated** criticals — it is the same field the `no_critical_findings` DoD gate and the Control Plane's Evidence Gate scoreboard read, so it must be an honest integer, not a placeholder.

If the CE ran inline rather than through `Agent()` (no SubagentStop, so no receipt ship loop), the orchestrator MUST write that receipt itself. The session-end hook ships unshipped receipts.

## Visual Flow

```
━━━ Secure Mode ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Target: {what the user asked about}
  Scope:  {diff|full|path} — {N} files
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [1/4] Scope
    ✓ {N} changed files vs {BASE}
    ✓ Scope: {diff|full|path}

  [2/4] Pre-pass
    ✓ security_scan.py: {N} leads ({C} critical-shaped)
    ✓ finding-memory: {M} suppressed

  [3/4] Compliance Engineer
    ✓ STRIDE: {N} threats
    ✓ OWASP: {N} findings — {C} critical, {H} high, {M} medium, {L} low
    ✓ Receipt: SECURE-{date}-ce.json

  ⬥ Triage findings

  [4/4] Outcome
    ✓ {fixed / deferred / suppressed} — {counts}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## What Secure Does Not Do

- **No lifecycle writes.** No sprint state, no story transition, no DoD evaluation. It is a standalone utility.
- **No code changes** unless the user explicitly asks at the Step 5 gate — and then the Software Engineer makes them, not the CE.
- **No infrastructure security.** WAF rules, IAM policy, network security groups, and container CVE scanning belong to the Platform Engineer. Secure is application-level.
- **No compliance certification.** A green Secure run is not HIPAA, SOC 2, or PCI attestation. For regulated projects the healthcare gate inside the story pipeline still applies (`healthcare.baa_enforced`), and Secure does not substitute for it.
