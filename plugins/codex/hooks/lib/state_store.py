#!/usr/bin/env python3
"""Host-neutral state primitives: file locking and atomic JSON writes.

Extracted from `spec_state.py` when SPQ stopped borrowing the Multi-Spec store
(#303/#304/#305). Two stores now coexist:

    spec_state.py                 scrum/kanban, `pipeline-state.json`
                                  (flat v2.0 and the v3.0 `specs` envelope)
    spq_paths.py + friends        SPQ, `.synaptory/.orchestrator/spq/...`

Both need exactly the same two guarantees, and duplicating them would be worse
than the coupling it removes: the flock re-entrancy rule below is subtle enough
that a second copy would drift. So the primitives live here once and both stores
call them.

Generalized off `project_dir` onto an arbitrary path, because SPQ locks a
per-Cycle manifest and a per-workstream execution state, not one file per
project. `state_path`/`lock_path` deliberately do NOT move here: those encode
`.synaptory/.orchestrator/pipeline-state.json`, which is a *pipeline* fact, not
a store primitive. Each store owns its own layout.

Python 3.9 compatible: this file is projected into every host package.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


class StateLockTimeout(RuntimeError):
    """Raised when a state lock could not be acquired in time."""


# Bounded rather than indefinite: SubagentStop runs under a 30s hook budget, so
# a stuck holder must surface as a refusal, never as a hang.
LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.05

# Re-entrancy depth per lock path, per thread. flock is per
# open-file-description, so a second open+flock inside the same thread would
# block against itself: a transaction whose body calls a helper that locks too
# would deadlock. Counting depth makes inner acquisitions no-ops *on this thread
# only*. A process-global depth map would let a second thread skip flock and
# enter the critical section as a nested no-op.
_lock_depth_tls = threading.local()


def _lock_depth() -> Dict[str, int]:
    by_path = getattr(_lock_depth_tls, "by_path", None)
    if by_path is None:
        by_path = {}
        _lock_depth_tls.by_path = by_path
    return by_path


@contextlib.contextmanager
def file_lock(lock_path: str, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Advisory exclusive lock serializing a read-modify-write window.

    While the lock is held, no other process inside the same
    `file_lock(lock_path)` context can read the guarded file, modify it and
    write it back, which is what prevents lost updates.

    Re-entrant within a thread, so a transaction can wrap a body that itself
    locks. A second thread must still acquire flock. Acquisition is bounded:
    after `timeout` seconds it raises `StateLockTimeout` rather than blocking
    forever, so a stuck holder cannot hang a hook.

    No-op on platforms without fcntl (Windows) -- falls back to the racy
    behaviour, acceptable for the current target platforms (macOS + Linux).
    Locks are released on context exit even if the body raises.
    """
    if fcntl is None:
        yield
        return
    key = os.path.abspath(lock_path)
    depth = _lock_depth()
    if depth.get(key, 0) > 0:
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return

    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StateLockTimeout(
                        "could not acquire the state lock within %.1fs: %s"
                        % (timeout, lock_path)
                    )
                time.sleep(LOCK_POLL_S)
        depth[key] = 1
        yield
    finally:
        if acquired:
            depth.pop(key, None)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        else:
            os.close(fd)


@contextlib.contextmanager
def transaction(lock_path: str, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold a lock across a whole read-check-mutate-write window.

    A separate name from `file_lock` because callers read "transaction" as
    intent and "file_lock" as mechanism, and the intent is the load-bearing
    part: locking only the write leaves every caller's read → check → mutate
    window unprotected, so two concurrent advances can both read the same
    state, both pass an anti-replay check, and both write.

        with transaction(lock_path):
            state = read_json(path)
            ...                       # checks that depend on that state
            write_json_atomic(path, state)

    Raises StateLockTimeout if another holder does not release in time.
    """
    with file_lock(lock_path, timeout=timeout):
        yield


def read_json(path: str) -> Dict[str, Any]:
    """Parse a JSON object file. Empty dict when missing, unreadable or invalid.

    Never raises: a corrupt state file must surface as a refusal from the
    caller that understands the schema, not as a traceback from the store.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_json_atomic(path: str, payload: Any, *, mode: int | None = None) -> None:
    """Write JSON atomically: temp file in the TARGET dir, then os.replace.

    Same-directory temp matters -- `os.replace` is only atomic within a
    filesystem, and a temp in /tmp can land on a different one. The temp is
    unlinked on any BaseException so a failed write never leaves a `.tmp`
    behind for a later glob to pick up.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or None, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def now_iso() -> str:
    """UTC timestamp in the form every Synaptory record uses."""
    return datetime.now(timezone.utc).isoformat()
