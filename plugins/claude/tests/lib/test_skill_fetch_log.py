"""Layer 1 - `core/lib/skill_fetch_log.py`, the #446 retrieval producer.

#404 and #480 made tech packs, phase guides and five agent bodies fetchable
rather than mandatory. The JIT-eligible ceiling is known; the volume actually
retrieved was not, because nothing recorded a retrieval. These tests pin the
producer's three obligations:

  * classify all three retrieval routes, and nothing else;
  * attribute a retrieval to a dispatching role and story from the
    SubagentStart markers, and SAY when it could not;
  * write per retrieval, so a refetch is its own record.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import skill_fetch_log


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_cli_retrieval_is_classified_with_its_catalog_name() -> None:
    hit = skill_fetch_log.classify(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "synaptory skills get software-engineer/tech-packs/nextjs"
            },
        }
    )
    assert hit == ("software-engineer/tech-packs/nextjs", "cli")


def test_the_local_channel_binary_is_classified_too() -> None:
    """`synaptory-local` is the loopback-stamped binary a dev run uses."""
    hit = skill_fetch_log.classify(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "synaptory-local skills get protocols/tdd-discipline"},
        }
    )
    assert hit == ("protocols/tdd-discipline", "cli")


def test_a_disk_fallback_read_is_classified_as_the_same_catalog_name() -> None:
    """The route every post-#404 SKILL documents for source-tree dev.

    A CLI-side emission would be structurally blind to this. The name it
    yields is the CLI name, so the two routes aggregate as one body rather
    than as two different things.
    """
    hit = skill_fetch_log.classify(
        {
            "tool_name": "Read",
            "tool_input": {
                "file_path": "/repo/plugin-claude/agents/software-engineer/tech-packs/nextjs.md"
            },
        }
    )
    assert hit == ("software-engineer/tech-packs/nextjs", "disk-agent-body")


def test_a_protocol_body_read_is_classified() -> None:
    hit = skill_fetch_log.classify(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/repo/.synaptory/.protocols/receipt-protocol.md"},
        }
    )
    assert hit == ("protocols/receipt-protocol", "disk-protocol")


def test_a_bash_cat_of_the_fallback_path_is_still_a_retrieval() -> None:
    hit = skill_fetch_log.classify(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "cat agents/code-reviewer/phases/01-triage.md"},
        }
    )
    assert hit == ("code-reviewer/phases/01-triage", "disk-agent-body")


def test_the_cursor_payload_shape_is_classified() -> None:
    """Cursor sends tool/args, not tool_name/tool_input. The composed copy of
    this module runs on that host, so it must read that envelope too."""
    hit = skill_fetch_log.classify(
        {
            "tool": "Shell",
            "args": {"command": "synaptory skills get quality-engineer/phases/02-cases"},
        }
    )
    assert hit == ("quality-engineer/phases/02-cases", "cli")


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
        {"tool_name": "Read", "tool_input": {"file_path": "src/app/page.tsx"}},
        # The MANDATORY bodies are not catalog entries. Charging them as JIT
        # volume would double-count the payload the mandatory figure already
        # measured, which is the exact accounting error #444 settled.
        {"tool_name": "Read", "tool_input": {"file_path": "agents/software-engineer/SKILL.md"}},
        {"tool_name": "Read", "tool_input": {"file_path": "agents/software-engineer/agent.md"}},
        # Authoring a catalog body is not retrieving one.
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "agents/software-engineer/tech-packs/nextjs.md"},
        },
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "agents/software-engineer/tech-packs/go.md"},
        },
        {},
        None,
    ],
)
def test_non_retrievals_classify_to_nothing(payload) -> None:
    assert skill_fetch_log.classify(payload) is None


# ---------------------------------------------------------------------------
# Attribution, off the SubagentStart markers
# ---------------------------------------------------------------------------


def _marker(project: Path, agent_id: str, agent_type: str, link: str = "") -> Path:
    d = project / ".synaptory" / ".orchestrator" / "subagent-markers"
    d.mkdir(parents=True, exist_ok=True)
    p = d / agent_id
    p.write_text("%s\n%s\n" % (agent_type, link), encoding="utf-8")
    return p


def test_an_agent_id_on_the_payload_selects_its_own_marker(tmp_path: Path) -> None:
    _marker(tmp_path, "agent-1", "synaptory:software-engineer", "US-042\tD-9")
    _marker(tmp_path, "agent-2", "synaptory:quality-engineer", "US-077\tD-10")
    got = skill_fetch_log.attribute(str(tmp_path), {"agent_id": "agent-2"})
    assert got["role_attribution"] == "agent-id"
    assert got["role"] == "quality-engineer"
    assert got["story_id"] == "US-077"
    assert got["dispatch_id"] == "D-10"


def test_a_sole_live_dispatch_attributes_without_an_agent_id(tmp_path: Path) -> None:
    """PostToolUse stdin is not guaranteed to name the subagent. When exactly
    one dispatch is in flight the marker set answers unambiguously, and the
    record says the answer was reached that way rather than exactly."""
    _marker(tmp_path, "agent-1", "synaptory:software-engineer", "US-042\tD-9")
    got = skill_fetch_log.attribute(str(tmp_path), {})
    assert got["role_attribution"] == "sole-live-marker"
    assert got["role"] == "software-engineer"
    assert got["story_id"] == "US-042"


def test_two_live_dispatches_claim_no_role_at_all(tmp_path: Path) -> None:
    """A wrong role is worse than an absent one, because a wrong role is
    silently believed. Two in flight and nothing to tell them apart means the
    record carries no `role` and the reader falls back to inferring one from
    the catalog name, which it then labels as inferred."""
    _marker(tmp_path, "agent-1", "synaptory:software-engineer", "US-042\tD-9")
    _marker(tmp_path, "agent-2", "synaptory:code-reviewer", "US-042\tD-11")
    got = skill_fetch_log.attribute(str(tmp_path), {})
    assert got["role_attribution"] == "ambiguous"
    assert got["live_dispatches"] == 2
    assert "role" not in got
    assert "story_id" not in got


def test_no_marker_at_all_reports_no_live_dispatch(tmp_path: Path) -> None:
    got = skill_fetch_log.attribute(str(tmp_path), {})
    assert got == {"role_attribution": "no-live-dispatch"}


def test_a_marker_with_no_correlation_line_still_yields_the_role(tmp_path: Path) -> None:
    """SubagentStart records the story/dispatch pair only while it is
    unambiguous. The role is always there, so the role is always usable."""
    _marker(tmp_path, "agent-1", "synaptory:code-reviewer", "")
    got = skill_fetch_log.attribute(str(tmp_path), {})
    assert got["role"] == "code-reviewer"
    assert "story_id" not in got


def test_the_agent_id_is_matched_through_the_hooks_own_sanitiser(tmp_path: Path) -> None:
    """The marker filename is `tr -c 'A-Za-z0-9_.-' '_'` of the agent id, so
    matching has to apply the same transform or a real dispatch with a slash
    or colon in its id silently loses attribution."""
    _marker(tmp_path, "agent_1_2", "synaptory:software-engineer", "US-1\tD-1")
    got = skill_fetch_log.attribute(str(tmp_path), {"agent_id": "agent/1:2"})
    assert got["role_attribution"] == "agent-id"
    assert got["role"] == "software-engineer"


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _log_records(project: Path) -> list:
    p = project / skill_fetch_log.LOG_RELPATH
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_recorded_fetch_names_catalog_role_and_story(tmp_path: Path) -> None:
    _marker(tmp_path, "agent-1", "synaptory:software-engineer", "US-042\tD-9")
    written = skill_fetch_log.record_fetch(
        str(tmp_path),
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "synaptory skills get software-engineer/tech-packs/postgresql"
            },
            "session_id": "sess-1",
        },
    )
    assert written is not None
    (rec,) = _log_records(tmp_path)
    assert rec["event"] == "skill_fetch"
    assert rec["name"] == "software-engineer/tech-packs/postgresql"
    assert rec["role"] == "software-engineer"
    assert rec["story_id"] == "US-042"
    assert rec["route"] == "cli"
    assert rec["role_attribution"] == "sole-live-marker"
    assert rec["session_id"] == "sess-1"


def test_a_refetch_inside_one_dispatch_is_its_own_record(tmp_path: Path) -> None:
    """#444's verified example counted 3 distinct plus 1 refetch for SE. The
    refetch costs tokens again, so the producer must not deduplicate; the
    distinct/refetch split is the reader's job and it needs both events."""
    _marker(tmp_path, "agent-1", "synaptory:software-engineer", "US-042\tD-9")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "synaptory skills get software-engineer/tech-packs/nextjs"},
    }
    skill_fetch_log.record_fetch(str(tmp_path), payload)
    skill_fetch_log.record_fetch(str(tmp_path), payload)
    records = _log_records(tmp_path)
    assert len(records) == 2
    assert {r["name"] for r in records} == {"software-engineer/tech-packs/nextjs"}


def test_a_non_retrieval_writes_nothing(tmp_path: Path) -> None:
    assert (
        skill_fetch_log.record_fetch(
            str(tmp_path), {"tool_name": "Bash", "tool_input": {"command": "npm test"}}
        )
        is None
    )
    assert _log_records(tmp_path) == []


def test_the_signal_record_names_no_catalog(tmp_path: Path) -> None:
    """`arm` proves the producer was installed; it must not look like volume."""
    assert skill_fetch_log.arm_signal(str(tmp_path), role="software-engineer") is True
    (rec,) = _log_records(tmp_path)
    assert rec["event"] == "skill_fetch_signal"
    assert "name" not in rec
    assert rec["role"] == "software-engineer"


def test_an_unwritable_run_directory_is_silent_not_fatal(tmp_path: Path) -> None:
    """The whole signal must degrade to silence. A run directory this process
    cannot write is a measurement loss, never a dispatch failure."""
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        assert skill_fetch_log.arm_signal(str(blocked)) is False
        assert (
            skill_fetch_log.record_fetch(
                str(blocked),
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "synaptory skills get x/tech-packs/y"},
                },
            )
            is None
        )
    finally:
        blocked.chmod(0o700)


def test_the_cli_verbs_always_exit_zero(tmp_path: Path, monkeypatch) -> None:
    """The hook calls this in the tool-call hot path. Every argv shape,
    including garbage, must exit 0."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert skill_fetch_log.main(["record", str(tmp_path)]) == 0
    assert skill_fetch_log.main([]) == 0
    assert skill_fetch_log.main(["nonsense", str(tmp_path)]) == 0
    assert skill_fetch_log.main(["arm", str(tmp_path), "software-engineer", "US-1", "D-1"]) == 0
    (rec,) = _log_records(tmp_path)
    assert rec["dispatch_id"] == "D-1"
