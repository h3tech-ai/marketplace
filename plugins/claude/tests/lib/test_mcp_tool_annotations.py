"""Layer 1 — MCP tool annotations (#333, from #323 G5).

`tools/list` emitted no annotations. A client told nothing about a tool must
assume the worst, so a NON-INTERACTIVE client has to seek approval for every
tool and, with nobody to ask, cancels. That is exactly what was observed:
`codex exec` returned `user cancelled MCP tool call` for every synaptory tool
including pure reads, while the same server answered correctly when driven
directly over NDJSON.

Verified against codex-cli 0.147.0, same command and sandbox flag, annotations
the only difference:

    without   mcp: synaptory/get_status (failed)     user cancelled MCP tool call
    with      mcp: synaptory/get_status (completed)  isError: false

So the `--dangerously-bypass-approvals-and-sandbox` requirement was never a
Codex limitation. It was compensating for a server that declared nothing about
itself.
"""

from __future__ import annotations

import pytest

import mcp_transport as t


pytestmark = pytest.mark.unit


def _spec(description="d", schema=None, **extra):
    return {"fn": None, "description": description, "schema": schema or {}, **extra}


def test_every_tool_carries_annotations():
    """The whole defect was their absence, so this is the load-bearing check."""
    listed = t.tools_list({"next_action": _spec(), "spq_lifecycle": _spec()})
    for tool in listed["tools"]:
        assert "annotations" in tool, tool["name"]
        for key in ("readOnlyHint", "destructiveHint", "idempotentHint",
                    "openWorldHint"):
            assert key in tool["annotations"], (tool["name"], key)


def test_reads_are_marked_read_only():
    listed = t.tools_list({name: _spec() for name in
                           ("doctor", "get_status", "get_state", "next_action")})
    for tool in listed["tools"]:
        assert tool["annotations"]["readOnlyHint"] is True, tool["name"]
        assert tool["annotations"]["idempotentHint"] is True, tool["name"]


def test_mutations_are_not_marked_read_only():
    """Understating a mutation to buy smoother approvals is how a gate stops
    being a gate. The advance boundary is the thing being protected."""
    listed = t.tools_list({name: _spec() for name in
                           ("advance", "spq_lifecycle", "bootstrap_project",
                            "begin_lifecycle_dispatch")})
    for tool in listed["tools"]:
        assert tool["annotations"]["readOnlyHint"] is False, tool["name"]


def test_mutations_do_not_claim_idempotency():
    """The advance boundary records receipt digests to prevent replay.

    Telling a client a mutation is safe to retry would invite exactly the
    double-advance that guard exists to stop.
    """
    listed = t.tools_list({"advance": _spec(), "spq_lifecycle": _spec()})
    for tool in listed["tools"]:
        assert tool["annotations"]["idempotentHint"] is False, tool["name"]


def test_nothing_is_marked_destructive():
    """A guarded forward transition adds state and evidence.

    `destructiveHint` is for tools that can undo or discard committed work; a
    lifecycle advance is a mutation, not a destruction, and marking it
    destructive would push clients to demand approval for ordinary progress.
    """
    listed = t.tools_list({name: _spec() for name in
                           ("advance", "next_action", "spq_lifecycle")})
    for tool in listed["tools"]:
        assert tool["annotations"]["destructiveHint"] is False, tool["name"]
        assert tool["annotations"]["openWorldHint"] is False, tool["name"]


def test_an_explicit_annotation_on_the_spec_wins():
    """A host must be able to correct one tool without editing the shared list."""
    listed = t.tools_list({
        "odd_one": _spec(annotations={"readOnlyHint": True, "title": "custom"}),
    })
    assert listed["tools"][0]["annotations"] == {
        "readOnlyHint": True, "title": "custom"}


def test_the_read_only_set_is_explicit_not_pattern_matched():
    """`get_*` / `*_status` is a convention, and a convention mislabels the
    first tool that breaks it -- in the direction of claiming read-only for
    something that writes."""
    assert "spq_status" in t._READ_ONLY_TOOLS
    # A mutation whose NAME looks like a read must still be a mutation.
    listed = t.tools_list({"get_or_create_thing": _spec()})
    assert listed["tools"][0]["annotations"]["readOnlyHint"] is False


def test_schema_and_description_still_pass_through():
    """Annotations are additive; nothing a client already used may change."""
    listed = t.tools_list({"next_action": _spec("the desc", {"type": "object"})})
    tool = listed["tools"][0]
    assert tool["description"] == "the desc"
    assert tool["inputSchema"] == {"type": "object"}
