"""Extract the final Usage block from a Claude Code transcript.

Subagents cannot see their own final `Usage` — that object is emitted by the
SDK to the parent session, not into the subagent's context. The receipt
written by a subagent therefore omits `token_usage`, and the `/cost` rollup
attributes $0 to the call.

The SubagentStop hook runs in the parent context with `transcript_path`
pointing at the parent's JSONL. The last assistant message before the hook
fires carries `message.usage`. This helper parses it out so
`synaptory-verify-receipt.sh` can patch a missing `token_usage` onto the receipt
before shipping.

Pure logic. No subprocess, no network. Layer-1 testable.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Evidence contract (issue #163, GAP-7) — optional sibling module used to
# derive the canonical v3 stage from the receipt's own role when the caller
# passes no default_stage. Missing module → no stage backfill (pre-contract
# behavior). Kept import-optional so this module stays Layer-1 testable.
try:  # pragma: no cover - import plumbing
    import evidence_contract as _evidence_contract  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from hooks.lib import evidence_contract as _evidence_contract  # type: ignore
    except ImportError:
        _evidence_contract = None  # type: ignore[assignment]


_USAGE_FIELD_MAP = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}


def extract_last_usage(transcript_path: str | Path) -> dict[str, int] | None:
    """Return the most recent `usage` block from a JSONL transcript.

    Walks the transcript bottom-up and returns the first `message.usage` it
    finds, mapped to the receipt's `token_usage` shape (without `stage`,
    which the agent must still set itself). Returns None if the file is
    unreadable, empty, or contains no usage block.
    """
    path = Path(transcript_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = _find_usage(entry)
        if usage is None:
            continue
        out: dict[str, int] = {}
        for receipt_key, sdk_key in _USAGE_FIELD_MAP.items():
            v = usage.get(sdk_key, 0)
            out[receipt_key] = int(v) if isinstance(v, (int, float)) else 0
        if any(out.values()):
            return out
    return None


def _find_usage(entry: Any) -> dict | None:
    """Return the `usage` dict from a transcript entry, or None.

    Claude Code transcripts wrap the API message under `message`, and the
    `usage` object lives at `message.usage`. Some entry kinds (user, system,
    tool_result) won't carry one — they get skipped.
    """
    if not isinstance(entry, dict):
        return None
    msg = entry.get("message")
    if isinstance(msg, dict):
        u = msg.get("usage")
        if isinstance(u, dict):
            return u
    u = entry.get("usage")
    if isinstance(u, dict):
        return u
    return None


def _valid_stages() -> frozenset[str]:
    """The v3 stage enum from the evidence contract; empty when unavailable."""
    if _evidence_contract is None:
        return frozenset()
    try:
        contract = _evidence_contract.load_contract()
        return frozenset(contract.get("stages", {}).get("valid", []))
    except Exception:
        return frozenset()


def _stage_for_role(role: Any) -> str | None:
    """Canonical single stage for a receipt role, or None (unknown/multi-stage)."""
    if _evidence_contract is None or not isinstance(role, str):
        return None
    try:
        return _evidence_contract.role_stage(role)
    except Exception:
        return None


def _apply_stage_backfill(receipt: dict, default_stage: str | None) -> bool:
    """Normalize `token_usage.stage` in place; return True when modified.

    GAP-7 (#163): agents often write a descriptive stage ("implementing auth")
    or none at all. When the stage is missing/blank or not in the v3 enum AND
    a canonical stage is derivable (explicit `default_stage`, else the
    receipt's own role via evidence_contract.role_stage), set the canonical
    stage and preserve any original value as `stage_raw`. A VALID stage is
    never overwritten. Only operates on an existing token_usage dict — it
    never fabricates the block.
    """
    tu = receipt.get("token_usage")
    if not isinstance(tu, dict):
        return False
    valid = _valid_stages()
    stage = tu.get("stage")
    if isinstance(stage, str) and stage.strip():
        if not valid or stage in valid:
            return False  # valid (or unverifiable) stage — never overwrite
    canonical = default_stage or _stage_for_role(receipt.get("role"))
    if not canonical or canonical == stage:
        return False
    if isinstance(stage, str) and stage.strip():
        tu["stage_raw"] = stage
    tu["stage"] = canonical
    return True


def patch_receipt(receipt_path: str | Path, transcript_path: str | Path,
                  default_stage: str | None = None) -> bool:
    """Patch `token_usage` counts (and canonical stage) onto a receipt.

    Two independent fixes, either of which triggers a rewrite:
      1. Counts backfill — a receipt missing token_usage (or carrying only
         zeros) gets the last `message.usage` from the parent transcript.
      2. Stage backfill (GAP-7, #163) — a missing or non-enum stage is
         replaced by the canonical stage for the receipt's role (or the
         explicit `default_stage`), preserving the original as `stage_raw`.
         A valid stage is never overwritten.

    Returns True if the receipt was updated and rewritten, False otherwise.
    Never raises — failure is silent because the hook must not block
    shipping over a parse miss.
    """
    rp = Path(receipt_path)
    try:
        receipt = json.loads(rp.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict):
        return False

    changed = False

    existing = receipt.get("token_usage")
    has_counts = isinstance(existing, dict) and any(
        isinstance(existing.get(k), int) and existing.get(k) > 0
        for k in ("input", "output", "cache_read", "cache_write")
    )
    if not has_counts:
        usage = extract_last_usage(transcript_path)
        if usage is not None:
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(usage)
            receipt["token_usage"] = merged
            changed = True

    if _apply_stage_backfill(receipt, default_stage):
        changed = True

    if not changed:
        return False
    try:
        rp.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: transcript_usage.py <receipt.json> <transcript.jsonl> [stage]",
              file=sys.stderr)
        sys.exit(2)
    stage = sys.argv[3] if len(sys.argv) > 3 else None
    patched = patch_receipt(sys.argv[1], sys.argv[2], default_stage=stage)
    print("patched" if patched else "unchanged")
