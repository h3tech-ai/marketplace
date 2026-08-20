# Phase 7: Agent Systems Architecture

## Objective

Design, evaluate, and harden production agent systems. Covers framework selection, agent topology, MCP/A2A protocol integration, observability, security, and human-in-the-loop controls. Produce deployable architecture and implementation artifacts.

> **Freshness requirement:** Agent frameworks, protocols, and tooling evolve rapidly. Use `WebSearch` to verify current framework versions, MCP server availability, and A2A spec status before recommending or implementing.

## Context Bridge

Read Phase 1 agent system audit from `analysis/system-audit.md` for the current agent inventory, topology, tool list, and identified gaps.

---

## Step 1: Framework Selection (if greenfield or migration)

If the system is greenfield or the current framework is mismatched, evaluate and select the right framework.

> **Always WebSearch current framework versions and benchmarks before presenting this table to the user.** The landscape changes quarterly.

### Framework Decision Matrix

| Framework | Best For | Token Overhead | Production Durability | Learning Curve |
|-----------|----------|---------------|----------------------|----------------|
| **Agno** | Multi-agent teams, RAG-heavy systems, unified storage/memory, production-first | Low–Medium | ⭐⭐⭐⭐⭐ | Medium |
| **LangGraph** | Complex stateful flows, long-running workflows, fault tolerance, human-in-the-loop | Low | ⭐⭐⭐⭐ | High |
| **CrewAI** | Rapid prototyping, role-based workflows, business process automation | High (~3x) | ⭐⭐⭐⭐ | Low |
| **AutoGen / AG2** | Conversation-driven multi-agent, research workflows, dialogue patterns | Medium | ⭐⭐ | Medium |
| **OpenAI Agents SDK** | OpenAI-native, simple tool use, handoffs between agents | Low | ⭐⭐⭐ | Low |
| **Semantic Kernel** | .NET/Python enterprise, Microsoft ecosystem, Semantic Kernel plugins | Medium | ⭐⭐⭐⭐ | Medium |

**Decision rules:**
- **Choose LangGraph** when: workflows are complex, stateful, long-running, or require fault tolerance and checkpointing. Production durability is the priority.
- **Choose Agno** when: you need multi-agent teams with shared memory/knowledge, RAG integration, and a clean production runtime (AgentOS).
- **Choose CrewAI** when: prototyping quickly, workflow is sequential with defined roles, token cost at scale is not yet a concern.
- **Avoid CrewAI at scale** when: projected monthly token cost with 3x overhead exceeds budget. Migrate to LangGraph or Agno.
- **Choose OpenAI Agents SDK** when: system is OpenAI-only, workflows are simple, and handoffs between specialized agents are the primary pattern.

Present recommendation with cost projection:

```
## Framework Recommendation

**Recommended:** [Framework name + version]
**Rationale:** [2-3 sentences on why this fits the system's requirements]

**Token overhead estimate:**
  Current approach:    ~X tokens/run
  With [framework]:    ~Y tokens/run  (+Z% overhead)
  At 10,000 runs/day:  ~$A/day vs $B/day

**Migration effort:** [Low / Medium / High] — [estimated hours]
**Alternatives considered:** [brief note on why alternatives were rejected]
```

---

## Step 2: Agent Topology Design

Design the agent graph — how agents are structured, how they communicate, and how work flows between them.

### Topology Patterns

| Pattern | When to Use | Example |
|---------|-------------|---------|
| **Sequential chain** | Tasks have strict dependencies, each step feeds the next | Research → Draft → Review → Publish |
| **Parallel fan-out** | Independent subtasks that can run concurrently | Analyze 5 documents simultaneously |
| **Hierarchical (orchestrator + workers)** | Complex tasks requiring coordination and specialization | Orchestrator delegates to domain specialists |
| **Event-driven** | Reactive workflows triggered by external events | Webhook → triage agent → route to specialist |
| **Human-in-the-loop** | High-stakes or irreversible actions requiring approval | Payment agent → human approval → execute |

### Topology Document Template

Produce `agent-systems/agent-topology.md`:

```markdown
# Agent Topology

## System Overview
[1-2 sentences on what the agent system does]

## Agent Roster

| Agent | Role | Framework | Tools | Triggers |
|-------|------|-----------|-------|----------|
| [name] | [role] | [framework] | [tool list] | [what invokes it] |

## Execution Flow

```
[User/Event] → [Orchestrator/Router]
                    ├── [Agent A] → [Tool 1, Tool 2]
                    ├── [Agent B] → [Tool 3]
                    └── [Agent C] → [Human Gate] → [Tool 4]
```

## State Management
[How state is passed between agents — shared memory, message passing, database]

## Failure Handling
[What happens when an agent fails — retry policy, fallback agent, escalation]

## Human-in-the-Loop Gates
[List every gate: which action, which agent, what the human approves]
```

---

## Step 3: MCP Integration

Model Context Protocol (MCP) is the standard for connecting agents to tools and data sources. Prefer MCP servers over bespoke tool wrappers for reusable integrations.

### When to Use MCP

- **Use MCP** when: the tool will be used by multiple agents or multiple projects, the integration is with an external service (database, API, file system), or the tool needs to be independently deployable and versioned.
- **Use direct function tools** when: the tool is specific to one agent, simple, and has no reuse value.

### MCP Server Design

For each integration that warrants an MCP server, produce `agent-systems/mcp-integration.md` with:

```markdown
# MCP Integration Design

## MCP Servers

### [Server Name]
**Purpose:** [what this server provides]
**Transport:** [stdio / HTTP / SSE]
**Authentication:** [none / API key / OAuth / JWT]
**Tools exposed:**
  - `tool_name(params)` → [description, return type]
  - ...
**Resources exposed:**
  - `resource://path` → [description]
**Security considerations:** [what data this server can access, blast radius if compromised]

## .mcp.json Configuration

```json
{
  "mcpServers": {
    "[server-name]": {
      "command": "[command]",
      "args": ["[args]"],
      "env": {
        "API_KEY": "${ENV_VAR}"
      }
    }
  }
}
```

## AuthN/AuthZ Design
[How the MCP server authenticates callers, what permissions are enforced]
```

### MCP Security Checklist

- [ ] Every MCP server that exposes write/destructive tools requires authentication
- [ ] API keys and secrets are environment variables — never in `.mcp.json` directly
- [ ] Tool descriptions do not leak sensitive information (connection strings, internal paths)
- [ ] MCP servers are scoped to minimum required permissions
- [ ] Destructive tools (delete, send, execute) have explicit confirmation parameters

---

## Step 4: A2A Integration (if cross-service agent communication)

Agent-to-Agent (A2A) protocol enables agents built on different frameworks to discover each other, negotiate capabilities, and delegate tasks across service boundaries.

### When to Use A2A

- **Use A2A** when: two agent systems built on different frameworks need to collaborate, agents are deployed as separate services, or you need vendor-neutral agent interoperability.
- **Use direct function calls** when: agents are in the same process/service and framework.

### A2A Design Document

Produce `agent-systems/a2a-design.md`:

```markdown
# A2A Integration Design

## Agent Cards

Each agent service exposes an Agent Card at `/.well-known/agent.json`:

### [Agent Service Name]
```json
{
  "name": "[agent-name]",
  "description": "[what this agent does]",
  "url": "https://[service-url]",
  "capabilities": {
    "streaming": true,
    "pushNotifications": false
  },
  "skills": [
    {
      "id": "[skill-id]",
      "name": "[skill-name]",
      "description": "[what this skill does]",
      "inputModes": ["text"],
      "outputModes": ["text", "data"]
    }
  ],
  "authentication": {
    "schemes": ["Bearer"]
  }
}
```

## Task Delegation Flow

```
[Client Agent] → POST /tasks/send → [Remote Agent]
                                          ↓
                              [Remote Agent executes]
                                          ↓
[Client Agent] ← task result ← [Remote Agent]
```

## Security
[JWT validation, allowed callers, rate limiting]
```

---

## Step 5: Observability Setup

Every production agent system needs trace-level observability. Silent failures — hallucinations, tool errors, cost explosions, prompt drift — are undetectable without traces.

> **WebSearch for current Langfuse, Arize Phoenix, and LangSmith versions and pricing before recommending.**

### Observability Platform Selection

| Platform | Best For | Self-Hosted | Key Features |
|----------|----------|-------------|--------------|
| **Langfuse** | Production monitoring, cost analytics, prompt governance | Yes (ClickHouse + Redis + S3) | Trace capture, cost tracking, prompt versioning, evaluations |
| **Arize Phoenix** | Local dev + evaluation, LLM-as-judge, experiment comparison | Yes (single Docker container) | OpenInference instrumentation, evaluation workflows, dataset management |
| **LangSmith** | LangGraph-native teams, debugging complex graphs | Cloud only | Deep LangGraph integration, dataset management, human feedback |
| **Helicone** | Lightweight cost monitoring, proxy-based | Yes | Zero-code integration via proxy, cost dashboards |

**Decision rule:** For most production systems, start with Langfuse (self-hosted, open-source, framework-agnostic). Add Arize Phoenix for evaluation workflows. Use LangSmith only if the team is fully committed to LangGraph.

### Instrumentation Plan

Produce `agent-systems/observability.md` with:

```markdown
# Observability Setup

## Platform: [Langfuse / Arize Phoenix / LangSmith / Helicone]
**Deployment:** [self-hosted / cloud]
**Integration method:** [SDK / OpenTelemetry / proxy]

## What Gets Traced

| Event | Trace Fields | Alert Threshold |
|-------|-------------|-----------------|
| LLM call | model, input_tokens, output_tokens, latency, cost | latency > 5s, cost > $0.10/call |
| Tool call | tool_name, input, output, success/failure, duration | failure_rate > 1% |
| Agent run | agent_name, total_tokens, total_cost, steps, outcome | total_cost > $1.00/run |
| A2A delegation | source_agent, target_agent, task_id, latency | latency > 10s |

## Alerts

| Alert | Condition | Action |
|-------|-----------|--------|
| Cost spike | Daily spend > 2x 7-day average | Page on-call |
| Quality regression | Evaluation score drops > 10% | Block deployment |
| Tool failure rate | > 1% in 5-minute window | Auto-rollback |
| Prompt drift | Response format deviation > 5% | Flag for review |

## Implementation

```python
# Example: Langfuse instrumentation for any LLM call
from langfuse.decorators import observe, langfuse_context

@observe()
def run_agent(user_input: str) -> str:
    langfuse_context.update_current_trace(
        user_id=current_user.id,
        session_id=session_id,
        tags=["production", "agent-v2"]
    )
    # ... agent execution
```
```

---

## Step 6: Human-in-the-Loop Controls

Any agent action that is irreversible — send email, execute payment, delete data, deploy code, call external API with side effects — MUST have a human confirmation gate.

### Gate Design

For each irreversible action identified in the tool inventory:

```markdown
## Human-in-the-Loop Gates

| Action | Agent | Gate Type | Timeout | Fallback |
|--------|-------|-----------|---------|----------|
| Send email | communication-agent | Approval (show draft) | 24h | Cancel and notify |
| Execute payment | payment-agent | Approval (show amount + recipient) | 5min | Abort transaction |
| Delete records | data-agent | Approval (show affected rows) | 1h | Cancel |
| Deploy to production | devops-agent | Approval (show diff) | 2h | Deploy to staging only |
```

### Implementation Pattern

```python
# LangGraph human-in-the-loop pattern
from langgraph.types import interrupt

def payment_node(state: AgentState) -> AgentState:
    payment_details = state["payment_details"]

    # Interrupt for human approval
    approval = interrupt({
        "action": "execute_payment",
        "amount": payment_details["amount"],
        "recipient": payment_details["recipient"],
        "message": f"Approve payment of ${payment_details['amount']} to {payment_details['recipient']}?"
    })

    if not approval.get("approved"):
        return {**state, "status": "cancelled", "reason": "Human rejected payment"}

    # Proceed with approved payment
    result = execute_payment(payment_details)
    return {**state, "payment_result": result, "status": "complete"}
```

---

## Output Files

- `agent-systems/framework-selection.md`
- `agent-systems/agent-topology.md`
- `agent-systems/mcp-integration.md` (if MCP servers designed)
- `agent-systems/a2a-design.md` (if A2A integration designed)
- `agent-systems/observability.md`
- Implementation code in `agent-systems/code/`

## Validation

Before completing Phase 7, verify:
- [ ] Framework selection includes token overhead estimate at projected scale
- [ ] Agent topology document covers all agents, tools, and failure paths
- [ ] Every MCP server has AuthN/AuthZ design
- [ ] Every irreversible tool action has a human-in-the-loop gate
- [ ] Observability platform is selected and instrumentation plan is defined
- [ ] A2A is used (or explicitly rejected) for cross-service agent communication

> **GATE: Present agent system design. Wait for user approval before implementing.**

## Quality Bar

Every recommendation must include implementation code or config. "Consider using LangGraph" is not acceptable. "Migrate from CrewAI to LangGraph — here is the equivalent graph definition, estimated token reduction from 3x to 1x overhead, and migration steps" is acceptable.
