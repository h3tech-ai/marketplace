# Code Reviewer — Phase: Code Quality Analysis

**Adversarial framing:** Assume every function has a bug. Look for the edge case the author was too close to the code to see.

**Goal:** Evaluate code against software engineering best practices. Identify structural issues that static analysis tools typically miss.

**Inputs to read:**
- `services/`, `libs/` all backend source files
- `frontend/` all frontend source files

**Review checklist:**

**SOLID Principles:**
1. **Single Responsibility** — Does each class/module have one reason to change? Flag god-classes and god-functions (functions > 50 lines, classes > 300 lines).
2. **Open/Closed** — Are extension points used (interfaces, strategy pattern) or is behavior added via if/else chains and switch statements?
3. **Liskov Substitution** — Do subclasses/implementations honor the contracts of their base types? Are there type-check downcasts that violate polymorphism?
4. **Interface Segregation** — Are interfaces focused? Flag interfaces with > 7 methods that force implementors to stub unused methods.
5. **Dependency Inversion** — Do high-level modules depend on abstractions? Flag direct instantiation of infrastructure dependencies (new DatabaseClient()) in business logic.

**Code Structure:**
6. **DRY violations** — Identify duplicated logic (not just duplicated strings). Business rules implemented in multiple places are high-severity findings. **Threshold: ≤5% code duplication ratio** (measured as duplicated lines / total lines across the codebase). Report the ratio in `metrics/complexity.json`. Duplication >5% is a **High** severity finding. Duplication >15% is a **Critical** finding (indicates systemic copy-paste culture).
7. **Cyclomatic complexity** — Flag functions with complexity > 10. Calculate and record in `metrics/complexity.json`.
8. **Naming conventions** — Are names consistent, intention-revealing, and following language idioms? Flag abbreviations, single-letter variables (outside loops), and misleading names.
9. **Error handling** — Are errors caught at the right level? Flag swallowed exceptions (empty catch blocks), generic catches (`catch (e: any)`), and errors that lose stack traces.
10. **Logging** — Is logging structured (JSON)? Are appropriate levels used (error for errors, warn for degraded, info for business events, debug for troubleshooting)? Are sensitive fields redacted?

**Frontend-Specific:**
11. **Component size** — Flag components > 200 lines. Identify components that mix data fetching, business logic, and presentation.
12. **State management** — Is state lifted to the appropriate level? Flag prop drilling > 3 levels. Flag global state used for local concerns.
13. **Effect management** — Flag useEffect with missing dependencies, effects that should be event handlers, and effects without cleanup for subscriptions/timers.
14. **Accessibility** — Flag interactive elements without ARIA labels, images without alt text, forms without labels, and missing keyboard navigation.

**Boundary Safety** (see `boundary-safety.md` protocol):
15. **Framework abstraction misuse** — Flag `<Link>` / `navigate()` / router-based navigation targeting API routes (`/api/*`), external URLs, OAuth endpoints, or file downloads. These need raw `<a href>` or `window.location`.
16. **Duplicated control flow** — Flag UI code that manually checks auth state and redirects when middleware/guards already handle it. Flag links pointing to auth/error endpoints instead of protected destinations.
17. **Self-referencing configuration** — Flag auth config overrides (signIn, error pages) that point back to the framework's default handler. Compare override values against known defaults.
18. **Unconditional global interceptors** — Flag auth callbacks, API interceptors, or error handlers that return a hardcoded value without branching on input parameters (url, request, error type).
19. **Identity consistency** — Flag mismatched identity formats across integrated systems (OAuth provider email vs app username, local git email vs CI/CD expected email, staging tokens in production config).
20. **Dead interactive elements** — Flag buttons with empty/missing onClick, links with empty/missing href, forms with empty/missing onSubmit. Every interactive element that renders MUST be wired to a real action. Dead elements are Critical findings.
21. **Navigation completeness** — Verify logo links to home, every sidebar/nav item links to an existing route, cross-page-group links resolve. Flag unreachable pages (exist in routes but not linked from any navigation).

**Output:** Write findings to `.synaptory/code-reviewer/findings/` by severity. Write complexity metrics to `.synaptory/code-reviewer/metrics/complexity.json`.
