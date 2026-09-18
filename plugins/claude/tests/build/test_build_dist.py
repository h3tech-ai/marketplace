"""Layer 2 — `./synaptory build` dist-tree composition tests.

Runs the build script in an isolated `web/dist/marketplace/` working
copy and asserts on the structure of what came out:

  * Top-level skill SKILL.md is a stub.
  * Every agent has an agent.md stub naming `<role>/agent`.
  * No `.enc` files anywhere.
  * `rules/`, `_shared/protocols/`, `_shared/templates/`,
    `_shared/design-assets/`, `hooks/data/*.md` purged.
  * Integrity manifest covers stubs + plaintext content.
  * marketplace.json catalog has the plugin entry.

These pin ADR-016's dist-composition contract. A future change that
tries to drop agent SKILL.md from dist before the prompt-rewrite
follow-up lands will fail here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def fresh_build(repo_root: Path, tmp_path_factory) -> Path:
    """Run `./synaptory build` once per module; return path to the dist tree.

    Builds into the real `web/dist/marketplace/` (gitignored) — the build
    script's expectations about that path are baked in. We snapshot
    the prior contents and restore them on teardown so tests don't
    smear into developer state.
    """
    synaptory = repo_root / "synaptory"
    dist_root = repo_root / "web" / "dist" / "marketplace"

    # Snapshot existing dist (best-effort — a stale tree from a manual
    # build is fine to keep around; we just don't want to grow it).
    backup = tmp_path_factory.mktemp("dist-backup")
    if dist_root.exists():
        shutil.copytree(dist_root, backup / "marketplace", dirs_exist_ok=True)

    # Wipe and rebuild fresh. The build script needs SYNAPTORY_CP_URL and the
    # Entra IDs stamped in; pull from the local stack defaults.
    if dist_root.exists():
        shutil.rmtree(dist_root)
    dist_root.mkdir(parents=True)

    env = {
        **os.environ,
        "SYNAPTORY_PLUGIN_NAME": "synaptory",
        "SYNAPTORY_CP_URL": os.environ.get("SYNAPTORY_CP_URL", "http://localhost:8080"),
        "SYNAPTORY_CP_ENTRA_TENANT_ID": os.environ.get(
            "SYNAPTORY_CP_ENTRA_TENANT_ID", "00000000-0000-0000-0000-000000000000"
        ),
        "SYNAPTORY_CP_ENTRA_CLIENT_ID": os.environ.get(
            "SYNAPTORY_CP_ENTRA_CLIENT_ID", "00000000-0000-0000-0000-000000000001"
        ),
        # These tests exercise plugin-tree composition only. Skip the
        # heavy docker image + Go cross-compile steps that the unified
        # `./synaptory build` runs by default.
        "SYNAPTORY_BUILD_SKIP_HEAVY": "1",
    }
    # Pin the version instead of letting the build bump it (#233). `cmd_build`
    # bumps /VERSION by default and rewrites plugin.json to match, so running
    # this suite used to leave the working tree dirty at a version nobody asked
    # for — 1.1.1 -> 1.1.2 on a bare test run. That is why the directory could
    # not simply be added to the pytest search path: `release.yml` runs
    # `./synaptory test` BEFORE its own bump step, so a test run could shift the
    # version the release then publishes.
    #
    # `--version <current>` still writes the file, but with identical content,
    # so git sees nothing. The snapshot/restore below covers the rest and any
    # future field the build learns to stamp.
    version_file = repo_root / "VERSION"
    plugin_json = repo_root / "plugin-claude" / ".claude-plugin" / "plugin.json"
    cursor_plugin_json = repo_root / "plugin-cursor" / ".cursor-plugin" / "plugin.json"
    codex_plugin_json = (
        repo_root / "plugin-codex" / "plugins" / "synaptory" / ".codex-plugin" / "plugin.json"
    )
    cursor_cp_url = repo_root / "plugin-cursor" / "hooks" / "lib" / "cp-url"
    pinned = version_file.read_text(encoding="utf-8").strip()
    before = {p: p.read_text(encoding="utf-8") for p in (
        version_file, plugin_json, cursor_plugin_json, codex_plugin_json,
        cursor_cp_url,
    )
              if p.exists()}
    try:
        result = subprocess.run(
            [str(synaptory), "build", "--version", pinned],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"./synaptory build failed: rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    finally:
        # Restore unconditionally: a failing assertion above must not leave the
        # developer's tree mutated either.
        for path, text in before.items():
            if path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")

    plugin_dist = dist_root / "plugins" / "claude"
    assert plugin_dist.is_dir(), (
        f"build did not produce {plugin_dist}\nstdout:\n{result.stdout}"
    )

    yield plugin_dist

    # Teardown: restore the prior dist if there was one. Tests should
    # not accumulate.
    if dist_root.exists():
        shutil.rmtree(dist_root)
    if (backup / "marketplace").exists():
        shutil.copytree(backup / "marketplace", dist_root)


@pytest.mark.build
def test_no_enc_files(fresh_build: Path):
    """ADR-016: build never emits encrypted artifacts."""
    enc_files = list(fresh_build.rglob("*.enc"))
    assert not enc_files, f"unexpected .enc files in dist: {enc_files}"


@pytest.mark.build
def test_top_level_skill_is_stub(fresh_build: Path):
    """skills/<top>/SKILL.md is the CLI-stub, not the full body."""
    skill_md = fresh_build / "skills" / "synaptory" / "SKILL.md"
    assert skill_md.is_file()
    content = skill_md.read_text(encoding="utf-8")
    # Stub characteristics: short, contains the bash backtick that calls
    # `synaptory skills get`.
    line_count = content.count("\n")
    assert line_count < 40, (
        f"SKILL.md should be a stub (<40 lines), got {line_count}"
    )
    assert "synaptory skills get" in content, (
        "stub must call the CLI to fetch the body"
    )


@pytest.mark.build
def test_every_agent_has_agent_md_stub(fresh_build: Path):
    """ADR-016: every agents/<role>/agent.md is a stub naming <role>/agent."""
    agents_dir = fresh_build / "agents"
    assert agents_dir.is_dir()

    role_dirs = [d for d in agents_dir.iterdir() if d.is_dir()]
    assert len(role_dirs) >= 9, f"expected ≥9 agents, found {len(role_dirs)}"

    for role_dir in role_dirs:
        agent_md = role_dir / "agent.md"
        assert agent_md.is_file(), f"missing agent.md for {role_dir.name}"
        content = agent_md.read_text(encoding="utf-8")
        assert content.count("\n") < 15, (
            f"agent.md for {role_dir.name} should be a stub, got {content.count(chr(10))} lines"
        )
        # Stub must call CLI with the path-keyed name `<role>/agent`.
        expected_name = f"{role_dir.name}/agent"
        assert expected_name in content, (
            f"agent.md for {role_dir.name} should reference '{expected_name}'; "
            f"got: {content[:200]!r}"
        )


@pytest.mark.build
def test_cp_served_dirs_purged_from_dist(fresh_build: Path):
    """ADR-016: directories served exclusively by the CP must be absent."""
    must_be_absent = [
        "rules",
        "skills/_shared/protocols",
        "skills/_shared/templates",
        "skills/_shared/design-assets",
    ]
    for path in must_be_absent:
        d = fresh_build / path
        assert not d.exists(), f"{path} should be absent from dist"

    # hooks/data/ may exist as a directory but should have no .md left.
    hooks_data = fresh_build / "hooks" / "data"
    if hooks_data.is_dir():
        leftover_md = list(hooks_data.glob("*.md"))
        assert not leftover_md, f"hooks/data/*.md should be CP-served; found {leftover_md}"


@pytest.mark.build
def test_evidence_contract_ships_in_dist(fresh_build: Path):
    """#134/#163: the evidence contract is runtime plumbing, not IP — the
    SubagentStart envelope renders from it locally, so the ADR-016 purge must
    never catch it. Same for the hooks/lib modules that consume it."""
    contract = fresh_build / "receipt-schema" / "evidence-contract.json"
    assert contract.is_file(), "receipt-schema/evidence-contract.json must ship"
    import json as _json

    data = _json.loads(contract.read_text(encoding="utf-8"))
    assert data.get("contract_version"), "shipped contract must be versioned"
    for mod in (
        "evidence_contract.py",
        "verification_cache.py",
        "worktree_manager.py",
        "preview_asset_check.py",
    ):
        assert (fresh_build / "hooks" / "lib" / mod).is_file(), f"hooks/lib/{mod} must ship"


@pytest.mark.build
def test_integrity_manifest_covers_stubs_and_plaintext(fresh_build: Path):
    """The integrity manifest lists the bytes that actually ship."""
    manifest = fresh_build / "hooks" / ".integrity-manifest"
    assert manifest.is_file(), "integrity manifest must ship at hooks/.integrity-manifest"
    lines = manifest.read_text(encoding="utf-8").strip().splitlines()
    # Each line is "<sha256>  <path>".
    assert len(lines) > 50, f"manifest should cover many files, got {len(lines)}"

    # Spot-check a few expected entries.
    paths = [line.split(maxsplit=1)[1] for line in lines]
    assert "hooks/synaptory-inject-protocols.sh" in paths
    assert "skills/synaptory/SKILL.md" in paths
    assert any(p.startswith("agents/") and p.endswith("agent.md") for p in paths), (
        "manifest should include agent stubs"
    )
    # No .enc references (ADR-016).
    assert not any(p.endswith(".enc") for p in paths)


@pytest.mark.build
def test_marketplace_json_catalog_present(fresh_build: Path):
    """marketplace.json is upserted with the plugin entry."""
    catalog = fresh_build.parents[1] / ".claude-plugin" / "marketplace.json"
    assert catalog.is_file()
    data = json.loads(catalog.read_text(encoding="utf-8"))
    plugins = data.get("plugins", [])
    matches = [p for p in plugins if p.get("name") == "synaptory"]
    assert matches, f"no synaptory entry in marketplace.json: {data}"
    entry = matches[0]
    # `source` for a plugin within the same marketplace repo is a STRING
    # relative path (per Claude Code's marketplace docs at
    # https://code.claude.com/docs/en/plugin-marketplaces). The earlier
    # object form `{"type":"local","path":"./plugins/claude"}` was rejected by
    # the marketplace-add validator at install time — see ADR follow-up.
    assert isinstance(entry["source"], str), (
        f"`source` must be a string relative path, got {type(entry['source']).__name__}: "
        f"{entry['source']!r}"
    )
    assert entry["source"] == "./plugins/claude", (
        f"`source` must be './plugins/claude', got {entry['source']!r}"
    )


@pytest.mark.build
def test_codex_package_and_catalog_present(fresh_build: Path, repo_root: Path):
    """The unified build emits the staged Codex package beside Claude when present."""
    if not (repo_root / "plugin-codex" / "scripts" / "compose.py").is_file():
        pytest.skip("plugin-codex is not in this tree")
    marketplace_root = fresh_build.parents[1]
    codex_plugin = marketplace_root / "plugins" / "codex"
    manifest = codex_plugin / ".codex-plugin" / "plugin.json"
    metadata = codex_plugin / ".codex-plugin" / "build-metadata.json"
    control_plane_url = codex_plugin / "hooks" / "lib" / "cp-url"
    catalog = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    assert manifest.is_file()
    assert metadata.is_file()
    assert control_plane_url.is_file()
    assert catalog.is_file()
    plugin_data = json.loads(manifest.read_text(encoding="utf-8"))
    catalog_data = json.loads(catalog.read_text(encoding="utf-8"))
    assert plugin_data["name"] == "synaptory"
    assert plugin_data["version"] == (marketplace_root.parents[2] / "VERSION").read_text().strip()
    assert catalog_data["plugins"][0]["source"] == {
        "source": "local",
        "path": "./plugins/codex",
    }
    assert control_plane_url.read_text(encoding="utf-8").strip() == os.environ.get(
        "SYNAPTORY_CP_URL", "http://localhost:8080"
    ).rstrip("/")


@pytest.mark.build
def test_runtime_fixtures_table_ships_in_dist(fresh_build: Path):
    """#739: the certified adapter-profile table must ship with the Claude
    package.

    `core/runtime-fixtures/` is a monorepo sibling of `core/lib`, not a child
    of `plugin-claude/`, so the `rsync -aL "$PLUGIN_ROOT/" ...` that builds
    this dist tree never touched it on its own. `runtime_selector` resolves
    the table relative to its OWN module (`hooks/lib/` in every composed
    package), so it must land beside that at `hooks/runtime-fixtures/` — the
    same destination the Codex and Cursor composers already use. Without it,
    a standalone Claude package denies every governed runtime dispatch with
    `profile_mismatch` ("the certified profile table is not in this
    installation") even though the pilot's reservation, seal, and policy are
    all valid.
    """
    table = fresh_build / "hooks" / "runtime-fixtures" / "profiles.json"
    assert table.is_file(), (
        "the composed Claude package has no certified profile table at "
        f"{table}, so its selector denies every governed dispatch"
    )
    records = json.loads(table.read_text(encoding="utf-8"))
    records = records.get("profiles") if isinstance(records, dict) else records
    assert records, table


@pytest.mark.build
def test_packaged_selector_selects_a_builtin_profile_outside_the_monorepo(
    fresh_build: Path, tmp_path_factory
):
    """Package-level: runtime selection must work with NO monorepo checkout
    nearby — this is the exact reproduction shape from #739 ("prepare the
    standalone Claude package outside the monorepo").

    Copies the freshly built dist package (already dereferenced by `rsync
    -aL`, so it carries no symlinks back into `core/`) to a directory under
    the system temp root — never inside this repo's working tree — and runs
    `runtime_selector` there with `sys.path` pointing ONLY at the copied
    `hooks/lib/`. If the module fell back to a path relative to the monorepo
    (or the table were simply absent), this would fail the same way the real
    pilot dispatch did: `profile_mismatch` / zero loaded profiles.
    """
    standalone_root = tmp_path_factory.mktemp("standalone-claude-package")
    package = standalone_root / "claude"
    shutil.copytree(fresh_build, package)

    probe = (
        "import sys, json;"
        "sys.path.insert(0, sys.argv[1]);"
        "import runtime_selector as rs;"
        "pol = rs.parse_policy('build_mode: spq\\n"
        "runtimes:\\n  enabled: true\\n  policy_version: 1\\n"
        "  allowed_profiles:\\n    - claude-local-v1\\n');"
        "res = rs.select_runtime(build_mode='spq', policy=pol,"
        " request=rs.SelectionRequest(role='se'),"
        " profiles=rs.load_profiles(''),"
        " availability=(rs.Availability('claude-local-v1', True),));"
        "print(json.dumps({'profiles': len(rs.load_profiles('')),"
        " 'denied': res.denied, 'selected': res.selected}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe, str(package / "hooks" / "lib")],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(standalone_root),  # no repo_root, no core/ sibling anywhere nearby
    )
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["profiles"] > 0, (
        f"the packaged selector loaded no profiles outside the monorepo: {result!r}"
    )
    assert result["denied"] is False, (
        f"the packaged selector denied an available built-in profile: {result!r}"
    )
    assert result["selected"] == "claude-local-v1", result


@pytest.mark.build
def test_packaged_selector_still_fails_closed_without_the_table(
    fresh_build: Path, tmp_path_factory
):
    """The packaging fix is additive; the fail-closed refusal (#396) when the
    table is genuinely missing must be unchanged.

    Simulates a package built before this fix (or corrupted in transit) by
    copying dist and deleting the fixtures directory, then asserts loading
    profiles still raises loudly instead of silently returning zero profiles.
    """
    broken_root = tmp_path_factory.mktemp("standalone-claude-package-broken")
    package = broken_root / "claude"
    shutil.copytree(fresh_build, package)
    shutil.rmtree(package / "hooks" / "runtime-fixtures")

    probe = (
        "import sys, json\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import runtime_selector as rs\n"
        "try:\n"
        "    rs.load_profiles('')\n"
        "    print(json.dumps({'raised': False}))\n"
        "except rs.InvalidRuntimeInput as exc:\n"
        "    print(json.dumps({'raised': True, 'message': str(exc)}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe, str(package / "hooks" / "lib")],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(broken_root),
    )
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["raised"] is True, (
        "a package with no certified table must fail closed, not load zero "
        f"profiles silently: {result!r}"
    )
    assert "certified profile table" in result["message"]


@pytest.mark.build
def test_marketplace_json_schema_compatible(fresh_build: Path):
    """marketplace.json validates against Claude Code's marketplace schema.

    A pragmatic structural check based on the public docs at
    https://code.claude.com/docs/en/plugin-marketplaces. We cannot run
    Claude Code's actual validator here, but the rules below pin the
    shapes the validator rejects (same bug class that shipped to prod
    and blocked `/plugin marketplace add`).
    """
    catalog_path = fresh_build.parents[1] / ".claude-plugin" / "marketplace.json"
    data = json.loads(catalog_path.read_text(encoding="utf-8"))

    # Top-level required keys.
    for key in ("name", "owner", "plugins"):
        assert key in data, f"top-level key '{key}' missing from marketplace.json"
    assert isinstance(data["name"], str) and data["name"], "marketplace name must be a non-empty string"
    assert isinstance(data["owner"], dict), "marketplace owner must be an object"
    assert isinstance(data["plugins"], list) and data["plugins"], "marketplace must declare ≥1 plugin"

    for i, entry in enumerate(data["plugins"]):
        ctx = f"plugins[{i}]"
        assert isinstance(entry, dict), f"{ctx} must be an object"
        for key in ("name", "source", "version"):
            assert key in entry, f"{ctx} missing required key '{key}'"
        assert isinstance(entry["name"], str) and entry["name"], f"{ctx}.name must be a non-empty string"
        assert isinstance(entry["version"], str) and entry["version"], f"{ctx}.version must be a non-empty string"

        src = entry["source"]
        if isinstance(src, str):
            # Relative path within the marketplace repo. Must start with './' and
            # resolve to a directory containing .claude-plugin/plugin.json.
            assert src.startswith("./"), (
                f"{ctx}.source string must start with './', got {src!r}"
            )
            plugin_dir = catalog_path.parent.parent / src
            assert plugin_dir.is_dir(), (
                f"{ctx}.source resolves to {plugin_dir}, which is not a directory"
            )
            assert (plugin_dir / ".claude-plugin" / "plugin.json").is_file(), (
                f"{ctx}.source must point to a directory containing .claude-plugin/plugin.json"
            )
        elif isinstance(src, dict):
            # External source: requires `source` discriminator key.
            assert "source" in src, (
                f"{ctx}.source object must include a 'source' discriminator "
                f"(e.g. 'github', 'gitlab', 'git', 'npm'); got {src}"
            )
            # Reject the legacy `{type, path}` shape that does NOT validate.
            assert src.get("source") not in (None, "local"), (
                f"{ctx}.source: 'local' is not a valid object form. Use a "
                f"string relative path (e.g. './synaptory') for in-repo plugins."
            )
        else:
            raise AssertionError(
                f"{ctx}.source must be a string or object, got {type(src).__name__}"
            )
