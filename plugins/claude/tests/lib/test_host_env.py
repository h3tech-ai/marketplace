"""Layer 1 - host-neutral environment lookups (#285)."""

from __future__ import annotations

import subprocess
import sys

import host_env
import pytest


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in host_env.PROJECT_DIR_VARS + host_env.PLUGIN_ROOT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SYNAPTORY_CHANNEL", raising=False)


def test_neutral_name_wins_over_the_legacy_one(monkeypatch):
    monkeypatch.setenv("SYNAPTORY_PROJECT_DIR", "/neutral")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/legacy")
    assert host_env.project_dir() == "/neutral"


def test_legacy_name_still_works(monkeypatch):
    """Claude Code sets CLAUDE_PROJECT_DIR itself; it is a real input, not a
    deprecated spelling, so it must keep resolving indefinitely."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/legacy")
    assert host_env.project_dir() == "/legacy"


def test_project_dir_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert host_env.project_dir() == str(tmp_path)


def test_project_dir_explicit_default(monkeypatch):
    assert host_env.project_dir(".") == "."


def test_blank_values_do_not_win(monkeypatch):
    """An exported-but-empty variable must not shadow a real one."""
    monkeypatch.setenv("SYNAPTORY_PROJECT_DIR", "   ")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/legacy")
    assert host_env.project_dir() == "/legacy"


def test_plugin_root_resolution(monkeypatch):
    assert host_env.plugin_root() == ""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/legacy-root")
    assert host_env.plugin_root() == "/legacy-root"
    monkeypatch.setenv("SYNAPTORY_PLUGIN_ROOT", "/neutral-root")
    assert host_env.plugin_root() == "/neutral-root"


def test_loopback_stamped_plugin_uses_local_state(monkeypatch, tmp_path):
    root = tmp_path / "plugin"
    cp_url = root / "hooks" / "lib" / "cp-url"
    cp_url.parent.mkdir(parents=True)
    cp_url.write_text("http://localhost:8080\n", encoding="utf-8")
    monkeypatch.setenv("SYNAPTORY_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert host_env.cli_channel() == "local"
    assert host_env.state_dir() == str(tmp_path / "home" / ".synaptory-local")


def test_production_stamp_wins_over_ambient_local_channel(monkeypatch, tmp_path):
    root = tmp_path / "plugin"
    cp_url = root / "hooks" / "lib" / "cp-url"
    cp_url.parent.mkdir(parents=True)
    cp_url.write_text("https://synaptory.h3t.co\n", encoding="utf-8")
    monkeypatch.setenv("SYNAPTORY_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("SYNAPTORY_CHANNEL", "local")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert host_env.cli_channel() == "stable"
    assert host_env.state_dir() == str(tmp_path / "home" / ".synaptory")


def test_shared_runtime_reads_no_host_specific_name_directly():
    """core/ must not read a variable named after one host.

    The tell that this was wrong: the other two hosts had to SET the
    Claude-named variable for shared code to work.
    """
    from pathlib import Path

    import re

    core = Path(host_env.__file__).resolve().parent
    # Match the NAME wherever it appears in an environ lookup, including calls
    # split across lines. The same-line form missed
    # session_manager.fetch_skill_body, which wrapped its default onto the next
    # line, so #285 shipped incomplete.
    pattern = re.compile(
        r"environ(?:\.get\(|\[)\s*\n?\s*[\"']CLAUDE_(?:PROJECT_DIR|PLUGIN_ROOT)[\"']"
    )
    offenders = []
    for module in list(core.glob("*.py")) + list((core.parent / "scripts").glob("*.py")):
        if module.name == "host_env.py":
            continue
        text = module.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append("%s:%d" % (module.name, line))
    assert not offenders, (
        "shared runtime reads a host-specific env name directly; go through "
        "host_env: %s" % offenders
    )


# ─── #320 — the runtime's own stamp, when no host set a plugin root ──────────


def test_runtime_stamp_resolves_when_no_plugin_root_is_set(monkeypatch, tmp_path):
    """A source-tree call has no plugin root; the stamp beside the runtime is
    the same signal `hooks/_cp-url.sh` reads from the script's own location.

    Without this, `plugin_control_plane_url()` returned "" for every source-tree
    call and every downstream channel decision silently defaulted to production
    (#320): gate events from a purely local project were addressed to
    https://synaptory.h3t.co, 404'd, and queued forever.
    """
    stamp = tmp_path / "lib"
    stamp.mkdir()
    (stamp / "cp-url.local").write_text("http://localhost:8080\n", encoding="utf-8")
    monkeypatch.setattr(host_env, "RUNTIME_STAMP_DIR", stamp)

    assert host_env.plugin_control_plane_url() == "http://localhost:8080"
    assert host_env.local_plugin_runtime() is True
    assert host_env.cli_channel() == "local"


def test_runtime_stamp_prefers_local_over_build_stamp(monkeypatch, tmp_path):
    stamp = tmp_path / "lib"
    stamp.mkdir()
    (stamp / "cp-url").write_text("https://synaptory.h3t.co\n", encoding="utf-8")
    (stamp / "cp-url.local").write_text("http://127.0.0.1:8080\n", encoding="utf-8")
    monkeypatch.setattr(host_env, "RUNTIME_STAMP_DIR", stamp)

    assert host_env.plugin_control_plane_url() == "http://127.0.0.1:8080"


def test_runtime_stamp_ignores_the_build_placeholder(monkeypatch, tmp_path):
    """An unstamped source tree carries the literal placeholder. That is not a
    URL and must not be reported as one."""
    stamp = tmp_path / "lib"
    stamp.mkdir()
    (stamp / "cp-url").write_text(host_env.CP_URL_PLACEHOLDER + "\n", encoding="utf-8")
    monkeypatch.setattr(host_env, "RUNTIME_STAMP_DIR", stamp)

    assert host_env.plugin_control_plane_url() == ""
    assert host_env.plugin_channel_source() == ""


def test_plugin_root_stamp_wins_over_the_runtime_stamp(monkeypatch, tmp_path):
    """The active host's plugin tree is the authority; the sibling stamp is the
    fallback for calls that arrive without one."""
    root = tmp_path / "plugin"
    (root / "hooks" / "lib").mkdir(parents=True)
    (root / "hooks" / "lib" / "cp-url").write_text(
        "https://synaptory.h3t.co\n", encoding="utf-8"
    )
    stamp = tmp_path / "lib"
    stamp.mkdir()
    (stamp / "cp-url.local").write_text("http://localhost:8080\n", encoding="utf-8")
    monkeypatch.setattr(host_env, "RUNTIME_STAMP_DIR", stamp)
    monkeypatch.setenv("SYNAPTORY_PLUGIN_ROOT", str(root))

    assert host_env.plugin_control_plane_url() == "https://synaptory.h3t.co"


def test_runtime_stamp_covers_a_plugin_root_with_no_stamp_of_its_own(
    monkeypatch, tmp_path
):
    """Codex puts its shared scripts under `runtime/scripts` and its stamp under
    `hooks/lib`, so exactly one of the two lookups can hit. A plugin root that
    yields nothing must fall through rather than end the search."""
    root = tmp_path / "plugin"
    root.mkdir()
    stamp = tmp_path / "lib"
    stamp.mkdir()
    (stamp / "cp-url").write_text("https://cp.example\n", encoding="utf-8")
    monkeypatch.setattr(host_env, "RUNTIME_STAMP_DIR", stamp)
    monkeypatch.setenv("SYNAPTORY_PLUGIN_ROOT", str(root))

    assert host_env.plugin_control_plane_url() == "https://cp.example"


def test_channel_source_distinguishes_production_from_unknown(monkeypatch, tmp_path):
    """`local_plugin_runtime()` answers False in both cases, which is fine for
    picking a state directory and wrong for picking a control plane."""
    stamp = tmp_path / "lib"
    stamp.mkdir()
    monkeypatch.setattr(host_env, "RUNTIME_STAMP_DIR", stamp)
    assert host_env.local_plugin_runtime() is False
    assert host_env.plugin_channel_source() == ""

    (stamp / "cp-url").write_text("https://synaptory.h3t.co\n", encoding="utf-8")
    assert host_env.local_plugin_runtime() is False
    assert host_env.plugin_channel_source() != ""


def test_ambient_local_channel_is_a_determinate_signal(monkeypatch, tmp_path):
    stamp = tmp_path / "lib"
    stamp.mkdir()
    monkeypatch.setattr(host_env, "RUNTIME_STAMP_DIR", stamp)
    monkeypatch.setenv("SYNAPTORY_CHANNEL", "local")

    assert host_env.local_plugin_runtime() is True
    assert host_env.plugin_channel_source() == "SYNAPTORY_CHANNEL=local"


def test_a_child_of_the_stamp_free_runtime_names_no_channel(stamp_free_runtime):
    """The subprocess half of `_neutralise_runtime_stamp` (conftest).

    `RUNTIME_STAMP_DIR` is the runtime module's own directory, so a test that
    execs `core/lib/<module>.py` reads whatever stamp `./synaptory deploy local`
    left there and resolves the ambient installed CLI -- which is how 23 SPQ
    barrier tests failed only on a laptop that had deployed locally. Tests exec
    a stamp-free copy instead; this asserts the copy actually is one, in the
    child, where it matters.
    """
    for name in host_env.CP_URL_FILENAMES:
        assert not (stamp_free_runtime.lib / name).exists()

    probe = subprocess.run(
        [sys.executable, "-c",
         "import host_env; print(repr(host_env.plugin_channel_source()))"],
        cwd=str(stamp_free_runtime.lib),
        env={**stamp_free_runtime.env(), "PYTHONPATH": str(stamp_free_runtime.lib)},
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "''", probe.stdout
