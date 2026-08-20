
# Crew Preview

Launch your app, see it running, verify it works — using Claude Code's native Preview browser.

!`cat .synaptory/.protocols/visual-identity.md 2>/dev/null || true`
!`cat .synaptory/.protocols/receipt-protocol.md 2>/dev/null || true`

## Overview

This skill bridges the gap between "code written" and "code verified." It uses Claude Code's built-in Preview system to:

1. **Launch** — detect framework, configure `.claude/launch.json`, start the dev server
2. **See** — take screenshots, inspect elements, check layout
3. **Verify** — read console for errors, check network requests, validate DOM structure
4. **Test** — click buttons, fill forms, navigate pages, verify responses
5. **Adapt** — resize viewport for mobile/tablet/desktop responsive testing

## Progress Output

**Skill header** (print on start):
```
━━━ Preview ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Phase progress** (print during execution):
```
  [detect] Framework Detection
    ✓ Next.js detected (package.json)
    ⧖ creating .claude/launch.json...

  [launch] Server Startup
    ✓ Server running on port 3000
    ⧖ taking initial screenshot...

  [verify] Smoke Test
    ✓ Home page renders (200 OK, no console errors)
    ✓ 3 key pages verified
    ○ responsive check
```

**Completion summary** (print on finish):
```
✓ Preview    {N} pages verified, {M} console errors, {K} screenshots    ⏱ Xm Ys
```

---

## Phase 1: Detect Framework and Configure launch.json

### Step 1a: Check for existing launch.json

```python
Read(".claude/launch.json")
```

If it exists and has valid configurations, skip to Phase 2.

### Step 1b: Auto-detect framework

Run these checks in parallel:

```python
Glob("package.json")
Glob("next.config.*")
Glob("vite.config.*")
Glob("angular.json")
Glob("manage.py")
Glob("main.py")
Glob("app.py")
Glob("go.mod")
Glob("Gemfile")
Glob("Cargo.toml")
```

Classify using first match:

| Signal | Framework | runtimeExecutable | runtimeArgs | Port |
|--------|-----------|-------------------|-------------|------|
| `next.config.*` | Next.js | `npm` | `["run", "dev"]` | 3000 |
| `vite.config.*` | Vite | `npm` | `["run", "dev"]` | 5173 |
| `angular.json` | Angular | `npm` | `["start"]` | 4200 |
| `package.json` with `react-scripts` | CRA | `npm` | `["start"]` | 3000 |
| `package.json` with `scripts.dev` | Node.js | `npm` | `["run", "dev"]` | 3000 |
| `package.json` with `scripts.start` | Node.js | `npm` | `["start"]` | 3000 |
| `manage.py` | Django | `python` | `["manage.py", "runserver"]` | 8000 |
| `main.py`/`app.py` + uvicorn | FastAPI | `uvicorn` | `["main:app", "--reload"]` | 8000 |
| `main.py`/`app.py` + flask | Flask | `flask` | `["run"]` | 5000 |
| `go.mod` | Go | `go` | `["run", "."]` | 8080 |
| `Gemfile` with rails | Rails | `rails` | `["server"]` | 3000 |
| `Cargo.toml` | Rust | `cargo` | `["run"]` | 8080 |

For `package.json` projects, read it to inspect `scripts` and `dependencies`:

```python
Read("package.json")
```

**Monorepo handling:** If the project has `apps/` or `packages/` directories with multiple services, detect all of them and create a configuration entry for each:

```python
Glob("apps/*/package.json")
Glob("packages/*/package.json")
```

### Step 1c: Check dependencies

```python
# Node.js projects
Bash("test -d node_modules && echo 'installed' || echo 'missing'")
# If missing, install:
#   pnpm-lock.yaml → pnpm install
#   yarn.lock → yarn install
#   default → npm install
```

### Step 1d: Create .claude/launch.json

If no launch.json exists, create one:

```python
Bash("mkdir -p .claude")
Write(".claude/launch.json", json.dumps({
    "version": "0.0.1",
    "configurations": [{
        "name": detected_name,           # e.g., "web", "api", "frontend"
        "runtimeExecutable": executable,  # e.g., "npm"
        "runtimeArgs": args,              # e.g., ["run", "dev"]
        "port": port,                     # e.g., 3000
        "autoPort": True                  # handle port conflicts automatically
    }]
}, indent=2))
```

For monorepos, include multiple configurations with `cwd` pointing to each app.

---

## Phase 2: Launch Server

### Step 2a: Check if server is already running

```python
preview_list()
```

If the server is already running, skip to Phase 3. If not:

### Step 2b: Start the server

```python
preview_start(name="web")  # Use the name from launch.json
```

### Step 2c: Verify server is healthy

```python
# Check server logs for errors
preview_logs(serverId=server_id, level="error")

# If errors found, read full logs for context
preview_logs(serverId=server_id, lines=30)
```

If the server fails to start, diagnose:
1. Read error logs
2. Common fixes: missing dependencies, port conflict, missing env vars
3. Fix the issue and retry `preview_start`

---

## Phase 3: Visual Verification

### Step 3a: Take initial screenshot

```python
preview_screenshot(serverId=server_id)
```

Analyze the screenshot:
- Does the page render correctly?
- Any blank pages or error screens?
- Is the layout broken?

### Step 3b: Check DOM structure

```python
preview_snapshot(serverId=server_id)
```

The snapshot returns an accessibility tree — verify:
- Key UI elements are present (navigation, headings, forms)
- No error boundaries or fallback UI visible
- Text content matches expectations

### Step 3c: Check console for errors

```python
preview_console_logs(serverId=server_id, level="error")
```

Report any JavaScript errors, unhandled promise rejections, or React/Vue hydration mismatches.

### Step 3d: Check network for failed requests

```python
preview_network(serverId=server_id, filter="failed")
```

Report any 4xx/5xx responses or network errors (CORS, connection refused, etc.).

---

## Phase 4: Interactive Smoke Test

If the user requested verification or the project has interactive elements, perform a smoke test.

### Step 4a: Navigate key pages

Identify routes from the codebase (read router config, pages directory, or route definitions):

```python
# Next.js
Glob("app/**/page.{tsx,jsx,ts,js}")
Glob("pages/**/*.{tsx,jsx,ts,js}")

# React Router
Grep("path.*['\"/]", glob="src/**/*.{tsx,jsx}")

# API routes
Glob("app/api/**/route.{ts,js}")
```

For each key route:
```python
preview_eval(serverId=server_id, expression="window.location.href = '/route'")
preview_screenshot(serverId=server_id)
preview_console_logs(serverId=server_id, level="error")
```

### Step 4b: Test interactive elements

If forms exist on the page:
```python
# Find form inputs
preview_snapshot(serverId=server_id)

# Fill a form field
preview_fill(serverId=server_id, selector="input[name='email']", value="test@example.com")

# Click a button
preview_click(serverId=server_id, selector="button[type='submit']")

# Verify response
preview_screenshot(serverId=server_id)
preview_console_logs(serverId=server_id, level="error")
```

### Step 4c: Verify specific CSS/styles

For design verification, use `preview_inspect` instead of screenshots — it returns exact computed styles:

```python
preview_inspect(serverId=server_id, selector=".hero-title", styles=["color", "font-size", "font-weight"])
preview_inspect(serverId=server_id, selector=".cta-button", styles=["background-color", "padding", "border-radius"])
```

---

## Phase 5: Responsive Testing

If the user requests responsive testing or the project has responsive layouts:

```python
# Test mobile viewport
preview_resize(serverId=server_id, preset="mobile")
preview_screenshot(serverId=server_id)

# Test tablet viewport
preview_resize(serverId=server_id, preset="tablet")
preview_screenshot(serverId=server_id)

# Return to desktop
preview_resize(serverId=server_id, preset="desktop")
preview_screenshot(serverId=server_id)
```

For dark mode testing:
```python
preview_resize(serverId=server_id, colorScheme="dark")
preview_screenshot(serverId=server_id)
preview_resize(serverId=server_id, colorScheme="light")
```

Report layout issues found at each viewport size.

---

## Phase 6: Report Results

Print a summary of all findings:

```
━━━ Preview Results ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Framework    {detected framework}
  Server       {server name} on port {port}
  Status       Running

  Pages Verified
    ✓ /              — renders, 0 console errors
    ✓ /dashboard     — renders, 0 console errors
    ✗ /settings      — 2 console errors (TypeError: Cannot read property 'name')

  Console Errors     {N} total
  Network Failures   {N} total
  Responsive         mobile ✓  tablet ✓  desktop ✓

  Screenshots saved to session context
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### User Options

After presenting results, offer next steps:

```python
AskUserQuestion(questions=[{
  "question": "Preview complete. What next?",
  "header": "Preview",
  "options": [
    {"label": "Fix reported issues (Recommended)", "description": "Address console errors and rendering problems"},
    {"label": "Test more pages", "description": "Navigate to additional routes and verify"},
    {"label": "Run responsive check", "description": "Test mobile, tablet, and desktop viewports"},
    {"label": "Stop server", "description": "Shut down the dev server"},
    {"label": "Chat about this", "description": "Free-form discussion about the preview results"}
  ],
  "multiSelect": false
}])
```

If user selects "Stop server":
```python
preview_stop(serverId=server_id)
```

---

## Pipeline Integration API

When the orchestrator needs local deployment verification at phase boundaries, it invokes Preview mode in **pipeline mode** — a non-interactive verification sequence.

### Pipeline Mode vs Standalone Mode

| Aspect | Pipeline Mode | Standalone Mode |
|--------|--------------|-----------------|
| **Invoked by** | Orchestrator (synaptory phases) | User (`/synaptory`) |
| **Interactive** | No — returns results, orchestrator decides | Yes — presents AskUserQuestion |
| **Scope** | Verification only (start, check, screenshot, stop) | Full exploration (navigate, click, fill, resize) |
| **On failure** | Returns failure data → orchestrator blocks pipeline | Presents error + fix options to user |

### Pipeline Mode Sequence

The orchestrator calls these steps directly (no Preview mode skill invocation needed):

1. **Ensure launch.json** — `Read(".claude/launch.json")`, create if missing via framework detection
2. **Start server** — `preview_start(name="{server_name}")`
3. **Verify health** — `preview_console_logs(serverId, level="error")` + `preview_network(serverId, filter="failed")`
4. **Screenshot** — `preview_screenshot(serverId)` for visual verification
5. **Return results** — orchestrator evaluates:
   - `server_running`: did preview_start succeed?
   - `console_errors`: count from preview_console_logs
   - `network_failures`: count from preview_network
   - `screenshot_taken`: visual verification available
6. **Stop server** — `preview_stop(serverId)` when verification complete

### Result Interpretation

| Result | Pipeline Action |
|--------|----------------|
| Server starts, 0 critical errors, pages render | **PASS** — proceed to next phase |
| Server starts, non-critical warnings only | **PASS with warnings** — proceed, include in report |
| Any referenced asset non-200 (`preview_asset_check.py` exit 1) | **BLOCK** — the page "renders" but its stylesheets/scripts don't load. Route to PE (deploy-artifact copy step) or SE (broken reference) |
| Server fails to start | **BLOCK** — route to SE/Platform Engineer for fix |
| Pages render blank or with error boundaries | **BLOCK** — route to SE (frontend) for fix |
| E2E tests fail against live server | **BLOCK** — route to QA/SE for fix |
| Docker containers fail to start | **BLOCK** — route to Platform Engineer for fix |
| Health endpoint doesn't respond | **BLOCK** — route to SE for health endpoint fix |

### Verification Commands for Pipeline Receipts

When Preview mode is used during pipeline verification, the phase completion receipt should include:

```json
"verification_commands": [
  {"command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/lib/preview_asset_check.py http://localhost:{port} /route-a /route-b", "exit_code": 0, "summary": "All routes render and every referenced asset returns 200"},
  {"command": "test -f .claude/launch.json", "exit_code": 0, "summary": "launch.json configured"}
]
```

A root-only `curl` is NOT an acceptable smoke check (#134 GAP-9): `GET / == 200` passes even when every stylesheet and script 404s. `preview_asset_check.py <base_url> [route ...]` fetches each route's HTML, extracts every same-origin asset reference (scripts, stylesheets, images, preloads, CSS `url(...)`/`@import` one level deep) and GETs each one — JSON `{assets_checked, failures, passed}` on stdout, exit 1 on any non-2xx asset.

---

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Using `preview_screenshot` for style verification | Use `preview_inspect` with specific CSS properties — more accurate than visual comparison |
| Not checking console logs after navigation | Always run `preview_console_logs(level="error")` after each page load |
| Hardcoding selectors that break on re-render | Use `preview_snapshot` to get current element UIDs, then use those for clicks/fills |
| Forgetting to check network failures | `preview_network(filter="failed")` catches CORS errors, missing APIs, broken assets |
| Running responsive tests without returning to desktop | Always end with `preview_resize(preset="desktop")` |
| Not creating launch.json | Native preview requires `.claude/launch.json` — always create it if missing |
| Testing before dependencies are installed | Check `node_modules/` (or equivalent) exists before launching |
| Root-only `curl` as the smoke check | `GET / == 200` passes even when every stylesheet/script 404s. Use `preview_asset_check.py <base_url> [routes]` — it GETs every referenced asset |
| Next.js standalone deploy without static assets | `next build` output `.next/static` (and `public/`) must be copied next to the standalone server (`.next/standalone/`) or every asset 404s while the page still returns 200. Never `2>/dev/null` the copy — check its exit code; a silent-fail `cp` was the GAP-9 field incident |

---

## Receipt & Verification Protocol

Before writing your receipt, complete ALL verification steps:

### Pre-Receipt Checklist

1. [ ] Server launched and responding
2. [ ] Initial screenshot taken — page renders without blank/error screens
3. [ ] Console checked — no critical JavaScript errors
4. [ ] Network checked — no failed API requests
5. [ ] At least one page visually verified

### Required verification_commands

```json
"verification_commands": [
  {"command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/lib/preview_asset_check.py http://localhost:{port} /route-a /route-b", "exit_code": 0, "summary": "All routes render and every referenced asset returns 200"},
  {"command": "test -f .claude/launch.json", "exit_code": 0, "summary": "launch.json exists"}
]
```

### Receipt Template

```json
{
  "story_id": "PREVIEW-001",
  "role": "software-engineer",
  "artifacts": [".claude/launch.json"],
  "metrics": {
    "pages_verified": 0,
    "console_errors": 0,
    "network_failures": 0,
    "responsive_viewports_tested": 0
  },
  "verification_commands": [
    {"command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/lib/preview_asset_check.py http://localhost:{port}", "exit_code": 0, "summary": "Root route renders with all assets 200"}
  ]
}
```
