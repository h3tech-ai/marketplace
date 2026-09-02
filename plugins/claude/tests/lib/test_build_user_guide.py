"""Layer 1 — `plugin-claude/scripts/build_user_guide.py` version-stamp tests.

Locks the contract that every guide build carries machine-readable
version metadata in the HTML, regardless of how the builder was invoked
(explicit `--version`, ``SYNAPTORY_VERSION`` env, or `/VERSION` fallback).

The release pipeline relies on these stamps to make cross-module version
drift visible to operators: every artefact (CLI, plugin, marketplace.json,
container images, single-file user guide) must carry the same /VERSION.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Load the builder as a module so we can call its functions directly,
# without spawning a subprocess for each assertion.
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[3]
_BUILDER_PATH = _REPO_ROOT / "plugin-claude" / "scripts" / "build_user_guide.py"


@pytest.fixture(scope="module")
def builder_module():
    spec = importlib.util.spec_from_file_location("build_user_guide", _BUILDER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_user_guide"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
def test_doc_groups_includes_every_user_guide_markdown(builder_module):
    """Every `*.md` under docs/user-guide/ must be registered in DOC_GROUPS.

    Regression guard for the bug that shipped in v2.7.0 / v2.7.1: the new
    multi-spec guide was created at docs/user-guide/guides/multi-spec.md but
    forgotten in DOC_GROUPS, so it never appeared in the rendered HTML on
    https://synaptory.h3t.co/docs/user-guide.html. The builder enumerates docs
    explicitly (deliberate — controls section ordering and group names) so
    we need a separate check that the enumeration stays in sync with the
    filesystem.
    """
    docs_root = _REPO_ROOT / "docs" / "user-guide"
    on_disk = {
        p.relative_to(docs_root).as_posix()
        for p in docs_root.rglob("*.md")
    }
    registered = {
        rel for _group, files in builder_module.DOC_GROUPS for rel in files
    }
    missing = on_disk - registered
    assert not missing, (
        f"docs/user-guide/ contains markdown files that aren't listed in "
        f"DOC_GROUPS — they won't be rendered into user-guide.html. "
        f"Add them to the appropriate group in build_user_guide.py: {sorted(missing)}"
    )

    extra = registered - on_disk
    assert not extra, (
        f"DOC_GROUPS references files that don't exist under docs/user-guide/. "
        f"Either create them or remove from build_user_guide.py: {sorted(extra)}"
    )


@pytest.mark.unit
def test_spq_content_precedes_legacy_compatibility(builder_module):
    """The full guide's information architecture must teach SPQ first."""
    groups = [group for group, _files in builder_module.DOC_GROUPS]
    assert groups.index("SPQ Concepts") < groups.index("Guides")
    assert groups.index("Guides") < groups.index("Legacy")

    ordered = [
        rel for _group, files in builder_module.DOC_GROUPS for rel in files
    ]
    assert ordered.index("guides/spq-delivery.md") < ordered.index(
        "guides/scrum-delivery.md"
    )
    assert ordered.index("concepts/hosts-and-runtimes.md") < ordered.index(
        "guides/scrum-delivery.md"
    )


@pytest.mark.unit
def test_getting_started_build_covers_all_hosts_and_spq(tmp_path):
    """The builder emits both promised HTML artifacts from default sources."""
    guide = tmp_path / "user-guide.html"
    result = subprocess.run(
        [
            sys.executable,
            str(_BUILDER_PATH),
            "--output",
            str(guide),
            "--no-web",
            "--built-at",
            "",
            "--no-git-sha",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    onboarding = tmp_path / "getting-started.html"
    assert guide.exists()
    assert onboarding.exists()
    full_rendered = guide.read_text(encoding="utf-8")
    rendered = onboarding.read_text(encoding="utf-8")
    assert "Claude Code" in rendered
    assert "Codex" in rendered
    assert "Cursor IDE" in rendered
    assert "build_mode: &quot;spq&quot;" in rendered
    assert "Discovery" in rendered
    assert "user-guide.html#concepts-hosts-and-runtimes" in rendered
    relative_markdown_href = re.compile(
        r'href="(?!https?://|mailto:|#)[^"]+\.md(?:#[^"]*)?"'
    )
    assert not relative_markdown_href.search(full_rendered)
    assert not relative_markdown_href.search(rendered)


@pytest.mark.unit
def test_user_guide_local_markdown_links_exist(builder_module):
    """Reader-facing Markdown links must point at existing files and headings."""
    docs_root = _REPO_ROOT / "docs" / "user-guide"
    missing: list[str] = []
    missing_fragments: list[str] = []
    heading_cache: dict[Path, set[str]] = {}
    link_pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    for source in docs_root.rglob("*.md"):
        for target in link_pattern.findall(source.read_text(encoding="utf-8")):
            target = target.strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _separator, fragment = target.partition("#")
            resolved = source.resolve() if not file_part else (
                source.parent / file_part
            ).resolve()
            if not resolved.exists():
                missing.append(
                    f"{source.relative_to(docs_root)} -> {target}"
                )
                continue
            if fragment and resolved.suffix == ".md" and resolved.is_relative_to(docs_root):
                if resolved not in heading_cache:
                    headings = set()
                    for line in resolved.read_text(encoding="utf-8").splitlines():
                        match = re.match(r"^#{1,6}\s+(.*)$", line)
                        if match:
                            headings.add(
                                builder_module.slugify(
                                    builder_module.strip_inline_markdown(match.group(1))
                                )
                            )
                    heading_cache[resolved] = headings
                if builder_module.slugify(fragment) not in heading_cache[resolved]:
                    missing_fragments.append(
                        f"{source.relative_to(docs_root)} -> {target}"
                    )
    assert not missing, "Broken local documentation links:\n" + "\n".join(missing)
    assert not missing_fragments, (
        "Broken local documentation fragments:\n" + "\n".join(missing_fragments)
    )


@pytest.mark.unit
def test_primary_guides_do_not_document_private_lifecycle_commands():
    """Public SPQ docs must route state changes through hosts/MCP, not internals."""
    docs_root = _REPO_ROOT / "docs" / "user-guide"
    primary = [
        docs_root / "getting-started.md",
        docs_root / "guides" / "spq-delivery.md",
        docs_root / "reference" / "commands.md",
        docs_root / "troubleshooting.md",
    ]
    forbidden = re.compile(r"python3\s+[^\n`]*(?:state_machine|tracker_cli)\.py")
    violations = [str(path) for path in primary if forbidden.search(path.read_text())]
    assert not violations, f"Private implementation commands exposed in: {violations}"


@pytest.mark.unit
def test_resolve_version_uses_explicit_env(builder_module, monkeypatch):
    """``SYNAPTORY_VERSION=…`` env wins over /VERSION, normalised with leading v."""
    monkeypatch.setenv("SYNAPTORY_VERSION", "9.9.9")
    assert builder_module.resolve_version() == "v9.9.9"


@pytest.mark.unit
def test_resolve_version_passes_through_leading_v(builder_module, monkeypatch):
    """Already-normalised env value stays as-is — no double-v."""
    monkeypatch.setenv("SYNAPTORY_VERSION", "v1.2.3")
    assert builder_module.resolve_version() == "v1.2.3"


@pytest.mark.unit
def test_resolve_version_falls_back_to_dev_for_dev_default(builder_module, monkeypatch):
    """The Dockerfile default ``0.0.0-dev`` renders as the literal "dev" so
    operators don't mistake an unstamped build for a real semver release."""
    monkeypatch.setenv("SYNAPTORY_VERSION", "0.0.0-dev")
    assert builder_module.resolve_version() == "dev"


@pytest.mark.unit
def test_resolve_version_falls_back_to_version_file(builder_module, monkeypatch):
    """When env is empty, /VERSION is the source of truth."""
    monkeypatch.delenv("SYNAPTORY_VERSION", raising=False)
    version_file = _REPO_ROOT / "VERSION"
    assert version_file.exists(), "VERSION file missing — repo invariant broken"
    expected = version_file.read_text(encoding="utf-8").strip()
    expected_label = "dev" if expected in ("", "0.0.0-dev") else (
        expected if expected.startswith("v") else f"v{expected}"
    )
    assert builder_module.resolve_version() == expected_label


@pytest.mark.unit
def test_build_html_renders_version_chip_and_meta(builder_module):
    """Hero block carries a version chip; HEAD carries machine-readable meta."""
    docs = builder_module.resolve_doc_map()
    for doc in docs:
        builder_module.parse_headings(doc)
    link_map = builder_module.build_link_map(docs)
    for doc in docs:
        doc.body_html = builder_module.render_markdown(doc, link_map)

    html = builder_module.build_html(
        docs,
        builder_module.load_brand(),
        version="2.5.31",
        built_at="2026-05-07T12:00:00Z",
        git_sha="abc1234",
    )
    # Visible chip — what humans see.
    assert 'data-stamp="version">v2.5.31' in html
    assert 'data-stamp="built-at">built 2026-05-07T12:00:00Z' in html
    assert 'data-stamp="git-sha">abc1234' in html
    # Machine-readable HEAD meta — what release-time tooling scrapes.
    assert '<meta name="synaptory-version" content="v2.5.31">' in html
    assert (
        '<meta name="synaptory-built-at" content="2026-05-07T12:00:00Z">'
        in html
    )
    assert '<meta name="synaptory-git-sha" content="abc1234">' in html


@pytest.mark.unit
def test_build_html_omits_optional_chips_when_unsupplied(builder_module):
    """built_at + git_sha chips/meta only render when supplied. Version
    chip is always present (defaults to "dev")."""
    docs = builder_module.resolve_doc_map()
    for doc in docs:
        builder_module.parse_headings(doc)
    link_map = builder_module.build_link_map(docs)
    for doc in docs:
        doc.body_html = builder_module.render_markdown(doc, link_map)

    html = builder_module.build_html(
        docs,
        builder_module.load_brand(),
        version=None,
        built_at=None,
        git_sha=None,
    )
    assert 'data-stamp="version">dev' in html
    assert 'data-stamp="built-at"' not in html
    assert 'data-stamp="git-sha"' not in html
    assert '<meta name="synaptory-built-at"' not in html
    assert '<meta name="synaptory-git-sha"' not in html


@pytest.mark.unit
def test_build_html_normalises_explicit_version_arg(builder_module):
    """Even when called from Python (skipping the env/file resolver), the
    version label is normalised with leading ``v`` so the hero chip looks
    consistent across CLI invocations and library usage."""
    docs = builder_module.resolve_doc_map()
    for doc in docs:
        builder_module.parse_headings(doc)
    link_map = builder_module.build_link_map(docs)
    for doc in docs:
        doc.body_html = builder_module.render_markdown(doc, link_map)

    html = builder_module.build_html(
        docs,
        builder_module.load_brand(),
        version="2.5.31",  # no leading v
    )
    assert 'data-stamp="version">v2.5.31' in html
    # And again with leading v — should not double up.
    html2 = builder_module.build_html(
        docs, builder_module.load_brand(), version="v9.9.9"
    )
    assert 'data-stamp="version">v9.9.9' in html2
    assert 'data-stamp="version">vv9.9.9' not in html2


@pytest.mark.unit
def test_render_markdown_hides_front_matter_and_resolves_local_fragment(
    builder_module, tmp_path
):
    """Specification metadata stays in Markdown while generated readers get
    clean prose and working links to headings in the same source document."""
    source = tmp_path / "spec.md"
    source.write_text(
        "---\n"
        "spec_id: TEST\n"
        "status: Drafting\n"
        "---\n\n"
        "# Test Specification\n\n"
        "[Jump to details](#details)\n\n"
        "## Details\n\n"
        "Reader-facing content.\n",
        encoding="utf-8",
    )
    doc = builder_module.Document(
        rel_path="spec.md",
        group="Test",
        order=0,
        source_path=source,
        title="Test Specification",
        doc_anchor="spec",
    )
    builder_module.parse_headings(doc)
    link_map = builder_module.build_link_map([doc])

    rendered = builder_module.render_markdown(doc, link_map)

    assert "spec_id" not in rendered
    assert "status: Drafting" not in rendered
    assert '<a href="#spec-details">Jump to details</a>' in rendered
    assert '<h2 id="spec-details">Details</h2>' in rendered


@pytest.mark.unit
def test_render_inline_preserves_underscores_in_repository_links(builder_module):
    """Resolved repository URLs must not be reparsed as Markdown emphasis."""
    doc = builder_module.Document(
        rel_path="concepts/identity-and-access.md",
        group="Test",
        order=0,
        source_path=_REPO_ROOT / "docs" / "user-guide" / "concepts" / "identity-and-access.md",
        title="Identity and Access",
        doc_anchor="identity-and-access",
    )

    rendered = builder_module.render_inline(
        "[watermark.py](../../../api/synaptory_api/watermark.py)", doc, {}
    )

    assert (
        'href="https://github.com/h3tech-ai/synaptory-v1/blob/main/'
        'api/synaptory_api/watermark.py"' in rendered
    )
    assert "<em>" not in rendered
