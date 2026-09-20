"""Layer 2 — SubagentStart records WHICH SESSION drives the Cycle (#791).

`loop_engine.should_continue` now speaks only to the holder, and the holder is
established by a dispatch. That makes this hook the one place a claim comes
into existence, so an engine test alone would prove nothing: a correct engine
wired to a hook that writes no claim leaves every project permanently
`unclaimed`, which is exactly the pre-fix behaviour and is silent.

These tests exec the real hook and assert the record it leaves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.hook

_HOLDER_REL = Path(".synaptory") / ".orchestrator" / "cycle-holder.json"


@pytest.fixture
def inject_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-inject-protocols.sh"


def _stdin(agent_type: str, session_id: str = "s-delivery") -> str:
    return json.dumps(
        {
            "agent_id": "cyc791-agent-001",
            "agent_type": agent_type,
            "session_id": session_id,
            "transcript_path": "",
            "cwd": "/tmp",
            "hook_event_name": "SubagentStart",
        }
    )


def _holders(project_dir: Path) -> dict:
    path = project_dir / _HOLDER_REL
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("holders") or {}


def _workspace(project_dir: Path) -> None:
    orch = project_dir / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "settings.md").write_text("Engagement: structured\n", encoding="utf-8")


def test_a_delivery_dispatch_records_the_dispatching_session(
    inject_hook, hook_env, run_hook
):
    """Impossible after this change: a Work Unit dispatched by a session that
    the Stop-hook loop then cannot tell apart from any other session sharing
    the project directory."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    _workspace(project_dir)

    result = run_hook(
        inject_hook,
        env={**hook_env, "SYNAPTORY_AUTH_NO_GATE": "1"},
        stdin=_stdin("synaptory:software-engineer"),
    )
    assert result.returncode == 0, result.stderr

    holders = _holders(project_dir)
    assert holders, "SubagentStart left no Cycle-ownership claim"
    assert {h["session_id"] for h in holders.values()} == {"s-delivery"}


def test_an_advisory_dispatch_records_nothing(inject_hook, hook_env, run_hook):
    """A coordination session asking a research-advisor a question must not
    take the Cycle — that would mute the real Engineering Lead, which is #791
    inverted rather than fixed."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    _workspace(project_dir)

    run_hook(
        inject_hook,
        env={**hook_env, "SYNAPTORY_AUTH_NO_GATE": "1"},
        stdin=_stdin("synaptory:research-advisor", session_id="s-orchestration"),
    )
    assert _holders(project_dir) == {}


def test_a_non_synaptory_dispatch_records_nothing(inject_hook, hook_env, run_hook):
    """Other plugins' subagents and the host's own workers are not deliveries."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    _workspace(project_dir)

    run_hook(
        inject_hook,
        env={**hook_env, "SYNAPTORY_AUTH_NO_GATE": "1"},
        stdin=_stdin("general-purpose", session_id="s-elsewhere"),
    )
    assert _holders(project_dir) == {}


def test_the_last_delivery_dispatcher_holds_it(inject_hook, hook_env, run_hook):
    """The holder is the session that most recently dispatched, which is what
    lets a restarted Engineering Lead reclaim its own Cycle by acting."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    _workspace(project_dir)
    env = {**hook_env, "SYNAPTORY_AUTH_NO_GATE": "1"}

    run_hook(inject_hook, env=env, stdin=_stdin("synaptory:software-engineer"))
    run_hook(
        inject_hook,
        env=env,
        stdin=_stdin("synaptory:quality-engineer", session_id="s-after-restart"),
    )
    assert {h["session_id"] for h in _holders(project_dir).values()} == {
        "s-after-restart"
    }
