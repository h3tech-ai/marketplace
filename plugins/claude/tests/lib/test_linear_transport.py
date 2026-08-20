"""Layer 1 — LinearTransport GraphQL wire contract.

Two things about Linear differ from every other transport in the package and
are the reason most of these tests exist:

1. Personal API keys go in `Authorization` **raw** — no `Bearer ` prefix. The
   wrong form yields a bare 400 with no hint, so it is pinned explicitly.
2. Linear reports failures as an `errors` array inside an **HTTP 200**. A
   transport that classifies on status code alone (what teamwork_transport
   does) surfaces auth failures as confusing downstream KeyErrors.

No HTTP is mocked at the socket level — `urllib.request.urlopen` is patched,
matching the substitute-at-the-boundary convention used across this suite.
"""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.error
from pathlib import Path

import pytest

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills" / "_shared" / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def transport(monkeypatch):
    """A LinearTransport with __init__ bypassed so we don't need creds."""
    from tracker.transport.linear_transport import LinearTransport
    t = object.__new__(LinearTransport)
    t.team_key = "ENG"
    t.team_id = "team-uuid"
    t._api_available = True
    t._team = None
    t._labels_cache = None
    t._projects_cache = None
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    return t


class _FakeResponse(io.BytesIO):
    """Minimal urlopen context-manager stand-in."""

    def __init__(self, payload, headers=None, raw: str | None = None):
        body = raw if raw is not None else json.dumps(payload)
        super().__init__(body.encode("utf-8"))
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, response, captured=None):
    """Patch urlopen to return `response` and record the Request."""
    import urllib.request

    def fake_urlopen(req, timeout=None):
        if captured is not None:
            captured["req"] = req
            captured["timeout"] = timeout
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen, raising=True)


# ---------------------------------------------------------------------------
# Wire behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_authorization_header_has_no_bearer_prefix(transport, monkeypatch):
    """Personal API keys are sent raw. A `Bearer ` prefix yields a bare 400."""
    captured = {}
    _patch_urlopen(monkeypatch, _FakeResponse({"data": {"viewer": {}}}), captured)

    transport._api_call("query Q { viewer { id } }", op="viewer")

    auth = captured["req"].get_header("Authorization")
    assert auth == "lin_api_test"
    assert "Bearer" not in auth


@pytest.mark.unit
def test_posts_to_linear_graphql_endpoint(transport, monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, _FakeResponse({"data": {}}), captured)

    transport._api_call("query Q { viewer { id } }")

    assert captured["req"].full_url == "https://api.linear.app/graphql"
    assert captured["req"].get_method() == "POST"


@pytest.mark.unit
def test_request_body_is_query_plus_variables(transport, monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, _FakeResponse({"data": {}}), captured)

    transport._api_call("query Q($x: String) { viewer { id } }", {"x": "y"})

    body = json.loads(captured["req"].data.decode("utf-8"))
    assert set(body) == {"query", "variables"}
    assert body["variables"] == {"x": "y"}


@pytest.mark.unit
def test_graphql_errors_array_raises_adapter_error(transport, monkeypatch):
    """HTTP 200 + `errors` is the Linear-specific failure mode."""
    from tracker.base import AdapterError
    _patch_urlopen(monkeypatch, _FakeResponse(
        {"errors": [{"message": "Field 'nope' doesn't exist"}]}
    ))

    with pytest.raises(AdapterError) as exc:
        transport._api_call("query Q { nope }", op="probe")
    assert "doesn't exist" in str(exc.value)


@pytest.mark.unit
def test_graphql_authentication_error_raises_auth_error(transport, monkeypatch):
    from tracker.base import AdapterAuthError
    _patch_urlopen(monkeypatch, _FakeResponse({"errors": [{
        "message": "Authentication required",
        "extensions": {"code": "AUTHENTICATION_ERROR"},
    }]}))

    with pytest.raises(AdapterAuthError):
        transport._api_call("query Q { viewer { id } }")


@pytest.mark.unit
def test_graphql_ratelimited_code_raises_offline(transport, monkeypatch):
    from tracker.base import AdapterOfflineError
    _patch_urlopen(monkeypatch, _FakeResponse({"errors": [{
        "message": "too many requests",
        "extensions": {"code": "RATELIMITED"},
    }]}))

    with pytest.raises(AdapterOfflineError):
        transport._api_call("query Q { viewer { id } }")


@pytest.mark.unit
def test_partial_data_with_errors_still_raises(transport, monkeypatch):
    """Nothing in the tracker layer has partial-result semantics."""
    from tracker.base import AdapterError
    _patch_urlopen(monkeypatch, _FakeResponse({
        "data": {"issues": {"nodes": [{"id": "1"}]}},
        "errors": [{"message": "partial failure"}],
    }))

    with pytest.raises(AdapterError):
        transport._api_call("query Q { issues { nodes { id } } }")


@pytest.mark.unit
def test_success_returns_data_not_envelope(transport, monkeypatch):
    _patch_urlopen(monkeypatch, _FakeResponse({"data": {"viewer": {"id": "u1"}}}))

    out = transport._api_call("query Q { viewer { id } }")

    assert out == {"viewer": {"id": "u1"}}


@pytest.mark.unit
def test_http_429_reports_retry_after(transport, monkeypatch):
    from tracker.base import AdapterOfflineError
    err = urllib.error.HTTPError(
        "https://api.linear.app/graphql", 429, "Too Many Requests",
        {"Retry-After": "30"}, io.BytesIO(b""),
    )
    _patch_urlopen(monkeypatch, err)

    with pytest.raises(AdapterOfflineError) as exc:
        transport._api_call("query Q { viewer { id } }")
    assert "30" in str(exc.value)
    assert "1500" in str(exc.value)


@pytest.mark.unit
def test_http_401_raises_auth_error(transport, monkeypatch):
    from tracker.base import AdapterAuthError
    err = urllib.error.HTTPError(
        "https://api.linear.app/graphql", 401, "Unauthorized", {}, io.BytesIO(b""),
    )
    _patch_urlopen(monkeypatch, err)

    with pytest.raises(AdapterAuthError):
        transport._api_call("query Q { viewer { id } }")


@pytest.mark.unit
def test_rate_limit_remaining_zero_raises_offline(transport, monkeypatch):
    from tracker.base import AdapterOfflineError
    _patch_urlopen(monkeypatch, _FakeResponse(
        {"data": {}}, headers={"X-RateLimit-Requests-Remaining": "0"},
    ))

    with pytest.raises(AdapterOfflineError):
        transport._api_call("query Q { viewer { id } }")


@pytest.mark.unit
def test_missing_api_key_raises_offline_with_direnv_recipe(transport, monkeypatch):
    from tracker.base import AdapterOfflineError
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    transport._api_available = None  # force re-check

    with pytest.raises(AdapterOfflineError) as exc:
        transport._api_call("query Q { viewer { id } }")
    msg = str(exc.value)
    assert "LINEAR_API_KEY" in msg
    assert "direnv" in msg


@pytest.mark.unit
def test_urlerror_raises_offline(transport, monkeypatch):
    from tracker.base import AdapterOfflineError
    _patch_urlopen(monkeypatch, urllib.error.URLError("no route to host"))

    with pytest.raises(AdapterOfflineError):
        transport._api_call("query Q { viewer { id } }")


@pytest.mark.unit
def test_timeout_raises_offline(transport, monkeypatch):
    from tracker.base import AdapterOfflineError
    _patch_urlopen(monkeypatch, TimeoutError())

    with pytest.raises(AdapterOfflineError):
        transport._api_call("query Q { viewer { id } }")


# ---------------------------------------------------------------------------
# Resource methods
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_issues_always_merges_team_scope(transport, monkeypatch):
    """No caller can accidentally query the whole workspace."""
    captured = {}

    def fake_api_call(query, variables=None, *, op=""):
        captured["variables"] = variables
        return {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}

    monkeypatch.setattr(transport, "_api_call", fake_api_call)
    transport.search_issues({"labels": {"name": {"eq": "US-1"}}})

    assert captured["variables"]["filter"]["team"] == {"key": {"eq": "ENG"}}
    assert captured["variables"]["filter"]["labels"] == {"name": {"eq": "US-1"}}


@pytest.mark.unit
def test_paginate_follows_end_cursor(transport, monkeypatch):
    pages = [
        {"issues": {"nodes": [{"id": "a"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cur1"}}},
        {"issues": {"nodes": [{"id": "b"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}},
    ]
    seen_cursors = []

    def fake_api_call(query, variables=None, *, op=""):
        seen_cursors.append((variables or {}).get("after"))
        return pages[len(seen_cursors) - 1]

    monkeypatch.setattr(transport, "_api_call", fake_api_call)
    out = transport._paginate("q", {}, ("issues",))

    assert [n["id"] for n in out] == ["a", "b"]
    assert seen_cursors == [None, "cur1"]


@pytest.mark.unit
def test_paginate_stops_without_pageinfo(transport, monkeypatch):
    """A malformed response must terminate the loop, not spin forever."""
    calls = {"n": 0}

    def fake_api_call(query, variables=None, *, op=""):
        calls["n"] += 1
        return {"issues": {"nodes": [{"id": "a"}]}}  # no pageInfo

    monkeypatch.setattr(transport, "_api_call", fake_api_call)
    out = transport._paginate("q", {}, ("issues",))

    assert calls["n"] == 1
    assert len(out) == 1


@pytest.mark.unit
def test_paginate_walks_nested_path(transport, monkeypatch):
    """`("team", "cycles")` drills two levels before reading nodes."""
    def fake_api_call(query, variables=None, *, op=""):
        return {"team": {"cycles": {
            "nodes": [{"number": 1}], "pageInfo": {"hasNextPage": False},
        }}}

    monkeypatch.setattr(transport, "_api_call", fake_api_call)
    out = transport._paginate("q", {"id": "t"}, ("team", "cycles"))

    assert out == [{"number": 1}]


@pytest.mark.unit
def test_get_or_create_label_reuses_cache(transport, monkeypatch):
    """A cached label must not issue a mutation."""
    transport._labels_cache = {"US-1": "label-uuid"}
    calls = []

    def fake_api_call(query, variables=None, *, op=""):
        calls.append(op)
        return {}

    monkeypatch.setattr(transport, "_api_call", fake_api_call)
    assert transport.get_or_create_label("US-1") == "label-uuid"
    assert calls == []


@pytest.mark.unit
def test_get_or_create_label_creates_when_absent(transport, monkeypatch):
    transport._labels_cache = {}

    def fake_api_call(query, variables=None, *, op=""):
        return {"issueLabelCreate": {
            "success": True, "issueLabel": {"id": "new-uuid", "name": "US-2"},
        }}

    monkeypatch.setattr(transport, "_api_call", fake_api_call)
    assert transport.get_or_create_label("US-2") == "new-uuid"
    # And it is cached for the next call.
    assert transport._labels_cache["US-2"] == "new-uuid"


@pytest.mark.unit
def test_get_or_create_label_recovers_from_already_exists(transport, monkeypatch):
    """Losing the create race must re-read rather than fail the write."""
    from tracker.base import AdapterError
    transport._labels_cache = {}
    state = {"n": 0}

    def fake_api_call(query, variables=None, *, op=""):
        state["n"] += 1
        if state["n"] == 1:
            raise AdapterError("label name must be unique")
        return {"team": {"labels": {"nodes": [{"id": "raced-uuid", "name": "US-3"}]}}}

    monkeypatch.setattr(transport, "_api_call", fake_api_call)
    assert transport.get_or_create_label("US-3") == "raced-uuid"


@pytest.mark.unit
def test_get_team_raises_actionable_error_when_key_unknown(transport, monkeypatch):
    from tracker.base import AdapterError
    transport.team_id = ""

    monkeypatch.setattr(
        transport, "_api_call",
        lambda q, v=None, *, op="": {"teams": {"nodes": []}},
    )
    with pytest.raises(AdapterError) as exc:
        transport.get_team()
    assert "team_key" in str(exc.value)


# ---------------------------------------------------------------------------
# GraphQL document contract
# ---------------------------------------------------------------------------


def _documents():
    from tracker.transport import linear_transport as lt
    return {
        name: getattr(lt, name)
        for name in dir(lt)
        if re.match(r'^_[QM]_', name) and isinstance(getattr(lt, name), str)
    }


@pytest.mark.unit
def test_documents_exist():
    docs = _documents()
    assert docs, "no _Q_*/_M_* GraphQL constants found"


@pytest.mark.unit
def test_all_documents_have_balanced_braces():
    for name, doc in _documents().items():
        assert doc.count("{") == doc.count("}"), f"{name}: unbalanced braces"


@pytest.mark.unit
def test_declared_and_used_variables_match():
    """Every `$var` in a body is declared, and every declaration is used."""
    for name, doc in _documents().items():
        m = re.search(r'(?:query|mutation)\s+\w+\s*\(([^)]*)\)', doc)
        declared = set(re.findall(r'\$(\w+)\s*:', m.group(1))) if m else set()
        header_span = m.span() if m else (0, 0)
        body = doc[:header_span[0]] + doc[header_span[1]:]
        used = set(re.findall(r'\$(\w+)', body))
        assert used <= declared, f"{name}: undeclared variables {used - declared}"
        assert declared <= used, f"{name}: unused declarations {declared - used}"


@pytest.mark.unit
def test_paginating_documents_request_pageinfo():
    for name, doc in _documents().items():
        if "$after" not in doc:
            continue
        assert "hasNextPage" in doc and "endCursor" in doc, (
            f"{name}: paginates but does not select pageInfo — _paginate would "
            "silently return only the first page"
        )


@pytest.mark.unit
def test_issue_fragment_selects_every_field_the_mapper_reads():
    """Guards the silent-failure mode: a trimmed fragment makes
    `_issue_to_story` return `Story(status="TODO")` for every issue."""
    from tracker.transport.linear_transport import _ISSUE_FIELDS
    for field in ("id", "identifier", "url", "title", "description",
                  "estimate", "priority", "state", "type", "position",
                  "labels", "assignee", "cycle", "project"):
        assert field in _ISSUE_FIELDS, f"_ISSUE_FIELDS is missing {field!r}"


@pytest.mark.unit
def test_issue_queries_spread_the_issue_fragment():
    """A query selecting IssueFields must also embed the fragment text."""
    from tracker.transport import linear_transport as lt
    for name in ("_Q_ISSUES", "_Q_ISSUE", "_M_ISSUE_UPDATE"):
        doc = getattr(lt, name)
        assert "...IssueFields" in doc, f"{name}: does not spread IssueFields"
        assert "fragment IssueFields on Issue" in doc, (
            f"{name}: spreads IssueFields without including the fragment "
            "definition — Linear would reject the document"
        )


@pytest.mark.unit
def test_cycles_query_has_no_nested_issues_connection():
    """Linear multiplies nested connection sizes for complexity scoring, so
    `cycles(first: 100) { issues(first: 250) }` scores ~25,000 against a
    10,000-point ceiling and every sprint read is rejected outright. Cycle
    issues must come from a separate flat query."""
    from tracker.transport.linear_transport import _Q_CYCLES
    assert "issues(" not in _Q_CYCLES, (
        "_Q_CYCLES nests an issues connection — this exceeds Linear's maximum "
        "query complexity and also cannot carry the spec/entity filters"
    )
    assert "...IssueFields" not in _Q_CYCLES


@pytest.mark.unit
def test_no_document_nests_two_paginated_connections():
    """General guard against reintroducing the complexity blow-up anywhere."""
    for name, doc in _documents().items():
        # Count connection selections that request a page size.
        page_sites = re.findall(r'\w+\((?:[^()]*?)first:', doc)
        # `labels(first: 50)` inside the issue fragment is a leaf connection on
        # a single object, not a multiplier over another paginated list.
        multipliers = [s for s in page_sites
                       if not s.startswith(("labels(", "states(", "issues(first: 250"))]
        assert len(multipliers) <= 2, (
            f"{name}: {len(multipliers)} paginated connections "
            f"({multipliers}) — check the complexity budget"
        )


@pytest.mark.unit
def test_resolve_project_id_passes_uuids_through(transport, monkeypatch):
    called = []
    monkeypatch.setattr(transport, "_api_call",
                        lambda *a, **k: called.append(1) or {})
    uuid = "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
    assert transport.resolve_project_id(uuid) == uuid
    assert called == [], "a uuid needs no lookup"


@pytest.mark.unit
def test_resolve_project_id_maps_name_to_uuid(transport, monkeypatch):
    """IssueCreateInput.projectId rejects a display name."""
    monkeypatch.setattr(transport, "_api_call", lambda *a, **k: {
        "team": {"projects": {
            "nodes": [{"id": "proj-uuid", "name": "Mobile Q3"}],
            "pageInfo": {"hasNextPage": False},
        }}})
    assert transport.resolve_project_id("Mobile Q3") == "proj-uuid"
    assert transport.resolve_project_id("mobile q3") == "proj-uuid"


@pytest.mark.unit
def test_resolve_project_id_raises_on_unknown_name(transport, monkeypatch):
    from tracker.base import AdapterError
    monkeypatch.setattr(transport, "_api_call", lambda *a, **k: {
        "team": {"projects": {"nodes": [], "pageInfo": {"hasNextPage": False}}}})
    with pytest.raises(AdapterError, match="project not found"):
        transport.resolve_project_id("Nope")
