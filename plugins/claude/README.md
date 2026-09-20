# Synaptory for Claude Code

**Governed multi-agent SPQ delivery for Claude Code.** Synaptory coordinates role-based work through SPQ Cycles, validates dispatch-bound receipts and verification evidence, and stops at human decisions instead of treating agent completion as delivery acceptance.

> **Version:** 1.3.4 · **Author:** [H3Tech Inc.](https://github.com/h3tech-ai) · **Distribution:** Proprietary

## Requirements

| Requirement | Minimum |
|---|---|
| **Claude Code** | `2.1.196` |
| **Synaptory CLI** | Compatible with the plugin version and available on `PATH` |
| **Project access** | Membership in the Control Plane project named by `.synaptory.yaml` |

## Quick start

### 1. Install the CLI and sign in

```bash
# macOS / Linux
curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash
```

```powershell
# Windows PowerShell
iwr -useb https://synaptory.h3t.co/cli/install.ps1 | iex
```

```bash
synaptory login
synaptory whoami
```

Use `synaptory login --device` on a machine without a browser.

### 2. Install the Claude Code plugin

Run inside Claude Code:

```text
/plugin marketplace add https://github.com/h3tech-ai/marketplace
/plugin install "synaptory@h3tech-ai"
```

Reload Claude Code and run:

```text
/synaptory doctor
```

### 3. Configure SPQ explicitly

At the project root:

```yaml
# .synaptory.yaml
project_id: "my-project"
build_mode: "spq"
engagement_mode: "structured"

tracker:
  backend: "local"
```

When `build_mode` is omitted, the current compatibility default is Scrum. New projects should always select SPQ explicitly.

### 4. Start the first Cycle

```text
/synaptory Use SPQ to deliver account sign-in with audit logging. Start
Discovery and show me the proposed baseline before Commit.
```

The primary lifecycle is:

```text
Discovery → Commit → Cycle Execution → Sync → Checkpoint ─┐
                      ↑                                   │
                      └──────── next Cycle ───────────────┘
                                                          └→ Acceptance → Complete
```

Baseline approval, Cycle scope, Sync entry/clearance, Checkpoint direction, and final Acceptance remain explicit human decisions.

## Delivery roles

| Agent | Responsibility |
|---|---|
| **Project Owner** | Discovery baseline, Cycle outcome, Work Unit decomposition |
| **Solution Architect** | Architecture decisions, ADRs, and system contracts |
| **Software Engineer** | Bounded implementation work |
| **Quality Engineer** | Verification, tests, and evidence |
| **Code Reviewer** | Findings and architecture conformance |
| **Compliance Engineer** | Security, risk, and compliance evidence |
| **Platform Engineer** | Delivery infrastructure, reliability, and operations |
| **Technical Writer** | Cycle/Acceptance evidence and documentation |
| **Research Advisor** | Research and structured option analysis |

Claude Code supplies the host-native integration. Runtime federation is an **operator preview**: the bridge can execute an authorized SPQ attempt on a certified profile through `synaptory runtime execute`, but normal host dispatch does not route through it yet, so configuring `runtimes:` does not change an ordinary Cycle. Installing another host package does not enable federation by itself.

```bash
synaptory runtimes list
synaptory runtimes doctor
```

Runtime selection cannot expand a Work Unit's authority, approve evidence, or advance canonical lifecycle state.

## Compatibility paths

Scrum and Kanban remain available for existing projects while retirement is planned. They do not support runtime federation. Start substantial new delivery as SPQ and use the migration guide at a safe lifecycle boundary.

Focused operations such as Debug, Review, Test, Secure, Architect, Document, Explore, Preview, and Status remain available. When a Cycle is active, their outputs should be bound to its Work Unit evidence instead of creating a parallel lifecycle.

## Multi-host ownership

- Shared lifecycle, receipt, verification, tracker, and runtime-selection code is authored in the shared/core source and composed into every host distribution.
- Claude-specific hook and plugin behavior is authored here.
- Cursor-specific behavior lives in `../plugin-cursor`.
- Codex-specific behavior lives in `../plugin-codex`; Codex `0.147.0+` is certified for the full standard, non-regulated SPQ lifecycle.
- Cross-host handoff is not supported. Use one host as writer for an active governed stage.

## Runtime artifacts

Synaptory stores repository-owned lifecycle evidence under `.synaptory/`, including Cycle manifests, Work Unit receipts, workstream readiness records, Sync verdicts, and resume information. Treat these artifacts as governed records: do not hand-edit them or invoke internal lifecycle Python modules to force a transition.

## Documentation

Open **GUIDE.html** in the distribution for the complete guide. The build also produces **getting-started.html** for focused onboarding.

The guides cover:

- Claude Code, Codex, Cursor IDE, and Cursor headless installation;
- SPQ Cycle, Workstream, Work Unit, Sync, Checkpoint, and Acceptance concepts;
- optional multi-runtime policy and supervision;
- Configuration, Control Plane, enforcement, commands, and troubleshooting; and
- migration from legacy Scrum/Kanban projects.

## Session and IP terms

This software is proprietary to H3Tech Inc. See `LICENSE` and `NOTICE`.

- each session is personal to the signed-in user and non-transferable;
- governed runtime content is traceable to the authenticated session;
- unauthorized redistribution is prohibited.

Copyright 2024–2026 H3Tech Inc. All rights reserved.
