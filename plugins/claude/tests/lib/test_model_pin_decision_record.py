"""Layer 1 -- a pin must be a recorded decision, not an inherited default (#441).

WHY THIS FILE EXISTS
--------------------
#441 opened on an observation: `model-pins.json` pinned `opus` to
`claude-opus-4-8` (family `claude-4.8`) while `sonnet` was on `claude-sonnet-5`
(family `claude-5`), so the tier the four strategic roles pay a premium for was
a generation behind the tier the executors ran on. Two things rested on "opus
is the more capable tier": the strategic-role routing, and #434's argument for
moving CR off the producer's tier.

#519 then measured the host and inverted the premise. The `Agent()` tool's
`model` parameter accepts only the four tier aliases, so the alias is the only
value a dispatch can carry and the pin never reaches one. Nine real dispatches
on Claude Code 2.1.236 (2026-09-03) all ran `claude-opus-5`. The premium tier
was never a generation behind in the running system -- only in the file. What
was wrong was the file's statement about the system, which `./synaptory build`
ships as `ai_bom.model_pins`.

#441's resolution: roll `opus` to the measured id, keep `sonnet` and `haiku`,
and record the reasoning for all three so the next reader can tell a REVIEWED
pin from an INHERITED one. `last_reviewed` alone cannot carry that -- it is one
date for a whole file, and the drift here was per tier.

WHAT THIS FILE ASSERTS IS IMPOSSIBLE
------------------------------------
1. A tier whose pin disagrees with the alias resolution measured for it. That
   disagreement is the #441 defect itself, and it is silent: nothing on a
   dispatch path reads this file, so only a test can notice.
2. A tier carried forward with no recorded decision, or with a decision that
   does not say which of `kept` / `rolled` it was, when it was taken, or why.
   An inherited pin that reads identically to a reviewed one is how this
   defect survived from the last roll to #409's observation.
3. A `last_reviewed` older than the measurement the decisions rest on -- the
   exact rot #441 names ("`last_reviewed` predates the Claude 5 family
   reaching the opus line").
4. A retired pin laundered into `runtime_variants` to stop the checker
   reporting it. `runtime_variants` means context variants that bill
   identically; a previous pin is an id no dispatch on this host can reach.
5. A capability ordering asserted between tiers. No such benchmark exists in
   this repository, and `cost_tier: premium` must not be read as "stronger".

WHAT THIS FILE DOES NOT ASSERT
------------------------------
That any pin BINDS. It does not, and #441 did not change that: re-pinning
alters what the file claims, never which weights run. Nor that a receipt's
`model` is true -- it is self-attested (#493, #519). Nor a capability ordering
in either direction: this file forbids claiming one, it does not establish the
opposite.

Nor is the measured table below a permanent fact. Alias-to-id resolution is a
server-side decision that can change on any day, on one host version, on one
deployment. When it moves, tests here fail -- which is the alarm working, and
the fix is a fresh measurement plus a new `decision` block, never an edit that
widens the expectation until the alarm stops.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
BACKENDS = PLUGIN_ROOT / "skills" / "_shared" / "backends"
MODEL_PINS = BACKENDS / "model-pins.json"
CLAUDE_WRAPPER = BACKENDS / "claude.md"

#: The alias-to-id resolutions #519 measured, per tier, with the class of
#: evidence behind each one. This is the ONLY place a pin is compared against
#: an observation, so the provenance travels with the number rather than being
#: recoverable only from a PR body.
#:
#: `subagent-dispatch` -- observed on a real `Agent(model=<alias>)` dispatch,
#: read out of the subagent's own transcript (`message.model`). Cells A-D and I
#: of #519/PR #525: marketplace install and `--plugin-dir` sideload, with and
#: without an ambient CLI model default. Five dispatches, one answer.
#:
#: `session-level` -- observed on a session started with `--model <alias>`
#: (cells E1/E2 and the session half of cell C), not on a subagent dispatch. A
#: weaker observation of the same aliasing, recorded as weaker rather than
#: rounded up. No synaptory role routes to `haiku`, so no subagent dispatch on
#: that tier exists to measure.
MEASURED_ALIAS_RESOLUTION = {
    "opus": ("claude-opus-5", "subagent-dispatch"),
    "sonnet": ("claude-sonnet-5", "session-level"),
    "haiku": ("claude-haiku-4-5", "session-level"),
}

#: When the measurements above were taken (Claude Code 2.1.236). A
#: `last_reviewed` older than this would claim a review that could not have
#: seen them.
MEASURED_ON = date(2026, 9, 3)

VALID_OUTCOMES = frozenset({"kept", "rolled"})

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module")
def document() -> dict:
    return json.loads(MODEL_PINS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pins(document) -> dict:
    return document["tiers"]


def _as_date(value: str) -> date:
    assert _ISO_DATE.match(value), f"{value!r} is not an ISO yyyy-mm-dd date"
    return date.fromisoformat(value)


# --- 1. the pin must agree with what was measured ----------------------------

@pytest.mark.parametrize("alias", sorted(MEASURED_ALIAS_RESOLUTION))
def test_each_pin_names_the_id_its_alias_was_measured_resolving_to(
    alias: str, pins
) -> None:
    """The #441 defect, asserted rather than left to a receipt to reveal.

    Impossible after this: `model-pins.json` naming an id for a tier while a
    different id is what that tier's alias resolves to. On `dev` before #441
    the `opus` row said `claude-opus-4-8` against a measured `claude-opus-5`,
    and the only thing that noticed was `model_pin_check.py` refusing the
    HONEST receipts of the six roles routed there.
    """
    assert alias in pins, f"tier {alias!r} was measured but is not pinned"
    measured, _evidence = MEASURED_ALIAS_RESOLUTION[alias]
    assert pins[alias]["model_id"] == measured, (
        f"tier {alias!r} pins {pins[alias]['model_id']!r} but its alias was "
        f"measured resolving to {measured!r} (#519, Claude Code 2.1.236, "
        f"{MEASURED_ON.isoformat()}). Either the alias moved -- re-measure and "
        f"record a new `decision` block -- or the pin was edited without one."
    )


def test_no_pinned_tier_is_missing_from_the_measured_table(pins) -> None:
    """A tier nobody measured must not look like a tier somebody did.

    The zero rule applied to evidence: 'no measurement for this tier' and
    'measured, and it agrees' are different states, and a table that silently
    omits a tier collapses them into the second one.
    """
    unmeasured = sorted(set(pins) - set(MEASURED_ALIAS_RESOLUTION))
    assert not unmeasured, (
        f"tier(s) {unmeasured} are pinned but have no entry in "
        f"MEASURED_ALIAS_RESOLUTION, so nothing here checks their pin against "
        f"an observation. Measure the alias and add the row, with its evidence "
        f"class -- do not delete the assertion."
    )


def test_the_evidence_class_of_each_measurement_is_recorded(pins) -> None:
    """A session-level observation must not be filed as a dispatch observation.

    `haiku` and `sonnet` were measured by starting a session on the alias, not
    by dispatching a subagent through `Agent(model=...)`. That is a weaker
    observation of the same aliasing and the file says so per tier; this
    asserts the two agree, so neither can be quietly upgraded.
    """
    for alias, (_measured, evidence) in MEASURED_ALIAS_RESOLUTION.items():
        assert evidence in {"subagent-dispatch", "session-level"}
        recorded = pins[alias]["decision"]["observation_class"]
        assert recorded == evidence, (
            f"tier {alias!r} records observation_class {recorded!r} but the "
            f"measurement behind it was {evidence!r}"
        )


# --- 2. every tier carries a decision ----------------------------------------

@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku"])
def test_every_tier_records_a_decision_including_the_decision_to_keep(
    alias: str, pins
) -> None:
    """#441's first acceptance criterion, made unforgeable by a test.

    Impossible after this: a tier carried into a release with no recorded
    decision. `kept` is a decision and must be written down like `rolled` --
    otherwise a pin nobody looked at is indistinguishable from one somebody
    reviewed, which is precisely how `opus` sat a generation behind in the file
    from the last roll until #409 tripped over it.
    """
    tier = pins[alias]
    decision = tier.get("decision")
    assert isinstance(decision, dict), (
        f"tier {alias!r} has no `decision` block. Every pin is a decision, "
        f"including the decision to keep it unchanged (#441)."
    )

    outcome = decision.get("outcome")
    assert outcome in VALID_OUTCOMES, (
        f"tier {alias!r} records outcome {outcome!r}; expected one of "
        f"{sorted(VALID_OUTCOMES)}"
    )

    for field in ("decided_on", "reason", "previous_model_id",
                  "observed_model_id", "observation_class"):
        value = decision.get(field)
        assert isinstance(value, str) and value.strip(), (
            f"tier {alias!r} decision is missing {field!r}"
        )

    assert decision.get("issue") == 441, (
        f"tier {alias!r} decision does not name the issue that took it"
    )

    assert len(decision["reason"].split()) >= 25, (
        f"tier {alias!r} gives a {len(decision['reason'].split())}-word reason. "
        f"A reason short enough to be a label is not a recorded decision; the "
        f"next reader needs to know what was considered, not just the verdict."
    )


@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku"])
def test_the_recorded_outcome_matches_what_the_pin_actually_says(
    alias: str, pins
) -> None:
    """A `kept` that changed the id, or a `rolled` that changed nothing, is a lie.

    Impossible after this: the narrative and the data disagreeing. That pairing
    -- a machine-readable claim next to prose saying something else, with the
    claim being the half that ships -- is the failure #517 named and #519 found
    again in this very file, whose `description` still calls itself the source
    of truth for a resolution nothing performs.
    """
    tier = pins[alias]
    decision = tier["decision"]
    changed = decision["previous_model_id"] != tier["model_id"]
    if decision["outcome"] == "kept":
        assert not changed, (
            f"tier {alias!r} is recorded as `kept` but its model_id "
            f"({tier['model_id']!r}) differs from previous_model_id "
            f"({decision['previous_model_id']!r})"
        )
    else:
        assert changed, (
            f"tier {alias!r} is recorded as `rolled` but its model_id is "
            f"unchanged from previous_model_id ({tier['model_id']!r})"
        )
    assert decision["observed_model_id"] == tier["model_id"], (
        f"tier {alias!r} pins {tier['model_id']!r} while recording that "
        f"{decision['observed_model_id']!r} was observed. A pin that "
        f"deliberately differs from the observation needs a reason stated "
        f"here, not a mismatch left for a reader to notice."
    )


# --- 3. the review date must cover the evidence ------------------------------

def test_last_reviewed_is_not_older_than_the_measurement_it_rests_on(
    document, pins
) -> None:
    """The rot #441 names, guarded.

    Impossible after this: a `last_reviewed` that predates the measurement the
    decisions cite. The issue's own words -- "`last_reviewed` in that file
    predates the Claude 5 family reaching the opus line" -- describe a review
    date that had stopped meaning anything, and a stale date is worse than an
    absent one because it reads as reassurance.
    """
    reviewed = _as_date(document["last_reviewed"])
    assert reviewed >= MEASURED_ON, (
        f"last_reviewed is {reviewed.isoformat()} but the decisions rest on a "
        f"measurement taken {MEASURED_ON.isoformat()}, so the file claims a "
        f"review that could not have seen it"
    )
    for alias, tier in pins.items():
        decided = _as_date(tier["decision"]["decided_on"])
        assert decided >= MEASURED_ON, (
            f"tier {alias!r} was decided {decided.isoformat()}, before the "
            f"{MEASURED_ON.isoformat()} measurement its reason cites"
        )
        assert decided <= reviewed, (
            f"tier {alias!r} records a decision dated {decided.isoformat()}, "
            f"after last_reviewed ({reviewed.isoformat()}); the file-level "
            f"review date must cover every decision in it"
        )


# --- 4. a retired pin is not a runtime variant -------------------------------

@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku"])
def test_a_retired_pin_is_never_declared_as_a_runtime_variant(alias: str, pins) -> None:
    """The cheap wrong fix, closed before anyone reaches for it.

    Impossible after this: silencing `model_pin_check.py` by adding the
    previous pin to `runtime_variants`. The checker accepts each tier's
    `model_id` plus its declared variants, so listing a retired pin there would
    make a receipt naming unreachable weights pass. `runtime_variants` means
    the SAME model surfaced with a context suffix, billing identically; a
    previous pin is a different model that no dispatch on this host can reach
    through the alias.
    """
    tier = pins[alias]
    variants = tier.get("runtime_variants") or []
    previous = tier["decision"]["previous_model_id"]
    if previous == tier["model_id"]:
        return  # nothing was retired
    assert previous not in variants, (
        f"tier {alias!r} lists its retired pin {previous!r} in "
        f"runtime_variants, which makes model_pin_check accept a receipt "
        f"naming weights the alias cannot reach"
    )
    for variant in variants:
        assert variant.startswith(tier["model_id"]), (
            f"tier {alias!r} declares runtime variant {variant!r}, which is "
            f"not a suffixed form of its pinned id {tier['model_id']!r}"
        )


# --- 5. the tier alias must not be sold as a capability claim ----------------

def test_the_pin_file_says_a_tier_alias_is_a_routing_label(document) -> None:
    """#441's fallback resolution, taken as well as the roll rather than instead.

    Impossible after this: the file implying a capability ordering it cannot
    source. Nothing in this repository benchmarks one tier against another, so
    `cost_tier: premium` states a price and nothing else. The roll removed the
    generation gap; it did not establish that `opus` leads `sonnet` on any
    axis, and this keeps the two claims from being confused.
    """
    binding = document.get("binding")
    assert isinstance(binding, dict), "model-pins.json has no `binding` section"
    label = binding.get("tier_alias_is_a_routing_label", "")
    assert "routing" in label.lower() and "capability" in label.lower(), (
        "model-pins.json does not state that a tier alias is a routing label "
        "rather than a capability guarantee (#441)"
    )


def test_claude_md_says_the_same_thing_where_the_orchestrator_reads_it() -> None:
    """The pin file is not on the dispatch path; `claude.md` is what gets read.

    Impossible after this: the routing-label caveat living only in a JSON file
    that no dispatch consults, while the wrapper the orchestrator actually
    loads leaves 'premium' to imply an ordering.
    """
    text = CLAUDE_WRAPPER.read_text(encoding="utf-8")
    assert "routing label, not a capability guarantee" in text, (
        "backends/claude.md does not say that a tier alias is a routing label "
        "rather than a capability guarantee (#441)"
    )


def test_no_tier_claims_to_be_more_capable_than_another(pins) -> None:
    """Guard against the claim creeping back in as prose.

    Impossible after this: a tier's own notes or decision asserting it is
    stronger, smarter, or more capable than another tier. #434 chose `opus`
    for the provers on a DECORRELATION argument, which needs the weights to
    differ and not to be ordered; the capability half was always an assumption
    and #441 is where it stops being restated as fact.
    """
    banned = ("more capable than", "stronger than", "smarter than",
              "outperforms", "better model than")
    for alias, tier in pins.items():
        blob = " ".join(
            [tier.get("notes", "")] + [str(v) for v in tier.get("decision", {}).values()]
        ).lower()
        for phrase in banned:
            assert phrase not in blob, (
                f"tier {alias!r} claims {phrase!r}. No benchmark in this "
                f"repository supports a capability ordering between tiers; "
                f"say what the tier is routed to and why, not that it wins."
            )


# ── HC0-F2 is recorded NOT DELIVERABLE IN V1, and must stay recorded ──────────
#
# Decided 2026-09-06 by the maintainer, closing #596. #519 measured that no
# component reads the pins on a dispatch path and #441 documented it, but the
# product still carried sentences telling a regulated customer the opposite.
# The decision is that V1 states the limit plainly rather than half-building a
# binding that would read as provenance while proving no more than the receipt
# already does.
#
# A claim is cheap to reintroduce and expensive to notice, so it is guarded
# where an auditor would actually meet it: the shipped AI-BOM, and the prose.

#: Phrasings that assert the pin is execution provenance. Absolute: a line that
#: DENIES the property must not use them either, because an exception list is a
#: place for the claim to hide. When a correction needs to name the old promise,
#: it is reworded instead, and #610's own first draft had to be.
_REPRODUCIBILITY_CLAIMS = (
    "reproduce behavior audit-to-audit",
    "reproduce behaviour audit-to-audit",
    "verifiable record of exactly which models",
    "shipped provenance",
    "provenance claim",
    "resolved to exact pinned ids",
    "resolved to pinned ids",
)


def _distribution_text() -> dict:
    """Every file a regulated reader can be pointed at, WALKED rather than listed.

    Two censuses have now been wrong in the same direction. #610 enumerated
    four hand-picked files and the claim was alive in four others. #614 walked
    `plugin-claude/`, `docs/brd/` and `docs/adrs/`, which is narrower than the
    docstring's own words: `docs/spq-lifecycle-design.md` carried a BANNED
    phrase the whole time and the guard passed, because the file was outside
    the walk.

    The boundary is now stated as a rule instead of a preference, so widening
    it is a decision rather than an accident. A file is in the surface when
    someone outside this test can be told to read it:

    * the three host plugin trees, because the marketplace build copies them;
    * every requirement, decision and design document under `docs/`, because
      that is where a reader goes to learn whether a control was delivered;
    * `benchmarks/`, because an evidence report is written to be quoted;
    * `.claude/skills/`, because a reviewer applies the checklist as written.

    Tests are excluded, and only tests: they quote the claims in order to
    refuse them. `test_the_surface_reaches_the_files_that_escaped_it` pins the
    files each past census missed, so this boundary cannot quietly shrink back.
    """
    import os

    repo = Path(__file__).resolve().parents[3]
    roots = (
        repo / "plugin-claude",
        repo / "plugin-cursor",
        repo / "plugin-codex",
        repo / "core",
        repo / "docs",
        repo / "benchmarks",
        repo / ".claude" / "skills",
        repo / "CLAUDE.md",
    )
    skip = {"tests", "node_modules", "dist", "__pycache__", ".venv"}
    out: dict[str, str] = {}

    def take(path: Path) -> None:
        if path.suffix not in (".md", ".py", ".json") and path.name != "synaptory":
            return
        try:
            out[str(path.relative_to(repo))] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return

    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            take(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for name in filenames:
                take(Path(dirpath) / name)
    return out


def _flatten(text: str) -> str:
    """A claim wrapped across two lines is still the claim.

    `model_pin_check.py` carried "reproduce behaviour\naudit-to-audit", which a
    literal substring test walks straight past. Normalising whitespace is what
    turned a four-site census into a five-site one.
    """
    return re.sub(r"\s+", " ", text).replace("``", "`").lower()


#: A line that opens a markdown block: list item, table row, heading, quote,
#: fence. Prose wraps across lines and must be rejoined before it can be read
#: as a sentence; block lines are separate statements and joining them would
#: manufacture sentences nobody wrote.
_BLOCK_START = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\||#{1,6}\s|>|```)")

#: Naming the pin file, in any of the spellings the tree uses.
_PIN_REFERENCE = re.compile(r"model[-_]pins(\.json)?|model_pin_check", re.I)

#: Vocabulary that asserts something RESOLVES to something else. Paired with a
#: pin reference in the same sentence, it says the pin turns an alias into an
#: exact model ID, which is the one thing #519 measured that it does not do.
_RESOLUTION_VOCABULARY = re.compile(
    r"resolv|resolution|pinned to|pinned id|maps? to|expands? to|substitut", re.I
)


def _sentences(text: str):
    """Sentences, with wrapped prose rejoined first.

    The wrapping matters twice over. `docs/spq-lifecycle-design.md` reads
    "resolved to exact pinned IDs through\n`backends/model-pins.json`", so a
    per-line reader sees a resolution verb on one line and a filename on the
    next and finds nothing on either.
    """
    lines = text.split("\n")
    joined = []
    for i, line in enumerate(lines):
        joined.append(line)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        wrapped = (
            line.strip()
            and nxt.strip()
            and not _BLOCK_START.match(line)
            and not _BLOCK_START.match(nxt)
        )
        joined.append(" " if wrapped else "\n")
    flat = re.sub(r"[ \t]+", " ", "".join(joined))
    for line in flat.split("\n"):
        for sentence in re.split(r"(?<=[.!?;:])\s+", line):
            sentence = sentence.strip()
            if sentence:
                yield sentence


def _dispatch_path_claims() -> list:
    """Every sentence in the surface that puts the pin on the dispatch path."""
    return sorted(
        "%s: %s" % (name, sentence)
        for name, text in _distribution_text().items()
        for sentence in _sentences(text)
        if _PIN_REFERENCE.search(sentence) and _RESOLUTION_VOCABULARY.search(sentence)
    )


def test_no_product_facing_file_claims_the_pin_is_execution_provenance() -> None:
    """Asserts impossible: the delivered-control claim surviving anywhere shipped.

    Failing this does not mean a document is badly worded. It means a regulated
    reader can still be told the pin reproduces behaviour, which HC0-F2's
    decision (#596) says it does not.
    """
    offenders = sorted(
        "%s: %r" % (name, claim)
        for name, text in _distribution_text().items()
        for claim in _REPRODUCIBILITY_CLAIMS
        if claim in _flatten(text)
    )
    assert offenders == [], (
        "product-facing files still claim the pin is execution provenance:\n  %s\n"
        "HC0-F2 is NOT DELIVERABLE IN V1 (#596): the pin records intent and is "
        "not evidence of the weights that ran." % "\n  ".join(offenders)
    )


def test_no_product_facing_file_puts_the_pin_on_the_dispatch_path() -> None:
    """Asserts impossible: the claim returning in wording nobody listed.

    `_REPRODUCIBILITY_CLAIMS` is a list of promises that were actually made, so
    it can only ever catch a promise someone already wrote down. The #592
    re-review made that concrete: `backend-dispatch.md` said "tier to model-ID
    resolution via model-pins.json" forty lines above the paragraph denying it,
    inside a file the guard was already walking and reading in full. Every
    focused test passed.

    This test asserts the MEANING instead of the wording. A sentence that names
    the pin file and also says something resolves is claiming the pin turns an
    alias into an exact model ID on the dispatch path. #519 measured that it
    does not: `Agent()` accepts only the alias, and nothing in `core/`, the
    hooks, or the CLI opens the file.

    There is deliberately no exception list. A denial has no need of resolution
    vocabulary, and an exception list is where the claim would go to hide, so a
    sentence that trips this gets reworded rather than excused.
    """
    claims = _dispatch_path_claims()
    assert claims == [], (
        "these sentences put `model-pins.json` on the dispatch path:\n  %s\n"
        "Nothing reads that file when a role is dispatched (#519). Say that the "
        "alias is what is passed and the pin records the intent behind it."
        % "\n  ".join(claims)
    )


def test_the_surface_reaches_the_files_that_escaped_it() -> None:
    """Asserts impossible: the census narrowing back to where the claim was known.

    Each of these carried the claim while a guard that was supposed to cover
    them reported clean, because the file was outside that guard's walk. They
    are pinned by name so shrinking the boundary fails here loudly rather than
    in the next review.
    """
    surface = _distribution_text()
    escaped = {
        "docs/spq-lifecycle-design.md": "#592 re-review: outside #614's walk",
        ".claude/skills/synaptory-review/references/synaptory-review-checklist.md":
            "the checklist a reviewer applies, outside every earlier walk",
        "plugin-claude/skills/_shared/backends/backend-dispatch.md":
            "#592 re-review: inside the walk, invisible to the phrase list",
        "plugin-claude/skills/synaptory/modes/spq.md": "#592 re-review addendum",
        "plugin-claude/skills/_shared/backends/model_pin_check.py": "#610",
        "docs/brd/brd-plugin.md": "#596's decision record",
    }
    missing = sorted(
        "%s (%s)" % (name, why) for name, why in escaped.items() if name not in surface
    )
    assert missing == [], (
        "the product surface no longer reaches files that have already carried "
        "this claim once:\n  %s" % "\n  ".join(missing)
    )


def test_the_brd_records_the_decision_and_what_to_tell_a_customer() -> None:
    """A decision nobody can find is a decision nobody applies.

    The requirement is where a reader goes to learn whether a control was
    delivered, so the ruling and the sentence to say out loud both live there.
    """
    text = _distribution_text()["docs/brd/brd-plugin.md"]
    assert "NOT DELIVERABLE IN V1" in text
    assert "What a regulated customer must be told" in text
    assert "2026-09-06" in text


def test_the_shipped_ai_bom_carries_its_own_limit() -> None:
    """Asserts impossible: shipping the pins with the caveat only in a comment.

    An auditor reads `build-metadata.json`. The build script's comment is not
    in that file, so the caveat is emitted INTO the object.
    """
    script = _distribution_text()["plugin-claude/synaptory"]
    marker = script.split("ai_bom = {", 1)
    assert len(marker) == 2, "the AI-BOM is no longer built as a literal"
    block = marker[1].split("\nmeta = {", 1)[0]
    for key in (
        "'binds_dispatch': False",
        "'hc0_f2_status': 'not-deliverable-in-v1'",
        "'model_identity_source': 'receipt-authored'",
        "what_this_is_not",
    ):
        assert key in block, (
            "the emitted AI-BOM no longer states %r, so a reader of "
            "build-metadata.json cannot tell intent from provenance" % key
        )
