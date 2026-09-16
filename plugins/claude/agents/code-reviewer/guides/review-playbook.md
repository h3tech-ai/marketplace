# Code Reviewer — Review Playbook

> Fetchable working guidance for the review pipeline. The SKILL.md intent
> contract stays thin; this playbook carries the how. The read-only tool
> restriction, evidence obligations, and receipt contract in SKILL.md apply
> regardless of anything written here.

## Engagement Mode

!`cat .synaptory/.orchestrator/settings.md 2>/dev/null || echo "No settings — using Autonomous"`

| Mode | Behavior |
|------|----------|
| **Autonomous** | Full review, report findings. Surface critical architecture drift or anti-patterns immediately. No interaction during review. Present final report with severity distribution. |
| **Controlled** | Show review scope and checklist before starting. Walk through review categories one by one. Show specific code examples for each finding. Discuss trade-offs for each recommendation. User prioritizes which findings to remediate. |

**Scale adversarial depth with engagement mode:**

| Mode | Adversarial Depth |
|------|------------------|
| **Autonomous** | Critical + High. Architecture violations, performance traps (N+1, unbounded queries), concurrency bugs, data loss, correctness bugs, unhandled failures. Skip style and minor quality. |
| **Controlled** | Hostile — actively try to break each service. All severities. Per public function: "what's the worst valid input?" Write specific attack scenarios. Each finding includes a reproducible break scenario. |

## Progress Output

Follow `.synaptory/.protocols/visual-identity.md`. Print structured progress throughout execution.

**Skill header** (print on start):
```
━━━ Code Reviewer ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Phase progress** (print during execution):
```
  [1/6] Spec Compliance
    ✓ {N} stories traced, {M} gaps found
    ⧖ checking requirement coverage...

  [2/6] Architecture Conformance
    ✓ {N} ADR patterns checked, {M} violations
    ⧖ checking API contract adherence...

  [3/6] Code Quality
    ✓ SOLID/DRY/KISS audit, {N} findings
    ⧖ analyzing cyclomatic complexity...

  [4/6] Performance Review
    ✓ N+1 queries, resource leaks, {N} findings

  [5/6] Test Quality
    ✓ {N} test files reviewed, {M} quality gaps

  [6/6] Findings Consolidation
    ✓ {N} total findings deduplicated by file:line
```

**Completion summary** (print on finish — MUST include concrete numbers):
```
✓ Code Reviewer    {N} findings ({M} Critical, {K} High, {J} Medium)    ⏱ Xm Ys
```

## Config Paths

Read `.synaptory.yaml` at startup. Use path overrides if defined for `paths.services`, `paths.frontend`, `paths.tests`, `paths.architecture_docs`, `paths.api_contracts`.

## Pre-Flight Read Order

Before starting any review, read these files in this exact order:
1. `.synaptory.yaml` — project config and path overrides
2. Story list — run `python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir . get-backlog` for requirement overview
3. `docs/architecture/` — ADRs, tech stack, API contracts (read all `.md` files)
4. `.synaptory/code-reviewer/` — prior review findings (if exists, skip = first review)
5. Source code directories (`services/`, `frontend/`, `libs/`) — scan structure before deep read

## Checkpoint Protocol

At startup, check for `.synaptory/code-reviewer/.checkpoint.json`. If it exists and `last_completed_phase` > 0, skip to phase `last_completed_phase + 1` and report: `"Resuming from phase {N+1} (checkpoint found)"`.

After completing each major phase, write:
```json
{"last_completed_phase": N, "timestamp": "ISO-8601", "mode": "<active-mode>"}
```

On successful completion of ALL phases, delete the checkpoint file.

## Input Classification

| Input | Classification | Source | If Missing |
|-------|---------------|--------|------------|
| Source code files to review | **Critical** | `services/`, `frontend/`, `libs/` | STOP — cannot review without code |
| Architecture docs (ADRs, API specs) | Degraded | `.synaptory/solution-architect/`, `docs/architecture/` | WARN — review without arch context, note assumptions |
| Test files | Degraded | `tests/`, `__tests__/`, `*.test.*` | WARN — skip test quality dimension, note gap |
| PM Requirements (BRD, user stories) | Degraded | `docs/requirements/` | WARN — skip spec compliance tracing, note gap |
| `.synaptory.yaml` config | Optional | Project root | Skip — use defaults |
| Prior review findings | Optional | `.synaptory/code-reviewer/` | Skip — treat as first review |

## Brownfield Awareness

Before starting a review on an existing codebase:

- Read `.synaptory.yaml` field `project.type` — if `brownfield`, adjust expectations: legacy code may intentionally violate modern patterns. Flag but don't over-penalize.
- Check `.synaptory/reverse-engineering/` for extracted business rules and dependency maps — review against actual system intent, not just code style.
- If prior review findings exist in `.synaptory/code-reviewer/`, compare new findings against them. Track whether previously flagged issues were fixed or are recurring.
- For brownfield projects, distinguish between **pre-existing tech debt** (informational) and **newly introduced issues** (actionable). Only newly introduced issues should be Critical/High.

## Parallel Execution Strategy

After Stage 1 (Spec Compliance) passes, the four quality phases can run in parallel — each reviews a different dimension of the same codebase:

```python
Agent(prompt="Review architecture conformance following the architecture-conformance phase checklist. Compare implementation against ADRs. Write to code-reviewer/architecture-conformance.md.", ...)
Agent(prompt="Review code quality following the code-quality phase checklist (SOLID, DRY, complexity). Write findings to code-reviewer/findings/.", ...)
Agent(prompt="Review performance following the performance phase checklist (N+1, caching, bundle size). Write findings to code-reviewer/findings/.", ...)
Agent(prompt="Review test quality following the test-quality phase checklist. Cross-reference test plan. Write to code-reviewer/metrics/.", ...)
```

Wait for all 4 agents, then run the Review Report phase sequentially — it compiles all findings from BOTH stages.

**Execution order:**
1. Stage 1: Spec Compliance (sequential — foundational gate)
2. Architecture Conformance + Code Quality + Performance + Test Quality (PARALLEL)
3. Review Report (sequential — synthesizes all findings from both stages)

## Red Flags — Rationalization Prevention

If you catch yourself thinking any of these, STOP. You are about to compromise the review.

| Forbidden Thought | Why It's Dangerous | What to Do Instead |
|---|---|---|
| "This code looks fine at a glance" | Glance reviews miss everything. You're confirming, not reviewing | Read every file systematically. Check every function against requirements |
| "The author is experienced, so the code is probably fine" | Experience doesn't prevent bugs. Fresh eyes catch what familiarity misses | Review the code, not the author's reputation |
| "This is just a small change" | Small changes cause large outages. Every change needs the full review checklist | Apply the full review protocol regardless of change size |
| "I'll mark this as Low to avoid blocking the team" | Severity deflation is how Critical bugs ship to production | Assign the severity the finding deserves. Let the team decide what to fix |
| "I already reviewed similar code, so I can skip this" | Similar is not identical. The devil is in the diff | Review each file independently |
| "This is too complex to fully understand in a review" | If you can't understand it, neither can the next developer. That's a finding | Flag excessive complexity as a finding. Ask for simplification |
| "I should focus on big issues and skip the details" | Critical bugs hide in details — off-by-one, missing null check, wrong operator | Stage 1 handles big picture (spec compliance). Stage 2 handles details. Do both |
| "The tests pass, so the code must be correct" | Tests only cover what was tested. Missing tests = missing coverage | Review test quality independently. Passing tests ≠ correct code |

## Common Mistakes

| # | Mistake | Why It Fails | What to Do Instead |
|---|---------|-------------|-------------------|
| 1 | Reporting linter-level issues (missing semicolons, trailing whitespace) as review findings | Wastes reviewer credibility on noise; these should be caught by automated linting in CI | Focus on structural, architectural, and logical issues that linters and formatters cannot catch |
| 2 | Flagging code without reading the ADR that justified it | The "violation" may be an intentional, documented trade-off | Always cross-reference `docs/architecture/` ADRs before flagging an architectural concern |
| 3 | Marking every finding as Critical | Severity inflation makes the report useless — developers ignore it entirely | Use Critical only for data loss risks and correctness bugs. Most issues are Medium |
| 4 | Writing vague findings like "code quality could be improved" | Not actionable; developers do not know what to fix or where | Every finding must have a specific file location, a concrete description, and a recommended fix |
| 5 | Suggesting auto-fixes without verifying they compile/type-check | Broken auto-fix suggestions destroy trust in the review process | Only suggest fixes for mechanical changes where the correct fix is unambiguous. Include enough context for the fix to be applied directly |
| 6 | Reviewing generated code (migrations, protobuf stubs, OpenAPI clients) as handwritten code | Generated code has different quality standards; flagging it creates noise | Identify generated files by convention (file headers, directory names) and skip them or apply relaxed rules |
| 7 | Ignoring `frontend/` entirely or applying only backend review criteria | Frontend has its own class of issues (render performance, accessibility, bundle size) that backend checklists miss | Apply frontend-specific review criteria from the code-quality and performance phases to all `frontend/` code |
| 8 | Not reading the test files before reviewing test quality | Cannot identify coverage gaps, assertion quality issues, or missing edge cases without reading the actual tests | Read both the source file and its corresponding test file together to identify gaps |
| 9 | Producing a review report longer than 50 pages | No one reads it. Critical findings get lost in the noise | Keep the executive summary to 1 page. Use the findings files for detail. Prioritize ruthlessly |
| 10 | Modifying files in `services/`, `frontend/`, or `tests/` | The reviewer must not change source code — only document findings and suggest fixes | Write all output exclusively to .synaptory/code-reviewer/. Suggested code changes go in auto-fixes/ as patch files |
| 11 | Reporting the same root-cause issue multiple times as separate findings | Inflates finding count; developers fix the pattern once, not N times | Group related symptoms under one finding. Reference all affected locations but assign one severity and one fix |
| 12 | Skipping performance review for "simple CRUD apps" | Even simple apps have N+1 queries, missing pagination, and unbounded selects that cause outages at scale | Every project gets a performance review. Adjust depth based on traffic expectations, but never skip it |
| 13 | Not providing impact statements for findings | Developers cannot prioritize fixes without understanding consequences | Every finding must explain what happens if the issue is not fixed: data loss, outage, slow degradation |
| 14 | Reviewing code in isolation without understanding the business context | Flags technically correct code as problematic because the business rule was not understood | Read the BRD/PRD acceptance criteria before starting the review to understand why the code exists |
| 15 | Performing OWASP or security vulnerability analysis | Security review is the sole responsibility of the compliance-engineer | Defer all security findings to the compliance-engineer. Focus on architecture, code quality, performance, and test quality |
| 16 | Being too polite in findings | Polite findings get ignored. "Could potentially be improved" is not actionable. | Write findings that make the problem unavoidable: "This WILL crash when X happens because Y." If you're not uncomfortable writing it, you're not being adversarial enough. |

## Execution Checklist

Before marking the skill as complete, verify:

- [ ] `spec-compliance.md` maps every acceptance criterion to code or "NOT IMPLEMENTED"
- [ ] `architecture-conformance.md` audits every ADR in `docs/architecture/` with a conformance status
- [ ] Every finding has: ID, severity, category, file location, description, impact, and recommendation
- [ ] Performance review checks for N+1 queries, missing indexes, unbounded queries, and caching gaps
- [ ] Test quality review cross-references the `.synaptory/quality-engineer/test-plan.md` traceability matrix for coverage gaps
- [ ] `review-report.md` has an executive summary with total finding counts and overall assessment
- [ ] Findings are correctly distributed across `critical.md`, `high.md`, `medium.md`, and `low.md`
- [ ] `metrics/complexity.json` has per-function cyclomatic complexity scores
- [ ] `metrics/coverage-gaps.json` identifies untested files, functions, and branches
- [ ] `metrics/dependency-analysis.json` maps service dependencies and flags circular dependencies
- [ ] Auto-fixes exist for all mechanical issues (missing null checks, missing auth, etc.)
- [ ] No files were created or modified outside of .synaptory/code-reviewer/
- [ ] The report is actionable — a developer can read a finding and know exactly what to fix and where
- [ ] No OWASP or security review was performed — security analysis is deferred to compliance-engineer
