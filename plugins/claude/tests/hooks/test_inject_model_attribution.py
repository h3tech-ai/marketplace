"""Layer 2 -- what SubagentStart reports as the dispatch's model (#577).

WHY THIS FILE EXISTS
--------------------
`synaptory-inject-protocols.sh` opened both the control-plane span and the
OTLP span with `--model "${SYNAPTORY_AGENT_MODEL:-claude-sonnet-4-5}"`. Nothing
sets `SYNAPTORY_AGENT_MODEL` on a real dispatch, so the fallback WAS the
behaviour, and it named an id `model-pins.json` has not carried under any tier
since the pins rolled to `claude-sonnet-5` / `claude-opus-4-8`. Every
subagent span in every project therefore recorded a model that no dispatch had
run on since the id was retired -- not a gap in the data, a uniform wrong
answer in it.

WHAT THE HOOK DOES INSTEAD, AND WHY ABSENCE
-------------------------------------------
It omits `--model` entirely. The hook has no observation to report: Claude
Code's SubagentStart stdin carries no model identifier, and #519 measured that
the host's `Agent()` tool takes a tier alias rather than an exact id, so the
pin never reaches the dispatch either. `model_pin_check`'s docstring reads a
missing model as "no recorded provenance at all", and #493's finding is that a
confidently wrong provenance value is worse than an absent one -- an absent one
is legible as a gap, a wrong one is indistinguishable from a measurement.

`SYNAPTORY_AGENT_MODEL` survives as the escape hatch for a caller that DOES
observe the model (an e2e harness, a wrapper that dispatches explicitly), so
the second test pins that the flag still appears when the value is real.

WHAT THIS FILE DOES NOT PROVE
-----------------------------
It says nothing about `Receipt.model`, which is authored by the dispatched
agent and is what `/by-model` and `/cost` actually read. That surface is
guarded by `tests/lib/test_model_pin_binding.py` and, for authored examples,
by #441's work. It also does not prove a real dispatch records nothing -- it
proves this hook sends nothing, which is the only half observable from here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.hook


@pytest.fixture
def synaptory_subagent_stdin() -> str:
    """SubagentStart stdin for a *synaptory* agent.

    The shared `subagent_stdin` fixture carries `agent_type: general-purpose`,
    which #130's namespace gate deliberately excludes from span emission --
    a test using it would assert on a code path that never runs.
    """
    return json.dumps(
        {
            "agent_id": "11111111-2222-3333-4444-555555555555",
            "agent_type": "synaptory:software-engineer",
            "session_id": "test-session-001",
            "prompt_id": "prompt-001",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/tmp",
            "hook_event_name": "SubagentStart",
        }
    )


def _subagent_start_line(telemetry_log: Path) -> str:
    assert telemetry_log.exists(), (
        "the hook never invoked `telemetry subagent-start` -- the #130 "
        "namespace gate or the CLI resolver skipped the span entirely, so "
        "this test would pass vacuously"
    )
    lines = [
        ln
        for ln in telemetry_log.read_text(encoding="utf-8").splitlines()
        if "subagent-start" in ln
    ]
    assert len(lines) == 1, f"expected one subagent-start invocation, got {lines!r}"
    return lines[0]


def test_no_model_is_reported_when_none_is_observed(
    plugin_root: Path, hook_env, synaptory_subagent_stdin, run_hook, tmp_path: Path
):
    """The regression: an unobserved model must be absent, not guessed.

    Asserted on the stub CLI's recorded argv rather than on the script text,
    so a future re-introduction anywhere on the path -- a wrapper, a default
    in `_plugin-env.sh` -- fails here too.
    """
    telemetry_log = tmp_path / "telemetry.log"
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}
    env.pop("SYNAPTORY_AGENT_MODEL", None)

    hook = plugin_root / "hooks" / "synaptory-inject-protocols.sh"
    result = run_hook(hook, env=env, stdin=synaptory_subagent_stdin)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    line = _subagent_start_line(telemetry_log)
    assert "--model" not in line, (
        f"SubagentStart reported a model it never observed: {line!r}. "
        f"Nothing on this path sees the model a dispatch ran on, so the "
        f"flag must be omitted and `subagent_spans.model` left NULL."
    )
    # The rest of the attribution must still be there -- dropping the model
    # is the change; dropping the span is not.
    assert "--role software-engineer" in line, line
    assert "--backend claude" in line, line


def test_an_observed_model_is_still_reported(
    plugin_root: Path, hook_env, synaptory_subagent_stdin, run_hook, tmp_path: Path
):
    """`SYNAPTORY_AGENT_MODEL` remains the escape hatch for a real observation.

    Without this, "omit the flag" could be implemented by deleting it, and a
    harness that genuinely knows which model ran would silently lose the
    ability to say so.

    The value is deliberately NOT a pinned id. The hook passes the variable
    through untouched -- it validates nothing against `model-pins.json` -- and
    naming a real pin here would both imply otherwise and go stale on the next
    re-pin.
    """
    telemetry_log = tmp_path / "telemetry.log"
    env = {
        **hook_env,
        "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log),
        "SYNAPTORY_AGENT_MODEL": "observed-model-id",
    }

    hook = plugin_root / "hooks" / "synaptory-inject-protocols.sh"
    result = run_hook(hook, env=env, stdin=synaptory_subagent_stdin)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    assert "--model observed-model-id" in _subagent_start_line(telemetry_log)


def test_the_otlp_span_carries_no_model_attribute_either(
    plugin_root: Path, hook_env, synaptory_subagent_stdin, run_hook
):
    """The same absence on the OTel path, which feeds the reliability rollups.

    `otel_writer` records `model or ""`, and `cli/internal/cli/traces.go`
    emits the `agent.model` attribute only for a non-empty value -- so an
    empty field here means no attribute on the exported span, not an
    attribute whose value is the empty string.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    hook = plugin_root / "hooks" / "synaptory-inject-protocols.sh"
    result = run_hook(hook, env=hook_env, stdin=synaptory_subagent_stdin)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    events = sorted((project_dir / ".synaptory" / ".orchestrator" / "otel").rglob("*.jsonl"))
    assert events, (
        "the hook wrote no OTel event file; this test would pass vacuously"
    )
    starts = [
        json.loads(ln)
        for path in events
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    starts = [e for e in starts if e.get("event") == "start"]
    assert starts, f"no start event among {[e.get('event') for e in starts]}"
    for event in starts:
        assert event.get("role") == "synaptory:software-engineer", (
            "the reliability rollup accepts only plugin-namespaced roles; "
            f"got {event.get('role')!r}"
        )
        assert event.get("model") == "", (
            f"OTLP start span recorded model {event.get('model')!r}; nothing "
            f"on this path observed one"
        )
