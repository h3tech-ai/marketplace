"""Layer 1 — receipt-template drift guard (#163, WP of the #134 program).

Hypothesis: every receipt example the plugin ships in agent SKILL.md /
phase / protocol files must satisfy the Evidence Contract it teaches
against — otherwise agents copy templates that the DoD gate scores as
None (plain strings) or that the SubagentStop replay hook rejects
(pipes, env prefixes, non-allowlisted programs). This is the permanent
fix for the field failure where templates taught
``"npm run build 2>&1 | tail -5"`` and a CR receipt with no ``status``.

Rules enforced (derived from evidence_contract.load_contract() so they
cannot drift from the shipped contract; the replay runner sources the
same contract, so agreement is transitive):

  1. Every ``verification_commands`` entry in every fenced ```json block
     is EITHER an executed object (dict with ``command`` + ``exit_code``)
     OR a plain string that passes the replayability rules.
  2. Executed-object command strings must ALSO pass the replayability
     rules (verification_runner validates dict entries too).
  3. Replayability = shlex-parseable (posix), argv[0] (or its basename)
     in the contract allowlist, no ``=`` in argv[0] (env prefix), no
     bare shell-control tokens, no ``$(``/backtick substitution in any
     token, no denied interpreter flags (bash/sh -c/-s/-i) and no
     absolute interpreter script paths, no denied find flags
     (-exec/-delete/...).
  4. The CR SKILL.md receipt example carries top-level
     ``"status": "complete"`` AND ``story_dod.code_reviewed: true``.
  5. Every ``token_usage.stage`` literal in extracted examples is in the
     contract's stage enum.
  6. The literal decoration ``2>&1 | tail`` (raw or markdown-escaped
     ``2>&1 \\| tail``) appears NOWHERE in the SE/QE/CR/PE agent files.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

import pytest

from hooks.lib import evidence_contract as ec

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

# Template files that ship receipt / verification_commands examples.
# Paths are relative to the plugin root.
TEMPLATE_FILES = [
    "agents/software-engineer/SKILL.md",
    "agents/quality-engineer/phases/receipt-protocol.md",
    "agents/quality-engineer/phases/08-test-data.md",
    "agents/quality-engineer/modes/exploratory.md",
    "agents/quality-engineer/modes/testability-review.md",
    "agents/code-reviewer/SKILL.md",
    "agents/code-reviewer/phases/receipt-protocol.md",
    "agents/platform-engineer/SKILL.md",
    # #170 review: these also ship receipt verification_commands examples and
    # previously taught inline `python3 -c` payloads — pin them too.
    "agents/compliance-engineer/SKILL.md",
    "agents/technical-writer/modes/report.md",
    "agents/project-owner/SKILL.md",
    "skills/_shared/protocols/receipt-protocol.md",
    "hooks/data/compacted-protocols.md",
    # SPQ lifecycle STATE files. Each ships receipt examples with
    # verification_commands, so they are subject to the same contract as the
    # agent templates. Added with the lifecycle itself, because the list is
    # hardcoded and a new prompt file is otherwise unguarded by default.
    # `modes/spq.md` is deliberately absent: it is a dispatcher and carries no
    # receipt examples, and this test requires every listed file to have some.
    "skills/synaptory/spq/discovery.md",
    "skills/synaptory/spq/commit.md",
    "skills/synaptory/spq/sync.md",
    "skills/synaptory/spq/checkpoint.md",
    "skills/synaptory/spq/acceptance.md",
]

CR_SKILL = "agents/code-reviewer/SKILL.md"

# Files whose raw text must never contain piped-tail output decoration.
NO_PIPED_TAIL_FILES = [
    p for p in TEMPLATE_FILES if p.startswith("agents/")
] + ["skills/_shared/protocols/receipt-protocol.md", "hooks/data/compacted-protocols.md"]

FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

RECEIPT_KEYS = {"verification_commands", "story_dod", "status"}


# ─── Extraction helpers ───────────────────────────────────────────────────────


def _read(relpath: str) -> str:
    path = PLUGIN_ROOT / relpath
    assert path.is_file(), f"template file missing: {path}"
    return path.read_text(encoding="utf-8")


def _extract_json_blocks(text: str) -> list:
    """Parse every fenced ```json block; fragments like

        "verification_commands": [ ... ]

    are retried wrapped in braces. Blocks that still fail to parse are
    skipped (the raw-text scans below are the backstop for those).
    """
    parsed = []
    for match in FENCE_RE.finditer(text):
        raw = match.group(1).strip()
        for candidate in (raw, "{" + raw + "}"):
            try:
                parsed.append(json.loads(candidate))
                break
            except json.JSONDecodeError:
                continue
    return parsed


def _walk(obj):
    """Yield every dict nested anywhere inside obj."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _receipt_shaped(obj) -> list[dict]:
    return [d for d in _walk(obj) if RECEIPT_KEYS & set(d.keys())]


# ─── Replayability validation (implemented from the contract data) ──────────


def _replay_rules() -> dict:
    contract = ec.load_contract(str(PLUGIN_ROOT))
    return contract["verification_commands"]["replayability"]


def _stage_enum() -> set[str]:
    contract = ec.load_contract(str(PLUGIN_ROOT))
    return set(contract["stages"]["valid"])


def replay_violations(command: str, replay: dict) -> list[str]:
    """Return every replayability-rule violation for a command string."""
    problems: list[str] = []
    if not isinstance(command, str) or not command.strip():
        return ["empty or non-string command"]
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return [f"unparseable: {exc}"]
    if not argv:
        return ["empty argv after parse"]

    allow = set(replay["allowlist"])
    forbidden = set(replay["forbidden_tokens"])
    find_deny = set(replay["find_flag_denylist"])
    interp_deny = replay["interpreter_flag_denylist"]

    prog = argv[0]
    if "=" in prog:
        # env-var prefix ("FOO=1 cmd") — only works under a shell.
        return [f"env-var prefix in argv[0]: {prog!r}"]
    base = os.path.basename(prog)
    if prog not in allow and base not in allow:
        problems.append(f"program {prog!r} not in allowlist")

    for token in argv:
        if token in forbidden:
            problems.append(f"bare shell-control token {token!r}")
        # The runner inspects every token, including quoted ones.
        if "$(" in token or "`" in token:
            problems.append(f"command substitution in token {token!r}")

    if base in interp_deny:
        # Positional parse mirroring verification_runner._validate_interpreter
        # (#170 re-review): leading option flags, then the FIRST positional
        # token is the script. `-m` (module) is honored only before any
        # positional script; inline/loader flags (incl. `--flag=value` and
        # node `-pe` clusters) and absolute script paths are rejected.
        denied_flags = set(interp_deny[base])
        module_ok = base in {"python", "python3"}
        for token in argv[1:]:
            flag = token.split("=", 1)[0]
            if flag in denied_flags:
                problems.append(f"inline/loader interpreter flag {base} {token}")
                break
            if (
                base == "node"
                and token.startswith("-")
                and not token.startswith("--")
                and len(token) > 1
                and set(token[1:]) & {"e", "p"}
            ):
                problems.append(f"inline eval/print flag {base} {token}")
                break
            if token == "-":
                problems.append(f"{base} reads script from stdin")
                break
            if token == "-m" and module_ok:
                break  # module mode reached before any positional script
            if not token.startswith("-"):
                if os.path.isabs(token):
                    problems.append(
                        f"interpreter script must be repo-relative: {token!r}"
                    )
                break

    if base == "find":
        for token in argv[1:]:
            if token in find_deny:
                problems.append(f"denied find flag {token!r}")

    return problems


def _iter_verification_entries(relpath: str):
    """Yield (receipt_dict, entry) for every verification_commands entry."""
    for block in _extract_json_blocks(_read(relpath)):
        for receipt in _receipt_shaped(block):
            commands = receipt.get("verification_commands")
            if not isinstance(commands, list):
                continue
            for entry in commands:
                yield receipt, entry


# ─── 1+2+3. verification_commands entries are contract-clean ─────────────────


@pytest.mark.unit
@pytest.mark.parametrize("relpath", TEMPLATE_FILES)
def test_verification_command_examples_are_contract_clean(relpath):
    replay = _replay_rules()
    found = 0
    for _receipt, entry in _iter_verification_entries(relpath):
        found += 1
        if isinstance(entry, dict):
            assert "command" in entry and "exit_code" in entry, (
                f"{relpath}: executed-object entry must carry command + "
                f"exit_code, got {entry!r}"
            )
            violations = replay_violations(entry["command"], replay)
            assert not violations, (
                f"{relpath}: executed-object command {entry['command']!r} "
                f"violates replayability rules: {violations}"
            )
        elif isinstance(entry, str):
            violations = replay_violations(entry, replay)
            assert not violations, (
                f"{relpath}: plain-string command {entry!r} violates "
                f"replayability rules: {violations}"
            )
        else:
            pytest.fail(
                f"{relpath}: verification_commands entry must be an executed "
                f"object or a plain string, got {type(entry).__name__}: {entry!r}"
            )
    # Guard the guard: files listed here are expected to carry examples,
    # except pure-checklist files with no JSON receipt blocks.
    if relpath not in (
        "agents/code-reviewer/phases/receipt-protocol.md",
        "hooks/data/compacted-protocols.md",
    ):
        assert found > 0, f"{relpath}: expected verification_commands examples"


# ─── 4. CR receipt example teaches the status contract ───────────────────────


@pytest.mark.unit
def test_cr_receipt_example_has_status_complete():
    text = _read(CR_SKILL)
    receipts = [
        r
        for block in _extract_json_blocks(text)
        for r in _receipt_shaped(block)
        if r.get("role") == "code-reviewer"
    ]
    assert receipts, f"{CR_SKILL}: expected a code-reviewer receipt example"
    complete = [r for r in receipts if r.get("status") == "complete"]
    assert complete, (
        f"{CR_SKILL}: the CR receipt template must carry top-level "
        '"status": "complete" — the code_reviewed DoD gate reads it'
    )
    assert any(
        (r.get("story_dod") or {}).get("code_reviewed") is True for r in complete
    ), f"{CR_SKILL}: the CR receipt template must keep story_dod.code_reviewed: true"


# ─── 5. token_usage.stage literals match the contract enum ───────────────────


@pytest.mark.unit
@pytest.mark.parametrize("relpath", TEMPLATE_FILES)
def test_stage_literals_are_in_contract_enum(relpath):
    stages = _stage_enum()
    for block in _extract_json_blocks(_read(relpath)):
        for d in _walk(block):
            token_usage = d.get("token_usage")
            if not isinstance(token_usage, dict):
                continue
            stage = token_usage.get("stage")
            if isinstance(stage, str) and not stage.startswith("{"):
                assert stage in stages, (
                    f"{relpath}: token_usage.stage {stage!r} is not a valid "
                    f"contract stage ({sorted(stages)})"
                )


# ─── 6. No piped-tail output decoration anywhere in the agent files ──────────


@pytest.mark.unit
@pytest.mark.parametrize("relpath", NO_PIPED_TAIL_FILES)
def test_no_piped_tail_decoration(relpath):
    text = _read(relpath)
    for pattern in ("2>&1 | tail", "2>&1 \\| tail"):
        assert pattern not in text, (
            f"{relpath}: contains the forbidden output decoration "
            f"{pattern!r} — the replay hook captures/truncates output "
            "itself and rejects shell composition"
        )
