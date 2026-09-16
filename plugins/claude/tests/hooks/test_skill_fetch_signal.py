"""Layer 2 - the #446 retrieval signal, through the real PostToolUse hook.

The producer lives inside `synaptory-activity-ping.sh` because that is the one
place both retrieval routes are visible: a CLI-side emission would see
`synaptory skills get` and be blind to the on-disk fallback every post-#404
SKILL documents for source-tree dev.

These tests exec the hook, not the module, so they also pin the two properties
a unit test cannot see: that the local write happens BEFORE the control-plane
CLI gate (so an unstamped tree still measures), and that the hook still exits 0
either way.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def activity_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-activity-ping.sh"


def _project(env: dict) -> Path:
    project = Path(env["CLAUDE_PROJECT_DIR"])
    (project / ".synaptory" / ".orchestrator").mkdir(parents=True, exist_ok=True)
    return project


def _dispatch_marker(project: Path, agent_id: str, agent_type: str, link: str) -> None:
    d = project / ".synaptory" / ".orchestrator" / "subagent-markers"
    d.mkdir(parents=True, exist_ok=True)
    (d / agent_id).write_text("%s\n%s\n" % (agent_type, link), encoding="utf-8")


def _fetch_records(project: Path) -> list:
    log = project / ".synaptory" / ".orchestrator" / "skill-fetches.jsonl"
    if not log.is_file():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _tool_call(command: str = "", read_path: str = "") -> str:
    if command:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    else:
        payload = {"tool_name": "Read", "tool_input": {"file_path": read_path}}
    payload["session_id"] = "sess-446"
    payload["hook_event_name"] = "PostToolUse"
    return json.dumps(payload)


@pytest.mark.hook
def test_a_dispatch_that_retrieves_leaves_a_record_per_retrieval(
    activity_hook: Path, hook_env: dict, run_hook
) -> None:
    """The acceptance criterion: per-retrieval records naming catalog, role
    and story. Three distinct bodies plus one refetch, the exact shape #444
    verified by hand, and the refetch is a fourth record rather than a
    swallowed duplicate."""
    project = _project(hook_env)
    _dispatch_marker(project, "agent-se-1", "synaptory:software-engineer", "US-042\tD-9")

    calls = [
        "synaptory skills get software-engineer/tech-packs/nextjs",
        "synaptory skills get software-engineer/tech-packs/tailwind",
        "synaptory skills get software-engineer/phases/02-service-implementation",
        # …and the model comes back for the first one later in the dispatch.
        "synaptory skills get software-engineer/tech-packs/nextjs",
    ]
    for command in calls:
        result = run_hook(activity_hook, env=hook_env, stdin=_tool_call(command=command))
        assert result.returncode == 0, result.stderr

    fetches = [r for r in _fetch_records(project) if r["event"] == "skill_fetch"]
    assert len(fetches) == 4, fetches
    assert [r["name"] for r in fetches] == [
        "software-engineer/tech-packs/nextjs",
        "software-engineer/tech-packs/tailwind",
        "software-engineer/phases/02-service-implementation",
        "software-engineer/tech-packs/nextjs",
    ]
    for rec in fetches:
        assert rec["role"] == "software-engineer"
        assert rec["story_id"] == "US-042"
        assert rec["route"] == "cli"


@pytest.mark.hook
def test_the_disk_fallback_retrieval_is_recorded_too(
    activity_hook: Path, hook_env: dict, run_hook
) -> None:
    """This is the route a CLI-side emission cannot see, and it is exactly the
    route a source-tree dev run takes. It resolves to the same catalog name as
    the CLI route, so the two aggregate as one body."""
    project = _project(hook_env)
    _dispatch_marker(project, "agent-cr-1", "synaptory:code-reviewer", "US-042\tD-11")

    result = run_hook(
        activity_hook,
        env=hook_env,
        stdin=_tool_call(read_path="/repo/plugin-claude/agents/code-reviewer/phases/01-triage.md"),
    )
    assert result.returncode == 0, result.stderr

    (rec,) = [r for r in _fetch_records(project) if r["event"] == "skill_fetch"]
    assert rec["name"] == "code-reviewer/phases/01-triage"
    assert rec["route"] == "disk-agent-body"
    assert rec["role"] == "code-reviewer"
    assert rec["story_id"] == "US-042"


@pytest.mark.hook
def test_the_first_tool_call_arms_the_signal_so_zero_stays_expressible(
    activity_hook: Path, hook_env: dict, run_hook
) -> None:
    """A run that fetched nothing must report a measured 0, not ABSENT. That
    needs proof the producer was watching, which is what the signal record is.
    A tool call that is not a retrieval arms it and adds no volume."""
    project = _project(hook_env)
    result = run_hook(activity_hook, env=hook_env, stdin=_tool_call(command="pytest -q"))
    assert result.returncode == 0, result.stderr

    records = _fetch_records(project)
    assert [r["event"] for r in records] == ["skill_fetch_signal"]
    assert "name" not in records[0]


@pytest.mark.hook
def test_the_signal_is_armed_once_not_once_per_tool_call(
    activity_hook: Path, hook_env: dict, run_hook
) -> None:
    """The arming spawn is bounded to the first tool call of a run directory.
    Otherwise every tool call in a session would pay for a python start to
    re-assert something already on disk."""
    project = _project(hook_env)
    for _ in range(3):
        run_hook(activity_hook, env=hook_env, stdin=_tool_call(command="ls"))
    signals = [r for r in _fetch_records(project) if r["event"] == "skill_fetch_signal"]
    assert len(signals) == 1


@pytest.mark.hook
def test_an_unstamped_tree_still_records_and_still_exits_zero(
    activity_hook: Path,
    plugin_root: Path,
    stub_cli_unavailable: dict,
    tmp_path: Path,
    run_hook,
) -> None:
    """The #320 rule: an unstamped tree addresses nothing, and #425 made the
    shell resolver fail closed on exactly that. So the control-plane ping is
    correctly skipped here. The local measurement must not be, because it
    needs no CLI at all, and the dispatch must be untouched either way.

    No `cp-url.local`, no stub on PATH: `_resolve-cli.sh` resolves nothing.
    """
    project = tmp_path / "unstamped-project"
    (project / ".synaptory" / ".orchestrator").mkdir(parents=True)
    env = {
        **stub_cli_unavailable,
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    _dispatch_marker(project, "agent-se-1", "synaptory:software-engineer", "US-042\tD-9")

    result = run_hook(
        activity_hook,
        env=env,
        stdin=_tool_call(command="synaptory skills get software-engineer/tech-packs/go"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", "the hook must inject nothing into the tool call"

    (rec,) = [r for r in _fetch_records(project) if r["event"] == "skill_fetch"]
    assert rec["name"] == "software-engineer/tech-packs/go"
    assert rec["role"] == "software-engineer"


@pytest.mark.hook
def test_two_live_dispatches_record_the_ambiguity_rather_than_a_guess(
    activity_hook: Path, hook_env: dict, run_hook
) -> None:
    """Concurrent dispatches are the blind spot of a PostToolUse emission that
    is not handed an agent id. The record must say so rather than pick one of
    the two roles, because a wrong role would be believed."""
    project = _project(hook_env)
    _dispatch_marker(project, "agent-se-1", "synaptory:software-engineer", "US-042\tD-9")
    _dispatch_marker(project, "agent-cr-1", "synaptory:code-reviewer", "US-077\tD-11")

    run_hook(
        activity_hook,
        env=hook_env,
        stdin=_tool_call(command="synaptory skills get software-engineer/tech-packs/mcp"),
    )
    (rec,) = [r for r in _fetch_records(project) if r["event"] == "skill_fetch"]
    assert rec["role_attribution"] == "ambiguous"
    assert "role" not in rec
    assert rec["live_dispatches"] == 2


@pytest.mark.hook
def test_an_agent_id_on_the_payload_beats_the_ambiguity(
    activity_hook: Path, hook_env: dict, run_hook
) -> None:
    """When the host does hand PostToolUse the subagent id, concurrency stops
    being a blind spot. Nothing in this tree can make the host send it, so the
    producer uses it when present and degrades honestly when not."""
    project = _project(hook_env)
    _dispatch_marker(project, "agent-se-1", "synaptory:software-engineer", "US-042\tD-9")
    _dispatch_marker(project, "agent-cr-1", "synaptory:code-reviewer", "US-077\tD-11")

    payload = json.loads(_tool_call(command="synaptory skills get code-reviewer/phases/02-depth"))
    payload["agent_id"] = "agent-cr-1"
    run_hook(activity_hook, env=hook_env, stdin=json.dumps(payload))

    (rec,) = [r for r in _fetch_records(project) if r["event"] == "skill_fetch"]
    assert rec["role_attribution"] == "agent-id"
    assert rec["role"] == "code-reviewer"
    assert rec["story_id"] == "US-077"


@pytest.mark.hook
def test_outside_a_synaptory_project_nothing_is_recorded(
    activity_hook: Path,
    plugin_root: Path,
    stub_cli_unavailable: dict,
    tmp_path: Path,
    run_hook,
) -> None:
    """Ad-hoc Claude Code use outside a synaptory project generates no
    telemetry, and must generate no measurement file either."""
    project = tmp_path / "plain-repo"
    project.mkdir()
    env = {
        **stub_cli_unavailable,
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    result = run_hook(
        activity_hook,
        env=env,
        stdin=_tool_call(command="synaptory skills get software-engineer/tech-packs/go"),
    )
    assert result.returncode == 0
    assert not (project / ".synaptory").exists()


@pytest.mark.hook
def test_the_control_plane_ping_still_ships(
    activity_hook: Path, hook_env: dict, run_hook, tmp_path: Path
) -> None:
    """Regression guard on the reordering: the local write was inserted ahead
    of the CLI gate, so the existing outbox ping must still fire and still
    carry its session id (#117)."""
    telemetry_log = tmp_path / "telemetry.log"
    telemetry_log.write_text("", encoding="utf-8")
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}
    _project(env)

    result = run_hook(
        activity_hook,
        env=env,
        stdin=_tool_call(command="synaptory skills get software-engineer/tech-packs/react"),
    )
    assert result.returncode == 0

    deadline = time.monotonic() + 5.0
    text = ""
    while time.monotonic() < deadline:
        text = telemetry_log.read_text(encoding="utf-8")
        if "telemetry activity" in text:
            break
        time.sleep(0.05)
    assert "telemetry activity" in text, text
    assert "--session-id sess-446" in text, text
