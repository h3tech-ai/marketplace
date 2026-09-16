"""Layer 1 -- reader-facing copy must not promise runtime routing (#396).

`select_runtime` and `build_envelope` have no production caller, and no host
dispatch path consults the selector, so configuring `runtimes:` does not change
an ordinary SPQ Cycle. #430 corrected the detailed guide; the review then found
six higher-visibility surfaces still promising the behaviour, which is worse
than the deep page being wrong: a reader entering through the repository
README, the plugin catalog, the marketplace card or the onboarding page can
configure the policy and never reach the caveat.

This guards the FRONT DOORS specifically, because they are the copies most
likely to be edited for marketing reasons and least likely to be re-read
against the code. It is deliberately a claim test rather than a wording test:
it fails when a surface asserts routing WITHOUT a qualifier, and stays quiet
about how the qualification is phrased.

Delete this file when Epic #339's dispatch wiring lands and the claims become
true, and let the conformance suite carry the property instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every reader-facing surface that describes what the product does. Paths are
#: relative to the repository root.
FRONT_DOORS = (
    "README.md",
    "plugin-claude/README.md",
    "plugin-claude/assets/marketplace-README.md",
    "docs/user-guide/concepts/how-it-works.md",
    "docs/user-guide/concepts/hosts-and-runtimes.md",
    "docs/user-guide/guides/release.md",
    "docs/user-guide/getting-started.md",
    "web/static/get-started/index.html",
)

#: Phrases that assert routing happens on the ordinary dispatch path.
ROUTING_CLAIMS = (
    "capability-profile runtime routing",
    "stage/capability routing overlay",
    "can dispatch an authorized spq attempt",
    "enable spq runtime federation so",
)

#: Any one of these near a claim marks it as qualified. Kept broad on purpose:
#: the test polices the promise, not the prose.
QUALIFIERS = (
    "operator preview",
    "not yet",
    "does not route",
    "no host dispatch path",
    "does not change an ordinary",
    "does not make an ordinary",
    "has no effect",
    "not on this path",
    "not yet wired",
)


def _text(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("rel", FRONT_DOORS)
def test_no_front_door_asserts_unqualified_routing(rel: str) -> None:
    body = _text(rel)
    for claim in ROUTING_CLAIMS:
        if claim in body:
            assert any(q in body for q in QUALIFIERS), (
                "%s asserts %r with no not-yet-wired qualifier anywhere in the "
                "file. Either qualify it or remove the claim: no host dispatch "
                "path calls the selector, so a reader who configures "
                "`runtimes:` on the strength of this sees nothing happen." % (rel, claim)
            )


@pytest.mark.parametrize(
    "rel",
    (
        "README.md",
        "plugin-claude/README.md",
        "plugin-claude/assets/marketplace-README.md",
        "web/static/get-started/index.html",
        "docs/user-guide/concepts/how-it-works.md",
    ),
)
def test_surfaces_that_mention_federation_carry_the_boundary(rel: str) -> None:
    """The catalog and landing copies the review named. Mentioning runtime
    federation at all obliges them to say where it currently stops."""
    body = _text(rel)
    if "runtime federation" not in body and "runtimes:" not in body:
        pytest.skip("%s does not mention runtime federation" % rel)
    assert any(q in body for q in QUALIFIERS), (
        "%s mentions runtime federation without stating that normal host "
        "dispatch does not route through it yet" % rel
    )
