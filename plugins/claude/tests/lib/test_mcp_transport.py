"""Layer 1 - the shared MCP stdio transport (#299).

Both host servers previously carried their own copy of this: framing, dispatch,
the tool registry, the -32601 reply. ~120 duplicated lines, so every fix had to
land twice.

These tests cover the PRODUCTION framing path for both hosts. Before the
extraction that path was untested on Cursor: every Cursor MCP test ran with
SYNAPTORY_MCP_NDJSON=1, so the Content-Length framing it actually ships with was
exercised by nothing.
"""

from __future__ import annotations

import io
import json

import mcp_transport as mt
import pytest


TOOLS = {
    "echo": {
        "fn": lambda args: {"echoed": args.get("value")},
        "description": "Echo a value back.",
        "schema": {"type": "object", "properties": {"value": {"type": "string"}}},
    },
    "boom": {
        "fn": lambda args: (_ for _ in ()).throw(RuntimeError("tool exploded")),
        "description": "Always raises.",
        "schema": {"type": "object"},
    },
}


@pytest.fixture(autouse=True)
def _no_framing_override(monkeypatch):
    monkeypatch.delenv("SYNAPTORY_MCP_NDJSON", raising=False)
    monkeypatch.delenv("SYNAPTORY_MCP_FRAMED", raising=False)


# ── framing round-trip ───────────────────────────────────────────────────────


def test_framed_round_trip():
    """The Cursor production path: Content-Length framing."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    out = io.BytesIO()
    mt.write_framed(payload, stream=out)

    raw = out.getvalue()
    assert raw.startswith(b"Content-Length: ")
    assert b"\r\n\r\n" in raw

    assert mt.read_framed(io.BytesIO(raw)) == payload


def test_framed_read_returns_none_at_eof():
    assert mt.read_framed(io.BytesIO(b"")) is None


def test_framed_read_returns_none_on_zero_length():
    assert mt.read_framed(io.BytesIO(b"Content-Length: 0\r\n\r\n")) is None


def test_framed_read_tolerates_a_malformed_header_line():
    body = json.dumps({"ok": True}).encode()
    raw = b"garbage-without-a-colon\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    assert mt.read_framed(io.BytesIO(raw)) == {"ok": True}


# ── dispatch ─────────────────────────────────────────────────────────────────


def test_initialize_negotiates_to_the_supported_protocol():
    """Never echo a client version we do not speak. Codex may send 2025-06-18;
    we still answer with PROTOCOL_VERSION."""
    response = mt.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        TOOLS,
        server_version="9.9.9",
    )
    assert response["result"]["protocolVersion"] == mt.PROTOCOL_VERSION
    assert response["result"]["serverInfo"]["version"] == "9.9.9"


def test_initialize_negotiates_an_unknown_protocol_version():
    """Was `..._refuses_...`, and the refusal was the defect.

    The rule it encoded -- never speak a version we do not implement -- is
    right, and is still enforced: the reply echoes OUR version, never the
    client's. What was wrong is refusing outright. The check could not tell an
    OLDER unknown version (1999-01-01, the case this test was written for) from
    a NEWER one, and Cursor's agent sends 2025-11-25 -- so a `-32602` dropped the
    connection and made the whole Cursor tool surface unreachable from the CLI.

    A client that cannot work with `2024-11-05` disconnects on its own. That is
    its decision, not the server's.
    """
    response = mt.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01"}},
        TOOLS,
    )
    assert "error" not in response
    assert response["result"]["protocolVersion"] == mt.PROTOCOL_VERSION


def test_initialize_falls_back_to_the_default_protocol():
    response = mt.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, TOOLS)
    assert response["result"]["protocolVersion"] == mt.PROTOCOL_VERSION


def test_initialized_notification_gets_no_reply():
    for method in ("notifications/initialized", "initialized"):
        assert mt.dispatch({"jsonrpc": "2.0", "method": method}, TOOLS) is None


def test_tools_list_reports_the_registry():
    result = mt.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, TOOLS)
    names = {t["name"] for t in result["result"]["tools"]}
    assert names == {"echo", "boom"}
    assert all("inputSchema" in t for t in result["result"]["tools"])


def test_spq_publish_schema_accepts_structured_output_evidence():
    output = mt.SPQ_LEDGER_APPEND_SCHEMA["properties"]["output"]
    assert output["type"] == "object"
    assert output["properties"]["id"]["type"] == "string"
    assert output["properties"]["digest"]["type"] == "string"


def test_tools_call_wraps_the_result_as_text_content():
    response = mt.dispatch(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"value": "hi"}}},
        TOOLS,
    )
    assert response["result"]["isError"] is False
    assert json.loads(response["result"]["content"][0]["text"]) == {"echoed": "hi"}


def test_tools_call_enforces_schema_required_fields_before_the_handler():
    """The advertised schema is a server boundary, not client-side advice."""
    mutations = []
    tools = {
        "publish": {
            "fn": lambda args: mutations.append(args) or {"ok": True},
            "description": "guarded mutation",
            "schema": mt.SPQ_LEDGER_APPEND_SCHEMA,
        },
        "cut": {
            "fn": lambda args: mutations.append(args) or {"ok": True},
            "description": "guarded mutation",
            "schema": mt.SPQ_CUT_SCHEMA,
        },
    }
    cases = (
        ("publish", {"work_unit_id": "WU-1", "condition": "integrated"}),
        ("cut", {"work_unit_id": "WU-1"}),
    )
    for request_id, (name, arguments) in enumerate(cases, start=10):
        response = mt.dispatch(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            tools,
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        assert response["result"]["isError"] is True
        assert "missing required argument" in payload["error"]

    assert mutations == [], "invalid calls must not reach either writer"


def test_tools_call_refuses_a_non_object_argument_payload():
    response = mt.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": []},
        },
        TOOLS,
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert response["result"]["isError"] is True
    assert "arguments must be an object" in payload["error"]


def test_unknown_method_returns_minus_32601():
    response = mt.dispatch({"jsonrpc": "2.0", "id": 4, "method": "nope"}, TOOLS)
    assert response["error"]["code"] == -32601


def test_unknown_method_without_an_id_gets_no_reply():
    """A notification is never answered, even when the method is unknown."""
    assert mt.dispatch({"jsonrpc": "2.0", "method": "nope"}, TOOLS) is None


# ── tool invocation ──────────────────────────────────────────────────────────


def test_unknown_tool_is_an_error_payload_not_an_exception():
    assert mt.call_tool(TOOLS, "missing", {}) == {"error": "unknown tool: missing"}


def test_a_raising_tool_never_kills_the_transport():
    """A tool bug must refuse, not leave the host with a dead stdio pipe."""
    result = mt.call_tool(TOOLS, "boom", {})
    assert result["error"].startswith("RuntimeError: tool exploded")


def test_tools_call_marks_an_error_result():
    response = mt.dispatch(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "boom", "arguments": {}}},
        TOOLS,
    )
    assert response["result"]["isError"] is True


# ── framing selection ────────────────────────────────────────────────────────


def test_each_host_keeps_its_own_default(monkeypatch):
    """Framing is a per-host protocol requirement, not drift.

    Codex must default to NDJSON — header bytes corrupt its stdio handshake
    before initialize completes. Cursor ships Content-Length framing.
    """
    assert mt.resolve_framing(mt.NDJSON) == mt.NDJSON
    assert mt.resolve_framing(mt.FRAMED) == mt.FRAMED


def test_env_overrides_win(monkeypatch):
    monkeypatch.setenv("SYNAPTORY_MCP_NDJSON", "1")
    assert mt.resolve_framing(mt.FRAMED) == mt.NDJSON
    monkeypatch.delenv("SYNAPTORY_MCP_NDJSON")
    monkeypatch.setenv("SYNAPTORY_MCP_FRAMED", "1")
    assert mt.resolve_framing(mt.NDJSON) == mt.FRAMED


# ── the two defects that made the Cursor host unreachable ──────────────────
#
# Found by pointing the real `cursor-agent` CLI at the shipped Cursor plugin.
# Neither is visible from `handle_tool`, which is how every existing Cursor MCP
# test drives the server -- they bypass the JSON-RPC and framing layer entirely,
# so the transport had no coverage at all from a real client.


def test_a_newer_protocol_version_negotiates_rather_than_refusing():
    """Cursor's agent sends `2025-11-25`. The server answered
    `-32602 Unsupported MCP protocol version` and the client dropped the
    connection, so the entire Cursor tool surface was unusable from the CLI.

    The spec's rule is that a server which does not speak the client's version
    answers with one it DOES speak. The original concern -- never claim a
    capability this transport lacks -- still holds, because the reply echoes OUR
    version, not the client's.
    """
    response = mt.dispatch(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "Cursor", "version": "1.0.0"}}},
        {},
    )
    assert "error" not in response, response
    assert response["result"]["protocolVersion"] == mt.PROTOCOL_VERSION


@pytest.mark.parametrize(
    "requested", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25",
                  "2099-01-01", "", None],
)
def test_initialize_never_refuses_on_version(requested):
    params = {"clientInfo": {"name": "x", "version": "1"}}
    if requested is not None:
        params["protocolVersion"] = requested
    response = mt.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}, {}
    )
    assert "error" not in response, (requested, response)
    assert response["result"]["protocolVersion"] == mt.PROTOCOL_VERSION


def test_framing_is_detected_from_the_first_byte_not_assumed():
    """`plugin-cursor` declared FRAMED because Cursor's IDE speaks it; Cursor's
    agent CLI speaks NDJSON. The framed reader then could not parse the bare
    JSON line, `serve` returned 0, and the client reported "connection failed"
    with nothing on stderr -- a silent exit indistinguishable from a clean one.

    A host's default is a guess about a client it does not control, so the wire
    decides.
    """
    import io as _io

    ndjson = _io.BufferedReader(_io.BytesIO(b'{"jsonrpc":"2.0","id":0}\n'))
    framed = _io.BufferedReader(_io.BytesIO(b'Content-Length: 2\r\n\r\n{}'))

    assert mt.detect_framing(mt.FRAMED, ndjson) == mt.NDJSON, (
        "a FRAMED-defaulting host must still serve an NDJSON client"
    )
    assert mt.detect_framing(mt.NDJSON, framed) == mt.FRAMED, (
        "and the reverse, or Cursor's IDE breaks while fixing its CLI"
    )


def test_an_explicit_framing_override_still_wins(monkeypatch):
    import io as _io

    ndjson = _io.BufferedReader(_io.BytesIO(b'{"jsonrpc":"2.0"}\n'))
    monkeypatch.setenv("SYNAPTORY_MCP_FRAMED", "1")
    assert mt.detect_framing(mt.NDJSON, ndjson) == mt.FRAMED


def test_an_unpeekable_stream_falls_back_to_the_default():
    """No guessing when the wire cannot be read."""
    class Opaque:
        pass

    assert mt.detect_framing(mt.FRAMED, Opaque()) == mt.FRAMED
    assert mt.detect_framing(mt.NDJSON, Opaque()) == mt.NDJSON


def test_sealed_manifests_have_an_automatic_producer():
    """`cycle_manifests` and `coordination_cycles` shipped with ingest
    endpoints, tables and UI pages — and nothing that called them.

    That is the defect #303 shipped when it added `cycle_id` columns with no
    field on any request model, repeated one level up: the views could only
    populate if an operator ran a CLI command nothing told them to run. Measured
    on a full 13-clone run: 0 rows before, 3 + 1 after.
    """
    from pathlib import Path

    core = Path(__file__).resolve().parents[3] / "core" / "lib"
    assert (core / "manifest_emitter.py").is_file()

    seal = (core / "spq_state_machine.py").read_text(encoding="utf-8")
    assert "emit_cycle_manifest" in seal, "seal_manifest must report the seal"

    coord = (core / "coordination_cycle.py").read_text(encoding="utf-8")
    assert coord.count("_observe(") >= 3, (
        "open AND every revision must report: the table is append-only, so a "
        "dropped child is a new row and the only record of what the release "
        "held before the drop"
    )
