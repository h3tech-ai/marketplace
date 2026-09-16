# Code Reviewer — Phase: Review Report

> **Anchor: Final phase. Compile findings — do NOT add new analysis. Do NOT modify source code.**

**Goal:** Compile all findings into a structured, actionable review report. Generate auto-fix suggestions for issues where the fix is unambiguous.

**Inputs:**
- All findings from the architecture, code quality, performance, and test quality phases
- All metrics from those phases

**Actions:**

1. Write `.synaptory/code-reviewer/review-report.md` with the following sections:
   - **Executive Summary** — Total finding count by severity. Overall assessment (Pass / Pass with Conditions / Fail). Top 3 most critical issues.
   - **Findings by Category** — Architecture, Code Quality, Performance, Test Quality. Each finding includes: ID, severity, category, location (file + line), description, impact, and recommended fix.
   - **Metrics Summary** — Cyclomatic complexity distribution, coverage gap summary, dependency health.
   - **Recommendations** — Prioritized list of actions. What to fix now, what to fix next sprint, what to add to tech debt backlog.
   - **Sign-off Criteria** — Conditions that must be met before this review is considered passed: all Critical findings resolved, all High findings resolved or accepted with justification.

2. Write individual findings files to `.synaptory/code-reviewer/findings/`:
   - `critical.md` — Findings that block deployment
   - `high.md` — Findings that must be fixed before production
   - `medium.md` — Findings that should be fixed soon
   - `low.md` — Advisory findings

   Each finding in these files uses the following format:
   ```markdown
   ### [FINDING-ID] Short description

   **Severity:** Critical | High | Medium | Low
   **Category:** Architecture | Code Quality | Performance | Test Quality
   **Location:** `path/to/file.ts:42`

   **Description:**
   What the issue is and why it matters.

   **Impact:**
   What happens if this is not fixed.

   **Evidence:**
   ```code
   // The problematic code
   ```

   **Recommendation:**
   How to fix it, with a code example if applicable.
   ```

3. Generate auto-fix suggestions for findings where the fix is mechanical and unambiguous:
   - Missing null checks
   - Missing auth middleware
   - Missing input validation
   - Missing error handling
   - Unused imports
   - Missing index definitions

   Write each auto-fix to `.synaptory/code-reviewer/auto-fixes/<service>/<file>.patch.md` with:
   - Finding ID reference
   - Before code block
   - After code block
   - Explanation of the change

4. Compile metrics:
   - `.synaptory/code-reviewer/metrics/complexity.json` — Cyclomatic complexity per function, flagged functions with complexity > 10
   - `.synaptory/code-reviewer/metrics/coverage-gaps.json` — List of untested files, untested functions, untested branches
   - `.synaptory/code-reviewer/metrics/dependency-analysis.json` — Service dependency graph, coupling score per service, circular dependency detection

**Output:** Write all report files, findings, metrics, and auto-fixes to `.synaptory/code-reviewer/`.
