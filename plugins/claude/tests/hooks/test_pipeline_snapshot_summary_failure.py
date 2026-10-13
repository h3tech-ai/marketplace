"""Layer 2 — the Stop hook must not hide a failed summary build (#847).

`synaptory-pipeline-snapshot.sh` rebuilds `.synaptory/pipeline-summary.json` on
every Stop. It used to run the builder as `... 2>/dev/null || true`, so a
failure left no trace anywhere and (before the builder learned to delete its
own stale output) status kept reading a 19-day-old file. The hook still must
never block or fail the Stop event; it now records why the build failed.

The failure is a REAL one, not a stub builder: the summary's destination is a
directory, so the atomic replace cannot land. That is the builder's own failure
path, end to end through the real hook script.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.hook

LOG = Path(".synaptory") / ".orchestrator" / "summary-build.log"
SUMMARY = Path(".synaptory") / "pipeline-summary.json"


def _project(hook_env: dict[str, str]) -> Path:
    project = Path(hook_env["CLAUDE_PROJECT_DIR"])
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True)
    (receipts / "US-001-qe.json").write_text(json.dumps({
        "task": "t", "agent": "quality-engineer", "backend": "claude",
        "model": "sonnet", "story_id": "US-001", "role": "qe",
        "findings": ["a legacy plain-string finding"],
    }), encoding="utf-8")
    return project


def test_a_failed_summary_build_is_logged_and_the_hook_still_exits_zero(
    plugin_root: Path, hook_env: dict[str, str], run_hook
):
    project = _project(hook_env)
    (project / SUMMARY).mkdir()  # nothing can be replaced onto a directory

    result = run_hook(
        plugin_root / "hooks" / "synaptory-pipeline-snapshot.sh", env=hook_env, stdin=""
    )

    assert result.returncode == 0, f"the Stop event must never fail: {result.stderr!r}"
    log = project / LOG
    assert log.is_file(), "the failed build left no trace"
    text = log.read_text(encoding="utf-8")
    assert "build_summary.py exited" in text
    assert "Error:" in text, f"the builder's own message was not captured: {text!r}"
    assert "summary-build.log" in result.stderr
    # The rest of the hook's job is unaffected by the failure.
    assert (project / ".synaptory" / ".orchestrator" / "last-session.md").is_file()


def test_a_successful_build_writes_the_summary_and_clears_an_old_failure_log(
    plugin_root: Path, hook_env: dict[str, str], run_hook
):
    project = _project(hook_env)
    log = project / LOG
    log.write_text("2000-01-01T00:00:00Z build_summary.py exited 1\n", encoding="utf-8")

    result = run_hook(
        plugin_root / "hooks" / "synaptory-pipeline-snapshot.sh", env=hook_env, stdin=""
    )

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    summary = json.loads((project / SUMMARY).read_text(encoding="utf-8"))
    assert summary["findings"]["summary"]["unknown"] == 1
    assert not log.exists(), "a log of a failure that has since healed would mislead"
