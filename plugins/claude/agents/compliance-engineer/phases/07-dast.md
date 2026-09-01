# Phase 7: Dynamic Application Security Testing (DAST)

> **Anchor: You are the Compliance Engineer. Perform active scanning of the running application. Read-only findings — do NOT apply fixes.**

## Objective

Perform active scanning of the running application to detect runtime vulnerabilities that static analysis (Phases 2-5) cannot find:
- Authentication bypass through live HTTP flows
- Session management flaws only visible at runtime
- Injection vulnerabilities reachable only via HTTP (not apparent in static code)
- Business logic flaws in multi-step flows
- Server-side request forgery (SSRF) via actual HTTP paths

## Prerequisites

1. The application must be running (docker-compose stack up, or staging/preview environment URL).
2. OpenAPI specs must exist in `api/openapi/*.yaml` — ZAP uses them to enumerate scan targets.
3. This phase runs AFTER Phases 2-5 (static analysis). DAST findings supplement, not replace, static findings.

## Tool Selection

Detect available tooling in this order:

```bash
# Check for OWASP ZAP
which zap.sh 2>/dev/null || which zaproxy 2>/dev/null || docker image ls | grep -q owasp/zap

# Check for Nuclei
which nuclei 2>/dev/null

# Check for neither
echo "No DAST tools found"
```

Priority:
1. **OWASP ZAP** — preferred. Full DAST suite, OpenAPI import, authenticated scans.
2. **Nuclei** — fallback. Template-based, faster but narrower coverage.
3. **Neither available** — log as WARN finding, document setup instructions in remediation plan. Do NOT block pipeline.

## Detect Target URL

```python
# Check for running stack
ports_to_try = [3000, 8080, 8000, 4000, 5000]
base_url = None
for port in ports_to_try:
    result = Bash(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}/health 2>/dev/null || "
                  f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}/ 2>/dev/null")
    if result.strip() in ["200", "301", "302"]:
        base_url = f"http://localhost:{port}"
        break

if not base_url:
    # Try docker-compose service names
    result = Bash("docker-compose ps --format json 2>/dev/null | head -50")
    # Parse for running services and their mapped ports
    # base_url = detected URL

if not base_url:
    log_warning("DAST: No running application detected. Start with: docker-compose up -d")
    # Write WARN finding and exit phase gracefully — not a BLOCK
```

## Step 1 — Passive Scan (Spider)

Spider the application to discover all routes:

```bash
# ZAP spider
zap-cli --zap-url http://localhost:8090 spider {BASE_URL}
# OR docker-based ZAP:
docker run --network host owasp/zap2docker-stable zap-cli spider {BASE_URL}
```

Record all discovered URLs. Compare against `api/openapi/*.yaml` paths:
- Routes in OpenAPI spec but NOT discovered by spider: may require authenticated scan (log as note)
- Routes discovered but NOT in OpenAPI spec: potential undocumented endpoints (log as WARN finding)

## Step 2 — Authenticated Scan Setup

If the application has authentication (detected from Phase 3 auth review findings):

```bash
# Obtain a test token via the login endpoint
AUTH_TOKEN=$(curl -s -X POST {BASE_URL}/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"TestPassword123!"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))")

# Configure ZAP with the auth header
zap-cli --zap-url http://localhost:8090 session set-header "Authorization" "Bearer ${AUTH_TOKEN}"
```

Use test credentials from `tests/fixtures/` if available. Never use production credentials.

Re-spider with authenticated context to discover protected routes.

## Step 3 — Active Scan

Run ZAP active scan against all discovered URLs with authenticated context:

```bash
# ZAP active scan — targets all discovered URLs
zap-cli --zap-url http://localhost:8090 active-scan {BASE_URL}
# Wait for scan to complete (may take 5-30 minutes depending on app size)
```

**Scope:** Only scan `localhost` or the designated test environment. NEVER scan production or staging environments without explicit authorization. If the target URL is not localhost, confirm with user before proceeding.

```python
if "localhost" not in base_url and "127.0.0.1" not in base_url:
    AskUserQuestion(questions=[{
        "question": f"DAST target is {base_url} — not localhost. Active scanning a non-local environment can affect real users and data.",
        "header": "DAST Scope Confirmation Required",
        "options": [
            {"label": "Proceed — this is a dedicated test environment", "description": "I confirm this environment is safe to scan"},
            {"label": "Skip DAST — too risky", "description": "Run static analysis only"},
        ]
    }])
```

## Step 4 — OpenAPI-Driven Scan

Import the OpenAPI spec into ZAP for structured endpoint coverage:

```bash
# For each OpenAPI spec
for spec in api/openapi/*.yaml; do
    zap-cli --zap-url http://localhost:8090 openapi-import --url "file://$(pwd)/${spec}"
done
```

This ensures every documented endpoint is scanned even if spidering missed it (e.g., due to auth requirements).

## Step 5 — Nuclei Fallback (if ZAP unavailable)

```bash
nuclei -u {BASE_URL} \
  -t cves/ -t exposures/ -t vulnerabilities/ \
  -severity critical,high,medium \
  -j -o .synaptory/compliance-engineer/dast/nuclei-raw.json \
  2>/dev/null
```

Nuclei runs template-based checks and is faster but narrower than ZAP. Report it as "partial DAST coverage" in findings.

## Step 6 — Triage Findings

For every finding from ZAP or Nuclei:

1. **Map to OWASP Top 10 category** (same taxonomy as Phase 2 static findings)
2. **Contextual severity re-evaluation** — ZAP over-reports. Apply the same contextual severity adjustment as Phase 2:
   - Is the endpoint user-accessible without special access?
   - Is the input user-controlled?
   - Is the vulnerable path reachable in production configuration?
3. **Deduplicate against Phase 2 static findings** — same vulnerability found dynamically = increase severity by one level (dynamic confirmation proves exploitability)
4. **New DAST-only findings** — add to `issues.json` with `"source": "dast"`

## Output Deliverables

Write all outputs to `.synaptory/compliance-engineer/dast/`:

| File | Contents |
|------|----------|
| `scan-report.json` | Raw ZAP/Nuclei output |
| `findings.md` | Triaged findings with OWASP mapping, contextual severity, deduplication notes |

Also append DAST findings to `.synaptory/compliance-engineer/issues.json` (same schema as Phase 2 static findings) with `"source": "dast"` field.

## Gate Behaviour

| Finding | Action |
|---------|--------|
| DAST Critical/High | Same gate as static Critical/High — blocks deployment |
| DAST Medium/Low | Logged as findings, do not block |
| DAST tools unavailable | WARN finding: "DAST not performed — gitleaks/ZAP/Nuclei not installed. Install for runtime vulnerability detection." Non-blocking. |
| Application not running | WARN finding: "DAST skipped — no running application detected." Non-blocking. |

DAST is non-blocking in BUILD (no running app). It only runs in VERIFY when a stack is available.

## CI Integration

DAST uses two tracks with different speeds and scopes:

**Track 1 — Per-PR (Nuclei, fast):** Lightweight scan runs against the ephemeral PR environment after it's live. Covers critical/high severity only. Blocks merge on Critical findings. Configured in the ephemeral environment protocol (`.synaptory/.protocols/ephemeral-environments.md` — DAST Integration section). Write to `.github/workflows/pr-environment.yml` as part of the ephemeral environment workflow.

**Track 2 — Weekly scheduled (ZAP, comprehensive):** Full OWASP ZAP scan runs weekly against staging. Covers all severities with OpenAPI-driven endpoint coverage. This is the scan that catches medium/low findings and verifies full endpoint coverage.

```yaml
# .github/workflows/dast.yml
name: DAST Scan (Weekly)

on:
  schedule:
    - cron: '0 3 * * 1'   # Weekly, Monday 3am UTC
  workflow_dispatch:        # Manual trigger

jobs:
  dast:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Start application stack
        run: docker-compose -f docker-compose.yml up -d --wait
      - name: Run OWASP ZAP full scan
        uses: zaproxy/action-full-scan@v0.10.0
        with:
          target: 'http://localhost:${{ env.APP_PORT }}'
          rules_file_name: '.zap/rules.tsv'
          cmd_options: '-a'
      - name: Upload ZAP report
        uses: actions/upload-artifact@v4
        with:
          name: dast-report-${{ github.run_number }}
          path: report_html.html
      - name: Alert on new Critical findings
        if: failure()
        run: |
          echo "::error::DAST scan found Critical/High vulnerabilities"
          # Post to Slack/Teams channel if configured
```

Write `dast.yml` to `.github/workflows/` when generating CI configuration.

## Receipt

Write receipt to `.synaptory/.orchestrator/receipts/T6a-dast.json`:
```json
{
  "story_id": "{story_id}",
  "role": "compliance-engineer",
  "backend": "claude",
  "model": "{model_id_used}",
  "artifacts": [".synaptory/compliance-engineer/dast/findings.md"],
  "metrics": {
    "tool_used": "zap|nuclei|none",
    "endpoints_scanned": 0,
    "findings_critical": 0,
    "findings_high": 0,
    "findings_medium": 0,
    "dast_only_findings": 0,
    "deduplicated_with_static": 0
  },
  "verification_commands": [
    {
      "command": "test -f .synaptory/compliance-engineer/dast/findings.md && echo 'DAST findings file exists'",
      "exit_code": 0,
      "summary": "DAST findings file written to disk"
    }
  ],
  "token_usage": {
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_write": 0,
    "stage": "ce-compliance"
  },
  "completed_at": "{iso8601_utc_timestamp}"
}
```
