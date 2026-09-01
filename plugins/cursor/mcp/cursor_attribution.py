#!/usr/bin/env python3
"""Host-observed receipt attribution for Cursor agents.

Cursor Router may hide the routed model. Never invent `backend: claude`.
Stamp only `ide` / `plugin_version`. If the receipt already has a model id
from hook stdin, keep it; otherwise leave model empty rather than guess.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def plugin_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / ".cursor-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "0.0.0"
    return str(payload.get("version") or "0.0.0")


def attribute_receipt_from_dispatch(
    project: Path,
    path: Path,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Stamp host metadata; do not invent model or token_usage."""
    del project, started_at  # dispatch time is recorded on the story, not guessed here
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("receipt is unreadable: %s" % exc) from exc
    if not isinstance(receipt, dict):
        raise ValueError("receipt must contain a JSON object")
    if receipt.get("backend") not in (None, "", "cursor"):
        return {"patched": False, "reason": "non-cursor backend"}
    receipt["backend"] = "cursor"
    receipt["ide"] = "cursor"
    receipt["plugin_version"] = plugin_version()
    model = receipt.get("model")
    if not isinstance(model, str) or not model.strip():
        receipt.pop("model", None)
    _atomic_json(path, receipt)
    return {
        "patched": True,
        "ide": "cursor",
        "plugin_version": receipt["plugin_version"],
        "model": receipt.get("model"),
    }
