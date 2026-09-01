"""Parse Claude Code's PostToolUse / Stop stdin into a (tool, summary) pair.

Reads JSON on stdin, writes three lines to stdout:
    <tool_name>
    <one-line UI-ready summary>
    <session_id>

The first two lines may be empty if stdin couldn't be parsed. The shell
hook wrapper (``synaptory-activity-ping.sh``) treats both-empty as "nothing
to ship" and exits 0. The third line carries Claude Code's session_id so
the CLI can attribute the event to the session's bound project rather than
the process cwd (issue #117); it may be empty for a Stop envelope without
one, in which case the CLI falls back to the cached session id.

Kept as a separate file (not a bash heredoc) so the hook script stays
inspectable and the parser can be unit-tested directly.
"""

from __future__ import annotations

import json
import sys


def _truncate(s: object, n: int = 120) -> str:
    text = str(s)
    return text if len(text) <= n else text[:n] + "…"


def summarize(payload: dict) -> tuple[str, str]:
    """Map a PostToolUse / Stop payload to (tool_name, summary).

    Returns ("", "") when the payload carries no recognised tool — the
    caller decides what to do (Stop events synthesise a "Turn ended"
    line; PostToolUse events with no tool are dropped).

    Claude Code sends ``tool_name`` + ``tool_input``. Cursor's postToolUse
    / beforeShellExecution envelopes use ``tool`` / ``name``, ``args`` /
    ``input``, a top-level ``command``, and ``conversation_id``.
    """
    tool = str(
        payload.get("tool_name")
        or payload.get("tool")
        or payload.get("name")
        or ""
    ).strip()
    inp = payload.get("tool_input") or payload.get("args") or payload.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}

    if tool in {"Edit", "Write", "Read", "NotebookEdit", "StrReplace"}:
        path = (
            inp.get("file_path")
            or inp.get("notebook_path")
            or inp.get("path")
            or payload.get("file_path")
            or payload.get("file_path_after")
            or ""
        )
        return tool, f"{tool}: {_truncate(path)}"
    if tool in {"Bash", "Shell"}:
        cmd = str(inp.get("command") or payload.get("command") or "").splitlines()
        head = cmd[0] if cmd else ""
        label = "Bash" if tool == "Bash" else tool
        return tool, f"{label}: {_truncate(head)}"
    if tool in {"Glob", "Grep"}:
        pattern = inp.get("pattern") or inp.get("query") or ""
        return tool, f"{tool}: {_truncate(pattern, 80)}"
    if tool == "WebFetch":
        return tool, f"WebFetch: {_truncate(inp.get('url') or '')}"
    if tool == "WebSearch":
        return tool, f"WebSearch: {_truncate(inp.get('query') or '', 80)}"
    if tool == "Task":
        desc = inp.get("description") or inp.get("subagent_type") or ""
        return tool, f"Task: {_truncate(desc, 80)}"
    if tool == "TodoWrite":
        todos = inp.get("todos") or []
        return tool, f"TodoWrite: {len(todos)} item(s)"
    if tool:
        return tool, tool
    return "", ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("")
        print("")
        print("")
        return 0
    if not isinstance(payload, dict):
        print("")
        print("")
        print("")
        return 0
    tool, summary = summarize(payload)
    session_id = str(payload.get("session_id") or "").strip()
    print(tool)
    print(summary)
    print(session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
