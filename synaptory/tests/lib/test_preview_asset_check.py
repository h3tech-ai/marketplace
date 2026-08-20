"""Layer 1 — `plugin/hooks/lib/preview_asset_check.py` asset-gate tests.

Hypothesis (issue #134 GAP-9): a preview smoke test that only checks
`GET / == 200` passes even when every stylesheet/script 400s. The
asset-exhaustive gate must fetch every same-origin asset referenced by
the route HTML (scripts, links, images, srcset candidates, og:image)
plus CSS-referenced url(...)/@import targets, and fail on any non-2xx.

Tests run against a real local `http.server.ThreadingHTTPServer` on an
ephemeral port serving a tmp_path fixture site — no external network.
A second local server on a different port plays the "cross-origin"
host; its request log proves cross-origin refs are skipped, not fetched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hooks.lib.preview_asset_check import (
    _extract_css_refs,
    _parse_srcset,
    _should_skip,
    check_preview_assets,
)

SCRIPT = Path(__file__).resolve().parents[2] / "hooks" / "lib" / "preview_asset_check.py"

# PNG-ish / arbitrary bytes — content is irrelevant, only status codes matter.
_BYTES = b"\x89PNG-not-really"


# ─── Local HTTP fixture servers ───────────────────────────────────────────────


def _start_server(directory: Path, log: list) -> ThreadingHTTPServer:
    """Serve `directory` on an ephemeral 127.0.0.1 port, logging GET paths."""

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            log.append(self.path)
            super().do_GET()

        def log_message(self, *args):  # silence stderr chatter
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(directory))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _write_site(root: Path, cross_origin_base: str, broken: bool) -> None:
    """Write the fixture site.

    Good site (broken=False) — every referenced asset exists:
      index.html -> main.css, og.png, good.js, hero.png,
                    srcset(hero-1x.png, hero-2x.png)
      main.css   -> @import extra.css, url(good.woff2), url(bg.png)
      extra.css  -> url(extra.png)
      plus one cross-origin script (must be skipped) and one data: img.

    Broken site (broken=True) — three refs point at missing files:
      missing.js (script), missing-2x.png (srcset candidate),
      missing-bg.png (CSS url() background).
    """
    for sub in ("js", "img", "fonts", "styles"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    (root / "js" / "good.js").write_bytes(b"console.log('ok');")
    (root / "img" / "hero.png").write_bytes(_BYTES)
    (root / "img" / "hero-1x.png").write_bytes(_BYTES)
    (root / "img" / "extra.png").write_bytes(_BYTES)
    (root / "fonts" / "good.woff2").write_bytes(_BYTES)
    (root / "og.png").write_bytes(_BYTES)
    if not broken:
        (root / "img" / "hero-2x.png").write_bytes(_BYTES)
        (root / "img" / "bg.png").write_bytes(_BYTES)

    srcset_2x = "/img/missing-2x.png" if broken else "/img/hero-2x.png"
    bg = "/img/missing-bg.png" if broken else "/img/bg.png"
    missing_script = (
        '<script src="/js/missing.js"></script>' if broken else ""
    )

    (root / "styles" / "extra.css").write_text(
        ".x { background: url(/img/extra.png); }\n"
    )
    (root / "styles" / "main.css").write_text(
        '@import "/styles/extra.css";\n'
        '@font-face { font-family: g; src: url("/fonts/good.woff2"); }\n'
        f"body {{ background: url('{bg}'); }}\n"
    )
    (root / "index.html").write_text(
        "<!doctype html>\n"
        "<html><head>\n"
        '<link rel="stylesheet" href="/styles/main.css">\n'
        '<meta property="og:image" content="/og.png">\n'
        '<script src="/js/good.js"></script>\n'
        f"{missing_script}\n"
        f'<script src="{cross_origin_base}/x.js"></script>\n'
        "</head><body>\n"
        f'<img src="/img/hero.png" srcset="/img/hero-1x.png 1x, {srcset_2x} 2x">\n'
        '<img src="data:image/png;base64,iVBORw0KGgo=">\n'
        '<a href="#top">top</a>\n'
        "</body></html>\n"
    )


@pytest.fixture()
def site_factory(tmp_path):
    """Build a fixture site + its server (+ a cross-origin server).

    Yields build(broken) -> (base_url, main_log, cross_log). All servers
    are shut down at teardown.
    """
    servers: list[ThreadingHTTPServer] = []

    cross_root = tmp_path / "cross"
    cross_root.mkdir()
    (cross_root / "x.js").write_bytes(b"// would 200 if ever fetched")
    cross_log: list = []
    cross_server = _start_server(cross_root, cross_log)
    servers.append(cross_server)
    cross_base = f"http://127.0.0.1:{cross_server.server_address[1]}"

    counter = {"n": 0}

    def build(broken: bool):
        counter["n"] += 1
        root = tmp_path / f"site{counter['n']}"
        root.mkdir()
        _write_site(root, cross_base, broken=broken)
        log: list = []
        server = _start_server(root, log)
        servers.append(server)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        return base, log, cross_log

    yield build

    for s in servers:
        s.shutdown()
        s.server_close()


# ─── Pure-parsing unit tests ──────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_srcset_takes_url_token_of_each_candidate():
    assert _parse_srcset("/a.png 1x, /b.png 2x") == ["/a.png", "/b.png"]
    assert _parse_srcset("/a.png 480w,/b.png 800w ,") == ["/a.png", "/b.png"]


@pytest.mark.unit
def test_extract_css_refs_urls_and_imports():
    css = (
        '@import "/deep.css";\n'
        "@import url('/deeper.css');\n"
        'a { background: url("/x.png"); }\n'
        "b { src: url(/y.woff2); }\n"
        "c { cursor: url(data:image/png;base64,AA==); }\n"
    )
    plain, imports = _extract_css_refs(css)
    assert set(imports) == {"/deep.css", "/deeper.css"}
    assert "/x.png" in plain and "/y.woff2" in plain
    # data: refs survive extraction but are dropped by the skip filter.
    assert _should_skip("data:image/png;base64,AA==")


@pytest.mark.unit
@pytest.mark.parametrize(
    "ref", ["data:image/png;base64,AA==", "mailto:a@b.co", "javascript:void(0)", "#top", "  "]
)
def test_skip_filter(ref: str):
    assert _should_skip(ref)


# ─── End-to-end against a real local server ───────────────────────────────────


@pytest.mark.unit
def test_all_good_site_passes(site_factory):
    base, _log, _cross_log = site_factory(broken=False)
    result = check_preview_assets(base)
    assert result["passed"] is True
    assert result["failures"] == []
    assert result["routes"] == ["/"]
    # main.css, extra.css (@import), extra.png, og.png, good.js,
    # hero.png, hero-1x.png, hero-2x.png, good.woff2, bg.png
    assert result["assets_checked"] == 10
    assert "warning" not in result


@pytest.mark.unit
def test_missing_assets_fail_with_referrers(site_factory):
    base, _log, _cross_log = site_factory(broken=True)
    result = check_preview_assets(base)
    assert result["passed"] is False
    # good-site set, minus hero-2x/bg, plus missing.js/missing-2x/missing-bg
    assert result["assets_checked"] == 11

    by_url = {f["url"]: f for f in result["failures"]}
    assert set(by_url) == {
        f"{base}/js/missing.js",
        f"{base}/img/missing-2x.png",
        f"{base}/img/missing-bg.png",
    }
    for f in by_url.values():
        assert f["status"] == 404
    # Document-referenced failures point at the route HTML...
    assert by_url[f"{base}/js/missing.js"]["referrer"] == f"{base}/"
    assert by_url[f"{base}/img/missing-2x.png"]["referrer"] == f"{base}/"
    # ...CSS-referenced failure points at the stylesheet that named it.
    assert by_url[f"{base}/img/missing-bg.png"]["referrer"] == f"{base}/styles/main.css"


@pytest.mark.unit
def test_route_404_is_a_failure(site_factory):
    base, _log, _cross_log = site_factory(broken=False)
    result = check_preview_assets(base, routes=["/nope"])
    assert result["passed"] is False
    assert result["assets_checked"] == 0
    assert result["failures"] == [
        {"url": f"{base}/nope", "status": 404, "referrer": None}
    ]


@pytest.mark.unit
def test_cross_origin_and_data_refs_not_fetched(site_factory):
    base, log, cross_log = site_factory(broken=False)
    result = check_preview_assets(base)
    assert result["passed"] is True
    # The cross-origin server never saw a request — skipped, not fetched.
    assert cross_log == []
    # And the main server saw neither the cross-origin ref nor a data: URI.
    assert all("x.js" not in path for path in log)
    assert all(not path.lstrip("/").startswith("data:") for path in log)


@pytest.mark.unit
def test_max_assets_cap_fails_closed(site_factory):
    """#170 review: truncation must fail the gate — unchecked assets could
    include a missing stylesheet, so an all-good-so-far run cannot pass."""
    base, _log, _cross_log = site_factory(broken=False)
    result = check_preview_assets(base, max_assets=3)
    assert result["assets_checked"] == 3
    assert result["truncated"] is True
    assert result["passed"] is False
    assert "warning" in result
    assert "max-assets" in result["warning"]


@pytest.mark.unit
def test_max_assets_cli_exit_1_on_truncation(site_factory):
    base, _log, _cross_log = site_factory(broken=False)
    rc, payload = _run_cli(base, "/", "--max-assets", "3")
    assert rc == 1
    assert payload["passed"] is False
    assert payload["truncated"] is True


# ─── CLI round-trip ───────────────────────────────────────────────────────────


def _run_cli(*args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, json.loads(proc.stdout)


@pytest.mark.unit
def test_cli_subprocess_good_site_exits_zero(site_factory):
    base, _log, _cross_log = site_factory(broken=False)
    rc, payload = _run_cli(base, "--timeout", "10")
    assert rc == 0
    assert payload["passed"] is True
    assert payload["base_url"] == base
    assert payload["assets_checked"] == 10


@pytest.mark.unit
def test_cli_subprocess_broken_site_exits_one(site_factory):
    base, _log, _cross_log = site_factory(broken=True)
    rc, payload = _run_cli(base, "/", "--max-assets", "100")
    assert rc == 1
    assert payload["passed"] is False
    assert len(payload["failures"]) == 3
