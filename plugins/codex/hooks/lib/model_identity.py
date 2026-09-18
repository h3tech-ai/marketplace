"""Receipt model attribution (#705); metadata is self-reported, never weights proof.

Bare strings are immutable legacy-untyped history. Version 1 typed identities
are bridge observations. Source semantics, not value spelling, determine kind.
"""

from __future__ import annotations

FAMILIES = {"claude-code", "cursor-agent", "codex"}
KINDS_BY_SOURCE = {"claude-code": "vendor_id", "cursor-agent": "display_name"}
PLACEHOLDERS = {"runtime-default", "policy-default", "unattributed", "unknown"}


def is_placeholder(value: str) -> bool:
    text = value.strip().lower()
    return text in PLACEHOLDERS or text.endswith("-runtime-unattributed")


def identity_problems(receipt: dict) -> list[str]:
    model = receipt.get("model")
    if not isinstance(model, dict):
        if "model_identity_version" in receipt:
            return ["model must be typed for model_identity_version"]
        if not isinstance(model, str):
            return [
                "model must be a legacy string or typed identity; a stand-in names no model"
            ]
        return []
    if (
        type(receipt.get("model_identity_version")) is not int
        or receipt["model_identity_version"] != 1
    ):
        return ["model_identity_version must be 1"]
    family, version = receipt.get("runtime_family"), receipt.get("runtime_version")
    if (
        not isinstance(family, str)
        or family not in FAMILIES
        or not isinstance(version, str)
        or not version.strip()
        or is_placeholder(version)
    ):
        return ["typed model requires runtime_family and measured runtime_version"]
    route = receipt.get("model_route")
    if (
        not isinstance(route, dict)
        or not isinstance(route.get("requested"), str)
        or not route["requested"].strip()
    ):
        return ["typed model requires model_route.requested"]
    if set(model) - {"kind", "value", "source", "evidence_class"}:
        return ["model has an unknown field"]
    kind, value = model.get("kind"), model.get("value")
    source, evidence_class = model.get("source"), model.get("evidence_class")
    if kind == "unreported":
        if (
            "value" in model
            or source != family + ".stream"
            or evidence_class != "runtime-only"
        ):
            return [
                "unreported model must omit value and carry its stream source and runtime-only evidence class"
            ]
        return []
    expected = KINDS_BY_SOURCE.get(family)
    if (
        not expected
        or kind != expected
        or source != family + ".system.init.model"
        or evidence_class != "runtime-reported-" + expected.replace("_", "-")
        or not isinstance(value, str)
        or not value.strip()
        or is_placeholder(value)
    ):
        return [
            "model kind, source, value and evidence_class must match the runtime metadata contract"
        ]
    return []


def read_identity(model: object) -> dict:
    """Read history without upgrading a string to an attested vendor ID."""
    if isinstance(model, str):
        return {
            "kind": "legacy_untyped",
            "value": model,
            "evidence_class": "legacy-untyped",
        }
    return dict(model) if isinstance(model, dict) else {"kind": "invalid"}
