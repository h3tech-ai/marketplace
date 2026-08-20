# receipt-schema

Receipt JSON schema consumed by both plugin hooks and the control-plane Ingest
API (`POST /v1/receipts`).

**Status:** empty scaffold. The canonical prose spec lives at
[`../skills/_shared/protocols/receipt-protocol.md`](../skills/_shared/protocols/receipt-protocol.md);
the runtime validator is
[`../hooks/lib/receipt_validator.py`](../hooks/lib/receipt_validator.py).

A future task will derive a machine-readable JSON Schema from the protocol and
land it here as `receipt.schema.json` so the control-plane can reuse the same
contract for `POST /v1/receipts` payload validation.
