# Retro Mode

Engineering retrospective that mines git history, pipeline artifacts, and team activity to produce actionable insights.

> **Note:** For Scrum projects with an active sprint lifecycle, the Sprint Retrospective ceremony runs automatically after Sprint Review (adaptive — skipped if sprint went smoothly). This standalone retro mode is for on-demand analysis outside the sprint ceremony flow, or for Kanban projects.
>
> Integrated ceremony: `${CLAUDE_PLUGIN_ROOT}/skills/synaptory/ceremonies/sprint-retro.md`

## Trigger Signals

"retro", "retrospective", "what did we ship", "team metrics", "weekly summary", "how are we doing", "shipping velocity", "code health"

## Execution

### Phase 1 — Data Collection

Gather all available signals in parallel:

```python
# Git history (last 7 days by default, or user-specified range)
Bash("git log --since='7 days ago' --format='%H|%an|%ae|%aI|%s' --numstat")
Bash("git shortlog --since='7 days ago' -sne")
Bash("git diff --stat HEAD~$(git rev-list --count --since='7 days ago' HEAD)..HEAD 2>/dev/null || git diff --stat @{7.days.ago}..HEAD 2>/dev/null || echo 'Unable to compute diff'")

# Branch activity
Bash("git branch -r --sort=-committerdate | head -20")
Bash("git log --since='7 days ago' --merges --format='%aI|%an|%s'")

# Pipeline artifacts (if synaptory workspace exists)
Read(".synaptory/.orchestrator/pipeline-state.json")
Glob(".synaptory/.orchestrator/receipts/*.json")
Read(".synaptory/.orchestrator/rework-log.md")
Glob(".synaptory/quality-engineer/findings.md")
Glob(".synaptory/compliance-engineer/findings.md")

# Test gap lifecycle (if known-test-gaps.md exists)
Read(".synaptory/.orchestrator/known-test-gaps.md")

# Quality trends history (if prior retros exist)
Glob(".synaptory/retro/retro-*.json")
Read(".synaptory/retro/quality-trends.json")
```

**Test Gap Age Analysis** (run if `known-test-gaps.md` was found):

Read the gap SLA from config (default 14 days):
```python
gap_sla_days = config.get("quality", {}).get("gap_sla_days", 14)
```

For each entry in `known-test-gaps.md`, extract `gap_id`, `ac_id`, `story_id`, `accepted_date`, `reason`. Compute age in days: `today - accepted_date`. Classify:
- `age > gap_sla_days` → **overdue** (needs resolution or formal closure)
- `age <= gap_sla_days` → **current** (within SLA)

For each overdue gap, call:
```python
tracker_cli.py --project-dir . create-story \
  --title "[Test Debt] Review or close accepted gap: {ac_id} ({story_id})" \
  --description "Gap accepted {age} days ago. Reason: {reason}. Either write the test, change the AC to be measurable, or formally close this gap." \
  --tags "test-debt,gap-review" \
  --priority "Medium"
```

Before creating: check if a story with the same title pattern already exists (`tracker_cli.py query "title:gap-review ac_id:{ac_id}"`). Skip if found.

### Phase 2 — Metrics Computation

From git data, compute:

**Volume Metrics:**
- Total commits (by author)
- Lines added / removed (by author, by directory)
- Test lines vs production lines ratio
- Number of files changed
- Average commit size (lines per commit)

**Quality Metrics:**
- Test-to-production code ratio (files matching `*test*`, `*spec*`, `*_test.*` vs others)
- Hotspot files (most frequently modified — high churn = high coupling risk)
- PR merge frequency (from merge commits)
- Fix/revert commits (commits matching "fix", "revert", "hotfix", "patch")

**Velocity Metrics:**
- Commits per day distribution
- Peak coding hours (from commit timestamps — local timezone)
- Shipping streak (consecutive days with commits)
- Average time between commits

**Pipeline Metrics** (if `.synaptory/` exists):
- Sprint/ticket completion rates
- Per-story DoD compliance
- Agent receipts: effort metrics (files_read, files_written, tool_calls)
- Findings by severity (from quality/compliance directories)
- Remediation rate (findings fixed / findings total)

### Phase 3 — Analysis & Insights

Generate insights from the data:

**Per-Author Breakdown:**
For each contributor:
- Commits, lines added/removed
- Primary areas (which directories/services)
- Specific praise: "Alice refactored the auth module — 3 files consolidated into 1, test coverage added"
- Growth signal: "Bob's PRs have low test ratio this week (12%) — discuss test-first approach?"

**Team Patterns:**
- Are we shipping consistently or in bursts?
- Is test coverage trending up or down?
- Which areas of the codebase are getting the most attention?
- Are hotspot files also the least-tested files? (coupling risk)
- Weekend/late-night commit patterns (potential burnout signal)

**Scope Drift Detection:**
- Compare initial plan (from BRD/architecture if available) to actual work done
- Flag: "Architecture specified 4 services but 6 directories were created — scope expanded?"

### Phase 4 — Trend Comparison

Read previous retros if they exist (already loaded in Phase 1):

If previous retros exist:
- Compare velocity: "Shipping velocity up 15% from last retro"
- Compare quality: "Test ratio improved from 28% → 34%"
- Compare hotspots: "auth-service is still the #1 hotspot for 3 consecutive retros — consider refactoring"
- Flag regressions: "Test ratio dropped below 25% — was 34% last retro"

**Quality Metrics Trending** — read `test-health.json` from the most recent sprint receipts to extract:
- `coverage_percent` (from `test-health.json` if present in sprint receipt artifacts)
- Findings count Critical/High (from compliance receipt `metrics.findings_critical`, `metrics.findings_high`)
- Flaky test count (from `tests/reports/flaky-tests.json` if exists)
- Mutation score (from `tests/mutation/results-*.json` if exists)

Append to `.synaptory/retro/quality-trends.json` (create if absent, append-only — never overwrite prior entries):
```json
{
  "entries": [
    {
      "date": "ISO timestamp",
      "sprint": N,
      "coverage_percent": 82.4,
      "findings_critical": 0,
      "findings_high": 2,
      "flaky_test_count": 1,
      "mutation_score": 68.3,
      "test_ratio": 0.34,
      "accepted_gaps_count": 2
    }
  ]
}
```

When reading existing `quality-trends.json`, compare current metrics to the prior entry to detect trends:
- Coverage drop ≥ 5%: flag as **regression**
- Findings (Critical/High) increased: flag as **regression**
- Flaky tests increased: flag as **warning**
- Mutation score dropped below 60%: flag as **warning**

### Phase 5 — Output

**Terminal Dashboard:**

```
━━━ Retro ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Period: {start_date} → {end_date} ({N} days)
  Contributors: {N}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  VOLUME
  ─────────────────────────────────────────────────────────────
  Commits:     {N} total ({N}/day avg)
  Lines:       +{added} / -{removed} net
  Test ratio:  {N}% of new code is tests
  Files:       {N} changed ({M} hotspots)
  Streak:      {N} consecutive shipping days

  TEAM
  ─────────────────────────────────────────────────────────────
  {author1}    {commits} commits  +{added}/-{removed}  {primary_area}
  {author2}    {commits} commits  +{added}/-{removed}  {primary_area}
  ...

  HOTSPOTS (most modified files)
  ─────────────────────────────────────────────────────────────
  {file1}    {N} changes    {tested? yes/no}
  {file2}    {N} changes    {tested? yes/no}
  ...

  INSIGHTS
  ─────────────────────────────────────────────────────────────
  {bullet insights from Phase 3}

  TRENDS (vs previous retro)
  ─────────────────────────────────────────────────────────────
  {trend comparisons or "First retro — no baseline yet"}

  QUALITY METRICS
  ─────────────────────────────────────────────────────────────
  Coverage:      {N}%  {↑/↓/→ vs prior}
  Findings:      {N} Critical  {N} High  {↑/↓/→ vs prior}
  Flaky tests:   {N}  {↑/↓/→ vs prior}
  Mutation score:{N}%  {↑/↓/→ vs prior}

  ACCEPTED TEST GAPS
  ─────────────────────────────────────────────────────────────
  {if no gaps: "No accepted gaps"}
  {if gaps exist:}
  ⚠ {N} overdue (>{gap_sla_days} days)   {N} current
  {gap_id}  {ac_id}  accepted {age} days ago  {story_id}
  ...
  {if overdue gaps: "Tracker stories created for overdue gaps — see backlog"}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Workspace Artifacts:**

Write to `.synaptory/retro/`:
- `retro-{date}.md` — Human-readable retro report (the terminal output above plus expanded details)
- `retro-{date}.json` — Machine-readable snapshot for trend comparison:
  ```json
  {
    "period": { "start": "...", "end": "..." },
    "commits": { "total": N, "per_author": {...}, "per_day": {...} },
    "lines": { "added": N, "removed": N, "test_ratio": 0.34 },
    "hotspots": [...],
    "velocity": { "commits_per_day": N, "streak_days": N },
    "pipeline": { "phases_completed": [...], "findings": {...} },
    "trends": { "velocity_change": "+15%", "test_ratio_change": "+6%" },
    "quality_metrics": {
      "coverage_percent": 82.4,
      "findings_critical": 0,
      "findings_high": 2,
      "flaky_test_count": 1,
      "mutation_score": 68.3
    },
    "accepted_gaps": {
      "total": N,
      "overdue": N,
      "entries": [
        { "gap_id": "GAP-001", "ac_id": "AC-007", "story_id": "US-E02-001", "age_days": 18, "status": "overdue" }
      ]
    }
  }
  ```

**Receipt:**
Write standard receipt to `.synaptory/.orchestrator/receipts/`:
```json
{
  "story_id": "RETRO-001",
  "role": "research-advisor",
  "backend": "claude",
  "model": "{model_id_used}",
  "artifacts": [".synaptory/retro/retro-{date}.md", ".synaptory/retro/retro-{date}.json"],
  "metrics": { "contributors": 0, "commits": 0, "test_ratio": "34%", "hotspots": 0 },
  "verification_commands": [
    "test -s .synaptory/retro/retro-{date}.md",
    "test -s .synaptory/retro/retro-{date}.json"
  ],
  "token_usage": {
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_write": 0,
    "stage": "orchestrator"
  },
  "completed_at": "{iso8601_utc_timestamp}"
}
```

> **RA inline carve-out:** Retro mode normally runs RA inline rather than via `Agent()`. The orchestrator MUST still write this receipt to `.synaptory/.orchestrator/receipts/RETRO-{date}-ra.json` — the session-end hook ships unshipped receipts on close.

## Configuration

From `.synaptory.yaml` (optional):
- `retro.period_days: 7` — lookback window (default: 7)
- `retro.authors_exclude: ["dependabot", "renovate"]` — exclude bot accounts
- `retro.test_patterns: ["*test*", "*spec*", "*_test.*"]` — patterns to detect test files
- `quality.gap_sla_days: 14` — days before an accepted test gap is flagged as overdue (default: 14, i.e. 2 sprints)

## Notes

- This mode is read-only for the codebase — it only writes to `.synaptory/retro/`
- Retro snapshots accumulate over time — trend comparison improves with more data
- If git history is shallow (< 7 days), adjust the period and note the limitation
- Respect privacy: focus on code patterns, not personal productivity judgments
