"""Layer 1 — `modes/doctor.md` must agree with the plugin it diagnoses (#846).

`/synaptory doctor` is a prompt, not a module, and nothing read it. It drifted
into reporting a FAIL for a plugin that was installed and working: it read a
manifest from a path that does not exist, required a plugin version above the
whole 1.x line, called `controlled` canonical when `mode_reader` says
`interactive` is, and its auto-fix rewrote a correct `interactive` project to
the legacy spelling. Green tests meant nothing here because no test opened it.

Two kinds of check, both run against the authored Claude document and the
generated Cursor copy:

  static     parse the fenced Python and assert what must not be in it.
  behavioral execute the document's own Python against a scratch project, with
             only the shell and the CLI stubbed, and assert what it concludes
             and what its auto-fix writes. This is what catches a fix that is
             correct in the prose and wrong in the code.

The engagement vocabulary is deliberately not asserted as a list here: the
point of the fix is that the document holds no copy of it, so the assertions
are phrased against `mode_reader`, the owner.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import compose_directives
import doctor
import mode_reader
import runtime_selector

REPO_ROOT = Path(__file__).resolve().parents[3]
CLAUDE_ROOT = REPO_ROOT / "plugin-claude"
CURSOR_ROOT = REPO_ROOT / "plugin-cursor"
DOC_REL = Path("skills") / "synaptory" / "modes" / "doctor.md"

HOSTS = {
    "claude": (CLAUDE_ROOT, ".claude-plugin"),
    "cursor": (CURSOR_ROOT, ".cursor-plugin"),
}

PLUGIN_VERSION = tuple(
    int(x) for x in json.loads((CLAUDE_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"].split(".")[:3]
)

# Spellings a stale doctor used for engagement modes. `guided` is not a mode the
# runtime knows (it is only a stale spelling in init.md), `supervised` likewise.
STALE_MODE_WORDS = {"guided", "supervised"}


def _source(host: str) -> str:
    root, _ = HOSTS[host]
    text = (root / DOC_REL).read_text(encoding="utf-8")
    if host == "claude":
        # What compose does for the Claude package: `{{path:}}` -> ${CLAUDE_PLUGIN_ROOT}/...
        text = compose_directives.expand(text, host="claude", own_dir="skills/synaptory")
    return text


def _blocks(text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", text, re.S)


@pytest.fixture(params=sorted(HOSTS))
def host(request) -> str:
    return request.param


# ─── static ──────────────────────────────────────────────────────────────────


def test_every_python_block_parses(host):
    for index, block in enumerate(_blocks(_source(host))):
        ast.parse(block, filename=f"doctor.md[{host}] block {index}")


def test_no_hard_coded_marketplace_path(host):
    """The installed plugin lives in the plugin cache; `marketplaces/` never held it."""
    assert "marketplaces/" not in _source(host)


def test_plugin_manifest_is_read_through_the_plugin_root(host):
    """Resolved from the executing plugin, so cache, local and sideloaded installs agree."""
    _, manifest_dir = HOSTS[host]
    text = _source(host)
    root_var = "CLAUDE_PLUGIN_ROOT" if host == "claude" else "PLUGIN_ROOT"
    assert f"${{{root_var}}}/{manifest_dir}/plugin.json" in text
    assert "expanduser" not in text.split("# --- 1c.")[1].split("# --- 1d.")[0]


def test_no_plugin_version_floor_above_the_installed_version(host):
    """There is no plugin-version floor; only the CLI has one (`cli_min_version`)."""
    text = _source(host)
    assert "Plugin version >=" not in text
    for floor in re.findall(r">=\s*(\d+)\.(\d+)", text):
        assert tuple(int(x) for x in floor) <= PLUGIN_VERSION[:2], floor
    assert "cli_min_version" in text


def test_document_holds_no_copy_of_the_engagement_vocabulary(host):
    """`mode_reader` owns the vocabulary; doctor asks it (`doctor.py facts`)."""
    vocabulary = set(mode_reader.VALID_MODES)
    for block in _blocks(_source(host)):
        for node in ast.walk(ast.parse(block)):
            if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
                names = {
                    e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
                assert len(names & vocabulary) < 2, f"doctor.md defines its own engagement set: {sorted(names)}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in STALE_MODE_WORDS, node.value


def test_no_auto_fix_adds_baa_enforced(host):
    """Absence is the normal state of a non-healthcare project, and Codex refuses the key."""
    for block in _blocks(_source(host)):
        for node in ast.walk(ast.parse(block)):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "append":
                if getattr(node.func.value, "id", "") == "auto_fixes":
                    assert "baa_enforced" not in ast.dump(node)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"Edit", "Write"}:
                assert "baa_enforced" not in ast.dump(node)


def test_cli_probes_use_the_resolved_cli_name(host):
    """A bare `synaptory ...` probe asks the production CLI on a local-channel install."""
    for block in _blocks(_source(host)):
        for node in ast.walk(ast.parse(block)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Bash":
                head = node.args[0] if node.args else None
                literal = head.value if isinstance(head, ast.Constant) and isinstance(head.value, str) else ""
                assert not re.match(r"\s*synaptory\b", literal), literal
                if isinstance(head, ast.JoinedStr):
                    first = head.values[0] if head.values else None
                    assert not (
                        isinstance(first, ast.Constant) and re.match(r"\s*synaptory\b", first.value)
                    ), ast.dump(head)


def test_build_mode_fix_text_names_spq(host):
    """The check accepts spq; its fix text must not tell the reader otherwise."""
    assert 'Set build_mode to \\"spq\\"' in _source(host)


# ─── behavioral ──────────────────────────────────────────────────────────────


def _run_doctor(host: str, project: Path, home: Path, *, apply_fixes: bool, monkeypatch):
    """Run the document's own Python against `project`. Only the shell's CLI calls are stubbed."""
    root, _ = HOSTS[host]
    text = _source(host)
    blocks = _blocks(text)
    apply_block = next(b for b in blocks if b.startswith("yaml_text = Read("))

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("PLUGIN_ROOT", str(root))
    monkeypatch.delenv("SYNAPTORY_CHANNEL", raising=False)
    monkeypatch.delenv("SYNAPTORY_PROJECT_ID", raising=False)

    out: list[str] = []
    edits: list[tuple[str, str]] = []
    commands: list[str] = []

    def bash(command: str) -> str:
        """The shell, in-process where the answer is a file read.

        Process spawn dominates this suite's wall time, so only the commands
        whose output matters are interpreted: the plugin-root token is expanded
        exactly as the shell would, and the path each command names is recorded
        so a test can assert it exists. `doctor.py facts` has its own
        subprocess test below.
        """
        commands.append(command)
        head = command.split()[0] if command.split() else ""
        if head in {"synaptory", "synaptory-local"}:
            if " version" in command:
                return "synaptory version %s" % ".".join(map(str, PLUGIN_VERSION))
            if " status" in command:
                return "control_plane_url: https://synaptory.h3t.co"
            if " whoami" in command:
                return "upn: dev@example.com"
            return "slug: demo"
        expanded = os.path.expandvars(command)
        match = re.match(r'cat "([^"]+)"', expanded)
        if match:
            target = Path(match.group(1))
            return target.read_text() if target.is_file() else "__MISSING__"
        match = re.match(r'python3 "([^"]+/hooks/lib/(doctor|loop_engine)\.py)" (facts|status) "([^"]+)"', expanded)
        if match:
            script, project_arg = Path(match.group(1)), match.group(4)
            if not script.is_file():
                return "{}"
            if match.group(2) == "doctor":
                return json.dumps(doctor.config_facts(project_arg))
            return "{}"  # loop_engine's own tests cover its status verb
        if "loop_engine.py" in command:  # reached through a malformed token in an old document
            return "{}"
        if re.match(r"ls |find ", command):
            return ""
        raise AssertionError(f"unexpected shell command: {command}")

    def edit(path, old_string, new_string):
        content = Path(path).read_text()
        assert old_string in content, old_string
        Path(path).write_text(content.replace(old_string, new_string, 1))
        edits.append((old_string, new_string))

    g = {
        "Bash": bash,
        "log": out.append,
        "Read": lambda p: Path(p).read_text(),
        "Edit": edit,
        "Write": lambda p, c: Path(p).write_text(c),
        "AskUserQuestion": lambda questions: None,
        "AskQuestion": lambda questions: None,  # the Cursor spelling
        # A document that still interpolates a plugin-root token inside an
        # f-string reads these as Python names; defining them lets such a
        # document run far enough to be judged on what it concludes.
        "CLAUDE_PLUGIN_ROOT": str(root),
        "PLUGIN_ROOT": str(root),
    }
    for block in blocks:
        if block is not apply_block:
            exec(compile(block, f"doctor.md[{host}]", "exec"), g)
    if apply_fixes and g["auto_fixes"]:
        exec(compile(apply_block, f"doctor.md[{host}] apply", "exec"), g)
    g["_commands"] = commands
    return g, "\n".join(out), edits


def _project(tmp_path: Path, engagement_line: str, extra: str = "") -> tuple[Path, Path]:
    project = tmp_path / "demo"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (project / ".synaptory.yaml").write_text(
        'version: "1.3.5"\nproject_id: "demo"\nbuild_mode: "spq"\n'
        + engagement_line
        + 'agents:\n  default_backend: "claude"\ntracker:\n  backend: "local"\n'
        + extra
    )
    # An otherwise healthy project: the plugin is enabled in the project file.
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text('{"enabledPlugins": {"synaptory@h3tech-ai": true}}')
    return project, home


def _engagement_check(g) -> dict:
    return next(c for c in g["checks"] if c["check"] == "engagement_mode canonical")


def test_canonical_modes_pass_and_get_no_auto_fix(host, tmp_path, monkeypatch):
    """The reported case: `interactive` was a WARN with a fix toward `controlled`."""
    for mode in sorted(mode_reader.CANONICAL_MODES):
        sub = tmp_path / mode
        sub.mkdir()
        project, home = _project(sub, f'engagement_mode: "{mode}"\n')
        g, report, _ = _run_doctor(host, project, home, apply_fixes=False, monkeypatch=monkeypatch)
        assert _engagement_check(g)["result"] == "PASS", mode
        assert not any("engagement_mode" in f for f in g["auto_fixes"]), g["auto_fixes"]


@pytest.mark.parametrize("alias", sorted(set(mode_reader.MODE_ALIASES) - mode_reader.CANONICAL_MODES))
def test_alias_is_a_rename_note_and_the_fix_keeps_the_meaning(host, alias, tmp_path, monkeypatch):
    """`autonomous` -> `structured`, `controlled` -> `interactive`; never the inverse."""
    project, home = _project(tmp_path, f'engagement_mode: "{alias}"  # keep me\n')
    g, report, edits = _run_doctor(host, project, home, apply_fixes=True, monkeypatch=monkeypatch)
    check = _engagement_check(g)
    assert check["result"] == "WARN"
    assert "alias" in check["fix"]
    canonical = mode_reader.MODE_ALIASES[alias]
    assert f'"{canonical}"' in check["fix"]

    declared = runtime_selector.read_declared(project, "engagement_mode")
    assert declared == canonical, f"{alias!r} was rewritten to {declared!r}"
    assert mode_reader.normalize_mode(declared) == mode_reader.normalize_mode(alias)
    assert "# keep me" in (project / ".synaptory.yaml").read_text()


def test_alias_fix_handles_unquoted_and_single_quoted_values(host, tmp_path, monkeypatch):
    for spelling, expected in (("engagement_mode: autonomous\n", "structured"), ("engagement_mode: 'controlled'\n", "interactive")):
        sub = tmp_path / expected
        sub.mkdir()
        project, home = _project(sub, spelling)
        _run_doctor(host, project, home, apply_fixes=True, monkeypatch=monkeypatch)
        assert runtime_selector.read_declared(project, "engagement_mode") == expected


def test_unknown_mode_fails_and_is_not_guessed_at(host, tmp_path, monkeypatch):
    project, home = _project(tmp_path, 'engagement_mode: "guided"\n')
    before = (project / ".synaptory.yaml").read_text()
    g, report, _ = _run_doctor(host, project, home, apply_fixes=True, monkeypatch=monkeypatch)
    assert _engagement_check(g)["result"] == "FAIL"
    assert not any("engagement_mode" in f for f in g["auto_fixes"])
    assert (project / ".synaptory.yaml").read_text() == before


def test_absent_mode_declares_the_runtime_default_only(host, tmp_path, monkeypatch):
    project, home = _project(tmp_path, "")
    g, report, _ = _run_doctor(host, project, home, apply_fixes=True, monkeypatch=monkeypatch)
    assert _engagement_check(g)["result"] == "WARN"
    assert runtime_selector.read_declared(project, "engagement_mode") == mode_reader.DEFAULT_MODE


def test_every_promised_auto_fix_is_applied_and_nothing_else_is(host, tmp_path, monkeypatch):
    """A promise with no handler is how doctor offered fixes it then skipped."""
    project = tmp_path / "demo"
    project.mkdir()
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (project / ".claude").mkdir()
    (project / ".synaptory.yaml").write_text(
        'version: "1.3.5"\nbuild_mode: "spq"\nengagement_mode: "controlled"\nmaster_password: "x"\n'
    )
    (project / ".claude" / "settings.json").write_text('{"enabledPlugins": {"tracker@h3tech-ai": true}}')
    g, report, _ = _run_doctor(host, project, home, apply_fixes=True, monkeypatch=monkeypatch)
    promised = " | ".join(g["auto_fixes"])
    for word in ("project_id", "controlled → interactive", "master_password", "tracker@h3tech-ai", "synaptory@h3tech-ai"):
        assert word in promised, (word, promised)
    yaml_text = (project / ".synaptory.yaml").read_text()
    assert "project_id:" in yaml_text
    assert "master_password" not in yaml_text
    assert "baa_enforced" not in yaml_text
    assert runtime_selector.read_declared(project, "engagement_mode") == "interactive"
    plugins = json.loads((project / ".claude" / "settings.json").read_text())["enabledPlugins"]
    assert "tracker@h3tech-ai" not in plugins and plugins["synaptory@h3tech-ai"] is True


def test_plugin_enabled_in_user_scope_is_enabled(host, tmp_path, monkeypatch):
    """`plugin install` defaults to user scope; the project file need not name it."""
    project, home = _project(tmp_path, 'engagement_mode: "interactive"\n')
    (project / ".claude" / "settings.json").unlink()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text('{"enabledPlugins": {"synaptory@h3tech-ai": true}}')
    g, report, _ = _run_doctor(host, project, home, apply_fixes=False, monkeypatch=monkeypatch)
    check = next(c for c in g["checks"] if c["check"] == "synaptory@h3tech-ai enabled")
    assert check["result"] == "PASS"


def test_nested_backend_key_reaches_the_baa_check(host, tmp_path, monkeypatch):
    """A column-0 regex never matched `agents.default_backend`, so this check was dead."""
    project, home = _project(tmp_path, 'engagement_mode: "interactive"\n')
    text = (project / ".synaptory.yaml").read_text().replace('default_backend: "claude"', 'default_backend: "codex"')
    (project / ".synaptory.yaml").write_text(text + "healthcare:\n  baa_enforced: true\n")
    g, report, _ = _run_doctor(host, project, home, apply_fixes=False, monkeypatch=monkeypatch)
    check = next(c for c in g["checks"] if c["check"].startswith("BAA-covered backend"))
    assert check["result"] == "FAIL"
    assert "'codex'" in check["detail"]


def test_every_path_the_document_reads_exists_in_the_package(host, tmp_path, monkeypatch):
    """The failure this issue reported: a path that resolves to nothing, reported as an install fault."""
    project, home = _project(tmp_path, 'engagement_mode: "interactive"\n')
    g, report, _ = _run_doctor(host, project, home, apply_fixes=False, monkeypatch=monkeypatch)
    _, manifest_dir = HOSTS[host]
    seen = {"manifest": False, "facts": False}
    for command in g["_commands"]:
        expanded = os.path.expandvars(command)
        for path in re.findall(r'"([^"]*(?:plugin\.json|doctor\.py|loop_engine\.py))"', expanded):
            assert Path(path).is_file(), path
            seen["manifest"] |= path.endswith(f"{manifest_dir}/plugin.json")
            seen["facts"] |= path.endswith("hooks/lib/doctor.py")
    assert all(seen.values()), seen


def test_plugin_and_cli_version_checks_pass_for_a_matching_cli(host, tmp_path, monkeypatch):
    project, home = _project(tmp_path, 'engagement_mode: "interactive"\n')
    g, report, _ = _run_doctor(host, project, home, apply_fixes=False, monkeypatch=monkeypatch)
    by_name = {c["check"]: c for c in g["checks"]}
    assert by_name["Plugin installed"]["result"] == "PASS"
    assert by_name["CLI meets the plugin's cli_min_version"]["result"] == "PASS"
    assert not any("Plugin version" in name for name in by_name)


# ─── the verbs the document calls ────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,kind,canonical",
    [
        ("structured", "canonical", "structured"),
        ("interactive", "canonical", "interactive"),
        ("Interactive", "canonical", "interactive"),
        ("autonomous", "alias", "structured"),
        ("controlled", "alias", "interactive"),
        ("hands-on", "alias", "interactive"),
        ("hands_off", "alias", "structured"),
        ("guided", "unknown", ""),
        ("", "absent", "structured"),
        (None, "absent", "structured"),
    ],
)
def test_classify_mode(raw, kind, canonical):
    verdict = mode_reader.classify_mode(raw)
    assert (verdict["kind"], verdict["canonical"]) == (kind, canonical)


def test_classify_mode_agrees_with_the_runtime_for_every_known_spelling():
    """An alias must land on the mode `normalize_mode` resolves it to, never a different one."""
    for name in mode_reader.VALID_MODES:
        verdict = mode_reader.classify_mode(name)
        assert verdict["canonical"] == mode_reader.normalize_mode(name)
        assert verdict["canonical"] in mode_reader.CANONICAL_MODES
        assert (verdict["kind"] == "canonical") == (name in mode_reader.CANONICAL_MODES)


def test_read_declared_reads_top_level_and_nested_keys(tmp_path):
    (tmp_path / ".synaptory.yaml").write_text(
        'engagement_mode: "interactive"  # note\nagents:\n  default_backend: "codex"\ntracker:\n  backend: github\n'
    )
    assert runtime_selector.read_declared(tmp_path, "engagement_mode") == "interactive"
    assert runtime_selector.read_declared(tmp_path, "agents", "default_backend") == "codex"
    assert runtime_selector.read_declared(tmp_path, "tracker", "backend") == "github"
    assert runtime_selector.read_declared(tmp_path, "agents") is None
    assert runtime_selector.read_declared(tmp_path, "agents", "missing") is None
    assert runtime_selector.read_declared(tmp_path, "default_backend") is None  # indented key is not top-level
    assert runtime_selector.read_declared(tmp_path / "nowhere", "engagement_mode") is None


def test_config_facts_cli_prints_json(tmp_path, monkeypatch):
    (tmp_path / ".synaptory.yaml").write_text(
        'project_id: "p"\nbuild_mode: "spq"\nengagement_mode: "autonomous"\nagents:\n  default_backend: "claude"\n'
    )
    monkeypatch.delenv("SYNAPTORY_CHANNEL", raising=False)
    done = subprocess.run(
        [sys.executable, doctor.__file__, "facts", str(tmp_path)], capture_output=True, text=True, check=True
    )
    facts = json.loads(done.stdout)
    assert facts["project_id"] == "p"
    assert facts["build_mode"] == "spq"
    assert facts["engagement"] == {"value": "autonomous", "kind": "alias", "canonical": "structured"}
    assert facts["declared_backend"] == "claude"
    assert facts["baa_enforced"] is False
    assert facts["cli"] in {"synaptory", "synaptory-local"}


def test_config_facts_honours_the_local_channel(tmp_path, monkeypatch):
    """Same choice `doctor._run_cli` makes: the local channel probes `synaptory-local`."""
    monkeypatch.setattr(doctor.host_env, "local_plugin_runtime", lambda: True)
    assert doctor.config_facts(str(tmp_path))["cli"] == "synaptory-local"
    monkeypatch.setattr(doctor.host_env, "local_plugin_runtime", lambda: False)
    assert doctor.config_facts(str(tmp_path))["cli"] == "synaptory"
