"""Runtime selection on the real dispatch path (#396 finding 1, #343, #345).

`select_runtime` and `build_envelope` shipped as component shapes with no
production caller: `execute_dispatch` minted a local attempt and returned a
receipt contract, and nothing ever chose a profile or built an envelope.
Auto/Prefer/Pin could not affect a real dispatch, a host could not route to
another runtime family, and the worker could never receive a kernel-created
envelope. Every downstream story was blocked on that missing call.

These pin the three outcomes, because the difference between them is the
design:

  inert     not SPQ, policy absent or disabled, or NOBODY PROBED this machine.
            The existing dispatch path continues untouched.
  denied    the policy admits no profile that can serve the role. Refused,
            because running it on whatever happens to be present is the silent
            degradation EP-12 forbids.
  selected  the envelope is built and returned on the contract.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import _spq_fixture
import pipeline_board
from hooks.lib import advance_kernel as ak

# Imported by the SAME name the kernel uses internally (`import
# runtime_selector`), not through `hooks.lib`. Those are two distinct module
# objects in `sys.modules`, so patching the wrong one silently patches nothing
# and the test that thought it injected a failure observes the happy path.
import runtime_selector as rsel  # noqa: E402  isort:skip

CONFIG_GOVERNED = """build_mode: spq
project_id: taskflow

runtimes:
  enabled: true
  policy_version: 1
  allowed_profiles:
    - claude-local-v1
    - codex-local-v1
"""

CONFIG_UNGOVERNED = """build_mode: spq
project_id: taskflow
"""


def _project(tmp_path: Path, config: str) -> Path:
    """A governed project with a REAL open Cycle and a real git checkout.

    Both halves are load-bearing, and neither was here before #640. The fixture
    used to hand-write `{"build_mode": "spq", "lifecycle_state":
    "CYCLE_EXECUTION", "current_stories": [...]}` straight into
    `pipeline-state.json` -- the shape SPQ stopped producing at #303, which
    `test_board_reader_census.py` records as the reason three blind-reader
    defects survived their own suites -- so `next_action` selected nothing and
    every case below stopped at `next_action_mismatch` before it reached
    selection at all.

    The git checkout is needed because `source_revision` is a required envelope
    field resolved from the repository, and the Cycle is needed because
    `cycle_id` and `manifest_hash` are the SPQ binding. With both present the
    happy path is reachable HERE rather than only in
    `tests/hooks/test_dispatch_envelope_wiring.py`, which is what lets
    `selected` be asserted as an outcome rather than inferred from a denial one
    step past it.
    """
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    (project / ".synaptory.yaml").write_text(config, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(project), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(project), check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", "fixture"],
        cwd=str(project), check=True,
    )
    _spq_fixture.open_project(
        project,
        units=[{"id": "WU-001", "title": "wire selection into dispatch"}],
        goal="wire runtime selection into the dispatch path",
    )
    return project


def _snapshot(project: Path, *entries: tuple[str, bool]) -> None:
    path = project.joinpath(*rsel.AVAILABILITY_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "probed_at": "2026-09-02T00:00:00Z",
        "reports": [
            {"profile_id": pid, "available": ok, "reason": ""}
            for pid, ok in entries
        ],
    }), encoding="utf-8")


def _dispatch(project: Path):
    return ak.execute_dispatch(str(project), "WU-001", role="se")


def test_a_probed_governed_project_selects_a_profile(tmp_path):
    """Selection reaches a real dispatch, names the profile, and RUNS.

    Was an `envelope_failed` assertion, for two reasons that #640 removed. The
    fixture had no Cycle and no git checkout, so four required envelope fields
    were empty; and `runtime_contracts` required `workstream_id` non-empty
    while `advance_kernel._cycle_binding` writes it empty on purpose now that
    `SPD-194` has retired the Workstream, which refused EVERY governed dispatch
    regardless of the fixture. With both fixed, the outcome asserted here is
    the one the module docstring calls `selected` -- previously reachable only
    from `tests/hooks/test_dispatch_envelope_wiring.py`, so this file described
    three outcomes and could observe two.
    """
    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", True), ("codex-local-v1", True))

    decision = _dispatch(project)
    assert decision.allowed is True, (
        "a fully governed dispatch with an open Cycle, a probed runtime and a "
        "git checkout was refused: %r / %r"
        % (decision.reason, decision.extra.get("runtime_selection"))
    )

    account = decision.extra.get("runtime_selection") or {}
    assert account.get("state") == "selected", account
    assert account.get("profile_id") == "claude-local-v1", account
    assert account.get("runtime_family") == "claude-code", account

    # The envelope is the authority the worker runs under, so it has to be ON
    # the contract rather than merely buildable.
    envelope = (decision.extra.get("receipt_contract") or {}).get("dispatch_envelope")
    assert envelope, decision.extra.get("receipt_contract")
    assert envelope["adapter_profile_id"] == "claude-local-v1", envelope
    assert envelope["cycle_id"], "the SPQ binding must reach the envelope"
    assert envelope["manifest_hash"], "the SPQ binding must reach the envelope"
    assert envelope["source_revision"], envelope
    # Retained and empty, which is the whole shape of the retirement: an old
    # bridge still decodes the key, and nothing claims a lane exists.
    assert envelope["workstream_id"] == "", envelope

    story = _story(project)
    assert story["state"] == "in_progress", story


def test_no_probe_snapshot_leaves_the_existing_path_alone(tmp_path):
    """Absence of a probe is not evidence of availability.

    Assuming a runtime is present because nobody looked is the failure the
    snapshot exists to prevent; denying instead would break every opted-in
    project the moment this shipped. So: inert, with the missing step named.
    """
    project = _project(tmp_path, CONFIG_GOVERNED)

    decision = _dispatch(project)
    assert decision.allowed is True, decision.reason
    account = decision.extra.get("runtime_selection") or {}
    assert account.get("state") == "inert", account
    assert "runtimes doctor" in account.get("reason", ""), account
    assert "dispatch_envelope" not in (
        decision.extra.get("receipt_contract") or {}
    )


def test_a_project_that_never_opted_in_is_untouched(tmp_path):
    project = _project(tmp_path, CONFIG_UNGOVERNED)
    _snapshot(project, ("claude-local-v1", True))

    decision = _dispatch(project)
    assert decision.allowed is True, decision.reason
    assert (decision.extra.get("runtime_selection") or {}).get("state") == "inert"


def test_nothing_available_denies_rather_than_degrading(tmp_path):
    """A probe that found nothing is a real answer, unlike no probe at all.

    Running the dispatch anyway on whatever is present is exactly the silent
    degradation EP-12 forbids, so this refuses.
    """
    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", False), ("codex-local-v1", False))

    decision = _dispatch(project)
    assert decision.allowed is False, (
        "a dispatch ran with no available runtime: %r"
        % (decision.extra.get("runtime_selection"),)
    )
    assert (decision.extra.get("runtime_selection") or {}).get("state") == "denied"


def test_the_snapshot_reader_distinguishes_absent_from_empty(tmp_path):
    """`None` and `()` are different answers and the kernel branches on it."""
    project = _project(tmp_path, CONFIG_GOVERNED)
    assert rsel.load_availability(str(project)) is None
    _snapshot(project)
    assert rsel.load_availability(str(project)) == ()


# ─── a denial must not consume the dispatch ─────────────────────────────────


def _board_path(project: Path) -> Path:
    """The Cycle's board -- NOT `pipeline-state.json`, which is a pointer here.

    Resolved through `pipeline_board.read_board`, the sanctioned accessor, so
    this file cannot become the next entry in `test_board_reader_census.py`.
    """
    return Path(pipeline_board.read_board(str(project)).source)


def _state(project: Path) -> dict:
    return json.loads(_board_path(project).read_text(encoding="utf-8"))


def _story(project: Path) -> dict:
    return _state(project)["current_stories"][0]


def test_a_denied_dispatch_leaves_the_state_file_untouched(tmp_path):
    """The refusal must refuse, not consume.

    Selection used to run AFTER `_write_state`, so a denial returned
    allowed=False while the Work Unit was already `in_progress` with a live
    dispatch and attempt binding on disk. The retry then hit the
    already-started guard, which makes the advertised refusal strictly worse
    than having allowed the run. My first version of this test asserted only
    the response, which is exactly why it did not see this.
    """
    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", False), ("codex-local-v1", False))
    before = _state(project)

    decision = _dispatch(project)
    assert decision.allowed is False, decision.extra.get("runtime_selection")

    after = _state(project)
    assert after == before, (
        "a denied dispatch mutated the board. Story state: %r"
        % [(u["id"], u["state"]) for u in after.get("current_stories", [])]
    )
    story = after["current_stories"][0]
    assert story["state"] == "queued", story
    assert not story.get("mcp_active_dispatches"), (
        "a denied dispatch left a live binding behind, so the retry will hit "
        "the already-started guard: %r" % story.get("mcp_active_dispatches")
    )


def test_a_denied_dispatch_can_be_retried_once_the_cause_is_fixed(tmp_path):
    """The consequence that makes the state assertion matter.

    The retry has to prove that the first denial consumed nothing: the second
    dispatch is evaluated afresh and gets past the availability denial it was
    refused on. A consumed dispatch would instead come back
    `dispatch_already_started` and never reach selection at all.

    It now retries all the way to `selected` rather than to the next unmet
    envelope requirement, because the fixture carries a real Cycle and a real
    checkout (#640). That is a stronger statement about the retry, not a weaker
    one: the whole dispatch completes on the second attempt.
    """
    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", False))
    first = _dispatch(project)
    assert first.allowed is False
    assert (first.extra.get("runtime_selection") or {}).get("state") == "denied"
    assert _story(project)["state"] == "queued", (
        "the denial consumed the dispatch, so the retry below proves nothing"
    )

    _snapshot(project, ("claude-local-v1", True))
    retried = _dispatch(project)
    assert retried.code != ak.DISPATCH_ALREADY_STARTED, (
        "the first denial consumed the dispatch: the retry never reached "
        "selection"
    )
    assert (retried.extra.get("runtime_selection") or {}).get("state") == (
        "selected"
    ), retried.extra.get("runtime_selection")
    assert _story(project)["state"] == "in_progress", _story(project)


# ─── governed inputs fail closed, unconfigured ones stay inert ──────────────


def test_a_corrupt_probe_snapshot_denies_rather_than_falling_back(tmp_path):
    """Present and unreadable is not "nobody probed".

    Reporting it as absent made a corrupt snapshot indistinguishable from no
    snapshot, so a governed project silently ran the legacy path.
    """
    project = _project(tmp_path, CONFIG_GOVERNED)
    path = project.joinpath(*rsel.AVAILABILITY_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    decision = _dispatch(project)
    assert decision.allowed is False, (
        "a corrupt snapshot let the dispatch run outside its declared authority"
    )
    assert _state(project)["current_stories"][0]["state"] == "queued"


def test_an_unreadable_explicit_profile_registry_denies(tmp_path, monkeypatch):
    """An explicit registry that cannot be read must not fall through to a
    less specific one. Silently using a different registry than the operator
    named is how a project pinning its profiles ends up on the built-in table.
    """
    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", True))
    bad = tmp_path / "profiles.json"
    bad.write_text("not json at all", encoding="utf-8")
    monkeypatch.setenv(rsel.PROFILES_ENV, str(bad))

    decision = _dispatch(project)
    assert decision.allowed is False, (
        "an unreadable explicit registry was skipped in favour of another one"
    )
    assert _state(project)["current_stories"][0]["state"] == "queued"


def test_a_selection_failure_denies_rather_than_running_ungoverned(
    tmp_path, monkeypatch
):
    """Any failure to establish the authority is a refusal, not permission."""
    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", True))

    def _boom(**_kw):
        raise RuntimeError("selector exploded")

    monkeypatch.setattr(rsel, "select_runtime", _boom)
    decision = _dispatch(project)
    assert decision.allowed is False, (
        "a selector exception left the dispatch allowed and ungoverned"
    )
    assert (decision.extra.get("runtime_selection") or {}).get("state") == "error"


def test_an_unbuildable_envelope_denies_the_selected_dispatch(
    tmp_path, monkeypatch
):
    """A selection with no envelope is an authority nobody can carry."""
    import runtime_contracts as rc

    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", True))

    def _boom(**_kw):
        raise RuntimeError("envelope builder exploded")

    monkeypatch.setattr(rc, "build_envelope", _boom)
    decision = _dispatch(project)
    assert decision.allowed is False, (
        "a selected dispatch ran with no envelope to run under"
    )
    account = decision.extra.get("runtime_selection") or {}
    assert account.get("state") == "envelope_failed", account


def test_an_absent_snapshot_is_still_inert_not_denied(tmp_path):
    """The migration case the fail-closed rule must not break: a governed
    project nobody has probed keeps working on the legacy path."""
    project = _project(tmp_path, CONFIG_GOVERNED)
    decision = _dispatch(project)
    assert decision.allowed is True, decision.reason
    assert (decision.extra.get("runtime_selection") or {}).get("state") == "inert"


# ─── present-but-invalid governed inputs, in every shape ────────────────────


def test_a_nonexistent_explicit_registry_denies(tmp_path, monkeypatch):
    """A SET override is authoritative even when its target is missing.

    Skipping it and selecting from the project or built-in registry is worse
    than refusing: the operator named a registry, and running against a
    different one is the silent substitution EP-12 forbids. The Go loader
    refuses the same case, and the previous guard on `Path.exists()` did not.
    """
    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", True))
    monkeypatch.setenv(rsel.PROFILES_ENV, str(tmp_path / "nowhere.json"))

    decision = _dispatch(project)
    assert decision.allowed is False, (
        "a missing explicit registry was skipped and another one selected from"
    )
    assert _state(project)["current_stories"][0]["state"] == "queued"


def test_a_registry_with_one_bad_entry_denies_rather_than_partially_loading(
    tmp_path, monkeypatch
):
    """A registry half of whose entries were dropped is not the registry
    anyone reviewed."""
    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", True))
    registry = tmp_path / "profiles.json"
    registry.write_text(json.dumps({"profiles": [
        {
            "profile_id": "claude-local-v1",
            "runtime_family": "claude-code",
            "placement": "local",
            "capability_profiles": ["producer"],
            "capabilities": ["workspace.write"],
        },
        "this is not a profile record",
    ]}), encoding="utf-8")
    monkeypatch.setenv(rsel.PROFILES_ENV, str(registry))

    decision = _dispatch(project)
    assert decision.allowed is False, (
        "a registry with a malformed entry was partially loaded and used"
    )


def test_a_non_boolean_availability_denies(tmp_path):
    """`bool("false")` is True, so coercing this field made a snapshot that
    reported a runtime as UNAVAILABLE select it anyway."""
    project = _project(tmp_path, CONFIG_GOVERNED)
    path = project.joinpath(*rsel.AVAILABILITY_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "probed_at": "2026-09-02T00:00:00Z",
        "reports": [{"profile_id": "claude-local-v1", "available": "false"}],
    }), encoding="utf-8")

    decision = _dispatch(project)
    assert decision.allowed is False, (
        "a runtime reported unavailable as the string \"false\" was selected"
    )
    assert _state(project)["current_stories"][0]["state"] == "queued"


def test_a_malformed_snapshot_report_denies(tmp_path):
    """Discarding a bad report is the same hazard one level up: a snapshot
    half of whose reports were dropped is not the probe anyone ran."""
    project = _project(tmp_path, CONFIG_GOVERNED)
    path = project.joinpath(*rsel.AVAILABILITY_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "probed_at": "2026-09-02T00:00:00Z",
        "reports": [
            {"profile_id": "claude-local-v1", "available": True},
            "not a report",
        ],
    }), encoding="utf-8")

    decision = _dispatch(project)
    assert decision.allowed is False, (
        "a snapshot with a malformed report was partially loaded and used"
    )


def test_a_conventional_project_registry_may_be_absent(tmp_path):
    """The distinction the refusals must not lose: an override the operator SET
    is authoritative, while the conventional project path is simply not in use
    when it is absent."""
    project = _project(tmp_path, CONFIG_GOVERNED)
    _snapshot(project, ("claude-local-v1", True))
    assert not project.joinpath(*rsel.PROFILES_RELPATH).exists()

    decision = _dispatch(project)
    # An absent conventional path is not in use, so the shared fixture registry
    # is loaded and selection RESOLVES a profile. What matters here is that a
    # profile was chosen at all: a refused registry would have denied with
    # `no-eligible-profile` instead.
    account = decision.extra.get("runtime_selection") or {}
    assert account.get("state") == "selected", account
    assert account.get("profile_id") == "claude-local-v1", account


# ─── the loaders' own contract, where the guards live ────────────────────────
#
# The dispatch-level tests above prove the KERNEL denies. They do not prove
# WHICH guard refused: removing the `isinstance` check on profile records left
# them green, because the next line then raised an AttributeError on a string
# and the kernel's blanket handler denied anyway. Right outcome, wrong reason,
# and a guard covered only by accident is not covered.


def test_load_profiles_refuses_a_mixed_registry(tmp_path, monkeypatch):
    registry = tmp_path / "profiles.json"
    registry.write_text(json.dumps({"profiles": [
        # Entry 0 must be genuinely VALID, or the refusal names it and this
        # test stops covering the non-object case it is about.
        {
            "profile_id": "claude-local-v1",
            "runtime_family": "claude-code",
            "placement": "local",
            "pinned_version": "PIN-AT-PHASE-B",
            "capability_profiles": ["producer"],
            "capabilities": ["workspace.write"],
        },
        "this is not a profile record",
    ]}), encoding="utf-8")
    monkeypatch.setenv(rsel.PROFILES_ENV, str(registry))
    with pytest.raises(rsel.InvalidRuntimeInput) as caught:
        rsel.load_profiles(str(tmp_path))
    assert "entry 1" in str(caught.value), caught.value


def test_load_profiles_refuses_a_record_with_no_profile_id(tmp_path, monkeypatch):
    registry = tmp_path / "profiles.json"
    registry.write_text(
        json.dumps({"profiles": [{"runtime_family": "claude-code"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(rsel.PROFILES_ENV, str(registry))
    with pytest.raises(rsel.InvalidRuntimeInput):
        rsel.load_profiles(str(tmp_path))


def test_load_profiles_refuses_a_missing_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setenv(rsel.PROFILES_ENV, str(tmp_path / "nowhere.json"))
    with pytest.raises(rsel.InvalidRuntimeInput) as caught:
        rsel.load_profiles(str(tmp_path))
    assert "no such file" in str(caught.value), caught.value


def test_load_availability_refuses_a_non_boolean(tmp_path):
    project = tmp_path / "proj"
    path = project.joinpath(*rsel.AVAILABILITY_RELPATH)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(
        {"reports": [{"profile_id": "claude-local-v1", "available": "false"}]}
    ), encoding="utf-8")
    with pytest.raises(rsel.InvalidRuntimeInput) as caught:
        rsel.load_availability(str(project))
    assert "not a boolean" in str(caught.value), caught.value


def test_load_availability_refuses_a_malformed_report(tmp_path):
    project = tmp_path / "proj"
    path = project.joinpath(*rsel.AVAILABILITY_RELPATH)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"reports": [
        {"profile_id": "claude-local-v1", "available": True},
        "not a report",
    ]}), encoding="utf-8")
    with pytest.raises(rsel.InvalidRuntimeInput) as caught:
        rsel.load_availability(str(project))
    assert "report 1" in str(caught.value), caught.value


def test_load_profiles_refuses_a_malformed_but_well_shaped_record(
    tmp_path, monkeypatch
):
    """The case the string-entry test could not reach.

    A record that IS a dict with an id passed the loader and became one
    ineligible candidate at selection time, so a registry containing invalid
    authority was still partially loaded. Go's `readProfileFile` refuses the
    whole file; this now does too, by running the shared
    `runtime_contracts.validate_profile` over every record.
    """
    registry = tmp_path / "profiles.json"
    registry.write_text(json.dumps({"profiles": [
        {
            "profile_id": "claude-local-v1",
            "runtime_family": "claude-code",
            "placement": "local",
            "pinned_version": "PIN-AT-PHASE-B",
            "capability_profiles": ["producer"],
            "capabilities": ["workspace.write"],
        },
        {
            "profile_id": "made-up-v1",
            "runtime_family": "not-a-runtime-family",
            "placement": "local",
            "pinned_version": "",
            "capability_profiles": [],
            "capabilities": "not-a-list",
        },
    ]}), encoding="utf-8")
    monkeypatch.setenv(rsel.PROFILES_ENV, str(registry))
    with pytest.raises(rsel.InvalidRuntimeInput) as caught:
        rsel.load_profiles(str(tmp_path))
    assert "made-up-v1" in str(caught.value), caught.value


def test_a_missing_certified_table_says_so_instead_of_denying_the_policy(
    tmp_path, monkeypatch
):
    """An absent table is a PACKAGING defect, and it used to be swallowed into
    an empty registry. `select_runtime` denies an empty registry with
    `no-eligible-profile`, which reads as a policy problem, so a composed host
    shipped without the table blamed the operator's configuration."""
    monkeypatch.setattr(rsel, "_certified_table_candidates", lambda: ())
    monkeypatch.delenv(rsel.PROFILES_ENV, raising=False)
    with pytest.raises(rsel.InvalidRuntimeInput) as caught:
        rsel.load_profiles(str(tmp_path))
    assert "not in this installation" in str(caught.value), caught.value


def test_an_unknown_backend_does_not_fabricate_a_family(tmp_path):
    """Refusing a dispatch because the vocabulary grew a value this table has
    not learned would be worse than not enforcing, so an unknown backend
    projects to "" and the guard stays quiet."""
    import advance_kernel as ak_direct

    assert ak_direct._receipt_runtime_family({"backend": "something-new"}) == ""
    assert ak_direct._receipt_runtime_family({}) == ""


def test_an_explicit_runtime_family_on_the_receipt_wins(tmp_path):
    """A bridge-produced receipt names the runtime directly; the legacy
    `backend` is the fallback projection, not the authority."""
    import advance_kernel as ak_direct

    assert ak_direct._receipt_runtime_family(
        {"backend": "claude", "runtime_family": "codex"}
    ) == "codex"
