---
name: synaptory
description: >-
  Synaptory orchestrator for Cursor. Certified for SPQ (`build_mode: spq`).
  Classify the request, run the Cycle loop with receipts, and stop at human
  gates. Warn and recommend SPQ if the project is Scrum or Kanban. Use at the
  start of delivery work, status, doctor, or when the user says /synaptory.
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
