"""
Central hook output helper for synaptory.
Enforces camelCase contract and 10 KB additionalContext cap.
"""
import json
import sys

_CAP_BYTES = 10 * 1024


def emit(
    event_name,
    *,
    additional_context=None,
    protected_context=None,
    decision=None,
    reason=None,
    system_message=None,
    suppress_output=False,
    terminal_sequence=None,
    session_title=None,
    watch_paths=None,
    reload_skills=None,
    initial_user_message=None,
    permission_decision=None,
    permission_decision_reason=None,
    updated_tool_output=None,
):
    """Emit hook JSON to stdout per the Claude Code hook output contract.

    `protected_context` is appended after `additional_context` and is the LAST
    thing trimmed. The cap is a blind head-truncation, so whatever a caller
    appends last is what silently disappears — and the SubagentStart hook
    appended the Execution Envelope last (#501). That is the machine-checked
    contract SubagentStop enforces, so losing its tail means grading an agent
    on text it was never shown; the compacted protocol index it displaced is
    a reference that names where the full versions live. Whichever side is
    trimmed, the trim is ANNOUNCED in place, because a silently shorter
    contract reads as a lighter one.
    """
    if protected_context:
        joined = (additional_context or "") + protected_context
        protected_raw = protected_context.encode("utf-8")
        if len(protected_raw) >= _CAP_BYTES:
            # The protected part alone overflows, so no budget can be
            # reserved. Drop the unprotected head entirely and let the ordinary
            # truncation below cut the protected part: keeping the head would
            # spend the whole cap on the reference material and deliver the
            # contract's first lines only, which is the worse half of each.
            additional_context = protected_context
        elif len(joined.encode("utf-8")) > _CAP_BYTES:
            marker = (
                "\n[synaptory: protocol index truncated to fit the 10 KB "
                "additionalContext cap; the Execution Envelope below is "
                "complete. Full protocols: .synaptory/.protocols/]\n"
            )
            budget = _CAP_BYTES - len(protected_raw) - len(marker.encode("utf-8"))
            head = (additional_context or "").encode("utf-8")
            additional_context = (
                head[: max(0, budget)].decode("utf-8", errors="ignore")
                + marker
                + protected_context
            )
        else:
            additional_context = joined

    if additional_context is not None:
        raw = additional_context.encode("utf-8")
        if len(raw) > _CAP_BYTES:
            # Reserve the marker's bytes INSIDE the cap so the final string
            # never exceeds _CAP_BYTES — slicing to _CAP_BYTES and then
            # appending the marker overshot the limit.
            marker = "\n[synaptory: additionalContext truncated to 10 KB cap]"
            budget = _CAP_BYTES - len(marker.encode("utf-8"))
            additional_context = (
                raw[: max(0, budget)].decode("utf-8", errors="ignore") + marker
            )

    out = {}
    if additional_context is not None:
        out["additionalContext"] = additional_context
    if system_message is not None:
        out["systemMessage"] = system_message
    if suppress_output:
        out["suppressOutput"] = True
    if terminal_sequence is not None:
        out["terminalSequence"] = terminal_sequence
    if decision is not None:
        out["decision"] = decision
    if reason is not None:
        out["reason"] = reason
    if updated_tool_output is not None:
        out["updatedToolOutput"] = updated_tool_output

    ev = (event_name or "").strip().upper().replace("-", "").replace("_", "")

    if ev == "SESSIONSTART":
        if session_title is not None:
            out["sessionTitle"] = session_title
        if watch_paths is not None:
            out["watchPaths"] = watch_paths
        if reload_skills is not None:
            out["reloadSkills"] = reload_skills
        if initial_user_message is not None:
            out["initialUserMessage"] = initial_user_message

    if ev == "PRETOOLUSE" and permission_decision is not None:
        hso = out.setdefault("hookSpecificOutput", {})
        hso["permissionDecision"] = permission_decision
        if permission_decision_reason is not None:
            hso["permissionDecisionReason"] = permission_decision_reason

    if out:
        print(json.dumps(out))
    sys.stdout.flush()
