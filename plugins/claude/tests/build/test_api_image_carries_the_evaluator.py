"""Layer 2 -- the api image can reach the shared DoD evaluator.

`#435` direction A has the control plane RECOMPUTE the Definition of Done from
the receipts it already ingests, which makes the verdict's origin
authoritative by construction rather than by trusting a client field. It works
by importing `core/lib/story_pipeline.py` -- the same evaluator the plugin
runs, so the two cannot disagree.

WHY THIS TEST EXISTS. `dod_recompute.evaluator()` probes `/app/core/lib`
first, its docstring said "`Dockerfile.api` stages `core/lib` beside the
package", and no `COPY` line did. Worse, the build context was `api/`, from
which `core/` is not reachable at all -- so the copy could not have been added
without also widening the context. The deployed control plane therefore always
took the "not on this deployment; no verdict is credited" branch: green under
test, crediting nothing in production, with a comment asserting the wiring.

It is the epic's recurring shape -- a check that reads as implemented and runs
as nothing -- and the only defence that holds is a test on the wiring rather
than on the code either side of it.

WHAT IT DOES NOT DO. It does not build the image; a build needs a daemon and
this suite must run without one. It asserts the two facts a build would
consume: the Dockerfile copies `core/lib`, and every build spec that names
this Dockerfile passes a context that contains `core/`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "infra" / "docker" / "Dockerfile.api"

#: Files that name an api image build. Each carries `svc:dockerfile:context`
#: triples, and a context is a path relative to the repository root.
_SPEC_FILES = (
    Path("plugin-claude") / "synaptory",
    Path("infra") / "scripts" / "e2e-stack.sh",
)

_SPEC = re.compile(r'"([a-z-]+):(infra/docker/Dockerfile\.api):([^"]+)"')


def _copy_sources() -> list[str]:
    """The left-hand side of every `COPY` in the Dockerfile."""
    out: list[str] = []
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        parts = line.split()
        if len(parts) >= 3:
            out.extend(parts[1:-1])
    return out


def test_the_image_copies_the_shared_evaluator():
    sources = _copy_sources()
    assert any(s.rstrip("/") == "core/lib" for s in sources), (
        "Dockerfile.api does not copy `core/lib`, so `dod_recompute` finds no "
        "evaluator at `/app/core/lib` and the deployed control plane credits "
        "no DoD verdict at all (#435). Sources copied: %s" % sources
    )


def test_the_copy_is_narrow():
    """`core/lib` and not `core/`. The control plane runs the evaluator, not
    the hooks or the host packages, and a wider copy would put plugin trees
    into an API image."""
    sources = _copy_sources()
    assert not any(s.rstrip("/") == "core" for s in sources), (
        "Dockerfile.api copies all of `core/`, which puts host packages and "
        "hook scripts into the API image: %s" % sources
    )


def test_every_api_build_spec_uses_a_context_that_contains_core():
    """The context is the half that cannot be fixed in the Dockerfile.

    A `COPY core/lib` from an `api/` context fails the build outright, so
    these two must agree -- and they must agree in every file that builds the
    image, not only in the one somebody remembered.
    """
    checked = 0
    for rel in _SPEC_FILES:
        path = REPO_ROOT / rel
        if not path.is_file():  # pragma: no cover - both ship in-tree
            pytest.fail("build spec file is missing: %s" % rel)
        for svc, _dockerfile, context in _SPEC.findall(
            path.read_text(encoding="utf-8")
        ):
            checked += 1
            resolved = (REPO_ROOT / context).resolve()
            assert (resolved / "core" / "lib").is_dir(), (
                "%s builds the api image (%s) from context %r, which does not "
                "contain `core/lib` -- so the Dockerfile's copy of the shared "
                "evaluator cannot resolve" % (rel, svc, context)
            )
    assert checked >= 2, (
        "found %d api build specs; expected at least the release build and the "
        "e2e stack. If a build moved, this guard needs its new home rather "
        "than a lower number." % checked
    )
