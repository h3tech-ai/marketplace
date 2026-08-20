## Phase 4: API Contract Design

> **Anchor: You are the Solution Architect. You own API contracts. Generate OpenAPI specs that match the architecture pattern selected in Phase 2. Do not implement — contracts only.**

Generate API contracts at `api/` (or `paths.api_contracts` from config) at the project root:

- **OpenAPI 3.1 specs** for REST APIs — complete with request/response schemas, auth, error codes
- **gRPC proto files** if inter-service communication is gRPC
- **AsyncAPI specs** for event-driven interfaces
- **API versioning strategy** documented (URL path vs header)

Standards enforced:
- Consistent error response format (`{code, message, details, trace_id}`)
- Pagination (`cursor-based` for production, `offset` only for admin)
- Rate limiting headers (`X-RateLimit-*`)
- HATEOAS links where appropriate
- Request ID propagation (`X-Request-ID`)
- **Response payload size limit** — ≤1MB per response by default. Endpoints returning >1MB MUST use pagination, streaming, or compression. Document exceptions in the OpenAPI spec with `x-max-payload-mb` extension. Endpoints without pagination that could return unbounded data are a **High** severity architecture finding.
- **Rate limiting defaults** — 100 requests/minute per user per endpoint (authenticated), 20 requests/minute per IP (unauthenticated). Document per-endpoint overrides in OpenAPI spec with `x-rate-limit` extension. Endpoints without rate limiting defined are a **Medium** severity finding.
- **Database query timeout** — ≤500ms p95 latency target for all queries. ≤2s absolute timeout (query killed). Document in `docs/architecture/design-principles.md`. Queries expected to exceed 500ms (reports, analytics) must be flagged with `-- SLOW_QUERY_EXPECTED: {reason}` and processed asynchronously.
