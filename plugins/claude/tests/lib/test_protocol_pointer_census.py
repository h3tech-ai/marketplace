"""Layer 1 -- the class-closing guard for "a protocol nothing points at" (#538).

The defect, in one sentence: a converted agent stops reading protocol bodies
inline, the injected compact digest carries only some of them under a heading,
and for the rest the deletion removed the last pointer in the dispatch -- body
on disk, materialized, served by the CLI, invisible to the agent that needed
it, with every other test green.

Three tickets have found the same omission:

    #480   five roles it converted; fixed by naming each orphan in that role's
           catalog, guarded both ways.
    #537   `coverage-ratchet` missed, because QE was the only role reading it
           and #480's set was therefore never a superset. Fixed QE; found
           #404's own two roles still uncovered and declared them.
    #538   this file: software-engineer's four and code-reviewer's three.

Each fix was written for the roles its own ticket touched, which is exactly why
there was a next one. **A fourth per-role guard is the wrong answer.** #528's
argument transfers without modification: an accessor cannot close a class
because nothing forces a new file to call it, and a per-role guard cannot close
this one because nothing forces the next role to be covered. So both axes are
walked from the tree and the registry has to classify what the walk finds:

    role axis       Every agent whose SKILL.md carries the thin contract
                    (`converted_roles()`) must have a recorded pre-conversion
                    read list. An unclassified role fails, with the class
                    explained. Adding a tenth role and forgetting its protocol
                    pointers is now a red suite rather than silence.

    protocol axis   Every body under `skills/_shared/protocols/` that the
                    digest does not carry (`digest_absent_protocols()`) must be
                    named by some converted role's catalog, or declared as
                    orchestrator-owned with the file that does point at it.
                    This is the half that survives a role being deleted, a
                    table entry and its catalog row being removed together, or
                    a protocol being added that the digest was never taught.

Both sets are DERIVED. #538's instruction, and the correction #537 earned the
hard way: enumerate from the digest, not from an earlier ticket's list of
names, because that list was never a superset.

The predicates live in `protocol_pointers.py` and return problems rather than
asserting, so the same code runs against the real tree (the guard) and against
a synthetic one (`test_the_census_fires_*`, which watch the guard fail). A
guard nobody has seen fail is a guard nobody has tested.

What this file does NOT establish is written down in `protocol_pointers.py`'s
docstring: it cannot see a role's needs GROWING past what it used to read, it
cannot tell a route from a load (#409 measured zero actual retrieval across 12
dispatches), and a name reachable only from inside a fetchable playbook does
not count, because the catalog is the mandatory surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lib.protocol_pointers import (
    CONVERTED_MARKERS,
    NOT_CONVERTED,
    PLUGIN_ROOT,
    POINTED_AT_OUTSIDE_THE_AGENT_CATALOGS,
    PROTOCOLS_READ_BEFORE_CONVERSION,
    READ_LIST_SOURCE,
    agent_roles,
    converted_roles,
    covered_by_digest,
    digest_absent_protocols,
    digest_absent_reads,
    named_in_catalog,
    protocol_bodies,
    skill_text,
    unclassified_converted_roles,
    unnamed_orphans_for,
    unpointed_digest_absent_protocols,
)

pytestmark = pytest.mark.unit

CONVERTED = converted_roles()
DIGEST_ABSENT = digest_absent_protocols()

_CLASS = (
    "A protocol the injected digest does not carry, that no role's catalog "
    "names, is unreachable from every dispatch: the body is on disk, "
    "materialized and served by `synaptory skills get`, and nothing in the "
    "prompt points at it. #480, #537 and #538 are three instances of that one "
    "omission. Name it in the role's `protocols/<name>` catalog row, or - if "
    "it belongs to the orchestrator rather than to an agent - declare it in "
    "POINTED_AT_OUTSIDE_THE_AGENT_CATALOGS with the file that points at it."
)


# ── The census's own completeness ─────────────────────────────────────────────
#
# A census is only as good as its denominator, and the failure mode is silent
# in exactly the same way as the defect it guards: a detector that stops
# matching parametrizes over fewer roles, every remaining item passes, and the
# suite reports green over the gap. A sibling ticket in this epic hit precisely
# that this week -- a census that claimed to cover "every check" was
# parametrized over five of eight, and one of the three it skipped held a live
# instance of the defect it existed to prevent. So the enumeration this file is
# built from is asserted rather than trusted.


def test_every_agent_directory_is_either_converted_or_declared():
    """The denominator. Nothing may fall between the two classifications."""
    roles = set(agent_roles())
    assert roles, "no agent directories found at all; the walk is broken"
    accounted = set(CONVERTED) | set(NOT_CONVERTED)
    unaccounted = sorted(roles - accounted)
    assert not unaccounted, (
        "these agents have a SKILL.md that the thin-contract detector does not "
        "recognise and that is declared in neither direction: %s. Either the "
        "detector's markers %r have drifted -- in which case the census has "
        "been silently parametrizing over fewer roles than exist -- or the "
        "role genuinely still reads its protocols inline, in which case say so "
        "in NOT_CONVERTED." % (unaccounted, CONVERTED_MARKERS)
    )
    ghosts = sorted(set(NOT_CONVERTED) - roles)
    assert not ghosts, "NOT_CONVERTED names agents that do not exist: %s" % ghosts


def test_a_role_declared_unconverted_really_still_reads_its_protocols():
    """The declaration's premise, so it cannot rot into a false exemption.

    An unconverted role is outside the orphan class only because its SKILL
    still reads the bodies. If it stops, the exemption is wrong and the role
    needs catalog rows, not a declaration.
    """
    wrong = []
    for role, reason in NOT_CONVERTED.items():
        assert reason.strip(), "%s is declared unconverted with no reason" % role
        if ".synaptory/.protocols/" not in skill_text(role):
            wrong.append(role)
    assert not wrong, (
        "these roles are declared unconverted but read no protocol bodies "
        "inline, so nothing points at their digest-absent protocols: %s" % wrong
    )


def test_the_census_is_parametrized_over_every_converted_role():
    """A guard against a vacuous suite: zero parameters cannot fail.

    `CONVERTED` and `DIGEST_ABSENT` are computed once at import and drive the
    parametrized tests below. If either resolved empty, every one of those
    tests would collect zero items and the file would pass having checked
    nothing.
    """
    assert CONVERTED, "the converted-role detector matched nothing"
    assert DIGEST_ABSENT, (
        "no digest-absent protocols found; either the digest now carries every "
        "protocol (delete this class of guard) or the walk is broken"
    )
    assert set(CONVERTED) == set(agent_roles()) - set(NOT_CONVERTED)


# ── The role axis ─────────────────────────────────────────────────────────────


def test_every_converted_role_is_classified():
    """Adding a converted role without covering its protocols must fail.

    This is the assertion #480's and #537's guards could not make. Both named
    the roles their own ticket converted; a role converted by the NEXT ticket
    was outside them by construction, and stayed outside for two waves.
    """
    unclassified = unclassified_converted_roles()
    assert not unclassified, (
        "these agents carry the thin contract and have no recorded "
        "pre-conversion protocol read list, so nothing checks whether their "
        "protocol pointers survived the conversion: %s\n\n%s\n\nRecord what "
        "the role read in PROTOCOLS_READ_BEFORE_CONVERSION, with the commit in "
        "READ_LIST_SOURCE." % (unclassified, _CLASS)
    )


def test_the_registry_names_only_roles_that_still_exist():
    """A stale entry is how a wrong answer becomes durable (#504)."""
    ghosts = [role for role in PROTOCOLS_READ_BEFORE_CONVERSION if role not in CONVERTED]
    assert not ghosts, (
        "these registry entries name no converted agent any more - delete "
        "them, or the census is guarding a role that is gone: %s" % ghosts
    )
    unsourced = sorted(set(PROTOCOLS_READ_BEFORE_CONVERSION) - set(READ_LIST_SOURCE))
    assert not unsourced, (
        "these read lists cite no commit, so nothing lets a reader re-derive "
        "them: %s" % unsourced
    )


def test_recorded_reads_name_protocol_bodies_that_still_exist():
    """The registry's own premise: every name it records is a real body.

    A renamed or deleted protocol would otherwise leave the census demanding a
    catalog row for a body that no longer exists, or - worse - quietly stop
    demanding one for the body it became.
    """
    known = set(protocol_bodies())
    dangling = {
        role: sorted(set(names) - known)
        for role, names in PROTOCOLS_READ_BEFORE_CONVERSION.items()
        if set(names) - known
    }
    assert not dangling, (
        "recorded protocol reads with no body under "
        "skills/_shared/protocols/: %s" % dangling
    )


@pytest.mark.parametrize("role", CONVERTED)
def test_a_converted_role_no_longer_reads_protocol_bodies_inline(role: str):
    """The premise the whole class rests on, checked for EVERY converted role.

    The catalog suite checks this only for the roles listed in its
    `MOVED_TO_PLAYBOOK` table, which does not include software-engineer or
    code-reviewer - so the two roles this ticket fixes were outside that check
    as well.
    """
    text = skill_text(role)
    catalog_free = text.split("## Skill Catalog")[0]
    assert ".synaptory/.protocols/" not in catalog_free, (
        "%s reads full protocol bodies into its mandatory payload again; the "
        "SubagentStart hook already injects the compact digest" % role
    )


@pytest.mark.parametrize("role", CONVERTED)
def test_every_digest_absent_protocol_the_role_read_is_named_in_its_catalog(role: str):
    """The fix, stated once for every converted role instead of per ticket.

    Derived in both directions: the read list is the role's, the absence is the
    digest's. A protocol that enters the digest drops out of this set on its
    own, and one that leaves the digest enters it, with no table to update.
    """
    unnamed = unnamed_orphans_for(role)
    assert not unnamed, (
        "%s used to read %s and the compact digest carries none of them, so "
        "they are unreachable from this dispatch.\n\n%s"
        % (role, unnamed, _CLASS)
    )


# ── The protocol axis ─────────────────────────────────────────────────────────


def test_every_digest_absent_protocol_has_a_pointer_somewhere():
    """The half that survives edits which keep every per-role table consistent.

    Deleting a role, or removing a table entry together with its catalog row,
    leaves every per-role assertion green while removing the last pointer in
    the tree. So does adding a protocol body the digest was never taught about.
    """
    unpointed = unpointed_digest_absent_protocols()
    assert not unpointed, (
        "these protocol bodies are absent from the compact digest and named in "
        "no converted role's catalog: %s\n\n%s" % (unpointed, _CLASS)
    )


@pytest.mark.parametrize("name", sorted(POINTED_AT_OUTSIDE_THE_AGENT_CATALOGS))
def test_a_declared_orchestrator_protocol_still_has_the_pointer_it_claims(name: str):
    """A declaration that cannot rot: the named pointer must still be there.

    The `declared_gaps` pattern. Without this the declaration outlives the
    pointer and the census reports coverage that no longer exists.
    """
    reason, pointers = POINTED_AT_OUTSIDE_THE_AGENT_CATALOGS[name]
    assert reason.strip(), "%s is declared with no reason" % name
    missing = []
    for relative in pointers:
        path = PLUGIN_ROOT / relative
        if not path.is_file():
            missing.append("%s (file is gone)" % relative)
        elif name not in path.read_text(encoding="utf-8"):
            missing.append("%s (no longer names it)" % relative)
    assert not missing, (
        "%r is declared as pointed at outside the agent catalogs, and these "
        "pointers are gone: %s. Either restore one, or name the protocol in "
        "an agent catalog, or delete the body." % (name, missing)
    )


@pytest.mark.parametrize("name", sorted(POINTED_AT_OUTSIDE_THE_AGENT_CATALOGS))
def test_a_declared_orchestrator_protocol_is_not_also_an_agent_catalog_entry(name: str):
    """The other direction, so the declaration cannot become a false claim.

    If a role starts naming it, the protocol is an agent's after all and the
    declaration should go rather than sit there asserting otherwise.
    """
    namers = [role for role in CONVERTED if named_in_catalog(skill_text(role), name)]
    assert not namers, (
        "%r is declared orchestrator-owned but %s now names it in the catalog; "
        "drop the declaration" % (name, namers)
    )


@pytest.mark.parametrize("name", sorted(POINTED_AT_OUTSIDE_THE_AGENT_CATALOGS))
def test_a_declared_orchestrator_protocol_is_still_absent_from_the_digest(name: str):
    """And the third direction: the digest may have learned it meanwhile."""
    assert not covered_by_digest(name), (
        "the compact digest now carries %r, so it needs no external pointer "
        "declaration at all; delete the entry" % name
    )


# ── The claims the catalogs make ABOUT the digest ─────────────────────────────


def _digest_coverage_claims(text: str) -> list:
    """Protocol names a SKILL claims the injected digest covers.

    Six catalog rows spell the covered set out in prose ("a compact digest is
    injected at dispatch and covers iron-laws, receipt-protocol, ..."). That is
    a claim about a file this test can read, so it should not be taken on
    trust: if the digest drops a heading, the row starts telling the dispatch
    that a body it cannot see is already loaded, which is the same content loss
    the orphan class produces, arrived at from the other side.
    """
    known = set(protocol_bodies())
    claims = []
    for line in text.splitlines():
        lowered = line.lower()
        if "digest" not in lowered or "covers" not in lowered:
            continue
        segment = lowered.split("covers", 1)[1]
        # Stop before the "does NOT carry them:" half of the same sentence.
        segment = segment.split(".")[0]
        for token in segment.replace(" and ", ", ").split(","):
            token = token.strip().strip("`")
            if token in known and token not in claims:
                claims.append(token)
    return claims


@pytest.mark.parametrize("role", CONVERTED)
def test_a_catalog_never_claims_the_digest_carries_what_it_does_not(role: str):
    wrong = [
        name
        for name in _digest_coverage_claims(skill_text(role))
        if not covered_by_digest(name)
    ]
    assert not wrong, (
        "%s's catalog tells the dispatch the injected digest already covers "
        "%s, and it does not. The body is then loaded by nothing and fetched "
        "by nobody, which is the orphan defect reached from the other "
        "direction." % (role, wrong)
    )


def test_the_digest_heading_match_folds_hyphens_on_both_sides():
    """Regression for a normalization that was only accidentally right.

    The pre-#538 predicate normalized the protocol NAME (`anti-safe-harbor` to
    `ANTI SAFE HARBOR`) and compared it against a RAW heading
    (`## ANTI-SAFE-HARBOR (frontend agents)`), so the two headings that contain
    a hyphen never matched their own protocol. Harmless while the orphan sets
    were hand-written and neither name was on one; not harmless now that #538
    DERIVES the sets from this predicate, where it would have made
    `anti-safe-harbor` (read by software-engineer and solution-architect) and
    `clean-code-self-check` look like orphans and put a false claim in two
    catalogs.
    """
    for name in ("anti-safe-harbor", "clean-code-self-check"):
        assert covered_by_digest(name), (
            "%r has a heading in hooks/data/compacted-protocols.md and the "
            "coverage predicate does not see it" % name
        )
    assert "anti-safe-harbor" not in DIGEST_ABSENT
    assert "clean-code-self-check" not in DIGEST_ABSENT


# ── Demonstrations: the census firing, on a tree built to make it fire ────────
#
# These pass on `origin/dev` too, and that is correct rather than vacuous: they
# do not test the tree, they test the guard. #528 demonstrated its census by
# hand in the PR body; doing it in the suite means the demonstration cannot go
# stale the way a pasted terminal transcript does.


def _fake_plugin_root(tmp_path: Path, skills: dict, protocols, headings) -> Path:
    root = tmp_path / "plugin-fake"
    (root / "hooks" / "data").mkdir(parents=True)
    (root / "hooks" / "data" / "compacted-protocols.md").write_text(
        "# Digest\n\n" + "".join("## %s\n\nbody\n\n" % h for h in headings),
        encoding="utf-8",
    )
    (root / "skills" / "_shared" / "protocols").mkdir(parents=True)
    for name in protocols:
        (root / "skills" / "_shared" / "protocols" / (name + ".md")).write_text(
            "# %s\n" % name, encoding="utf-8"
        )
    for role, text in skills.items():
        (root / "agents" / role).mkdir(parents=True)
        (root / "agents" / role / "SKILL.md").write_text(text, encoding="utf-8")
    return root


def _thin_contract(*named) -> str:
    row = "| `protocols/<name>` | Full bodies; the digest carries none of %s |" % (
        ", ".join("`protocols/%s`" % n for n in named) or "these"
    )
    return "# Role\n\n## Skill Catalog\n\nBash(\"synaptory skills get <name>\")\n\n%s\n" % row


def test_the_census_fires_on_a_converted_role_nobody_classified(tmp_path: Path):
    """Add a tenth thin-contract agent, cover nothing, and the suite goes red.

    The failure #480's and #537's guards could not produce, because both
    enumerated roles by hand.
    """
    root = _fake_plugin_root(
        tmp_path,
        {"telemetry-analyst": _thin_contract("visual-identity")},
        ["visual-identity", "iron-laws"],
        ["IRON LAWS"],
    )
    assert converted_roles(root) == ("telemetry-analyst",)
    assert unclassified_converted_roles(root) == ["telemetry-analyst"]


def test_the_census_fires_on_a_protocol_the_digest_was_never_taught(tmp_path: Path):
    """Add a protocol body, leave it out of the digest, name it nowhere."""
    root = _fake_plugin_root(
        tmp_path,
        {"software-engineer": _thin_contract("visual-identity")},
        ["visual-identity", "iron-laws", "prompt-injection-guard"],
        ["IRON LAWS"],
    )
    assert "prompt-injection-guard" in digest_absent_protocols(root)
    assert unpointed_digest_absent_protocols(root) == ["prompt-injection-guard"]


def test_the_census_fires_when_a_classified_role_drops_an_orphan_row(tmp_path: Path):
    """The #480 edit itself: delete the read, do not add the catalog name.

    software-engineer is used because it is a role the registry DOES classify,
    so this exercises the per-role half rather than the unclassified half.
    """
    root = _fake_plugin_root(
        tmp_path,
        {"software-engineer": _thin_contract("visual-identity")},
        ["visual-identity", "boundary-safety", "conflict-resolution",
         "coverage-ratchet", "iron-laws"],
        ["IRON LAWS"],
    )
    assert unnamed_orphans_for("software-engineer", root) == [
        "boundary-safety",
        "conflict-resolution",
        "coverage-ratchet",
    ]


def test_the_census_does_not_fire_on_a_read_the_digest_covers(tmp_path: Path):
    """The other half of a guard that means anything: it stays quiet correctly.

    `iron-laws` is on software-engineer's read list and IS in the digest, so it
    needs no catalog row. A census that demanded one for every recorded read
    would be a payload tax, not a correctness check.
    """
    root = _fake_plugin_root(
        tmp_path,
        {"software-engineer": _thin_contract(
            "visual-identity", "boundary-safety", "conflict-resolution",
            "coverage-ratchet")},
        ["visual-identity", "boundary-safety", "conflict-resolution",
         "coverage-ratchet", "iron-laws"],
        ["IRON LAWS"],
    )
    assert "iron-laws" in PROTOCOLS_READ_BEFORE_CONVERSION["software-engineer"]
    assert unnamed_orphans_for("software-engineer", root) == []
    assert unpointed_digest_absent_protocols(root) == []


def test_the_completeness_check_fires_when_the_detector_stops_matching(tmp_path: Path):
    """The sibling failure: a census that quietly covers fewer roles than exist.

    Here the role's SKILL.md publishes no catalog, so the thin-contract
    detector does not match it. Every parametrized test below would then skip
    it in silence. `agent_roles()` is the denominator that catches it.
    """
    root = _fake_plugin_root(
        tmp_path,
        {"software-engineer": "# Software Engineer\n\nNo catalog in this body.\n"},
        ["visual-identity", "iron-laws"],
        ["IRON LAWS", "VISUAL IDENTITY"],
    )
    assert agent_roles(root) == ("software-engineer",)
    assert converted_roles(root) == ()
    unaccounted = set(agent_roles(root)) - set(converted_roles(root)) - set(NOT_CONVERTED)
    assert unaccounted == {"software-engineer"}


def test_the_derived_orphan_set_moves_when_the_digest_moves(tmp_path: Path):
    """Derivation, demonstrated: teach the digest a heading and the set shrinks.

    This is the property a hand-written orphan table cannot have, and the one
    #538 asked for: the enumeration comes from the digest, so a protocol added
    to or removed from it cannot silently create a new orphan.
    """
    protocols = ["visual-identity", "boundary-safety", "conflict-resolution",
                 "coverage-ratchet", "iron-laws"]
    before = _fake_plugin_root(
        tmp_path / "before", {"software-engineer": _thin_contract()},
        protocols, ["IRON LAWS"],
    )
    after = _fake_plugin_root(
        tmp_path / "after", {"software-engineer": _thin_contract()},
        protocols, ["IRON LAWS", "COVERAGE RATCHET"],
    )
    assert "coverage-ratchet" in digest_absent_reads("software-engineer", before)
    assert "coverage-ratchet" not in digest_absent_reads("software-engineer", after)


# ── the matchers themselves, which are the guard (#396) ──────────────────────
#
# The census is only as strong as what "the digest carries this" and "this
# SKILL offers this route" mean. Both predicates were broader than the claims
# they made, so a future omission could land quietly through either one while
# the axis stayed green. Neither collision exists in the tree today, which is
# exactly why they need synthetic arms: a guard nobody can show failing is the
# class of defect this ticket is about.


def test_a_prefix_of_a_heading_is_not_digest_coverage(tmp_path: Path):
    """`covered_by_digest` used normalized SUBSTRING containment, so a protocol
    body named `input` was covered by `## INPUT VALIDATION`.

    The new body has no heading of its own and no catalog route, and the
    protocol axis reported nothing: #538's acceptance criterion, that adding a
    protocol the digest was never taught cannot silently create an orphan,
    defeated by the matcher written to enforce it.
    """
    root = _fake_plugin_root(
        tmp_path,
        {"software-engineer": _thin_contract("visual-identity")},
        ["visual-identity", "iron-laws", "input"],
        ["IRON LAWS", "INPUT VALIDATION"],
    )
    assert not covered_by_digest("input", root), (
        "a heading that merely CONTAINS the name counted as carrying it"
    )
    assert "input" in digest_absent_protocols(root)
    assert unpointed_digest_absent_protocols(root) == ["input"]


def test_the_audience_note_is_still_stripped(tmp_path: Path):
    """The reason substring matching was reached for, kept. Headings carry
    notes like `(SE + QE)`, and the title before the note is the name."""
    root = _fake_plugin_root(
        tmp_path,
        {"software-engineer": _thin_contract()},
        ["tdd-discipline", "iron-laws"],
        ["IRON LAWS (non-negotiable, all agents)", "TDD DISCIPLINE (SE + QE)"],
    )
    assert covered_by_digest("tdd-discipline", root)
    assert covered_by_digest("iron-laws", root)
    assert list(digest_absent_protocols(root)) == []


def test_prose_about_a_route_is_not_a_catalog_entry():
    """`named_in_catalog` searched the whole SKILL body, so a role with no
    catalog row for a name but a line elsewhere mentioning it satisfied the
    guard. Deleting the row while leaving any mention kept the axis green."""
    skill = (
        "# Role\n\n"
        "## Skill Catalog\n\n"
        "| `protocols/<name>` | the digest carries none of `protocols/visual-identity` |\n\n"
        "## Working Notes\n\n"
        "Do not retrieve protocols/coverage-ratchet here; QE owns it.\n"
    )
    assert named_in_catalog(skill, "visual-identity"), "the real row stopped matching"
    assert not named_in_catalog(skill, "coverage-ratchet"), (
        "prose outside the catalog satisfied the catalog guard"
    )


def test_a_skill_with_no_catalog_offers_nothing():
    """An unconverted or malformed SKILL has no catalog section, and a route it
    never offers cannot be found in one."""
    assert not named_in_catalog("# Role\n\nprotocols/iron-laws\n", "iron-laws")
