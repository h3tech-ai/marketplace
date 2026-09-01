# receipt-schema

Receipt JSON schema consumed by both plugin hooks and the control-plane Ingest
API (`POST /v1/receipts`).

**Status:** empty scaffold. The runtime validator is
[`../lib/receipt_validator.py`](../lib/receipt_validator.py).

The canonical prose spec is `receipt-protocol.md`. It is not in this repo tree as
a sibling: protocols are IP-sensitive and control-plane delivered (ADR-016), so
they are authored under `plugin-claude/skills/_shared/protocols/` and land in a
project at `.synaptory/.protocols/` at runtime. They ship in no host package.

A future task will derive a machine-readable JSON Schema from the protocol and
land it here as `receipt.schema.json` so the control-plane can reuse the same
contract for `POST /v1/receipts` payload validation.
