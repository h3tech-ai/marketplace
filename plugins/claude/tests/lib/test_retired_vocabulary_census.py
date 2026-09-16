"""Layer 1 — the class-closing guard for the retired SPQ vocabulary (#640, #641).

`spq-methodology.md` §10.2 retires six words from the method. `SPD-194` retires
three of them: **Workstream**, **Coordination Cycle**, and the folk term **Cycle
of Cycles**. An earlier revision retired **Slice**, **Increment** and **the
Implementation phase**. Removing `SPD-194`'s three alone touches 262
tracked files, which is far too many to hold in a reviewer's head, and the
failure mode of a sweep that size is not that a file is missed loudly — it is
that a file is missed *quietly*, or that a file quietly acquires the vocabulary
again while the sweep is still in progress.

So this is a census, not a cleanliness check. It deliberately does **not**
assert that the tree is free of the retired words: for most of the epic it will
not be, and a test that fails for the whole epic is a test everyone learns to
ignore. What it asserts is that **every file carrying the vocabulary has been
classified**, and that **no classification has rotted**.

Modelled on `test_board_reader_census.py`, for the same reason that one exists:
nothing forces a new file to consult a shared accessor, and nothing forces a new
file to avoid a word.

    census      Every tracked file carrying a retired noun must appear in
                `fixtures/retired_vocabulary_census.json` with a category. A
                file that appears in the tree and not in the census fails, so a
                new occurrence cannot arrive unnoticed.

    no ghosts   A census entry whose file has been deleted fails, and so does an
                entry whose file no longer carries the vocabulary. Both mean the
                same thing — the entry has done its job and must go — and both
                are the `declared_gaps` discipline applied here: a declaration
                that cannot rot in either direction.

    patterns    The *set* of nouns a file carries is asserted; the *counts* are
                recorded and not asserted. A count changes on every edit, so
                asserting it would make this test churn without catching
                anything. A file that stops mentioning Coordination Cycle
                entirely is a real state change and is caught. The one
                exception is `other-sense`, where the exact count *is* asserted
                because the row is an exemption and an exemption may not grow
                unread.

    word list   The words come from a vendored copy of §10.2, not from a
                literal here, and every word in it must have a pattern. See
                "The scope of this census" below: this is the half of #657 that
                stops a later retirement from being missed by a guard nobody
                updated.

    coordinated One kind of occurrence cannot be discharged file by file: the
                word names a **retained identifier** whose removal is one
                versioned contract change. Those rows carry
                `category: "coordinated"` and name a group declared once in the
                census's `coordinated_changes` block, and **the group carries
                the reason, not the row**. So the obligation reads as one
                obligation at every row rather than as N independent
                judgements, and a row that must not be discharged alone stops
                instructing its own discharge.

The scope of this census: all six words, and where the list comes from (#657).

    This census tracks all six. It used to track `SPD-194`'s three only, and
    the other three went untracked by anything: a downstream prototype rendered
    *Increment* in screen copy and survived 216 audit combinations, because that
    surface's ban list held `SPD-194`'s three plus `slice`, and neither
    `increment` nor "the Implementation phase". An arbitrary partition reports
    clean over the words it happens to hold, says nothing about the ones it does
    not, and **reads identically to a complete one**.

    So the word list is no longer a literal in this file. It is derived from
    `fixtures/retired_vocabulary_table.json`, a **vendored** copy of §10.2 that
    records where it came from and carries each word's retirement revision, so
    the scope is data rather than a decision nobody wrote down. Every word in
    that table must have a pattern here and every pattern must name a word in
    it: a word the spec retires later cannot be silently absent, because
    vendoring it fails this suite until someone writes its pattern.

    §10.2 itself is not in this repository -- it lives in the sibling repo
    `h3tech-ai/synaptory-spec` at `spec/spq-methodology.md`, which is why
    ADR-035 cites it by absolute URL. A Layer 1 test reading it would pass on a
    workstation that happens to have that clone beside this one and fail in CI,
    and a Layer 1 test that *skipped* when the clone is absent would be this
    issue's own defect wearing a different hat: it would read exactly like one
    that had read the spec. So the split is:

      * **this suite** asserts the vendored table's integrity -- its shape, that
        every row's revision is one this file understands, and that its recorded
        digest matches its own content -- which needs no sibling checkout and can
        never be green on a malformed or hand-edited table;
      * **`scripts/check-retired-vocabulary-table`** re-derives the table from a
        spec checkout and fails with a row-by-row diff on drift. It is a script
        a person runs, not a test and deliberately not a CI job (see below), and when
        it cannot read the spec it **fails saying so** rather than reporting
        success over a source it never opened.

    The residual limit, stated rather than implied: drift between the spec and
    the vendored copy is caught only when someone runs that script with both
    repositories present. That is weaker than reading §10.2 directly and
    stronger than an undeclared copy.

    **Running it by hand is the decision, not a gap waiting on automation**
    (#684, closed on this reasoning). Automating it means a job in one
    repository reading the other, and both are private, so it means a standing
    cross-repository credential. That grant is permanent; the drift it guards
    against is a §10.2 revision, which is rare once implementation is under way,
    and V1 has a finite life with its successor being built elsewhere. A
    standing authority grant is the wrong price for a rare event in a codebase
    with an end date.

    **So the trigger is a person, and it is written down here because a trigger
    nobody can find is the same as no trigger.** When `spq-methodology.md` §10.2
    changes -- a word retired, a mapping altered, a revision shipped -- run, from
    a checkout with `synaptory-spec` beside it:

        scripts/check-retired-vocabulary-table            # compare, exit 1 on drift
        scripts/check-retired-vocabulary-table --write    # re-derive, then commit

    Then classify whatever the widened word set surfaces. The script exits 2
    rather than 0 when it cannot read the spec, so a run that proves nothing
    cannot be mistaken for a run that proved something.

The delivery-sense heuristic for `Slice` and `Increment`, and what it misses:

    `slice` and `increment` are ordinary English. Measured 2026-09-09 on
    `origin/dev`, case-insensitive `(?<![.\\w])slices?\\b` hits 46 tracked files
    and `(?<![.\\w])increments?\\b` hits 48, almost all of them "increment the
    retry counter", "a lane slice", "bucket_days slice". Guarding those is
    noise, and a guard that demands boilerplate is the failure this census is
    written against.

    So both patterns are **capitalised**: `(?<![.\\w])Slices?\\b` hits 7 files
    and `(?<![.\\w])Increments?\\b` hits 11. Capitalisation is the discriminator
    because the retired word is a proper noun naming the delivery unit, while
    the ordinary senses are a common noun and a verb.

    **The limit, spelled out: a lowercase use in the delivery sense is
    missed.** `the slice is closed` reads as delivery vocabulary to a human and
    is invisible here. That is the price of a pattern with a usable
    signal-to-noise ratio, and the point of writing it down is that the problem
    was never that the scope is narrow -- it was that the narrowness was
    unwritten.

    The heuristic also has false positives, and they are recorded as data rather
    than absorbed into an ever-growing regex of lookarounds: see the
    `other-sense` category. The `(?<![.\\w])` prefix is not part of the
    heuristic but a hard requirement -- `\\bslice\\b` matches `.slice(`, the
    array method, on every JavaScript file in the tree.

    "The Implementation phase" needs no such heuristic. It is a multi-word
    phrase, `\\bimplementation[\\s_-]+phases?\\b` case-insensitively hits 2
    files, and both are real.

The categories, and what each one obliges:

    remove              The file is deleted by this epic. Its entry goes with it.
    rewrite             The file survives; the vocabulary does not.
    coordinated         The word here is a **retained** identifier, kept
                        deliberately, because removing it is one coordinated
                        versioned contract change recorded in
                        `coordinated_changes`. The row is not discharged on its
                        own, and the category says so instead of instructing a
                        change that would break something. Its reason lives on
                        the group. Note what the per-file model still cannot
                        express: a `coordinated` file may also carry ordinary
                        occurrences a sweep should remove. The category records
                        the file's binding constraint, which is that it cannot
                        leave this census until the coordinated change ships.
    regenerate          A composed copy under `plugin-cursor/`. Never hand-edited:
                        the change is made in `core/` or `plugin-claude/` and
                        arrives here on the next compose.
    supersede / amend   An ADR whose record stays and whose status or vocabulary
                        section changes.
    history             A report, finding, proposal or superseded design. The
                        vocabulary is correct there: it describes what was true.
    other-sense         The pattern matched and the retired word is **not
                        there**: the match is a different sense of the same
                        string. Two shapes, both real in this tree -- an ordinary
                        English verb (`Increment the retry counter`), and a
                        different domain's term of art that ADR-028 explicitly
                        retains (Scrum's own Increment artifact, the platform
                        delivery ladder's increments I-0…I-6). This is the
                        declared cost of the capitalisation heuristic, and it is
                        held as data with a reason per row instead of being
                        pushed into the regex, because every lookaround added to
                        dodge one false positive narrows the pattern in a way
                        nobody can read afterwards. These rows are the only ones
                        whose `counts` are asserted **exactly** -- an exemption
                        that grows without being re-read is an exemption for
                        occurrences nobody looked at.
    applied-migration   An Alembic revision that has run. Never rewritten; a
                        change is a new forward migration.
    retirement-document The document that retires the vocabulary, which
                        necessarily names it.
    guard               This census and its test. A guard that searches for a
                        word has to contain it, so the recursion is closed by
                        classifying it rather than by excluding it — an
                        exclusion would also hide a real occurrence added here
                        later.

What this guard cannot do, stated plainly:

  * It cannot tell a live use from a described one inside a single file. A
    `history` file that grows new *behaviour* referencing a workstream looks
    identical to one that grows a new sentence about the past.
  * It cannot see a lowercase delivery-sense use of `slice` or `increment`.
    That is the stated heuristic above, and the honest reading of it is that
    those two words are guarded in their capitalised form and not otherwise.
  * It cannot tell whether the vendored word list still matches §10.2. Only
    `scripts/check-retired-vocabulary-table` can, run with both repositories
    present. What the suite guarantees is that the copy is well-formed and
    self-consistent, which is a different and smaller claim.
  * `other-sense` is a per-file exemption and the tree has no per-occurrence
    one. Asserting the exact count is the mitigation: a new match in an
    exempted file fails and has to be read. A match that *replaces* another in
    the same file, leaving the count unchanged, would not be.
  * It reads tracked files only, so an untracked scratch file is invisible —
    which is correct, but means a sweep verified only by this test has not been
    verified against a dirty tree.
  * `regenerate` is asserted by location, not by running the composer. A file
    under `plugin-cursor/` that is genuinely authored and carries the vocabulary
    would be miscategorised here; such files are classified by their real
    obligation instead, and this note is the reason that distinction is worth
    keeping. (This sentence used to name a count, and the count was already
    wrong before `coordinated` moved one of those files; a count in a docstring
    is exactly the kind of description that rots away from the mechanism it
    describes.)
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

#: The vendored copy of §10.2 that supplies the word list, and the tool that
#: re-derives it. The tool is imported rather than re-implemented so the id
#: slug and the table digest have exactly one definition: if the suite computed
#: a digest its own way, the two could agree with each other and with nothing.
#: It lives in `scripts/` with no extension, following `scripts/h3t-github`, so
#: it is loaded by path.
TABLE_PATH = "plugin-claude/tests/fixtures/retired_vocabulary_table.json"
DRIFT_CHECK_PATH = "scripts/check-retired-vocabulary-table"


def _load_drift_check(repo_root: Path):
    loader = importlib.machinery.SourceFileLoader(
        "check_retired_vocabulary_table", str(repo_root / DRIFT_CHECK_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


#: The retired words, with the spellings each actually takes in the tree, keyed
#: by the id the vendored table derives from the spec's `You may hear` cell.
#: The keys are asserted against that table by
#: `test_every_vendored_word_has_a_pattern`, so this map cannot drift from the
#: word list and a newly vendored word cannot arrive without a pattern.
#:
#: `coordination[\s_-]+cycle` is one pattern rather than three because the
#: snake, camel and prose spellings are the same noun and splitting them would
#: only produce three ways to be right about the same file.
#: Whitespace inside a multi-word noun is `\s+`, not a literal space, and that
#: is load-bearing rather than tidy. The first version of this file matched
#: `cycle of cycles` literally, and ADR-035 -- the record that retires the term
#: -- wrapped it as "**Cycle\nof Cycles**" at the 100-column margin. The guard
#: reported the ADR as carrying two nouns instead of three, and would have gone
#: on doing so for every prose occurrence in `docs/`, which is where this term
#: almost only appears. A line-sensitive pattern under-counts exactly the files
#: made of wrapped prose.
#:
#: `slice` and `increment` are case-SENSITIVE, and that is the delivery-sense
#: heuristic the module docstring states and bounds: lowercase they are ordinary
#: English, 46 and 48 files of it. `(?<![.\w])` is separate from the heuristic
#: and not optional: `\bslice\b` matches `.slice(` on every JavaScript file.
PATTERNS = {
    "workstream": re.compile(r"workstream", re.IGNORECASE),
    "coordination_cycle": re.compile(r"coordination[\s_-]+cycle", re.IGNORECASE),
    "cycle_of_cycles": re.compile(r"cycle\s+of\s+cycles", re.IGNORECASE),
    "slice": re.compile(r"(?<![.\w])Slices?\b"),
    "increment": re.compile(r"(?<![.\w])Increments?\b"),
    "the_implementation_phase": re.compile(
        r"\bimplementation[\s_-]+phases?\b", re.IGNORECASE
    ),
}

#: The retirement revisions this file understands, as the vendored table spells
#: them. A row carrying anything else fails: the census scopes itself from that
#: column, so an unrecognised value would silently mean "no revision".
REVISIONS = frozenset({"earlier-revision", "this-revision"})

CATEGORIES = frozenset({
    "remove",
    "rewrite",
    "coordinated",
    "regenerate",
    "supersede",
    "amend",
    "history",
    "other-sense",
    "applied-migration",
    "retirement-document",
    "guard",
})

#: The one category whose `counts` values are asserted exactly, not just their
#: keys. `other-sense` says "the pattern matched and the word is not here",
#: which is a per-file exemption; an exemption that can absorb new occurrences
#: without anyone re-reading them is how a real use hides inside a file someone
#: already dismissed. So these rows churn when the file changes, on purpose:
#: the churn is the prompt to look again.
COUNTS_ASSERTED_EXACTLY = frozenset({"other-sense"})

#: Categories whose entries must explain themselves, because the category is a
#: judgement rather than a location: someone reading the census later needs to
#: know why this ADR is amended and that one is only history.
#:
#: `coordinated` is a judgement too, and is deliberately NOT here, because its
#: reason is written once on the group it names rather than once per row. The
#: rule is unchanged, only the place the reason lives: a `coordinated` row
#: without a reachable, reasoned group fails
#: `test_the_coordinated_set_is_one_declaration`.
#:
#: `rewrite` is also a judgement and is also not here, and that gap is real
#: (#657). It is left open on purpose: `rewrite` is the largest category, most
#: of its rows are obvious, and a guard that demands a boilerplate sentence per
#: row is one people learn to satisfy without reading. What #657 actually found
#: was one shape of unreadable `rewrite` -- a row that cannot be discharged at
#: all -- and that shape has its own category and its own guard now.
#:
#: `other-sense` IS here. It is the sharpest judgement in the set -- "the
#: pattern matched and the word is not there" -- and it is the one an
#: unattentive sweeper would reach for to make a row go away, so it may not be
#: asserted without saying which sense the match is.
NEEDS_REASON = frozenset({
    "supersede", "amend", "retirement-document", "guard", "other-sense",
})

CENSUS_PATH = "plugin-claude/tests/fixtures/retired_vocabulary_census.json"

#: The census document's two top-level blocks. `files` is the row map every
#: other test reads; `coordinated_changes` is where a coordinated obligation is
#: declared once.
FILES_KEY = "files"
COORDINATED_KEY = "coordinated_changes"

#: The row field naming the group a `coordinated` row belongs to.
ROW_GROUP_FIELD = "coordinated_change"

#: This census is a git-tracked-file census. Binary and vendored trees are
#: excluded because a match inside them is not a sentence anyone wrote.
SKIP_PREFIXES = ("web/node_modules/", "web/dist/")
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pyc")


def _tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def _scan(repo_root: Path) -> dict[str, dict[str, int]]:
    """Map every carrying file -> {retired word id: occurrences}.

    Counts as well as the set, because `other-sense` asserts them exactly. Every
    other category compares key sets only, which is what it always did.
    """
    found: dict[str, dict[str, int]] = {}
    for rel in _tracked_files(repo_root):
        if rel.startswith(SKIP_PREFIXES) or rel.endswith(SKIP_SUFFIXES):
            continue
        path = repo_root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hit = {
            name: len(pat.findall(text))
            for name, pat in PATTERNS.items()
            if pat.search(text)
        }
        if hit:
            found[rel] = hit
    return found


@pytest.fixture(scope="module")
def census_document(repo_root: Path) -> dict:
    """The whole census file: `coordinated_changes` plus `files`."""
    return json.loads((repo_root / CENSUS_PATH).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def census(census_document: dict) -> dict:
    """The row map only, so every per-file test reads exactly what it did."""
    return census_document[FILES_KEY]


@pytest.fixture(scope="module")
def coordinated_changes(census_document: dict) -> dict:
    return census_document.get(COORDINATED_KEY) or {}


@pytest.fixture(scope="module")
def scanned(repo_root: Path) -> dict[str, dict[str, int]]:
    return _scan(repo_root)


@pytest.fixture(scope="module")
def drift_check(repo_root: Path):
    """`scripts/check-retired-vocabulary-table`, loaded by path.

    The suite does not run its spec comparison -- that needs the sibling
    checkout and is the script's job. It borrows the id slug and the digest so
    there is one definition of each.
    """
    return _load_drift_check(repo_root)


@pytest.fixture(scope="module")
def vocabulary_table(repo_root: Path) -> dict:
    """The vendored copy of §10.2 that supplies the word list."""
    return json.loads((repo_root / TABLE_PATH).read_text(encoding="utf-8"))


def test_every_carrying_file_is_classified(census, scanned, vocabulary_table):
    """A file carrying a retired word and absent from the census fails.

    This is the half that catches an occurrence nobody thought about — a new
    module, a new prompt, or a rewrite that reintroduced the word.
    """
    unregistered = sorted(set(scanned) - set(census))
    retired = "\n".join(
        f"  {w['heard']} -> read {w['read_instead']}  ({w['retired_in']})"
        for w in vocabulary_table.get("words") or []
    )
    assert not unregistered, (
        "These tracked files carry a retired SPQ word and are not classified in\n"
        f"{CENSUS_PATH}:\n\n"
        + "\n".join(f"  {p}  ({', '.join(sorted(scanned[p]))})" for p in unregistered)
        + "\n\nThe retired words, from " + TABLE_PATH + ":\n"
        + retired
        + "\n\nIf this file is new work, it should not use them at all. If it is a\n"
        "record of what was true, classify it `history`. If the pattern matched a\n"
        "different sense of the word -- an English verb, or a term of art ADR-028\n"
        "retains -- classify it `other-sense` and say which. Categories and what\n"
        "each obliges are in this file's docstring."
    )


def test_census_carries_no_ghosts(census, scanned, repo_root: Path):
    """An entry whose file is gone, or is now clean, must be deleted.

    Both directions matter. A deleted file leaves an entry that makes the sweep
    look bigger than it is; a cleaned file leaves an entry that makes it look
    smaller than it is. The epic is judged on this census, so neither is
    harmless.
    """
    gone = sorted(p for p in census if not (repo_root / p).is_file())
    cleaned = sorted(set(census) - set(scanned) - set(gone))
    assert not gone, (
        "These census entries name files that no longer exist. Delete the\n"
        "entries — a `remove` entry retires together with its file:\n\n"
        + "\n".join(f"  {p}" for p in gone)
    )
    assert not cleaned, (
        "These files no longer carry any retired noun. Delete their census\n"
        "entries: a classification that outlives its subject is the same defect\n"
        "as an unclassified file, pointing the other way:\n\n"
        + "\n".join(f"  {p}  (was: {census[p].get('category')})" for p in cleaned)
    )


def test_every_entry_declares_a_known_category(census):
    bad = sorted(
        f"{p} -> {entry.get('category')!r}"
        for p, entry in census.items()
        if entry.get("category") not in CATEGORIES
    )
    assert not bad, (
        "Unknown category. The closed set is: "
        + ", ".join(sorted(CATEGORIES))
        + "\n\n"
        + "\n".join(f"  {b}" for b in bad)
    )


def test_judgement_categories_explain_themselves(census):
    """Every category in `NEEDS_REASON` must carry a reason.

    Which categories those are is written once, on `NEEDS_REASON`, and this
    docstring does not restate them. It used to, and it was wrong: it named
    three and omitted `guard` for the life of the file (#657). The test body
    always iterated the frozenset, so the behaviour was right and only the
    description was wrong, which is worse than it sounds here -- a reader
    checking the rule against its description concluded `guard` needed no
    reason, inside the one file whose whole subject is that judgements get
    written down. The first fix restated the list correctly and added a count,
    which lasted exactly until `other-sense` joined the set; a description that
    enumerates a constant is a second copy of it, and the second copy is the one
    that rots. So the enumeration lives in the code, in one place, and is read
    there.

    A location-derived category speaks for itself. These are decisions, and a
    decision without its reason is the thing this whole epic is cleaning up
    after. `coordinated` is a decision too and satisfies the same rule one
    level up, on its group.
    """
    missing = sorted(
        p
        for p, entry in census.items()
        if entry.get("category") in NEEDS_REASON and not str(entry.get("reason") or "").strip()
    )
    assert not missing, (
        "These entries carry a judgement category and no `reason`:\n\n"
        + "\n".join(f"  {p}  ({census[p]['category']})" for p in missing)
    )


def test_recorded_nouns_match_what_the_file_carries(census, scanned):
    """The set of words is asserted; the counts are not.

    A count changes whenever anyone edits a line, so asserting it would make
    this test fail for reasons that are not findings. The set changing means a
    word has actually left a file -- which is progress worth noticing, and an
    entry worth revisiting.
    """
    drift = []
    for path, counts in scanned.items():
        entry = census.get(path)
        if entry is None:
            continue  # reported by test_every_carrying_file_is_classified
        recorded = set(entry.get("counts") or {})
        if recorded != set(counts):
            drift.append(
                f"  {path}\n"
                f"      census: {', '.join(sorted(recorded)) or '(none)'}\n"
                f"      actual: {', '.join(sorted(counts)) or '(none)'}"
            )
    assert not drift, (
        "The words these files carry no longer match their census entry.\n"
        "Update `counts`, and re-read the category while you are there — a\n"
        "word leaving a file often means its classification has changed too:\n\n"
        + "\n".join(drift)
    )


def test_exempted_rows_record_their_occurrences_exactly(census, scanned):
    """`other-sense` rows assert counts, not just the set of words.

    Every other category records a count and does not assert it, for the reason
    the sibling test gives. `other-sense` is different in kind: it is the row
    that says the retired word is not in this file at all, so the file is
    excused from the sweep. If a new occurrence could land there without
    failing, the exemption would cover an occurrence nobody read -- which is a
    smaller version of the whole finding this census exists to close.

    So these rows churn on edits to their files. That is the cost, and it is
    paid by a handful of rows rather than by all 170.
    """
    drift = []
    for path, entry in sorted(census.items()):
        if entry.get("category") not in COUNTS_ASSERTED_EXACTLY:
            continue
        actual = scanned.get(path)
        if actual is None:
            continue  # reported by test_census_carries_no_ghosts
        recorded = {k: v for k, v in (entry.get("counts") or {}).items()}
        if recorded != actual:
            drift.append(
                f"  {path}  ({entry.get('category')})\n"
                f"      census: {recorded}\n"
                f"      actual: {actual}"
            )
    assert not drift, (
        "These rows are exempt from the sweep because the match is a different\n"
        "sense of the word, and the number of matches has changed. Read the new\n"
        "occurrence before updating the count: an exemption is only as good as\n"
        "the last time someone looked at what it covers.\n\n"
        + "\n".join(drift)
    )


# ---------------------------------------------------------------------------
# The vendored word list: a copy, with provenance and a way to catch drift
# ---------------------------------------------------------------------------
#
# The census tracked three of §10.2's six words and nothing tracked the other
# three (#657). What made that hard to see is that a guard scoped to a subset
# reads exactly like a complete one. The fix is to stop keeping the list here:
# it is vendored from the spec, and the vendored copy carries its own
# provenance so nobody has to guess whether it is current.
#
# The tests below are the half that needs no sibling checkout. They cannot tell
# whether the copy matches today's spec -- only
# `scripts/check-retired-vocabulary-table` can, and it fails loudly rather than
# skipping when the spec is absent. What they can do, and do, is make the copy
# impossible to malform or to hand-edit quietly.


def test_the_vendored_table_has_the_shape_the_census_reads(vocabulary_table):
    """Shape first: without this every test below fails obscurely instead."""
    provenance = vocabulary_table.get("provenance") or {}
    missing = sorted(
        field
        for field in (
            "source_repo",
            "source_path",
            "source_section",
            "spec_revision",
            "source_table_sha256",
            "table_sha256",
            "derived_at",
            "derived_by",
        )
        if not str(provenance.get(field) or "").strip()
    )
    assert not missing, (
        "The vendored table's `provenance` is incomplete. A copy without\n"
        "provenance is the thing this file replaced: nobody can tell where it\n"
        f"came from or when. Missing: {', '.join(missing)}\n\n"
        f"Re-derive with `{DRIFT_CHECK_PATH} --write`."
    )

    words = vocabulary_table.get("words")
    assert isinstance(words, list) and words, (
        f"{TABLE_PATH} has no `words` list. An empty word list makes every\n"
        "census test vacuous rather than failing, so it fails here instead."
    )

    malformed = []
    for index, word in enumerate(words):
        if not isinstance(word, dict):
            malformed.append(f"  [{index}] is not an object")
            continue
        for field in ("id", "heard", "read_instead", "retired_in"):
            if not str(word.get(field) or "").strip():
                malformed.append(f"  [{index}] {word.get('heard', '?')!r} has no {field}")
    assert not malformed, (
        "These vendored rows are malformed:\n\n" + "\n".join(malformed)
    )

    ids = [w["id"] for w in words]
    assert len(ids) == len(set(ids)), (
        f"Duplicate ids in {TABLE_PATH}: two rows for one word means one of them\n"
        "is silently unreachable from `PATTERNS`."
    )


def test_vendored_ids_are_derived_from_the_words_they_name(vocabulary_table, drift_check):
    """The id is a slug of `heard`, computed by the same function as the tool.

    An id chosen by hand is an id that can be made to point anywhere, and the
    census keys `counts` by it. Deriving it means the census's own keys are a
    function of the spec's `You may hear` column.
    """
    wrong = sorted(
        f"  {w['heard']!r}: id {w['id']!r}, expected {drift_check.slug(w['heard'])!r}"
        for w in vocabulary_table["words"]
        if w["id"] != drift_check.slug(w["heard"])
    )
    assert not wrong, (
        "These vendored ids are not slugs of the word they name:\n\n"
        + "\n".join(wrong)
        + f"\n\nRe-derive with `{DRIFT_CHECK_PATH} --write` rather than editing them."
    )


def test_every_vendored_row_carries_a_revision_the_census_understands(vocabulary_table):
    """The census scopes itself from the `Retired` column, so it must read it.

    §10.2 marks each row `Earlier revision` or `**This revision**`. An
    unrecognised value would mean the scope is derived from something this file
    cannot interpret, which is the unwritten-scope defect again with an extra
    step.
    """
    unknown = sorted(
        f"  {w['heard']} -> {w['retired_in']!r}"
        for w in vocabulary_table["words"]
        if w["retired_in"] not in REVISIONS
    )
    assert not unknown, (
        "These vendored rows carry a retirement revision this census does not\n"
        f"understand. Known: {', '.join(sorted(REVISIONS))}\n\n"
        + "\n".join(unknown)
        + "\n\nIf the spec has shipped a new revision, add it to `REVISIONS` and\n"
        "re-read what the rows now say: the column is relative to the spec's own\n"
        "current revision, so a new one demotes the previous `this-revision`\n"
        "rows to `earlier-revision`."
    )

    described = vocabulary_table.get("revisions") or {}
    undescribed = sorted(
        {w["retired_in"] for w in vocabulary_table["words"]}
        - {k for k, v in described.items() if str(v or "").strip()}
    )
    assert not undescribed, (
        "These revisions are used by a row and described nowhere. `this revision`\n"
        "is relative to the spec and names itself nowhere, so the vendored table's\n"
        "`revisions` block is where it gets a name:\n\n"
        + "\n".join(f"  {r}" for r in undescribed)
    )


def test_the_recorded_digest_matches_the_vendored_table(vocabulary_table, drift_check):
    """A hand-edited table fails here, with no spec checkout needed.

    This is the assertion that makes the vendored copy an artifact rather than a
    literal someone can nudge. The digest is over the `words` list only, in the
    same canonical form the re-derive tool writes, so it moves when the mapping
    moves and not when the file is reformatted or a `revisions` note is
    reworded.
    """
    recorded = (vocabulary_table.get("provenance") or {}).get("table_sha256")
    actual = drift_check.table_digest(vocabulary_table["words"])
    assert recorded == actual, (
        "The vendored table's rows do not hash to the digest recorded beside\n"
        "them, so the table was edited by hand:\n"
        f"  recorded: {recorded}\n"
        f"  actual:   {actual}\n\n"
        f"Re-derive from the spec with `{DRIFT_CHECK_PATH} --write`. Do not just\n"
        "paste the actual digest in: the point of the field is that the rows came\n"
        "from §10.2, and a digest updated by hand records only that someone typed."
    )


def test_every_vendored_word_has_a_pattern(vocabulary_table):
    """The word list and the patterns are one set, in both directions.

    This is the mechanism that stops the next retirement from being missed. A
    word vendored from §10.2 with no pattern here fails, so widening the word
    list cannot be done without saying how to find the word; and a pattern for a
    word the table does not carry fails too, so the pattern map cannot grow a
    word that §10.2 never retired.
    """
    vendored = {w["id"] for w in vocabulary_table["words"]}
    without_pattern = sorted(vendored - set(PATTERNS))
    without_word = sorted(set(PATTERNS) - vendored)
    heard = {w["id"]: w["heard"] for w in vocabulary_table["words"]}

    assert not without_pattern, (
        "These words are vendored from §10.2 and this census cannot find them:\n\n"
        + "\n".join(f"  {heard[i]}  (id {i})" for i in without_pattern)
        + "\n\nAdd a pattern to `PATTERNS`, then classify whatever it surfaces.\n"
        "This failure is the point of the vendored table: a word the spec retires\n"
        "cannot be tracked by nothing, because vendoring it fails until it is."
    )
    assert not without_word, (
        "These patterns name words the vendored §10.2 table does not carry:\n\n"
        + "\n".join(f"  {i}" for i in without_word)
        + f"\n\nEither the spec un-retired the word -- re-derive with\n"
        f"`{DRIFT_CHECK_PATH} --write` -- or the pattern is guarding a word this\n"
        "method never retired, which belongs in its own guard and not in this one."
    )


def test_regenerate_entries_live_in_a_composed_tree(census):
    """`regenerate` means "a compose writes this", and only one tracked tree is.

    `plugin-cursor/` is composed from `core/` and `plugin-claude/`;
    `plugin-codex/`'s tracked tree is authored, and its composition happens at
    build time into `web/dist`. Miscategorising an authored file as
    `regenerate` would quietly excuse it from the sweep, so the location is
    checked rather than trusted.
    """
    misplaced = sorted(
        p for p, entry in census.items()
        if entry.get("category") == "regenerate" and not p.startswith("plugin-cursor/")
    )
    assert not misplaced, (
        "These entries claim to be composed copies but do not live under\n"
        "`plugin-cursor/`, the only composed tracked tree:\n\n"
        + "\n".join(f"  {p}" for p in misplaced)
        + "\n\n`plugin-codex/`'s tracked tree is authored — its composition\n"
        "writes into `web/dist` at build time, not into the source tree."
    )


# ---------------------------------------------------------------------------
# The coordinated set: one obligation, not N judgements (#657)
# ---------------------------------------------------------------------------
#
# `rewrite` reads "the file survives; the vocabulary does not". For 64 rows
# that was false and the census said it 64 times, once per file, for a change
# ADR-035 says is one coordinated versioned contract change or nothing. The
# worst of them was `cli/internal/cpclient/envelope_verify.go`, whose own
# comment explains that this vocabulary must survive: discharging that row
# alone changes the byte string the verifier hashes and fails every envelope
# the control plane has ever signed, not the next one written.
#
# 64 individually-plausible judgements is how a breaking change gets made
# without anyone deciding to make it, so the fix is not 64 reasons. It is one
# declaration that every one of those rows points at, and three guards holding
# the declaration in place from both directions: a row cannot join the set
# without it, and a row cannot leave the set on its own.


def _carries_any(repo_root: Path, rel: str, identifiers: list[str]) -> bool:
    """True if the tracked file spells one of a group's identifiers.

    Plain substring, not a word-boundary pattern, and that is deliberate. The
    census's own `PATTERNS` are prose-tolerant because they hunt a noun; these
    hunt an identifier, where the looser reading is the wrong one. `\\bworkstream\\b`
    also matches `workstream_owners`, `n_workstream` and `WORKSTREAM`, which are
    real retired-vocabulary occurrences and are NOT part of the signed-field
    contract change; folding them in would overstate the coordinated set.
    Measured 2026-09-09: `workstream_id` and `WorkstreamID` are the only
    spellings the field takes in this tree (`workstream_id_stays_null` is a
    test name for the same field, camelCase `workstreamId` appears nowhere),
    and the two spellings are bound by one struct tag at
    `cli/internal/cli/runtime_contract.go` -- which is why the Go spelling has
    to be listed separately, since two files carry the already-decoded field
    and a `workstream_id` grep cannot see them.
    """
    path = repo_root / rel
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(ident in text for ident in identifiers)


def _group_identifiers(coordinated_changes: dict) -> dict[str, list[str]]:
    return {
        name: [str(i) for i in (group.get("identifiers") or [])]
        for name, group in coordinated_changes.items()
    }


def test_the_coordinated_set_is_one_declaration(census, coordinated_changes):
    """The reason is written once, on the group, and every row reaches it.

    This is the whole mechanism: `coordinated` is exempt from `NEEDS_REASON`
    only because the obligation is declared at the group. If a row could name a
    group that does not exist, or a group could exist without a reason, the
    exemption would become an exemption from explaining anything.
    """
    rows = {
        p: entry
        for p, entry in census.items()
        if entry.get("category") == "coordinated"
    }
    # This assertion is what keeps the sibling guard from going quiet. The
    # `rewrite` guard below derives what to look for from the declared groups,
    # so deleting the declaration would make it vacuous rather than failing.
    # This fails instead, so the declaration cannot be removed silently: the
    # only way out is to remove the rows and the declaration together, which is
    # what shipping the coordinated change actually looks like.
    assert rows, (
        "No `coordinated` rows. If the coordinated change has shipped, remove\n"
        f"the `{COORDINATED_KEY}` block in the same commit -- a declaration\n"
        "outliving its subject is the ghost defect pointing the other way."
    )

    dangling = sorted(
        f"{p} -> {entry.get(ROW_GROUP_FIELD)!r}"
        for p, entry in rows.items()
        if entry.get(ROW_GROUP_FIELD) not in coordinated_changes
    )
    assert not dangling, (
        "These `coordinated` rows name no declared group. A coordinated row\n"
        f"carries `{ROW_GROUP_FIELD}` naming a key in `{COORDINATED_KEY}`;\n"
        "that group is where its reason lives, so a row that cannot reach one\n"
        "is a judgement with no reason anywhere:\n\n"
        + "\n".join(f"  {d}" for d in dangling)
        + f"\n\nDeclared groups: {', '.join(sorted(coordinated_changes)) or '(none)'}"
    )

    thin = sorted(
        name
        for name, group in coordinated_changes.items()
        if not str(group.get("reason") or "").strip()
        or not str(group.get("record") or "").strip()
        or not [str(i) for i in (group.get("identifiers") or []) if str(i).strip()]
    )
    assert not thin, (
        "These coordinated-change declarations are incomplete. Each needs a\n"
        "`reason` (why the rows move together, stated once), a `record` (the\n"
        "ADR or document that decided it) and `identifiers` (the exact\n"
        "spellings a sweeper would search for):\n\n"
        + "\n".join(f"  {name}" for name in thin)
    )

    unused = sorted(
        set(coordinated_changes)
        - {entry.get(ROW_GROUP_FIELD) for entry in rows.values()}
    )
    assert not unused, (
        "These coordinated-change declarations are referenced by no row.\n"
        "Delete them: the set has been discharged, or never existed:\n\n"
        + "\n".join(f"  {name}" for name in unused)
    )

    stray = sorted(
        f"{p} ({entry.get('category')})"
        for p, entry in census.items()
        if entry.get(ROW_GROUP_FIELD) and entry.get("category") != "coordinated"
    )
    assert not stray, (
        f"These rows carry `{ROW_GROUP_FIELD}` without the `coordinated`\n"
        "category, so the field means two things at once. A row is in the set\n"
        "or it is not:\n\n"
        + "\n".join(f"  {s}" for s in stray)
    )


def test_no_rewrite_row_instructs_a_coordinated_discharge(
    census, coordinated_changes, repo_root: Path
):
    """A file carrying a coordinated identifier may not be `rewrite`.

    This is the guard that catches a row joining the set without the shared
    declaration, which is how the 64 arrived in the first place: a sweeper
    working file by file classifies each one `rewrite`, every judgement is
    individually plausible, and nothing anywhere says these move together.

    Only `rewrite` is policed, because it is the category that asserts the
    vocabulary does not survive in this file. The location-derived categories
    make no such claim: `regenerate` is a composed copy that follows its
    source, `applied-migration` is a revision that has run, `guard` has to
    contain what it searches for, and `history` and the ADR categories describe
    the decision rather than instruct it. Measured 2026-09-09 at `28cf745a`:
    92 census rows carry the field, 64 of them were `rewrite`, and those 64 are
    the set.
    """
    identifiers = _group_identifiers(coordinated_changes)
    offenders = []
    for path, entry in sorted(census.items()):
        if entry.get("category") != "rewrite":
            continue
        for name, idents in identifiers.items():
            if _carries_any(repo_root, path, idents):
                offenders.append(f"  {path}  (belongs to {name})")
                break
    assert not offenders, (
        "These rows are classified `rewrite` -- \"the file survives; the\n"
        "vocabulary does not\" -- and they carry an identifier whose removal is\n"
        "a coordinated versioned contract change. As written, each one\n"
        "instructs its own discharge, and discharging it alone breaks the\n"
        "contract:\n\n"
        + "\n".join(offenders)
        + "\n\nClassify them `coordinated` and point them at the declared group\n"
        f"with `{ROW_GROUP_FIELD}`. Do not write a per-row reason for this:\n"
        "the reason is one sentence on the group, and 64 copies of it is the\n"
        "boilerplate #657 declined to add."
    )


def test_coordinated_rows_still_carry_their_identifier(
    census, coordinated_changes, repo_root: Path
):
    """A `coordinated` row whose file has dropped the identifier fails.

    The other direction, and the one that costs something to get wrong. The
    existing ghost test only notices when a file stops carrying the retired
    *noun* entirely, so a file that keeps the word in prose while quietly
    losing the field would pass it. This fails instead, which is what "cannot
    be discharged alone" has to mean mechanically: the field leaves every file
    in the group in one release, or it leaves none of them. A green sweep of
    one file is the failure mode ADR-035 is written against.
    """
    identifiers = _group_identifiers(coordinated_changes)
    discharged = []
    for path, entry in sorted(census.items()):
        if entry.get("category") != "coordinated":
            continue
        group = entry.get(ROW_GROUP_FIELD)
        idents = identifiers.get(group)
        if idents is None:
            continue  # reported by test_the_coordinated_set_is_one_declaration
        if not (repo_root / path).is_file():
            continue  # reported by test_census_carries_no_ghosts
        if not _carries_any(repo_root, path, idents):
            discharged.append(f"  {path}  ({group})")
    assert not discharged, (
        "These files no longer carry the coordinated identifier, but the rest\n"
        "of their group still does. Either one row was discharged on its own,\n"
        "which is the thing the group exists to prevent, or the coordinated\n"
        "change has shipped and every row in the group plus the declaration\n"
        "itself should go in one commit:\n\n"
        + "\n".join(discharged)
    )
