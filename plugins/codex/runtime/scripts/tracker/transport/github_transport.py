"""GitHub transport — gh CLI for issue/milestone/label operations.

MCP support is deferred to runtime detection. This module provides
the CLI fallback that works in all contexts (agent sessions and hooks).
"""

# Lazy annotations: `-> dict | list` on _run_api is otherwise evaluated at
# class-definition time and crashes module import on Python 3.9 (the plugin
# supports 3.9+; hooks resolve whatever python3 the host has).
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from ..base import AdapterError, AdapterAuthError, AdapterOfflineError

CLI_COMMAND_ENV = "SYNAPTORY_GITHUB_CLI"


def resolve_cli_command(configured: str = "") -> list[str]:
    """Return the argv prefix that speaks the `gh` command line.

    Defaults to plain ``gh``.  An organization that attributes agent activity
    to a bot identity rather than to whoever's laptop is running points this
    at a broker wrapper that mints a scoped token and enforces what that
    identity may do.  Any command accepting `gh`'s argument grammar works;
    this module knows nothing about a particular broker.

    A configured value wins over the environment.  The reverse order would let
    an ambient variable silently downgrade the identity a repository has
    committed to, and the identity is the whole point.
    """
    raw = configured.strip() or os.environ.get(CLI_COMMAND_ENV, "").strip()
    if not raw:
        return ["gh"]
    parts = shlex.split(raw)
    if not parts:
        raise AdapterError(
            f"GitHub CLI command {raw!r} is empty after parsing; "
            f"set a command such as `gh` or a broker wrapper"
        )
    return parts


class GitHubTransport:
    """Transport layer for GitHub operations via gh CLI."""

    def __init__(self, repo: str, cli_command: str = ""):
        """Initialize with owner/repo string (e.g. 'h3tech-ai/anchor')."""
        self.repo = repo
        self.cli = resolve_cli_command(cli_command)
        self._cli_available: Optional[bool] = None

    def check_cli(self) -> bool:
        """Check if gh CLI is installed and authenticated."""
        if self._cli_available is not None:
            return self._cli_available
        try:
            result = subprocess.run(
                [*self.cli, "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            self._cli_available = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._cli_available = False
        return self._cli_available

    def health_check(self) -> dict:
        cli = self.check_cli()
        return {
            "status": "ok" if cli else "offline",
            "mcp": False,  # MCP detection deferred
            "cli": cli,
            # Which command, so an operator can see whether this project is
            # acting as a person or as a bot identity.
            "cli_command": " ".join(self.cli),
            "repo": self.repo,
        }

    def current_user(self) -> str:
        """Resolve the authenticated gh user's login (for `--mine`).

        GitHub stores assignees as logins, so the cross-backend
        `currentUser()` sentinel must be resolved to a real login before
        filtering — comparing the literal sentinel matches nothing.
        """
        try:
            out = self._run([*self.cli, "api", "user", "--jq", ".login"])
        except AdapterError as exc:
            # A bot identity has no `/user`: an app installation token
            # authenticates an installation, not a person.  Say so, rather
            # than reporting it as an auth failure the operator would go and
            # try to fix.
            raise AdapterError(
                f"--mine cannot be resolved through {' '.join(self.cli)!r}: an "
                f"app or bot identity has no GitHub user of its own. Filter by "
                f"an explicit assignee instead. ({exc})"
            ) from exc
        login = out.strip()
        if not login:
            raise AdapterError(
                "Could not resolve the authenticated GitHub user for --mine "
                "(gh api user returned no login). Run `gh auth login`."
            )
        return login

    # ── Issues ───────────────────────────────────────────────

    def create_issue(self, title: str, body: str = "",
                     labels: Optional[list[str]] = None,
                     milestone: Optional[int] = None,
                     milestone_title: Optional[str] = None) -> dict:
        """Create a GitHub issue. Returns the issue JSON."""
        cmd = [*self.cli, "issue", "create", "-R", self.repo,
               "--title", title, "--body", body]
        if labels:
            cmd.extend(["--label", ",".join(labels)])
        if milestone_title:
            cmd.extend(["--milestone", milestone_title])
        elif milestone is not None:
            cmd.extend(["--milestone", str(milestone)])
        result = self._run(cmd)
        # gh issue create returns the URL, not JSON. Fetch the issue.
        url = result.strip()
        issue_number = url.rstrip("/").split("/")[-1]
        return self.get_issue(int(issue_number))

    def get_issue(self, number: int) -> dict:
        cmd = [*self.cli, "issue", "view", str(number), "-R", self.repo,
               "--json", "number,title,body,state,labels,milestone,url"]
        return json.loads(self._run(cmd))

    def update_issue(self, number: int, **fields) -> dict:
        """Update issue fields (title, body, state, labels, milestone)."""
        cmd = [*self.cli, "issue", "edit", str(number), "-R", self.repo]
        if "title" in fields:
            cmd.extend(["--title", fields["title"]])
        if "body" in fields:
            cmd.extend(["--body", fields["body"]])
        if "add_labels" in fields and fields["add_labels"]:
            cmd.extend(["--add-label", ",".join(fields["add_labels"])])
        if "remove_labels" in fields and fields["remove_labels"]:
            cmd.extend(["--remove-label", ",".join(fields["remove_labels"])])
        if "milestone" in fields:
            cmd.extend(["--milestone", str(fields["milestone"])])
        self._run(cmd)
        return self.get_issue(number)

    def close_issue(self, number: int) -> dict:
        self._run([*self.cli, "issue", "close", str(number), "-R", self.repo])
        return self.get_issue(number)

    def reopen_issue(self, number: int) -> dict:
        self._run([*self.cli, "issue", "reopen", str(number), "-R", self.repo])
        return self.get_issue(number)

    def list_issues(self, state: str = "all",
                    labels: Optional[list[str]] = None,
                    milestone: Optional[str] = None,
                    limit: int = 200) -> list[dict]:
        cmd = [*self.cli, "issue", "list", "-R", self.repo,
               "--state", state, "--limit", str(limit),
               "--json", "number,title,body,state,labels,milestone,url"]
        if labels:
            cmd.extend(["--label", ",".join(labels)])
        if milestone:
            cmd.extend(["--milestone", milestone])
        return json.loads(self._run(cmd))

    def search_issues(self, query: str, limit: int = 100,
                      state: str = "all") -> list[dict]:
        cmd = [*self.cli, "issue", "list", "-R", self.repo,
               "--search", query, "--state", state, "--limit", str(limit),
               "--json", "number,title,body,state,labels,milestone,url"]
        return json.loads(self._run(cmd))

    # ── Labels ───────────────────────────────────────────────

    def create_label(self, name: str, color: str = "",
                     description: str = "") -> dict:
        cmd = [*self.cli, "label", "create", name, "-R", self.repo, "--force"]
        if color:
            cmd.extend(["--color", color])
        if description:
            cmd.extend(["--description", description])
        self._run(cmd)
        return {"name": name, "color": color, "description": description}

    def list_labels(self) -> list[dict]:
        cmd = [*self.cli, "label", "list", "-R", self.repo,
               "--json", "name,color,description", "--limit", "200"]
        return json.loads(self._run(cmd))

    def delete_label(self, name: str) -> None:
        self._run([*self.cli, "label", "delete", name, "-R", self.repo, "--yes"])

    # ── Milestones ───────────────────────────────────────────

    def create_milestone(self, title: str, description: str = "",
                         due_date: Optional[str] = None) -> dict:
        """Create a milestone. Returns milestone data."""
        body = {"title": title}
        if description:
            body["description"] = description
        if due_date:
            body["due_on"] = due_date
        result = self._run_api(
            "POST", f"repos/{self.repo}/milestones",
            body=body,
        )
        return result

    def list_milestones(self, state: str = "open") -> list[dict]:
        return self._run_api(
            "GET", f"repos/{self.repo}/milestones?state={state}&per_page=100",
        )

    def get_milestone(self, number: int) -> dict:
        return self._run_api("GET", f"repos/{self.repo}/milestones/{number}")

    def close_milestone(self, number: int) -> dict:
        return self._run_api(
            "PATCH", f"repos/{self.repo}/milestones/{number}",
            body={"state": "closed"},
        )

    def update_milestone(self, number: int, **fields) -> dict:
        return self._run_api(
            "PATCH", f"repos/{self.repo}/milestones/{number}",
            body=fields,
        )

    # ── Sub-issues (via API) ─────────────────────────────────

    def add_sub_issue(self, parent_number: int, child_number: int) -> dict:
        """Add a sub-issue relationship using GitHub's sub-issues API.

        ``sub_issue_id`` is the issue's integer database id, not its GraphQL
        node id, and not its number.  Sending the node id makes GitHub reject
        the call with a validation error on every single link.
        """
        return self._run_api(
            "POST", f"repos/{self.repo}/issues/{parent_number}/sub_issues",
            body={"sub_issue_id": self._get_issue_id(child_number)},
        )

    def _get_issue_id(self, issue_number: int) -> int:
        """Get the integer database id for an issue number."""
        issue = self._run_api("GET", f"repos/{self.repo}/issues/{issue_number}")
        issue_id = issue.get("id") if isinstance(issue, dict) else None
        if not isinstance(issue_id, int):
            raise AdapterError(
                f"issue #{issue_number} in {self.repo} returned no numeric id, "
                f"so it cannot be linked as a sub-issue"
            )
        return issue_id

    # ── Issue Types (GraphQL) ────────────────────────────────

    def list_issue_types(self) -> list[dict]:
        """List available issue types for this repo. Returns [{"id": ..., "name": ...}]."""
        owner, repo = self.repo.split("/", 1)
        result = self._run_graphql(
            '{ repository(owner:"%s", name:"%s") { issueTypes(first:20) { nodes { id name description color } } } }'
            % (owner, repo)
        )
        nodes = result.get("data", {}).get("repository", {}).get("issueTypes", {}).get("nodes", [])
        return nodes

    def set_issue_type(self, issue_number: int, type_name: str) -> dict:
        """Set the issue type on an issue, by type name.

        REST carries the type name directly, which keeps this off the GraphQL
        mutation surface a governed identity has no business holding open.

        GitHub applies the type only for a caller with push access and
        **drops it silently otherwise**, so the write is read back and a
        mismatch is raised.  An unapplied type that reported success is worse
        than a refusal: the backlog then shows every item as untyped with
        nothing recording why.
        """
        self._run_api(
            "PATCH", f"repos/{self.repo}/issues/{issue_number}",
            body={"type": type_name},
        )
        issue = self._run_api("GET", f"repos/{self.repo}/issues/{issue_number}")
        applied = ((issue or {}).get("type") or {}).get("name") if isinstance(issue, dict) else None
        if applied != type_name:
            raise AdapterError(
                f"issue #{issue_number} type is {applied!r} after requesting "
                f"{type_name!r}; GitHub drops the type unless the caller has "
                f"push access to {self.repo}"
            )
        return issue if isinstance(issue, dict) else {}

    def get_owner_id(self) -> str:
        """Get the owner (org/user) node ID for this repo."""
        owner, repo = self.repo.split("/", 1)
        result = self._run_graphql(
            '{ repository(owner:"%s", name:"%s") { owner { id } } }' % (owner, repo)
        )
        return result.get("data", {}).get("repository", {}).get("owner", {}).get("id", "")

    def create_issue_type(self, owner_id: str, name: str,
                          description: str = "", color: str = "GRAY") -> dict:
        """Create a custom issue type. Requires admin:org scope."""
        result = self._run_graphql(
            'mutation { createIssueType(input: { ownerId: "%s", name: "%s", '
            'description: "%s", isEnabled: true, color: %s }) '
            '{ issueType { id name color } } }'
            % (owner_id, name, description, color)
        )
        return result.get("data", {}).get("createIssueType", {}).get("issueType", {})

    # ── Internal ─────────────────────────────────────────────

    def _run(self, cmd: list[str], timeout: int = 30) -> str:
        """Run a gh CLI command and return stdout."""
        if not self.check_cli():
            raise AdapterOfflineError(
                "GitHub CLI (gh) is not available. Install: brew install gh && gh auth login"
            )
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "authentication" in stderr.lower() or "401" in stderr:
                    raise AdapterAuthError(f"GitHub auth failed: {stderr}")
                if "not found" in stderr.lower() or "404" in stderr:
                    raise AdapterError(f"GitHub resource not found: {stderr}")
                raise AdapterError(f"gh command failed: {stderr}")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise AdapterOfflineError(f"GitHub CLI timed out: {' '.join(cmd[:4])}")

    def _run_api(self, method: str, endpoint: str,
                 body: Optional[dict] = None) -> dict | list:
        """Call the GitHub REST API via gh api.

        The body travels as inline ``-f``/``-F`` fields rather than as JSON on
        stdin.  A broker standing in for `gh` has to decide whether to mint a
        write token *before* the command runs, and it cannot read stdin to do
        that, so a stdin body is refusable on principle and was refused.
        Inline fields are inspectable, which is what makes the same call work
        both as a person and as a governed bot identity.
        """
        cmd = [*self.cli, "api", endpoint, "--method", method]
        for key, value in (body or {}).items():
            if isinstance(value, bool) or isinstance(value, (int, float)):
                # -F infers the JSON type, so numbers and booleans do not
                # arrive quoted (`sub_issue_id` must be a number, not "12").
                cmd.extend(["-F", f"{key}={json.dumps(value)}"])
            elif isinstance(value, str):
                # -f keeps the value a literal string, so a value that starts
                # with @ is not read as a filename.
                cmd.extend(["-f", f"{key}={value}"])
            else:
                raise AdapterError(
                    f"GitHub API field {key!r} is {type(value).__name__}; only "
                    f"strings, numbers and booleans can be sent as inspectable "
                    f"fields"
                )

        if not self.check_cli():
            raise AdapterOfflineError("GitHub CLI not available")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "422" in stderr and "already_exists" in stderr:
                    # Milestone/label already exists — not an error
                    return {}
                raise AdapterError(f"GitHub API error: {stderr}")
            if not result.stdout.strip():
                return {}
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            raise AdapterOfflineError(f"GitHub API timed out: {endpoint}")

    def _run_graphql(self, query: str) -> dict:
        """Run a GraphQL query via gh api graphql."""
        if not self.check_cli():
            raise AdapterOfflineError("GitHub CLI not available")
        try:
            result = subprocess.run(
                [*self.cli, "api", "graphql", "-f", f"query={query}"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "INSUFFICIENT_SCOPES" in stderr:
                    raise AdapterAuthError(f"Insufficient GitHub token scopes: {stderr}")
                raise AdapterError(f"GitHub GraphQL error: {stderr}")
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except subprocess.TimeoutExpired:
            raise AdapterOfflineError("GitHub GraphQL timed out")
