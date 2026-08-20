> **Anchor: You are the Quality Engineer. Configure CI test execution and coverage enforcement. Do not modify application code or infrastructure — test infrastructure only.**

### Phase 7 — Test Infrastructure

**Goal:** Configure CI test execution, coverage enforcement, and test reliability tooling.

**Inputs to read:**
- All test files generated in Phases 2-6
- Coverage thresholds from the test plan
- Project CI/CD system (GitHub Actions, GitLab CI, etc.)

**Actions:**
1. Write `tests/coverage/thresholds.json` with per-service and global coverage gates:
   ```json
   {
     "global": { "lines": 80, "branches": 75, "functions": 80, "statements": 80 },
     "services": {
       "<service-name>": { "lines": 85, "branches": 80, "functions": 85, "statements": 85 }
     }
   }
   ```
2. Write `.github/workflows/test.yml` (or `ci/test-config.yml`) with:
   - **Unit test stage** — runs first, fast, no containers. Fails fast on coverage threshold breach.
   - **Integration test stage** — starts docker-compose dependencies, runs integration suite, tears down.
   - **Contract test stage** — runs Pact tests, publishes results to broker.
   - **E2E test stage** — deploys to test environment, runs smoke + full E2E suite.
   - **Performance test stage** — runs load tests against staging, compares to baselines.
   - Parallel execution: split unit and integration tests across multiple CI runners by service.
   - Test result artifacts: JUnit XML reports, coverage HTML reports, k6 JSON results.
   - Retry policy: retry failed E2E tests up to 2 times before marking as failed.
   - Flaky test detection: see step 2a below — implement the full flaky detection pipeline, not just a retry policy.
   - **Mutation test stage** — nightly or release-branch only (not per-PR). Detect stack and configure:
     - TypeScript/JavaScript: [Stryker](https://stryker-mutator.io/) with `stryker.config.json`
     - Python: [mutmut](https://mutmut.readthedocs.io/) with `setup.cfg` `[mutmut]` section
     - Go: [go-mutesting](https://github.com/zimmski/go-mutesting)
     Scope: unit tests only — do NOT run mutants against integration or E2E suites (too slow).
     Thresholds (report in `test-health.json` `mutation` field):
     - Mutation score < 40%: **BLOCK** — unit tests are not exercising enough code paths
     - Mutation score 40–60%: **WARN** — flag per-service in QE report as Medium severity
     - Mutation score > 60%: **PASS**
     CI trigger: schedule nightly (`cron: '0 2 * * *'`) plus on push to release branches. Never block feature branch PRs.
     Write mutation results per service to `tests/mutation/results-{service}.json`.
2a. **Flaky Test Detection Pipeline:**

   Write `tests/scripts/flaky-detector.py` — a Python script that parses JUnit XML history and identifies flaky tests:

   ```python
   #!/usr/bin/env python3
   """Flaky test detector.

   Parses a directory of JUnit XML files (multiple runs of the same test suite)
   and identifies tests with a failure rate >= the configured threshold.

   Usage:
     python3 flaky-detector.py <reports-dir> [--threshold 0.05] [--output tests/reports/flaky-tests.json]
   """
   import argparse
   import json
   import os
   import xml.etree.ElementTree as ET
   from collections import defaultdict
   from datetime import datetime, timezone

   def parse_junit(xml_path: str) -> list[dict]:
       """Parse a JUnit XML file and return a list of test results."""
       tree = ET.parse(xml_path)
       root = tree.getroot()
       results = []
       suites = root.findall('.//testcase')
       for tc in suites:
           results.append({
               "classname": tc.get("classname", ""),
               "name": tc.get("name", ""),
               "key": f"{tc.get('classname', '')}.{tc.get('name', '')}",
               "failed": tc.find("failure") is not None or tc.find("error") is not None,
               "skipped": tc.find("skipped") is not None,
           })
       return results

   def detect_flaky(reports_dir: str, threshold: float = 0.05) -> list[dict]:
       xml_files = [f for f in os.listdir(reports_dir) if f.endswith(".xml")]
       if not xml_files:
           return []

       runs: dict[str, list[bool]] = defaultdict(list)
       for xml_file in sorted(xml_files):
           results = parse_junit(os.path.join(reports_dir, xml_file))
           for r in results:
               if not r["skipped"]:
                   runs[r["key"]].append(r["failed"])

       flaky = []
       for key, outcomes in runs.items():
           if len(outcomes) < 5:
               continue  # Not enough data
           fail_rate = sum(outcomes) / len(outcomes)
           if fail_rate >= threshold:
               flaky.append({
                   "test_name": key.split(".")[-1],
                   "classname": ".".join(key.split(".")[:-1]),
                   "fail_rate": round(fail_rate, 3),
                   "total_runs": len(outcomes),
                   "failures": sum(outcomes),
                   "detected_at": datetime.now(timezone.utc).isoformat(),
               })

       return sorted(flaky, key=lambda x: -x["fail_rate"])

   if __name__ == "__main__":
       parser = argparse.ArgumentParser()
       parser.add_argument("reports_dir")
       parser.add_argument("--threshold", type=float, default=0.05)
       parser.add_argument("--output", default="tests/reports/flaky-tests.json")
       args = parser.parse_args()

       flaky = detect_flaky(args.reports_dir, args.threshold)
       os.makedirs(os.path.dirname(args.output), exist_ok=True)
       with open(args.output, "w") as f:
           json.dump(flaky, f, indent=2)

       print(f"Detected {len(flaky)} flaky tests (threshold: {args.threshold*100:.0f}%)")
       for t in flaky:
           print(f"  {t['fail_rate']*100:.1f}%  {t['test_name']}")
   ```

   Write `tests/scripts/check-quarantine-sla.py` — fails CI if any quarantined test is past its 2-sprint SLA:

   ```python
   #!/usr/bin/env python3
   """Quarantine SLA enforcer.

   Reads tests/reports/flaky-tests.json and fails if any entry has been
   quarantined for more than 2 sprints (14 days default).

   Usage:
     python3 check-quarantine-sla.py [--sla-days 14]
   """
   import json
   import sys
   import argparse
   from datetime import datetime, timezone, timedelta

   parser = argparse.ArgumentParser()
   parser.add_argument("--sla-days", type=int, default=14)
   parser.add_argument("--registry", default="tests/reports/flaky-tests.json")
   args = parser.parse_args()

   try:
       with open(args.registry) as f:
           flaky = json.load(f)
   except FileNotFoundError:
       print("No flaky-tests.json found — OK")
       sys.exit(0)

   now = datetime.now(timezone.utc)
   cutoff = now - timedelta(days=args.sla_days)
   overdue = []

   for test in flaky:
       since_str = test.get("quarantined_since")
       if not since_str:
           continue
       since = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
       if since < cutoff:
           overdue.append(test)

   if overdue:
       print(f"ERROR: {len(overdue)} quarantined test(s) past {args.sla_days}-day SLA:")
       for t in overdue:
           print(f"  [{t.get('quarantined_since', 'unknown date')}] {t['test_name']} — {t.get('tracking_issue', 'no issue')}")
       print("Fix or permanently remove these tests before merging.")
       sys.exit(1)

   print(f"Quarantine SLA OK — {len(flaky)} quarantined, 0 overdue")
   ```

   Add the nightly flaky-detection job to `.github/workflows/test.yml`:
   ```yaml
   flaky-detection:
     if: github.event_name == 'schedule'
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - name: Install dependencies
         run: npm ci
       - name: Run test suite 5 times to collect history
         run: |
           mkdir -p tests/reports/flaky-runs
           for i in 1 2 3 4 5; do
             npx jest --reporters=jest-junit --testResultsProcessor=jest-junit \
               2>/dev/null || true
             mv junit.xml tests/reports/flaky-runs/run-$i.xml || true
           done
       - name: Detect flaky tests
         run: python3 tests/scripts/flaky-detector.py tests/reports/flaky-runs/ \
           --output tests/reports/flaky-tests.json
       - name: Enforce quarantine SLA
         run: python3 tests/scripts/check-quarantine-sla.py
       - name: Upload flaky report
         if: always()
         uses: actions/upload-artifact@v4
         with:
           name: flaky-test-report
           path: tests/reports/flaky-tests.json
   ```

   Also add the quarantine SLA check to the standard per-PR test run (catches overdue tests on every PR):
   ```yaml
   quarantine-sla:
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - name: Check quarantine SLA
         run: python3 tests/scripts/check-quarantine-sla.py
         # Fails if any test in flaky-tests.json is past its 2-sprint deadline
   ```

2b. **Parallel Test Sharding:**

   Distribute slow test suites across CI runners to keep total wall-clock time under budget.

   **Jest (TypeScript/JavaScript):**
   Add a `shard` matrix to `.github/workflows/test.yml`:
   ```yaml
   unit-tests:
     strategy:
       matrix:
         shard: [1, 2, 3, 4]
     runs-on: ubuntu-latest
     env:
       TEST_RUN_ID: ${{ github.run_id }}-shard-${{ matrix.shard }}
     steps:
       - uses: actions/checkout@v4
       - run: npm ci
       - run: npx jest --shard=${{ matrix.shard }}/4 --reporters=jest-junit
         env:
           JEST_JUNIT_OUTPUT_FILE: tests/reports/junit-shard-${{ matrix.shard }}.xml
       - uses: actions/upload-artifact@v4
         if: always()
         with:
           name: junit-shard-${{ matrix.shard }}
           path: tests/reports/junit-shard-*.xml
   ```
   Merge sharded JUnit results in a post-job:
   ```yaml
   merge-results:
     needs: [unit-tests]
     runs-on: ubuntu-latest
     steps:
       - uses: actions/download-artifact@v4
         with: { pattern: junit-shard-*, merge-multiple: true, path: tests/reports/ }
       - run: npx junit-merge tests/reports/junit-shard-*.xml -o tests/reports/junit-merged.xml
   ```

   **Playwright (E2E):**
   ```yaml
   e2e-tests:
     strategy:
       matrix:
         shard: [1/4, 2/4, 3/4, 4/4]
     runs-on: ubuntu-latest
     env:
       TEST_RUN_ID: ${{ github.run_id }}-${{ matrix.shard }}
     steps:
       - uses: actions/checkout@v4
       - run: npm ci && npx playwright install --with-deps
       - run: npx playwright test --shard=${{ matrix.shard }}
   ```

   **pytest (Python):**
   ```yaml
   unit-tests:
     strategy:
       matrix:
         group: [1, 2, 3, 4]
     steps:
       - run: pip install pytest-split
       - run: pytest --splits 4 --group ${{ matrix.group }} --junitxml=tests/reports/junit-${{ matrix.group }}.xml
         env:
           TEST_RUN_ID: ${{ github.run_id }}-${{ matrix.group }}
   ```

   **`TEST_RUN_ID` isolation rule:** Every test file that creates database rows, cache keys, or filesystem paths MUST namespace them with `process.env.TEST_RUN_ID` (or equivalent). Without this, shards running on the same host collide. Example:
   ```ts
   const uniqueEmail = `test-${process.env.TEST_RUN_ID ?? 'local'}-${counter}@example.com`;
   ```

2c. **Smart Test Selection (PR-scoped):**

   Feature branch PRs should run only tests affected by the change, not the full suite. Full suite runs only on `main`/release branches.

   Add a `pr-tests` job to `.github/workflows/test.yml` that runs alongside (not replacing) the standard stages when triggered by a PR:

   **Jest (TypeScript/JavaScript):**
   ```yaml
   pr-unit-tests:
     if: github.event_name == 'pull_request'
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - run: npm ci
       - name: Run tests for changed files only
         run: |
           CHANGED=$(git diff --name-only origin/${{ github.base_ref }}...HEAD | tr '\n' ' ')
           npx jest --findRelatedTests $CHANGED --passWithNoTests --reporters=jest-junit
         env:
           JEST_JUNIT_OUTPUT_FILE: tests/reports/junit-pr.xml
   ```

   **pytest (Python):**
   ```yaml
   pr-unit-tests:
     if: github.event_name == 'pull_request'
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - run: pip install pytest pytest-testmon
       - name: Run only affected tests
         run: pytest --testmon --junitxml=tests/reports/junit-pr.xml
   ```
   Note: `pytest-testmon` maintains a `.testmondata` file to track which tests cover which source files. Commit `.testmondata` so CI has a baseline.

   **Go:**
   ```yaml
   pr-unit-tests:
     if: github.event_name == 'pull_request'
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - name: Run tests for changed packages only
         run: |
           CHANGED_PKGS=$(git diff --name-only origin/${{ github.base_ref }}...HEAD \
             | grep '\.go$' \
             | xargs -I{} dirname {} \
             | sort -u \
             | xargs -I{} echo "./{}" \
             | tr '\n' ' ')
           if [ -n "$CHANGED_PKGS" ]; then
             go test $CHANGED_PKGS -count=1 -timeout 60s
           else
             echo "No Go files changed"
           fi
   ```

   **Rule:** Smart test selection runs in ADDITION to the full suite (not instead). PR gets fast feedback from smart selection; merge to main always triggers full suite.

2d. **Test Code Quality Linting:**

   Test files must pass linting checks for common quality anti-patterns. Add a `test-lint` step to `.github/workflows/test.yml` before the unit test stage:

   **Jest/Vitest (TypeScript/JavaScript):**
   Install `eslint-plugin-jest` (or `eslint-plugin-vitest` for Vitest projects):
   ```bash
   npm install --save-dev eslint-plugin-jest
   ```
   Add to `eslint.config.js` (or `.eslintrc`) — applies to `**/*.test.*` and `**/*.spec.*` files only:
   ```js
   // For test files only
   {
     files: ['**/*.test.ts', '**/*.test.js', '**/*.spec.ts', '**/*.spec.js'],
     plugins: { jest: require('eslint-plugin-jest') },
     rules: {
       'jest/no-disabled-tests': 'error',      // No skipped tests (.skip, xit, xdescribe)
       'jest/no-focused-tests': 'error',        // No focused tests (.only, fit, fdescribe)
       'jest/expect-expect': 'error',           // Every test must have at least one assertion
       'jest/valid-expect': 'error',            // expect() must be called with a matcher
       'jest/no-standalone-expect': 'warn',     // expect() outside of test/it blocks
     }
   }
   ```

   CI step:
   ```yaml
   test-lint:
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - run: npm ci
       - run: npx eslint 'tests/**/*.{ts,js}' 'src/**/*.{test,spec}.{ts,js}' --max-warnings=0
   ```

   **pytest (Python):**
   Install `flake8-pytest-style`:
   ```bash
   pip install flake8-pytest-style
   ```
   Add to `setup.cfg` or `.flake8`:
   ```ini
   [flake8]
   per-file-ignores =
     tests/**/*.py: E501
   select = PT  # flake8-pytest-style rules only for test files
   ```
   Key rules: `PT009` (no unittest-style assertions), `PT011` (avoid bare `pytest.raises`), `PT018` (no compound assertions).

   CI step:
   ```yaml
   test-lint:
     runs-on: ubuntu-latest
     steps:
       - uses: actions/checkout@v4
       - run: pip install flake8 flake8-pytest-style
       - run: flake8 tests/ --select=PT
   ```

   **Go:** `go vet ./...` + `staticcheck ./...` cover test quality automatically — no separate step needed.

3. Write seed data runner to `tests/fixtures/seed-data/seed-runner.ts`.
4. Write external API mock configurations to `tests/fixtures/mocks/`.

5. **Test Debt Stories (conditional):**

   After generating `tests/coverage/thresholds.json`, check which service modules fall below threshold. For each under-covered module, create a tracker story:

   ```python
   # Read coverage thresholds from thresholds.json
   # Read actual coverage from last test run (jest --coverage --json, pytest-cov --json, go cover)
   # For each service where actual_coverage < threshold:

   existing = tracker_cli.py query "title:[Test Debt]*{module}*"
   if not existing:
       tracker_cli.py create-story \
         --title "[Test Debt] Improve coverage for {module} ({actual}% → {threshold}%)" \
         --description "Current coverage: {actual}%. Required: {threshold}%. Priority based on risk tier." \
         --tags "test-debt,quality" \
         --priority "{P1→High, P2→Medium, P3→Low based on risk_tier from coverage-ratchet baseline}"
   ```

   Risk tier source: read `.synaptory/.orchestrator/coverage-baseline.json` if it exists (written by coverage-ratchet protocol). If not present, default all modules to P2 (Medium).

   Do NOT create stories for modules that already meet their threshold.

**Output:** Write CI config to `.github/workflows/test.yml`, coverage thresholds and test infrastructure to `tests/`.
