"""Layer 1 — every tracker verb a shipped prompt tells an agent to run must exist.

A prompt that names a verb `tracker_cli.py` does not implement fails at
runtime, in front of a user, every time an agent follows the instruction.
Three such phantom verbs shipped before this guard existed:

  * `update-story --field design_ref=…`  (sprint-planning.md)
  * `update-story --acs '<json>'`        (project-owner/modes/story-analysis.md)
  * `get`                                (quality-engineer/phases/01-test-planning.md)

Nothing in the build or the test suite connected prompt text to the CLI's
dispatch table, so the drift was invisible. This test closes that gap: it
extracts the verb set from `tracker_cli.py` itself and asserts every verb
referenced by a `.md` under `plugin/` is in it.

Generalises `test_spq_state_machine.py::test_every_cli_verb_referenced_by_a_prompt_exists`,
which does the same for the SPQ state machine and barrier.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[2]
_CLI = _PLUGIN_DIR / "skills" / "_shared" / "scripts" / "tracker" / "tracker_cli.py"

# `${TRACKER_CLI} verb`, `{TRACKER_CLI} verb` (Python f-string form),
# `$TRACKER_CLI verb`, or a bare `tracker_cli.py verb` — with any number of
# global flags (`--project-dir .`, `--spec X`) between the two.
_INVOCATION = re.compile(
    r"(?:\$?\{TRACKER_CLI\}|\$TRACKER_CLI\b|\btracker_cli\.py)"
    r"(?:\s+--[A-Za-z0-9_-]+(?:=\S+)?(?:\s+(?!--)[^\s`'\"]+)?)*"
    r"\s+([a-z][a-z0-9-]*)"
)

# Prose that reads as an invocation but names no verb. Keep this list empty
# unless a doc genuinely has to talk about the CLI without calling it.
_PROSE_ALLOWLIST: set[str] = set()


def _implemented_verbs() -> set[str]:
    """Verbs the CLI actually dispatches on, read out of its source."""
    src = _CLI.read_text(encoding="utf-8")
    verbs = set(re.findall(r'command\s*==\s*"([a-z0-9-]+)"', src))
    # `elif command in ("create-ticket", "create-story"):`
    for group in re.findall(r"command\s+in\s+\(([^)]*)\)", src):
        verbs |= set(re.findall(r'"([a-z0-9-]+)"', group))
    return verbs


def test_cli_verb_extraction_is_not_silently_empty():
    """If the dispatch style in tracker_cli.py changes, fail loudly here.

    Without this, a refactor to argparse would empty the verb set and the
    real test below would start reporting every prompt as broken (or, if
    the regex also stopped matching, pass while checking nothing).
    """
    verbs = _implemented_verbs()
    assert len(verbs) > 10, (
        f"Only extracted {sorted(verbs)} from {_CLI.name} — the dispatch "
        "style probably changed. Update _implemented_verbs()."
    )
    # Spot-check verbs that many prompts depend on.
    for expected in ("get-story", "get-backlog", "update-status", "create-story"):
        assert expected in verbs, f"expected {expected!r} in {sorted(verbs)}"


def _prompt_invocations() -> list[tuple[Path, int, str]]:
    hits = []
    for md in sorted(_PLUGIN_DIR.rglob("*.md")):
        if "tests" in md.relative_to(_PLUGIN_DIR).parts:
            continue
        for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
            for match in _INVOCATION.finditer(line):
                hits.append((md, lineno, match.group(1)))
    return hits


def test_prompt_scan_finds_invocations():
    """Guard the guard — an empty scan would make the real test vacuous."""
    hits = _prompt_invocations()
    assert len(hits) > 20, (
        f"Only found {len(hits)} tracker_cli invocations across plugin/*.md; "
        "the _INVOCATION regex has probably stopped matching."
    )


def test_every_tracker_verb_referenced_by_a_prompt_exists():
    verbs = _implemented_verbs()
    broken: dict[str, list[str]] = {}

    for md, lineno, verb in _prompt_invocations():
        if verb in verbs or verb in _PROSE_ALLOWLIST:
            continue
        broken.setdefault(verb, []).append(f"{md.relative_to(_PLUGIN_DIR)}:{lineno}")

    if broken:
        report = "\n".join(
            f"  {verb!r} referenced at:\n" + "\n".join(f"    {loc}" for loc in locs)
            for verb, locs in sorted(broken.items())
        )
        pytest.fail(
            "Prompt(s) instruct agents to run tracker_cli.py verbs that do not "
            f"exist.\n\n{report}\n\n"
            f"Implemented verbs: {', '.join(sorted(verbs))}\n\n"
            "Fix the prompt to use a real verb (or the human/UI path). Adding a "
            "verb is only correct if every adapter in tracker/*_adapter.py can "
            "honour it — the remote adapters restrict update_story() to a fixed "
            "field allow-list and silently drop the rest."
        )
