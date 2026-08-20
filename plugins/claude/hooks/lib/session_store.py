#!/usr/bin/env python3
# Copyright 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Session record storage — v2.5-compatible JSON shape.

On the control-plane (v2.5) branch, sessions live in the OS keychain keyed by
(control_plane_url, project_id). The record JSON is defined by the Go struct
`cli/internal/keychain.Record`:

    type Record struct {
        ControlPlaneURL string    `json:"control_plane_url"`
        UPN             string    `json:"upn"`
        ProjectID       string    `json:"project_id,omitempty"`
        Role            string    `json:"role,omitempty"`
        SessionToken    string    `json:"session_token"`
        RefreshToken    string    `json:"refresh_token"`
        ExpiresAt       time.Time `json:"expires_at"`
        IssuedAt        time.Time `json:"issued_at"`
    }

This module writes the *same JSON* to a local mode-0600 file keyed by a hash
of (url, project_id). When v2.5 ships, the Go CLI reads these same JSON blobs
out of the OS keychain. Field names, types, ordering — all identical, so
migration is a copy-from-disk → store-in-keychain step with no schema change.

Default file location: ~/.synaptory/.session/<hash>.json  (0600)
Override via env: SYNAPTORY_SESSION_STORE_DIR
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

_STORE_DIR_ENV = "SYNAPTORY_SESSION_STORE_DIR"
_DEFAULT_STORE_DIR = os.path.expanduser("~/.synaptory/.session")


def _store_dir() -> str:
    d = os.environ.get(_STORE_DIR_ENV) or _DEFAULT_STORE_DIR
    os.makedirs(d, exist_ok=True)
    # Harden the dir itself; best-effort (chmod fails are fine on exotic FS).
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _slot_key(control_plane_url: str, project_id: str) -> str:
    """Deterministic account-name hash. Matches the Go CLI's account()
    helper (cli/internal/keychain/keychain.go) — the input string is
    `<url>|<project_id>` and we take the first 16 hex chars of its sha256.
    """
    return hashlib.sha256(
        f"{control_plane_url}|{project_id}".encode()
    ).hexdigest()[:16]


@dataclass
class SessionRecord:
    """Mirrors the v2.5 `keychain.Record` Go struct, field-for-field."""

    control_plane_url: str
    upn: str
    session_token: str
    refresh_token: str
    expires_at: datetime
    issued_at: datetime
    project_id: str = ""
    role: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        d["expires_at"] = self.expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        d["issued_at"] = self.issued_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        # Match Go `omitempty` — drop empty optional strings.
        if not d.get("project_id"):
            d.pop("project_id", None)
        if not d.get("role"):
            d.pop("role", None)
        # Match Go struct field order for byte-identical JSON where possible.
        ordered = {}
        for key in (
            "control_plane_url", "upn", "project_id", "role",
            "session_token", "refresh_token", "expires_at", "issued_at",
        ):
            if key in d:
                ordered[key] = d[key]
        return json.dumps(ordered, separators=(",", ":"))

    @classmethod
    def from_json(cls, data: str) -> "SessionRecord":
        d = json.loads(data)

        def _parse_ts(s: str) -> datetime:
            # Tolerate both 'Z' and '+00:00' suffixes.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)

        return cls(
            control_plane_url=d["control_plane_url"],
            upn=d["upn"],
            project_id=d.get("project_id", ""),
            role=d.get("role", ""),
            session_token=d["session_token"],
            refresh_token=d.get("refresh_token", ""),
            expires_at=_parse_ts(d["expires_at"]),
            issued_at=_parse_ts(d["issued_at"]),
        )


def save(record: SessionRecord) -> str:
    """Persist the record to disk (mode 0600). Returns the file path."""
    slot = _slot_key(record.control_plane_url, record.project_id)
    path = os.path.join(_store_dir(), f"{slot}.json")
    with open(path, "w") as f:
        f.write(record.to_json())
    os.chmod(path, 0o600)
    return path


def load(control_plane_url: str, project_id: str = "") -> SessionRecord | None:
    """Return the record for (url, project_id), or None if not present.

    Mirrors the Go CLI fallback: if the exact slot is missing and project_id
    is empty, and exactly one record exists for this URL, return that.
    """
    slot = _slot_key(control_plane_url, project_id)
    path = os.path.join(_store_dir(), f"{slot}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return SessionRecord.from_json(f.read())
        except (OSError, ValueError, KeyError):
            return None

    if not project_id:
        matches = list_for_url(control_plane_url)
        if len(matches) == 1:
            return matches[0]
    return None


def delete(control_plane_url: str, project_id: str = "") -> bool:
    slot = _slot_key(control_plane_url, project_id)
    path = os.path.join(_store_dir(), f"{slot}.json")
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def list_for_url(control_plane_url: str) -> list[SessionRecord]:
    out: list[SessionRecord] = []
    d = _store_dir()
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name)) as f:
                rec = SessionRecord.from_json(f.read())
        except (OSError, ValueError, KeyError):
            continue
        if rec.control_plane_url == control_plane_url:
            out.append(rec)
    return out


def list_all() -> list[SessionRecord]:
    out: list[SessionRecord] = []
    d = _store_dir()
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name)) as f:
                out.append(SessionRecord.from_json(f.read()))
        except (OSError, ValueError, KeyError):
            continue
    return out
