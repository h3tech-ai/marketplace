"""Derivations and the registry behind the orphaned-protocol census (#538).

Not a test module (pytest collects `test_*.py` only). It holds the two
DERIVED axes of the census and the one thing that cannot be derived, so that
`test_protocol_pointer_census.py` and `test_agent_skill_catalog.py` read the
same answer instead of two hand-maintained copies of it.

## The defect class

A converted agent SKILL.md no longer reads protocol bodies inline. The
SubagentStart hook injects `hooks/data/compacted-protocols.md` instead, and
that digest carries only SOME protocols under a `##` heading. For a protocol
it does not carry, deleting the role's `{{read: .synaptory/.protocols/x.md}}`
line removes the last pointer in the dispatch: the body is still on disk,
still materialized, still served by `synaptory skills get`, and invisible to
the agent that needed it. Nothing fails. Every other test stays green.

Three tickets have now hit the same omission:

  * **#480** found it on the five roles it converted and fixed it by naming
    each orphan in that role's catalog, guarded both ways.
  * **#537 (#483)** found the guard did not cover `coverage-ratchet` (QE was
    the only role reading it, so #480's set was never a superset), fixed QE,
    and found #404's own two roles still uncovered.
  * **#538**, this work, fixed those two.

Each fix was written per role, which is why there was a next one. #528's
argument applies unchanged: **an accessor cannot close a class, and neither
can a per-role guard, because nothing forces the next role to be covered.**
So the role axis and the protocol axis are both walked from the tree, and the
registry below has to CLASSIFY what the walk finds. Adding a role, or adding a
protocol the digest does not carry, fails until it is classified.

## What is derived and what is not

Derived, so it cannot drift:

  * `digest_absent_protocols()` -- every body under `skills/_shared/protocols/`
    with no matching `##` heading in the digest. #538's instruction, and the
    correction #537 earned: enumerate from the digest, never from an earlier
    ticket's list of names.
  * `converted_roles()` -- every agent whose SKILL.md has the thin-contract
    shape, read off the tree rather than listed.

Not derivable, and recorded here with its provenance:

  * `PROTOCOLS_READ_BEFORE_CONVERSION` -- which protocols each role read
    BEFORE it was converted. That is a historical fact about a deleted block;
    no state of the current tree contains it. Recorded as a literal rather
    than read from `git`, because a `git archive` export has no history (four
    unrelated tests already fail that way) -- with the commit it came off named
    in `READ_LIST_SOURCE` so any reader can re-derive it.

## What the census cannot see, stated rather than implied

  * **A new need.** A converted role that ought to consult a protocol it never
    read before is invisible here: the criterion is what the role used to read,
    and a role's needs can grow. The protocol axis catches the case where such
    a protocol has no pointer ANYWHERE; it cannot tell you that a particular
    role should have one.
  * **A pointer's quality.** Naming `protocols/x` in the catalog proves a route
    exists, not that a dispatch takes it. #409 measured zero actual JIT
    retrieval across 12 dispatches with the producer proven present, so a
    catalog row is a route, not a load.
  * **Bodies below the SKILL.** A protocol named only inside a fetchable
    playbook is reachable only after that playbook is fetched. The catalog is
    the mandatory surface, so the catalog is the criterion.
"""

from __future__ import annotations

import re
from pathlib import Path

#: `plugin-claude/`.
PLUGIN_ROOT = Path(__file__).resolve().parents[2]

_DIGEST_RELATIVE = Path("hooks") / "data" / "compacted-protocols.md"
_PROTOCOLS_RELATIVE = Path("skills") / "_shared" / "protocols"


def _root(plugin_root: Path | None) -> Path:
    return PLUGIN_ROOT if plugin_root is None else Path(plugin_root)


def _normalize(text: str) -> str:
    """Fold to upper-case words, so hyphens match spaces on BOTH sides.

    The pre-#538 helper normalized the protocol NAME (`clean-code-self-check`
    to `CLEAN CODE SELF CHECK`) and compared it against a RAW heading
    (`## CLEAN CODE SELF-CHECK`), so a heading that contains a hyphen never
    matched its own protocol. Two of the seventeen headings do:
    `ANTI-SAFE-HARBOR` and `CLEAN CODE SELF-CHECK`. That is the same shape of
    bug #537 found in the substring orphan test -- a comparison that is only
    accidentally right -- and it matters more here, because #538 DERIVES the
    orphan set from this predicate: unfixed, it reports `anti-safe-harbor`
    (read by software-engineer and solution-architect) as an orphan the digest
    already covers, and would have had two roles carry a false catalog claim.
    """
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


def digest_headings(plugin_root: Path | None = None) -> list:
    digest = (_root(plugin_root) / _DIGEST_RELATIVE).read_text(encoding="utf-8")
    return re.findall(r"^## (.+)$", digest, re.MULTILINE)


#: The audience note a digest heading may carry, and the ONLY thing stripped
#: before the title is compared: `## IRON LAWS (non-negotiable, all agents)`,
#: `## TDD DISCIPLINE (SE + QE)`.
_AUDIENCE_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def _heading_title(heading: str) -> str:
    """A digest heading's normalized TITLE, without its audience note."""
    return _normalize(_AUDIENCE_SUFFIX.sub("", heading))


def covered_by_digest(name: str, plugin_root: Path | None = None) -> bool:
    """Does the injected compact digest carry `name` under a heading?

    THE TITLE, EXACTLY, once the audience note is removed. Substring
    containment was the first spelling, chosen because headings carry notes
    like `(SE + QE)`, and it certified names the digest does not carry: a
    protocol body named `input` was "covered" by `## INPUT VALIDATION`, so a
    new protocol the digest was never taught could be added with no heading and
    no catalog route and the orphan guard would stay green. That is the
    acceptance criterion #538 exists for, defeated by the matcher meant to
    enforce it.

    Stripping only the parenthesised suffix keeps the reason substring
    matching was reached for, without keeping what it let through.
    """
    needle = _normalize(name)
    return any(needle == _heading_title(h) for h in digest_headings(plugin_root))


def protocol_bodies(plugin_root: Path | None = None) -> tuple:
    """Every protocol body on disk, by stem. The protocol axis of the census."""
    directory = _root(plugin_root) / _PROTOCOLS_RELATIVE
    return tuple(sorted(path.stem for path in directory.glob("*.md")))


def digest_absent_protocols(plugin_root: Path | None = None) -> tuple:
    """Bodies the digest does not carry: the ones a deleted read strands."""
    return tuple(
        name
        for name in protocol_bodies(plugin_root)
        if not covered_by_digest(name, plugin_root)
    )


def skill_text(role: str, plugin_root: Path | None = None) -> str:
    return (_root(plugin_root) / "agents" / role / "SKILL.md").read_text(
        encoding="utf-8"
    )


#: What makes a SKILL.md a THIN CONTRACT rather than a full instruction body:
#: it tells the dispatch to retrieve, and it publishes a protocol catalog row.
#: Read off the file so a tenth role cannot be added outside the census.
CONVERTED_MARKERS = ("synaptory skills get", "`protocols/<name>`")


def agent_roles(plugin_root: Path | None = None) -> tuple:
    """Every agent directory with a SKILL.md. The denominator."""
    agents = _root(plugin_root) / "agents"
    return tuple(
        sorted(
            directory.name
            for directory in agents.iterdir()
            if directory.is_dir() and (directory / "SKILL.md").is_file()
        )
    )


def converted_roles(plugin_root: Path | None = None) -> tuple:
    """Every agent carrying the thin contract, walked from the tree.

    A DETECTOR, and a detector that stops matching is the failure mode this
    whole file exists to prevent, one level up: a census that silently
    parametrizes over fewer roles than exist reports green over the gap
    instead of over the property. `agent_roles()` is the denominator and
    `NOT_CONVERTED` is the declared remainder, so
    `test_every_agent_directory_is_either_converted_or_declared` fails if the
    two ever fail to add up.
    """
    out = []
    for role in agent_roles(plugin_root):
        text = skill_text(role, plugin_root)
        if all(marker in text for marker in CONVERTED_MARKERS):
            out.append(role)
    return tuple(out)


#: Agents that have NOT been converted to the thin contract, with the reason
#: they are outside the orphan class. An unconverted SKILL still reads its
#: protocol bodies with `{{read: .synaptory/.protocols/x.md}}`, so it carries
#: its own pointers and no catalog row is owed.
#:
#: Empty today: all nine agents are converted. It exists so that a role the
#: DETECTOR fails to recognise cannot pass as "not converted" by default. Each
#: entry's premise is checked, so a declaration cannot outlive the fact.
NOT_CONVERTED: dict = {}


def named_in_catalog(text: str, name: str) -> bool:
    """Does this SKILL offer `protocols/<name>` as a CATALOG entry?

    Three traps, all of which have already been fallen into:

      * A bare substring test also matches the PRE-conversion read directive
        `{{read: .synaptory/.protocols/ux-protocol.md}}`, so the check passes
        on the very tree it was written to reject. #537 found that in its own
        first version; its pre-change failure count moved 15 to 17 once fixed.
        Excluded by the `.synaptory/.` prefix.
      * A prefix match would let `protocols/receipt` be satisfied by
        `protocols/receipt-protocol`. Excluded by requiring the name not to
        continue into another name character.
      * A whole-body search matches PROSE about a route as readily as the route
        itself: a role with no catalog row for `coverage-ratchet` but a line
        elsewhere saying "do not retrieve protocols/coverage-ratchet here"
        satisfied the guard, so deleting the actual row while leaving any
        mention kept the axis green. Scoped to the catalog section, which is
        what "offers it as a catalog entry" means.
    """
    catalog = _catalog_section(text)
    if not catalog:
        return False
    pattern = re.compile(
        r"(?<!\.synaptory/\.)protocols/%s(?![A-Za-z0-9\-])" % re.escape(name)
    )
    return bool(pattern.search(catalog))


def _catalog_section(text: str) -> str:
    """The `## Skill Catalog ...` section of a SKILL body, or "".

    Bounded by the next `## ` heading, because a section that ran to the end of
    the file would put every later section back inside the search.
    """
    match = re.search(r"^##\s+Skill Catalog\b.*$", text, re.MULTILINE | re.IGNORECASE)
    if match is None:
        return ""
    rest = text[match.end():]
    following = re.search(r"^##\s+", rest, re.MULTILINE)
    return rest if following is None else rest[: following.start()]


# ── The registry: what each role read before it was converted ─────────────────
#
# Every protocol body the role's SKILL.md read UNCONDITIONALLY in the last
# version that still carried the reads. Taken by extracting
# `.synaptory/.protocols/<name>.md` from that file version; the commit is named
# in READ_LIST_SOURCE below so the extraction can be repeated.
#
# The digest-absent SUBSET is not stored: `digest_absent_reads()` intersects
# these with `digest_absent_protocols()` at call time, which is #538's
# requirement that the enumeration come from the digest rather than from a
# hand-copied list. Every per-role orphan table published by #480, #487 and
# #537 is reproduced exactly by that intersection.

PROTOCOLS_READ_BEFORE_CONVERSION = {
    "software-engineer": (
        "anti-safe-harbor",
        "boundary-safety",
        "conflict-resolution",
        "coverage-ratchet",
        "freshness-protocol",
        "input-validation",
        "iron-laws",
        "script-output-handling",
        "tool-efficiency",
        "verification-discipline",
        "visual-identity",
    ),
    "code-reviewer": (
        "conflict-resolution",
        "freshness-protocol",
        "input-validation",
        "iron-laws",
        "script-output-handling",
        "tool-efficiency",
        "ux-protocol",
        "verification-discipline",
        "visual-identity",
    ),
    "quality-engineer": (
        "conflict-resolution",
        "coverage-ratchet",
        "input-validation",
        "iron-laws",
        "receipt-protocol",
        "script-output-handling",
        "tool-efficiency",
        "ux-protocol",
        "verification-discipline",
        "visual-identity",
    ),
    "project-owner": (
        "input-validation",
        "open-decision-registry",
        "source-attribution",
        "ux-protocol",
        "verification-discipline",
        "visual-identity",
    ),
    "solution-architect": (
        "anti-safe-harbor",
        "boundary-safety",
        "conflict-resolution",
        "freshness-protocol",
        "input-validation",
        "iron-laws",
        "open-decision-registry",
        "script-output-handling",
        "socratic-gate",
        "source-attribution",
        "tool-efficiency",
        "ux-protocol",
        "verification-discipline",
        "visual-identity",
    ),
    "platform-engineer": (
        "boundary-safety",
        "conflict-resolution",
        "ephemeral-environments",
        "input-validation",
        "iron-laws",
        "script-output-handling",
        "tool-efficiency",
        "ux-protocol",
        "verification-discipline",
        "visual-identity",
    ),
    "technical-writer": (
        "boundary-safety",
        "conflict-resolution",
        "freshness-protocol",
        "input-validation",
        "iron-laws",
        "script-output-handling",
        "socratic-gate",
        "tool-efficiency",
        "ux-protocol",
        "verification-discipline",
        "visual-identity",
    ),
    "research-advisor": (
        "freshness-protocol",
        "input-validation",
        "iron-laws",
        "socratic-gate",
        "tool-efficiency",
        "ux-protocol",
        "verification-discipline",
        "visual-identity",
    ),
    "compliance-engineer": (
        "conflict-resolution",
        "freshness-protocol",
        "input-validation",
        "iron-laws",
        "script-output-handling",
        "tool-efficiency",
        "verification-discipline",
        "visual-identity",
    ),
}

#: The commit each read list was extracted from: the newest version of that
#: role's SKILL.md that still contained `.synaptory/.protocols/<name>.md`
#: directives. Recorded so the literals above are auditable without this file
#: depending on `git` at run time.
READ_LIST_SOURCE = {
    "software-engineer": "1315c961 (removed by #404's 5b2be3f4)",
    "code-reviewer": "1315c961 (removed by #404's 5b2be3f4)",
    "quality-engineer": "b6b062f1 (removed by #483's 5f454d7f)",
    "project-owner": "1315c961 (removed by #405's 2d3fc56f)",
    "solution-architect": "1315c961 (removed by #405's 2d3fc56f)",
    "platform-engineer": "1315c961 (removed by #405's 2d3fc56f)",
    "technical-writer": "cf1e6dc9 (removed by #405's 2d3fc56f)",
    "research-advisor": "cf1e6dc9 (removed by #405's 2d3fc56f)",
    "compliance-engineer": "bdd851ca (removed by #487's 4a2b1fb6)",
}


def digest_absent_reads(role: str, plugin_root: Path | None = None) -> tuple:
    """The protocols this role read that the digest does not carry.

    The orphan set for a role, derived rather than declared. Empty for a role
    whose every read is in the digest.
    """
    absent = set(digest_absent_protocols(plugin_root))
    return tuple(
        name
        for name in PROTOCOLS_READ_BEFORE_CONVERSION.get(role, ())
        if name in absent
    )


# ── The protocol axis: a digest-absent body no agent catalog names ────────────
#
# A protocol the digest does not carry and no converted role's catalog names is
# invisible to every agent dispatch. That is not automatically a defect: some
# protocols are the ORCHESTRATOR's, not an agent's. Those are declared here
# with the file that does point at them, and the declaration is checked in both
# directions -- the named pointer must still exist, and no agent catalog may
# have quietly started naming it -- so it cannot rot into a false claim (the
# `declared_gaps` pattern #528 borrowed from `conformance/contract.json`).

POINTED_AT_OUTSIDE_THE_AGENT_CATALOGS = {
    "design-grooming": (
        "orchestrator-owned: the design-grooming gate runs in the orchestrator's "
        "own context, not in an agent dispatch. Losing these pointers would "
        "leave the mockup-baseline gate with no body to follow.",
        (
            "skills/synaptory/ceremonies/sprint-planning.md",
            "skills/synaptory/ceremonies/inception.md",
            "skills/synaptory/ceremonies/sprint-review.md",
            "skills/synaptory/spq/discovery.md",
            "skills/synaptory/spq/commit.md",
            "agents/project-owner/modes/refinement.md",
        ),
    ),
    "story-pipeline": (
        "orchestrator-owned: the pipeline protocol tells the ORCHESTRATOR how "
        "to drive SE->QE->CR. No agent read it before conversion and none needs "
        "it; the orchestrator fetches it by name in each lifecycle mode.",
        (
            "skills/synaptory/modes/spq.md",
            "skills/synaptory/modes/kanban.md",
            "skills/synaptory/modes/sprint.md",
        ),
    ),
}


# ── The census predicates, as functions so they can be demonstrated ───────────
#
# Each returns the problems it found rather than asserting, so the same code
# runs against the real tree (the guard) and against a synthetic tree (the
# demonstration that the guard fires). A guard nobody has watched fail is a
# guard nobody has tested.


def unclassified_converted_roles(plugin_root: Path | None = None) -> list:
    """Converted roles with no recorded read list. The role axis."""
    return [
        role
        for role in converted_roles(plugin_root)
        if role not in PROTOCOLS_READ_BEFORE_CONVERSION
    ]


def unnamed_orphans_for(role: str, plugin_root: Path | None = None) -> list:
    """Digest-absent protocols this role read and no longer points at."""
    text = skill_text(role, plugin_root)
    return [
        name
        for name in digest_absent_reads(role, plugin_root)
        if not named_in_catalog(text, name)
    ]


def unpointed_digest_absent_protocols(plugin_root: Path | None = None) -> list:
    """Digest-absent protocols named by no catalog and declared by nobody.

    The protocol axis. This is the half that survives a role being deleted, a
    table entry and its catalog row being removed together, or a protocol being
    added to the tree that the digest was never taught about.
    """
    roles = converted_roles(plugin_root)
    texts = {role: skill_text(role, plugin_root) for role in roles}
    out = []
    for name in digest_absent_protocols(plugin_root):
        if any(named_in_catalog(text, name) for text in texts.values()):
            continue
        if name in POINTED_AT_OUTSIDE_THE_AGENT_CATALOGS:
            continue
        out.append(name)
    return out
