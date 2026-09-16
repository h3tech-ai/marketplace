# Backend Dispatch Protocol (plugin-claude)

> **Audience:** Synaptory Orchestrator only. This protocol governs how the Orchestrator dispatches work to agent roles inside plugin-claude.

## Purpose

In v1.0, each host plugin uses its host tool's native model family:

| Plugin | Model family |
|--------|--------------|
| plugin-claude (this plugin) | Claude (Opus / Sonnet / Haiku) |
| plugin-cursor (this repo) | Cursor Router (Claude / Codex / Gemini / Composer / Grok) — honest `backend` from the model id |
| plugin-codex (future) | OpenAI GPT / Codex models |
| plugin-antigravity (future) | Google Gemini models |

Inside **plugin-claude**, every agent role runs on Claude via the built-in `Agent()` subagent mechanism. There is no per-role backend selection — only **model tier** (Opus vs Sonnet vs Haiku) routing within the Claude family.

The `agents.default_backend` / `agents.roles` keys in `.synaptory.yaml` from v2.0 are ignored. The `backend_config.py` helper is retained as a compatibility shim that always returns `"claude"`.

## Dispatch Procedure

See [claude.md](claude.md) for the full dispatch procedure: prompt composition, per-role tier selection, `Agent()` invocation with the tier ALIAS, receipt verification, and the H3-F1 recovery ladder. The dispatch path never reads [model-pins.json](model-pins.json); see Model Tier Routing below for what that file is and is not.

## Receipt Contract

Every receipt written by a role dispatched from plugin-claude MUST set:

```json
{
  "backend": "claude",
  "model": "<the exact model ID you actually ran under>",
  ...
}
```

The `receipt_validator.py` module enforces `backend == "claude"`.

## Model Tier Routing

Model tier is resolved per role via the mapping in [claude.md](claude.md) → Step 2. Aliases (`opus`, `sonnet`, `haiku`) are recorded against exact model IDs in [model-pins.json](model-pins.json). That record is a statement of INTENT and a reviewable diff, and nothing more. This line used to promise regulated customers an audit-to-audit reproduction guarantee, which was false: nothing on the dispatch path reads the file, `Agent()` takes only the alias, and the model on a receipt is written by the dispatched agent about itself. HC0-F2 is recorded NOT DELIVERABLE IN V1 (#596, brd-plugin FR-PLUGIN-071).

## Concurrency & Depth Guard

The concurrency cap (`parallelism.max_concurrent_subagents`, default 3) and recursive-delegation depth guard (`MAX_AGENT_DEPTH=3`) are described in [claude.md](claude.md) → Step 3.
