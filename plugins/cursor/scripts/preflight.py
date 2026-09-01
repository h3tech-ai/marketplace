#!/usr/bin/env python3
"""Prove Synaptory is actually wired into `cursor-agent` before an agent runs.

WHY THIS CANNOT BE A HOOK
=========================
The obvious fix for "the plugin silently did not load" is a startup check
inside the plugin. That cannot work: a plugin that is not loaded has no hook to
fire. `plugin-cursor/hooks/hooks.json` registers `sessionStart`, but
`cursor-agent` never reads it, so nothing in this package gets a chance to
speak. The check has to live OUTSIDE the thing being checked, which is why this
is a script an operator or CI runs, not a hook.

WHAT WENT WRONG (#332, from #323 G4)
====================================
CLAUDE.md and README.md documented:

    ln -sfn .../plugin-cursor ~/.cursor/plugins/local/synaptory

That is an **IDE** convention. `cursor-agent plugin` exposes only
`marketplace`, so with the symlink correct `cursor-agent mcp list` showed only
unrelated servers. Nothing errored.

With no Synaptory surface the agent improvised, and the result LOOKED like
success: correct code written, hand-authored QE-only receipts into the legacy
path, the kernel never advanced, and both Work Units reported `done` while
`spq_state_machine.py read` said `queued`. The missing plugin is the defect;
the fabricated lifecycle state is the danger.

Exit codes: 0 ready, 1 not wired (with the fix), 2 could not tell.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent


def _mcp_config_path(project: Path) -> Path:
    return project / ".cursor" / "mcp.json"


def render_mcp_config(plugin_root: Path, upn: str = "") -> dict:
    """The project-local wiring that actually works with `cursor-agent`."""
    env = {
        "PLUGIN_ROOT": str(plugin_root),
        "SYNAPTORY_IDE": "cursor",
    }
    if upn:
        env["SYNAPTORY_UPN"] = upn
    return {
        "mcpServers": {
            "synaptory": {
                "command": "python3",
                "args": [str(plugin_root / "mcp" / "server.py")],
                "env": env,
            }
        }
    }


def write_wiring(project: Path, plugin_root: Path, upn: str = "") -> Path:
    """Write `.cursor/mcp.json`, MERGING rather than clobbering.

    A project may already declare other MCP servers, and replacing the file to
    add ours would silently remove them.
    """
    path = _mcp_config_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    servers = dict(existing.get("mcpServers") or {})
    servers.update(render_mcp_config(plugin_root, upn)["mcpServers"])
    existing["mcpServers"] = servers
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return path


def _cursor_agent_lists_synaptory() -> tuple[bool | None, str]:
    """(ready, detail). None when `cursor-agent` cannot be asked at all."""
    if not shutil.which("cursor-agent"):
        return None, "cursor-agent is not on PATH"
    try:
        proc = subprocess.run(
            ["cursor-agent", "mcp", "list"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "could not run `cursor-agent mcp list`: %s" % exc
    output = (proc.stdout or "") + (proc.stderr or "")
    if "synaptory" not in output:
        return False, "`cursor-agent mcp list` does not report a synaptory server"
    # Present is not the same as usable. The playbook's rule is to require the
    # ready state, because a server that is listed but failing to start leaves
    # the agent in exactly the improvising state this script exists to prevent.
    for line in output.splitlines():
        if "synaptory" in line:
            if "ready" in line.lower():
                return True, line.strip()
            return False, "synaptory is listed but not ready: %s" % line.strip()
    return False, "synaptory not found in `cursor-agent mcp list`"


def _fix_hint(project: Path) -> str:
    return (
        "Fix:\n"
        "  python3 %s --project %s --write\n"
        "  cd %s && cursor-agent mcp enable synaptory\n"
        "\n"
        "NOT `~/.cursor/plugins/local/synaptory` — that symlink is an IDE\n"
        "convention. `cursor-agent` has no local-plugin mechanism, so it loads\n"
        "nothing from there and says nothing about it."
        % (Path(__file__).resolve(), project, project)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=".", help="the clone to check")
    parser.add_argument("--plugin-root", default=str(PLUGIN_ROOT))
    parser.add_argument("--upn", default=os.environ.get("SYNAPTORY_UPN", ""))
    parser.add_argument("--write", action="store_true",
                        help="write .cursor/mcp.json instead of only checking")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    plugin_root = Path(args.plugin_root).resolve()

    if args.write:
        path = write_wiring(project, plugin_root, args.upn)
        print("wrote %s" % path)
        print("now run: cd %s && cursor-agent mcp enable synaptory" % project)
        return 0

    config = _mcp_config_path(project)
    if not config.is_file():
        print("NOT WIRED: %s does not exist\n\n%s" % (config, _fix_hint(project)),
              file=sys.stderr)
        return 1
    try:
        declared = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print("NOT WIRED: %s is unreadable (%s)\n\n%s"
              % (config, exc, _fix_hint(project)), file=sys.stderr)
        return 1
    if "synaptory" not in (declared.get("mcpServers") or {}):
        print("NOT WIRED: %s declares no synaptory server\n\n%s"
              % (config, _fix_hint(project)), file=sys.stderr)
        return 1

    ready, detail = _cursor_agent_lists_synaptory()
    if ready is None:
        # Config is right but the runtime cannot be asked. Distinct exit code:
        # "I could not tell" must not read as "ready", or this script becomes
        # the same silent pass it was written to prevent.
        print("UNKNOWN: %s is wired, but %s" % (config, detail), file=sys.stderr)
        return 2
    if not ready:
        print("NOT WIRED: %s\n\n%s" % (detail, _fix_hint(project)), file=sys.stderr)
        return 1

    print("ready: %s" % detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
