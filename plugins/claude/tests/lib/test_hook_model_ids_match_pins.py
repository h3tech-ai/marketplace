"""Layer 1 -- a hook may not hard-code a model id the pins do not name (#577).

WHY THIS FILE EXISTS
--------------------
Sibling to `test_authored_model_ids_match_pins.py`, which states the same
property over the authored prompt surface (`skills/`, `agents/`) for #441.
This one states it over the RUNTIME surface -- the hook scripts and the shared
Python they call -- because a stale literal there does not merely mislead a
reader, it is written to the control plane as a measurement.

The instance that prompted it: `synaptory-inject-protocols.sh` defaulted both
the CP span and the OTLP span to `--model claude-sonnet-4-5`, an id
`model-pins.json` has named under no tier since the pins rolled. Because
nothing sets `SYNAPTORY_AGENT_MODEL` on a real dispatch, the fallback was the
behaviour, and every subagent span in every project carried it. The fix is to
report nothing when nothing is observed (`test_inject_model_attribution.py`);
this file is what stops the next default from being added.

WHY THE CHECK RESOLVES THROUGH THE PIN FILE
-------------------------------------------
Comparing against a literal would go stale on the very re-pin it is meant to
survive -- the same mistake in a different file. `pinned_model_ids()` is read
fresh, so rolling a tier keeps this test honest, while a roll that leaves a
hard-coded id behind turns it red.

WHY COMMENTS ARE STRIPPED
-------------------------
A retired id is legitimate PROSE -- the comment that replaced the fallback
names `claude-sonnet-4-5` precisely so the next reader knows what was wrong
with it, and `test_authored_model_ids_match_pins` keeps an EXCLUDED set for
the same reason. What must not survive is a literal on an executable line,
which is the only kind that reaches a span. Excluding whole files instead
would blind the check to exactly the file it was written for.

WHAT THIS FILE DOES NOT PROVE
-----------------------------
Nothing about the model a dispatch really ran on. `model_pin_check`'s
docstring and #519 both say the pin does not bind on the dispatch path, and
#493 says presence is not authorization. This is a drift guard over source
text, not evidence about a run.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
BACKENDS = PLUGIN_ROOT / "skills" / "_shared" / "backends"
HOOKS = PLUGIN_ROOT / "hooks"

#: A concrete Claude model id, as opposed to a tier alias. Kept identical to
#: `test_authored_model_ids_match_pins.MODEL_ID_RE` -- the two files state one
#: property over two surfaces and must agree on what a model id looks like.
MODEL_ID_RE = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-\d[0-9A-Za-z.\-]*")

#: `# …` in shell and Python alike. Anchored at the start of a line (leading
#: whitespace allowed) so a `#` inside a string or a URL fragment is not
#: mistaken for a comment and used to smuggle a literal past the check.
COMMENT_LINE_RE = re.compile(r"^\s*#")


def _pin_checker():
    """`model_pin_check`, imported without dragging in the whole backends dir."""
    if str(BACKENDS) not in sys.path:
        sys.path.insert(0, str(BACKENDS))
    import model_pin_check  # noqa: PLC0415

    return model_pin_check


def _hook_sources() -> list[Path]:
    """Every hook script and every shared module a hook executes.

    `hooks/lib` is a symlink to `core/lib`, so this reaches the runtime all
    three hosts compose from -- a literal added there ships to Cursor and
    Codex too. Paths are de-duplicated by resolved target so the symlink
    cannot make one file look like two.
    """
    seen: dict[Path, Path] = {}
    # `os.walk(followlinks=True)` rather than `Path.glob("**/…")`: glob does
    # NOT descend into a symlinked directory, so the pathlib version silently
    # skipped every file under `hooks/lib` and the scan covered shell only.
    for dirpath, dirnames, filenames in os.walk(HOOKS, followlinks=True):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith((".sh", ".py")):
                continue
            path = Path(dirpath) / name
            # Key on the resolved target, keep the plugin-relative path: the
            # symlink must not make one file look like two, and a failure
            # should name the path a maintainer edits.
            seen.setdefault(path.resolve(), path)
    paths = sorted(seen.values())
    assert paths, f"no hook sources found under {HOOKS}"
    return paths


def _executable_ids(path: Path) -> set[str]:
    """Model ids on lines that are not whole-line comments."""
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if COMMENT_LINE_RE.match(line):
            continue
        found.update(MODEL_ID_RE.findall(line))
    return found


def test_no_hook_hard_codes_a_model_the_pins_do_not_name() -> None:
    mpc = _pin_checker()
    named = mpc.pinned_model_ids()

    stale = {
        path.relative_to(PLUGIN_ROOT).as_posix(): sorted(unpinned)
        for path in _hook_sources()
        if (unpinned := _executable_ids(path) - named)
    }
    assert not stale, (
        f"hook runtime hard-codes model ids that model-pins.json does not "
        f"name: {stale}. A hook is not a prompt -- what it puts in a span is "
        f"recorded as a measurement, so a retired id there is a wrong answer "
        f"in the data rather than a stale sentence. If the model is not "
        f"actually observed at this point, report nothing: the CLI flag, the "
        f"wire field and the column are all optional, and an absent model "
        f"reads as a gap where a wrong one does not."
    )


def test_the_scan_covers_the_hook_that_prompted_it() -> None:
    """Guards the property above against passing for an empty reason.

    The scan walks a glob over a symlinked tree; a change to either could
    quietly shrink it to nothing and leave the test green. Naming the
    SubagentStart hook explicitly ties this file to the defect it exists for.
    """
    scanned = {p.relative_to(PLUGIN_ROOT).as_posix() for p in _hook_sources()}
    assert "hooks/synaptory-inject-protocols.sh" in scanned, (
        "the SubagentStart hook is no longer scanned; if it moved, follow it"
    )
    assert "hooks/lib/otel_writer.py" in scanned, (
        "hooks/lib no longer resolves through to core/lib; the shared runtime "
        "every host composes from has dropped out of this check"
    )
    assert len(scanned) >= 20, f"only {len(scanned)} hook sources scanned"


def test_a_hard_coded_stale_id_is_actually_caught(tmp_path: Path) -> None:
    """The detector, run against a known-bad line.

    Without this the two tests above pass whether `_executable_ids` works or
    silently returns nothing -- and the comment-stripping rule is exactly the
    kind of thing that over-matches into uselessness.
    """
    mpc = _pin_checker()
    named = mpc.pinned_model_ids()

    bad = tmp_path / "hook.sh"
    bad.write_text(
        '# a comment naming claude-sonnet-4-5 is fine\n'
        'run --model "${SYNAPTORY_AGENT_MODEL:-claude-sonnet-4-5}"\n',
        encoding="utf-8",
    )
    assert _executable_ids(bad) - named == {"claude-sonnet-4-5"}

    good = tmp_path / "fixed.sh"
    good.write_text(
        '# this used to default to claude-sonnet-4-5\n'
        'run ${SYNAPTORY_AGENT_MODEL:+--model "$SYNAPTORY_AGENT_MODEL"}\n',
        encoding="utf-8",
    )
    assert not _executable_ids(good) - named
