"""Layer 1 — the GitHub tracker writes as a nameable identity, inspectably.

Four defects are pinned here, all in the story-creation path:

  * The transport hardcoded ``gh``, so every tracker write was attributed to
    whoever's `gh auth` session happened to be on the machine, and an
    organization routing agent writes through a bot identity could not.
  * Request bodies travelled on stdin (``gh api --input -``).  A broker
    standing in for `gh` has to decide whether to mint a write token before
    the command runs and cannot read stdin to do it, so those calls were
    refused outright — milestones and sub-issue links among them.
  * ``add_sub_issue`` sent the GraphQL node id where GitHub's REST API takes
    the integer database id, so every epic-to-story link failed.
  * ``set_issue_type`` and both sub-issue call sites swallowed the failure,
    which is how the three above stayed invisible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# Adapters use relative imports (`from .base import …`), so they must be
# imported through the `tracker` package.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "_shared" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _transport(cli_command: str = ""):
    from tracker.transport.github_transport import GitHubTransport

    return GitHubTransport("h3tech-ai/synaptory-v1", cli_command=cli_command)


def _completed(stdout: str = "", returncode: int = 0):
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = ""
    return result


@pytest.mark.unit
def test_cli_command_defaults_to_gh():
    from tracker.transport.github_transport import resolve_cli_command

    assert resolve_cli_command() == ["gh"]
    assert resolve_cli_command("   ") == ["gh"]


@pytest.mark.unit
def test_configured_cli_command_wins_over_the_environment(monkeypatch):
    """An ambient variable must not silently downgrade a committed identity.

    The usual precedence is the other way round, and here it would be a hole:
    the whole point of the setting is which identity a write is attributed to.
    """
    from tracker.transport.github_transport import CLI_COMMAND_ENV, resolve_cli_command

    monkeypatch.setenv(CLI_COMMAND_ENV, "gh")
    assert resolve_cli_command("h3t-gh-planner") == ["h3t-gh-planner"]
    # The environment still applies when the project declared nothing.
    assert resolve_cli_command("") == ["gh"]
    monkeypatch.setenv(CLI_COMMAND_ENV, "broker --as planner")
    assert resolve_cli_command("") == ["broker", "--as", "planner"]


@pytest.mark.unit
def test_every_write_goes_through_the_configured_command():
    transport = _transport("h3t-gh-planner")
    transport._cli_available = True
    with mock.patch(
        "tracker.transport.github_transport.subprocess.run",
        return_value=_completed(json.dumps({"number": 7})),
    ) as run:
        transport.update_issue(7, title="t")
        transport.create_label("blocked", color="d73a4a")
        transport.close_issue(7)
        transport.list_labels()
    for call in run.call_args_list:
        argv = call.args[0]
        assert argv[0] == "h3t-gh-planner", argv


@pytest.mark.unit
def test_readiness_probe_uses_the_configured_command():
    """`gh auth status` is what tells the caller the CLI is usable at all.

    Probing plain `gh` while writing through a wrapper would report a
    configured identity as offline, or worse, report a personal session as
    healthy when the wrapper is the thing that is broken.
    """
    transport = _transport("h3t-gh-planner")
    with mock.patch(
        "tracker.transport.github_transport.subprocess.run",
        return_value=_completed(),
    ) as run:
        assert transport.check_cli() is True
    assert run.call_args.args[0][:2] == ["h3t-gh-planner", "auth"]


@pytest.mark.unit
def test_api_bodies_are_inline_inspectable_fields_never_stdin():
    transport = _transport()
    transport._cli_available = True
    with mock.patch(
        "tracker.transport.github_transport.subprocess.run",
        return_value=_completed(json.dumps({"number": 3})),
    ) as run:
        transport.create_milestone("Sprint 1", description="first")
    argv = run.call_args.args[0]
    assert "--input" not in argv
    assert run.call_args.kwargs.get("input") is None
    assert "-f" in argv and "title=Sprint 1" in argv
    assert "description=first" in argv


@pytest.mark.unit
def test_numbers_are_typed_fields_so_they_do_not_arrive_quoted():
    transport = _transport()
    transport._cli_available = True
    responses = [
        _completed(json.dumps({"id": 3210987, "number": 42})),  # GET the child
        _completed(json.dumps({"number": 7})),                  # POST the link
    ]
    with mock.patch(
        "tracker.transport.github_transport.subprocess.run",
        side_effect=responses,
    ) as run:
        transport.add_sub_issue(7, 42)
    link_argv = run.call_args_list[-1].args[0]
    # The integer database id, not the issue number and not the node id:
    # GitHub rejects a string here, which failed every link silently.
    assert "-F" in link_argv
    assert "sub_issue_id=3210987" in link_argv
    assert "repos/h3tech-ai/synaptory-v1/issues/7/sub_issues" in link_argv


@pytest.mark.unit
def test_sub_issue_link_refuses_an_issue_with_no_numeric_id():
    from tracker.base import AdapterError

    transport = _transport()
    transport._cli_available = True
    with mock.patch(
        "tracker.transport.github_transport.subprocess.run",
        return_value=_completed(json.dumps({"node_id": "I_kwDOabc", "number": 42})),
    ):
        with pytest.raises(AdapterError, match="numeric id"):
            transport.add_sub_issue(7, 42)


@pytest.mark.unit
def test_issue_type_is_read_back_and_a_dropped_type_raises():
    """GitHub drops the type for a caller without push access, and says 200.

    An unapplied type that reported success is worse than a refusal: the
    backlog then shows every item untyped, and the reads that search by type
    return nothing, with no record of why.
    """
    from tracker.base import AdapterError

    transport = _transport()
    transport._cli_available = True
    applied = [
        _completed(json.dumps({"number": 7})),                        # PATCH
        _completed(json.dumps({"number": 7, "type": {"name": "Story"}})),  # GET
    ]
    with mock.patch(
        "tracker.transport.github_transport.subprocess.run", side_effect=applied
    ) as run:
        transport.set_issue_type(7, "Story")
    patch_argv = run.call_args_list[0].args[0]
    assert "--method" in patch_argv and "PATCH" in patch_argv
    assert "type=Story" in patch_argv

    dropped = [
        _completed(json.dumps({"number": 7})),          # PATCH reports success
        _completed(json.dumps({"number": 7})),          # GET shows no type
    ]
    with mock.patch(
        "tracker.transport.github_transport.subprocess.run", side_effect=dropped
    ):
        with pytest.raises(AdapterError, match="push access"):
            transport.set_issue_type(7, "Story")


@pytest.mark.unit
def test_non_scalar_api_field_is_refused_rather_than_silently_json_encoded():
    from tracker.base import AdapterError

    transport = _transport()
    transport._cli_available = True
    with pytest.raises(AdapterError, match="inspectable fields"):
        transport._run_api("PATCH", "repos/o/r/issues/7", body={"labels": ["a"]})


@pytest.mark.unit
def test_mine_says_a_bot_identity_has_no_user_rather_than_reporting_auth_failure():
    from tracker.base import AdapterError

    transport = _transport("h3t-gh-planner")
    transport._cli_available = True
    with mock.patch(
        "tracker.transport.github_transport.subprocess.run",
        return_value=_completed(returncode=1),
    ):
        with pytest.raises(AdapterError, match="no GitHub user of its own"):
            transport.current_user()


@pytest.mark.unit
def test_health_check_names_the_identity_command():
    transport = _transport("h3t-gh-planner")
    transport._cli_available = True
    assert transport.health_check()["cli_command"] == "h3t-gh-planner"


@pytest.mark.unit
def test_a_type_that_did_not_stick_is_reported_not_swallowed(capsys):
    """This used to `except AdapterError: pass`.

    Creating the item is the caller's real work, so a failed type must not
    fail it — but it must leave a trace, or a whole backlog goes untyped with
    nothing saying why.
    """
    from tracker.base import AdapterError
    from tracker.github_adapter import GitHubAdapter

    adapter = object.__new__(GitHubAdapter)
    adapter._type_ids = {"Story": "IT_kwabc"}
    adapter.transport = mock.Mock()
    adapter.transport.repo = "h3tech-ai/synaptory-v1"
    adapter.transport.set_issue_type.side_effect = AdapterError("dropped: no push access")

    adapter._set_issue_type(7, "Story")
    captured = capsys.readouterr()
    assert "#7" in captured.err and "Story" in captured.err
    assert "no push access" in captured.err

    # A type the repository does not define is reported too, and not attempted.
    adapter.transport.set_issue_type.reset_mock()
    adapter._set_issue_type(9, "Saga")
    assert "no issue type named 'Saga'" in capsys.readouterr().err
    adapter.transport.set_issue_type.assert_not_called()


@pytest.mark.unit
def test_config_carries_the_cli_command_from_yaml(tmp_path):
    """The YAML key is the wiring most likely to rot: the field can exist on
    the dataclass while the parser never fills it, and the project then writes
    as a person while its config says otherwise."""
    from tracker.config import GitHubConfig, TrackerConfig

    assert GitHubConfig().cli_command == ""
    (tmp_path / ".synaptory.yaml").write_text(
        "tracker:\n"
        "  backend: github\n"
        "  github:\n"
        "    repo: h3tech-ai/synaptory-v1\n"
        "    cli_command: h3t-gh-planner\n",
        encoding="utf-8",
    )
    config = TrackerConfig.load(tmp_path)
    assert config.github.cli_command == "h3t-gh-planner"
