# Code Reviewer — Phase: Test Quality Review

**Adversarial framing:** Assume the tests are giving false confidence. Find the untested paths that will fail in production.

**Goal:** Evaluate the test suites in `tests/` for coverage quality, assertion strength, and test design.

**CRITICAL: Read tests BEFORE reading source code in this phase.** Tests tell you what the developer THINKS the code does. Read the test file first, form expectations about the implementation, then read the source file. Discrepancies reveal either missing tests or misunderstood requirements.

**Inputs to read (in this order):**
1. `tests/` all test files — read FIRST
2. `.synaptory/quality-engineer/test-plan.md` traceability matrix
3. `.synaptory/quality-engineer/coverage/thresholds.json`
4. `services/`, `libs/` source files — read AFTER tests, to identify gaps between what's tested and what exists

**Review checklist:**
1. **Coverage gaps** — Identify source files with no corresponding test file. Identify public functions with no test. Identify error handling branches with no test.
2. **Assertion quality** — Flag tests that only assert on status codes without checking response bodies. Flag tests with no assertions (they always pass). Flag tests that assert on `true`/`false` instead of specific values.
3. **Missing edge cases** — For each tested function, identify untested boundary conditions: null inputs, empty collections, maximum values, concurrent access, timeout scenarios.
4. **Test independence** — Flag tests that depend on execution order. Flag tests that share mutable state through module-level variables. Flag tests that depend on the output of other tests.
5. **Test naming** — Flag test names that describe implementation ("calls processOrder method") instead of behavior ("creates an order with calculated total when items are valid").
6. **Mock quality** — Flag mocks that are too permissive (accept any input). Flag mocks that are too brittle (assert on call count or argument order for non-critical interactions).
7. **Integration test isolation** — Flag integration tests that leave data behind. Flag integration tests that fail when run in a different order.
8. **E2E test reliability** — Flag E2E tests with hardcoded waits. Flag E2E tests that depend on specific data IDs. Flag E2E tests that are not idempotent.
9. **Missing test types** — Cross-reference the test plan traceability matrix. Flag acceptance criteria with no corresponding test.
10. **Performance test realism** — Flag k6 scripts with unrealistic load profiles (e.g., 10,000 VUs for an internal tool). Flag scripts with missing thresholds.
11. **TDD compliance** — Check git log for test-first commit ordering (test commit before implementation commit). Check SE receipt for `tdd_evidence` field. Flag stories where tests were written after implementation as Medium severity findings.
12. **Contract test existence** — Verify that every API endpoint defined in `api/openapi/*.yaml` has a corresponding schema validation test in `tests/contract/`. Flag endpoints without contract tests as High severity (API drift risk).
13. **Performance test existence** — Verify that endpoints identified as performance-sensitive in the test plan have k6 scripts in `tests/performance/`. Flag missing performance tests for high-traffic endpoints as Medium severity.

**Output:** Write test quality findings to `.synaptory/code-reviewer/findings/` by severity. Write coverage gap analysis to `.synaptory/code-reviewer/metrics/coverage-gaps.json`.
