# Receipt & Verification Protocol

Complete ALL verification steps before writing your receipt. A receipt without
`verification_commands` FAILS validation and blocks the pipeline.

## Issues Ledger

You MUST write a machine-readable
`.synaptory/quality-engineer/issues.json`; the technical-writer consumes it.

```json
[
  {
    "id": "QE-001",
    "description": "Login returns 200 for invalid credentials, not 401",
    "type": "functional",
    "severity": "high",
    "status": "open",
    "parent_story": "US-E01",
    "file": "tests/auth/login.test.ts",
    "line": 45,
    "remediation": "Reject invalid credentials with 401",
    "source": "quality-engineer"
  }
]
```

`type` is one of `functional`, `test-gap`, `flaky-test`, `coverage-gap`;
`severity` one of `critical`, `high`, `medium`, `low`; `status` is `open` when
found and `remediated` once a fix is verified. `source` is always
`quality-engineer`. Per-field definitions: the playbook.

## Required verification_commands

At least one command must prove your work. Two entry forms — only one is
proof:

- **Executed object** `{"command": ..., "exit_code": ..., "summary": ...}` — a command you actually ran plus its exit code. The ONLY form that scores `tests_pass`. **Always record the test-suite run you performed this way.**
- **Plain string** — a replay instruction the SubagentStop hook re-runs with `shell=False` under an allowlist. No pipes, redirects, `$(...)`/backticks, env-var prefixes, or `bash -c`/`sh -c`; must be idempotent, non-interactive, non-blocking. Strings score nothing. Multi-step assertions belong in a committed script invoked directly.

## Receipt Template

```json
{
  "story_id": "{story_id}",
  "role": "quality-engineer",
  "backend": "claude",
  "model": "{model_id_used}",
  "artifacts": ["tests/", ".synaptory/quality-engineer/test-plan.md", "tests/coverage/thresholds.json", ".synaptory/quality-engineer/issues.json"],
  "metrics": {
    "test_files": 0,
    "test_cases": 0,
    "coverage_percent": 0,
    "coverage_delta": "+0.0%",
    "acceptance_criteria_covered": 0,
    "issues_total": 0,
    "runtime_verification": {"deployed": true, "logs_inspected": true},
    "ui_verification": {"rendered": true, "routes_tested": 0, "flows_failed": 0}
  },
  "verification_commands": [
    {"command": "npm test -- --coverage", "exit_code": 0, "summary": "112 passed, coverage 84.2%"},
    "test -s .synaptory/quality-engineer/issues.json",
    "test -s .synaptory/quality-engineer/test-plan.md"
  ],
  "token_usage": {
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_write": 0,
    "stage": "qe-verification"
  },
  "story_dod": {
    "tests_pass": false,
    "coverage_no_decrease": false,
    "no_critical_findings": false
  },
  "completed_at": "{iso8601_utc_timestamp}"
}
```

**Populating `model`, `token_usage`, `story_dod`:**

- `model` — the identifier you actually ran under. Never empty; the cost dashboard prices by it.
- `token_usage` — from the SDK's final `Usage`: `input_tokens` → `input`, `output_tokens` → `output`, `cache_read_input_tokens` → `cache_read`, `cache_creation_input_tokens` → `cache_write`. `stage` is fixed at `"qe-verification"`.
- `story_dod` — `tests_pass` from the test command's exit code; `coverage_no_decrease` from the coverage delta vs baseline (true when not measured); `no_critical_findings` false while any `critical` entry remains in `issues.json`. Booleans are self-assessment, not proof — the gate scores `tests_pass` from your executed objects.

**Structured `metrics` the DoD gate reads (write honest values):**

- `metrics.runtime_verification` — `{"deployed": true, "logs_inspected": true}` scores `runtime_verified`. Write it only when you really ran the system live and read its logs; prose is intent, not proof.
- `metrics.ui_verification` — `{"rendered": true, "routes_tested": N, "flows_failed": 0}` scores `ui_acceptance` for user-facing ACs.
- `metrics.coverage_delta` — number or percent string (e.g. `"+3.2%"`); negative fails `coverage_no_decrease`.
- `integrations` — `[{name, status: "live", ...}]` for endpoint/service-wiring stories; only `"live"` backed by a passing smoke in `verification_commands` satisfies `integration_verified`.
