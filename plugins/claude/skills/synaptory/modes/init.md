# Init Mode

Analyze the project and generate `.synaptory.yaml`. This mode also auto-triggers when any other mode detects that `.synaptory.yaml` does not exist.

Init is **detect → confirm → generate**, not detect-and-guess. Auto-detection only
seeds the *recommended* answer for each high-stakes field; the user confirms or
overrides before anything is written. This prevents the silent-misconfiguration
problem where init picked `project_id: "default"`, the wrong `build_mode`, or an
empty tracker/DoR and the user had to hand-edit generated YAML afterwards.

0. **Re-run guard — check for an existing config FIRST.** Read `.synaptory.yaml`:
   - **Missing** → fresh init: continue with the full flow below.
   - **Exists but incomplete** (any of `build_mode`, `project.name`, `project.type`,
     `dod`, `dor`, `tracker.templates` absent) → ask ONLY the missing fields, using
     the interview round that owns each one. NEVER re-ask a field that already has
     a value.
   - **Exists and complete** → do NOT re-run the interview. Print the resolved
     config summary (project_id, name/type, build_mode, engagement, tracker),
     then ask ONE question:
     ```python
     AskUserQuestion(questions=[{
       "question": "This project is already configured. What do you want to change?",
       "header": "Reconfigure",
       "options": [
         {"label": "Nothing — config is fine (Recommended)", "description": "Exit init; no files touched"},
         {"label": "Change specific settings", "description": "Pick a round: identity / lifecycle / tracker / UI prefs / quality gates"},
         {"label": "Reconfigure everything", "description": "Walk all interview rounds again, current values as defaults"}
       ],
       "multiSelect": false
     }])
     ```
     Jump only to what the user picked. On any re-run path, never touch
     `.synaptory/.orchestrator/pipeline-state.json` or existing receipts —
     reconfiguration edits config, not state.
1. Detect project structure: language (package.json, go.mod, pyproject.toml), framework (Next.js, NestJS, FastAPI, Gin), infrastructure (Dockerfile, Terraform, K8s, CI/CD), architecture (monolith/microservices/monorepo)
2. Classify health: greenfield (no code) vs brownfield (existing codebase)
3. **Run the Configuration Interview (below)** — confirm every high-stakes field with the user, seeding each question's default from detection.
4. Generate `.synaptory.yaml` from the template at `Bash("synaptory skills get templates/synaptory.yaml.tmpl")`, applying the confirmed answers. Resolve the top-level `{{PRODUCT_VERSION}}` with `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/product_version.py"` from the executing plugin. Use its exact output as `version`; never copy the historical config schema or infer it from pipeline state. If release metadata cannot be read, report the error before writing config. The helper also accepts `--template <downloaded-template-path>` to print the template with its release resolved.
5. Scaffold tracker description templates into `docs/templates/` (if not already present)
6. Write `.synaptory/.orchestrator/init-answers.md` (decisions + defaults applied) and print a summary of the final configuration.

```python
Skill(skill="synaptory:init")
# The init logic reads from: Bash("synaptory skills get templates/synaptory.yaml.tmpl")
# Output: .synaptory.yaml at project root
```

---

## Configuration Interview

Confirm each field with `AskUserQuestion`. Follow the pattern used by
`.synaptory/.protocols/socratic-gate.md`: the detected value is
the first option labelled **"(Recommended)"**, alternatives carry a one-line trade-off,
and "Chat about this" is always last. Never write a field the user has not seen.

**Engagement-mode depth:**
- **`interactive`** (or when the user asked to configure carefully) — run **all five rounds**.
- **`structured`/autonomous** — run **Round 1 + Round 2 minimum** (identity and lifecycle
  are too costly to guess), auto-fill Rounds 3–5 from detection, then print the resolved
  values and offer a single "Adjust anything?" prompt before writing. If engagement_mode
  itself is unknown at this point, ask it in Round 2 and re-evaluate depth.

> Engagement mode only controls how chatty *init* and *per-sprint* work is. It never
> disables the Inception clarification floor — see the `engagement_mode` note in the
> config template.

### Round 1 — Project identity & control-plane link (ALWAYS)
- **`project_id`** — the control-plane project **slug**. This drives telemetry, receipt,
  and skill-fetch attribution (see template lines 11–19). **Never silently leave it as
  `"default"`.** Ask the user for the slug their H3Tech operator created (e.g.
  `taskflow-pilot`); offer `default` only as an explicit "reserved fallback" choice with a
  warning that telemetry will not attribute correctly.
- **`project.name`** — default from git/dir name.
- **`project.type`** — greenfield | brownfield | evolved_brownfield | mixed (default from health classification).

### Round 2 — Lifecycle (ALWAYS)
- **`build_mode`** -- scrum | kanban | spq (kanban is brownfield-only; warn if chosen for greenfield).
  **If the answer is `spq`, run Round 2b before moving on.** It is a different lifecycle, not a
  setting to flip later: four stages (`DISCOVERY → CYCLE → ACCEPTANCE → COMPLETE`), three recorded
  method events (Commit, Sync, Checkpoint) that gate nothing, and an all-or-nothing barrier at each
  Cycle's close that integrates to one shared trunk. See `modes/spq.md`.
- **`engagement_mode`** — structured | interactive.
- **`sprint.inception`** -- foundation | blueprint (foundation for most; blueprint for regulated/fixed-scope). **Scrum only**: SPQ sizes Discovery to the uncertainty it must remove and reads no depth key.

### Round 2b -- SPQ (only when `build_mode: spq`)

Three things, and they are small on purpose. **Most of what an SPQ Cycle needs is declared per
Cycle at its Commit, not configured once here**: the admitted set, each Work Unit's `path_scope`,
the source region, the Engineering Lead, the Crew and the barrier criteria all live in the sealed
declaration `open_cycle` writes. A configuration key for any of them would be a second, staler
copy of a hash-sealed document.

- **`spq.source_region`** -- the default region a Cycle declares when its Commit does not name one,
  as a list in the declared path grammar (`api/`, `api/**`, or `api/routers/auth.py`; suffix
  patterns, leading globstars, interior wildcards, traversal and absolute paths are refused rather
  than guessed). Offer the top-level source directories as candidates and let the user pick. Two
  concurrent Cycles never declare overlapping regions, so this is a default for a single-Cycle
  project and a starting point for a concurrent one.
- **`spq.trunk_ref`** -- the one shared trunk every Cycle integrates to at its Checkpoint. Default
  from `git symbolic-ref refs/remotes/origin/HEAD` (usually `main`); confirm it. It is the
  `--trunk-ref` a Commit declares, and a Cycle that integrates into a different ref is the
  deferred integration the retired composition layer existed to gather.
- **`parallelism.max_concurrent`** -- a **resource ceiling**, not a concurrency switch. Under SPQ
  there is no `story_parallelism` and no `isolation` mode: concurrency is a consequence of the
  declared path scopes, and this only limits how much runs at once. Say that when you ask, because
  a user who read the Scrum block will expect a switch. A ceiling can never make colliding work
  legal.

Then check the one thing that is not a setting and blocks the first Commit if it is wrong:

```bash
git check-ignore -q .synaptory/cycles/ \
  && echo "BLOCKING: add '!.synaptory/cycles/' after '.synaptory/*' in .gitignore" \
  || echo "committed transport is visible to git"
```

A Cycle's sealed declaration, its cut record and its dependency events travel to other clones
through that directory. `open_cycle` refuses a gitignored declaration (`transport_ignored`), so fix
it during init rather than during the first sizing session.

**Do not write a barrier-criteria list, a threshold list, a lane list or a sync block.** The
criteria come from one constant in the runtime and are published into each sealed declaration; a
project may add a criterion or raise a threshold, but not by editing `.synaptory.yaml`, and there
is no key that reads one. Restating the list in configuration is how three different criteria
counts came to ship in one product.

### Round 3 — Tracker
- **`tracker.backend`** — local | github | jira | teamwork | linear.
- Then prompt only for the chosen backend's required fields: jira `url`+`project_key`,
  teamwork `site_name`+`project_id`, github `repo` (default from `git remote origin`),
  linear `team_key` (the short key, e.g. `ENG` — not the team name).
- Mention `specs:` (multi-spec, one project hosting N backlogs) as an advanced opt-in and
  point to the `specs:` comment block in the template; do not walk its full matrix here.

### Round 4 — UI surface & preferences
- **`features.frontend`** — true | false. If true, confirm `preferences.frontend_framework`
  (nextjs | nuxt | sveltekit | remix). This flag is the authoritative UI-surface signal
  that Inception's mockup step keys off of.
- Confirm the detected `preferences` that matter most: `test_framework`, `package_manager`, `orm`.

### Round 5 — Quality gates
- **`dor`** — confirm inline Definition of Ready criteria or a `dor.file` path. If the user
  has none, seed the sensible inline default from the template's `dor:` comment.
- Confirm the `dod.story` critical checks are appropriate for the project.
- Surface **`quality.runtime_verification`** (off | required) and
  **`sprint.review.per_story_acceptance`** (default true) as yes/no confirmations.
- **`quality.test_cases`** — where authored test cases for tickets live.
  **EXPLORE FIRST, then ask with evidence** — do not present a blind menu.
  Run the detection pass for the configured tracker backend (best-effort;
  skip silently on auth/network failure and fall back to the plain question):

  - **jira** — probe the live instance (creds from env, same as the adapter):
    1. `GET /rest/api/3/issuetype` — test-management issue types present?
       (`Test`, `Test Case`, `Test Execution`, `Test Plan`, `Xray Test`,
       Zephyr types) → suggests **tracker-linked**.
    2. `GET /rest/api/3/issueLinkType` — link types like `Test` /
       "is tested by" → strengthens **tracker-linked**.
    3. Sample 3–5 recently updated stories (JQL
       `project=<key> AND issuetype=Story ORDER BY updated DESC`, fields
       `description,comment,issuelinks`) and inspect each:
       - description headings (`## Test Cases`, `## Testing Notes`,
         `## Test Scenarios`) with real content → **ticket**;
       - comments carrying Given/When/Then blocks, numbered test steps, or
         `TC-` ids → **comments** (a very common pattern — QA posts test
         cases as comments after refinement);
       - `issuelinks` pointing at Test-type issues → **tracker-linked**.
  - **github** — sample recent issues: task-list checklists in bodies →
    **ticket**; linked "test" issues or a `test-case` label → **tracker-linked**;
    test cases in issue comments → **comments**.
  - **teamwork / local** — repo scan only.
  - **All backends** — scan the repo for `docs/test-cases/`, `test-cases/`,
    `qa/` directories → **repo**; grep project docs/README for TestRail,
    Zephyr Scale, qTest, Xray mentions → **external**.

  Then ask ONE question, citing what was found, with the detected source as
  the recommended option, e.g. "STAR-142 and STAR-138 have test cases posted
  as comments — use those?" Options: the detected source (Recommended, with
  evidence) | **None — derive tests from acceptance criteria** (Recommended
  only when nothing was detected) | the remaining sources
  (ticket / comments / repo / tracker-linked / external). Follow up ONLY for
  the chosen source's fields (ticket: `section`; comments: optional `marker`;
  repo: `path` glob with `{story_id}`; tracker-linked: `link_type` +
  `issue_type`; external: `tool` + `url` + free-text `instructions`).
  Credentials are never written to the YAML — reference env vars in
  `instructions` instead. Emit as the `quality.test_cases` block (see the
  template's comment examples for each source's shape). Jira detail: when the
  sampled links point at **"Test Set"** issues (Xray's grouping type) rather
  than individual Tests, emit `issue_type: "Test Set"` plus the `testSetTests()`
  JQL expansion recipe in `instructions` — the template's tracker-linked
  comment carries the exact wording. QE consumes this at test planning:
  authored cases become the required test set, ACs generate gap coverage on
  top, and per-case results land in the receipt's
  `metrics.authored_test_cases` (a failed authored case blocks the story's
  `tests_pass` gate).
- **`quality.e2e`** — the harness authored cases run in. Only relevant when a
  test-case source was configured above. Detect before asking: an existing
  `playwright.config.*` / `cypress.config.*` / `tests/e2e/` dir → emit the
  block from what's found (runner, dir, run command, JSON results path,
  per-story filter). Nothing found → offer to scaffold a minimal harness
  (like the tracker templates scaffold): `tests/e2e/{package.json,
  playwright.config.ts,README.md}` with the conventions QE depends on —
  test titles prefixed with the authored-case source ID, story-tag
  describes (`@<STORY-KEY>`), `data-testid` selectors, JSON reporter. Emit
  as the `quality.e2e` block (see the template's comment example).

### Persist answers
After the interview, write `.synaptory/.orchestrator/init-answers.md`:

```markdown
# Init Configuration Answers

## Context
- Date: {ISO date}
- Engagement mode: {structured|interactive}
- Rounds asked: {N}

## Decisions
### {Field}
- **Choice:** {value}
- **Rationale:** {why / detected-and-confirmed}

## Defaults Applied (not asked)
- {field}: {value} — {detected|template default}
```

After config generation, scaffold tracker description templates into the project. These templates define the section structure used when creating issues in the configured tracker (Jira, GitHub, Teamwork, Linear). If a `docs/templates/` directory already exists with templates, skip scaffolding — the user has customized them.

```python
# Scaffold tracker description templates (skip if already present)
# These templates control which ## sections appear in Jira/GitHub/Teamwork/Linear issue descriptions.
# The tracker adapters read section headings from these files via _load_template_sections().
templates_dir = "docs/templates"
story_exists = Glob(f"{templates_dir}/story.md")

if not story_exists:
    Bash(f'mkdir -p {templates_dir}')

    # Built-in defaults are control-plane delivered, not packaged (ADR-016), so
    # there is no plugin directory to copy them out of. The previous version of
    # this block copied the templates out of the package and explained the
    # failure as ".md.enc in the published plugin" — a mechanism ADR-016 removed,
    # so the copy simply always failed and the inline fallback always ran.
    for cp_name, dst_name in [("user-story", "story.md"), ("task", "task.md"), ("epic", "epic.md"), ("bug", "bug.md")]:
        dst_path = f"{templates_dir}/{dst_name}"
        if not Glob(dst_path):
            # Fetch the body, then write it. Falls through to the inline
            # scaffolding below when the fetch fails (offline, or not signed in).
            result = Bash(f'synaptory skills get "templates/tracker/{cp_name}" > "{dst_path}" 2>/dev/null && test -s "{dst_path}" && echo OK || echo SKIP')
            if "SKIP" in result:
                # Generate minimal template inline as fallback
                # These provide section headings that _load_template_sections() reads
                if dst_name == "story.md":
                    Write(dst_path, """# {{ID}}: {{TITLE}}

## Story

As a {{ROLE}}, I want {{CAPABILITY}}, so that {{BENEFIT}}.

## Acceptance Criteria

* [ ] **AC-01:** Given {{PRECONDITION}}, When {{ACTION}}, Then {{EXPECTED}}

## Business Rules

- {{RULE}}

## Testing Notes

- **Happy path:** {{DESCRIPTION}}
- **Negative:** {{DESCRIPTION}}

## Technical Context

{{NOTES}}

## Dependencies

- {{DEPENDENCY}}
""")
                elif dst_name == "bug.md":
                    Write(dst_path, """# {{ID}}: {{TITLE}}

## Summary

{{DESCRIPTION}}

## Steps to Reproduce

1. {{STEP}}

## Expected Behavior

{{EXPECTED}}

## Actual Behavior

{{ACTUAL}}

## Acceptance Criteria

* [ ] **AC-01: Fix verified** — {{DESCRIPTION}}
* [ ] **AC-02: Regression test** — Automated test prevents recurrence

## Technical Context

{{NOTES}}
""")
                elif dst_name == "task.md":
                    Write(dst_path, """# {{ID}}: {{TITLE}}

## Objective

{{DESCRIPTION}}

## Acceptance Criteria

* [ ] **AC-01:** Given {{PRECONDITION}}, When {{ACTION}}, Then {{EXPECTED}}

## Technical Context

{{NOTES}}

## Testing Notes

- {{DESCRIPTION}}

## Dependencies

- {{DEPENDENCY}}
""")
                elif dst_name == "epic.md":
                    Write(dst_path, """# {{ID}}: {{TITLE}}

## Objective

{{DESCRIPTION}}

## User Impact Statement

{{IMPACT}}

## Feature List

| Feature | Stories | Priority |
|---------|---------|----------|
| {{FEATURE}} | {{STORIES}} | {{PRIORITY}} |

## NFRs

- {{NFR}}

## Technical Context

{{NOTES}}
""")

# Wire scaffolded templates into .synaptory.yaml so adapters use them.
# This ensures Jira/GitHub/Teamwork adapters read section structure from
# the project's templates rather than using hardcoded defaults.
config_text = Read(".synaptory.yaml")
if 'story: ""' in config_text:
    Edit(".synaptory.yaml", old='story: ""', new='story: "docs/templates/story.md"')
    Edit(".synaptory.yaml", old='task: ""', new='task: "docs/templates/task.md"')
    Edit(".synaptory.yaml", old='epic: ""', new='epic: "docs/templates/epic.md"')
    Edit(".synaptory.yaml", old='bug: ""', new='bug: "docs/templates/bug.md"')
```

After config generation, generate lightweight CLAUDE.md and README.md sections:

```python
# Read project name from the just-created .synaptory.yaml
project_name = Read(".synaptory.yaml")  # extract project.name

# Generate CLAUDE.md synaptory section (fenced block — safe to run repeatedly)
claude_section = f"""# Synaptory Pipeline

This project uses **Synaptory** — a multi-agent adaptive delivery system with 9 specialized agents on Claude (tier-routed across Opus/Sonnet/Haiku) and Scrum + Kanban lifecycle support. Always route requests through `/synaptory` rather than making ad-hoc changes.

- **Config:** `.synaptory.yaml`
- **Workspace:** `.synaptory/`
- **Build mode:** {build_mode} (scrum or kanban)

To start: describe what you want to build, or run `/synaptory`.

## Agent Roster

| Agent | Role |
|---|---|
| `project-owner` | Backlog refinement, Sprint Planning, story decomposition |
| `solution-architect` | Incremental architecture, ADRs, API contracts (on-demand) |
| `software-engineer` | Story-level builder (backend, frontend, ai-ml, mobile) |
| `quality-engineer` | Per-story verifier, test generation |
| `code-reviewer` | Per-story reviewer (adaptive intensity) |
| `compliance-engineer` | Security audit, STRIDE/OWASP (on-demand) |
| `platform-engineer` | CI/CD, Docker, IaC, monitoring, reliability |
| `technical-writer` | Sprint reports, API docs, developer guides |
| `research-advisor` | Thinking partner, domain research |

## Mode Routing (via `/synaptory`)

New feature → **Feature** | Sprint work → **Sprint** | Tests → **Test** | Code review → **Review** | Architecture → **Architect** | Bug fix → **Debug** | Security → **Verify** | Deploy → **Deploy** | Research → **Explore** | Performance → **Optimize**

## Tracker CLI

All story/sprint/epic operations go through `tracker_cli.py` — never read story files directly.
```
python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir . <cmd>
```
Commands: `get-backlog`, `get-sprint-backlog <N>`, `get-story <id>`, `update-status <id> <status>`, `create-story`, `list-sprints`

## Receipt Protocol

Every agent writes a JSON receipt to `.synaptory/.orchestrator/receipts/`. Missing `verification_commands` **blocks the pipeline**. Required fields: `artifacts`, `metrics`, `verification_commands`, `verification_summary`.

## Git Safety Rules (MANDATORY)

These rules apply to all synaptory agents, Claude Code, and automated tooling:

1. **NEVER commit or push to shared branches** (`dev`, `qa`, `uat`, `main`, `prod`, `staging`, `release`). All work MUST happen on feature branches.
2. **NEVER create commits without explicit user approval.** Always show the diff and ask before committing.
3. **NEVER push to any remote branch without explicit user approval.** Always ask before running `git push`.
4. **NEVER create or merge pull requests without explicit user approval.**
5. **NEVER run destructive git operations** (`git push --force`, `git reset --hard`, `git clean -f`, `git checkout .`).
6. **NEVER run database migrations against shared environments** (dev, qa, uat, prod). Migrations can only be run locally.

If the user says "just do it" or "go ahead", that applies to the current code change only — NOT to committing, pushing, or merging.

**At the start of every session, ask the user how they'd like to work** using AskUserQuestion:
- "Use synaptory (Recommended)" — route changes through specialized agents
- "Work directly without the plugin" — make changes freely
- "Chat about this" — discuss approach together

<!-- synaptory-state
phase: INIT
sprint: 0/0
engagement: guided
config: .synaptory.yaml
-->"""
Bash(f'printf "%s" \'{claude_section}\' | python3 "${{CLAUDE_PLUGIN_ROOT}}/hooks/lib/update_claude_md.py" "${{CLAUDE_PROJECT_DIR}}"')

# Generate README.md scaffold (greenfield only — skip if README already exists)
if not Glob("README.md"):
    readme_section = f"""# {{project_name}}

> Built with [synaptory](https://github.com/h3tech-ai/synaptory-v1) delivery pipeline.

## Quick Start

_Commands will be added as the pipeline progresses._

## Architecture

_Architecture documentation will be generated during Inception._

## Development

_Development setup will be documented during Sprint Execution._

---

This README is updated automatically as synaptory delivers."""
    Bash(f'printf "%s" \'{readme_section}\' | python3 "${{CLAUDE_PLUGIN_ROOT}}/hooks/lib/update_claude_md.py" "${{CLAUDE_PROJECT_DIR}}" --file README.md')
```

After config generation, offer:
```python
AskUserQuestion(questions=[{
  "question": "Project configured. What next?",
  "options": [
    {"label": "Start building (Recommended)", "description": "Describe what you want to build"},
    {"label": "Reverse-engineer first", "description": "Run Discover mode to understand existing codebase"},
    {"label": "Just configure — done for now"}
  ]
}])
```

**Auto-init in other modes:** Before Step 1 (Request Classification), if `.synaptory.yaml` does not exist, run Init Mode inline and then continue to the requested mode.
