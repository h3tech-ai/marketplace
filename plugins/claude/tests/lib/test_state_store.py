"""Layer 1 — `state_store`: the locking and atomic-write primitives.

Extracted from `spec_state.py` when SPQ stopped borrowing the Multi-Spec store
(#303/#304/#305), so two stores now share them. That sharing is the reason
these need direct tests: previously the properties were only exercised
incidentally through `spec_state.state_transaction`, and a regression in the
flock re-entrancy rule would surface as a deadlock in whichever store happened
to hit it first.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

import state_store as ss

needs_flock = pytest.mark.skipif(ss.fcntl is None, reason="fcntl flock is required")


# ── re-entrancy: per thread, never per process ─────────────────────────────


@needs_flock
def test_lock_is_reentrant_within_one_thread(tmp_path: Path):
    """A transaction whose body locks again must not deadlock against itself.

    flock is per open-file-description, so a naive second acquire would block
    forever on the same thread.
    """
    lock = str(tmp_path / "s.lock")
    with ss.file_lock(lock, timeout=1.0):
        with ss.file_lock(lock, timeout=1.0):
            with ss.transaction(lock, timeout=1.0):
                pass


@needs_flock
def test_lock_is_not_reentrant_across_threads(tmp_path: Path):
    """A second thread must not inherit the first thread's nested depth.

    A process-global depth map would treat a concurrent thread as an inner
    acquisition and let both into the critical section -- silently, with the
    test still passing.
    """
    lock = str(tmp_path / "s.lock")
    barrier = threading.Barrier(2)
    inside = 0
    max_inside = []
    guard = threading.Lock()
    errors = []

    def worker():
        nonlocal inside
        barrier.wait()
        try:
            with ss.file_lock(lock, timeout=5.0):
                with guard:
                    inside += 1
                    max_inside.append(inside)
                time.sleep(0.05)
                with guard:
                    inside -= 1
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert max(max_inside) == 1, "two threads were inside the critical section"


@needs_flock
def test_depth_is_released_when_the_body_raises(tmp_path: Path):
    """A leaked depth entry would make every later acquire on this thread a
    no-op, disabling the lock without any visible failure."""
    lock = str(tmp_path / "s.lock")
    with pytest.raises(ValueError):
        with ss.file_lock(lock, timeout=1.0):
            raise ValueError("boom")
    assert not ss._lock_depth().get(os.path.abspath(lock))


# ── bounded acquisition ────────────────────────────────────────────────────


@needs_flock
def test_acquisition_times_out_rather_than_hanging(tmp_path: Path):
    """SubagentStop runs under a 30s hook budget, so a stuck holder has to
    surface as a refusal, never as a hang."""
    lock = str(tmp_path / "s.lock")
    holding = threading.Event()
    release = threading.Event()

    def holder():
        with ss.file_lock(lock, timeout=5.0):
            holding.set()
            release.wait(timeout=10)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert holding.wait(timeout=5)
        started = time.monotonic()
        with pytest.raises(ss.StateLockTimeout) as excinfo:
            with ss.file_lock(lock, timeout=0.3):
                pass
        assert time.monotonic() - started < 5, "timeout was not honoured"
        assert lock in str(excinfo.value), "the error must name the lock file"
    finally:
        release.set()
        t.join(timeout=10)


def test_lock_creates_its_parent_directory(tmp_path: Path):
    lock = str(tmp_path / "deep" / "nested" / "s.lock")
    with ss.file_lock(lock, timeout=1.0):
        pass
    assert os.path.exists(lock)


# ── atomic writes ──────────────────────────────────────────────────────────


def test_write_is_atomic_and_round_trips(tmp_path: Path):
    path = str(tmp_path / "out" / "state.json")
    ss.write_json_atomic(path, {"a": 1, "b": [2, 3]})
    assert ss.read_json(path) == {"a": 1, "b": [2, 3]}
    assert json.loads(Path(path).read_text(encoding="utf-8"))["a"] == 1


def test_write_leaves_no_temp_file_behind_on_failure(tmp_path: Path):
    """A stranded `.tmp` is worse than a failed write: a later glob picks it
    up as if it were state."""
    path = str(tmp_path / "state.json")

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        ss.write_json_atomic(path, {"bad": Unserializable()})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, leftovers


def test_write_replaces_rather_than_truncating(tmp_path: Path):
    """os.replace is only atomic within a filesystem, so the temp must live in
    the TARGET directory. Asserted by writing over an existing file and
    confirming no window where the file is absent or short."""
    path = tmp_path / "state.json"
    ss.write_json_atomic(str(path), {"v": 1})
    first = path.stat().st_ino
    ss.write_json_atomic(str(path), {"v": 2})
    assert ss.read_json(str(path)) == {"v": 2}
    assert path.stat().st_ino != first, "in-place truncation, not a replace"


def test_mode_is_applied_when_requested(tmp_path: Path):
    """The sealed Cycle manifest is written read-only (#303)."""
    path = str(tmp_path / "manifest.json")
    ss.write_json_atomic(path, {"sealed": True}, mode=0o444)
    assert oct(os.stat(path).st_mode)[-3:] == "444"


# ── read never raises ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content", ["", "not json", "[1, 2, 3]", '"a string"', "null", "{"]
)
def test_read_returns_empty_dict_for_anything_that_is_not_an_object(
    tmp_path: Path, content: str
):
    """A corrupt state file must surface as a refusal from the caller that
    understands the schema, not as a traceback from the store. A JSON array is
    valid JSON but not a state object, so it is treated as absent too."""
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    assert ss.read_json(str(path)) == {}


def test_read_of_a_missing_file_is_empty(tmp_path: Path):
    assert ss.read_json(str(tmp_path / "nope.json")) == {}


def test_now_iso_is_utc_and_parseable(tmp_path: Path):
    from datetime import datetime

    stamp = ss.now_iso()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, "timestamps must carry an offset"


# ── the delegation is real, not a copy ─────────────────────────────────────


def test_spec_state_delegates_rather_than_duplicating():
    """Two copies of the flock rule would drift, and the drift would show up as
    a deadlock in whichever store hit it first."""
    import spec_state

    assert spec_state._file_lock is ss.file_lock
    assert spec_state.StateLockTimeout is ss.StateLockTimeout
    assert not hasattr(spec_state, "fcntl"), (
        "spec_state should no longer own the flock import"
    )
