"""Install the region-registry double. Imported by every suite's conftest.

ONE DEFINITION, IMPORTED EXPLICITLY, because relying on a conftest was how
this broke. `plugin-claude/tests/conftest.py`'s autouse fixture applies to its
own subtree only, so the Codex and Cursor suites never got it -- and yet the
combined `./synaptory test` run passed locally while CI failed. The reason is
the conftest module-name collision (`plugin-cursor/tests/conftest.py` and
`plugin-codex/tests/conftest.py` both import as `conftest`): in one ordering
the plugin-claude fixture leaked across and the Codex tests passed for a
reason unrelated to their own configuration.

A local pass that depends on collection order is not a pass. So the double is
a module every conftest imports by path, and each host's suite installs it for
itself.
"""

from __future__ import annotations

from pathlib import Path

#: The stand-in `synaptory`. See its own docstring for why it holds state.
FAKE_REGISTRY = Path(__file__).resolve().parent / "fake_region_registry.py"


def install(monkeypatch, store_dir: Path) -> None:
    """Point `region_registry` at the double, in-process and for subprocesses.

    `SYNAPTORY_REGION_REGISTRY_BIN` covers the hosts that shell out to
    `spq_state_machine.py`; the attribute patch covers the ones that import it.
    Both, because a suite that had only one of them refused every Commit on
    the other kind of host and read as a host conformance gap.
    """
    store = Path(store_dir) / "region-registry.json"
    monkeypatch.setenv("SYNAPTORY_FAKE_REGISTRY", str(store))
    monkeypatch.setenv("SYNAPTORY_REGION_REGISTRY_BIN", str(FAKE_REGISTRY))
    try:
        import region_registry
    except ImportError:  # pragma: no cover - suites that import no runtime
        return
    monkeypatch.setattr(
        region_registry, "_resolve_cli", lambda: str(FAKE_REGISTRY)
    )
