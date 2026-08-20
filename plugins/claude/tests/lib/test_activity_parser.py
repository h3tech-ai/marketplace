"""Layer 1 — ``plugin-claude/hooks/lib/activity_parser.py`` summarizer tests.

Pure-logic tests for the PostToolUse / Stop stdin parser. The bash hook
that calls this is exercised separately (Layer 2).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hooks.lib.activity_parser import summarize

_PARSER = Path(__file__).resolve().parents[2] / "hooks" / "lib" / "activity_parser.py"


def _run_parser(stdin: str) -> list[str]:
    """Run the parser's main() over stdin and return its output lines."""
    out = subprocess.run(
        [sys.executable, str(_PARSER)],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


@pytest.mark.unit
def test_edit_yields_file_path_summary():
    tool, summary = summarize(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/foo.ts"}}
    )
    assert tool == "Edit"
    assert summary == "Edit: src/foo.ts"


@pytest.mark.unit
def test_bash_uses_first_line_only():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls /tmp\nrm /etc/passwd"},
    }
    tool, summary = summarize(payload)
    assert tool == "Bash"
    # Only the first line should be in the summary — guards against
    # multi-line commands leaking destructive payloads into telemetry.
    assert summary == "Bash: ls /tmp"
    assert "/etc/passwd" not in summary


@pytest.mark.unit
def test_long_file_path_is_truncated():
    long_path = "src/" + ("nested/" * 30) + "leaf.tsx"
    tool, summary = summarize(
        {"tool_name": "Edit", "tool_input": {"file_path": long_path}}
    )
    assert tool == "Edit"
    assert summary.endswith("…")
    # 120-char default cap + the "Edit: " prefix.
    assert len(summary) <= len("Edit: ") + 121


@pytest.mark.unit
def test_glob_and_grep_carry_pattern():
    tool, summary = summarize(
        {"tool_name": "Glob", "tool_input": {"pattern": "**/*.tsx"}}
    )
    assert (tool, summary) == ("Glob", "Glob: **/*.tsx")

    tool, summary = summarize(
        {"tool_name": "Grep", "tool_input": {"pattern": "foo|bar"}}
    )
    assert (tool, summary) == ("Grep", "Grep: foo|bar")


@pytest.mark.unit
def test_task_uses_description_then_subagent_type():
    tool, summary = summarize(
        {
            "tool_name": "Task",
            "tool_input": {
                "description": "Audit repo for X",
                "subagent_type": "Explore",
            },
        }
    )
    assert summary == "Task: Audit repo for X"

    # Falls back to subagent_type when description missing.
    _, summary = summarize(
        {"tool_name": "Task", "tool_input": {"subagent_type": "Explore"}}
    )
    assert summary == "Task: Explore"


@pytest.mark.unit
def test_todowrite_counts_items():
    tool, summary = summarize(
        {"tool_name": "TodoWrite", "tool_input": {"todos": [1, 2, 3]}}
    )
    assert (tool, summary) == ("TodoWrite", "TodoWrite: 3 item(s)")


@pytest.mark.unit
def test_unknown_tool_falls_back_to_tool_name():
    tool, summary = summarize({"tool_name": "NewTool"})
    assert tool == "NewTool"
    assert summary == "NewTool"


@pytest.mark.unit
def test_empty_payload_yields_blanks():
    assert summarize({}) == ("", "")


@pytest.mark.unit
def test_non_dict_tool_input_is_tolerated():
    """Buggy stdin (tool_input as a string) must not crash the parser."""
    tool, summary = summarize({"tool_name": "Edit", "tool_input": "not-a-dict"})
    assert tool == "Edit"
    # No file_path to pull from → summary is "Edit: " (truncated of empty).
    assert summary == "Edit: "


@pytest.mark.unit
def test_webfetch_carries_url():
    _, summary = summarize(
        {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com/a"}}
    )
    assert summary == "WebFetch: https://example.com/a"


# ── main() output contract: 3 lines, session_id last (issue #117) ────────────


@pytest.mark.unit
def test_main_emits_session_id_as_third_line():
    lines = _run_parser(
        '{"tool_name":"Bash","tool_input":{"command":"ls"},'
        '"session_id":"fc2dae1a-d78f-4ba8-892d-2ae13b33753a"}'
    )
    assert lines[0] == "Bash"
    assert lines[1] == "Bash: ls"
    assert lines[2] == "fc2dae1a-d78f-4ba8-892d-2ae13b33753a"


@pytest.mark.unit
def test_main_third_line_blank_when_no_session_id():
    # A payload with no session_id still emits three lines; the third is blank
    # so the hook's `sed -n '3p'` yields "" and skips --session-id.
    lines = _run_parser('{"tool_name":"Read","tool_input":{"file_path":"a.ts"}}')
    assert len(lines) == 3
    assert lines[2] == ""


@pytest.mark.unit
def test_main_malformed_stdin_emits_three_blank_lines():
    lines = _run_parser("not json")
    assert lines == ["", "", ""]
