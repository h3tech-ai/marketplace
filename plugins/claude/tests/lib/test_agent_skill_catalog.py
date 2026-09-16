"""Layer 2: the thin intent contract + JIT skill catalog (#404, #405, #483).

docs/proposals/capability-profile-pilot.md sections 3.1 and 3.2: agent
SKILL.md files are a thin intent contract (task frame, evidence obligations,
receipt contract) plus a catalog of fetchable skills, with the tech-pack
detection table deleted in favor of "retrieve what the task needs" via
`synaptory skills get`. #404 did the producer and one prover (SE, CR); #405
extended it to five non-pipeline roles (PO, SA, PE, TW, RA); #487 to CE; #483
to quality-engineer, which was the heaviest mandatory payload of the three
pipeline roles by a wide margin. These tests pin five things:

  1. The routing table stays deleted and the catalog instruction is present,
     so a dispatch on a Next.js+Tailwind+Postgres project can retrieve the
     right packs from the catalog alone (the #404 acceptance criterion).
  2. Every catalog name resolves to an on-disk body under the agent tree, and
     every fetchable body on disk appears in the catalog. A name that fails
     here is a `synaptory skills get` 404 in the field, and a body missing
     from the catalog is unreachable guidance.
  3. Every catalog name is retrievable BOTH ways: through the CLI path, by
     running the control plane's own name mapper
     (api/synaptory_api/seed.py `_skill_name_for`, executed here rather than
     restated, so the two cannot drift), and through the disk fallback the
     SKILL spells for source-tree dev. Two routes to one body is the whole
     point of the fallback, and #404's tests checked only that the body
     existed, never that the CLI would answer to that exact name.
  4. The fixed payload per dispatch stays under the 4k-word target, measured
     with the same components as benchmarks/parallelism/pilot_metrics.py
     `harness_payload`: agent.md + SKILL.md + one-level {{include:}} bodies +
     the compacted protocol/rule payloads the SubagentStart hook injects +
     the role's evidence envelope. Tech packs are absent by design: with the
     detection table gone they are per-task JIT retrievals, not fixed
     payload.

  5. The reachability half is NOT here any more. #480 found that deleting a
     role's `{{read: .synaptory/.protocols/x.md}}` block orphaned protocols
     the injected compact digest carries under NO heading, so the read
     removal deleted the content with every other test still green; #480,
     #483 and #538 each hit that same omission on the roles their own ticket
     touched. #538 moved the whole check to
     `test_protocol_pointer_census.py`, where both axes are derived from the
     tree (the converted roles by walking `agents/`, the digest-absent
     protocols by walking the digest), so a role or a protocol added without
     coverage fails instead of being silently uncovered. This file keeps the
     payload and retrievability halves and no longer restates the orphan
     tables.

The five non-pipeline roles are NOT exempt from the payload budget even
where they remain dispatch identities on a gated edge (PO at Work Unit
acceptance, PE and TW at SPQ Acceptance, TW at Checkpoint): a gated dispatch
is exactly the one that must stay cheap, because it is the one that always
runs. Nor is quality-engineer, which is dispatched on every work unit.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PLUGIN_ROOT.parent

#: The two roles #404 slimmed, plus `quality-engineer` from #483. All three
#: pipeline roles now carry the thin contract, so the pipeline that runs on
#: every work unit has no unslimmed dispatch left.
PIPELINE_ROLES = ("software-engineer", "code-reviewer", "quality-engineer")

#: The five #405 slimmed, plus `compliance-engineer` from #487.
ON_DEMAND_ROLES = (
    "project-owner",
    "solution-architect",
    "platform-engineer",
    "technical-writer",
    "research-advisor",
    "compliance-engineer",
)

ROLES = PIPELINE_ROLES + ON_DEMAND_ROLES

#: Subdirectories whose bodies the SKILL.md catalog must list, per role.
#: Mode-specific phase directories under software-engineer (frontend-phases/,
#: ai-ml-phases/, mobile-phases/) are deliberately absent: their loading is
#: owned by the mode guide that names them, and forcing them into the
#: top-level catalog would rebuild the bloat the thin contract removed.
#: platform-engineer's reliability-phases/ and research-advisor's reference/
#: ARE listed, because nothing else names them.
CATALOG_DIRS_BY_ROLE = {
    "software-engineer": ("modes", "phases", "tech-packs", "guides"),
    "code-reviewer": ("modes", "phases", "tech-packs", "guides"),
    "quality-engineer": ("modes", "phases", "guides"),
    "project-owner": ("modes", "phases", "references", "guides"),
    "solution-architect": ("modes", "phases", "guides"),
    "platform-engineer": ("phases", "reliability-phases", "guides"),
    "technical-writer": ("modes", "phases", "guides"),
    "research-advisor": ("modes", "reference", "guides"),
    "compliance-engineer": ("modes", "phases", "guides"),
}

#: Back-compat alias for the #404 spelling.
CATALOG_DIRS = CATALOG_DIRS_BY_ROLE["software-engineer"]

#: Bodies inlined into the SKILL at compose time rather than fetched.
INLINED = {"receipt-protocol.md"}

_INCLUDE_RE = re.compile(r"\{\{\s*include:\s*([^}|]+?)\s*\}\}")
_CATALOG_NAME_RE = re.compile(
    r"`(?P<name>(?:%s)/[a-z0-9\-]+(?:/[a-z0-9\-.]+)+)`" % "|".join(ROLES)
)


def _load_cp_name_mapper():
    """The control plane's real `_skill_name_for`, with nothing else imported.

    seed.py pulls in sqlalchemy and structlog, which a Layer-1 plugin test
    must not depend on, so the function and the one constant it reads are
    lifted out of the module's AST and compiled on their own. That keeps this
    a behavioural check against the actual mapper instead of a restatement of
    its rule in a second place, which is how a name that the SKILL offers and
    the CLI does not serve would slip through.
    """
    source = (REPO_ROOT / "api" / "synaptory_api" / "seed.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    wanted = {"_skill_name_for", "_CP_DELIVERED_SHARED"}
    kept: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            kept.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted for t in node.targets
        ):
            kept.append(node)
    names = {
        node.name if isinstance(node, ast.FunctionDef) else node.targets[0].id
        for node in kept
    }
    assert names == wanted, (
        "api/synaptory_api/seed.py no longer defines %s at module level; the "
        "CLI-path check cannot run against the real mapper any more"
        % sorted(wanted - names)
    )
    module = ast.Module(body=kept, type_ignores=[])
    namespace: dict = {"Path": Path}
    exec(compile(ast.fix_missing_locations(module), "<seed>", "exec"), namespace)
    return namespace["_skill_name_for"]


def _skill_text(role: str) -> str:
    return (PLUGIN_ROOT / "agents" / role / "SKILL.md").read_text(encoding="utf-8")


def _words(text: str) -> int:
    return len(text.split())


# ── 1. routing table deleted, catalog instruction present ────────────────────


def test_se_tech_pack_detection_table_is_deleted():
    text = _skill_text("software-engineer")
    # The old table routed packs off file probes; the model now decides from
    # the catalog plus the repo it can see.
    assert "| Detection |" not in text
    assert "Auto-detection" not in text
    assert "next.config" not in text, (
        "a file-probe routing rule survived; the detection table must stay deleted"
    )


@pytest.mark.parametrize("role", ROLES)
def test_catalog_fetch_instruction_present(role: str):
    text = _skill_text(role)
    assert "synaptory skills get" in text, (
        "%s SKILL.md lost the JIT fetch instruction" % role
    )
    # Disk fallback for source-tree dev, mirroring how hooks fall back.
    assert ("{{path: agents/%s/" % role) in text, (
        "%s SKILL.md lost the on-disk fallback path" % role
    )


def test_nextjs_tailwind_postgres_stack_is_retrievable_from_catalog():
    """The #404 acceptance fixture: a Next.js+Tailwind+Postgres dispatch must
    find its packs in the catalog with no routing table."""
    text = _skill_text("software-engineer")
    for pack in ("nextjs", "tailwind", "postgresql", "performance"):
        name = "software-engineer/tech-packs/%s" % pack
        assert name in text, "catalog does not offer %s" % name
        assert (PLUGIN_ROOT / "agents" / "software-engineer" / "tech-packs" / (pack + ".md")).is_file()


# ── 2. catalog and disk agree in both directions ─────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_every_fetchable_body_is_in_the_catalog(role: str):
    text = _skill_text(role)
    agent_dir = PLUGIN_ROOT / "agents" / role
    missing = []
    for sub in CATALOG_DIRS_BY_ROLE[role]:
        directory = agent_dir / sub
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if path.name in INLINED:
                continue
            name = "%s/%s/%s" % (role, sub, path.stem)
            if name not in text:
                missing.append(name)
    assert not missing, (
        "bodies on disk that the catalog never offers (unreachable after the "
        "inline deletion): %s" % missing
    )


@pytest.mark.parametrize("role", ROLES)
def test_every_catalog_name_resolves_to_a_body_on_disk(role: str):
    text = _skill_text(role)
    unresolved = []
    for match in _CATALOG_NAME_RE.finditer(text):
        name = match.group("name")
        owner, rest = name.split("/", 1)
        if owner != role or rest.endswith(".md"):
            # `.md`-suffixed spellings are fallback path examples, checked
            # via the {{path:}} directive form instead.
            continue
        if not (PLUGIN_ROOT / "agents" / owner / (rest + ".md")).is_file():
            unresolved.append(name)
    assert not unresolved, (
        "catalog names with no authored body (a `synaptory skills get` 404 in "
        "the field): %s" % unresolved
    )


# ── 2b. both retrieval routes answer to the same name (#405) ─────────────────


def _catalog_names(role: str) -> list[str]:
    """Every `<role>/<family>/<stem>` name this role's SKILL.md offers."""
    text = _skill_text(role)
    out = []
    for match in _CATALOG_NAME_RE.finditer(text):
        name = match.group("name")
        owner, rest = name.split("/", 1)
        if owner != role or rest.endswith(".md"):
            continue
        if name not in out:
            out.append(name)
    return out


@pytest.mark.parametrize("role", ROLES)
def test_every_catalog_name_is_served_by_the_cli_path(role: str):
    """Route one: `synaptory skills get <name>`.

    The CLI resolves a name against the control plane, whose skill rows come
    from seed.py walking `agents/**/*.md` and naming each row with
    `_skill_name_for`. So the proof that a catalog name is fetchable is that
    the real mapper, run over the real body path, returns that exact string.
    The seeder walk itself is asserted separately below, because a mapper that
    would produce the right name for a file the seeder never visits still
    yields a 404.
    """
    skill_name_for = _load_cp_name_mapper()
    names = _catalog_names(role)
    assert names, "%s SKILL.md offers no catalog names at all" % role
    wrong = []
    for name in names:
        # A catalog name is `<role>/<family>/<stem>`, and the seeder's own rule
        # is that it maps back to `agents/<role>/<family>/<stem>.md`.
        body = PLUGIN_ROOT / "agents" / (name + ".md")
        assert body.is_file(), "catalog offers %s with no body on disk" % name
        served = skill_name_for(body, PLUGIN_ROOT)
        if served != name:
            wrong.append((name, served))
    assert not wrong, (
        "the catalog tells the agent to fetch a name the control plane serves "
        "under a different one (offered, served): %s" % wrong
    )


def test_the_seeder_still_walks_every_agent_body():
    """The mapper is only half the CLI route; the walk is the other half.

    #404 relied on seed.py's `agents/**` rglob and changed nothing there. This
    asserts the rglob is still unrestricted, because narrowing it to an
    allowlist of subdirectories is the change that would 404 the new
    `guides/`, `references/`, `reference/` and `reliability-phases/` names
    silently, with every other test in this file still green.
    """
    source = (REPO_ROOT / "api" / "synaptory_api" / "seed.py").read_text(
        encoding="utf-8"
    )
    assert 'agent_root.rglob("*.md")' in source, (
        "seed.py no longer walks every agent body; catalog names outside "
        "whatever it walks now are `synaptory skills get` 404s"
    )


@pytest.mark.parametrize("role", ROLES)
def test_every_catalog_name_is_served_by_the_disk_fallback(role: str):
    """Route two: the on-disk path the SKILL spells for source-tree dev.

    The SKILL states the fallback as a `{{path: agents/<role>/...}}` directive
    plus the rule that `<role>/<family>/<stem>` lives at `<family>/<stem>.md`
    under the agent directory. This walks that rule for every offered name.
    """
    text = _skill_text(role)
    assert ("{{path: agents/%s/" % role) in text, (
        "%s SKILL.md states no disk fallback, so a source-tree dispatch with "
        "no control plane can reach none of its catalog" % role
    )
    agent_dir = PLUGIN_ROOT / "agents" / role
    unreachable = []
    for name in _catalog_names(role):
        relative = name.split("/", 1)[1] + ".md"
        if not (agent_dir / relative).is_file():
            unreachable.append(relative)
    assert not unreachable, (
        "%s catalog names whose disk fallback resolves to nothing: %s"
        % (role, unreachable)
    )


def test_moved_bodies_survived_the_move():
    """Deleted inline sections must be re-fetchable, not destroyed."""
    se_playbook = (
        PLUGIN_ROOT / "agents" / "software-engineer" / "guides" / "engineering-playbook.md"
    ).read_text(encoding="utf-8")
    for marker in (
        "Red Flags",
        "Common Mistakes",
        "Parallel Execution",
        "foundations-complete.json",
        "Sprint-Scoped Execution",
        "Input Classification",
    ):
        assert marker in se_playbook, "SE playbook lost the moved %r section" % marker

    cr_dir = PLUGIN_ROOT / "agents" / "code-reviewer"
    cr_playbook = (cr_dir / "guides" / "review-playbook.md").read_text(encoding="utf-8")
    for marker in ("Red Flags", "Common Mistakes", "Execution Checklist", "Adversarial Depth"):
        assert marker in cr_playbook, "CR playbook lost the moved %r section" % marker
    phase_markers = {
        "phases/01-spec-compliance.md": ">20% of acceptance criteria",
        "phases/02-architecture-conformance.md": "Service boundaries",
        "phases/03-code-quality.md": "Cyclomatic complexity",
        "phases/04-performance.md": "N+1 queries",
        "phases/05-test-quality.md": "Assertion quality",
        "phases/06-review-report.md": "Executive Summary",
    }
    for rel, marker in phase_markers.items():
        body = (cr_dir / rel).read_text(encoding="utf-8")
        assert marker in body, "%s lost the moved %r checklist" % (rel, marker)


#: Sections #405 moved out of each non-pipeline SKILL.md and into its
#: playbook. Deleting guidance is not the same as making it fetchable, and the
#: only way to tell the two apart afterwards is to name what moved.
MOVED_TO_PLAYBOOK = {
    "project-owner": (
        "guides/product-playbook.md",
        (
            "CARDINAL RULE",
            "Planning Parameters",
            "Output Routing",
            "Brownfield Awareness",
            "Input Classification",
            "Pre-Flight Read Order",
            "Checkpoint Protocol",
            "Quality Rules",
            "Anti-Patterns",
            "Red Flags",
            "Artifact Maintenance",
            "Release & Sign-Off Flow",
            "Pre-Receipt Checklist",
        ),
    ),
    "solution-architect": (
        "guides/architecture-playbook.md",
        (
            "Brownfield Awareness",
            "Engagement Mode",
            "Input Classification",
            "Config Paths",
            "Plan Chunking",
            "Pre-Flight Read Order",
            "Checkpoint Protocol",
            "Parallel Execution",
            "Cloud-Specific Patterns",
            "Red Flags",
            "Common Mistakes",
            "Execution Checklist",
            "Cross-Validation Pass",
        ),
    ),
    "platform-engineer": (
        "guides/platform-playbook.md",
        (
            "Engagement Mode",
            "Brownfield Awareness",
            "IaC Tool Resolution",
            "Phase Index",
            "Dispatch Protocol",
            "Parallel Execution",
            "Output Structure",
            "Red Flags",
            "Common Mistakes",
            "Verification Checklist",
            "Handoff",
        ),
    ),
    "technical-writer": (
        "guides/writing-playbook.md",
        (
            "Identity & Ownership",
            "Mode Detection Rules",
            "Engagement Mode",
            "Progress Output",
            "Dispatch Protocol",
        ),
    ),
    "research-advisor": (
        "guides/advisory-playbook.md",
        (
            "Core Principles",
            "Brownfield Awareness",
            "Pre-Flight Read Order",
            "Checkpoint Protocol",
            "Input Classification",
            "Pipeline Integration",
            "Gate Companion Behavior",
            "Tool Usage",
            "Red Flags",
            "Execution Checklist",
            "Common Mistakes",
        ),
    ),
    # #483. QE was the heaviest mandatory payload of the three pipeline roles
    # by a wide margin, and ~10,600 of its words were `{{include:}}` expansion
    # of its own phase guides. Those became catalog entries; everything below
    # was prose in the SKILL body and moved to the playbook.
    "quality-engineer": (
        "guides/testing-playbook.md",
        (
            "Mode Detection Rules",
            "Protocol Fallback",
            "Input Classification",
            "Pre-Flight Read Order",
            "Checkpoint Protocol",
            "Engagement Mode",
            "Progress Output",
            "Brownfield Awareness",
            "Coverage Ratchet",
            "Config Paths",
            "Position in the Pipeline",
            "Sprint-Scoped Testing",
            "Hardening Sprint Mode",
            "Graceful Degradation",
            "Output Structure",
            "Phases and Parallel Execution Strategy",
            "Parallel Output Verification",
            "Red Flags",
            "Common Mistakes",
            "Issues Ledger — field definitions",
            "Pre-Receipt Checklist",
            "Execution Checklist",
        ),
    ),
    "compliance-engineer": (
        "guides/security-playbook.md",
        (
            "Finding Memory",
            "Engagement Mode",
            "Progress Output",
            "Scope Boundary",
            "Pre-Flight Read Order",
            "Checkpoint Protocol",
            "Input Classification",
            "Brownfield Awareness",
            "Phase Index",
            "Dispatch Protocol",
            "Parallel Execution",
            "Phase 0: Reconnaissance",
            "Process Flow",
            "Output Contract",
            "Severity Classification Standard",
            "Red Flags",
            "Execution Checklist",
            "Common Mistakes",
        ),
    ),
}


#: Every role with a MOVED-SECTION INVENTORY, derived from the table rather
#: than restated, so adding a role to `MOVED_TO_PLAYBOOK` cannot leave its
#: inventory unchecked.
#:
#: This was called `CONVERTED_ROLES` until #538, and the name was part of the
#: problem: it holds seven of the NINE converted roles, because #404 wrote no
#: inventory for software-engineer or code-reviewer (their playbooks are
#: checked by `test_moved_bodies_survived_the_move` instead). Anything
#: parametrized over it therefore skips the two roles this ticket had to fix,
#: which is how the orphan guard missed them for two waves. The real set of
#: converted roles is walked from the tree by
#: `protocol_pointers.converted_roles()`.
PLAYBOOK_ROLES = tuple(sorted(MOVED_TO_PLAYBOOK))


@pytest.mark.parametrize("role", PLAYBOOK_ROLES)
def test_on_demand_playbooks_carry_what_left_the_skill(role: str):
    relative, markers = MOVED_TO_PLAYBOOK[role]
    playbook = (PLUGIN_ROOT / "agents" / role / relative).read_text(encoding="utf-8")
    for marker in markers:
        assert marker in playbook, "%s playbook lost the moved %r section" % (
            role,
            marker,
        )


@pytest.mark.parametrize("role", PLAYBOOK_ROLES)
def test_on_demand_skills_no_longer_inline_the_protocol_read_block(role: str):
    """The compact protocol digest is injected at dispatch; reading the full
    bodies too was the duplicate load #404 removed for SE and CR.

    Measured on a fixture whose `.synaptory/.protocols/` is materialized (what
    a real SessionStart produces), these reads were the single largest
    component of the five roles' mandatory payload. They stay reachable as
    `protocols/<name>` catalog entries.
    """
    text = _skill_text(role)
    assert ".synaptory/.protocols/" not in text.split("## Skill Catalog")[0], (
        "%s SKILL.md still reads full protocol bodies into the mandatory "
        "payload; the SubagentStart hook already injects the compact digest"
        % role
    )
    assert "`protocols/<name>`" in text, (
        "%s dropped the protocol read block without offering the bodies in "
        "the catalog, so the full protocols became unreachable" % role
    )


# ── 2c. the orphaned-protocol class lives in the census now (#538) ───────────
#
# What stood here: `PROTOCOLS_THE_DIGEST_DOES_NOT_CARRY` (a hand-written
# per-role orphan table), the exhaustive QE walk, the five-name cross-role
# guard from #480, and two STRICT XFAILS declaring that #404's own two roles
# still carried the defect #480 fixed.
#
# All of it is gone rather than moved verbatim, because the shape was the
# problem. Every one of those tables was written for the roles that one
# ticket happened to convert, so the next conversion started outside the
# guard, three times running. `test_protocol_pointer_census.py` derives both
# axes from the tree instead: the converted roles by walking `agents/`, the
# digest-absent protocols by walking the digest. The per-role orphan set is
# the intersection, computed at call time, so a protocol entering or leaving
# the digest cannot leave a stale claim behind.
#
# The two strict xfails were `test_pre_404_roles_name_their_digest_absent_
# protocols[software-engineer]` and `[code-reviewer]`. #538 named those four
# and three protocols in the two catalogs, so the declarations became
# XPASSes, which is precisely what `strict=True` was there to force. They are
# DELETED, not rewritten to point at a closed ticket (the #517 rule); the
# assertion they made is now made unconditionally, for every converted role,
# by `test_every_digest_absent_protocol_the_role_read_is_named_in_its_catalog`.


# ── 3. the fixed payload budget ───────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_fixed_dispatch_payload_under_4k_words(role: str):
    """Same components as pilot_metrics.harness_payload, minus per-task JIT
    retrievals; the section 3.2 target is a fixed payload below 4k words."""
    import evidence_contract as ec

    agent_dir = PLUGIN_ROOT / "agents" / role
    skill = _skill_text(role)
    total = _words((agent_dir / "agent.md").read_text(encoding="utf-8"))
    total += _words(skill)
    for match in _INCLUDE_RE.finditer(skill):
        target = agent_dir / match.group(1).strip()
        assert target.is_file(), "{{include: %s}} names a missing file" % match.group(1)
        total += _words(target.read_text(encoding="utf-8"))
    data_dir = PLUGIN_ROOT / "hooks" / "data"
    for name in ("compacted-protocols.md", "compacted-rules.md"):
        total += _words((data_dir / name).read_text(encoding="utf-8"))
    # #501: measure the envelope a REAL dispatch carries, which is the one
    # with the story's DoD contract resolved — and specifically its worst
    # case, a `baa_enforced` project where the undetermined list is longest.
    # Rendering the story-unresolved envelope here measured a shape that only
    # occurs when the hook cannot name the story, and so understated the
    # payload every ordinary dispatch actually pays.
    worst_case_dod = {
        "tier": "release",
        "tier_source": "planned",
        "active_checks": [
            "tests_pass", "build_succeeds", "code_reviewed",
            "coverage_no_decrease", "ui_acceptance", "runtime_verified",
        ],
        "undetermined_checks": [
            {"check": "no_critical_findings", "why": "if a receipt shows PHI"},
            {
                "check": "integration_verified",
                "why": "if you claim a service is live",
            },
        ],
    }
    total += _words(
        ec.render_envelope("synaptory:" + role, dod=worst_case_dod) or ""
    )
    assert total < 4000, (
        "%s fixed dispatch payload is %d words; the section 3.2 contract is "
        "under 4000. Move guidance into the fetchable catalog instead of the "
        "SKILL body." % (role, total)
    )


# ── 4. QE specifics: what moved out of MANDATORY, and what must not (#483) ───


def test_qe_phase_guides_are_no_longer_inlined_into_mandatory_payload():
    """Where QE's reduction actually came from, pinned so it cannot come back.

    Before #483 the QE SKILL carried twelve `{{include:}}` directives, and the
    eleven phase/mode bodies among them were 10,624 of its 17,922 mandatory
    words -- measured with `benchmarks/parallelism/pilot_metrics.py`
    `harness_payload` against the bare `benchmarks/parallelism/stack_fixture.py`
    project (Next.js + Tailwind + Postgres, no `.synaptory/.protocols/`
    materialized). An `{{include:}}` is expanded before the model decides
    anything, so those words were unconditional; as catalog entries they are a
    ceiling the model may draw on instead.

    **What the fixture cannot see**, and what keeps this from being a saving
    claim: whether the ceiling is ever drawn on. #409 measured ZERO retrievals
    across 12 dispatches on two instruments sharing no code, with #446's
    producer proven present, so on the evidence so far the JIT half has not
    been paid back. The defensible statement is that the MANDATORY half fell;
    the rest is a bound, not a cost.
    """
    skill = _skill_text("quality-engineer")
    included = {m.group(1).strip() for m in _INCLUDE_RE.finditer(skill)}
    leaked = sorted(
        rel for rel in included if Path(rel).name not in INLINED
    )
    assert not leaked, (
        "these bodies are still expanded into QE's mandatory payload instead "
        "of being fetched from the catalog: %s" % leaked
    )


def test_the_qe_receipt_contract_stays_mandatory():
    """The other half of the same rule: obligations must not be fetch-gated.

    #404's discipline is that receipt / evidence / DoD content stays complete
    and unconditional while advisory prose becomes fetchable. A dispatch that
    has to retrieve its own receipt contract can fail to retrieve it, and the
    failure mode is a receipt the DoD gate scores as `None` -- indistinguishable
    from work that was never done. So `phases/receipt-protocol.md` is the one
    body that must remain an `{{include:}}`.
    """
    skill = _skill_text("quality-engineer")
    assert "{{include: phases/receipt-protocol.md}}" in skill, (
        "QE's receipt contract stopped being inlined; evidence obligations "
        "must not depend on a retrieval succeeding"
    )
    body = (
        PLUGIN_ROOT
        / "agents"
        / "quality-engineer"
        / "phases"
        / "receipt-protocol.md"
    ).read_text(encoding="utf-8")
    # The shapes the DoD gate reads by name. Losing any of them silently
    # converts a scored check into an unevaluable one (#403's `None`).
    for shape in (
        "runtime_verification",
        "ui_verification",
        "coverage_delta",
        "integrations",
        "qe-verification",
        "verification_commands",
        "issues.json",
    ):
        assert shape in body, (
            "QE's mandatory receipt contract no longer names %r" % shape
        )


def test_the_authored_case_obligation_stays_in_the_mandatory_body():
    """#406's inversion is an obligation, not advice, so it cannot be fetched.

    QE must report a per-case outcome for every id in the sealed manifest, and
    an omitted id fails `tests_pass` outright. A QE that never retrieved that
    instruction would omit the block and read as having dropped every case.
    """
    skill = _skill_text("quality-engineer")
    for marker in (
        "authored_at_commit",
        "You do not author the acceptance set",
        "metrics.authored_test_cases.results",
        "not-applicable",
        "completed_at",
    ):
        assert marker in skill, (
            "#406's authored-case obligation lost %r from the mandatory "
            "payload" % marker
        )
