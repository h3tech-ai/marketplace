#!/usr/bin/env python3
"""Check an observed dispatch model against ``model-pins.json`` (HC0-F2, #519).

WHY THIS EXISTS
---------------
``model-pins.json`` records one exact id per tier alias so that a change of
intent is a reviewable diff rather than silent drift. ``./synaptory build``
copies the file into ``build-metadata.json`` as an AI bill of materials.

That record is INTENT, not execution provenance, and HC0-F2 is decided NOT
DELIVERABLE IN V1 (#596). Nothing on a dispatch path reads this file, the Agent
tool accepts tier aliases only, and a receipt's model field is written by the
dispatched agent about itself. This header used to describe every pinned id as
evidence of the weights that ran, and to promise a regulated customer an
audit-to-audit reproduction guarantee. Both were false in the same way.

#519 measured what actually binds on the Claude host and the answer is: not the
pin. The host's ``Agent()`` tool accepts only the tier aliases
``sonnet | opus | haiku | fable`` on its ``model`` parameter -- an exact id is
rejected with ``InputValidationError`` -- so the alias is the only value a
dispatch can carry, and the alias resolves through Anthropic's own aliasing to
whatever that tier currently points at. On 2026-09-03 that made ``opus`` mean
``claude-opus-5`` while the file pinned ``claude-opus-4-8``.

So the pin is a **declaration of intent**, not an enforced binding, and an
assurance nothing verifies is worth less than none at all. This module is the
verification half: it says whether an observed model is one the pin file
actually names.

#441 then rolled ``opus`` to ``claude-opus-5`` and recorded a ``decision`` block
per tier. That closed the known drift -- an honest receipt from a routed role is
no longer refused -- and it did **not** make the pin bind. Re-pinning changes
what the file claims, never which weights run; only the tier a role is routed to
does that. A finding from this module is therefore now a NEW observation, and
the fix is to re-measure the alias and record a fresh decision, never to widen
the accepted set until the finding stops firing.

WHAT IT REFUSES, AND WHAT IT CANNOT
-----------------------------------
It refuses a receipt whose ``model`` is **absent from the pin file** -- the
drift case, which is exactly what #409's run hit and what nothing caught.

It does **not** refuse a forged model string. The value it reads is written by
the dispatched subagent itself, from the authored prompt line
``model: (the model you are running on)``. A receipt that names
``claude-opus-4-8`` while the dispatch really ran on ``claude-opus-5`` passes
every check here and every check in ``receipt_validator.validate_receipt``,
which only requires ``model`` to be a non-empty string whose family does not
contradict ``backend``. The authoritative value lives in the host's own
transcript / ``modelUsage`` accounting, which no part of this plugin reads.
A presence check is not an authorization check (#493).

Variant handling is **data-driven on purpose**: the accepted set is each tier's
``model_id`` plus its declared ``runtime_variants``, nothing more. The file's
own ``runtime_variants_note`` is the authority, which is why
``claude-haiku-4-5-1m`` is refused -- Anthropic publishes no 1M Haiku 4.5 and
the file says to treat that id as unsupported.
"""

from __future__ import annotations

import json
import os
import re
import sys
from functools import lru_cache
from typing import Dict, FrozenSet, List, Optional, Tuple

BACKENDS_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PINS_PATH = os.path.join(BACKENDS_DIR, "model-pins.json")
CLAUDE_WRAPPER_PATH = os.path.join(BACKENDS_DIR, "claude.md")

#: The only values the Claude Code ``Agent()`` tool accepts on its ``model``
#: parameter. Measured on Claude Code 2.1.236: passing an exact id such as
#: ``claude-sonnet-4-6`` is rejected with
#: ``InputValidationError: Invalid option: expected one of
#: "sonnet"|"opus"|"haiku"|"fable"``. This is why the pin cannot reach a
#: dispatch through the authored ``Agent(model=...)`` call (#519).
AGENT_TOOL_MODEL_VALUES = frozenset({"sonnet", "opus", "haiku", "fable"})

#: Step 2 tier rows in ``claude.md``, e.g. ``| code_reviewer | opus | opus |``.
_TIER_ROW = re.compile(
    r"^\|\s*([a-z_]+)\s*\|\s*([a-z]+)\s*\|\s*([a-z]+)\s*\|\s*$", re.MULTILINE
)

#: Backends whose receipts are governed by this file. ``model-pins.json``
#: describes itself as pins "for plugin-claude/", so a Codex or Cursor receipt
#: is out of scope rather than non-conformant.
GOVERNED_BACKENDS = frozenset({"claude"})


def load_pins(path: Optional[str] = None) -> Dict[str, dict]:
    """Return the ``tiers`` mapping from ``model-pins.json``."""
    with open(path or MODEL_PINS_PATH, encoding="utf-8") as handle:
        return json.load(handle)["tiers"]


def pinned_model_ids(pins: Optional[Dict[str, dict]] = None) -> FrozenSet[str]:
    """Every model id the pin file names: each tier's id plus its declared variants."""
    pins = pins if pins is not None else load_pins()
    named = set()
    for tier in pins.values():
        named.add(tier["model_id"])
        named.update(tier.get("runtime_variants") or [])
    return frozenset(named)


def tier_for_model(
    model: str, pins: Optional[Dict[str, dict]] = None
) -> Optional[str]:
    """Return the tier alias that names ``model``, or ``None`` if none does."""
    pins = pins if pins is not None else load_pins()
    for alias, tier in pins.items():
        if model == tier["model_id"] or model in (tier.get("runtime_variants") or []):
            return alias
    return None


@lru_cache(maxsize=4)
def role_tiers(path: Optional[str] = None) -> Dict[str, Tuple[str, str]]:
    """Parse ``claude.md``'s Step 2 table into ``{role: (autonomous, controlled)}``.

    Cached: ``main`` checks a whole receipts directory and the table does not
    change under a single run.
    """
    with open(path or CLAUDE_WRAPPER_PATH, encoding="utf-8") as handle:
        text = handle.read()
    rows = {
        role: (autonomous, controlled)
        for role, autonomous, controlled in _TIER_ROW.findall(text)
        if "_" in role
    }
    if not rows:
        raise ValueError(f"no role-to-tier rows parsed from {path or CLAUDE_WRAPPER_PATH}")
    return rows


def check_model(model: str, pins: Optional[Dict[str, dict]] = None) -> Optional[str]:
    """Refuse a model the pin file does not name. Returns a problem, or ``None``.

    This is the check whose absence #519 is about: #409 observed CR dispatches
    on ``claude-opus-5``, a model no tier names, and nothing anywhere said so.
    """
    pins = pins if pins is not None else load_pins()
    if not isinstance(model, str) or not model.strip():
        return "model is missing or empty, so the dispatch has no recorded provenance at all"
    model = model.strip()
    if model in AGENT_TOOL_MODEL_VALUES:
        return (
            f"model {model!r} is a TIER ALIAS, not an exact id. An alias records "
            f"'whichever weights that tier pointed at on the day'. HC0-F2 aimed "
            f"to close that gap and is decided NOT DELIVERABLE IN V1 (#596), so "
            f"the alias is what a dispatch actually carries and this is a "
            f"reporting mismatch rather than a control failure."
        )
    named = pinned_model_ids(pins)
    if model not in named:
        return (
            f"model {model!r} is named by no tier in model-pins.json "
            f"(pinned: {', '.join(sorted(named))}). Either the dispatch ran on "
            f"unpinned weights or the pin file has drifted behind the models in "
            f"use; both make the AI-BOM's statement of intent wrong, which is "
            f"all it claims to be (#596). #441 re-pinned every "
            f"tier to its measured alias resolution, so this is NEW drift rather "
            f"than the known #409/#519 case: re-measure the alias and record a "
            f"fresh `decision` block in model-pins.json. Do not widen the accepted "
            f"set to silence it."
        )
    return None


def check_receipt(receipt: dict, pins: Optional[Dict[str, dict]] = None) -> List[str]:
    """Check one receipt's recorded model against the pins and its role's tier."""
    pins = pins if pins is not None else load_pins()
    backend = receipt.get("backend")
    if isinstance(backend, str) and backend.strip() and backend not in GOVERNED_BACKENDS:
        return []

    problems = []
    model = receipt.get("model")
    problem = check_model(model if isinstance(model, str) else "", pins)
    if problem:
        problems.append(problem)
        return problems

    # The model is pinned. Is it the tier this ROLE is routed to?
    role = receipt.get("role")
    if not isinstance(role, str) or not role.strip():
        return problems
    table_key = role.strip().replace("-", "_")
    try:
        tiers = role_tiers()
    except (OSError, ValueError):
        return problems
    if table_key not in tiers:
        return problems
    ran_on = tier_for_model(model, pins)
    allowed = set(tiers[table_key])
    if ran_on not in allowed:
        problems.append(
            f"role {role!r} is routed to tier(s) {sorted(allowed)} by claude.md, "
            f"but its receipt records {model!r}, which is the {ran_on!r} tier. "
            f"A prover that silently lands on the producer's tier erases the "
            f"decorrelation #434 bought."
        )
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    """Check receipt JSON files named on the command line.

    Usable now, by an operator or by CI, against a real receipts directory:

        python3 model_pin_check.py .synaptory/.orchestrator/receipts/*.json
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(
            "usage: model_pin_check.py <receipt.json> [...]\n"
            "Refuses a receipt whose recorded model is named by no tier in "
            "model-pins.json (#519). It cannot detect a forged model string; "
            "see this module's docstring.",
            file=sys.stderr,
        )
        return 2
    pins = load_pins()
    failed = 0
    for path in argv:
        try:
            with open(path, encoding="utf-8") as handle:
                receipt = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"{path}: UNREADABLE: {exc}")
            failed += 1
            continue
        problems = check_receipt(receipt, pins)
        if problems:
            failed += 1
            for problem in problems:
                print(f"{path}: {problem}")
        else:
            print(f"{path}: ok ({receipt.get('model')})")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
