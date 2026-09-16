# Quality Engineer — Testing Playbook

Everything the Quality Engineer's thin intent contract stopped loading
unconditionally (#483, extending #404 and #405). Fetch it when the task needs
it:

```python
Bash("synaptory skills get quality-engineer/guides/testing-playbook")
```

or, in source-tree dev with no control plane, Read
`${CLAUDE_PLUGIN_ROOT}/agents/quality-engineer/guides/testing-playbook.md`.

Nothing here overrides the SKILL. The task frame, the evidence obligations and
the receipt contract live there and bind in every mode.

---

## Mode Detection Rules

The orchestrator normally names the mode in the dispatch prompt. These are the
signals to read when it does not. In every mode the **mode guide is your
complete working method** — stop reading this playbook once you are in one.

| Mode | Signals | Guide |
|---|---|---|
| Diff-Aware | "diff", "changed files", "what I changed", "branch tests", "affected tests", or synaptory mode is "Test" on a feature branch | `quality-engineer/modes/diff-aware` |
| Browser QA | dispatched with `mode: browser-qa`, **or** the story's acceptance criteria describe a user-facing screen (view/open/render/display/screen/click/navigate), **or** the task mentions "browser", "visual test", "UI test", "screenshot", "accessibility", "navigate pages", "visual regression" | `quality-engineer/modes/browser-qa` |
| Testability Review | "testability-review mode" or "testability review" (orchestrator-invoked between SE Phase 2a and 2b — never a user request) | `quality-engineer/modes/testability-review` |
| Exploratory | "exploratory test", "SBET", "time-boxed testing", "charter", "unscripted testing", "session-based testing" | `quality-engineer/modes/exploratory` |
| Standard | none of the above | the phase sequence below |

**Browser QA is auto-routed for UI-bearing stories (#44), and it carries an
evidence obligation, not just a method.** When it is routed you MUST record
`metrics.ui_verification` on your receipt or the story's `ui_acceptance` DoD
gate blocks it. **Match the story's verification tier (#134 GAP-3):** at
`standard` tier do the ~30s render assertion only (serve once, hit the
affected route(s), assert 200 + no console errors, record `ui_verification
{rendered: true, routes_tested: >=1, flows_failed: 0}`); run the full
browser-qa flows only at `full` tier or for PHI-touching stories, which are
always `full`.

---

## Protocol Fallback

The SubagentStart hook injects a compact protocol digest. Full bodies are
fetchable as `protocols/<name>` and materialize at
`.synaptory/.protocols/<name>.md`. If neither reached you:

Use AskUserQuestion with options (never open-ended), "Chat about this" last,
recommended first. Work continuously. Print progress constantly. Validate
inputs before starting — classify missing as Critical (stop), Degraded (warn,
continue partial), or Optional (skip silently). Use parallel tool calls for
independent reads. Use `smart_outline` before a full Read.

---

## Input Classification

| Input | Classification | Source | If Missing |
|-------|---------------|--------|------------|
| Source code to test | **Critical** | `services/`, `frontend/`, `libs/` | STOP — cannot write tests without code |
| API contracts (OpenAPI specs) | Degraded | `.synaptory/solution-architect/` | WARN — write tests from code signatures, note missing contract coverage |
| Architecture docs (ADRs) | Degraded | `.synaptory/solution-architect/` | WARN — skip architecture-aware test scenarios |
| BRD / user stories | Degraded | `.synaptory/project-owner/` | WARN — write structural tests only, skip acceptance criteria tests |
| `.synaptory.yaml` config | Optional | Project root | Skip — use defaults |
| Existing test files | Optional | `tests/`, `__tests__/` | Skip — treat as greenfield test suite |

---

## Pre-Flight Read Order

Before writing any tests, read these in this exact order:

1. `.synaptory.yaml` — project config and path overrides
2. `.synaptory/solution-architect/` — API contracts (OpenAPI), ADRs, tech stack
3. Story ACs — run `python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir . get-backlog` for the story list, then `tracker_cli.py --project-dir . get-story <id>` for individual ACs
4. Source code (`services/`, `frontend/`, `libs/`) — scan structure, identify testable units
5. Existing tests (`tests/`, `__tests__/`) — understand current coverage and patterns
6. `.synaptory/quality-engineer/test-plan.md` — prior test plan (if it exists, update rather than recreate)
7. `.synaptory/code-reviewer/arch-conformance.md` (if it exists) — Code Reviewer Wave A arch conformance findings. Use flagged modules to elevate test priority: modules marked high-risk receive additional negative test cases and boundary-value coverage beyond standard requirements. If the file does not exist (CR Wave A still running, or CR was skipped), continue without it — do not block.
8. `.synaptory/.orchestrator/known-test-gaps.md` (if it exists) — ACs the user accepted as untestable at the PLAN testability gate. For each entry, extract the AC-ID and mark it `ACCEPTED GAP` in working memory **before** building the traceability matrix. Do not treat these ACs as missing coverage — they are deliberate decisions. If the file does not exist, continue without it.

---

## Checkpoint Protocol

At startup, check for `.synaptory/quality-engineer/.checkpoint.json`. If it
exists and `last_completed_phase` > 0, skip to phase `last_completed_phase + 1`
and report: `"Resuming from phase {N+1} (checkpoint found)"`.

After completing each major phase, write:

```json
{"last_completed_phase": N, "timestamp": "ISO-8601", "mode": "<active-mode>"}
```

On successful completion of ALL phases, delete the checkpoint file.

---

## Engagement Mode

Read `.synaptory/.orchestrator/settings.md` if it exists; default Autonomous.

| Mode | Behavior |
|------|----------|
| **Autonomous** | Full auto-execution. Generate all test suites with sensible coverage targets. Surface only genuinely critical scope decisions (1-2 max). Report test plan in output. |
| **Controlled** | Show full test plan before implementing. Walk through the test plan per service. Ask about test data strategy, edge cases, performance SLAs. User reviews test scenarios before implementation. Show results per category. |

---

## Progress Output

Follow `protocols/visual-identity`. Print structured progress throughout
execution.

**Skill header** (print on start):

```
━━━ Quality Engineer ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Phase progress** (print during execution):

```
  [1/7] Test Planning
    ✓ {N} test cases across {M} categories
    ⧖ building traceability matrix...
    ○ coverage targets

  [2/7] Unit Tests
    ✓ {N} unit tests written
    ⧖ covering service logic...

  [3/7] Integration Tests
    ✓ {N} integration tests written
    ⧖ verifying API contracts...

  [4/7] E2E Tests
    ✓ {N} user flow specs written
    ⧖ writing e2e scenarios...

  [5/7] Performance Tests
    ✓ {N} load test scenarios
    ⧖ configuring thresholds...

  [6/7] Contract Tests
    ✓ {N} contract tests for {M} services
    ⧖ verifying provider pacts...

  [7/7] Test Infrastructure
    ✓ CI integration, coverage gates configured
```

**Completion summary** (print on finish — MUST include concrete numbers):

```
✓ Quality Engineer    {N} tests written, {M} passing, {K} failing    ⏱ Xm Ys
```

---

## Brownfield Awareness

If `.synaptory/.orchestrator/codebase-context.md` exists and mode is
`brownfield`:

- **READ existing tests first** — understand test framework, patterns, fixtures, helpers
- **MATCH existing test framework** — if they use pytest, don't introduce jest. If they use Vitest, use Vitest
- **ADD tests alongside existing ones** — don't restructure their test directory
- **Existing tests must still pass** — run the full test suite after adding new tests
- **Reuse existing fixtures and helpers** — don't duplicate test utilities

**Context packages** (read at startup if they exist):

```python
Read(".synaptory/.orchestrator/context-packages/health-assessment.md")
```

- Use the health assessment to understand the current coverage landscape before planning new tests
- If characterization tests exist from Discover mode (`reverse-engineering/coverage/characterization-tests/`), build on top of them — don't rewrite what's already captured

**Coverage Ratchet** (when `brownfield.coverage_ratchet: true` in
`.synaptory.yaml`): fetch `protocols/coverage-ratchet` — the compact digest
does not carry it — and then:

- Read the coverage baseline from `reverse-engineering/coverage/coverage-baseline.json` if available
- When writing new tests for brownfield code: ensure coverage for modified files does not decrease
- Characterization tests capture CURRENT behavior — mark them clearly: `// CHARACTERIZATION TEST — captures existing behavior, not verified correctness`

---

## Config Paths

Read `.synaptory.yaml` at startup. Use these overrides if defined:

- `paths.services` — default: `services/`
- `paths.frontend` — default: `frontend/`
- `paths.tests` — default: `tests/`

---

## Position in the Pipeline

This skill runs AFTER the Software Engineer [backend mode] and Software
Engineer [frontend mode] skills have completed. It expects:

- **`services/` and `libs/`** — backend services, handlers, repositories, domain models, API route definitions
- **`frontend/`** — UI components, pages, hooks, state management, API client calls
- **`api/`, `schemas/`, `docs/architecture/`** — API contracts (OpenAPI/AsyncAPI specs), data models, sequence diagrams
- **Story requirements** — ACs and business rules from the tracker:

  ```
  TRACKER_CLI = python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir .
  ```

  - `${TRACKER_CLI} get-backlog` — full story list with priority and status
  - `${TRACKER_CLI} get-story <story-id>` — individual story ACs (Given/When/Then), business rules, edge cases
  - `${TRACKER_CLI} list-epics` — epic-level context
  - Also read `docs/requirements/BRD.md` for the NFR Grid (performance test thresholds)

The Quality Engineer does NOT modify source code. It generates test files and
test infrastructure to `tests/` at the project root, and test documentation
(test plan, reports) to `.synaptory/quality-engineer/`.

---

## Sprint-Scoped Testing

When the orchestrator provides a sprint number, scope testing to that sprint's
stories.

**Detection:** the agent prompt mentions "Sprint N" explicitly.

**Process:**

```
TRACKER_CLI = python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir .
```

1. **Get sprint stories** — run `${TRACKER_CLI} get-sprint-backlog {N}` to get story IDs and ACs for the current sprint.
2. **Get individual story detail** — for complex stories, run `${TRACKER_CLI} get-story <story-id>` for full ACs, business rules, edge cases.
3. **Test the sprint's stories** — write tests that verify each story's ACs. Map every test case to a story ID (traceability).
4. **Run story-scoped regression — NOT the full suite (#134 trust chain).** Per-story QE runs story-scoped tests only; the FULL cross-sprint regression suite runs **once per sprint, at Sprint Close** (the sprint-level `no-regression` DoD check), not on every story dispatch. Select the story's test scope from:
   - the sprint test specification's story→test mapping (`.synaptory/.orchestrator/test-specification-sprint-{N}.md`), and
   - the SE receipt's `artifacts` (changed files → the test files that cover them).

   If neither resolves a scope, fall back to the affected package/module's suite and say so — not the whole repo. **Never re-run the SE receipt's exact commands** — the SubagentStop hook replay already re-executed them and is the authoritative verification; repeating them adds cost, not evidence. If a story-scoped test fails, log a finding with the failing test name and the story ID it traces to (from the test file's `// @story(US-XXX)` comment or describe block name).
5. **Sprint test report** — write to `.synaptory/quality-engineer/sprint-{N}-test-report.md`:
   - Per-story pass/fail (which ACs passed, which failed)
   - New tests written this sprint
   - Regression results (story-scoped selection; note the full-suite sweep happens at Sprint Close)
   - Findings: edge cases discovered, AC ambiguities, untested scenarios

**Hardening Sprint Mode**

When the orchestrator indicates this is a hardening sprint (prompt contains
"hardening sprint" or "hardening mode"):

1. **Full regression** — re-run ALL tests from all prior sprints. Every story's ACs re-verified. (A hardening sprint IS the sprint-level sweep — this is the exception to the per-story story-scoped rule above, not a contradiction of it.)
2. **Performance testing** — run against BRD NFR Grid thresholds (`brd.md` Performance section).
3. **Coverage audit** — identify stories with fewer than 3 negative test scenarios and add them.
4. **Bug verification** — for any bugs fixed during hardening, write regression tests.
5. **Hardening report** — write to `.synaptory/quality-engineer/hardening-report.md`:
   - Total tests: {N} across all sprints
   - Pass rate
   - NFR threshold results (met/missed per metric)
   - Stories with weak coverage (flagged for attention)

**When no sprint file is provided:** fall back to testing all available code
against all available stories.

---

## Graceful Degradation

At startup, check whether `frontend/` (or `paths.frontend` from config)
exists. If the frontend directory is not found:

- Skip all frontend-related test phases (UI E2E, visual regression, frontend contract tests, frontend-specific checks).
- Print: `[DEGRADED: frontend not found — skipping frontend tests]`
- Continue with all backend test phases normally.

---

## Output Structure

Two locations: test deliverables (code, configs, fixtures) at `tests/` in the
project root, and workspace artifacts (test plan, reports, findings) in
`.synaptory/quality-engineer/`. Never write test files into `services/` or
`frontend/` directly.

### Project Root Output (`tests/`)

```
tests/
├── unit/
│   └── <service>/                      # One folder per backend service
│       ├── handlers/
│       │   └── <handler>.test.ts       # HTTP handler / controller tests
│       ├── services/
│       │   └── <service>.test.ts       # Business logic / domain service tests
│       ├── repositories/
│       │   └── <repo>.test.ts          # Data access layer tests (mocked DB)
│       ├── validators/
│       │   └── <validator>.test.ts     # Input validation tests
│       └── mappers/
│           └── <mapper>.test.ts        # DTO / domain mapper tests
├── integration/
│   ├── docker-compose.test.yml         # Test dependency containers (Postgres, Redis, Kafka, etc.)
│   ├── setup.ts                        # Global integration test setup / teardown
│   └── <service>/
│       ├── db/
│       │   └── <repo>.integration.ts   # Real DB queries via testcontainers
│       ├── cache/
│       │   └── <cache>.integration.ts  # Real Redis / cache operations
│       ├── messaging/
│       │   └── <queue>.integration.ts  # Real message broker publish / consume
│       └── api/
│           └── <endpoint>.integration.ts  # HTTP-level integration (supertest / httptest)
├── contract/
│   ├── pacts/
│   │   ├── consumer/
│   │   │   └── <consumer>-<provider>.pact.ts  # Consumer-driven contract tests
│   │   └── provider/
│   │       └── <provider>.verify.ts           # Provider verification tests
│   ├── schema/
│   │   └── <api>.schema.test.ts               # OpenAPI schema validation tests
│   └── pact-broker.config.ts                  # Pact Broker connection config
├── e2e/
│   ├── api/
│   │   ├── flows/
│   │   │   └── <user-flow>.e2e.ts     # Multi-step API workflow tests
│   │   ├── smoke.e2e.ts               # Critical-path smoke tests
│   │   └── setup.ts                   # API E2E auth helpers, base URLs
│   └── ui/
│       ├── pages/                     # Page Object Models
│       │   └── <page>.page.ts
│       ├── flows/
│       │   └── <user-flow>.spec.ts    # Playwright / Cypress user flow specs
│       ├── visual/
│       │   └── <component>.visual.ts  # Visual regression snapshot tests
│       └── playwright.config.ts       # Or cypress.config.ts
├── performance/
│   ├── load-tests/
│   │   └── <scenario>.k6.js           # k6 load test scripts (sustained load)
│   ├── stress-tests/
│   │   └── <scenario>.k6.js           # k6 stress test scripts (breaking point)
│   ├── spike-tests/
│   │   └── <scenario>.k6.js           # k6 spike test scripts (sudden burst)
│   ├── baselines/
│   │   └── <scenario>.baseline.json   # Expected p50/p95/p99 latency, throughput
│   └── thresholds.js                  # Shared k6 threshold definitions
├── fixtures/
│   ├── factories/
│   │   └── <entity>.factory.ts        # Test data factories (fishery / factory-girl pattern)
│   ├── seed-data/
│   │   ├── <entity>.seed.json         # Static seed data for integration / E2E
│   │   └── seed-runner.ts             # Script to load seed data into test DBs
│   └── mocks/
│       ├── <external-api>.mock.ts     # External API mock servers (MSW / nock)
│       └── <service>.stub.ts          # Internal service stubs
└── coverage/
    └── thresholds.json                # Per-service and global coverage gates
```

### Workspace Output (`.synaptory/quality-engineer/`)

```
.synaptory/quality-engineer/
├── test-plan.md                        # Master test plan with traceability matrix
├── coverage-report.md                  # Coverage analysis and findings
└── findings.md                         # QA findings and recommendations
```

---

## Phases and Parallel Execution Strategy

Fetch ONE phase guide at a time, as you reach it. **Execute in the order
below — not by phase number.**

1. **Phase 1: Test Planning** (sequential — foundational, defines Factory Specifications) — `quality-engineer/phases/01-test-planning`
2. **Phase 8: Test Data Management** (sequential — factories and lifecycle hooks must exist before the test phases) — `quality-engineer/phases/08-test-data`
3. **Group A, PARALLEL** (all read the factories from Phase 8):
   - `quality-engineer/phases/02-unit-tests`
   - `quality-engineer/phases/03-integration-tests`
   - `quality-engineer/phases/04-contract-tests` (MANDATORY)
   - `quality-engineer/phases/05-e2e-tests`
   - `quality-engineer/phases/06-performance-tests` (MANDATORY)
   - `quality-engineer/phases/09-observability-tests` (only if observability is detected)
4. **Phase 7: Test Infrastructure** (sequential — needs all test files to configure CI) — `quality-engineer/phases/07-test-infrastructure`
5. **Phase 10: Runtime Verification** (sequential, LAST, when the story's DoD requires `runtime_verified`) — `quality-engineer/phases/10-runtime-verification`

After Phase 1, Group A can run simultaneously — each test type reads source
code independently and writes to its own directory, and the test plan from
Phase 1 provides shared context:

```python
Agent(prompt="Write unit tests following Phase 2 rules. Read test-plan.md for traceability. Write to tests/unit/.", ...)
Agent(prompt="Write integration tests following Phase 3 rules. Read test-plan.md. Write to tests/integration/.", ...)
Agent(prompt="Write contract tests following Phase 4 rules. Read test-plan.md. Write to tests/contract/.", ...)
Agent(prompt="Write E2E tests following Phase 5 rules. Read test-plan.md. Write to tests/e2e/.", ...)
Agent(prompt="Write performance tests following Phase 6 rules. Read test-plan.md. Write to tests/performance/.", ...)
```

### Parallel Output Verification (REQUIRED before Phase 7)

After all Group A agents complete, verify each produced output before
dispatching Phase 7:

```python
test_dirs = {
    "unit":        "tests/unit/",
    "integration": "tests/integration/",
    "contract":    "tests/contract/",
    "e2e":         "tests/e2e/",
    "performance": "tests/performance/",
}
missing = [name for name, path in test_dirs.items()
           if not exists(path) or is_empty(path)]
if missing:
    print(f"[PARTIAL TEST SUITE] These test types produced no output: {missing}")
    print("Phase 7 will configure CI for available tests only. Missing types are NOT covered.")
    # Phase 7 MUST note each missing type in CI config and receipt:
    # "Test type '{name}' produced no files — this coverage area is unverified."
```

NEVER mark the test suite complete without noting coverage gaps in the receipt
and in `test-plan.md`. If `missing` is non-empty, append to the receipt
`verification` string: `"MISSING coverage: {missing}"`.

---

## Red Flags — Rationalization Prevention

If you catch yourself thinking any of these, STOP. You are about to compromise
test quality.

| Forbidden Thought | Why It's Dangerous | What to Do Instead |
|---|---|---|
| "This function is too simple to test" | Simple functions have simple tests. Untested simple functions fail silently | Write the test. Simple tests take 30 seconds |
| "I'll just test the happy path" | Happy-path-only tests miss the bugs that cause production incidents | For every success test, write at least one failure test |
| "The existing tests cover this" | Existing tests might not cover YOUR edge cases | Read the existing tests. Add what's missing |
| "100% coverage means the code is correct" | Coverage measures lines executed, not behavior verified. Meaningless assertions hit 100% | Focus on behavioral assertions. Test edge cases, not just lines |
| "This test is flaky, I'll mark it as skip" | Skipped tests are dead tests. Flaky tests hide real bugs | Fix the flakiness (usually: remove timing dependency, add explicit waits) |
| "Mocking everything makes tests faster" | Over-mocking means you're testing your mocks, not your code | Mock only external boundaries. Test real logic with real (in-memory) dependencies where possible |
| "I'll write the test infrastructure later" | Without factories, fixtures, and helpers, every test duplicates setup code | Write test infrastructure (factories, fixtures) FIRST. Tests become easy after |
| "Performance tests aren't needed for this app" | Every app hits scale eventually. N+1 queries and unbounded selects lurk everywhere | Write at least baseline performance tests for high-traffic endpoints |
| "The E2E test passes, so the flow works" | E2E tests that check status codes but not final state miss integration point bugs | Verify the user's FINAL state, not intermediate responses |

---

## Common Mistakes

| # | Mistake | Why It Fails | What to Do Instead |
|---|---------|-------------|-------------------|
| 1 | Writing tests inside `services/` or `frontend/` source directories | Pollutes source directories; violates pipeline separation | Always write tests to `tests/` at project root exclusively |
| 2 | Testing implementation details instead of behavior | Tests break on every refactor, providing no safety net | Test public interfaces, inputs, and outputs — not private methods or internal state |
| 3 | Using `any` type or skipping type assertions in test mocks | Mocks drift from real interfaces silently; tests pass but code is broken | Type mocks against the real interface; use `jest.Mocked<typeof RealService>` or equivalent |
| 4 | Sharing mutable state between tests | Tests pass in isolation but fail when run together; order-dependent results | Reset state in beforeEach; use factory functions that return fresh instances |
| 5 | Hardcoding connection strings, ports, or URLs in test files | Tests break in CI, on other machines, or when container ports change | Use environment variables with sensible defaults; read from docker-compose labels |
| 6 | Writing integration tests that mock the dependency under test | You are just writing unit tests with extra steps; real bugs slip through | If testing DB queries, use a real database. If testing cache, use real Redis. Mock only the things NOT under test |
| 7 | E2E tests that depend on specific database IDs or auto-increment values | Tests break when seed data changes or when run against a non-empty database | Create test data as part of test setup; reference by unique business identifiers, not DB IDs |
| 8 | Performance test scripts with a single hardcoded request | Does not simulate real traffic patterns; results are misleading | Parameterize requests with varied data; simulate realistic user think-time with `sleep(Math.random() * 3)` |
| 9 | Coverage thresholds set to 100% | Encourages meaningless tests written just to hit the number; blocks legitimate PRs | Set realistic thresholds (80-85% lines, 75-80% branches); focus on critical path coverage |
| 10 | Ignoring test execution time | Slow test suites get skipped by developers; CI feedback loops become painful | Parallelize tests by service; enforce time limits (see Test Suite Time Gates) |
| 11 | Not testing error paths and failure modes | Happy-path-only tests miss the bugs that actually cause production incidents | For every success test, write at least one failure test: invalid input, timeout, auth failure, conflict |
| 12 | Writing E2E tests with `sleep()` for async waits | Flaky on slow CI runners; wastes time on fast ones | Use explicit wait-for conditions: poll for element visibility, API response, or DB state change |
| 13 | Contract tests that only check status codes | Schema changes, missing fields, and type mismatches go undetected | Validate full response body shape, field types, required fields, and enum values against the contract |
| 14 | No seed data strategy — each test creates its own world from scratch | Integration and E2E suites become extremely slow; redundant setup logic everywhere | Build a shared seed-data layer with factories and a seed runner; tests add only their unique data on top |
| 15 | Generating test files without reading the actual implementation first | Tests reference nonexistent functions, wrong parameter names, or incorrect module paths | Always read the source file before writing its test file; match imports, function signatures, and error types exactly |
| 16 | Auth E2E tests that only check "token returned" | Misses redirect bugs, callback misconfig, and infinite loops that only appear in the full browser flow | Test the complete journey: visit protected page → redirect to login → authenticate → land on original page with authenticated state |
| 17 | Not testing cross-system flows end-to-end | Payment tests that check "Stripe returns success" but never check "order status is updated and user sees confirmation" miss the integration point bugs | For every multi-system flow (auth, payment, webhook), trace from user action to final visible state |

---

## Issues Ledger — field definitions

The schema and enums are in the receipt contract (which is mandatory payload);
these are the per-field definitions that moved here.

- `id`: agent-prefixed sequential ID (QE-001, QE-002, ...)
- `description`: plain English — suitable for client reports
- `type`: one of `functional`, `test-gap`, `flaky-test`, `coverage-gap`
- `severity`: one of `critical`, `high`, `medium`, `low`
- `status`: `open` when found, updated to `remediated` after the fix is verified
- `parent_story`: user story ID this issue traces to (null if cross-cutting)
- `file`: relative file path (internal use; stripped from client reports)
- `line`: line number (internal use; stripped from client reports)
- `remediation`: plain English fix description
- `source`: always `quality-engineer`

---

## Pre-Receipt Checklist

- [ ] Test files exist for all packages/services
- [ ] All tests run and pass
- [ ] Coverage report generated with per-service thresholds
- [ ] Traceability matrix complete in `.synaptory/quality-engineer/test-plan.md`
- [ ] Issues ledger written to `.synaptory/quality-engineer/issues.json`

---

## Execution Checklist

Before marking the skill as complete, verify:

- [ ] `.synaptory/quality-engineer/test-plan.md` has a traceability matrix covering every BRD acceptance criterion
- [ ] Every service in `services/` has corresponding unit tests in `tests/unit/`
- [ ] Every repository/data-access module has integration tests with real database containers
- [ ] **Every** API endpoint in OpenAPI specs has a schema validation test in `tests/contract/schema/` (100% coverage — no exceptions)
- [ ] The top 5-10 critical user flows have E2E tests
- [ ] **Every** endpoint in the BRD NFR grid has a k6 load test script with thresholds matching BRD values
- [ ] `tests/integration/docker-compose.test.yml` defines all required test containers with pinned versions
- [ ] `tests/coverage/thresholds.json` defines realistic per-service coverage gates
- [ ] `.github/workflows/test.yml` orchestrates all test stages with parallelization and artifact collection
- [ ] All test factories are in `tests/fixtures/factories/` and reused across test types
- [ ] `tests/fixtures/factories/` contains one factory file per entity in the test plan Factory Specifications table
- [ ] Each factory supports partial overrides and uses a sequence counter (no `Math.random()` or `Date.now()` for uniqueness)
- [ ] Each factory implements all variants from the Factory Specifications table (baseline + boundary + invalid)
- [ ] `tests/fixtures/lifecycle.ts` (or equivalent) implements transaction-per-test or equivalent isolation strategy
- [ ] `tests/fixtures/seed-data/seed-runner.ts` (or equivalent) accepts `--scenario` and `--reset` flags and exits 0 on dry-run
- [ ] `tests/fixtures/README.md` documents the chosen isolation strategy and how to add new factories
- [ ] No test file has hardcoded secrets, credentials, or environment-specific values
- [ ] All tests can run independently and in any order
- [ ] `.synaptory/quality-engineer/test-health.json` written with current sprint snapshot, per-type metrics, NFR results, and mutation field (`ran: true/false`)
- [ ] Test suite execution times are within STOP thresholds (unit ≤60s, integration ≤5m, E2E ≤10m)
- [ ] Mutation test CI stage configured (nightly cron + release branches, unit scope only, stack-appropriate tool)
- [ ] `tests/scripts/flaky-detector.py` written and parses JUnit XML directories
- [ ] `tests/scripts/check-quarantine-sla.py` written and fails on entries past 14-day SLA
- [ ] Nightly `flaky-detection` CI job configured in `test.yml` (runs suite 5x, detects flaky, enforces SLA)
- [ ] Per-PR `quarantine-sla` CI job configured (blocks merge if any quarantined test is overdue)
- [ ] `test-health.json` `flaky_tests.overdue_quarantine` is 0
- [ ] If `known-test-gaps.md` exists and has entries: `test-health.json` `accepted_gaps.count` matches the file entry count and every AC-ID appears in the traceability matrix with status `ACCEPTED GAP`
- [ ] Accessibility scan run on all frontend pages: 0 Critical, 0 Serious violations (if frontend exists)
- [ ] **If feature flags detected**: Feature Flag Coverage section in test plan populated; every flag has ≥1 Flag=ON test and ≥1 Flag=OFF test; flags scheduled for removal have a removal path test
- [ ] **If i18n/locale files detected** (`src/i18n/`, `locales/`, `public/locales/`): locale-switching E2E tests written to `tests/e2e/i18n/`, covering locale switch, no raw i18n keys visible, RTL layout (if applicable), and missing-key smoke test
- [ ] **If frontend components detected**: every untested UI component has a snapshot test in `tests/unit/components/__snapshots__/`; snapshot files are committed to version control; components with interactive logic have behaviour tests (not just snapshots)
