"""Record one catalog retrieval into the local run directory (#446).

#404 and #480 moved tech packs, phase guides and five agent bodies out of the
mandatory dispatch payload and into a fetch catalog. That converted a measured
cost into an unmeasured one: the JIT-eligible ceiling is known (34,713 words
for the three pipeline roles) but the volume actually retrieved is not, and a
ceiling bounds the risk without pricing it. This module is the producer half
of the signal that prices it. The consumer already exists:
``benchmarks/parallelism/pilot_metrics.fetch_volume``.

What it writes, and where
-------------------------
One JSON object per line into
``<project>/.synaptory/.orchestrator/skill-fetches.jsonl``. Two record kinds:

``skill_fetch_signal``
    Written once per instrumented dispatch by the SubagentStart hook. It names
    no catalog and counts no volume. It exists so that a run which fetched
    NOTHING can be distinguished from a run that never carried the signal at
    all. Without it the two collapse into ABSENT, and ABSENT is not zero:
    "unmeasured" and "measured and empty" are opposite claims about the very
    thing this ticket exists to measure.

``skill_fetch``
    One per retrieval, carrying the catalog ``name``, the dispatching ``role``,
    the ``story_id``, the ``route`` it came through, and how the role was
    attributed. A REFETCH of the same body inside one dispatch is written as
    its own record, not deduplicated: it costs tokens again, and the
    distinct/refetch split is what says whether the catalog is being used well
    or thrashed. Deduplication is the reader's business, not the producer's.

Retrieval routes recognised
---------------------------
Post-#404 a catalog body reaches an agent one of three ways, and this module
classifies all three from the PostToolUse payload:

===============================  ==========================================
route                            payload shape
===============================  ==========================================
``cli``                          ``Bash("synaptory skills get <name>")``
``disk-agent-body``              ``Read("…/agents/<role>/<rel>.md")``
``disk-protocol``                ``Read("…/.synaptory/.protocols/<n>.md")``
===============================  ==========================================

The two disk routes are the fallback every post-#404 SKILL documents for
source-tree dev, where no CLI is resolvable. Recording them is why the
emission point is PostToolUse and not the CLI: a CLI-side emission would see
the first route and be structurally blind to the other two.

Local-first, and silent when it cannot write
--------------------------------------------
No CLI, no network, no control plane. The whole emission is one append to a
file inside the run directory the other metrics already read. An unstamped
tree resolves no CLI by design (#320, #425) and this module never asks for
one, so it behaves identically there. Every entry point swallows every
exception and returns rather than raising: a measurement signal that can
break a dispatch is worse than no measurement signal.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# `Bash("synaptory skills get <name>")`, the CLI route. Deliberately the same
# pattern the reader uses (`pilot_metrics._FETCH_CMD_RE`) so the producer
# cannot recognise a shape the consumer rejects.
_CLI_FETCH_RE = re.compile(r"synaptory(?:-local)?\s+skills\s+get\s+([^\s\"';|&<>]+)")

# `.../agents/<role>/<relative>.md` — the documented on-disk fallback for a
# catalog name. `<relative>` must have at least one directory segment, which
# is what keeps `agents/<role>/agent.md` and `agents/<role>/SKILL.md` (the
# MANDATORY bodies, not catalog entries) out of the JIT count.
_AGENT_BODY_RE = re.compile(
    r"agents/([a-z][a-z0-9-]*)/((?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+)\.md"
)

# `.synaptory/.protocols/<name>.md` — the documented fallback for a
# `protocols/<name>` catalog entry.
_PROTOCOL_BODY_RE = re.compile(r"\.protocols/([A-Za-z0-9._/-]+)\.md")

LOG_RELPATH = os.path.join(".synaptory", ".orchestrator", "skill-fetches.jsonl")
MARKER_RELPATH = os.path.join(".synaptory", ".orchestrator", "subagent-markers")

SIGNAL_EVENT = "skill_fetch_signal"
FETCH_EVENT = "skill_fetch"


# ---------------------------------------------------------------------------
# Classification: is this tool call a catalog retrieval?
# ---------------------------------------------------------------------------


def classify(payload: dict) -> Optional[Tuple[str, str]]:
    """Return ``(catalog_name, route)`` for a retrieval, else ``None``.

    Accepts a raw PostToolUse payload in either host's shape: Claude Code
    sends ``tool_name`` + ``tool_input``, Cursor sends ``tool``/``name`` with
    ``args``/``input`` and sometimes a top-level ``command``. That mirrors
    ``activity_parser.summarize``, which is the other reader of this stdin.

    Only Bash and Read are inspected. An Edit or a Write of a catalog path is
    an agent AUTHORING a body, not retrieving one, and charging it as fetch
    volume would inflate the number this ticket exists to make honest.
    """
    if not isinstance(payload, dict):
        return None
    tool = str(
        payload.get("tool_name") or payload.get("tool") or payload.get("name") or ""
    ).strip()
    inp = payload.get("tool_input") or payload.get("args") or payload.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}

    if tool in {"Bash", "Shell"}:
        command = str(inp.get("command") or payload.get("command") or "")
        match = _CLI_FETCH_RE.search(command)
        if match:
            return match.group(1).strip().strip("\"'"), "cli"
        # A Bash `cat` of the fallback path is still a retrieval by the other
        # route, so fall through to the path patterns over the whole command.
        return _classify_path(command)

    if tool == "Read":
        path = str(
            inp.get("file_path")
            or inp.get("path")
            or payload.get("file_path")
            or ""
        )
        return _classify_path(path)

    return None


def _classify_path(haystack: str) -> Optional[Tuple[str, str]]:
    if not haystack:
        return None
    match = _AGENT_BODY_RE.search(haystack)
    if match:
        return "%s/%s" % (match.group(1), match.group(2)), "disk-agent-body"
    match = _PROTOCOL_BODY_RE.search(haystack)
    if match:
        return "protocols/%s" % match.group(1), "disk-protocol"
    return None


# ---------------------------------------------------------------------------
# Attribution: which dispatch was this retrieval inside?
# ---------------------------------------------------------------------------


def attribute(project_dir: str, payload: dict) -> dict:
    """Name the dispatching role and story from the SubagentStart markers.

    ``synaptory-inject-protocols.sh`` already drops a per-agent marker under
    ``.synaptory/.orchestrator/subagent-markers/<agent_id>`` at SubagentStart
    and ``synaptory-verify-receipt.sh`` consumes it at SubagentStop. So the
    live marker set IS the set of dispatches currently in flight, and this
    reads that rather than adding a second correlation mechanism.

    The returned ``role_attribution`` says how the answer was reached, and it is
    written onto every record. Four outcomes:

    ``agent-id``
        The payload named an ``agent_id`` and a marker exists for it. Exact.
    ``sole-live-marker``
        No agent id on the payload, exactly one dispatch in flight. Sound,
        but it is an inference and is labelled as one.
    ``ambiguous``
        Two or more dispatches in flight and nothing to tell them apart.
        No role is claimed; the reader falls back to inferring one from the
        catalog name's leading segment and reports THAT as inferred.
    ``no-live-dispatch``
        No marker at all. The retrieval happened outside an instrumented
        subagent dispatch (the orchestrator's own fetch, or a dispatch on a
        host that does not write markers). Again no role is claimed.

    A wrong role is worse than an absent one here, because a wrong role is
    silently believed. So ``role`` is omitted from the record whenever it was
    not actually determined.
    """
    out = {"role_attribution": "no-live-dispatch"}
    marker_dir = Path(project_dir) / MARKER_RELPATH
    try:
        markers = sorted(p for p in marker_dir.iterdir() if p.is_file())
    except Exception:  # noqa: BLE001 - an unreadable marker dir is not an error
        return out

    chosen = None
    agent_id = str(payload.get("agent_id") or "").strip() if isinstance(payload, dict) else ""
    if agent_id:
        safe = _safe_marker_name(agent_id)
        for marker in markers:
            if marker.name == safe:
                chosen = marker
                out["role_attribution"] = "agent-id"
                out["agent_id"] = agent_id
                break
    if chosen is None:
        if len(markers) == 1:
            chosen = markers[0]
            out["role_attribution"] = "sole-live-marker"
        elif len(markers) > 1:
            out["role_attribution"] = "ambiguous"
            out["live_dispatches"] = len(markers)
            return out
        else:
            return out

    role, story, dispatch_id = _read_marker(chosen)
    if role:
        out["role"] = role
    if story:
        out["story_id"] = story
    if dispatch_id:
        out["dispatch_id"] = dispatch_id
    return out


def _safe_marker_name(agent_id: str) -> str:
    """Mirror the hook's `tr -c 'A-Za-z0-9_.-' '_'` marker filename."""
    return "".join(c if (c.isalnum() and c.isascii()) or c in "_.-" else "_" for c in agent_id)


def _read_marker(marker: Path) -> Tuple[str, str, str]:
    """Read `(role, story, dispatch_id)` out of one SubagentStart marker.

    Line 1 is the agent type (``synaptory:<role>``); line 2 is
    ``story<TAB>dispatch_id`` when the correlation was unambiguous at
    SubagentStart, and empty otherwise. Same layout SubagentStop reads.
    """
    try:
        lines = marker.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return "", "", ""
    agent_type = lines[0].strip() if lines else ""
    role = agent_type.split(":", 1)[1] if ":" in agent_type else agent_type
    link = lines[1] if len(lines) > 1 else ""
    if "\t" in link:
        story, dispatch_id = link.split("\t", 1)
    else:
        story, dispatch_id = link, ""
    return role.strip(), story.strip(), dispatch_id.strip()


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _append(project_dir: str, record: dict) -> bool:
    """Append one JSONL record. Returns False on any failure, never raises."""
    try:
        path = Path(project_dir) / LOG_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        # One `write` of one short line opened in append mode: concurrent
        # dispatches interleave lines rather than corrupting them, which is
        # the same guarantee events.jsonl relies on.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return True
    except Exception:  # noqa: BLE001 - silence is the contract
        return False


def arm_signal(project_dir: str, role: str = "", story_id: str = "", dispatch_id: str = "") -> bool:
    """Record that this dispatch is instrumented, before it fetches anything.

    This is the line between ABSENT and zero. A dispatch that retrieves no
    catalog body still leaves this record, so the run reports a measured 0; a
    run on a tree without this producer leaves no file at all and reports
    ABSENT. Called from SubagentStart, where the role is known exactly.
    """
    record = {"event": SIGNAL_EVENT, "at": _now()}
    if role:
        record["role"] = role
    if story_id:
        record["story_id"] = story_id
    if dispatch_id:
        record["dispatch_id"] = dispatch_id
    return _append(project_dir, record)


def record_fetch(project_dir: str, payload: dict) -> Optional[dict]:
    """Classify one tool call and, if it is a retrieval, record it.

    Returns the written record, or None when the call was not a retrieval or
    the write failed. Never raises.
    """
    try:
        hit = classify(payload)
    except Exception:  # noqa: BLE001
        return None
    if not hit:
        return None
    name, route = hit
    record = {"event": FETCH_EVENT, "name": name, "route": route, "at": _now()}
    try:
        record.update(attribute(project_dir, payload))
    except Exception:  # noqa: BLE001
        record.setdefault("role_attribution", "no-live-dispatch")
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        record["session_id"] = session_id
    if not _append(project_dir, record):
        return None
    return record


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# CLI, for the hooks
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    """Always exit 0. Two verbs:

    ``record <project_dir>``
        Read a PostToolUse payload on stdin and append a ``skill_fetch``
        record if it was a retrieval.
    ``arm <project_dir> <role> [story] [dispatch_id]``
        Append the ``skill_fetch_signal`` record for a dispatch.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        return 0
    verb, project_dir = args[0], args[1]
    if verb == "arm":
        arm_signal(
            project_dir,
            role=args[2] if len(args) > 2 else "",
            story_id=args[3] if len(args) > 3 else "",
            dispatch_id=args[4] if len(args) > 4 else "",
        )
        return 0
    if verb == "record":
        try:
            payload = json.load(sys.stdin)
        except Exception:  # noqa: BLE001
            return 0
        if isinstance(payload, dict):
            record_fetch(project_dir, payload)
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
