"""Layer 1 - host-neutral content directives (#298).

These replace regexes over prose that failed silently in production:

  * `_BANG_CAT` matched only `` !`cat ...` ``. The table-cell spelling
    `` `!cat ...` `` was missed, so 38 instructions shipped to Cursor telling a
    host that cannot expand `!cat` to expand `!cat`.
  * `${CLAUDE_SKILL_DIR}` was rewritten to `${PLUGIN_ROOT}/roles`, dropping the
    role. Every rewritten plugin-relative path named a directory that does not
    exist.
  * One directive sat inside a fenced code block, where Claude never expands,
    so it had never worked at all.

None of the three failed a build. That is what the strict expander changes.
"""

from __future__ import annotations

from pathlib import Path

import compose_directives as cd
import pytest

REPO = Path(__file__).resolve().parents[3]


# ── include: plugin-owned content, resolved at compose time ──────────────────


def test_include_is_role_scoped_for_cursor():
    """The bug: the role segment was dropped, so the path did not exist."""
    out = cd.expand(
        "{{include: phases/01-discovery.md}}",
        host="cursor",
        role="solution-architect",
        strict=True,
    )
    assert "roles/solution-architect/phases/01-discovery.md" in out
    assert "roles/phases/" not in out


def test_include_keeps_inline_expansion_for_claude():
    """`!cat` inlines content with no round-trip and is not optional.

    Preserving it is the reason includes are not an MCP tool call: a tool call
    the model may skip is a weaker guarantee than content already in the prompt.
    """
    out = cd.expand(
        "{{include: phases/01-discovery.md}}", host="claude", role="sa", strict=True
    )
    assert out == "!`cat ${CLAUDE_SKILL_DIR}/phases/01-discovery.md`"


def test_include_fails_when_the_file_does_not_exist(tmp_path):
    with pytest.raises(cd.DirectiveError) as exc:
        cd.expand(
            "{{include: phases/nope.md}}",
            host="claude",
            role="sa",
            source_dir=tmp_path,
            strict=True,
        )
    assert "does not exist" in str(exc.value)


def test_include_cannot_escape_the_agent_directory():
    for bad in ("{{include: ../../etc/passwd}}", "{{include: /etc/passwd}}"):
        with pytest.raises(cd.DirectiveError):
            cd.expand(bad, host="claude", role="sa", strict=True)


# ── read: a file in the USER's project, at runtime ───────────────────────────


def test_read_is_not_validated_against_disk(tmp_path):
    """A project read cannot exist at compose time; validating it would fail
    every build. Conflating the two directives is the trap this avoids."""
    out = cd.expand(
        "{{read: .synaptory/.protocols/iron-laws.md}}",
        host="claude",
        source_dir=tmp_path,
        strict=True,
    )
    assert "iron-laws.md" in out


def test_read_preserves_the_fallback_message():
    out = cd.expand(
        "{{read: .synaptory.yaml | fallback: No config — defaults apply}}",
        host="claude",
        strict=True,
    )
    assert "No config — defaults apply" in out
    assert "2>/dev/null" in out


def test_read_without_fallback_is_quiet():
    out = cd.expand("{{read: a.md}}", host="claude", strict=True)
    assert out == "!`cat a.md 2>/dev/null || true`"


def test_read_path_may_contain_a_host_token():
    """`${CLAUDE_PLUGIN_ROOT}` closes with a brace; a naive path pattern stops
    there, the directive never matches, and the strict guard fails the build."""
    out = cd.expand(
        "{{read: ${CLAUDE_PLUGIN_ROOT}/skills/synaptory/routing-rules.json}}",
        host="cursor",
        strict=True,
    )
    assert "routing-rules.json" in out
    assert "{{read:" not in out


# ── the guard ────────────────────────────────────────────────────────────────


def test_malformed_known_directive_fails_loudly():
    with pytest.raises(cd.DirectiveError) as exc:
        cd.expand("{{include:}}", host="claude", strict=True)
    assert "malformed" in str(exc.value)


def test_jsx_braces_in_code_samples_are_not_directives():
    """A general `{{ word: }}` guard fails the build on a tailwind snippet.

    tech-packs/tailwind.md contains `<div dangerouslySetInnerHTML={{ __html:
    content }} />`, which is JSX, not a directive.
    """
    sample = "<div dangerouslySetInnerHTML={{ __html: content }} />"
    assert cd.expand(sample, host="cursor", strict=True) == sample


def test_unknown_host_is_refused():
    with pytest.raises(cd.DirectiveError):
        cd.expand("hello", host="emacs", strict=True)


# ── migration guards ─────────────────────────────────────────────────────────


def test_no_legacy_bang_cat_survives_in_authored_bodies():
    """Both spellings, including the table-cell one the old regex missed."""
    offenders = []
    for area in ("agents", "skills"):
        for md in (REPO / "plugin-claude" / area).rglob("*.md"):
            legacy = cd.find_legacy(md.read_text(encoding="utf-8", errors="ignore"))
            if legacy:
                offenders.append("%s: %s" % (md.relative_to(REPO), legacy[:2]))
    assert not offenders, (
        "authored bodies still carry legacy !cat; they would ship untranslated "
        "to any host that cannot expand it:\n  " + "\n  ".join(offenders)
    )


def test_find_legacy_catches_both_spellings():
    assert cd.find_legacy("!`cat a.md`") == ["a.md"]
    assert cd.find_legacy("| Phase | `!cat b.md` |") == ["b.md"]


def test_every_authored_directive_expands_for_every_host():
    """Whole-corpus check: no authored body can contain a directive that any
    host fails to expand."""
    plugin = REPO / "plugin-claude"
    for host in ("claude", "cursor", "codex"):
        for area in ("agents", "skills"):
            for md in (plugin / area).rglob("*.md"):
                own_dir = cd.own_dir_for(md.relative_to(plugin).as_posix())
                cd.expand(
                    md.read_text(encoding="utf-8", errors="ignore"),
                    host=host,
                    own_dir=own_dir,
                    source_dir=plugin / own_dir,
                    strict=True,
                )


def test_no_authored_body_points_at_the_unpackaged_protocol_path():
    """Protocols are CP-delivered, never packaged (ADR-016).

    Verified against the built packages: `skills/_shared/protocols/` contains
    zero files in the Claude, Cursor and Codex trees. A body that tells the
    model to read that path names a file that cannot exist once installed --
    and Cursor got the same broken path, since the token swap only turns
    `${CLAUDE_PLUGIN_ROOT}` into `${PLUGIN_ROOT}`.

    The runtime location is `.synaptory/.protocols/`.
    """
    offenders = []
    for area in ("agents", "skills"):
        for path in (REPO / "plugin-claude" / area).rglob("*"):
            if not path.is_file() or path.suffix not in (".md", ".tmpl"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "skills/_shared/protocols/" in text:
                offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        "authored bodies reference the unpackaged protocol location; use "
        ".synaptory/.protocols/<name>.md instead:\n  " + "\n  ".join(sorted(offenders))
    )


def test_protocol_paths_normalize_on_every_host():
    """The remapping is a rule every host needs, so it lives in the expander."""
    for host in ("claude", "cursor", "codex"):
        out = cd.expand(
            "{{read: ${CLAUDE_PLUGIN_ROOT}/skills/_shared/protocols/iron-laws.md}}",
            host=host,
            strict=True,
        )
        assert ".synaptory/.protocols/iron-laws.md" in out, host
        assert "skills/_shared/protocols" not in out, host


# ── path: a plugin path, named but not inlined ───────────────────────────────


def test_path_maps_the_installed_layout_not_just_the_token():
    """The bug the token swap could not see (#298).

    `${CLAUDE_PLUGIN_ROOT}` -> `${PLUGIN_ROOT}` gets the token right and the
    LAYOUT wrong: Cursor installs authored `agents/<role>/` as `roles/<role>/`,
    and its `agents/` holds flat dispatch stubs instead. 15 composed paths named
    a directory the Cursor package does not have, and nothing failed.
    """
    src = "Follow `{{path: agents/software-engineer/SKILL.md}}`."
    assert (
        cd.expand(src, host="cursor")
        == "Follow `${PLUGIN_ROOT}/roles/software-engineer/SKILL.md`."
    )
    assert (
        cd.expand(src, host="claude")
        == "Follow `${CLAUDE_PLUGIN_ROOT}/agents/software-engineer/SKILL.md`."
    )


def test_path_leaves_non_agent_paths_where_they_are():
    src = "python3 {{path: skills/_shared/scripts/tracker/tracker_cli.py}} get-backlog"
    for host, root in (("claude", "${CLAUDE_PLUGIN_ROOT}"), ("cursor", "${PLUGIN_ROOT}")):
        assert cd.expand(src, host=host) == (
            "python3 %s/skills/_shared/scripts/tracker/tracker_cli.py get-backlog" % root
        )


def test_path_is_not_validated_against_disk(tmp_path):
    """A `{{path:}}` names a RUNTIME location, which the composer cannot see.

    Per-host layout plus ADR-016 stripping means the correct path is routinely
    absent from the tree the composer is walking, so an existence check here
    fails on correct input. `{{include:}}` is checked because it inlines bytes.
    """
    out = cd.expand(
        "{{path: skills/_shared/design-assets/typography.md}}",
        host="cursor",
        source_dir=tmp_path,
    )
    assert out == "${PLUGIN_ROOT}/skills/_shared/design-assets/typography.md"


def test_path_keeps_an_authored_placeholder_segment():
    """`{mode}` is filled in by the agent at runtime, not by the composer.

    Its closing brace must not terminate the directive capture -- if it does,
    the directive never matches and the strict guard fails the build.
    """
    out = cd.expand(
        'Read("{{path: agents/software-engineer/modes/{mode}.md}}")', host="cursor"
    )
    assert out == 'Read("${PLUGIN_ROOT}/roles/software-engineer/modes/{mode}.md")'


def test_path_keeps_a_shell_variable_segment():
    out = cd.expand(
        "{{path: skills/_shared/backends/${SE_BACKEND}.md}}", host="claude"
    )
    assert out == "${CLAUDE_PLUGIN_ROOT}/skills/_shared/backends/${SE_BACKEND}.md"


def test_path_cannot_escape_the_plugin_tree():
    for bad in ("{{path: /etc/passwd}}", "{{path: ../../secrets.md}}"):
        with pytest.raises(cd.DirectiveError):
            cd.expand(bad, host="cursor")


def test_path_rejects_a_captured_shell_escape():
    """A backslash means the directive swallowed a `\\"` from its own line.

    Three authored sites hit exactly this. The Cursor layout used to normalise
    the backslash to `/`, which turned `build_summary.py\\` into
    `build_summary.py/` and shipped it -- a silent mangling of a command an
    agent is told to run.
    """
    with pytest.raises(cd.DirectiveError) as excinfo:
        cd.expand("{{path: skills/_shared/scripts/build_summary.py\\}}", host="cursor")
    assert "backslash" in str(excinfo.value)


def test_path_normalizes_a_protocol_path_on_every_host():
    """Protocols are CP-delivered, so their runtime location is host-agnostic."""
    for host in cd.HOSTS:
        assert (
            cd.expand("{{path: skills/_shared/protocols/iron-laws.md}}", host=host)
            == ".synaptory/.protocols/iron-laws.md"
        )


# ── self: this agent's own directory ─────────────────────────────────────────


def test_self_is_role_scoped():
    src = "load `{{self: modes/modernize.md}}`"
    assert (
        cd.expand(src, host="cursor", role="solution-architect")
        == "load `${PLUGIN_ROOT}/roles/solution-architect/modes/modernize.md`"
    )
    assert (
        cd.expand(src, host="claude", role="solution-architect")
        == "load `${CLAUDE_SKILL_DIR}/modes/modernize.md`"
    )


def test_self_cannot_escape_the_agent_directory():
    """`${CLAUDE_SKILL_DIR}/../_shared/templates/` was authored six times.

    With no role it composed to `${PLUGIN_ROOT}/roles/../_shared/templates/` --
    a path that resolves nowhere near the templates. A `..` in a `{{self:}}`
    means the author wanted a plugin path, so the directive refuses it and the
    build says so.
    """
    with pytest.raises(cd.DirectiveError):
        cd.expand("{{self: ../_shared/templates/prd.tmpl.md}}", host="cursor")


def test_malformed_path_and_self_directives_fail_loudly():
    for bad in ("{{path: }}", "{{self: }}", "{{path oops}}\n{{path:"):
        with pytest.raises(cd.DirectiveError):
            cd.expand(bad, host="cursor")


def test_has_directives_covers_every_known_directive():
    """The control-plane seeder gates on this before importing the expander.

    A hard-coded marker list there went stale the moment a directive was added,
    and a stale list serves the directive to a user verbatim.
    """
    for name in cd.KNOWN_DIRECTIVES:
        assert cd.has_directives("before {{%s: x.md}} after" % name), name
    assert not cd.has_directives("plain prose with {{ __html: content }}")


def test_has_directives_agrees_with_the_expander_on_whitespace():
    """The gate must not disagree with the regex that does the work.

    A gate matching only the literal `{{path:` would skip a file whose
    directive is spaced out, and ship the directive verbatim.
    """
    src = "{{  path:   agents/code-reviewer/SKILL.md  }}"
    assert cd.has_directives(src)
    assert cd.expand(src, host="cursor") == "${PLUGIN_ROOT}/roles/code-reviewer/SKILL.md"


def test_a_near_miss_spelling_fails_rather_than_shipping():
    """`{{ path : x }}` -- space before the colon -- is not a directive.

    The malformed guard is deliberately LOOSER than the expanding regexes so
    that a near miss stops the build. Silently shipping the line is the failure
    mode this whole mechanism exists to remove.
    """
    with pytest.raises(cd.DirectiveError):
        cd.expand("{{ path : agents/code-reviewer/SKILL.md }}", host="cursor")


def test_has_directives_sees_a_malformed_directive():
    """A malformed directive must reach the expander, which then fails loudly.

    A gate that skipped it would let the broken line through untouched.
    """
    assert cd.has_directives("{{path: unterminated")


# ── tool names: a declared map, deliberately not a directive ─────────────────


def test_every_host_declares_every_tool():
    """A host missing an entry would silently ship the authored (Claude) name."""
    for host, rules in cd.HOSTS.items():
        missing = [n for n in cd.TOOL_VOCABULARY if n not in rules.tools]
        assert not missing, "%s declares no mapping for %s" % (host, missing)


def test_tool_names_are_translated_for_cursor():
    src = 'Dispatch with Agent(prompt="build it") then Agent(). Ask via AskUserQuestion.'
    out = cd.apply_tool_names(src, "cursor")
    assert "Agent(" not in out
    assert "Task(prompt=" in out and "Task()" in out
    assert "AskQuestion" in out and "AskUserQuestion" not in out


def test_tool_names_do_not_reach_inside_an_identifier():
    """A substring replace would corrupt `SubAgent(` and `MyAgentClass`."""
    src = "SubAgent(x) and MyAgent(y) stay put; Agent(z) does not."
    out = cd.apply_tool_names(src, "cursor")
    assert "SubAgent(x)" in out and "MyAgent(y)" in out
    assert "Task(z)" in out


def test_claude_is_the_authored_dialect_so_its_map_is_identity():
    src = 'Agent(prompt="x") and AskUserQuestion'
    assert cd.apply_tool_names(src, "claude") == src


# ── the acceptance guard: authored bodies carry no host path token ───────────


def test_no_host_path_token_survives_in_authored_bodies():
    """#298 acceptance: authored markdown spells no `${CLAUDE_*}` path.

    A body that does gets whatever its host composer's string replacement
    happens to do with it -- the right token and the wrong layout, on Cursor.

    No directory allowlist any more: the guard flags the token in PATH position,
    so a per-backend document mentioning its own host's variable in prose is
    correctly not an offender, rather than exempted by path.
    """
    offenders = []
    for area in ("agents", "skills"):
        for md in sorted((REPO / "plugin-claude" / area).rglob("*.md")):
            tokens = cd.find_legacy_tokens(md.read_text(encoding="utf-8"))
            if tokens:
                offenders.append(
                    "%s: %s"
                    % (
                        md.relative_to(REPO / "plugin-claude").as_posix(),
                        sorted(set(tokens)),
                    )
                )
    assert not offenders, (
        "authored bodies must use {{path:}} / {{self:}} instead of a host "
        "token:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_catches_every_token_spelling():
    """The hole the first version of this guard shipped with.

    It matched `\\$\\{?TOKEN\\}?`, so `${{CLAUDE_PLUGIN_ROOT}}` -- the
    doubled-brace f-string escape -- slipped past in five files. Exactly the
    `_BANG_CAT`-missed-a-spelling failure, inside the code written to replace it.
    """
    for spelling in (
        'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/x.py"',
        'builtin = "${{CLAUDE_PLUGIN_ROOT}}/skills/_shared/templates/tracker"',
        "python3 $CLAUDE_PLUGIN_ROOT/hooks/lib/x.py",
        "load `${CLAUDE_SKILL_DIR}/modes/x.md`",
    ):
        assert cd.find_legacy_tokens(spelling), "not caught: %s" % spelling


def test_the_guard_ignores_an_environment_variable_reference():
    """A token with no path after it is a variable read, not a path.

    `{{path:}}` cannot express `os.environ.get("CLAUDE_PLUGIN_ROOT")`, and prose
    telling a Claude user to check that the variable is set is correct as
    written. Flagging these would force a directive where none fits.
    """
    for benign in (
        'plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")',
        "Check `${CLAUDE_PLUGIN_ROOT}` is set correctly; verify the plugin loaded",
    ):
        assert not cd.find_legacy_tokens(benign), benign


def test_the_guard_treats_a_doubled_brace_as_a_token_outside_an_fstring():
    """The f-string carve-out is structural, not a file allowlist.

    Inside an f-string the doubling is what makes `${TOKEN}` survive rendering,
    and a compose-time directive cannot emit that escape. On a line that opens no
    f-string the same spelling is an ordinary token and must still fail.
    """
    assert not cd.find_legacy_tokens(
        """TRACKER = f'python3 "${{CLAUDE_PLUGIN_ROOT}}/x.py"'"""
    )
    assert cd.find_legacy_tokens(
        """TRACKER = 'python3 "${{CLAUDE_PLUGIN_ROOT}}/x.py"'"""
    )


def test_project_dir_is_not_a_host_token_this_guard_owns():
    """A finding, not an omission -- see the guard's comment.

    All three hosts consume `CLAUDE_PROJECT_DIR` under the same name by design,
    so a `{{project}}` directive would expand identically on every host and buy
    nothing. Pinned so a future reader does not "fix" it by adding the token.
    """
    assert not cd.find_legacy_tokens('build_summary.py "${CLAUDE_PROJECT_DIR}"')
    assert "CLAUDE_PROJECT_DIR" not in cd.LEGACY_TOKEN_RE.pattern


# ── own_dir_for: every caller must answer this identically ───────────────────


def test_own_dir_for_resolves_an_agent_subtree_to_the_agent_root():
    """A phase body includes its siblings as `phases/...`, not `../phases/...`."""
    assert (
        cd.own_dir_for("agents/software-engineer/modes/frontend.md")
        == "agents/software-engineer"
    )
    assert cd.own_dir_for("agents/software-engineer/SKILL.md") == "agents/software-engineer"


def test_own_dir_for_resolves_a_skill_to_its_own_directory():
    assert cd.own_dir_for("skills/synaptory/SKILL.md") == "skills/synaptory"
    assert cd.own_dir_for("skills/synaptory/modes/spq.md") == "skills/synaptory/modes"


def test_include_in_a_skill_body_resolves_without_a_role():
    """The orchestrator inlines its own routing-rules.json.

    With `role` as the only handle, a skill body composed to `${PLUGIN_ROOT}/
    roles//routing-rules.json` -- the dropped-segment bug wearing a new hat.
    """
    src = "{{include: routing-rules.json}}"
    assert (
        cd.expand(src, host="cursor", own_dir="skills/synaptory")
        == "Read(`${PLUGIN_ROOT}/skills/synaptory/routing-rules.json`)"
    )
    assert cd.expand(src, host="claude", own_dir="skills/synaptory") == (
        "!`cat ${CLAUDE_SKILL_DIR}/routing-rules.json`"
    )


def test_include_without_an_authoring_directory_refuses_to_compose():
    """A hole in the path must stop the build, not ship."""
    with pytest.raises(cd.DirectiveError) as excinfo:
        cd.expand("{{include: routing-rules.json}}", host="cursor")
    assert "authoring directory" in str(excinfo.value)


# ── cp: bodies ADR-016 withholds and the control plane serves ────────────────


def _plugin_and_own(relative: str):
    plugin = REPO / "plugin-claude"
    own = cd.own_dir_for(relative)
    return plugin, own


def test_cp_expands_to_each_hosts_native_fetch():
    """Claude's shell tool is `Bash`, Cursor's is `Shell`, Codex has neither.

    A `{{path:}}` here would name a file the package does not contain, which is
    how 25 authored references came to resolve to nothing on every host.
    """
    plugin, own = _plugin_and_own("agents/software-engineer/modes/frontend.md")
    src = "{{cp: design-assets/typography}}"
    expected = {
        "claude": 'Bash("synaptory skills get design-assets/typography")',
        "cursor": 'Shell("synaptory skills get design-assets/typography")',
        "codex": "Run `synaptory skills get design-assets/typography`",
    }
    for host, want in expected.items():
        got = cd.expand(src, host=host, own_dir=own, source_dir=plugin / own)
        assert got == want, "%s: %s" % (host, got)


def test_cp_fails_on_a_name_the_control_plane_cannot_serve():
    """A typo here is a 404 for a user, so it is a compose-time error.

    Unlike `{{path:}}`, this IS checkable: the authored tree is exactly what the
    seeder walks, so the composer can see whether the body exists.
    """
    plugin, own = _plugin_and_own("agents/software-engineer/modes/frontend.md")
    with pytest.raises(cd.DirectiveError) as excinfo:
        cd.expand(
            "{{cp: design-assets/no-such-body}}",
            host="claude",
            own_dir=own,
            source_dir=plugin / own,
        )
    assert "404" in str(excinfo.value)


def test_cp_refuses_a_directory_that_ships_inside_the_package():
    """`skills/_shared/scripts/` ships; a `{{cp:}}` for it would be a lie."""
    plugin, own = _plugin_and_own("agents/software-engineer/modes/frontend.md")
    with pytest.raises(cd.DirectiveError) as excinfo:
        cd.expand(
            "{{cp: scripts/tracker/tracker_cli.py}}",
            host="claude",
            own_dir=own,
            source_dir=plugin / own,
        )
    assert "control-plane delivered" in str(excinfo.value)


def test_every_cp_name_in_authored_bodies_resolves():
    """Whole-corpus check, the one the missing 25 needed and did not have."""
    plugin = REPO / "plugin-claude"
    for area in ("agents", "skills"):
        for md in sorted((plugin / area).rglob("*.md")):
            own = cd.own_dir_for(md.relative_to(plugin).as_posix())
            cd.expand(
                md.read_text(encoding="utf-8", errors="ignore"),
                host="claude",
                own_dir=own,
                source_dir=plugin / own,
                strict=True,
            )


def test_no_authored_body_points_into_a_withheld_directory_with_path():
    """`{{path:}}` names something the package contains; these it does not.

    This is the defect class directly: a reference that composes to a perfectly
    well-formed path inside a directory ADR-016 strips from every package. The
    path was right, the file was never there, and no build failed.
    """
    plugin = REPO / "plugin-claude"
    offenders = []
    for area in ("agents", "skills"):
        for md in sorted((plugin / area).rglob("*.md")):
            text = md.read_text(encoding="utf-8", errors="ignore")
            for match in cd.PATH_RE.finditer(text):
                target = match.group("path").strip()
                if not target.startswith("skills/_shared/"):
                    continue
                head = target.split("/")[2] if target.count("/") >= 2 else ""
                if head in cd.CP_DELIVERED_SHARED:
                    offenders.append(
                        "%s: {{path: %s}} -- use {{cp:}}"
                        % (md.relative_to(plugin).as_posix(), target)
                    )
    assert not offenders, (
        "authored bodies reference a directory ADR-016 withholds from every "
        "package:\n  " + "\n  ".join(offenders)
    )


def test_the_withheld_set_matches_the_seeder():
    """Withheld and served must be ONE list.

    They were two, and the halves disagreed: templates and design-assets were
    stripped from every package and absent from the seeder's walk, so their
    bodies existed nowhere a host could reach.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_seed_probe", REPO / "api" / "synaptory_api" / "seed.py"
    )
    # seed.py pulls in the API package; read the tuple literal instead of
    # importing, so this Layer 1 test keeps its no-dependency contract.
    source = (REPO / "api" / "synaptory_api" / "seed.py").read_text(encoding="utf-8")
    import ast

    tree = ast.parse(source)
    seeder = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "_CP_DELIVERED_SHARED" for t in node.targets
        ):
            seeder = tuple(ast.literal_eval(node.value))
    assert seeder is not None, "seed.py no longer declares _CP_DELIVERED_SHARED"
    assert seeder == cd.CP_DELIVERED_SHARED, (
        "seeder withholds %s but the directive module says %s"
        % (seeder, cd.CP_DELIVERED_SHARED)
    )
