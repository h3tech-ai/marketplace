# Doctor Mode

Configuration diagnostic tool for synaptory. Validates the project's plugin configuration, control-plane connectivity, and workspace hygiene. Produces a clear pass/warn/fail report with remediation guidance for every finding.

## Overview

Run this mode whenever:
- A project is being migrated from the legacy v2.1 plugin
- A `/synaptory` dispatch returns unexpected errors
- A new team member sets up the plugin
- After any manual edits to `.synaptory.yaml` or `.claude/settings.json`

## Step 1: Collect Diagnostics

Read configuration and environment data. Do NOT modify any files in this step.

```python
import json, os, re

project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")

# --- 1a. .synaptory.yaml ---
yaml_raw = Bash(f'cat "{project_dir}/.synaptory.yaml" 2>/dev/null || echo "__MISSING__"')
yaml_ok = yaml_raw != "__MISSING__" and "version:" in yaml_raw

# Config values come from the runtime's own readers, never from a regex here.
# A column-0 regex cannot match an indented key (agents.default_backend), and
# this mode holding its own copy of the engagement vocabulary is how it came to
# treat the legacy alias as canonical when `mode_reader` says otherwise (#846).
# (String concatenation, not an f-string: once the path directive expands to a
# `${...}` plugin-root token, an f-string would read it as a Python placeholder.)
facts_json = Bash(
    'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/doctor.py" facts "' + project_dir + '" 2>/dev/null'
    ' || echo "{}"'
)
try:
    facts = json.loads(facts_json) or {}
except Exception:
    facts = {}
facts_ok = bool(facts)

cfg_project_id     = facts.get("project_id", "")
cfg_build_mode     = facts.get("build_mode", "scrum")
cfg_engagement     = facts.get("engagement", {"value": "", "kind": "unavailable", "canonical": ""})
cfg_backend        = facts.get("declared_backend", "") or "claude"
cfg_baa            = bool(facts.get("baa_enforced", False))
cli_name           = facts.get("cli", "synaptory")   # synaptory-local on the local channel

# Detect legacy v2.1 patterns
has_master_pw      = "master_password" in yaml_raw
has_litellm        = "litellm" in yaml_raw.lower()

# --- 1b. enabledPlugins, in every settings scope ---
# `claude plugin install` defaults to USER scope, so a plugin that is enabled
# and working is often absent from the project file. Read all three scopes.
SETTINGS_FILES = {
    "user":    os.path.expanduser("~/.claude/settings.json"),
    "project": f"{project_dir}/.claude/settings.json",
    "local":   f"{project_dir}/.claude/settings.local.json",
}
scope_plugins = {}   # scope -> its enabledPlugins dict
project_settings_ok = False
for scope, path in SETTINGS_FILES.items():
    raw = Bash(f'cat "{path}" 2>/dev/null || echo "__MISSING__"')
    if scope == "project":
        project_settings_ok = raw != "__MISSING__"
    try:
        scope_plugins[scope] = (json.loads(raw) if raw != "__MISSING__" else {}).get("enabledPlugins", {})
    except Exception:
        scope_plugins[scope] = {}

synaptory_enabled  = any(p.get("synaptory@h3tech-ai", False) for p in scope_plugins.values())
tracker_scopes     = [sc for sc, p in scope_plugins.items() if "tracker@h3tech-ai" in p]
tracker_plugin_on  = bool(tracker_scopes)

# --- 1c. Plugin and CLI versions ---
# The plugin reads its own manifest. The directive resolves to the executing
# plugin's root, so this holds for a cache install, a local install and a
# sideloaded checkout alike. There is NO plugin-version floor: the only version
# relation the plugin enforces is `cli_min_version`, checked against the CLI by
# the SessionStart hook (synaptory-access-token-check.sh).
plugin_json_raw  = Bash('cat "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json" 2>/dev/null || echo "__MISSING__"')
plugin_installed = plugin_json_raw != "__MISSING__"
try:
    plugin_meta    = json.loads(plugin_json_raw) if plugin_installed else {}
    plugin_version = plugin_meta.get("version", "?")
    cli_min_version = plugin_meta.get("cli_min_version", "")
except Exception:
    plugin_version, cli_min_version = "?", ""

def semver(text):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None

# --- 1d. CLI & control plane ---
# URL comes from the stamped CLI (`{cli_name} status`), not from env.
# SYNAPTORY_CONTROL_PLANE_URL / SYNAPTORY_CP_ENV are ignored at runtime.
status_out     = Bash(f"{cli_name} status 2>&1")
cp_url         = ""
for line in status_out.splitlines():
    if line.strip().startswith("control_plane_url:"):
        cp_url = line.split(":", 1)[-1].strip()
        break
whoami_out     = Bash(f"{cli_name} whoami 2>&1")
whoami_ok      = "upn:" in whoami_out or "email:" in whoami_out
whoami_upn     = ""
for line in whoami_out.splitlines():
    if "upn:" in line or "email:" in line:
        whoami_upn = line.split(":", 1)[-1].strip()
        break
cli_version_out = Bash(f"{cli_name} version 2>&1")
cli_version     = semver(cli_version_out)

# Project existence check (only if project_id is set and auth works)
project_ref    = cfg_project_id or os.environ.get("SYNAPTORY_PROJECT_ID", "")
project_out    = ""
project_ok     = False
if project_ref and whoami_ok:
    project_out = Bash(f'{cli_name} projects show "{project_ref}" 2>&1')
    project_ok  = "slug:" in project_out or "name:" in project_out or project_ref in project_out

# --- 1e. Workspace hygiene ---
protocols_dir  = Bash(f'ls "{project_dir}/.protocols/" 2>/dev/null | wc -l').strip()
has_protocols  = int(protocols_dir) > 0 if protocols_dir.isdigit() else False
enc_files      = Bash(f'find "{project_dir}" -maxdepth 3 -name "*.enc" 2>/dev/null | head -5').strip()
has_enc        = bool(enc_files)

env_project_id = os.environ.get("SYNAPTORY_PROJECT_ID", "")
env_cp_url     = os.environ.get("SYNAPTORY_CONTROL_PLANE_URL", "")
env_cp_env     = os.environ.get("SYNAPTORY_CP_ENV", "")
```

## Step 2: Evaluate Each Check

Map raw data to pass / warn / fail results. Use this scoring:

| Symbol | Meaning |
|--------|---------|
| ✓ PASS  | Requirement met |
| ⚠ WARN  | Recommended but not blocking |
| ✗ FAIL  | Blocking — dispatch will fail or produce incorrect results |

```python
checks = []

# ── Group A: .synaptory.yaml ───────────────────────────────────────────────

checks.append({
    "group": "Config file",
    "check": ".synaptory.yaml present",
    "result": "PASS" if yaml_ok else "FAIL",
    "fix": "Run `/synaptory init` to generate from the canonical template."
})

if not facts_ok:
    checks.append({
        "group": "Config file",
        "check": "config readable through the plugin runtime",
        "result": "FAIL",
        "fix": "`doctor.py facts` (hooks/lib in the plugin) produced no output. "
               "Reinstall the plugin; project_id, build_mode and engagement_mode cannot be checked until it runs."
    })

checks.append({
    "group": "Config file",
    "check": "project_id set",
    "result": "PASS" if cfg_project_id else "FAIL",
    "fix": "Add  project_id: \"<your-slug>\"  (e.g. \"hano-app\") near the top of .synaptory.yaml. "
           "This is required for telemetry attribution."
})

# The engagement vocabulary belongs to `mode_reader`; doctor only reports what
# `mode_reader.classify_mode` said (canonical / alias / absent / unknown).
eng_kind, eng_value, eng_target = cfg_engagement["kind"], cfg_engagement["value"], cfg_engagement["canonical"]
if eng_kind == "unavailable":
    eng_result, eng_fix = "WARN", "Not checked: the config could not be read (see above)."
elif eng_kind == "canonical":
    eng_result, eng_fix = "PASS", ""
elif eng_kind == "alias":
    eng_result = "WARN"
    eng_fix = (f"{eng_value!r} is an alias that still works. Consider renaming it to "
               f"engagement_mode: \"{eng_target}\", the canonical spelling for the same behavior.")
elif eng_kind == "absent":
    eng_result = "WARN"
    eng_fix = (f"engagement_mode is not set, so the runtime default ({eng_target}) applies. "
               f"Set engagement_mode: \"{eng_target}\" to make it explicit, or \"interactive\" "
               "for full user-in-the-loop.")
else:
    eng_result = "FAIL"
    eng_fix = (f"{eng_value!r} is not a known engagement mode. Set engagement_mode to "
               "\"structured\" (bounded workflow, reduced interaction) or \"interactive\" "
               "(strategic-role gates, user in the loop).")
checks.append({
    "group": "Config file",
    "check": "engagement_mode canonical",
    "result": eng_result,
    "detail": f"found: {eng_value!r}" if eng_kind != "absent" else "not set",
    "fix": eng_fix
})

checks.append({
    "group": "Config file",
    "check": "build_mode valid",
    "result": "PASS" if cfg_build_mode in {"scrum", "kanban", "spq"} else "FAIL",
    "detail": f"found: {cfg_build_mode!r}",
    "fix": "Set build_mode to \"spq\", \"scrum\" (default) or \"kanban\"."
})

# BAA check — INFORMATIONAL. Absent `baa_enforced` is the normal state of a
# non-healthcare project and the Codex host refuses a project that sets it, so
# doctor never recommends turning it on and never edits it in.
checks.append({
    "group": "Config file",
    "check": "healthcare.baa_enforced declared",
    "result": "PASS",
    "detail": "true (healthcare project)" if cfg_baa else "not set (non-healthcare project)",
    "fix": ""
})

# BAA + backend consistency
if cfg_baa:
    BAA_COVERED = {"claude"}
    checks.append({
        "group": "Config file",
        "check": "BAA-covered backend when baa_enforced",
        "result": "PASS" if cfg_backend in BAA_COVERED else "FAIL",
        "detail": f"agents.default_backend: {cfg_backend!r}",
        "fix": f"With baa_enforced: true, default_backend must be one of: {', '.join(BAA_COVERED)}."
    })

# Legacy v2.1 patterns
checks.append({
    "group": "Config file",
    "check": "No legacy master_password",
    "result": "FAIL" if has_master_pw else "PASS",
    "fix": "Remove master_password from .synaptory.yaml. Encryption moved to the control plane (ADR-016)."
})
checks.append({
    "group": "Config file",
    "check": "No LiteLLM gateway config",
    "result": "FAIL" if has_litellm else "PASS",
    "fix": "Remove litellm.* keys. The LiteLLM gateway was retired. Use agents.default_backend instead."
})

# ── Group B: .claude/settings.json ───────────────────────────────────────

checks.append({
    "group": "Plugin settings",
    "check": "synaptory@h3tech-ai enabled",
    "result": "PASS" if synaptory_enabled else "FAIL",
    "detail": "scope: " + ", ".join(sc for sc, p in scope_plugins.items() if p.get("synaptory@h3tech-ai", False)) if synaptory_enabled else "not enabled in user, project or local settings",
    "fix": "Add \"synaptory@h3tech-ai\": true to enabledPlugins in .claude/settings.json "
           "(or install it for your user: /plugin install synaptory@h3tech-ai)."
})
checks.append({
    "group": "Plugin settings",
    "check": "tracker@h3tech-ai removed (v2.1 artifact)",
    "result": "FAIL" if tracker_plugin_on else "PASS",
    "detail": ("enabled in: " + ", ".join(tracker_scopes)) if tracker_plugin_on else "",
    "fix": "Remove \"tracker@h3tech-ai\" from enabledPlugins. The tracker ships inside synaptory."
})

# ── Group C: Plugin and CLI versions ─────────────────────────────────────

checks.append({
    "group": "Plugin",
    "check": "Plugin installed",
    "result": "PASS" if plugin_installed else "FAIL",
    "detail": f"version: {plugin_version}" if plugin_installed else "plugin manifest not readable",
    "fix": "Run in Claude Code:\n"
           "  /plugin marketplace add https://github.com/h3tech-ai/marketplace\n"
           "  /plugin install synaptory@h3tech-ai"
})

# No plugin-version floor exists. The one version relation the plugin enforces
# is the CLI floor, `cli_min_version` in the plugin manifest, which the
# SessionStart hook checks and refuses on.
if plugin_installed and cli_min_version:
    if cli_version is None:
        cli_ok, cli_detail = False, f"could not read the {cli_name} version"
    else:
        cli_ok = cli_version >= (semver(cli_min_version) or (0, 0, 0))
        cli_detail = f"{cli_name} {'.'.join(map(str, cli_version))}, plugin requires >= {cli_min_version}"
    checks.append({
        "group": "Plugin",
        "check": "CLI meets the plugin's cli_min_version",
        "result": "PASS" if cli_ok else "FAIL",
        "detail": cli_detail,
        "fix": f"Update the CLI: {cli_name} update  (or reinstall: curl -fsSL https://synaptory.h3t.ai/cli/install.sh | bash)"
    })

# ── Group D: Control plane ───────────────────────────────────────────────

checks.append({
    "group": "Control plane",
    "check": "CLI stamped control-plane URL",
    "result": "PASS" if cp_url else "FAIL",
    "detail": cp_url or "(empty)",
    "fix": "Prod: curl -fsSL https://synaptory.h3t.ai/cli/install.sh | bash\nLocal: ./synaptory deploy local (installs synaptory-local). Do not export SYNAPTORY_CONTROL_PLANE_URL."
})
if env_cp_url or env_cp_env:
    checks.append({
        "group": "Control plane",
        "check": "No leftover SYNAPTORY_CONTROL_PLANE_URL / SYNAPTORY_CP_ENV",
        "result": "WARN",
        "detail": f"CONTROL_PLANE_URL={env_cp_url!r} CP_ENV={env_cp_env!r}",
        "fix": "unset SYNAPTORY_CONTROL_PLANE_URL SYNAPTORY_CP_ENV — they are ignored and mix local/prod installs."
    })
checks.append({
    "group": "Control plane",
    "check": f"CLI authenticated ({cli_name} whoami)",
    "result": "PASS" if whoami_ok else "FAIL",
    "detail": f"upn: {whoami_upn}" if whoami_ok else whoami_out[:120],
    "fix": f"Run: {cli_name} login"
})

if project_ref:
    checks.append({
        "group": "Control plane",
        "check": f"Project '{project_ref}' exists & member",
        "result": "PASS" if project_ok else "FAIL",
        "detail": project_out[:120] if not project_ok else f"slug: {project_ref}",
        "fix": f"Provision the project:\n"
               f"  {cli_name} projects create {project_ref}\n"
               f"  {cli_name} projects add-member {project_ref} {whoami_upn or '<your-upn>'}"
    })
else:
    checks.append({
        "group": "Control plane",
        "check": "project_id configured for CP lookup",
        "result": "WARN",
        "fix": "Set project_id in .synaptory.yaml so the CP can be verified."
    })

# ── Group E: Workspace hygiene ───────────────────────────────────────────

checks.append({
    "group": "Workspace",
    "check": "No legacy .protocols/ directory (v2.1 artifact)",
    "result": "FAIL" if has_protocols else "PASS",
    "fix": "Delete .protocols/  — protocols are CP-delivered (ADR-016)."
})
checks.append({
    "group": "Workspace",
    "check": "No encrypted .enc skill files (v2.1 artifact)",
    "result": "FAIL" if has_enc else "PASS",
    "detail": enc_files[:200] if has_enc else "",
    "fix": "Delete *.enc files — skills are CP-delivered, not stored locally."
})

# SYNAPTORY_PROJECT_ID env consistency
if env_project_id and cfg_project_id and env_project_id != cfg_project_id:
    checks.append({
        "group": "Workspace",
        "check": "SYNAPTORY_PROJECT_ID env matches project_id",
        "result": "WARN",
        "detail": f"env={env_project_id!r}  yaml={cfg_project_id!r}",
        "fix": f"Update SYNAPTORY_PROJECT_ID to {cfg_project_id!r} or remove the env var (yaml value is preferred)."
    })

# --- Continuation loop (#820) ---
# ASK THE ENGINE, DO NOT RE-DERIVE THE CONDITIONS. `loop_engine.loop_status`
# is the same predicate `should_continue` stops on, so this report cannot say
# the loop is live while the engine stops on it. Reproducing the three tests
# here in prose is exactly how the two would come to disagree.
#
# This check exists because nothing reported the switch. An engagement left
# ten Work Units queued for four hours overnight with zero attempts
# authorized, ran three Cycles to Checkpoint that way, and blamed the model.
loop_json = Bash(
    'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/loop_engine.py" status "' + project_dir + '" 2>/dev/null'
    ' || echo "{}"'
)
try:
    loop = json.loads(loop_json) or {}
except Exception:
    loop = {}
if loop and not loop.get("enabled", True):
    # `not_structured` is a MODE, not a misconfiguration — interactive is
    # user-in-the-loop by design. FAIL only when autonomy was switched off.
    is_defect = loop.get("reason") != "not_structured"
    checks.append({
        "group": "Delivery",
        "check": "Continuation loop drives the pipeline",
        "result": "FAIL" if is_defect else "WARN",
        "detail": (
            f"{loop.get('reason')}: {loop.get('detail')} — Work Units will sit "
            "queued with no attempts authorized and nothing else will say so."
        ),
        "fix": loop.get("remedy", ""),
    })
```

## Step 3: Print Diagnostic Report

Format as a structured table grouped by category.

```python
ICONS = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}
COLORS = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}

total  = len(checks)
passed = sum(1 for c in checks if c["result"] == "PASS")
warned = sum(1 for c in checks if c["result"] == "WARN")
failed = sum(1 for c in checks if c["result"] == "FAIL")

project_label = cfg_project_id or env_project_id or "(unset)"

log(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  synaptory doctor · {project_label}
  CP: {cp_url or "(not set)"}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")

current_group = None
for c in checks:
    if c["group"] != current_group:
        current_group = c["group"]
        log(f"\n  {current_group}")
        log("  " + "─" * 60)

    icon   = ICONS[c["result"]]
    label  = c["check"]
    detail = c.get("detail", "")
    detail_str = f"  ({detail})" if detail else ""

    log(f"  {icon} {c['result']:<4}  {label}{detail_str}")
    if c["result"] in ("WARN", "FAIL"):
        for fix_line in c["fix"].splitlines():
            log(f"         → {fix_line}")

log(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Summary: {passed}/{total} passed  ·  {warned} warnings  ·  {failed} failures
  {"✓ READY — project is correctly configured" if failed == 0 else
   "✗ ACTION REQUIRED — fix the failures above before dispatching"}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
```

## Step 4: Offer One-Shot Auto-Fix

After printing the report, classify every failing/warning check into two buckets:

- **Auto-fixable** — local file edits that are safe to apply without side-effects: adding `project_id`, renaming a legacy engagement alias, removing `master_password`, fixing `.claude/settings.json` plugin entries. These are reversible with `git checkout`.
- **Manual-only** — anything that touches external systems or the filesystem beyond the two config files: CLI authentication, control-plane project provisioning, deleting `.protocols/` or `.enc` files, removing `litellm` keys, an unrecognised or absent engagement mode, updating shell env vars. Doctor prints the exact commands; the user runs them.

```python
# Classify what can be auto-fixed. Every entry here MUST have a matching
# handler in the apply block below, and every handler an entry here: a promise
# with no handler is how doctor offered fixes it then silently skipped.
auto_fixes = []

if facts_ok and not cfg_project_id:
    auto_fixes.append("Add project_id to .synaptory.yaml")
if cfg_engagement["kind"] == "alias":
    # Rename to the canonical spelling of the SAME mode (autonomous -> structured,
    # controlled -> interactive). Never to a different-meaning mode.
    auto_fixes.append(
        f"Rename engagement_mode: {cfg_engagement['value']} → {cfg_engagement['canonical']} in .synaptory.yaml"
    )
if cfg_engagement["kind"] == "absent" and yaml_ok:
    auto_fixes.append(f"Add engagement_mode: \"{cfg_engagement['canonical']}\" (the runtime default) to .synaptory.yaml")
if has_master_pw:
    auto_fixes.append("Remove master_password from .synaptory.yaml")
if "project" in tracker_scopes:
    auto_fixes.append("Remove tracker@h3tech-ai from .claude/settings.json")
if not synaptory_enabled and project_settings_ok:
    auto_fixes.append("Enable synaptory@h3tech-ai in .claude/settings.json")

manual_steps = []
if not cp_url:
    manual_steps.append("Install the CLI: curl -fsSL https://synaptory.h3t.ai/cli/install.sh | bash  (or ./synaptory deploy local for synaptory-local)")
if not whoami_ok:
    manual_steps.append(f"{cli_name} login")
if not plugin_installed:
    manual_steps.append("/plugin marketplace add https://github.com/h3tech-ai/marketplace")
    manual_steps.append("/plugin install synaptory@h3tech-ai")
if plugin_installed and cli_min_version and (cli_version is None or cli_version < (semver(cli_min_version) or (0, 0, 0))):
    manual_steps.append(f"{cli_name} update  (the plugin requires CLI >= {cli_min_version})")
if not project_ok and project_ref and whoami_ok:
    manual_steps.append(f"{cli_name} projects create {project_ref}")
    manual_steps.append(f"{cli_name} projects add-member {project_ref} {whoami_upn or '<your-upn>'}")
if cfg_engagement["kind"] == "unknown":
    manual_steps.append("Set engagement_mode to \"structured\" or \"interactive\" in .synaptory.yaml  (the current value is not a known mode, so doctor will not guess which you meant)")
if has_litellm:
    manual_steps.append("Delete the litellm keys from .synaptory.yaml  (the block is too varied to remove safely)")
for scope in tracker_scopes:
    if scope != "project":
        manual_steps.append(f"Remove tracker@h3tech-ai from enabledPlugins in the {scope} settings file ({SETTINGS_FILES[scope]})")
if has_protocols:
    manual_steps.append(f"rm -rf {project_dir}/.protocols/   # v2.1 artifact")
if has_enc:
    manual_steps.append(f"find {project_dir} -maxdepth 3 -name '*.enc' -delete   # v2.1 artifacts")
if env_project_id and cfg_project_id and env_project_id != cfg_project_id:
    manual_steps.append(f"unset SYNAPTORY_PROJECT_ID  (or set to {cfg_project_id!r} — yaml value is preferred)")
```

If there are no failures or warnings, print a clean all-clear and stop:

```python
if failed == 0 and warned == 0:
    log("  No issues found. Project is ready for dispatch.")
    # Done — do not ask any question
```

Otherwise, if there are auto-fixable items, ask a **single yes/no** before touching any files:

```python
if auto_fixes:
    fix_list = "\n".join(f"  • {f}" for f in auto_fixes)
    log(f"\n  Auto-fixable ({len(auto_fixes)} items):\n{fix_list}")

    if manual_steps:
        step_list = "\n".join(f"  • {s}" for s in manual_steps)
        log(f"\n  Manual steps (run in terminal after):\n{step_list}")

    AskUserQuestion(questions=[{
        "question": f"Apply {len(auto_fixes)} auto-fix(es) now?",
        "options": [
            {"id": "yes",    "label": "Yes — fix all of the above automatically"},
            {"id": "no",     "label": "No — I'll fix manually (commands printed above)"}
        ]
    }])
else:
    # Only manual steps remain — print them and stop
    if manual_steps:
        step_list = "\n".join(f"  • {s}" for s in manual_steps)
        log(f"\n  Nothing to auto-fix. Run these in your terminal:\n{step_list}")
```

### On "Yes — fix all automatically"

Apply each auto-fix as a targeted `Edit()`. Never rewrite the whole file from scratch — patch only what failed.

```python
yaml_text = Read(f"{project_dir}/.synaptory.yaml")

# 1. project_id missing → insert after the first non-comment, non-blank line (version or build_mode line)
if facts_ok and not cfg_project_id:
    # Find the first content line to insert after
    first_line = next(l for l in yaml_text.splitlines() if l.strip() and not l.strip().startswith("#"))
    Edit(f"{project_dir}/.synaptory.yaml",
         old_string=first_line,
         new_string=f'{first_line}\n\nproject_id: "{project_dir.split("/")[-1]}"')
    # Note: use the actual directory basename as the slug default; user can edit afterward.

# 2. engagement_mode alias → its canonical spelling (same mode, new name).
#    The target comes from mode_reader via cfg_engagement["canonical"]; it is
#    never chosen here. Patch the declared line in place so quoting style and a
#    trailing comment survive.
if cfg_engagement["kind"] == "alias":
    line = next(l for l in yaml_text.splitlines() if re.match(r"^engagement_mode\s*:", l))
    renamed = re.sub(r"^(engagement_mode\s*:\s*)[\"']?[\w-]+[\"']?",
                     lambda m: f'{m.group(1)}"{cfg_engagement["canonical"]}"', line)
    Edit(f"{project_dir}/.synaptory.yaml", old_string=line, new_string=renamed)

# 3. engagement_mode absent → declare the runtime default explicitly
if cfg_engagement["kind"] == "absent" and yaml_ok:
    first_line = next(l for l in yaml_text.splitlines() if l.strip() and not l.strip().startswith("#"))
    Edit(f"{project_dir}/.synaptory.yaml",
         old_string=first_line,
         new_string=f'{first_line}\n\nengagement_mode: "{cfg_engagement["canonical"]}"')

# 4. Remove master_password line
if has_master_pw:
    for line in yaml_text.splitlines():
        if "master_password" in line:
            Edit(f"{project_dir}/.synaptory.yaml", old_string=line + "\n", new_string="")

# 5. Fix .claude/settings.json (the PROJECT file only; other scopes are manual steps)
if "project" in tracker_scopes or (not synaptory_enabled and project_settings_ok):
    settings_obj = json.loads(Read(f"{project_dir}/.claude/settings.json"))
    if "project" in tracker_scopes:
        settings_obj.get("enabledPlugins", {}).pop("tracker@h3tech-ai", None)
    if not synaptory_enabled:
        settings_obj.setdefault("enabledPlugins", {})["synaptory@h3tech-ai"] = True
    Write(f"{project_dir}/.claude/settings.json", json.dumps(settings_obj, indent=2))

# Done — summarise what was changed
log("\n  ✓ Auto-fixes applied. Re-run /synaptory doctor to confirm.")

# Print any remaining manual steps
if manual_steps:
    step_list = "\n".join(f"  • {s}" for s in manual_steps)
    log(f"\n  Still needs manual action:\n{step_list}")
```

### On "No — I'll fix manually"

Print all remediation commands (auto-fixable ones + manual steps) and stop. No files are modified.

```python
all_steps = [f"Edit .synaptory.yaml: {f}" for f in auto_fixes] + manual_steps
step_list = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(all_steps))
log(f"\n  Remediation checklist:\n{step_list}\n\n  Re-run /synaptory doctor after fixing.")
```

## Notes

- Doctor is **read-only** — never modifies files automatically without asking.
- All checks are additive — failing one check does not skip later checks.
- `healthcare.baa_enforced` is reported, never recommended: its absence is the normal state of a non-healthcare project, and the Codex host refuses a project that sets it. Doctor never adds it. The only BAA finding is a declared non-`claude` `agents.default_backend` on a project that sets it.
- Engagement vocabulary comes from `core/lib/mode_reader.py` through `doctor.py facts`: `structured` and `interactive` are canonical, `autonomous` and `controlled` (and the hands-on / hands-off spellings) are aliases that still work and are reported as a rename suggestion, anything else is a FAIL. This document holds no copy of those sets.
- There is no plugin-version floor. The only version relation checked is the CLI against the plugin's `cli_min_version`, the same floor the SessionStart hook enforces.
- Workspace hygiene checks (`.protocols/`, `.enc`) are FAIL — these are unambiguous v2.1 leftovers with no valid use in the current plugin.
- When `project_id` is missing from the YAML, the CP project check uses `SYNAPTORY_PROJECT_ID` from env as fallback; if both are absent, the check downgrades to WARN.
