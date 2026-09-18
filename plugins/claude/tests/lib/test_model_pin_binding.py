"""Layer 1 -- the HC0 model pin is a claim; this file is what verifies it (#519).

WHAT #519 SETTLED, BY MEASUREMENT
---------------------------------
#409's evidence run observed CR dispatches reporting `claude-opus-5` while
`model-pins.json` pinned `opus` to `claude-opus-4-8`. The ticket named two
candidate causes with opposite consequences: either a headless
`claude --plugin-dir` session resolves the CLI's own default model (a note about
how to run a measurement), or the pin does not bind on the dispatch path (a
compliance defect).

**The second one is real, and neither axis in the four-cell matrix mattered.**
Nine real dispatches on Claude Code 2.1.236, 2026-09-03: marketplace install and
`--plugin-dir` sideload, with and without an ambient CLI model default, all
recorded `claude-opus-5` for the `opus` tier. The discriminating cell pinned the
main session to `haiku` and still got `claude-opus-5` for the subagent, which
rules out inheritance of the session default -- the tier binds, the *pin* does
not.

The mechanism is that the host's `Agent()` tool accepts only the four tier
aliases on its `model` parameter and rejects an exact id with
`InputValidationError`. So `claude.md`'s standing instruction -- "Do not pass
alias strings to `Agent()` -- pass the resolved exact ID" -- was impossible to
follow, and no component anywhere performed the alias-to-id resolution.
`model-pins.json` is read by markdown prose, by the AI-BOM generator, and by
tests. By nothing on a dispatch path.

WHAT THIS FILE PROVES
---------------------
1. The refusal that was missing: a receipt naming a model the pin file does not
   name is now reported, with `claude-opus-5` as the live regression case.
2. `claude.md` no longer instructs the impossible, and states that the pin is a
   declaration of intent rather than an enforced binding.
3. The accepted-variant set is data-driven off each tier's declared
   `runtime_variants`, so it cannot silently accept an id the pin file warns
   against.

WHAT THIS FILE DOES NOT PROVE
-----------------------------
It does not refuse a **forged** model string, and it must not be read as if it
did. `model` is self-attested by the dispatched subagent from an authored prompt
line; the authoritative value lives in the host's transcript and `modelUsage`
accounting, which no part of this plugin reads. A receipt naming
`claude-opus-4-8` while the dispatch ran on `claude-opus-5` passes every check
here and in `receipt_validator.validate_receipt`. Presence is not authorization
(#493). Closing that needs the observed model lifted from a source the subagent
does not author, which is neither this ticket's lane nor available on this host.

It also does not make the pin bind. #441 has since taken the deliberate re-pin
half -- `opus` now names `claude-opus-5`, the measured resolution -- and that
changed what the file CLAIMS, not which weights run. The remaining half is a
dispatch-mechanism change (role subagents instead of `general-purpose`), which
neither #519 nor #441 took. The recorded-decision contract that came with the
re-pin lives in `test_model_pin_decision_record.py`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
BACKENDS = PLUGIN_ROOT / "skills" / "_shared" / "backends"
CLAUDE_WRAPPER = BACKENDS / "claude.md"
MODEL_PINS = BACKENDS / "model-pins.json"
CHECKER_PATH = BACKENDS / "model_pin_check.py"

#: The model #409 actually observed on the `opus` tier and #519 measured nine
#: times. Kept as a literal on purpose: it is the ground truth about the host,
#: not a placeholder.
#:
#: #441 re-pinned `opus` to it, which RETIRED the refusal these tests used to
#: assert and replaced it with the opposite obligation. The literal stays, and
#: its assertions inverted: the pin file must now NAME this model, because a
#: file that does not name the id every measured dispatch ran is a provenance
#: claim that contradicts the observation it exists to record.
OBSERVED_CR_MODEL = "claude-opus-5"

#: A model no tier names and none is expected to. The unpinned-drift case now
#: needs its own literal, because the one #409 observed became a pinned id.
UNPINNED_MODEL = "claude-nonesuch-9"


def _load_checker():
    spec = importlib.util.spec_from_file_location("synaptory_model_pin_check", CHECKER_PATH)
    assert spec and spec.loader, f"cannot load {CHECKER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


@pytest.fixture(scope="module")
def pins():
    return json.loads(MODEL_PINS.read_text(encoding="utf-8"))["tiers"]


# --- the refusal that was missing -------------------------------------------

def test_an_unpinned_model_is_refused(checker, pins) -> None:
    """The acceptance criterion: a dispatch on a model the pins do not name fails.

    #409 saw `claude-opus-5` on roughly 45% of its delivery tokens and nothing
    in the plugin, the hooks, or the control plane said a word about it. #441
    re-pinned that specific id, so the regression case here is now a model no
    tier names at all -- the SHAPE of the defect, which is what must keep
    failing after any legitimate re-pin.
    """
    problem = checker.check_model(UNPINNED_MODEL, pins)
    assert problem, (
        f"{UNPINNED_MODEL!r} is named by no tier in model-pins.json, but the "
        f"checker accepted it. That is the #519 defect restored: a provenance "
        f"claim nothing verifies."
    )
    assert UNPINNED_MODEL in problem


def test_the_model_409_observed_is_now_pinned(checker, pins) -> None:
    """#441's decision, asserted where the refusal it replaced used to live.

    This file previously asserted that `claude-opus-5` is REFUSED. That was
    correct and it was also the problem: the checker fired on the honest
    receipt of every role routed to `opus` (PO, SA, CE, RA, and after #434/#483
    both provers) while staying silent on a forged one, which is a detector
    pointed the wrong way round. #441 rolled the pin to the measured id.

    What this asserts is impossible: the pin file naming, for the `opus` tier,
    an id other than the one every measured dispatch on that tier ran. If the
    alias moves again this fails, which is the intended alarm -- the fix is a
    fresh measurement plus a new `decision` block, not an edit here.
    """
    assert checker.check_model(OBSERVED_CR_MODEL, pins) is None, (
        f"{OBSERVED_CR_MODEL!r} is the id #519 measured for the `opus` tier in "
        f"nine dispatches, but model-pins.json does not name it. An honest "
        f"receipt from any role routed to `opus` would be refused (#441)."
    )
    assert pins["opus"]["model_id"] == OBSERVED_CR_MODEL


def test_every_pinned_id_and_declared_variant_is_accepted(checker, pins) -> None:
    """A pin the file names must never be reported as drift."""
    for alias, tier in pins.items():
        for model in [tier["model_id"], *(tier.get("runtime_variants") or [])]:
            assert checker.check_model(model, pins) is None, (
                f"tier {alias!r} names {model!r} in model-pins.json but the "
                f"checker refused it"
            )


def test_a_bare_tier_alias_is_refused_as_a_model_id(checker, pins) -> None:
    """`opus` is not provenance. Recording the alias records nothing reproducible."""
    for alias in sorted(checker.AGENT_TOOL_MODEL_VALUES):
        problem = checker.check_model(alias, pins)
        assert problem and "ALIAS" in problem, (
            f"alias {alias!r} was accepted as a recorded model id; an alias means "
            f"'whichever weights that tier pointed at on the day', which is the "
            f"gap HC0-F2 exists to close"
        )


def test_an_absent_model_is_refused_distinctly_from_an_unpinned_one(checker, pins) -> None:
    """A zero that means 'not measured' must not read as 'measured, and fine' (#518, #444)."""
    absent = checker.check_model("", pins)
    unpinned = checker.check_model(UNPINNED_MODEL, pins)
    assert absent and unpinned
    assert absent != unpinned, (
        "'no model recorded' and 'a model the pins do not name' collapsed into "
        "one message; they are different failures with different fixes"
    )


def test_an_undeclared_runtime_variant_is_refused(checker, pins) -> None:
    """The pin file's own note is the authority on variants, not suffix guessing.

    `model-pins.json` says Anthropic publishes no 1M Haiku 4.5 and that
    `claude-haiku-4-5-1m` must be treated as unsupported. A checker that
    stripped `-1m` generically would accept it and contradict the file.
    """
    assert pins["haiku"]["runtime_variants"] == [], (
        "haiku now declares runtime variants; re-read its `notes` before "
        "loosening this test"
    )
    assert checker.check_model("claude-haiku-4-5-1m", pins), (
        "claude-haiku-4-5-1m was accepted although model-pins.json declares no "
        "haiku runtime variants and its notes call that id unsupported"
    )


# --- receipt-level checks ----------------------------------------------------

def _receipt(**overrides) -> dict:
    receipt = {
        "story_id": "US-001",
        "role": "code-reviewer",
        "backend": "claude",
        # Resolved from the file rather than written as a literal, so a future
        # re-pin of `opus` cannot leave this fixture asserting against an id no
        # tier names any more. That is the failure #441 had to clean up.
        "model": json.loads(MODEL_PINS.read_text(encoding="utf-8"))["tiers"]["opus"]["model_id"],
    }
    receipt.update(overrides)
    return receipt


def test_an_honest_receipt_from_a_routed_role_is_not_refused(checker, pins) -> None:
    """#441's acceptance criterion at receipt level, for every role on `opus`.

    What this asserts is impossible: the checker refusing the receipt a role
    routed to `opus` would write if it told the truth about the model it ran
    on. That was the state on `dev` before #441 -- and a checker that refuses
    the honest answer teaches the roles to write a different one.
    """
    for role in ("project-owner", "solution-architect", "compliance-engineer",
                 "research-advisor", "code-reviewer", "quality-engineer"):
        problems = checker.check_receipt(
            _receipt(role=role, model=OBSERVED_CR_MODEL), pins
        )
        assert problems == [], (
            f"{role}: an honest receipt recording {OBSERVED_CR_MODEL!r}, the id "
            f"#519 measured for the `opus` tier, was refused: {problems}"
        )


def test_a_receipt_on_its_own_routed_tier_passes(checker, pins) -> None:
    assert checker.check_receipt(_receipt(), pins) == []


def test_a_prover_receipt_on_the_producer_tier_is_refused(checker, pins) -> None:
    """#434's decorrelation, checked against a receipt rather than a table.

    The unit test #434 shipped compares the authored table to the pin file. This
    one catches the case that table cannot see: a CR receipt that pinned-but-wrong
    tier, i.e. a real dispatch that landed back on the producer's weights.
    """
    producer_model = checker.load_pins()["sonnet"]["model_id"]
    problems = checker.check_receipt(_receipt(model=producer_model), pins)
    assert problems, (
        f"a code-reviewer receipt recording {producer_model!r} -- the "
        f"software-engineer tier -- passed; the #434 decorrelation would be gone "
        f"with nothing reporting it"
    )


def test_a_non_claude_backend_receipt_is_out_of_scope_not_failing(checker, pins) -> None:
    """These pins are for plugin-claude. A Codex receipt is not non-conformant."""
    assert checker.check_receipt(_receipt(backend="codex", model="gpt-5-codex"), pins) == []


# --- the authored contract must not re-state the impossible ------------------

#: The instruction #519 proved unfollowable. It may still be QUOTED, because
#: naming a retracted instruction is how a reader learns it was retracted -- but
#: only inside a paragraph that retracts it.
_RETRACTED_INSTRUCTION = "Do not pass alias strings to `Agent()`"
_RETRACTION_MARKER = "impossible to follow"


def test_claude_md_does_not_instruct_passing_an_exact_id_to_agent() -> None:
    """The instruction #519 found unfollowable must not come back as guidance.

    `Agent()`'s `model` parameter accepts only `sonnet|opus|haiku|fable`;
    measured on Claude Code 2.1.236, an exact id is rejected with
    `InputValidationError`. Telling the orchestrator to pass a resolved exact id
    made the pin look enforced while nothing enforced it.

    A plain substring ban cannot tell a directive from a retraction, and banning
    the words outright would forbid documenting the correction -- which is how a
    wrong answer becomes durable, inherited as a premise rather than read as a
    claim (#504). So this checks per paragraph: the phrase may appear only where
    it is being withdrawn.
    """
    text = CLAUDE_WRAPPER.read_text(encoding="utf-8")
    offenders = [
        para
        for para in text.split("\n\n")
        if _RETRACTED_INSTRUCTION in para and _RETRACTION_MARKER not in para
    ]
    assert not offenders, (
        "claude.md tells the orchestrator to pass a resolved exact model ID to "
        "Agent(), without withdrawing it. That call fails with "
        "InputValidationError -- the parameter accepts only the four tier "
        f"aliases. See #519. Offending paragraph: {offenders[0][:200]!r}"
    )


def test_the_retraction_test_would_catch_a_live_instruction() -> None:
    """Guard the guard: the paragraph scoping must not make the check vacuous."""
    live = f"Some heading.\n\n{_RETRACTED_INSTRUCTION}, then pass the resolved exact ID.\n"
    offenders = [
        para
        for para in live.split("\n\n")
        if _RETRACTED_INSTRUCTION in para and _RETRACTION_MARKER not in para
    ]
    assert offenders, (
        "the paragraph-scoped check no longer flags an unqualified instruction, "
        "so it would pass on the very text #519 removed"
    )


def test_claude_md_states_that_the_pin_is_not_enforced_by_dispatch() -> None:
    """A reader must not be able to take the pin for a runtime guarantee."""
    text = CLAUDE_WRAPPER.read_text(encoding="utf-8")
    assert "declaration of intent" in text, (
        "claude.md does not say that model-pins.json is a declaration of intent "
        "rather than an enforced binding, so a reader can still take the AI-BOM "
        "entry for evidence of the weights that ran (#519)"
    )
    assert "self-attested" in text, (
        "claude.md does not say the receipt's `model` is self-attested by the "
        "subagent, which is the reason the pin check cannot refuse a forgery"
    )


def test_the_agent_tool_alias_set_is_recorded_where_a_reader_will_find_it() -> None:
    """The measured host contract belongs in the wrapper, not only in a test."""
    text = CLAUDE_WRAPPER.read_text(encoding="utf-8")
    assert "InputValidationError" in text
    for alias in ("sonnet", "opus", "haiku", "fable"):
        assert alias in text


def test_typed_vendor_id_is_compared_literally(checker, pins):
    receipt = _receipt()
    receipt["model"] = {"kind": "vendor_id", "value": receipt["model"]}
    assert checker.check_receipt(receipt, pins) == []


@pytest.mark.parametrize("kind", ["display_name", "unreported"])
def test_non_vendor_identity_is_not_pin_drift_or_a_match(checker, pins, kind):
    receipt = _receipt()
    receipt["model"] = {"kind": kind, "value": receipt["model"]}
    problems = checker.check_receipt(receipt, pins)
    assert len(problems) == 1 and problems[0].startswith("NOT COMPARABLE:")
