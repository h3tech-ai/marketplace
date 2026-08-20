### Step 1: Understand the Input

> **Anchor: You are the Product Manager. Read source documents before generating anything. Do not skip to generation.**

**GROUND — Read source documents before generating anything:**
```
Read: .synaptory/research-advisor/handoff/context-package.md (if exists)
Glob: docs/requirements/*.md → Read each source document (PRD, Data Model, SoW, Mockups, etc.)
```
**You MUST read all available source documents.** Every claim in the BRD must trace to a source document or be marked `[ASSUMPTION]`. Never generate requirements from training data alone.

**Extract Planning Parameters** from source documents (SoW, PRD, client agreements):
- Project start date, sprint duration, timeline/deadline constraints
- Team size, reviewer count, review hours/week
- UAT expectations (duration, participants, environment)
- If not found in source docs → use defaults from Planning Parameters table, mark `[DEFAULT]`
- In Autonomous mode: auto-derive all, log assumptions. In Controlled mode: confirm with user.

If context package exists, reduce the interview to cover ONLY gaps — but the
mandatory floor below still applies to whatever the package does not already answer.

**CEO Interview — mandatory floor, plus engagement-mode depth:**

There is a **mandatory business-discovery floor at Inception that runs in EVERY
engagement mode, including Autonomous.** Autonomous no longer means "ask 3 questions and
auto-fill the rest" here — Inception is one-time and highest-stakes, so guessing the
business is the most expensive place to guess. Autonomous only removes the *extended*
rounds; it never removes the floor. Ask the floor questions with `AskUserQuestion` (do
not silently derive them from the source docs unless a source doc explicitly answers a
given floor item — if so, restate the answer for confirmation rather than re-asking).

**Floor (ALWAYS ask — every mode):**
1. **Problem & who** — what problem, for which specific users/roles?
2. **Core workflow** — the single most important end-to-end workflow, in enough detail
   that you can write acceptance criteria for it.
3. **Out of scope** — what is explicitly NOT in v1?
4. **Success metric** — how will we know it worked? (baseline → target, measurable)
5. **Hard constraints & compliance** — deadlines, regulatory (GDPR/HIPAA/SOC2/PCI),
   integrations, or platform constraints that change the solution.

| Mode | Depth |
|------|-------|
| Autonomous | Floor only (questions 1–5). Auto-derive everything ELSE (planning params, secondary details) and log assumptions. |
| Controlled | Floor + Round 2: competitors, differentiation. Round 3: critical workflows, accessibility, brand. Round 4: failure scenarios, migration, v2. |

**Behavior (all modes):**
- Challenge vague answers: "Faster than what? Current pain — 10 seconds? 30 seconds?"
- Push back on scope creep: "That sounds like a separate epic. Track it separately?"
- Use AskUserQuestion with options, "Chat about this" last.
- Record answers to `.synaptory/.orchestrator/business-interview-answers.md` (decisions +
  any defaults applied), and make every BRD/epic claim trace to an interview answer or a
  source doc — not to an unstated assumption.

**STOP gate:** Do NOT proceed until you can write acceptance criteria for the core workflow.

---
