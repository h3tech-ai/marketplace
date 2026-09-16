"""Layer 1 -- every remedy a refusal names is a route somebody can take.

A refusal that names an unreachable remedy is the unactionable kind `#517`
was about, and in one place here it was worse than unhelpful. Three refusals
in `story_pipeline` tell an operator to "supersede the authored set with
`revise_manifest --reason`", and the verb was exposed on no surface at all --
not the state machine's CLI, not the MCP. `#406`'s incentive design rests on
the legal route being available and RECORDED: with the route missing,
deleting the seal was a shorter path than revising it, which is `#507`
exactly.

WHAT THIS GUARDS, AND WHAT IT DOES NOT. It reads the refusal strings and
checks that every state-machine verb they name is dispatched by the CLI. It
cannot check that the remedy is the RIGHT one -- that is what the refusal's
own test is for -- and it cannot check prose. What it catches is the specific
rot this epic produced twice: a verb removed or never wired while the message
telling someone to run it stayed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import spq_state_machine as sm
import story_pipeline as story

#: Files whose refusal text may name a state-machine verb.
_SOURCES = ("story_pipeline", "spq_state_machine", "advance_kernel")


def _cli_verbs() -> set[str]:
    """The verbs `main` actually dispatches, read from its own source.

    From the DISPATCH, not from the usage string. The usage line is prose and
    has been wrong before; a verb is reachable only if there is an `elif` for
    it.
    """
    source = Path(sm.__file__).read_text(encoding="utf-8")
    body = source[source.index("def main("):]
    return set(re.findall(r'verb == "([a-z_]+)"', body))


def _named_verbs() -> dict[str, set[str]]:
    """Verb -> the modules whose strings name it as a remedy."""
    found: dict[str, set[str]] = {}
    for name in _SOURCES:
        module = sys.modules.get(name)
        if module is None:  # pragma: no cover - all three are imported above
            continue
        text = Path(module.__file__).read_text(encoding="utf-8")
        for verb in re.findall(r"`(?:spq_state_machine\.py )?([a-z_]+) --", text):
            found.setdefault(verb, set()).add(name)
    return found


def test_every_verb_a_refusal_names_is_dispatched_by_the_cli():
    import advance_kernel  # noqa: F401  - registers the module for the scan

    dispatched = _cli_verbs()
    unreachable = {
        verb: sorted(where)
        for verb, where in _named_verbs().items()
        if verb in _KNOWN_STATE_MACHINE_VERBS and verb not in dispatched
    }
    assert not unreachable, (
        "these refusals name a state-machine verb the CLI does not dispatch, "
        "so the remedy cannot be run: %s. Wire the verb, or change the "
        "message -- an unreachable remedy makes the illegal route the cheap "
        "one (#406, #507)." % unreachable
    )


#: The public verbs of the state machine, which is what the guard above
#: filters against. Declared rather than derived, because a refusal may
#: legitimately name a `git` or `synaptory` command with the same shape.
_KNOWN_STATE_MACHINE_VERBS = frozenset(
    name
    for name in dir(sm)
    if not name.startswith("_") and callable(getattr(sm, name, None))
)


def test_revise_manifest_is_reachable_and_needs_a_reason():
    """The specific route the three refusals name. Reachable, and it still
    refuses an unexplained supersession -- `C-14` makes a correction a linked
    supersession, and an unexplained one is an edit with extra steps."""
    assert "revise_manifest" in _cli_verbs()
    result = subprocess.run(
        [
            sys.executable,
            str(Path(sm.__file__)),
            "revise_manifest",
            ".",
            "--cycle-id=1-abcdef12",
            "--reason=",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "reason" in (result.stdout + result.stderr).lower()


def test_the_refusals_that_name_it_still_do():
    """Non-vacuity. If those messages are reworded away, this guard is
    guarding nothing and should be deleted rather than left green."""
    text = Path(story.__file__).read_text(encoding="utf-8")
    assert text.count("revise_manifest --reason") >= 2, (
        "no refusal names `revise_manifest --reason` any more, so the guard "
        "above has no subject"
    )


# ── The same class, one layer out: refusals that name a `synaptory` verb ─────
#
# `region_registry`'s refusals tell the operator to run
# `synaptory cycles regions reserve`, and the guard above deliberately cannot
# see it: that is a Go CLI verb, not a state-machine one. The rot is identical
# though -- rename the cobra command and the message telling someone to run it
# stays -- and the message is on the Commit path, where an unreachable remedy
# means the operator's only option is to stop.


def _synaptory_verbs_named() -> set[str]:
    """`synaptory <a> <b> <c>` phrases the Python refusals tell people to run."""
    named: set[str] = set()
    for name in ("region_registry",):
        module = __import__(name)
        text = Path(module.__file__).read_text(encoding="utf-8")
        # UP TO THE CLOSING BACKTICK, not up to the first space. The first
        # cut was non-greedy to the next whitespace, so every phrase collapsed
        # to its first word: the set was `{"cycles"}` and renaming
        # `regions` to anything at all kept the guard green. A guard whose
        # subject is one word of a four-word command is the vacuous kind this
        # file exists to catch elsewhere.
        for phrase in re.findall(r"`synaptory ([a-z][a-z0-9 -]*)`", text):
            parts = [p for p in phrase.split() if not p.startswith("-")]
            if parts:
                named.add(" ".join(parts))
    return named


def _cobra_paths() -> set[str]:
    """Command paths the Go CLI registers, read from `Use:` strings.

    From the source rather than by running `--help`, because the guard has to
    hold in an environment with no Go toolchain -- which is most CI legs and
    every plugin-only test run.
    """
    root = Path(__file__).resolve().parents[3] / "cli" / "internal" / "cli"
    uses: set[str] = set()
    for path in root.glob("*.go"):
        uses |= set(re.findall(r'Use:\s+"([a-z][a-z-]*)"', path.read_text()))
    return uses


def test_every_synaptory_verb_a_refusal_names_is_registered():
    registered = _cobra_paths()
    named = _synaptory_verbs_named()
    assert named, "no refusal names a `synaptory` command, so this guards nothing"
    missing = {
        phrase: [word for word in phrase.split() if word not in registered]
        for phrase in named
    }
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, (
        "these refusals name a `synaptory` command the CLI does not register, "
        "so the remedy cannot be run: %s. The Commit refuses without a "
        "reservation, so an unreachable remedy leaves the operator with no "
        "route at all." % missing
    )
