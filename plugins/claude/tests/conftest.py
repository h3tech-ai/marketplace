"""Pytest fixtures for the plugin module test suite.

Three layers live under plugin-claude/tests/:

  * lib/    — Layer 1 unit tests for plugin Python (no subprocess, no network).
  * hooks/  — Layer 2 integration tests that exec real .sh hooks against a
              stub `synaptory` CLI (this module's `stub_cli` fixture).
  * build/  — Layer 2 tests that exec the build script against a tmp output dir.

Layer 3 (full e2e against a live control plane) lives in e2e/scenarios/
and is run by `./synaptory e2e`, not pytest from here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pytest

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
REPO_ROOT = PLUGIN_ROOT.parent

# Make the plugin's Python helpers importable from L1 tests.
# Two paths are added because modules under hooks/lib/ use sibling-import
# style (e.g. `from story_pipeline import …`) — they're typically loaded
# as scripts. Tests mirror that import style.
for _p in (
    PLUGIN_ROOT,
    PLUGIN_ROOT / "hooks" / "lib",
    # core/scripts (via the plugin-claude symlink): tracker, backend config, and
    # the compose directive expander.
    PLUGIN_ROOT / "skills" / "_shared" / "scripts",
    # The suite's own shared fixture shapes (`_spq_fixture`). On the path
    # because `cycle_records` requires three fields of every admitted unit, and
    # a fixture whose subject is the ledger or the seal should not restate them
    # -- one required field would otherwise edit thirty files.
    HERE / "lib",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ─── The suite must not write into the SHARED runtime tree (#502) ────────────
#
# `hook_env` needs the hooks it execs to resolve a control-plane URL, and
# `hooks/_cp-url.sh` reads that from `${CLAUDE_PLUGIN_ROOT}/hooks/lib/cp-url*`.
# The fixture used to satisfy that by writing `cp-url.local` into the real
# `plugin-claude/hooks/lib/`, saving the old contents and restoring them in a
# `finally`, which fails in two ways that look nothing alike from the outside:
#
#   * `plugin-claude/hooks/lib` is a SYMLINK to `core/lib`, the host-neutral
#     runtime all three hosts compose from, so the suite was stamping shared
#     state rather than `tmp_path`;
#   * save/restore is correct for one run and a lost update for two, and it
#     does not run at all when a run is killed hard -- a pytest timeout, or a
#     Ctrl-C landing between the write and the `try`.
#
# Either way the stamp outlives the run that wrote it, and the damage lands on
# LATER runs, not on the one that caused it: an unstamped source tree resolves
# no CLI, but a stamped one resolves the ambient installed `synaptory`, so
# `test_spq_barrier_e2e.py` starts failing 23 of 38 with Work Units reported
# `blocked`. The file is gitignored, so `git status` is clean and
# `git checkout .` does not remove it, which is what makes it read as a
# mysterious corruption instead of as a test leak.
#
# So the stamp no longer goes anywhere near the repository. `stamped_plugin_root`
# below builds a throwaway plugin root under pytest's tmp dir -- a farm of
# symlinks into the real tree whose only real files are the two directories on
# the way to `hooks/lib/cp-url.local` and the stamp itself -- and `hook_env`
# points `CLAUDE_PLUGIN_ROOT` at that. Nothing to restore, nothing to leak, and
# the OS reclaims it whether or not the run unwinds.
#
# The guard below stays, for the debris already on developers' laptops and as a
# tripwire on anything that starts writing there again.

_SHARED_STAMP = REPO_ROOT / "core" / "lib" / "cp-url.local"

#: What the fixtures stamp. Deliberately NOT a loopback URL: that is what makes
#: fixture debris distinguishable from a deliberate `./synaptory deploy local`
#: stamp, which is always `http://localhost:<port>` or `http://127.0.0.1:<port>`.
#: Only a stamp carrying THIS exact value is ever removed by the guard.
_FIXTURE_STAMP_URL = "https://cp.test"


def _stale_fixture_stamp() -> str:
    """The stray stamp's contents when it is recognisably fixture debris."""
    try:
        found = _SHARED_STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return found if found == _FIXTURE_STAMP_URL else ""


@pytest.fixture(scope="session", autouse=True)
def _no_leaked_control_plane_stamp():
    # Sweep first: a run killed hard by an older version of this suite (or by a
    # concurrent one) leaves debris that poisons every later run in the tree,
    # and the previous guard only checked at session END -- so it saw the
    # debris as pre-existing and left it there for good. A stamp that is not
    # the fixture's own value is somebody's deliberate `deploy local` and is
    # left alone.
    if _stale_fixture_stamp():
        _SHARED_STAMP.unlink(missing_ok=True)
        warnings.warn(
            "removed a stale %r stamp left at %s by an earlier run -- a hard "
            "kill skips fixture teardown. Nothing in this suite writes there "
            "any more (#502)." % (_FIXTURE_STAMP_URL, _SHARED_STAMP),
            stacklevel=1,
        )

    preexisting = _SHARED_STAMP.exists()
    yield
    if preexisting or not _SHARED_STAMP.exists():
        return
    leaked = _SHARED_STAMP.read_text(encoding="utf-8").strip()
    if leaked == _FIXTURE_STAMP_URL:
        _SHARED_STAMP.unlink(missing_ok=True)
        disposition = (
            "The stamp was this suite's own fixture value and has been removed, "
            "so the next run starts clean"
        )
    else:
        disposition = (
            "The stamp is NOT this suite's fixture value, so it has been left "
            "in place rather than deleted -- remove it yourself if it is stale"
        )
    raise AssertionError(
        "this run leaked a control-plane stamp into the SHARED runtime tree: "
        "%s now contains %r, and it did not exist when the session started.\n\n"
        "`plugin-claude/hooks/lib` is a symlink to `core/lib`, so anything "
        "stamping the plugin tree stamps the runtime every host composes from. "
        "Left behind, it makes later runs in this tree resolve a control plane "
        "they should not have -- test_spq_barrier_e2e.py fails 23 of 38 with "
        "Work Units reported `blocked` -- and it is gitignored, so nothing in "
        "`git status` shows it.\n\n"
        "`hook_env` no longer writes there: it stamps a throwaway plugin root "
        "under tmp_path instead (`stamped_plugin_root`). Whatever wrote this "
        "file should do the same.\n\n%s (#502)." % (_SHARED_STAMP, leaked, disposition)
    )


# ─── The suite must not write PROJECT state into the checkout (#380) ─────────
#
# `.synaptory/.orchestrator/` is a project's runtime state: the board, the
# event log, the chain id every process in that project shares. The suite runs
# against projects under `tmp_path`, so it has no business creating one here.
#
# It did. `story_pipeline`'s `story_create` and `forced_transition` events, and
# `wait_for`'s timeout breadcrumb, were emitted with no project dir;
# `synaptory_logger.emit` then addressed them at `host_env.project_dir()`,
# which falls through to `os.getcwd()`. So a plain `./synaptory test` grew a
# `.synaptory/.orchestrator/{events.jsonl,chain-id}` in whatever checkout it
# ran from -- the repository root, or a lane's git worktree.
#
# `.synaptory/*` is gitignored, which is what kept it invisible for as long as
# it lasted: `git status` clean, `git checkout .` no help. The same
# invisibility is why #502 and #568 read as mysterious corruption rather than
# as a test leak, and it is why this guard exists at all -- a leak nobody can
# see is a leak nobody fixes.
#
# The guard is deliberately narrow: `.orchestrator/` specifically. `.synaptory/`
# itself can legitimately be committed in an SPQ project (`.gitignore` keeps
# `cycles/`, `sync/` and friends), so failing on the parent would punish a
# repository for being a Synaptory project.

_PROJECT_STATE_IN_CHECKOUT = REPO_ROOT / ".synaptory" / ".orchestrator"


@pytest.fixture(scope="session", autouse=True)
def _no_project_state_written_into_the_checkout():
    preexisting = _PROJECT_STATE_IN_CHECKOUT.exists()
    yield
    if preexisting or not _PROJECT_STATE_IN_CHECKOUT.exists():
        return
    contents = sorted(p.name for p in _PROJECT_STATE_IN_CHECKOUT.iterdir())
    raise AssertionError(
        "this run created %s, which did not exist when the session started.\n\n"
        "That is project runtime state -- board, event log, chain id -- written "
        "into the CHECKOUT the suite is running from rather than into a "
        "`tmp_path` project. Found: %s.\n\n"
        "The usual cause is a project event emitted without a project dir: "
        "`synaptory_logger.emit(None)` resolves `host_env.project_dir()`, which "
        "ends at `os.getcwd()`. In `story_pipeline` go through "
        "`_emit_project_event`, which drops an unaddressed event instead of "
        "redirecting it; anywhere else, name the project or move the cwd to "
        "`tmp_path`.\n\n"
        "It is not removed for you: it may be sitting in a colleague's worktree "
        "and it is gitignored, so nothing in `git status` will show it (#380)."
        % (_PROJECT_STATE_IN_CHECKOUT, ", ".join(contents) or "(empty)")
    )


# ─── Session-scoped roots ─────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def plugin_root() -> Path:
    return PLUGIN_ROOT


def _mirror(source: Path, into: Path, *, except_for: str) -> None:
    """Fill `into` with one symlink per entry of `source`, minus `except_for`.

    A symlink farm rather than a copy: `plugin-claude/` is 15 MB and the hooks
    under test read real agent, rule, skill and protocol bodies out of it. Only
    the directories on the path to the stamp have to be real, so that writing
    the stamp cannot follow a link back into the repository.
    """
    into.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name == except_for:
            continue
        link = into / entry.name
        if not link.is_symlink() and not link.exists():
            link.symlink_to(entry)


@pytest.fixture(scope="session")
def stamped_plugin_root(tmp_path_factory) -> Path:
    """A disposable `CLAUDE_PLUGIN_ROOT` carrying the fixture's cp-url stamp.

    Everything is a symlink into the real plugin tree except `hooks/`,
    `hooks/lib/` and the `cp-url.local` inside it, so hooks execed against this
    root read genuine bodies but resolve OUR control-plane URL -- and no test
    writes to `core/lib/`. See the #502 note at the top of this file for what
    stamping the real tree cost.

    Session-scoped: ~90 symlinks, built once. The real `hooks/lib/cp-url.local`
    is deliberately not mirrored, so a developer who has run
    `./synaptory deploy local` gets the same URL here as CI does.
    """
    root = tmp_path_factory.mktemp("stamped-plugin-root")
    _mirror(PLUGIN_ROOT, root, except_for="hooks")
    _mirror(PLUGIN_ROOT / "hooks", root / "hooks", except_for="lib")
    _mirror(
        PLUGIN_ROOT / "hooks" / "lib",
        root / "hooks" / "lib",
        except_for="cp-url.local",
    )
    stamp = root / "hooks" / "lib" / "cp-url.local"
    assert not stamp.is_symlink(), f"{stamp} must be a real file, not a link home"
    stamp.write_text(_FIXTURE_STAMP_URL + "\n", encoding="utf-8")
    return root


@pytest.fixture(scope="session")
def stub_cli_path() -> Path:
    """Absolute path to the bash-shim stub `synaptory` binary."""
    p = HERE / "fixtures" / "stub_cli" / "synaptory"
    assert p.is_file(), f"stub CLI missing at {p}"
    return p


# ─── Per-test stub-CLI scaffolding ────────────────────────────────────────────


@dataclass
class StubCli:
    """Snapshot of the stub-CLI environment a test has set up.

    The hook process must see `synaptory` first on `$PATH`. We build a
    per-test bin dir containing a symlink to the shim, plus a per-test
    `bodies_dir` that the shim reads for canned `skills get` responses.
    Tests pass `env` to subprocess.run when invoking hooks.
    """

    bin_dir: Path
    bodies_dir: Path
    env: dict[str, str]

    def add_body(self, name: str, body: str) -> Path:
        """Write a canned body the stub will return for `skills get <name>`.

        `name` follows the seeder's naming (`protocols/receipt-protocol`,
        `rules/synaptory-ux`, `software-engineer/agent`, etc.).
        """
        body_path = self.bodies_dir / f"{name}.md"
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(body, encoding="utf-8")
        return body_path

    def fail_names(self, names: Iterable[str]) -> None:
        """Force the stub to exit 1 for any of these names (partial-failure tests)."""
        self.env["SYNAPTORY_STUB_FAIL_NAMES"] = " ".join(names)


@pytest.fixture
def stub_cli(tmp_path: Path, stub_cli_path: Path) -> StubCli:
    bin_dir = tmp_path / "bin"
    bodies_dir = tmp_path / "bodies"
    bin_dir.mkdir()
    bodies_dir.mkdir()
    # Both channel names point at the same fixture. The resolver chooses one
    # from the plugin stamp, and the fixture reports the matching immutable
    # identity from argv[0].
    (bin_dir / "synaptory").symlink_to(stub_cli_path)
    (bin_dir / "synaptory-local").symlink_to(stub_cli_path)

    env = {
        # Minimal sanitised env — hooks shouldn't need anything else.
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LANG": "C",
        "SYNAPTORY_STUB_BODIES_DIR": str(bodies_dir),
    }
    return StubCli(bin_dir=bin_dir, bodies_dir=bodies_dir, env=env)


@pytest.fixture
def stub_cli_unavailable(tmp_path: Path) -> dict[str, str]:
    """Env with NO stub on $PATH — tests the on-disk fallback path."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LANG": "C",
    }


# ─── Hook invocation helper ───────────────────────────────────────────────────


def _run_hook(
    hook_path: Path,
    *,
    env: dict[str, str],
    stdin: str = "",
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run a hook script and return the full CompletedProcess.

    Tests that want the parsed `additional_context` should call
    `parse_hook_output(result.stdout)` on the returned `.stdout`.
    """
    return subprocess.run(
        ["bash", str(hook_path)],
        input=stdin,
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_hook_output(stdout: str) -> dict | None:
    """Parse a hook's JSON output payload.

    Returns None when stdout is empty (hooks may legitimately exit silently
    when there's nothing to inject).

    Normalises the camelCase ``additionalContext`` key introduced in the Phase 1
    modernisation back to the legacy ``additional_context`` key so that existing
    test assertions continue to pass during the transition.  Both keys are
    present in the returned dict when the hook emits either form.
    """
    s = stdout.strip()
    if not s:
        return None
    d = json.loads(s)
    # Accept both the new camelCase key and the legacy snake_case key.
    if "additionalContext" in d and "additional_context" not in d:
        d["additional_context"] = d["additionalContext"]
    elif "additional_context" in d and "additionalContext" not in d:
        d["additionalContext"] = d["additional_context"]
    return d


@pytest.fixture
def run_hook():
    """Convenience wrapper for invoking a hook script."""
    return _run_hook


@pytest.fixture
def parse_output():
    return parse_hook_output


# ─── Hook env builder ─────────────────────────────────────────────────────────


@pytest.fixture
def hook_env(stamped_plugin_root: Path, stub_cli: StubCli, tmp_path: Path):
    """Default hook env: stub CLI on PATH + plugin root + tmp project dir.

    `CLAUDE_PLUGIN_ROOT` is the disposable `stamped_plugin_root`, which already
    carries a `hooks/lib/cp-url.local`, so hooks resolve a URL without honouring
    SYNAPTORY_CONTROL_PLANE_URL and without this fixture writing to the
    repository. Nothing to restore afterwards -- see the #502 note above.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return {
        **stub_cli.env,
        "CLAUDE_PLUGIN_ROOT": str(stamped_plugin_root),
        "CLAUDE_PROJECT_DIR": str(project_dir),
    }


@pytest.fixture
def subagent_stdin() -> str:
    """JSON payload Claude Code sends to a SubagentStart hook on stdin."""
    return json.dumps(
        {
            "agent_id": "test-agent-001",
            "agent_type": "general-purpose",
            "session_id": "test-session-001",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/tmp",
            "hook_event_name": "SubagentStart",
        }
    )


# ─── SPQ board fixture, produced by the state machine ─────────────────────────


@pytest.fixture
def spq_board():
    """Open a real SPQ Cycle and return `(project_dir, board_state)`.

    **The board is produced by `spq_state_machine`, never hand-written**, and
    that is the whole point of the fixture existing (#514, the lesson from
    #509). Three of the four independently-found "reader is blind to the SPQ
    board" defects survived their own test suites because the fixtures wrote
    `{"build_mode": "spq", "current_stories": [...]}` straight into
    `pipeline-state.json` — a shape SPQ has not produced since #303. A fixture
    that puts the board where the blind reader already looks cannot reach the
    defect.

    What the real lifecycle produces instead: `initialize` → `transition(COMMIT)`
    → `open_cycle` leaves `pipeline-state.json` as a two-key mode + identity
    POINTER and writes the board to
    `spq/cycles/<cycle-id>/workstreams/<ws>/execution-state.json`.

    Signature: `spq_board(project_dir, units=..., goal=..., workstream_id=...)`.
    `units` are `open_cycle` work-unit dicts (`{"id": ..., "title": ...}`).
    """

    def _open(project_dir, units=None, goal: str = "cycle goal",
              workstream_id: str | None = None):
        import spq_state_machine as spq

        project = Path(project_dir)
        project.mkdir(parents=True, exist_ok=True)
        cfg = project / ".synaptory.yaml"
        if not cfg.exists():
            cfg.write_text('build_mode: "spq"\n', encoding="utf-8")
        spq.initialize(str(project), workstream_id=workstream_id)
        spq.transition(str(project), "COMMIT")
        state = spq.open_cycle(
            str(project),
            1,
            goal,
            list(units if units is not None else [{"id": "WU-1", "title": "first unit"}]),
            workstream_id=workstream_id,
        )
        return project, state

    return _open


@pytest.fixture
def spq_repeated_failure(spq_board):
    """An SPQ board carrying a *recurring* failure class, ladder-recorded.

    The same failure reason is pushed through `story_pipeline.record_retry`
    twice — the real BEA5-F1 ladder, not a hand-typed `retry_failure_hashes`
    list — and persisted through `spq_state_machine._write_state`, so the hash
    list lands in the execution state exactly as a real Cycle would leave it.

    Returns `(project_dir, unit_id, reason_hash)`.
    """

    def _seed(project_dir, unit_id: str = "WU-1",
              reason: str = "flaky integration test: timeout on /health"):
        import spq_state_machine as spq
        import story_pipeline

        project, _ = spq_board(project_dir, units=[{"id": unit_id, "title": "unit"}])
        state = spq.read_state(str(project))
        story_pipeline.record_retry(state, unit_id, "qe", reason)
        story_pipeline.record_retry(state, unit_id, "qe", reason)
        spq._write_state(str(project), state)
        persisted = spq.read_state(str(project))
        story = next(s for s in persisted["current_stories"] if s["id"] == unit_id)
        return project, unit_id, story["retry_failure_hashes"]["qe"][0]

    return _seed


# ─── Test-isolation safety ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_synaptory_state(monkeypatch, tmp_path):
    """Make sure tests don't leak into the developer's real ~/.synaptory/.

    Every test gets a fresh $HOME inside tmp_path.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _neutralise_runtime_stamp(monkeypatch, tmp_path):
    """No test may depend on whether this laptop ran `./synaptory deploy local`.

    `host_env.RUNTIME_STAMP_DIR` is `core/lib/`, and that is where
    `./synaptory deploy local` writes a gitignored `cp-url.local` (#320). Left
    pointing there, channel resolution — and so every CLI-resolution test —
    would answer differently on a laptop that has deployed locally than in CI.
    Tests that care about a stamp set one inside this empty directory.

    IN-PROCESS ONLY. A test that execs the runtime as a script gets a child
    that recomputes `RUNTIME_STAMP_DIR` from its own location; those tests use
    `stamp_free_runtime` below, which is the same invariant for a subprocess.
    """
    stamp_dir = tmp_path / "runtime-stamp"
    stamp_dir.mkdir(exist_ok=True)
    try:
        import host_env
    except ImportError:  # pragma: no cover - shared runtime not on sys.path
        return
    monkeypatch.setattr(host_env, "RUNTIME_STAMP_DIR", stamp_dir)


@pytest.fixture
def stamp_runtime(monkeypatch, tmp_path):
    """Give the shared runtime a cp-url stamp beside itself, as a host does.

    Counterpart to `_neutralise_runtime_stamp`: any test whose subject resolves
    a CLI or a control plane has to say which channel it is standing in, because
    "no stamp" now resolves nothing rather than falling through to production
    (#320). `local=True` writes the gitignored `cp-url.local` name that
    `./synaptory deploy local` uses; otherwise the immutable build stamp.
    """

    def _stamp(url: str, *, local: bool = False) -> Path:
        import host_env

        directory = tmp_path / "runtime-stamp"
        directory.mkdir(parents=True, exist_ok=True)
        name = "cp-url.local" if local else "cp-url"
        (directory / name).write_text(url + "\n", encoding="utf-8")
        monkeypatch.setattr(host_env, "RUNTIME_STAMP_DIR", directory)
        return directory

    return _stamp


# ─── Stamp-free runtime for subprocess-spawning tests ─────────────────────────

#: Env vars that let something OTHER than `RUNTIME_STAMP_DIR` name a channel or
#: a CLI. A child process inherits the developer's shell, so they are cleared
#: alongside the stamp rather than left to decide the answer.
_CHANNEL_ENV_VARS = (
    "SYNAPTORY_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_ROOT",
    "SYNAPTORY_CLI_BIN",
    "SYNAPTORY_CHANNEL",
    "SYNAPTORY_CONTROL_PLANE_URL",
    "SYNAPTORY_CP_ENV",
)


@dataclass
class StampFreeRuntime:
    """A copy of `core/` whose `lib/` carries no cp-url stamp.

    `_neutralise_runtime_stamp` monkeypatches `host_env.RUNTIME_STAMP_DIR`
    in-process, which does not reach a test that execs the runtime as a script:
    in that child, `RUNTIME_STAMP_DIR` is the module's own directory -- the real
    `core/lib/` -- so a developer's `./synaptory deploy local` stamp makes
    `channel_signal()` name a channel and `gate_emitter._resolve_cli()` resolve
    the ambient installed `synaptory`. Gate emission then really runs and
    perturbs the pipeline state such tests assert on.

    Real copies, not symlinks: `RUNTIME_STAMP_DIR` is
    `Path(__file__).resolve().parent`, and `resolve()` walks a symlink straight
    back to the stamped directory it is meant to avoid.
    """

    lib: Path

    def script(self, module) -> str:
        """The path of an already-imported `core/lib` module inside this copy."""
        name = Path(module.__file__).name
        path = self.lib / name
        assert path.is_file(), f"{name} missing from the stamp-free runtime copy"
        return str(path)

    def env(self, **extra: str) -> dict[str, str]:
        """`os.environ` with every channel-naming variable cleared."""
        env = {k: v for k, v in os.environ.items() if k not in _CHANNEL_ENV_VARS}
        # sys.path[0] is the script's own directory, so the copy already wins
        # for sibling imports; drop the real lib anyway so nothing subtler can
        # reach around it.
        real_lib = str((REPO_ROOT / "core" / "lib").resolve())
        pythonpath = [
            p for p in env.get("PYTHONPATH", "").split(os.pathsep)
            if p and str(Path(p).resolve()) != real_lib
        ]
        if pythonpath:
            env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        else:
            env.pop("PYTHONPATH", None)
        env.update(extra)
        return env


@pytest.fixture(scope="session")
def stamp_free_runtime(tmp_path_factory) -> StampFreeRuntime:
    """Session-scoped stamp-free copy of the shared runtime. See StampFreeRuntime."""
    import host_env

    core = REPO_ROOT / "core"
    root = tmp_path_factory.mktemp("stamp-free-core")
    lib = root / "lib"
    lib.mkdir()
    # The excluded names come from the runtime itself, so a new stamp filename
    # cannot be added there and silently keep leaking into these children.
    for entry in (core / "lib").iterdir():
        if entry.is_dir() or entry.name in host_env.CP_URL_FILENAMES:
            continue
        shutil.copy2(entry, lib / entry.name)
    # `story_pipeline._resolve_tracker_cli` probes `<parent>/scripts/tracker/`
    # relative to its own directory, so the sibling has to stay reachable or
    # the copy would silently lose tracker sync that the real tree has.
    (root / "scripts").symlink_to(core / "scripts", target_is_directory=True)
    return StampFreeRuntime(lib=lib)


# ─── The source-region registry, doubled ─────────────────────────────────────
#
# `open_cycle` requires a GRANTED reservation from the control plane's region
# registry and refuses on anything else -- an unreachable registry, a project
# with no id, a collision. That is `SC-MTH-012` and issue #643: two clones each
# hold a sealed declaration the other cannot see until somebody pushes, so
# nothing local can establish separation and "I could not ask" is not "no
# collision".
#
# THE CONSEQUENCE FOR TESTS is that a Cycle cannot be opened without a
# registry, and there is no control plane in a unit test. So the registry is
# DOUBLED here, at the seam that resolves it, rather than the product growing a
# mode where the requirement does not apply. A test that wants to exercise the
# refusals overrides this with its own double (see
# `tests/lib/test_region_reservation.py`), because a later `monkeypatch` wins.
#
# Doubling the resolution rather than `reserve` itself keeps the double honest:
# the fixture answers as a registry would, so the code under test still parses
# a verdict, still distinguishes granted from refused, and a change to that
# parsing is not silently bypassed here.


#: One definition of the double, imported rather than repeated. Each suite
#: installs it for itself: `plugin-claude/tests/conftest.py`'s autouse fixture
#: reaches only its own subtree, and the combined run passing locally while CI
#: failed was the conftest module-name collision leaking it across.
def _install_registry_double(monkeypatch, tmp_path) -> None:
    """Load the shared installer by path, not by package import.

    `from fixtures...` requires this directory on `sys.path`, which it is not
    for every invocation of the suite -- and a conftest that fails to import
    takes the whole run with it.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "region_registry_double",
        str(HERE / "fixtures" / "region_registry_double.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def _region_registry_double(monkeypatch, tmp_path) -> None:
    """A per-test registry that grants, remembers, and refuses an overlap.

    A yes-man double does not work here and the reason is instructive: the
    dispatch recheck asks the registry whether this Cycle's grant is still
    live, so a double that answered "reserved" and then listed nothing made
    every dispatch refuse with `region_contradicted`. The double has to be
    consistent with itself or the suite exercises the wrong refusal.

    Per test, so one test's reservations cannot collide with another's, and a
    test wanting the refusals overrides it with its own double -- the later
    `monkeypatch` wins.
    """
    _install_registry_double(monkeypatch, tmp_path)
