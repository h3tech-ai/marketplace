"""Producer/verifier backend and model diversity (#753); a recorded fact, never a gate.

Two axes, kept apart on purpose, mirroring the split `ptb-assistant#86`
validated for its own PR-review loop:

- `backend_diversity` is the hard fact. The orchestrator dispatched to a named
  binary and knows which one, so `single-agent` vs `backend-distinct` is
  derived from the receipt's own `backend` field, never asserted from routing
  that does not exist yet (`agent_backends.roles` is a retired v2.5 shim;
  cross-backend dispatch routing is `runtimes:` / Epic #339, not this).
- `model_attribution` is an attestation. `model_identity.py` (#705) already
  types a receipt's model as `vendor_id`, `display_name`, or `unreported`, and
  a receipt whose runtime cannot report a model (Codex's `--json` stream
  carries no model field at all) is typed `unreported` rather than guessing.
  This module never upgrades that into `model-same` or `model-distinct`: a
  same-backend pair where either side is unattested is `unattributed`, not
  `model-same` -- reporting sameness nothing attested would be exactly the
  guarantee-credited-to-a-mechanism-not-providing-it defect this exists to
  reduce.

A missing receipt on either side answers `None`: absence is not a claim about
sameness or difference either.
"""

from __future__ import annotations

from model_identity import read_identity

ATTESTED_KINDS = {"vendor_id", "display_name"}


def _backend_of(receipt: dict) -> str | None:
    backend = receipt.get("backend")
    if isinstance(backend, str) and backend.strip():
        return backend.strip().lower()
    return None


def _model_summary(receipt: dict) -> dict:
    identity = read_identity(receipt.get("model"))
    kind = identity.get("kind")
    attested = kind in ATTESTED_KINDS
    return {
        "kind": kind,
        "value": identity.get("value") if attested else None,
        "attested": attested,
    }


def diversity_signal(
    producer_receipt: dict | None, verifier_receipt: dict | None
) -> dict | None:
    """Compare one producing receipt against one verifying receipt.

    Returns `None` when either side is missing. Otherwise a dict with
    `backend_diversity` ("single-agent" | "backend-distinct"),
    `model_attribution` ("unattributed" | "model-same" | "model-distinct"),
    and each side's resolved backend and model summary.
    """
    if not producer_receipt or not verifier_receipt:
        return None

    producer_backend = _backend_of(producer_receipt)
    verifier_backend = _backend_of(verifier_receipt)
    producer_model = _model_summary(producer_receipt)
    verifier_model = _model_summary(verifier_receipt)

    backend_diversity = (
        "backend-distinct"
        if producer_backend and verifier_backend and producer_backend != verifier_backend
        else "single-agent"
    )

    if not producer_model["attested"] or not verifier_model["attested"]:
        model_attribution = "unattributed"
    elif producer_model["value"] != verifier_model["value"]:
        model_attribution = "model-distinct"
    else:
        model_attribution = "model-same"

    return {
        "backend_diversity": backend_diversity,
        "producer_backend": producer_backend,
        "verifier_backend": verifier_backend,
        "model_attribution": model_attribution,
        "producer_model": producer_model,
        "verifier_model": verifier_model,
    }
