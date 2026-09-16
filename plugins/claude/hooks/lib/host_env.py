"""Host-neutral environment lookups for the shared runtime.

`core/` is consumed by three hosts, so it should not read a variable named after
one of them. It did, and the tell was that the other two hosts had to set the
Claude-named variable to make shared code work:

    plugin-codex/.../mcp_server.py:  os.environ.setdefault("CLAUDE_PLUGIN_ROOT", ...)

Resolution order is SYNAPTORY_* first, then the CLAUDE_* name, then a default.
The legacy names stay supported indefinitely: Claude Code sets CLAUDE_PROJECT_DIR
and CLAUDE_PLUGIN_ROOT itself, so they are that host's real inputs, not
deprecated spellings.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit

PROJECT_DIR_VARS = ("SYNAPTORY_PROJECT_DIR", "CLAUDE_PROJECT_DIR")
PLUGIN_ROOT_VARS = ("SYNAPTORY_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")

CP_URL_PLACEHOLDER = "SYNAPTORY_CP_URL_PLACEHOLDER"
#: The stamp pair, in precedence order. `cp-url.local` is gitignored and written
#: by `./synaptory deploy local`; `cp-url` is the immutable build stamp.
CP_URL_FILENAMES = ("cp-url.local", "cp-url")

#: The runtime's OWN stamp directory, used when no host set a plugin root.
#:
#: `hooks/_cp-url.sh` derives its root from the script's own location, so a
#: source-tree hook reads the local stamp; this module only read the env vars,
#: so the same source-tree call resolved no stamp at all and every downstream
#: channel decision defaulted to production (#320). The stamp pair sits beside
#: this file in every layout -- `core/lib/` in the source tree, `hooks/lib/` in
#: all three composed host packages -- so the module's own directory is the
#: equivalent signal. Module-level so tests can point it at an empty directory
#: instead of depending on whether the developer ran `./synaptory deploy local`.
RUNTIME_STAMP_DIR = Path(__file__).resolve().parent


def _first(names, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "")
        if value and value.strip():
            return value
    return default


def project_dir(default: Optional[str] = None) -> str:
    """The project directory, or `default` (cwd when omitted)."""
    return _first(PROJECT_DIR_VARS) or (
        default if default is not None else os.getcwd()
    )


def plugin_root(default: str = "") -> str:
    """The host's plugin/runtime root, or `default`."""
    return _first(PLUGIN_ROOT_VARS, default)


def _read_stamp(directory: Path) -> str:
    """First usable cp-url value in `directory`, or "" when there is none."""

    value, _unreadable = _read_stamp_state(directory)
    return value


def _read_stamp_state(directory: Path) -> Tuple[str, bool]:
    """`(value, unreadable)` for `directory`'s stamp.

    ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS (#396). `_read_stamp` collapses
    them, which is right for its callers: they want a URL and "" means they do
    not have one. It is wrong for a caller deciding whether a control plane
    EXISTS, because a stamp that is present and cannot be read is an install
    whose integrity is unknown, not a project that reports nowhere. A file that
    is not there raises the same `OSError` as one whose permissions deny it, so
    the two are separated by asking whether the path exists.
    """
    unreadable = False
    for name in CP_URL_FILENAMES:
        path = directory / name
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            try:
                if path.exists():
                    unreadable = True
            except OSError:
                unreadable = True
            continue
        if value and value != CP_URL_PLACEHOLDER:
            return value, unreadable
    return "", unreadable


def control_plane_stamp_state() -> Tuple[str, str]:
    """`(state, detail)` where state is `stamped`, `unstamped` or `unreadable`.

    The tri-state a caller needs when the QUESTION is whether a control plane
    exists rather than what its URL is. `plugin_control_plane_url` answers the
    second and cannot answer the first: it returns "" for a tree with no stamp,
    for a placeholder stamp, and for a stamp it could not open, and only the
    first two mean "this addresses nothing".
    """
    detail = ""
    unreadable = False
    root = plugin_root().strip()
    if root:
        value, missed = _read_stamp_state(Path(root) / "hooks" / "lib")
        unreadable = unreadable or missed
        if value:
            return "stamped", "cp-url stamp (%s)" % value
    value, missed = _read_stamp_state(RUNTIME_STAMP_DIR)
    unreadable = unreadable or missed
    if value:
        return "stamped", "cp-url stamp (%s)" % value
    if unreadable:
        detail = "a cp-url stamp exists and could not be read"
        return "unreadable", detail
    return "unstamped", detail


def plugin_control_plane_url() -> str:
    """Return the active plugin's immutable URL, preferring its local stamp.

    Two places are consulted, in order: the stamp under a host-provided plugin
    root, then the stamp beside this module (`RUNTIME_STAMP_DIR`). The second
    is what makes a source-tree call agree with `hooks/_cp-url.sh` — and it also
    covers Codex, whose shared scripts live under `runtime/scripts` while its
    stamp is written to `hooks/lib`, so only one of the two lookups can hit.
    """

    root = plugin_root().strip()
    if root:
        value = _read_stamp(Path(root) / "hooks" / "lib")
        if value:
            return value
    return _read_stamp(RUNTIME_STAMP_DIR)


def is_loopback_url(raw: str) -> bool:
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return False
    return host == "localhost" or host == "::1" or host.startswith("127.")


def channel_signal() -> tuple[bool, str]:
    """``(is_local, source)``. `source` is "" when nothing here decides.

    An empty source means this machine has told us nothing about which control
    plane the runtime belongs to. That is NOT the same as "production", and
    callers that address a control plane must not treat it as such — see
    `gate_emitter._resolve_cli`.
    """

    stamped = plugin_control_plane_url()
    if stamped:
        return is_loopback_url(stamped), "cp-url stamp (%s)" % stamped
    root = plugin_root().replace("\\", "/")
    if "/plugins/local/" in f"/{root.strip('/')}/":
        return True, "plugin root under plugins/local/"
    # Explicit channel is a fallback for source-tree tools with no composed
    # plugin stamp. A real installed plugin's stamp always wins.
    if os.environ.get("SYNAPTORY_CHANNEL", "").strip() == "local":
        return True, "SYNAPTORY_CHANNEL=local"
    return False, ""


def local_plugin_runtime() -> bool:
    """Whether the active plugin is the isolated local-test channel."""

    return channel_signal()[0]


def plugin_channel_source() -> str:
    """What decided local-vs-production, or "" when nothing did.

    Callers that only need a directory or a display string can keep using
    `local_plugin_runtime()`, whose "no signal → not local" default is
    harmless. Callers that SEND data somewhere need to know the difference
    between "production" and "unknown" (#320).
    """

    return channel_signal()[1]


def cli_channel() -> str:
    return "local" if local_plugin_runtime() else "stable"


def state_dir() -> str:
    """Channel-specific CLI/plugin state root."""

    return os.path.expanduser(
        "~/.synaptory-local" if local_plugin_runtime() else "~/.synaptory"
    )


def default_agent_backend() -> str:
    """Default receipt `backend` for a newly initialized project.

    Cursor sessions export `SYNAPTORY_IDE=cursor` (agents.md, sessionStart).
    Without that, keep `claude` so Claude Code and unspecified hosts stay
    unchanged.
    """
    ide = (
        os.environ.get("SYNAPTORY_IDE") or os.environ.get("SYNAPTORY_HOST") or ""
    ).strip().lower()
    if ide == "cursor":
        return "cursor"
    return "claude"
