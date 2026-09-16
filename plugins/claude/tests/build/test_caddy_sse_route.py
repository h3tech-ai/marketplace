# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Layer 2 — the supervision SSE routes survive in every Caddy config (#394).

The edge serves TWO streaming routes, not one. `GET
/v1/attempts/{id}/events/stream` is the direct control-plane tail, and
`GET /api/attempts/{id}/stream` is the Next.js BFF route the Portal actually
opens: `SupervisionFeed` points an EventSource at the same-origin proxy
because EventSource cannot attach a bearer token, and
`supervisionStream.test.ts` pins that. An earlier revision of this module said
the direct route was the only one, which left the declarative contract and
this guard on the surface users do not consume (#396).

Each gets its own `handle` with `flush_interval -1`. Three properties are
load-bearing and none is visible from the directive:

  1. **Order.** `handle` blocks are mutually exclusive and evaluated in FILE
     ORDER, not by matcher specificity. Measured against Caddy 2.11.2: with the
     SSE block moved below `@api_v1`, `/v1/attempts/x/events/stream` was served
     by `@api_v1` and the streaming directive never applied. The route keeps
     answering 200 with `text/event-stream` either way, so the regression is
     invisible from the outside.

  2. **Every block.** `Caddyfile` carries TWO server blocks that proxy `/v1/*`
     (the named-host production block and a `:80` local block) and
     `Caddyfile.local` carries a third. A fix applied to one copy and not the
     others is the same silent-drift shape `test_ci_workflow.py` guards for
     `paths-ignore`.

  3. **Which blocks the BFF route belongs in.** Only the blocks whose
     `handle /api/*` forwards to the WEB container serve the BFF at all. The
     `:80` block of `Caddyfile` uses `handle_path /api/*` to STRIP the prefix
     and forward to the api service, so it never reaches a Next.js route
     handler and must not carry this matcher. The check keys on the handler
     rather than on the block, so it stays right if a block is added.

The check is textual on purpose: adapting the Caddyfile needs a Caddy binary,
and the `test-plugin` job installs pytest and nothing else.
"""
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CADDY_DIR = _REPO_ROOT / "infra" / "caddy"

_SSE_MATCHER = "path /v1/attempts/*/events/stream"
_API_MATCHER = "path /v1/* /healthz /readyz"
_BFF_SSE_MATCHER = "path /api/attempts/*/stream"
#: `handle`, not `handle_path`: the stripping form addresses the api service.
_BFF_HANDLE = "handle /api/* {"
_FLUSH = "flush_interval -1"


def _configs():
    return sorted(p for p in _CADDY_DIR.glob("Caddyfile*") if p.is_file())


def _lines(path):
    return path.read_text().splitlines()


def test_there_are_caddy_configs_to_check():
    """A rename that empties the glob must fail here, not pass vacuously."""
    assert _configs(), "no Caddyfile* under infra/caddy/ — did the glob rot?"


@pytest.mark.parametrize("config", _configs(), ids=lambda p: p.name)
def test_every_api_block_precedes_its_v1_catch_all_with_a_flushing_sse_route(config):
    """One SSE `handle` before each `/v1/*` `handle`, carrying the directive."""
    lines = _lines(config)
    api_at = [i for i, ln in enumerate(lines) if _API_MATCHER in ln]
    sse_at = [i for i, ln in enumerate(lines) if _SSE_MATCHER in ln]

    assert api_at, (
        "%s no longer proxies /v1/* — if the edge stopped serving the api, "
        "this guard needs rewriting rather than deleting" % config.name
    )
    assert len(sse_at) == len(api_at), (
        "%s has %d `/v1/*` block(s) but %d supervision-SSE block(s). Every "
        "server block that proxies the api needs its own copy (#394)."
        % (config.name, len(api_at), len(sse_at))
    )

    for sse_line, api_line in zip(sse_at, api_at):
        assert sse_line < api_line, (
            "%s:%d — the SSE matcher must come BEFORE the `/v1/*` handle at "
            "line %d. `handle` blocks are evaluated in file order, so below it "
            "the streaming route is swallowed by the catch-all while still "
            "answering 200." % (config.name, sse_line + 1, api_line + 1)
        )
        body = "\n".join(lines[sse_line:api_line])
        assert _FLUSH in body, (
            "%s:%d — the supervision SSE handle lost `%s`. It is declarative "
            "(Caddy already flushes text/event-stream), but it is where the "
            "route's streaming contract is written down." % (
                config.name, sse_line + 1, _FLUSH,
            )
        )


@pytest.mark.parametrize("config", _configs(), ids=lambda p: p.name)
def test_every_bff_block_precedes_its_api_catch_all_with_a_flushing_sse_route(config):
    """The stream the Portal opens gets the same treatment as the direct one.

    Keyed on `handle /api/*`, which is the form that forwards to the web
    container with the prefix intact so Next.js routing matches. A
    `handle_path /api/*` block strips the prefix and addresses the api
    service, serves no BFF route, and is correctly skipped.
    """
    lines = _lines(config)
    bff_at = [i for i, ln in enumerate(lines) if _BFF_HANDLE in ln]
    sse_at = [i for i, ln in enumerate(lines) if _BFF_SSE_MATCHER in ln]

    assert len(sse_at) == len(bff_at), (
        "%s has %d BFF `/api/*` handle(s) but %d BFF-SSE block(s). The Portal "
        "opens `/api/attempts/{id}/stream`, so every block that serves the BFF "
        "needs its own copy (#396)." % (config.name, len(bff_at), len(sse_at))
    )

    for sse_line, bff_line in zip(sse_at, bff_at):
        assert sse_line < bff_line, (
            "%s:%d — the BFF SSE matcher must come BEFORE the `handle /api/*` "
            "at line %d. Below it the stream is swallowed by the catch-all "
            "while still answering 200." % (config.name, sse_line + 1, bff_line + 1)
        )
        body = "\n".join(lines[sse_line:bff_line])
        assert _FLUSH in body, (
            "%s:%d — the BFF SSE handle lost `%s`. It is declarative (Caddy "
            "short-circuits its flush interval for text/event-stream, and the "
            "BFF route sets that content type), but it is where this route's "
            "streaming contract is written down." % (
                config.name, sse_line + 1, _FLUSH,
            )
        )


def test_the_bff_sse_matcher_is_absent_where_api_is_stripped_to_the_control_plane():
    """A `handle_path /api/*` block must NOT carry the BFF matcher.

    That form strips `/api` and forwards to the api service, so
    `/api/attempts/x/stream` would arrive as `/attempts/x/stream` on the
    control plane, which is not a route. Adding the matcher there would
    declare a streaming contract for a 404.
    """
    for config in _configs():
        lines = _lines(config)
        stripping = [i for i, ln in enumerate(lines) if "handle_path /api/* {" in ln]
        for start in stripping:
            end = next(
                (i for i in range(start + 1, len(lines)) if lines[i] == "\t}"),
                len(lines),
            )
            window = "\n".join(lines[max(0, start - 20):end])
            assert _BFF_SSE_MATCHER not in window, (
                "%s:%d — a `handle_path /api/*` block strips the prefix and "
                "addresses the api service; the BFF SSE matcher does not "
                "belong here." % (config.name, start + 1)
            )
