"""#818 — an artifact outside the declared scope is named at the dispatch.

`receipt_validator` checks an artifact is a string, exists, and is non-empty.
It never asks whether the path is inside the unit's declared `path_scope`, so
the first thing that notices is `_eval_admitted_set_closed` at the Checkpoint
-- where the declaration is sealed, `revise_manifest` is prohibited, and the
review rounds have already been paid for.

Warning, not refusal: an artifact outside the scope is sometimes deliberate.
The cost being closed is that nobody was told while it was still cheap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import advance_kernel as ak  # noqa: E402

from _spq_fixture import open_project, unit  # noqa: E402

pytestmark = pytest.mark.unit


def _project(tmp_path):
    # `unit()` derives `api/wu-1/` from the id; declare it explicitly so the
    # test reads as a statement about scope rather than about the fixture.
    return open_project(
        tmp_path / "p", units=[unit("WU-1", path_scope=["api/wu-1/"])]
    )[0]


def test_an_artifact_inside_the_declared_scope_is_not_flagged(tmp_path):
    project = _project(tmp_path)
    assert ak.unclaimed_artifacts(
        str(project), "WU-1", ["api/wu-1/routers/auth.py"]
    ) == []


def test_an_artifact_outside_the_declared_scope_is_named(tmp_path):
    """The reporting engagement's case: a records file written mid-Cycle."""
    project = _project(tmp_path)
    assert ak.unclaimed_artifacts(
        str(project),
        "WU-1",
        ["api/wu-1/routers/auth.py", "records/wu-1-open-questions.md"],
    ) == ["records/wu-1-open-questions.md"]


def test_it_agrees_with_the_predicate_the_barrier_uses(tmp_path):
    """Telling a dispatch one thing and the close another would be worse
    than saying nothing."""
    import path_scope

    project = _project(tmp_path)
    artifacts = ["api/wu-1/a.py", "docs/elsewhere.md", "web/src/x.ts"]

    assert ak.unclaimed_artifacts(str(project), "WU-1", artifacts) == (
        path_scope.uncovered(["api/wu-1/"], artifacts)
    )


def test_a_unit_the_declaration_does_not_admit_is_not_judged(tmp_path):
    project = _project(tmp_path)
    assert ak.unclaimed_artifacts(str(project), "WU-UNKNOWN", ["anything.md"]) == []


def test_a_non_spq_project_is_never_judged(tmp_path):
    """Scrum and Kanban have no declared path scope to be outside of."""
    project = tmp_path / "scrum"
    project.mkdir()
    (project / ".synaptory.yaml").write_text('build_mode: "scrum"\n', encoding="utf-8")
    assert ak.unclaimed_artifacts(str(project), "US-1", ["anywhere/at/all.py"]) == []


@pytest.mark.parametrize("artifacts", [None, [], "not-a-list", [""], [123]])
def test_unreadable_artifact_input_is_fail_open(tmp_path, artifacts):
    """An advisory lane must never become the thing that breaks a dispatch."""
    project = _project(tmp_path)
    assert ak.unclaimed_artifacts(str(project), "WU-1", artifacts) == []


def test_an_unreadable_declaration_is_fail_open(tmp_path):
    project = tmp_path / "spq-no-seal"
    project.mkdir()
    (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")
    assert ak.unclaimed_artifacts(str(project), "WU-1", ["x.py"]) == []
