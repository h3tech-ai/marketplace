#!/usr/bin/env python3
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Which SESSION is driving a Cycle — the record that did not exist (#791).

The Stop-hook loop used to address "whatever session the hook fired in", which
is a property of where a terminal happens to be pointed. In a two-session
arrangement it told a coordination-only session to dispatch a Work Unit of a
Cycle it did not hold, against a sealed declaration that names exactly one
Engineering Lead, and then refused to let it stop.

WHAT IDENTIFIES THE HOLDER, and why it is this and nothing else. Four
candidates existed when this module was written; three do not discriminate:

  cwd            not ownership, location. This is the signal that failed.
  `runner_id`    `spq_paths.default_runner_id()` is the HOSTNAME. Two sessions
                 on one laptop share it. It discriminates machines (#766), not
                 sessions.
  the sealed declaration's `engineering_lead`
                 a PERSON. Both sessions run as the same UPN, so it cannot
                 separate them either.
  the host session id
                 the only value that differs between two sessions in one
                 checkout. It is not durable by itself — this module is what
                 makes it durable.

THE CLAIM IS ESTABLISHED BY AN ACT, NEVER BY A STOP. A session becomes the
holder when it DISPATCHES a delivery agent into the Cycle (SubagentStart, the
one event that both carries the session id and proves the session writes into
the Cycle). A claim taken at Stop time would be "first session to stop wins",
which does not merely fail to fix #791 — it can invert it, muting the real
Engineering Lead because a coordinator happened to stop first.

So the holder is THE SESSION THAT MOST RECENTLY DISPATCHED INTO THIS CYCLE.
That definition is what survives a session restart: a restarted lead reclaims
on its first dispatch, with no operator step and no environment variable. It
is also why `claim` takes the Cycle from a live holder rather than refusing —
refusing would leave a crashed session's claim muting its own successor.

WHAT THIS IS NOT. It is not a lock and it grants nothing. Two leads on one
Cycle are forbidden by the sealed declaration and refused by scope
intersection in `begin_dispatch`; this module only decides who the loop may
SPEAK TO. A stolen claim is logged (`cycle_holder_takeover`) precisely because
it is the observable trace of the condition those other checks exist to stop.

TTL. A claim that has not been refreshed within `DEFAULT_TTL_SECONDS` stops
muting anyone. Expiry only ever PERMITS continuation — it never transfers a
claim away from a session that is still dispatching or still stopping, because
such a session is never stale. A generous TTL is therefore the safe direction:
too short re-admits #791, too long only delays the loop resuming for a
restarted session that has not yet dispatched.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Two hours. Comfortably longer than any single dispatch (the gap during
#: which a live holder neither dispatches nor stops), and short enough that a
#: crashed session does not mute its project for a working day.
DEFAULT_TTL_SECONDS = 7200

#: The roles whose dispatch means "this session is driving the Cycle".
#:
#: Deliberately NARROWER than `synaptory:*`. The claim moves to the most recent
#: dispatcher, so a role a COORDINATOR plausibly dispatches must not establish
#: one — a coordination session asking a research-advisor a question would
#: otherwise take the Cycle and mute the real Engineering Lead until its next
#: dispatch, which is #791 inverted rather than fixed.
#:
#: The members are exactly the roles the loop itself instructs: the pipeline
#: roles behind `story_pipeline.CONTINUE_ELIGIBLE`'s `dispatch_se` / `_qe` /
#: `_cr`, plus `spq_state_machine.ACCEPTANCE_ROLES`. `test_cycle_holder.py`
#: pins that correspondence so neither list can drift alone.
DRIVING_ROLES = frozenset(
    {
        "software-engineer",
        "quality-engineer",
        "code-reviewer",
        "compliance-engineer",
        "platform-engineer",
        "technical-writer",
    }
)


def drives_a_cycle(role: str) -> bool:
    """True when dispatching `role` makes a session the Cycle's driver.

    An UNKNOWN or absent role claims: a host that cannot tell us which role it
    dispatched is one whose loop we would otherwise never be able to address.
    """
    text = str(role or "").strip().lower()
    if not text:
        return True
    return text.split(":")[-1] in DRIVING_ROLES


#: Verdicts `evaluate` returns. Only `held_by_other` mutes the loop.
UNCLAIMED = "unclaimed"
HELD_BY_SELF = "held_by_self"
HELD_BY_OTHER = "held_by_other"
STALE = "stale"

_SCHEMA = "1"
_KIND = "synaptory.cycle_holder"


def holder_path(project_dir: str) -> Path:
    return Path(project_dir) / ".synaptory" / ".orchestrator" / "cycle-holder.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: Any) -> datetime | None:
    text = str(stamp or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_holders(project_dir: str) -> dict[str, Any]:
    """Every recorded claim, keyed by scope. Unreadable file → no claims.

    An unreadable record must read as "nobody holds anything", never as "held
    by someone else": the failure direction that mutes a legitimate loop is the
    one this whole module exists to avoid creating.
    """
    import json

    try:
        body = json.loads(holder_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    holders = body.get("holders") if isinstance(body, dict) else None
    return dict(holders) if isinstance(holders, dict) else {}


def _write_holders(project_dir: str, holders: dict[str, Any]) -> None:
    import json

    path = holder_path(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": _SCHEMA, "kind": _KIND, "holders": holders}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        # Best-effort, exactly like the runaway guard beside it: a lost write
        # re-evaluates on the next dispatch. It must never break a hook.
        pass


def evaluate(
    project_dir: str,
    scope: str,
    session_id: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only: may `session_id` be driven for `scope`?

    Returns ``{"verdict", "holder", "age_seconds"}``. `holder` is the recorded
    claim (or None). An EMPTY `session_id` can never match a recorded holder,
    so a session whose host gave the hook no identity is treated as "not the
    holder" — the fail-safe direction for a governance component, which is to
    let the session stop.
    """
    record = read_holders(project_dir).get(scope)
    if not isinstance(record, dict) or not str(record.get("session_id") or "").strip():
        return {"verdict": UNCLAIMED, "holder": None, "age_seconds": None}

    moment = now or _now()
    seen = _parse(record.get("last_seen")) or _parse(record.get("claimed_at"))
    age = (moment - seen).total_seconds() if seen else None

    if str(record.get("session_id")) == str(session_id or ""):
        return {"verdict": HELD_BY_SELF, "holder": record, "age_seconds": age}
    # No readable timestamp → treat the claim as expired rather than as an
    # eternal mute. A record we cannot date is not evidence of a live holder.
    if age is None or age > max(0, int(ttl_seconds)):
        return {"verdict": STALE, "holder": record, "age_seconds": age}
    return {"verdict": HELD_BY_OTHER, "holder": record, "age_seconds": age}


def claim(
    project_dir: str,
    scope: str,
    session_id: str,
    *,
    runner_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record `session_id` as the holder of `scope` — the ACT-based claim.

    Called when a session dispatches a delivery agent. Takes the Cycle from a
    previous holder, live or not: the most recent dispatcher is the holder, by
    definition, and that is what lets a restarted session reclaim its own Cycle
    without an operator step.
    """
    sid = str(session_id or "").strip()
    key = str(scope or "").strip()
    if not sid or not key:
        return {"claimed": False, "reason": "no session id or scope"}

    moment = _iso(now or _now())
    holders = read_holders(project_dir)
    previous = holders.get(key) if isinstance(holders.get(key), dict) else {}
    prior_sid = str(previous.get("session_id") or "")

    holders[key] = {
        "session_id": sid,
        "runner_id": str(runner_id or previous.get("runner_id") or ""),
        "claimed_at": moment if prior_sid != sid else (previous.get("claimed_at") or moment),
        "last_seen": moment,
        "dispatches": (int(previous.get("dispatches") or 0) + 1) if prior_sid == sid else 1,
    }
    _write_holders(project_dir, holders)
    return {
        "claimed": True,
        "scope": key,
        "session_id": sid,
        "previous_session_id": prior_sid,
        "took_over": bool(prior_sid and prior_sid != sid),
    }


def touch(
    project_dir: str,
    scope: str,
    session_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Refresh a claim this session ALREADY holds. Never creates, never steals.

    The Stop hook calls this so an actively looping holder does not age out
    between dispatches. It is deliberately incapable of establishing a claim:
    a Stop-established claim is the "first to stop wins" failure described in
    the module docstring.
    """
    sid = str(session_id or "").strip()
    key = str(scope or "").strip()
    if not sid or not key:
        return False
    holders = read_holders(project_dir)
    record = holders.get(key)
    if not isinstance(record, dict) or str(record.get("session_id") or "") != sid:
        return False
    record["last_seen"] = _iso(now or _now())
    holders[key] = record
    _write_holders(project_dir, holders)
    return True
