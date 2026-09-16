"""Layer 1 -- an authored body may not name a model id the pins do not name.

WHY THIS FILE EXISTS
--------------------
The same defect has now been chased three times, in three different shapes:

  * #440 found `backends/claude.md`'s prose pin list still naming
    `claude-opus-4-7` / `claude-sonnet-4-6` long after the pins had rolled to
    `claude-opus-4-8` / `claude-sonnet-5`, and answered it with a test that
    resolves the prose against `model-pins.json` instead of comparing literals
    (`test_model_tier_decorrelation.test_prose_pin_list_matches_the_pin_file`).
  * #441's re-pin of the `opus` tier to `claude-opus-5` left eleven example
    receipts in the SPQ ceremonies, the receipt protocol and the agent bodies
    still showing `claude-opus-4-8` or `claude-sonnet-4-6`.
  * Three of those eleven had ALSO gone stale on tier: they showed the
    `sonnet` pin for `code-reviewer` (moved to `opus` in #434),
    `compliance-engineer` and `quality-engineer` (moved in #483).

Each time the fix was the same edit in more places, so this file removes the
class rather than the instances. Two properties, both resolved through
`model-pins.json` rather than against a literal, so a legitimate re-pin cannot
break them and a stale restatement cannot survive one.

WHAT AN EXAMPLE RECEIPT SHOULD SAY
----------------------------------
`{model_id_used}` -- the placeholder fifteen agent receipt templates already
used before this file existed. A receipt's `model` is an OBSERVATION written
by the dispatch about itself ("the model you are running on"), not a pin: the
pin records what H3Tech intends a tier to resolve to, and #519 measured that
it does not reach a dispatch at all. So an example that shows a concrete id
teaches a dispatch to copy a fixed string instead of reporting what ran, which
is the forgery-shaped failure `model_pin_check`'s docstring says nothing can
detect. The placeholder is both drift-proof and more honest.

A literal is still ALLOWED, and checked rather than banned: an example may
name an exact id as long as `model_pin_check.check_receipt` accepts it for the
role the example carries. That is deliberate -- banning literals outright
would push the next author to a comment or a table the checker cannot see.

WHAT THIS FILE DOES NOT PROVE
-----------------------------
Nothing about a real dispatch. It reads authored markdown. Whether a receipt's
`model` is TRUE of the run that wrote it is unverifiable from inside the
plugin (`model_pin_check` docstring; #493), and this file does not change that.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
BACKENDS = PLUGIN_ROOT / "skills" / "_shared" / "backends"

#: Authored prompt bodies: everything a dispatched agent or the orchestrator
#: is instructed to read. `agents/` and `skills/` is the whole surface; the
#: pin-governance files under `backends/` are excluded below.
AUTHORED_ROOTS = (PLUGIN_ROOT / "skills", PLUGIN_ROOT / "agents")

#: Files that name a model id ON PURPOSE and must not be swept up.
EXCLUDED = {
    # The pin-governance narrative itself. It states the current pins in prose
    # (already guarded by test_model_tier_decorrelation) and quotes the
    # SUPERSEDED ones while explaining #519's measurement and #441's roll --
    # a history that stops being tellable if every retired id is scrubbed.
    "skills/_shared/backends/claude.md",
    # Uses `claude-sonnet-4-20250514` as an EXAMPLE OF A VOLATILE VALUE, in a
    # protocol whose subject is that such values go stale. The staleness is
    # the illustration; replacing it with a current id would weaken the point.
    "skills/_shared/protocols/freshness-protocol.md",
}

#: A concrete Claude model id, as opposed to a tier alias or a placeholder.
MODEL_ID_RE = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-\d[0-9A-Za-z.\-]*")

#: `{model_id_used}`, `{story_id}` -- a template slot the dispatch fills in.
PLACEHOLDER_RE = re.compile(r"^\{[a-z_]+\}$")

#: The one placeholder spelling for this field. Asserted rather than assumed:
#: two spellings would mean the checker below has to know both, and the next
#: author would have to guess which one is current.
MODEL_PLACEHOLDER = "{model_id_used}"

_JSON_FENCE = re.compile(r"```json\s*\n(.*?)^```", re.DOTALL | re.MULTILINE)


def _pin_checker():
    """`model_pin_check`, imported without dragging in the whole backends dir."""
    if str(BACKENDS) not in sys.path:
        sys.path.insert(0, str(BACKENDS))
    import model_pin_check  # noqa: PLC0415

    return model_pin_check


def _authored_bodies() -> list[Path]:
    paths = []
    for root in AUTHORED_ROOTS:
        for path in root.rglob("*.md"):
            if path.relative_to(PLUGIN_ROOT).as_posix() in EXCLUDED:
                continue
            paths.append(path)
    assert paths, f"no authored bodies found under {AUTHORED_ROOTS}"
    return sorted(paths)


def _example_receipts(path: Path) -> list[dict]:
    """Every ```json fence in `path` that parses to an object with a `model`."""
    receipts = []
    for match in _JSON_FENCE.finditer(path.read_text(encoding="utf-8")):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            # A fence carrying `...` or a `A | B` alternation is prose, not a
            # receipt. Any model id inside one is still caught by the raw-text
            # scan below, so skipping here loses no coverage.
            continue
        for candidate in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(candidate, dict) and "model" in candidate:
                receipts.append(candidate)
    return receipts


def _ids(path: Path) -> set[str]:
    return set(MODEL_ID_RE.findall(path.read_text(encoding="utf-8")))


# --- the two properties ------------------------------------------------------

def test_no_authored_body_names_a_model_the_pins_do_not() -> None:
    """The drift property, stated over the whole authored surface.

    Resolved through `model-pins.json`, so rolling a tier keeps this test
    honest instead of turning it red for the wrong reason -- but a roll that
    leaves a restatement behind DOES turn it red, which is the point.
    """
    mpc = _pin_checker()
    named = mpc.pinned_model_ids()

    stale = {
        path.relative_to(PLUGIN_ROOT).as_posix(): sorted(unpinned)
        for path in _authored_bodies()
        if (unpinned := _ids(path) - named)
    }
    assert not stale, (
        f"authored bodies name model ids that model-pins.json does not: "
        f"{stale}. An id restated in prose or in an example receipt goes stale "
        f"on the next re-pin and nothing tells the reader. Prefer "
        f"{MODEL_PLACEHOLDER!r} for a receipt's `model` -- it is an "
        f"observation about the run, not a pin. If a literal is genuinely "
        f"needed, it must be one a tier names."
    )


def test_every_example_receipt_model_is_a_placeholder_or_conformant() -> None:
    """An example an agent copies verbatim must not itself be a finding.

    `check_receipt` is the same function an operator runs over a real receipts
    directory, so an example that passes here is one whose copy passes there:
    the id is pinned AND it belongs to the tier the example's own `role` is
    routed to. Three of the eleven examples this file was written for failed
    the second half, not the first.
    """
    mpc = _pin_checker()

    problems = []
    for path in _authored_bodies():
        for receipt in _example_receipts(path):
            model = receipt.get("model")
            if isinstance(model, str) and PLACEHOLDER_RE.match(model):
                assert model == MODEL_PLACEHOLDER, (
                    f"{path.relative_to(PLUGIN_ROOT)}: example receipt uses "
                    f"placeholder {model!r} for `model`; the authored "
                    f"convention is {MODEL_PLACEHOLDER!r}"
                )
                continue
            for problem in mpc.check_receipt(receipt):
                problems.append(f"{path.relative_to(PLUGIN_ROOT)}: {problem}")

    assert not problems, "\n".join(problems)


def test_the_placeholder_convention_is_actually_used() -> None:
    """Guards the two tests above against passing for an empty reason.

    Both would be vacuously green if every example receipt disappeared. This
    asserts the surface is still populated, and that the SPQ ceremonies -- the
    bodies #441's re-pin actually went stale in -- are part of it.
    """
    with_examples = {
        path.relative_to(PLUGIN_ROOT).as_posix()
        for path in _authored_bodies()
        if _example_receipts(path)
    }
    assert len(with_examples) >= 15, (
        f"only {len(with_examples)} authored bodies carry an example receipt; "
        f"this test is close to vacuous"
    )
    for required in (
        "skills/_shared/protocols/receipt-protocol.md",
        "skills/synaptory/spq/commit.md",
        "skills/synaptory/spq/checkpoint.md",
        "skills/synaptory/spq/discovery.md",
        "skills/synaptory/spq/acceptance.md",
        "skills/synaptory/spq/sync.md",
        "skills/synaptory/ceremonies/inception.md",
    ):
        assert required in with_examples, (
            f"{required} no longer contributes an example receipt to this "
            f"check; if its receipt example moved, follow it"
        )
