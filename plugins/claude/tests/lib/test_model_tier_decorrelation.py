"""Layer 1 -- producer and prover must not resolve to the same model id.

Epic #410 measured the baseline arm and found that SE, QE and CR all resolved
to the `sonnet` tier, so every verification the delivery path performed was
same-model verification. #434 moved the judgment-heaviest prover (CR, which
reads diffs and decides whether logic is right) off the producer's tier, and
#483 moved the other one (QE, whose per-case outcome for the acceptance set
sealed at COMMIT became the primary `tests_pass` evidence in #406). Both
halves of the pipeline's verification now resolve to different weights than
the half that produced the candidate.

WHAT THIS FILE PROVES
---------------------
That the authored dispatch contract is self-consistent and yields DISTINCT
exact model ids for the SE and CR dispatches of one work unit. It resolves
them the way a dispatch resolves them: parse the role-to-tier table in
`skills/_shared/backends/claude.md` Step 2, then map the tier alias to an
exact id through `backends/model-pins.json`. Neither id is written down here,
so re-pinning a tier cannot make this test pass by accident, and an edit that
puts CR back on the SE tier fails it.

WHAT THIS FILE DOES NOT PROVE
-----------------------------
1. It is not receipt evidence. #434's acceptance criterion is that a real CR
   dispatch RECORDED a different `model` field than the SE dispatch of the
   same work unit. Production resolution is prompt-driven -- the orchestrator
   reads the markdown table -- so no unit test can observe it. The end-to-end
   receipt pair lands with #409's consolidated-arm run.
2. It is not runtime independence. Both ids are Claude weights from one
   vendor: partial decorrelation, not an independent runtime. Role-level
   runtime choice has no production caller on the dispatch path (#396
   finding 1).
3. It says nothing about review QUALITY. Distinct weights is a necessary
   condition for independent judgment, not a sufficient one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
BACKENDS = PLUGIN_ROOT / "skills" / "_shared" / "backends"
CLAUDE_WRAPPER = BACKENDS / "claude.md"
MODEL_PINS = BACKENDS / "model-pins.json"

#: The dispatch these tickets are about: the producing role and the proving
#: roles of one work unit. Table keys are underscored (`code_reviewer`); agent
#: directories are hyphenated (`code-reviewer`).
PRODUCER_ROLE = "software_engineer"
PROVER_ROLE = "code_reviewer"

#: Every role that proves a work unit it did not produce. `code_reviewer`
#: moved off the producer's tier in #434; `quality_engineer` in #483, once
#: #406 had made its per-case verdict the primary `tests_pass` evidence
#: rather than a suite exit code. BOTH are checked, so returning either one
#: to the producer's tier fails.
PROVER_ROLES = ("code_reviewer", "quality_engineer")

#: An exact pinned id, as opposed to a tier alias. Rejects bare `opus` /
#: `sonnet` / `haiku`, which is the HC0-F2 failure mode: passing an alias to
#: `Agent()` lets Anthropic's own aliasing decide which weights ran, so a
#: regulated customer cannot reproduce a dispatch audit-to-audit.
EXACT_MODEL_ID = re.compile(r"^claude-[a-z]+-\d[0-9A-Za-z.\-]*$")

#: The Step 2 table rows, e.g. `| code_reviewer | opus | opus |`.
_TIER_ROW = re.compile(
    r"^\|\s*([a-z_]+)\s*\|\s*([a-z]+)\s*\|\s*([a-z]+)\s*\|\s*$", re.MULTILINE
)

#: `model: <tier>` in a YAML frontmatter block.
_FRONTMATTER_MODEL = re.compile(r"^model:\s*(\S+)\s*$", re.MULTILINE)


# --- resolution path (mirrors what a dispatch does) --------------------------

def _role_tiers() -> dict[str, tuple[str, str]]:
    """Parse the authored role-to-tier table into {role: (autonomous, controlled)}."""
    text = CLAUDE_WRAPPER.read_text(encoding="utf-8")
    rows = {
        role: (autonomous, controlled)
        for role, autonomous, controlled in _TIER_ROW.findall(text)
        # The table's own header separator and any unrelated 3-column table
        # cannot match: a role name is snake_case and both tiers are bare
        # words. Guard anyway by requiring the role to end in a known suffix
        # of the roster's naming convention.
        if "_" in role
    }
    assert rows, f"no role-to-tier rows parsed from {CLAUDE_WRAPPER}"
    return rows


def _pins() -> dict[str, dict]:
    return json.loads(MODEL_PINS.read_text(encoding="utf-8"))["tiers"]


def _resolve(role: str, mode: str = "autonomous") -> str:
    """Resolve a role to its EXACT model id, the way a dispatch resolves it."""
    tiers = _role_tiers()
    assert role in tiers, f"{role} has no row in the claude.md tier table"
    alias = tiers[role][0 if mode == "autonomous" else 1]
    pins = _pins()
    assert alias in pins, f"tier alias {alias!r} for {role} is not pinned in {MODEL_PINS}"
    return pins[alias]["model_id"]


# --- the acceptance property -------------------------------------------------

@pytest.mark.parametrize("prover", PROVER_ROLES)
@pytest.mark.parametrize("mode", ["autonomous", "controlled"])
def test_prover_and_producer_resolve_to_different_model_ids(
    prover: str, mode: str
) -> None:
    """#434 / #483: a work unit's producing and proving dispatches differ in
    weights, for BOTH provers.

    Resolved through the authored table plus the pin file rather than compared
    against literals, so this fails on an edit that returns either prover to
    the SE tier AND survives a legitimate re-pin of any tier.
    """
    producer = _resolve(PRODUCER_ROLE, mode)
    resolved = _resolve(prover, mode)

    assert producer and resolved
    assert producer != resolved, (
        f"{prover} resolves to {resolved!r}, the same weights as "
        f"{PRODUCER_ROLE} in {mode} mode. Same-model verification: the prover "
        f"inherits the producer's blind spots on every judgment-heavy check. "
        f"See #434 (CR) and #483 (QE)."
    )


def test_the_two_provers_share_a_tier_and_that_is_deliberate() -> None:
    """A recorded decision, not a requirement, so a later reader is not misled.

    #483 put QE on the SAME tier as CR rather than on a third one. `haiku` is
    the only other pinned tier and it is the weaker one, which is precisely
    why #434 rejected it for a prover. The consequence is worth stating: the
    two provers are decorrelated from the PRODUCER and not from each other.
    Neither one checks the other's output, so that is a much weaker gap than
    a prover sharing the producer's weights -- but an epic whose thesis is
    independent derivation should not discover it by accident later.
    """
    tiers = _role_tiers()
    assert tiers["code_reviewer"][0] == tiers["quality_engineer"][0], (
        "the two provers no longer share a tier. That is not wrong, but this "
        "assertion records the #483 decision; update it deliberately."
    )


def test_both_dispatch_tiers_are_exactly_pinned_and_baa_covered() -> None:
    """HC0-F2 plus the `healthcare.baa_enforced` refusal.

    A tier the delivery path routes to must be an exact id (reproducible
    audit-to-audit) and BAA-covered, or a regulated project cannot run the
    role at all.
    """
    pins = _pins()
    tiers = _role_tiers()

    for role in (PRODUCER_ROLE,) + PROVER_ROLES:
        for alias in tiers[role]:
            pin = pins[alias]
            model_id = pin["model_id"]
            assert EXACT_MODEL_ID.match(model_id), (
                f"tier {alias!r} (used by {role}) pins {model_id!r}, which does "
                f"not look like an exact versioned model id"
            )
            assert pin["baa_covered"] is True, (
                f"tier {alias!r} is routed to by {role} but is not "
                f"baa_covered; a healthcare.baa_enforced project would refuse it"
            )


def test_every_routed_tier_is_pinned_and_covered() -> None:
    """No role in the roster may route to an unpinned or uncovered tier."""
    pins = _pins()
    for role, (autonomous, controlled) in _role_tiers().items():
        for alias in {autonomous, controlled}:
            assert alias in pins, f"{role} routes to unpinned tier {alias!r}"
            assert pins[alias]["baa_covered"] is True, (
                f"{role} routes to tier {alias!r}, which is not baa_covered"
            )


# --- drift guards: one table, three places that repeat it --------------------

@pytest.mark.parametrize(
    "role_dir,table_key",
    [
        ("code-reviewer", "code_reviewer"),
        ("software-engineer", "software_engineer"),
        ("quality-engineer", "quality_engineer"),
        ("platform-engineer", "platform_engineer"),
        ("technical-writer", "technical_writer"),
        ("project-owner", "project_owner"),
        ("solution-architect", "solution_architect"),
        ("compliance-engineer", "compliance_engineer"),
        ("research-advisor", "research_advisor"),
    ],
)
@pytest.mark.parametrize("filename", ["agent.md", "SKILL.md"])
def test_agent_frontmatter_matches_the_tier_table(
    role_dir: str, table_key: str, filename: str
) -> None:
    """A table that disagrees with the frontmatter is worse than either alone.

    Claude Code honours the subagent's own `model:` frontmatter, so a stale
    value there silently overrides the orchestrator's routing decision.
    """
    path = PLUGIN_ROOT / "agents" / role_dir / filename
    match = _FRONTMATTER_MODEL.search(path.read_text(encoding="utf-8"))
    assert match, f"{path} has no `model:` frontmatter field"

    expected = _role_tiers()[table_key][0]
    assert match.group(1) == expected, (
        f"{path} declares model {match.group(1)!r} but the claude.md tier "
        f"table routes {table_key} to {expected!r}"
    )


def test_prose_pin_list_matches_the_pin_file() -> None:
    """claude.md restates the pins in prose; the restatement must be true.

    It was stale before #434 (it still named `claude-opus-4-7` /
    `claude-sonnet-4-6` after the pins had rolled to 4-8 / sonnet-5), which is
    how a reader ends up believing the wrong weights ran.
    """
    text = CLAUDE_WRAPPER.read_text(encoding="utf-8")
    for alias, pin in _pins().items():
        expected = f"`{alias} → {pin['model_id']}`"
        assert expected in text, (
            f"claude.md does not state {expected}; its prose pin list has "
            f"drifted from model-pins.json"
        )


# --- what the tier move does and does not establish (#483, #519, #525) ------
#
# The declared gap that used to live here is GONE rather than rewritten.
# `test_qe_shares_the_producer_tier_declared_gap` was a strict xfail whose
# reason read "QE's payload and prove path are #406's subject"; #406 closed
# without touching either, so the declaration was pointing at a ticket that
# did not own it -- the exact rot a strict xfail exists to prevent, and the
# reason #483 was filed. #483 closed the gap instead of re-owning it, and the
# assertion it made is now made unconditionally, in both engagement modes, by
# `test_prover_and_producer_resolve_to_different_model_ids[quality_engineer]`.


def _pin_checker():
    """`model_pin_check`, imported without dragging in the whole backends dir."""
    import sys

    if str(BACKENDS) not in sys.path:
        sys.path.insert(0, str(BACKENDS))
    import model_pin_check  # noqa: PLC0415

    return model_pin_check


def test_the_tier_binds_and_the_pin_does_not() -> None:
    """#519's measurement, asserted where a reader of this file will meet it.

    The `Agent()` tool's `model` parameter accepts ONLY the four tier aliases;
    an exact id is refused with `InputValidationError`. So moving a role
    between tiers changes which weights actually run, and re-pinning a tier
    does not. That asymmetry is why #483's tier half is worth doing at all,
    and why #441's re-pin is a separate and weaker lever rather than a
    prerequisite.
    """
    mpc = _pin_checker()
    assert mpc.AGENT_TOOL_MODEL_VALUES == frozenset(
        {"sonnet", "opus", "haiku", "fable"}
    )
    for alias in _pins():
        assert alias in mpc.AGENT_TOOL_MODEL_VALUES, (
            f"tier alias {alias!r} is pinned in model-pins.json but is not a "
            f"value Agent(model=...) accepts, so no dispatch can reach it"
        )
        # The pinned id itself is NOT an accepted value. This is the half that
        # makes the pin advisory.
        assert _pins()[alias]["model_id"] not in mpc.AGENT_TOOL_MODEL_VALUES


@pytest.mark.parametrize("role", ["code-reviewer", "quality-engineer"])
def test_the_pin_drift_the_qe_move_widened_is_closed(role: str) -> None:
    """The interaction with #525's checker, after #441 resolved it.

    `model_pin_check.check_receipt` refuses a receipt whose model no tier
    names. #519 measured that `opus` resolves to `claude-opus-5`; while the
    file pinned `claude-opus-4-8`, an HONEST receipt from any role routed to
    `opus` was refused. #434 made that true for CR and #483 for QE, taking the
    count from one role to two -- and a detector that fires on the honest
    answer and stays silent on the forged one is pointed the wrong way round.

    #441 rolled the pin to the measured id. Asserted in both directions so
    neither half can be misread:

      1. a receipt naming the pinned opus id -- which is now also the OBSERVED
         alias resolution -- passes;
      2. a receipt naming an id no tier names is still refused, so closing the
         known case did not disarm the check.
    """
    mpc = _pin_checker()
    pinned_opus = _pins()["opus"]["model_id"]

    assert pinned_opus == "claude-opus-5", (
        "the opus pin no longer names the id #519 measured; re-measure the "
        "alias and record a new decision block rather than editing this"
    )

    ok = {"role": role, "backend": "claude", "model": pinned_opus}
    assert mpc.check_receipt(ok) == [], (
        f"{role}: an honest receipt naming the observed opus resolution must "
        f"pass; if it does not, #441's re-pin has been reverted"
    )

    drift = {"role": role, "backend": "claude", "model": "claude-nonesuch-9"}
    problems = mpc.check_receipt(drift)
    assert problems, (
        f"{role}: a model named by no tier must still be refused; closing the "
        f"#409 case must not have widened the accepted set to everything"
    )
    assert "named by no tier" in problems[0]
    assert "#441" in problems[0], (
        "the refusal must name the ticket that owns the re-pin decision, or "
        "the next reader will widen the pin file to silence it"
    )


def test_a_qe_receipt_on_the_producer_tier_is_now_refused() -> None:
    """The decorrelation regression detector, extended to QE for free.

    #525 built the mis-tiering half of `check_receipt` to catch a CR that
    silently lands back on the producer's tier -- a regression the
    table-level tests above cannot see, because they read the table rather
    than a receipt. Moving QE to `opus` puts QE under the same protection,
    and that is worth an assertion: a QE receipt recording the `sonnet` pin
    was CONFORMANT before this ticket and is a finding after it.
    """
    mpc = _pin_checker()
    on_producer_tier = {
        "role": "quality-engineer",
        "backend": "claude",
        "model": _pins()["sonnet"]["model_id"],
    }
    problems = mpc.check_receipt(on_producer_tier)
    assert problems, (
        "a QE receipt recording the producer's tier is no longer conformant; "
        "check_receipt must say so"
    )
    assert "quality-engineer" in problems[0]
    assert "'sonnet' tier" in problems[0]


def test_the_move_does_not_establish_which_weights_ran() -> None:
    """The forgeability boundary, kept next to the claim it bounds (#493).

    #525 established that nothing refuses a receipt naming a pinned model it
    did not run on: `model` is self-attested by the dispatched subagent and
    the authoritative value lives in the host's transcript, which no part of
    this plugin reads. #483 changes the tier a dispatch is ROUTED to. It adds
    no channel to the dispatch's real identity, so a forged `model` string
    still passes every check.

    This asserts the gap explicitly, because an epic-mandated forgeability
    review that lives only in a PR body is a claim nothing holds afterwards.
    """
    mpc = _pin_checker()
    forged = {
        "role": "quality-engineer",
        "backend": "claude",
        # A pinned id, for a dispatch that in reality ran on claude-opus-5.
        "model": _pins()["opus"]["model_id"],
    }
    assert mpc.check_receipt(forged) == [], (
        "if this now returns problems, something began verifying the recorded "
        "model against the observed one -- update the forgeability statement "
        "in backends/claude.md, which says nothing does"
    )
