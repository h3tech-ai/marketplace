# Report Mode

File a bug report or feature request against the Synaptory plugin. The report goes through the **control plane**, which creates a GitHub issue server-side on the user's behalf — clients never need direct GitHub access or a personal access token.

## Prerequisites

This mode delegates filing to the `synaptory` CLI, which authenticates against the control plane using the user's existing SYNAPTORY1 session. If the CLI is missing or the user isn't signed in, fall through to the manual fallback in Step 6.

## Step 1: Check CLI Availability

Verify the CLI is installed and the user has an active session:

```bash
command -v synaptory && synaptory whoami
```

If the CLI is missing:

```
The Synaptory CLI is not installed. Install it with:
  curl -fsSL https://synaptory.h3t.co/cli/install.sh | sh

Then re-run `/synaptory report`.
```

If the CLI is present but unauthenticated (whoami fails):

```
You need to sign in first:
  synaptory login

Then re-run `/synaptory report`.
```

Continue to Step 2 regardless — context gathering works without auth, and the offline fallback in Step 6 still prints a copyable report.

## Step 2: Gather Context Automatically

Read these files silently (no user interaction needed):

```python
# Plugin version
version = Read("${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json")  # extract version field

# Pipeline state (if exists)
state = Read(".synaptory/.orchestrator/pipeline-state.json")

# Current config
config = Read(".synaptory.yaml")

# Engagement mode
settings = Read(".synaptory/.orchestrator/settings.md")

# Last 2-3 receipts (most recent by mtime)
receipts = Glob(".synaptory/.orchestrator/receipts/*.json")
# Read the 3 most recent by filename sort (last = most recent)

# Last session context
session = Read(".synaptory/.orchestrator/last-session.md")
```

Extract key context:
- **Plugin version** from plugin.json `version` field
- **Project slug** from .synaptory.yaml `project_id` / `project` field (REQUIRED — the CP validates membership)
- **Lifecycle state** from pipeline-state.json `lifecycle_state`
- **Sprint state** from pipeline-state.json `current_sprint` + `sprint_goal` (if Scrum)
- **Engagement mode** from settings.md
- **Build mode** from .synaptory.yaml `build_mode`
- **Last agent** from most recent receipt filename + agent field
- **OS** from system info

**Privacy:** Do NOT include file contents, source code, API keys, or custom paths. Only include synaptory operational metadata.

## Step 3: Ask User to Describe the Issue

```python
AskUserQuestion(questions=[{
  "question": "What issue are you experiencing with synaptory?",
  "header": "Report Issue",
  "options": [
    {"label": "Bug — something broke or doesn't work", "description": "Unexpected errors, crashes, wrong behavior"},
    {"label": "Feature request — something's missing", "description": "A capability you wish the plugin had"},
    {"label": "Documentation — confusing or incorrect docs", "description": "Help text, guides, or reference issues"},
    {"label": "Performance — too slow or resource-heavy", "description": "Slowness, high memory use, timeouts"},
    {"label": "Chat about this", "description": "Describe the issue in your own words"}
  ],
  "multiSelect": false
}])
```

After category selection, ask for specifics:

```python
AskUserQuestion(questions=[{
  "question": "Please describe the issue. What happened? What did you expect instead?",
  "header": "Issue Details"
}])
```

## Step 4: Compose the Issue

**The body is capped at 8,000 characters (#808).** The CLI refuses an over-long body before it reaches the network, so nothing is sent and nothing is written to `.failed-reports/` — but you still have to compose within the cap. A long analysis belongs in a linked document with the issue body carrying the summary; #807's first draft was 14.2k characters and cost two failed submissions before this was checked client-side.

Build a structured issue body:

```markdown
## Description

{user's description}

## Category

{Bug | Feature Request | Documentation | Performance}

## Environment

| Field | Value |
|-------|-------|
| synaptory version | {version from plugin.json} |
| Lifecycle state | {lifecycle_state or "no pipeline active"} |
| Engagement mode | {engagement or "not set"} |
| Build mode | {build_mode or "scrum"} |
| Sprint / Ticket | {current_sprint} (Scrum) or ticket #{cumulative_ticket_number} (Kanban) or "N/A" |
| OS | {platform} |

## Reproduction Context

- Last mode executed: {from most recent receipt agent + task_id}
- Pipeline state: {lifecycle_state} ({total_elapsed} elapsed)
- Last agent: {agent name} — status: {status}

## Expected vs Actual

{from user input — what they expected vs what happened}

---
*Filed via synaptory Report mode*
```

> The control plane will prepend a non-spoofable identity block (UPN, project, session, timestamp) before the issue is sent to GitHub. The reporter's identity is always traceable; clients cannot forge it.

## Step 5: Preview and Confirm

```python
AskUserQuestion(questions=[{
  "question": "Here's the issue I'll file via the control plane:\n\n"
    "**Title:** [{category}] {concise title}\n\n"
    "{issue body preview}\n\n"
    "Ready to file?",
  "header": "Confirm Issue",
  "options": [
    {"label": "File this issue (Recommended)", "description": "Submit via synaptory CLI → control plane → GitHub"},
    {"label": "Edit the description", "description": "Modify before filing"},
    {"label": "Cancel", "description": "Don't file"}
  ]
}])
```

## Step 6: File the Issue

**If the CLI is available and authenticated:**

Compose the request as JSON and pipe it into `synaptory report submit`:

```bash
synaptory report submit --json - <<'EOF'
{
  "project": "{project_slug}",
  "category": "{bug|feature|documentation|performance}",
  "title": "[{category}] {concise title}",
  "body": "{full markdown body from Step 4}",
  "context": {
    "plugin_version": "{version}",
    "lifecycle_state": "{state}",
    "build_mode": "{build_mode}",
    "engagement_mode": "{engagement}",
    "os": "{platform}"
  }
}
EOF
```

The CLI prints `Filed: <issue_url>` on success (exit 0).

Print confirmation to the user:
```
Issue filed successfully!
  URL: {issue_url}

The Synaptory team will review this. Thank you for the report.
```

**Error handling:**

- Exit code 2 (`run \`synaptory login\``) → ask the user to run `synaptory login` and try again.
- Other non-zero exit → show the CLI's stderr message, then fall through to the offline fallback below so the report isn't lost.

**If the CLI is unavailable / unauthenticated / errored (fallback):**

Print the composed issue as a copyable block so the user can submit it through another channel (email to support, paste into the Control Plane support form when it ships, etc.):

```
I've prepared your report. To submit it later:

  1. Install or sign in to the synaptory CLI:
       curl -fsSL https://synaptory.h3t.co/cli/install.sh | sh
       synaptory login
  2. Re-run `/synaptory report` — your context will be re-gathered automatically.

For urgent issues, email this report to support@h3t.co:

─────────────────────────────────────────────────────────────
Title: [{category}] {concise title}
Project: {project_slug}
Category: {category}

{full issue body}
─────────────────────────────────────────────────────────────
```

## Category Mapping

The mode's user-facing labels map to the CLI's `--category` values like so:

| User Selection | CLI `category` value |
|----------------|-------------|
| Bug | `bug` |
| Feature request | `feature` |
| Documentation | `documentation` |
| Performance | `performance` |
