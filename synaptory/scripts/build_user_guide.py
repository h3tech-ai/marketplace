#!/usr/bin/env python3
"""Build a single-file HTML user guide from docs/user-guide Markdown sources."""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import html
import json
import mimetypes
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = REPO_ROOT / "docs" / "user-guide"
BRAND_PATH = REPO_ROOT / "docs" / "h3t_brand_identity.json"
VENDOR_MERMAID = REPO_ROOT / "plugin" / "scripts" / "vendor" / "mermaid.min.js"
DEFAULT_OUTPUT   = REPO_ROOT / "dist" / "user-guide.html"
WEB_DIST_OUTPUT  = REPO_ROOT / "web" / "dist" / "docs" / "user-guide.html"

DOC_GROUPS = [
    ("Start", ["index.md", "getting-started.md", "install-cli.md"]),
    (
        "Concepts",
        [
            "concepts/platform.md",
            "concepts/architecture.md",
            "concepts/identity-and-access.md",
            "concepts/personas.md",
            "concepts/delivery-lifecycle.md",
            "concepts/modes.md",
            "concepts/agents.md",
            "concepts/engagement-modes.md",
            "concepts/enforcement.md",
            "concepts/v2-roadmap.md",
        ],
    ),
    (
        "Guides",
        [
            "guides/scrum-delivery.md",
            "guides/kanban-delivery.md",
            "guides/spq-delivery.md",
            "spq-setup-runbook.md",
            "guides/multi-spec.md",
            "guides/release.md",
            "guides/resuming-pipelines.md",
            "guides/using-the-control-plane.md",
            "guides/inviting-members.md",
            "guides/migrate-from-hiro-crew.md",  # transitional — remove post-cutover
        ],
    ),
    (
        "Reference",
        [
            "reference/config.md",
            "reference/commands.md",
            "reference/routing.md",
            "reference/rules.md",
            "reference/hooks.md",
            "reference/urls.md",
            "reference/auth-flows.md",
            "reference/permissions.md",
            "reference/glossary.md",
            "troubleshooting.md",
        ],
    ),
]


@dataclass
class Heading:
    level: int
    text: str
    anchor: str


@dataclass
class Document:
    rel_path: str
    group: str
    order: int
    source_path: Path
    title: str
    doc_anchor: str
    headings: list[Heading] = field(default_factory=list)
    body_html: str = ""


def slugify(value: str) -> str:
    value = unquote(value).strip().lower()
    value = re.sub(r"[`*_~]+", "", value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "section"


def load_brand() -> dict:
    if not BRAND_PATH.exists():
        return {}
    return json.loads(BRAND_PATH.read_text(encoding="utf-8"))


def inline_asset(path: Path) -> str | None:
    if not path.exists():
        return None
    mime, _ = mimetypes.guess_type(path.name)
    if not mime:
        return None
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def strip_inline_markdown(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    return html.unescape(text).strip()


def resolve_doc_map() -> list[Document]:
    docs: list[Document] = []
    order = 0
    for group, rel_paths in DOC_GROUPS:
        for rel_path in rel_paths:
            source_path = DOCS_ROOT / rel_path
            if not source_path.exists():
                continue
            title = extract_title(source_path.read_text(encoding="utf-8"), fallback=source_path.stem)
            docs.append(
                Document(
                    rel_path=rel_path,
                    group=group,
                    order=order,
                    source_path=source_path,
                    title=title,
                    doc_anchor=slugify(rel_path.replace(".md", "").replace("/", "-")),
                )
            )
            order += 1
    return docs


def extract_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return strip_inline_markdown(line[2:])
    return fallback.replace("-", " ").title()


def build_link_map(docs: Iterable[Document]) -> dict[tuple[str, str | None], str]:
    link_map: dict[tuple[str, str | None], str] = {}
    for doc in docs:
        link_map[(doc.rel_path, None)] = f"#{doc.doc_anchor}"
        for heading in doc.headings:
            link_map[(doc.rel_path, slugify(heading.text))] = f"#{heading.anchor}"
    return link_map


def parse_headings(doc: Document) -> None:
    seen: dict[str, int] = {}
    for line in doc.source_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not match:
            continue
        level = len(match.group(1))
        text = strip_inline_markdown(match.group(2))
        base = f"{doc.doc_anchor}-{slugify(text)}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchor = base if count == 0 else f"{base}-{count + 1}"
        doc.headings.append(Heading(level=level, text=text, anchor=anchor))


def render_inline(text: str, current_doc: Document, link_map: dict[tuple[str, str | None], str]) -> str:
    code_spans: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_spans.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"@@CODE{len(code_spans) - 1}@@"

    text = re.sub(r"`([^`]+)`", stash_code, text)
    text = html.escape(text)

    def repl_link(match: re.Match[str]) -> str:
        label = render_inline(match.group(1), current_doc, link_map)
        target = match.group(2).strip()
        resolved = resolve_link(current_doc.rel_path, target, link_map)
        attrs = ' target="_blank" rel="noreferrer"' if resolved.startswith("http") else ""
        return f'<a href="{html.escape(resolved, quote=True)}"{attrs}>{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", repl_link, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"<em>\1</em>", text)

    for idx, code in enumerate(code_spans):
        text = text.replace(f"@@CODE{idx}@@", code)
    return text


def resolve_link(current_rel: str, target: str, link_map: dict[tuple[str, str | None], str]) -> str:
    if target.startswith(("http://", "https://", "mailto:")):
        return target
    if target == "#":
        return "#"
    doc_part, _, fragment = target.partition("#")
    if not doc_part:
        if not fragment:
            return "#"
        fragment_key = slugify(fragment)
        return link_map.get((current_rel, fragment_key), f"#{fragment_key}")

    current_doc_path = DOCS_ROOT / current_rel
    target_path = (current_doc_path.parent / doc_part).resolve()
    try:
        rel_path = target_path.relative_to(DOCS_ROOT.resolve()).as_posix()
    except ValueError:
        return target
    frag_key = slugify(fragment) if fragment else None
    return link_map.get((rel_path, frag_key)) or link_map.get((rel_path, None)) or target


def render_markdown(doc: Document, link_map: dict[tuple[str, str | None], str]) -> str:
    lines = strip_front_matter(doc.source_path.read_text(encoding="utf-8").splitlines())
    return render_markdown_lines(lines, doc, link_map)


def strip_front_matter(lines: list[str]) -> list[str]:
    """Remove a leading YAML front-matter block from Markdown input.

    Specification sources use front matter for machine-readable governance.
    It must remain in Markdown but must not render as reader-facing prose in
    the generated single-file document. A lone leading horizontal rule is
    preserved when no closing delimiter exists.
    """
    if not lines or lines[0].strip() != "---":
        return lines
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[index + 1 :]
    return lines


def render_markdown_lines(
    lines: list[str],
    doc: Document,
    link_map: dict[tuple[str, str | None], str],
) -> str:
    headings = iter(doc.headings)
    current_heading = next(headings, None)
    out: list[str] = []
    i = 0

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        if not stripped:
            i += 1
            continue

        if raw.startswith("```"):
            lang = raw[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            body = chr(10).join(code_lines)
            if lang.lower() == "mermaid":
                # Emit a mermaid container; the vendored mermaid.js (injected
                # by build_html when any diagram is present) renders it to SVG.
                # Escaping is correct: mermaid reads textContent, which the
                # browser un-escapes back to the raw diagram source.
                out.append(f'<pre class="mermaid">{html.escape(body)}</pre>')
            else:
                lang_attr = f' data-lang="{html.escape(lang, quote=True)}"' if lang else ""
                out.append(
                    f'<pre class="code-block"{lang_attr}><code>{html.escape(body)}</code></pre>'
                )
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if heading_match:
            level = len(heading_match.group(1))
            text = strip_inline_markdown(heading_match.group(2))
            anchor = current_heading.anchor if current_heading else f"{doc.doc_anchor}-{slugify(text)}"
            out.append(
                f'<h{level} id="{anchor}">{render_inline(heading_match.group(2), doc, link_map)}</h{level}>'
            )
            current_heading = next(headings, None)
            i += 1
            continue

        if stripped == "---":
            out.append("<hr>")
            i += 1
            continue

        if raw.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].startswith(">"):
                quote_lines.append(lines[i][1:].lstrip())
                i += 1
            quote_doc = Document(
                rel_path=doc.rel_path,
                group=doc.group,
                order=doc.order,
                source_path=doc.source_path,
                title=doc.title,
                doc_anchor=doc.doc_anchor,
            )
            out.append(f'<blockquote>{render_markdown_lines(quote_lines, quote_doc, link_map)}</blockquote>')
            continue

        if stripped.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            out.append(render_table(table_lines, doc, link_map))
            continue

        if re.match(r"^[-*]\s+", stripped):
            list_lines = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                list_lines.append(lines[i])
                i += 1
            out.append(render_list(list_lines, ordered=False, doc=doc, link_map=link_map))
            continue

        if re.match(r"^\d+\.\s+", stripped):
            list_lines = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                list_lines.append(lines[i])
                i += 1
            out.append(render_list(list_lines, ordered=True, doc=doc, link_map=link_map))
            continue

        para_lines = [raw.strip()]
        i += 1
        while i < len(lines):
            next_line = lines[i]
            next_stripped = next_line.strip()
            if not next_stripped:
                break
            if next_line.startswith(("```", ">", "#")):
                break
            if next_stripped == "---":
                break
            if next_stripped.startswith("|"):
                break
            if re.match(r"^[-*]\s+", next_stripped) or re.match(r"^\d+\.\s+", next_stripped):
                break
            para_lines.append(next_stripped)
            i += 1
        out.append(f"<p>{render_inline(' '.join(para_lines), doc, link_map)}</p>")

    return "\n".join(out)


def render_list(
    list_lines: list[str],
    *,
    ordered: bool,
    doc: Document,
    link_map: dict[tuple[str, str | None], str],
) -> str:
    tag = "ol" if ordered else "ul"
    items = []
    pattern = r"^\s*\d+\.\s+" if ordered else r"^\s*[-*]\s+"
    for line in list_lines:
        item = re.sub(pattern, "", line.strip(), count=1)
        items.append(f"<li>{render_inline(item, doc, link_map)}</li>")
    return f"<{tag}>\n{''.join(items)}\n</{tag}>"


def render_table(
    table_lines: list[str],
    doc: Document,
    link_map: dict[tuple[str, str | None], str],
) -> str:
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in table_lines
        if line.strip().strip("|")
    ]
    if len(rows) < 2:
        return ""
    header = rows[0]
    body = rows[2:] if re.fullmatch(r"[:\-\s|]+", table_lines[1].strip()) else rows[1:]
    thead = "".join(f"<th>{render_inline(cell, doc, link_map)}</th>" for cell in header)
    tbody = []
    for row in body:
        cells = row + [""] * (len(header) - len(row))
        tbody.append("<tr>" + "".join(f"<td>{render_inline(cell, doc, link_map)}</td>" for cell in cells[: len(header)]) + "</tr>")
    return f"<div class=\"table-wrap\"><table><thead><tr>{thead}</tr></thead><tbody>{''.join(tbody)}</tbody></table></div>"


def render_nav(docs: list[Document]) -> str:
    groups: dict[str, list[Document]] = {}
    for doc in docs:
        groups.setdefault(doc.group, []).append(doc)

    sections = []
    for group, _ in DOC_GROUPS:
        entries = groups.get(group)
        if not entries:
            continue
        links = "".join(
            f'<a class="nav-link" href="#{doc.doc_anchor}"><span>{doc.order + 1:02d}</span>{html.escape(doc.title)}</a>'
            for doc in entries
        )
        sections.append(f'<div class="nav-group"><div class="nav-group-title">{html.escape(group)}</div>{links}</div>')
    return "".join(sections)


def render_outline(docs: list[Document]) -> str:
    max_h2_per_doc = 5
    items = []
    for doc in docs:
        items.append(f'<a class="outline-doc" href="#{doc.doc_anchor}">{html.escape(doc.title)}</a>')
        count = 0
        for heading in doc.headings:
            if heading.level != 2:
                continue
            if heading.text == doc.title:
                continue
            if count >= max_h2_per_doc:
                break
            items.append(f'<a class="outline-sub" href="#{heading.anchor}">{html.escape(heading.text)}</a>')
            count += 1
    return "".join(items)


def render_documents(docs: list[Document]) -> str:
    sections = []
    for doc in docs:
        sections.append(
            f"""
<section class="chapter" id="{doc.doc_anchor}">
  <div class="chapter-kicker">{html.escape(doc.group)} · {doc.order + 1:02d}</div>
  <div class="chapter-body">
    {doc.body_html}
  </div>
</section>
"""
        )
    return "\n".join(sections)


def resolve_version() -> str:
    """Resolve the build version from /VERSION (single source of truth, ADR-018).

    Order of resolution:
      1. ``SYNAPTORY_VERSION`` env var — set by CI when ``release.yml`` invokes us.
      2. ``REPO_ROOT/VERSION`` file — committed source of truth.
      3. ``"dev"`` — local builds without either.

    The label is normalised to ``v<x.y.z>`` (with leading ``v``) so it scans
    as a version at a glance, matching the Sidebar footer convention.
    """
    raw = os.environ.get("SYNAPTORY_VERSION", "").strip()
    if not raw:
        version_file = REPO_ROOT / "VERSION"
        if version_file.exists():
            raw = version_file.read_text(encoding="utf-8").strip()
    if not raw or raw == "0.0.0-dev":
        return "dev"
    return raw if raw.startswith("v") else f"v{raw}"


def resolve_git_sha() -> str | None:
    """Short git SHA at HEAD, or ``None`` outside a git checkout.

    Useful in dev builds to disambiguate "v2.5.31 with this WIP commit on top"
    from "v2.5.31 release". The release pipeline builds from a clean tag so
    the SHA matches the tag's commit.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        sha = out.stdout.strip()
        return sha or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def build_html(
    docs: list[Document],
    brand: dict,
    *,
    version: str | None = None,
    built_at: str | None = None,
    git_sha: str | None = None,
    title: str | None = None,
    mermaid_js: str | None = None,
) -> str:
    colors = brand.get("colors", {})
    typography = brand.get("typography", {})
    tables = brand.get("tables", {})
    table_header = tables.get("header", {})
    primary_font = typography.get("fontFamily", {}).get("primary", "Inter, sans-serif").strip()
    accent_font = typography.get("fontFamily", {}).get("accent", "Courier, monospace").strip()
    base_font = typography.get("baseFontSizePx", 14)
    scale = typography.get("scaleRem", {})
    line_height = typography.get("lineHeight", {})

    logo_rel = brand.get("logo")
    logo_src = inline_asset(REPO_ROOT / "docs" / logo_rel) if logo_rel else None
    hero_src = None  # banner removed to reduce file size

    title_doc = title or (docs[0].title if docs else "synaptory User Guide")
    subtitle = "Single-file documentation build for offline distribution."

    # Inline the vendored mermaid bundle only when a diagram is present, so
    # diagram-free builds stay lean and the output remains fully offline.
    mermaid_script = ""
    if mermaid_js:
        mermaid_script = (
            f"<script>{mermaid_js}</script>\n"
            "<script>mermaid.initialize({startOnLoad:true,securityLevel:'loose'});</script>"
        )

    try:
        docs_rel = DOCS_ROOT.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        docs_rel = "docs/user-guide"

    # Version + build-time stamps. Always render the version chip; the
    # built-at/git-sha chips render only when supplied so dev runs don't
    # leak transient timestamps into committed reproduction tests.
    raw_version = (version or "dev").strip() or "dev"
    # Normalise to ``v<x.y.z>`` so the chip scans as a version regardless
    # of whether the caller passed ``2.5.31``, ``v2.5.31``, or ``"dev"``.
    if raw_version == "dev" or raw_version.startswith("v"):
        version_label = raw_version
    else:
        version_label = f"v{raw_version}"
    version_chip = (
        f'<div class="hero-chip" data-stamp="version">'
        f'{html.escape(version_label)}</div>'
    )
    built_at_chip = (
        f'<div class="hero-chip" data-stamp="built-at">'
        f'built {html.escape(built_at)}</div>'
        if built_at
        else ""
    )
    git_sha_chip = (
        f'<div class="hero-chip" data-stamp="git-sha">'
        f'{html.escape(git_sha)}</div>'
        if git_sha
        else ""
    )
    extra_meta_chips = "".join([version_chip, git_sha_chip, built_at_chip])

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title_doc)}</title>
  <meta name="synaptory-version" content="{html.escape(version_label)}">
  {f'<meta name="synaptory-built-at" content="{html.escape(built_at)}">' if built_at else ''}
  {f'<meta name="synaptory-git-sha" content="{html.escape(git_sha)}">' if git_sha else ''}
  <style>
    :root {{
      --brand: {colors.get("primary", "#E00000")};
      --brand-ink: {colors.get("text", "#000000")};
      --bg: {colors.get("background", "#FFFFFF")};
      --surface: #ffffff;
      --surface-alt: {colors.get("surface", "#E6E6E6")};
      --muted: {colors.get("secondaryText", "#666666")};
      --white: {colors.get("white", "#FFFFFF")};
      --table-head: {table_header.get("background", colors.get("primary", "#E00000"))};
      --table-head-ink: {table_header.get("text", "#FFFFFF")};
      --table-border: {tables.get("border", "#E6E6E6")};
      --shadow: 0 18px 45px rgba(0, 0, 0, 0.08);
      --radius: 18px;
      --radius-sm: 10px;
      --content-max: 860px;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      font-family: {primary_font};
      font-size: {base_font}px;
      line-height: {line_height.get("body", 1.6)};
      color: var(--brand-ink);
      background:
        radial-gradient(circle at top left, rgba(224, 0, 0, 0.08), transparent 28rem),
        linear-gradient(180deg, #fff 0%, #faf8f7 100%);
    }}
    a {{ color: var(--brand); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code, pre {{ font-family: {accent_font}; }}
    .page {{
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr) 240px;
      gap: 24px;
      max-width: 1600px;
      margin: 0 auto;
      padding: 24px;
    }}
    .sidebar, .outline {{
      position: sticky;
      top: 20px;
      align-self: start;
      max-height: calc(100vh - 40px);
      overflow: auto;
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(0, 0, 0, 0.06);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }}
    .sidebar {{ padding: 22px 18px; }}
    .outline {{ padding: 20px 16px; }}
    .brand-lockup {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 20px;
      padding-bottom: 18px;
      border-bottom: 1px solid rgba(0, 0, 0, 0.08);
    }}
    .brand-mark {{
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--brand), #2e0000);
      display: grid;
      place-items: center;
      color: var(--white);
      font-weight: 800;
      letter-spacing: 0.06em;
      flex: 0 0 auto;
      overflow: hidden;
    }}
    .brand-mark img {{ width: 100%; height: 100%; object-fit: cover; }}
    .brand-text strong {{
      display: block;
      font-size: 1rem;
      line-height: 1.1;
    }}
    .brand-text span {{
      display: block;
      color: var(--muted);
      font-size: 0.84rem;
      margin-top: 4px;
    }}
    .nav-group + .nav-group {{ margin-top: 22px; }}
    .nav-group-title {{
      font-size: 0.78rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 8px;
    }}
    .nav-link {{
      display: flex;
      gap: 10px;
      align-items: baseline;
      padding: 8px 10px;
      border-radius: 10px;
      color: var(--brand-ink);
    }}
    .nav-link span {{
      color: var(--brand);
      font-weight: 700;
      min-width: 22px;
    }}
    .nav-link:hover {{
      background: rgba(224, 0, 0, 0.06);
      text-decoration: none;
    }}
    main {{
      min-width: 0;
    }}
    .hero {{
      position: relative;
      overflow: hidden;
      border-radius: 28px;
      background:
        linear-gradient(120deg, rgba(0, 0, 0, 0.92), rgba(40, 0, 0, 0.82)),
        linear-gradient(135deg, var(--brand), #1c1c1c);
      color: var(--white);
      box-shadow: var(--shadow);
      margin-bottom: 24px;
    }}
    .hero::after {{
      content: "";
      position: absolute;
      inset: auto -8% -35% auto;
      width: 320px;
      height: 320px;
      border-radius: 999px;
      background: radial-gradient(circle, rgba(224, 0, 0, 0.55), transparent 70%);
      pointer-events: none;
    }}
    .hero-inner {{
      position: relative;
      z-index: 1;
      padding: 44px 40px;
      max-width: 760px;
    }}
    .hero-kicker {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.14);
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 14px;
    }}
    .hero h1 {{
      margin: 0;
      font-size: clamp(2rem, 4.6vw, {scale.get("h1", 1.6) * 2.1:.2f}rem);
      line-height: 1.08;
      letter-spacing: -0.025em;
      word-break: normal;
      overflow-wrap: break-word;
      hyphens: none;
    }}
    .hero p {{
      margin: 12px 0 0;
      max-width: 58ch;
      color: rgba(255, 255, 255, 0.84);
      font-size: 1.04rem;
    }}
    .hero-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 20px;
    }}
    .hero-chip {{
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.1);
      border: 1px solid rgba(255, 255, 255, 0.12);
      font-size: 0.82rem;
    }}
    .chapter {{
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid rgba(0, 0, 0, 0.06);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 28px 34px;
    }}
    .chapter + .chapter {{ margin-top: 18px; }}
    .chapter-kicker {{
      color: var(--brand);
      font-size: 0.8rem;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 18px;
    }}
    .chapter-body {{
      max-width: var(--content-max);
    }}
    h1, h2, h3, h4, h5, h6 {{
      scroll-margin-top: 20px;
      line-height: {line_height.get("headings", 1.25)};
      letter-spacing: -0.02em;
      margin: 0;
    }}
    h1 {{ font-size: {scale.get("h1", 1.6):.2f}rem; margin-bottom: 16px; }}
    h2 {{
      font-size: {scale.get("h2", 1.4):.2f}rem;
      margin: 34px 0 14px;
      padding-top: 10px;
      border-top: 1px solid rgba(0, 0, 0, 0.08);
    }}
    h3 {{ font-size: {scale.get("h3", 1.2):.2f}rem; margin: 26px 0 12px; }}
    h4 {{ font-size: {scale.get("h4", 1.1):.2f}rem; margin: 20px 0 10px; }}
    p, ul, ol, blockquote, .table-wrap, pre, hr {{
      margin: 14px 0;
    }}
    ul, ol {{
      padding-left: 1.3rem;
    }}
    li + li {{ margin-top: 8px; }}
    hr {{
      border: 0;
      height: 1px;
      background: linear-gradient(90deg, var(--brand), transparent);
    }}
    blockquote {{
      margin-left: 0;
      padding: 14px 16px;
      border-left: 4px solid var(--brand);
      background: rgba(224, 0, 0, 0.05);
      border-radius: 0 14px 14px 0;
    }}
    .code-block {{
      overflow: auto;
      background: #121212;
      color: #f5f5f5;
      padding: 16px 18px;
      border-radius: 16px;
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.08);
    }}
    code {{
      background: rgba(224, 0, 0, 0.08);
      color: #750000;
      padding: 0.12rem 0.38rem;
      border-radius: 6px;
      font-size: 0.94em;
    }}
    pre code {{
      background: none;
      color: inherit;
      padding: 0;
      border-radius: 0;
    }}
    .mermaid {{
      background: #ffffff;
      border: 1px solid rgba(0, 0, 0, 0.06);
      border-radius: 16px;
      padding: 16px;
      margin: 14px 0;
      overflow: auto;
      text-align: center;
      white-space: normal;
      font-family: {primary_font};
      line-height: 1.3;
    }}
    .table-wrap {{
      overflow: auto;
      border: 1px solid var(--table-border);
      border-radius: 16px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--surface);
    }}
    th, td {{
      text-align: left;
      padding: 12px 14px;
      border-bottom: 1px solid var(--table-border);
      vertical-align: top;
    }}
    th {{
      background: var(--table-head);
      color: var(--table-head-ink);
      font-weight: {tables.get("header", {}).get("fontWeight", 700)};
    }}
    tbody tr:nth-child(even) td {{
      background: rgba(0, 0, 0, 0.02);
    }}
    .outline-title {{
      font-size: 0.78rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 10px;
    }}
    .outline-doc, .outline-sub {{
      display: block;
      padding: 6px 8px;
      border-radius: 8px;
      color: var(--brand-ink);
    }}
    .outline-doc {{
      font-weight: 600;
      margin-top: 8px;
    }}
    .outline-sub {{
      color: var(--muted);
      padding-left: 16px;
      font-size: 0.95rem;
    }}
    .outline-doc:hover, .outline-sub:hover {{
      background: rgba(224, 0, 0, 0.05);
      text-decoration: none;
    }}
    .footer {{
      color: var(--muted);
      font-size: 0.9rem;
      text-align: center;
      padding: 20px 0 10px;
    }}
    @media (max-width: 1220px) {{
      .page {{
        grid-template-columns: 260px minmax(0, 1fr);
      }}
      .outline {{
        display: none;
      }}
    }}
    @media (max-width: 860px) {{
      .page {{
        grid-template-columns: 1fr;
        padding: 14px;
      }}
      .sidebar, .outline {{
        position: static;
        max-height: none;
      }}
      .hero-inner {{
        padding: 30px 22px;
      }}
      .chapter {{
        padding: 22px 18px;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <aside class="sidebar">
      <div class="brand-lockup">
        <div class="brand-mark">{f'<img src="{logo_src}" alt="H3Tech logo">' if logo_src else 'H3T'}</div>
        <div class="brand-text">
          <strong>{html.escape(brand.get("brand", "H3Tech Inc."))}</strong>
          <span>synaptory documentation</span>
        </div>
      </div>
      {render_nav(docs)}
    </aside>
    <main>
      <section class="hero">
        <div class="hero-inner">
          <div>
            <div class="hero-kicker">H3Tech · Single-File Guide</div>
            <h1>{html.escape(title_doc)}</h1>
            <p>{html.escape(subtitle)}</p>
            <div class="hero-meta">
              <div class="hero-chip">{len(docs)} chapters</div>
              {extra_meta_chips}
            </div>
          </div>
        </div>
      </section>
      {render_documents(docs)}
      <div class="footer">Built from <code>{docs_rel}/*.md</code> with the H3Tech brand palette.</div>
    </main>
    <aside class="outline">
      <div class="outline-title">Section Outline</div>
      {render_outline(docs)}
    </aside>
  </div>
  {mermaid_script}
</body>
</html>
"""


def build(
    output_path: Path,
    *,
    version: str | None = None,
    built_at: str | None = None,
    git_sha: str | None = None,
    title: str | None = None,
) -> None:
    docs = resolve_doc_map()
    for doc in docs:
      parse_headings(doc)

    link_map = build_link_map(docs)
    for doc in docs:
        doc.body_html = render_markdown(doc, link_map)

    brand = load_brand()

    # Inline the vendored mermaid bundle only when at least one diagram is
    # present in the rendered output, keeping non-diagram builds lean.
    mermaid_js = None
    if any('class="mermaid"' in doc.body_html for doc in docs) and VENDOR_MERMAID.exists():
        mermaid_js = VENDOR_MERMAID.read_text(encoding="utf-8")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_html(
            docs,
            brand,
            version=version,
            built_at=built_at,
            git_sha=git_sha,
            title=title,
            mermaid_js=mermaid_js,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output HTML path")
    parser.add_argument("--no-web", action="store_true", help="Skip secondary write to web/dist/docs/")
    parser.add_argument(
        "--docs-root",
        type=Path,
        default=None,
        help="Docs source directory (default: docs/user-guide).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="JSON manifest of ordered {group, files} entries (and optional "
             "title). Default: the built-in v1.x DOC_GROUPS.",
    )
    parser.add_argument(
        "--web-output",
        type=Path,
        default=None,
        help="Override the secondary web/dist output path (implies a web write "
             "unless --no-web is also given).",
    )
    parser.add_argument(
        "--title",
        type=str,
        default=None,
        help="Page title. Defaults to the manifest title, then the first doc's H1.",
    )
    parser.add_argument(
        "--version",
        type=str,
        default=None,
        help="Version label to stamp into the hero. Defaults to /VERSION (or "
             "$SYNAPTORY_VERSION if set), normalised to v<x.y.z>.",
    )
    parser.add_argument(
        "--built-at",
        type=str,
        default=None,
        help="ISO-8601 timestamp to stamp as the build time. Defaults to "
             "now (UTC). Pass an empty string to suppress the chip.",
    )
    parser.add_argument(
        "--no-git-sha",
        action="store_true",
        help="Suppress the short git-SHA chip even when git is available.",
    )
    args = parser.parse_args()

    # Reassign module-level source/output constants from CLI args so the same
    # script can build either guide. Defaults preserve the v1.x behavior when
    # no flag is passed. Functions read these globals, so reassign before build.
    global DOCS_ROOT, DOC_GROUPS, WEB_DIST_OUTPUT
    manifest_title: str | None = None
    if args.docs_root is not None:
        DOCS_ROOT = args.docs_root.resolve()
    if args.manifest is not None:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        DOC_GROUPS = [(grp["group"], list(grp["files"])) for grp in manifest["groups"]]
        manifest_title = manifest.get("title")
    if args.web_output is not None:
        WEB_DIST_OUTPUT = args.web_output.resolve()
    title = args.title if args.title is not None else manifest_title

    # Resolve stamps once and reuse for every output path so both copies of
    # the guide carry identical metadata.
    version_label = args.version if args.version is not None else resolve_version()
    if args.built_at is None:
        built_at_label = (
            _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    else:
        # Empty string suppresses the chip — supports reproducible test runs.
        built_at_label = args.built_at or None
    git_sha_label = None if args.no_git_sha else resolve_git_sha()

    build(
        args.output.resolve(),
        version=version_label,
        built_at=built_at_label,
        git_sha=git_sha_label,
        title=title,
    )
    print(f"Wrote {args.output}")
    if not args.no_web:
        build(
            WEB_DIST_OUTPUT,
            version=version_label,
            built_at=built_at_label,
            git_sha=git_sha_label,
            title=title,
        )
        print(f"Wrote {WEB_DIST_OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
