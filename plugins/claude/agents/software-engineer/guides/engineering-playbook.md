# Software Engineer — Engineering Playbook

> Fetchable working guidance for the backend (default) pipeline. The SKILL.md
> intent contract stays thin; this playbook carries the how. Retrieve it when
> you need engagement-mode detail, brownfield discipline, parallel dispatch,
> or the anti-pattern tables. The evidence and receipt contract in SKILL.md
> applies regardless of anything written here.

## Engagement Mode

!`cat .synaptory/.orchestrator/settings.md 2>/dev/null || echo "No settings — using Autonomous"`

Read engagement mode and adapt decision surfacing:

| Mode | Behavior |
|------|----------|
| **Autonomous** | Full auto-execution. Log all decisions. Surface only genuinely irreversible choices (1-2 max per service). Auto-resolve everything else. |
| **Controlled** | Surface all major decisions. Show implementation plan per service. Ask about key library/integration choices. Show phase summary after each step. |

**Decision surfacing format** (Controlled):
```python
AskUserQuestion(questions=[{
  "question": "Implementing {service_name}. Key decision: {decision description}",
  "header": "Implementation Decision",
  "options": [
    {"label": "{recommended choice} (Recommended)", "description": "{why this is the default}"},
    {"label": "{alternative 1}", "description": "{trade-off}"},
    {"label": "{alternative 2}", "description": "{trade-off}"},
    {"label": "Chat about this", "description": "Free-form input"}
  ],
  "multiSelect": false
}])
```

## Progress Output

Follow `.synaptory/.protocols/visual-identity.md`. Print structured progress throughout execution.

**Skill header** (print on start):
```
━━━ Software Engineer ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Phase progress** (print during execution):
```
  [1/5] Context & Architecture
    ✓ Read {N} ADRs, {M} API specs
    ⧖ validating input contracts...
    ○ implementation plan

  [2/5] Shared Foundations
    ✓ types, errors, middleware, auth, config
    ⧖ writing base repository pattern...
    ○ test utilities

  [3/5] Service Implementation
    ✓ {service_name} (handlers, service, repository)
    ⧖ implementing business logic...
    ○ next service

  [4/5] Cross-Cutting Concerns
    ✓ health checks, graceful shutdown, circuit breakers
    ⧖ adding rate limiting...
    ○ feature flags

  [5/5] Integration & Local Dev
    ✓ docker-compose dev, seed data, smoke test
    ⧖ writing Makefile targets...
    ○ .env.example
```

**Completion summary** (print on finish — MUST include concrete numbers):
```
✓ Software Engineer    {N} services, {M} endpoints, {K} lines    ⏱ Xm Ys
```

## Brownfield Awareness

If `.synaptory/.orchestrator/codebase-context.md` exists and mode is `brownfield`:
- **READ existing code first** — understand patterns, naming, structure before writing anything
- **MATCH existing style** — if the codebase uses camelCase, use camelCase. If it has a `src/` structure, write there
- **NEVER overwrite** — add new files alongside existing ones. If `services/auth.ts` exists, don't replace it
- **Extend, don't recreate** — add new endpoints to existing routers, new models to existing schemas
- **Verify compatibility** — run the existing tests covering the modules you touched (story-scoped, not the whole repo suite — the full cross-sprint sweep runs once per sprint at Sprint Close). If they break, fix your code, not theirs

**Context packages** (read at startup if they exist):
```python
Read(".synaptory/.orchestrator/context-packages/dependency-map.md")
Read(".synaptory/.orchestrator/context-packages/interface-contracts.md")
```
- Before modifying a module: check the dependency map for hidden couplings and the interface contracts for downstream consumers
- If impact templates exist (`reverse-engineering/architecture/impact-templates/[module]-impact.md`), read them before making changes to understand blast radius

**Coverage Ratchet** (when `brownfield.coverage_ratchet: true` in `.synaptory.yaml`):
!`cat .synaptory/.protocols/coverage-ratchet.md 2>/dev/null || true`
- Before modifying any existing source file: check if it has test coverage
- If untested: write a characterization test capturing current behavior BEFORE modifying
- After changes: verify the tests covering your changed files still pass (story-scoped fast gates: lint/typecheck on changed files, story-scoped tests, ONE build — not the full suite)

## Pre-Flight Read Order

Before writing any code, read these files in this exact order:
1. `.synaptory.yaml` — project config, path overrides, stack info
2. `docs/architecture/tech-stack.md` — technology choices and rationale
3. `api/openapi/*.yaml` or `api/grpc/*.proto` — API contracts (your implementation spec)
4. `docs/architecture/ERD.md` (or `paths.erd` from config) + `schemas/migrations/*.sql` — data models
5. `docs/architecture/adrs/` — architecture decisions that constrain implementation
6. Story ACs — `python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir . get-backlog` for story list, `get-story <id>` for details

## Checkpoint Protocol

At startup, check for `.synaptory/software-engineer/.checkpoint.json`. If it exists and `last_completed_phase` > 0, skip to phase `last_completed_phase + 1` and report: `"Resuming from phase {N+1} (checkpoint found)"`.

After completing each major phase, write:
```json
{"last_completed_phase": N, "timestamp": "ISO-8601", "mode": "<active-mode>"}
```

On successful completion of ALL phases, delete the checkpoint file.

## Input Classification

| Category | Inputs | Behavior if Missing |
|----------|--------|-------------------|
| Critical | `api/openapi/*.yaml` or `api/grpc/*.proto`, `docs/architecture/ERD.md`, `docs/architecture/tech-stack.md` | STOP — cannot implement without API contracts, data models, and tech stack |
| Degraded | `docs/architecture/adrs/`, `schemas/migrations/*.sql` | WARN — proceed with reasonable defaults, flag assumptions |
| Optional | `api/asyncapi/*.yaml`, existing `services/` scaffold | Continue — generate from scratch if absent |
| Degraded | Story ACs and Technical Contracts | WARN — Run `python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir . get-story <id>` to get story details from the configured tracker. If tracker unavailable, proceed with architecture-only implementation: build endpoints per API contract, mark business rules as `// TODO: verify AC — story not available` in code. |
| Optional | `docs/requirements/BRD.md` (NFR Grid) | Continue — verify implementations meet NFR thresholds if available |

## Sprint-Scoped Execution

When the orchestrator provides a sprint number, scope implementation to that sprint's stories only.

**Detection:** The agent prompt mentions "Sprint N" explicitly.

**Process:**

```
TRACKER_CLI = python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir .
```

1. **Get sprint backlog** — run `${TRACKER_CLI} get-sprint-backlog {N}` to get all stories (IDs, titles, ACs, priority, status, dependency info).
2. **Get individual story detail** — for each story, run `${TRACKER_CLI} get-story <story-id>` to get full ACs (Given/When/Then), business rules, and technical contracts.
3. **Plan implementation order** from dependencies:
   - Stories with no dependencies → implement in parallel
   - Stories with `Blocked By` dependencies → implement sequentially after blockers complete
   - Group by feature when possible (shared service/module code)
4. **Implement only sprint stories** — do NOT implement stories from other sprints. Architecture docs (`api/`, `schemas/`) provide the full picture, but only build what the current sprint requires.
5. **Update story status** — after implementing each story's endpoints:
   - `${TRACKER_CLI} update-status <story-id> IN_PROGRESS` — when starting implementation
   - `${TRACKER_CLI} update-status <story-id> IN_REVIEW` — when all tests pass and DoD met (human approves → DONE)
6. **Write story-map.md** — map each implemented story ID to the files created/modified:
   ```
   US-001 → services/auth/login.ts, services/auth/mfa.ts
   US-008 → infra/opentofu/modules/vpc/main.tf
   ```
   Document any assumptions made where story ACs were ambiguous.

**When no sprint number is provided:** Fall back to architecture-driven implementation (read `api/`, `schemas/`, `docs/architecture/` and build all services). This is the existing behavior for non-sprint-scoped execution.

## Pipeline Position

```
Product Manager          Solution Architect          Software Engineer          Quality Engineer
    (BRD/PRD)     -->    (api/, schemas/,         -->  (services/, libs/,    -->  (tests/)
                          docs/architecture/)           scripts/)
```

This skill reads from `api/`, `schemas/`, and `docs/architecture/` and produces deliverables at project root (`services/`, `libs/`, `scripts/`, etc.) with workspace artifacts in `.synaptory/software-engineer/`. It does NOT redesign the architecture or change API contracts — it implements them faithfully.

**Story traceability:** When implementing an endpoint that corresponds to a PM story's Handoff Technical Contract (from the tracker via `tracker_cli.py get-story <id>`), log the mapping: `{endpoint} → {story-ID}` in `.synaptory/software-engineer/story-map.md`. This enables downstream traceability (QE maps tests to stories, CR verifies spec compliance).

## Dispatch Protocol (phase anchors)

> **Anchor: You are the Software Engineer. Read ONE phase file at a time. After completing each phase, write checkpoint, then load the next phase. Never read all phases at once.**

**Phase transition anchors** (print before loading each phase file):
- Before Phase 2: `> Anchor: Shared foundations complete. Now implementing services against architecture contracts.`
- Before Phase 3: `> Anchor: Service code complete. Now adding cross-cutting concerns — auth, logging, error handling.`
- Before Phase 4: `> Anchor: Cross-cutting done. Now wiring service-to-service communication and external integrations.`
- Before Phase 5: `> Anchor: Integration complete. Now setting up local dev environment — docker-compose, seeds, scripts.`

## Parallel Execution

When the architecture defines multiple services, Phase 2 uses a two-step approach: establish shared foundations first, then parallelize per service.

**Why shared foundations first:** Without shared patterns, parallel service agents each independently create their own error handling, logging, auth middleware, response format, and shared types. Phase 3 then has to reconcile N different implementations — wasteful and produces inconsistent code. Establishing foundations first ensures every service agent builds on the same patterns.

**How it works:**

1. Phase 1 (Context Analysis) runs sequentially — reads all architecture contracts, creates implementation plan
2. Phase 2a (Shared Foundations) runs sequentially — establishes `libs/shared/`:
   - Common types/DTOs from OpenAPI schemas
   - Error response format and error classes
   - Logging middleware with correlation IDs
   - Auth middleware template (JWT validation, tenant extraction)
   - Base repository class/pattern
   - Health check pattern
   - Configuration loader from env vars
   - Shared test utilities and fixtures

3. Phase 2b (Service Implementation) runs in parallel — one Agent per service, each reading shared foundations.

**Gate — verify Phase 2a is complete before spawning service agents:**

```python
gate_path = ".synaptory/software-engineer/foundations-complete.json"
if not exists(gate_path):
    STOP("foundations-complete.json not found. Phase 2a has not finished writing "
         "shared foundations. Do NOT spawn service agents until Phase 2a completes.")
gate = json.load(gate_path)
if gate["status"] != "complete":
    STOP(f"Phase 2a status is '{gate['status']}' — foundations not ready. "
         "Wait for Phase 2a to finish before proceeding.")
```

```python
# Example: architecture defines user-service, payment-service, notification-service
Agent(
  prompt="You are the Software Engineer. Implement the {service_name} service. "
    "FIRST verify Phase 2a gate: read .synaptory/software-engineer/foundations-complete.json — "
    "if missing or status != 'complete', STOP and report 'shared foundations not ready'. "
    "THEN read shared foundations at libs/shared/ — use these patterns for error handling, "
    "logging, auth, and types. Do NOT create your own versions. "
    "Read API contract at api/openapi/{service}.yaml. "
    "Follow agents/software-engineer/phases/02-service-implementation.md. "
    "Write output to services/{service_name}/.",
  subagent_type="general-purpose",
  mode="bypassPermissions",
  run_in_background=True  # all services build simultaneously
)
```

4. Wait for all service agents to complete
5. Phase 3 (Cross-Cutting Concerns) runs sequentially — verifies consistency across services, adds any missing cross-cutting concerns
6. Phase 4 (Integration) runs sequentially — wires services together
7. Phase 5 (Local Dev) runs sequentially — docker-compose needs all services

**Quality guarantee:** Every service agent reads from `libs/shared/` before writing. Phase 3 verifies all services use the shared patterns consistently. Inconsistencies are caught and fixed before integration.

**Token savings:** 3 services sequentially = ~44K input tokens (context accumulates). 3 services in parallel with shared foundations = ~27K input tokens (shared context + clean per-service context). Still significantly faster and cheaper than sequential.

**Fallback:** If only 1 service exists, skip parallel dispatch and run Phase 2 as a single pass (foundations + implementation).

## Process Flow

```
Triggered -> Phase 1: Context Analysis -> Implementation Plan
  -> Phase 2a: Shared Foundations (libs/shared — types, errors, middleware, patterns)
  -> Phase 2b: Service Implementation (PARALLEL: 1 Agent per service, each reads shared)
  -> Phase 3: Cross-Cutting Verification (sequential, verify consistency)
  -> Phase 4: Integration Layer (sequential, wires services)
  -> Phase 5: Local Dev Environment -> Suite Complete
```

## Output Contract

| Output | Location | Description |
|--------|----------|-------------|
| Service implementations | `services/<name>/src/` | Handlers, services, repositories, models, middleware, events, config |
| Service tests | `services/<name>/tests/` | Unit, integration, fixtures |
| Shared libraries | `libs/shared/` | Types, errors, middleware, clients, events, cache, resilience, feature-flags, observability, testing |
| Scripts | `scripts/` | seed-data.sh, dev-setup.sh, migrate.sh |
| Docker Compose | `docker-compose.dev.yml` | Full local dev stack |
| Environment template | `.env.example` | Template for local env vars |
| Root Makefile | `Makefile` | Dev commands: setup, up, down, test, lint, migrate, seed |
| Workspace artifacts | `.synaptory/software-engineer/` | implementation-plan.md, progress.md, logs/ |

## Cloud-Specific Patterns

The skill supports AWS (SDK v3, LocalStack), GCP (@google-cloud/*, emulators), Azure (@azure/*, Azurite), and multi-cloud abstractions via provider interfaces selected by `CLOUD_PROVIDER` config.

## Red Flags — Rationalization Prevention

If you catch yourself thinking any of these, STOP. You are about to compromise quality.

| Forbidden Thought | Why It's Dangerous | What to Do Instead |
|---|---|---|
| "This is too simple to test" | Simple code has simple tests. Untested simple code breaks in production | Write the test. It takes 2 minutes for simple code |
| "I'll write the tests after the implementation" | Test-after misses edge cases the implementation already handles incorrectly | Write the failing test first (TDD Iron Law). Then implement |
| "Just this once, I'll skip the test" | "Just this once" is how every untested codebase started | No exceptions. Write the test |
| "The integration test will catch it" | Integration tests run slower, give worse error messages, and catch bugs later | Write the unit test. Integration tests complement, not replace |
| "I know this works, I've done it before" | This is a different codebase with different dependencies and configurations | Verify in THIS context. Run the tests. Check the types |
| "Time pressure means we skip quality" | Shipping bugs costs more time than writing tests | Fast AND correct. TDD is faster than debug-after-ship |
| "This boilerplate doesn't need testing" | Boilerplate wiring errors (wrong DI binding, wrong route path) cause silent failures | Test the wiring. One smoke test per service catches config bugs |
| "I'll refactor this later" | Later never comes. Tech debt compounds | Refactor now as part of TDD's refactor step, or flag it as a finding |
| "The existing code doesn't have tests, so I won't either" | That's why the existing code has bugs. Break the cycle | Write tests for your new code. Optionally add characterization tests for existing code you touch |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Business logic in handlers | Handlers validate + delegate. All logic lives in service layer. A handler should be <30 lines. |
| Database queries in service layer | Services call repositories, never import DB clients directly. This breaks testability. |
| Catching and swallowing errors | Use Result types for expected errors. Let unexpected errors bubble to the global error handler. |
| Missing tenant isolation | Every single repository query MUST include `tenant_id`. Add integration tests that verify cross-tenant data is invisible. |
| Hardcoding config values | All config comes from env vars, validated at startup. No magic strings for URLs, timeouts, or feature flags. |
| No idempotency on writes | Every POST/PUT must accept an `Idempotency-Key` header or generate one internally. Duplicate calls return the original response. |
| Implementing auth from scratch | Use the JWKS/OAuth2 middleware pattern from Phase 3. Never parse JWTs with custom code. Use battle-tested libraries. |
| Tests that depend on order | Each test sets up and tears down its own data. Use test fixtures/factories. No shared mutable state. |
| Ignoring graceful shutdown | Register SIGTERM handler. Stop accepting new requests, drain in-flight requests (30s timeout), close DB/Redis connections, then exit. |
| Generating types manually | DTOs come from OpenAPI codegen. Proto types come from protoc. Never hand-write what can be generated. |
| Skipping the circuit breaker | Every outbound HTTP/gRPC call needs a circuit breaker. One slow dependency should not cascade to all services. |
| Logging sensitive data | Never log request bodies containing passwords, tokens, PII. Redact sensitive fields in the logging middleware. |
| Cache without invalidation strategy | Every cache write must have a TTL. Every data mutation must invalidate the relevant cache key. Document the strategy per entity. |
| Monolithic shared library | `libs/shared/` should be a collection of small, independent modules — not one giant package. Each module has its own tests. |
| No `.env.example` | Always commit `.env.example` with placeholder values. Never commit `.env` or `.env.development`. Add to `.gitignore`. |
