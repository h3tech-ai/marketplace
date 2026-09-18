# Synaptory for Cursor (staff install)

This is the **Cursor host overlay**. Shared state machines, tracker and receipt logic live in `core/`; role bodies and the orchestrator content are Claude-authored and live in `plugin-claude/`. Both are **composed** here by `scripts/compose.sh` (also run by `./synaptory build`). Do not hand-edit files marked `GENERATED`.

Install this tree — not `plugin-claude/` (that is Claude Code) and not a personal `~/.cursor/skills` wrapper.

## IP wall (matches Claude / ADR-016)

The **public marketplace copy** is stubbed. Orchestrator `SKILL.md`, `dispatch.md` / `limitations.md` / `receipts.md`, protocols, and rules are **not** in that git tree. At runtime:

```bash
export SYNAPTORY_IDE=cursor   # hooks set this
synaptory skills get synaptory --host cursor
```

`./synaptory build` rsyncs `plugin-cursor/` into `web/dist/marketplace/plugin-cursor/` and runs `compose.py --strip-ip`. Publish **that** tree to `h3tech-ai/marketplace`, not the private working copy.

The private `plugin-cursor/skills/synaptory/SKILL.md` stays full so the API seeder can encrypt it. Source-tree symlink install can still Read it; marketplace installs cannot.

Agent `roles/*/SKILL.md` + phases still ship plaintext — same leftover as Claude (ADR-016 B2).

## Remaining platform gaps

- Public cursor.com/marketplace requires OSS — do **not** submit. Use GitHub `h3tech-ai/marketplace` (IP-stripped) or a local symlink.
- Silent model substitution (Cursor Router). Receipts record the id from hook stdin; never invent `backend: claude`.
- Cloud **VMs** skip `sessionStart` / `sessionEnd`. Private workers fire them on claim/release. Skill body restores pipeline state for VMs.
- `subagentStop` cannot hard-block finished work — followup only. **MCP `advance` refuses** without a valid receipt (fail-closed).
- HC0 (HIPAA) is **Enterprise-gated**: BAA + Privacy Mode lock + Eligible Model. Teams / Pro / Start: refuse. Plugin MCP is not BAA-covered.
- One nesting level (main → child). Roles must not spawn roles.
- No `PostCompact`, `StopFailure`, or `FileChanged` events.
- **Certified delivery path is SPQ** (`build_mode: spq`). Scrum and Kanban have not been proven end-to-end on Cursor; the orchestrator must warn and recommend SPQ.

## Staff install (public marketplace repo)

1. Admin: import GitHub `h3tech-ai/marketplace` (the **stripped** tree from `./synaptory build`). Teams: Team marketplace → Import from Repo. Personal license: clone + symlink `plugin-cursor/` from that clone.
2. Install the CLI once per laptop (same binary as Claude Code staff):

```bash
curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash
synaptory login
synaptory skills sync --host cursor
```

3. Reload Cursor. Use `/synaptory`. The stub SKILL.md fetches the real body from the control plane.

Do **not** copy the private `synaptory` repo's `plugin-cursor/` into the public marketplace — that still has full overlay files.

## Dev install (private synaptory checkout)

Bring up the local stack first so this tree gets a gitignored
`hooks/lib/cp-url.local` and `~/.local/bin/synaptory-local` is installed
without overwriting prod `synaptory`:

### Cursor IDE

```bash
./synaptory deploy local
mkdir -p ~/.cursor/plugins/local
ln -sfn /absolute/path/to/synaptory/plugin-cursor ~/.cursor/plugins/local/synaptory
```

Reload Window. After editing `plugin-claude/`, run `plugin-cursor/scripts/compose.sh` (or `./synaptory build`) and Reload.

### Cursor CLI (`cursor-agent`) — the symlink above does nothing

`~/.cursor/plugins/local` is an **IDE** convention. `cursor-agent` has no
local-plugin mechanism (`cursor-agent plugin` exposes only `marketplace`), so
with the symlink correct it still loads no Synaptory server — and **it says
nothing about it**.

That silence is the actual danger. With no Synaptory surface the agent
improvises, and the run looks successful: correct code, hand-authored QE-only
receipts into the legacy path, the kernel never advanced, and both Work Units
reported `done` while `spq_state_machine.py read` said `queued` (#332, from
#323 G4).

```bash
python3 plugin-cursor/scripts/preflight.py --project <clone> --write
cd <clone> && cursor-agent mcp enable synaptory
python3 plugin-cursor/scripts/preflight.py --project <clone>   # must print `ready:`
```

**Run the check before every headless agent run.** It cannot be a hook: a
plugin that is not loaded has no hook to fire, which is precisely why the
failure was silent. `--write` merges into an existing `.cursor/mcp.json` rather
than replacing it, so other MCP servers survive.

Exit codes: `0` ready, `1` not wired (the message carries the fix), `2` wired
but `cursor-agent` could not be asked. `2` is deliberately not `0`: "I could not
tell" must never read as "ready", or the check becomes the same silent pass it
exists to prevent.

Do **not** export `SYNAPTORY_CONTROL_PLANE_URL` or `SYNAPTORY_CP_ENV`. Cursor
hooks read `hooks/lib/cp-url.local` then the stamped `hooks/lib/cp-url`, and
resolve `synaptory-local` when that URL is loopback.

CLI / CI: `agent --plugin-dir ./plugin-cursor`. Optional per-checkout sideload: the `workspaceOpen` hook may return `pluginPaths`.

## Composing from plugin-claude/

```bash
plugin-cursor/scripts/compose.sh
# or
./synaptory build   # also recomposes this tree at /VERSION
```

Layer-2 drift tests fail CI if `plugin-claude/` changed and generated files here were not recomposed.

## Manual check

1. Local symlink as above.
2. `/synaptory status` on a project with `build_mode: spq`.
3. One SPQ Cycle (hydrate → SE→QE→CR → `declare_sync_ready` → Sync) on a scratch project. Scrum/Kanban is uncertified on Cursor — expect a warning recommending SPQ.


## Staff-pilot readiness

Run against a representative HTTPS deployment:

```bash
python3 <installed-plugin>/scripts/standard_pilot.py \
  --project-dir <project> \
  --deployment-health-url https://app.example.test/health \
  --protected-url https://app.example.test/private
```

Schema version 2 replaces the single `result` with independent
`ready_to_install` (existing plugin and deployment checks) and
`ready_to_dispatch` (at least one runtime profile can serve a capability role).
`dispatch.profiles` includes each profile's roles, availability and the exact
probe reason used by the selector; `dispatch.available_roles` identifies the
roles that can run. Availability does not authorize a particular dispatch or
certify every lifecycle role. Exit zero requires both verdicts.

Runtime checks use the installation's CLI with `runtimes doctor --read-only`;
they never register a runner or update the project availability snapshot.
An older CLI, missing binary, timeout or invalid output leaves dispatch
readiness false with `dispatch.checked: false` and `dispatch.error`, while
preserving the installation verdict. Upgrade the CLI together with the plugin.
