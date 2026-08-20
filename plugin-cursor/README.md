# Synaptory for Cursor (staff install)

This is the **Cursor host overlay**. Shared state machines, tracker, and role bodies live in `plugin/` and are **composed** here by `scripts/compose.sh` (also run by `./synaptory build`). Do not hand-edit files marked `GENERATED`.

Install this tree — not `plugin/` (that is Claude Code) and not a personal `~/.cursor/skills` wrapper.

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

```bash
mkdir -p ~/.cursor/plugins/local
ln -sfn /absolute/path/to/synaptory/plugin-cursor ~/.cursor/plugins/local/synaptory
```

Reload Window. After editing `plugin/`, run `plugin-cursor/scripts/compose.sh` (or `./synaptory build`) and Reload.

CLI / CI: `agent --plugin-dir ./plugin-cursor`. Optional per-checkout sideload: the `workspaceOpen` hook may return `pluginPaths`.

## Composing from plugin/

```bash
plugin-cursor/scripts/compose.sh
# or
./synaptory build   # also recomposes this tree at /VERSION
```

Layer-2 drift tests fail CI if `plugin/` changed and generated files here were not recomposed.

## Manual check

1. Local symlink as above.
2. `/synaptory status`
3. One SE→QE→CR story on a scratch `.synaptory` project.
