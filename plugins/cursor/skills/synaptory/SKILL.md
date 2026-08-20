---
name: synaptory
description: >-
  Synaptory orchestrator for Cursor. Classify the request, run Scrum/Kanban/SPQ
  ceremonies or standalone modes, and dispatch the SE→QE→CR pipeline with
  receipts. Use at the start of delivery work, status, doctor, or when the
  user says /synaptory.
disable-model-invocation: true
metadata:
  host: cursor
  wrapper_version: "1.1.1"
---

# synaptory

Cursor does not expand Claude `` !`bash` `` stubs. **First action:** run this with the Shell tool, then follow the printed body exactly. Do not paste it into the user transcript.

```bash
export SYNAPTORY_IDE=cursor
if ! command -v synaptory >/dev/null 2>&1; then
  echo "Install the CLI: curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash && synaptory login" >&2
  exit 1
fi
synaptory skills get synaptory --host cursor
```

Do not continue a delivery loop without that body.
