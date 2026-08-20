## Phase 2: Architecture Design

> **Anchor: You are the Solution Architect. Design with fitness function justification. ADRs and diagrams — do not write implementation code.**

Generate architecture documents in `docs/architecture/` (or `paths.architecture_docs` from config).

### SAD (System Architecture Document)

Generate the master System Architecture Document at `paths.sad` (default: `docs/architecture/SAD.md`). This is the single source of truth for the system design — all downstream agents (BUILD, VERIFY, DEPLOY) read from this path.

### adrs/
One ADR per major decision using this template:
```markdown
# ADR-NNN: [Title]
**Status:** Accepted | Superseded | Deprecated
**Context:** Why this decision is needed
**Decision:** What we chose and why
**Consequences:** Trade-offs accepted
**Alternatives Considered:** What we rejected and why
```

Required ADRs:
- Architecture pattern (monolith, microservices, modular monolith, event-driven)
- Communication patterns (sync REST/gRPC, async messaging, CQRS)
- Data strategy (shared DB, DB-per-service, event sourcing)
- Auth architecture (JWT, OAuth2, session-based)
- Multi-tenancy strategy (row-level, schema-level, DB-level)

### system-diagrams/
Create Mermaid diagrams in `paths.system_diagrams` (default: `docs/architecture/system-diagrams/`):
- **C4 Context** — system boundaries and external actors
- **C4 Container** — services, databases, message brokers, CDN
- **Sequence diagrams** — for critical user flows (auth, payment, data pipeline)
- **Infrastructure topology** — cloud resources and networking

### Design Principles
Apply and document these production patterns:
- 12-Factor App methodology
- Defense in depth (security layers)
- Circuit breaker, retry, timeout patterns
- Idempotency for all write operations
- Eventual consistency where appropriate
- Zero-trust networking

### Testability Requirements

**Every architectural decision must be testable.** If a design cannot be tested at the unit, integration, or e2e level, the design is wrong.

Include a **Testability** section in each ADR that addresses:

1. **Dependency injection** — Can every external dependency (DB, cache, message broker, external API) be replaced with a test double? If not, redesign the boundary.
2. **Deterministic behavior** — Can each service produce the same output for the same input? Flag sources of non-determinism (timestamps, random IDs, external state) and document how tests should handle them.
3. **Observable side effects** — Can the test verify what the service DID (not just what it returned)? Document which side effects (DB writes, events published, emails sent) are observable via which mechanism (DB query, message consumer, mock server).
4. **Contract testability** — Is every service boundary defined by a contract (OpenAPI, AsyncAPI, gRPC proto) that can be validated automatically? Flag any service-to-service communication that lacks a formal contract.
5. **Local executability** — Can the full system (or a meaningful subset) run locally via docker-compose for integration and e2e testing? If not, document which components require cloud emulators or mocks.

**Architecture validation:** The architecture MUST have at least one testability concern documented per ADR. If an ADR has no testability section, the Code Reviewer will flag it as a High severity finding during review.

**Present architecture to user via AskUserQuestion for approval before proceeding.**
