#!/usr/bin/env python3
"""
analyze_token_usage.py — Analyze token/effort usage from synaptory pipeline receipts.

Reads all receipts from .synaptory/.orchestrator/receipts/ and produces a
cost/effort breakdown by agent, phase, and total.

Usage: python3 analyze_token_usage.py [project_dir] [--json]

Output: Terminal dashboard or JSON report.
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional


def load_receipts(project_dir: Path) -> list:
    receipts_dir = project_dir / ".synaptory" / ".orchestrator" / "receipts"
    receipts = []
    if not receipts_dir.exists():
        return receipts
    for f in sorted(receipts_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_filename"] = f.name
            receipts.append(data)
        except (json.JSONDecodeError, IOError):
            continue
    return receipts


def analyze(receipts: list) -> dict:
    """Compute usage metrics from receipts."""
    by_agent = {}
    by_phase = {}
    totals = {"files_read": 0, "files_written": 0, "tool_calls": 0, "agents": 0}

    for r in receipts:
        agent = r.get("agent", r.get("_filename", "unknown"))
        phase = r.get("phase", "UNKNOWN")
        effort = r.get("effort", {})
        fr = effort.get("files_read", 0)
        fw = effort.get("files_written", 0)
        tc = effort.get("tool_calls", 0)

        # By agent
        if agent not in by_agent:
            by_agent[agent] = {"files_read": 0, "files_written": 0, "tool_calls": 0, "tasks": 0}
        by_agent[agent]["files_read"] += fr
        by_agent[agent]["files_written"] += fw
        by_agent[agent]["tool_calls"] += tc
        by_agent[agent]["tasks"] += 1

        # By phase
        if phase not in by_phase:
            by_phase[phase] = {"files_read": 0, "files_written": 0, "tool_calls": 0, "agents": 0}
        by_phase[phase]["files_read"] += fr
        by_phase[phase]["files_written"] += fw
        by_phase[phase]["tool_calls"] += tc
        by_phase[phase]["agents"] += 1

        # Totals
        totals["files_read"] += fr
        totals["files_written"] += fw
        totals["tool_calls"] += tc
        totals["agents"] += 1

    # Estimate costs (rough — based on typical tool call token usage)
    # ~1000 tokens per tool call average, ~$0.003 per 1K input tokens (Sonnet)
    estimated_tokens = totals["tool_calls"] * 1000
    estimated_cost = estimated_tokens * 0.003 / 1000

    return {
        "by_agent": by_agent,
        "by_phase": by_phase,
        "totals": totals,
        "estimated_tokens": estimated_tokens,
        "estimated_cost_usd": round(estimated_cost, 2),
    }


def format_terminal(analysis: dict) -> str:
    lines = []
    lines.append("━━━ Token/Effort Analysis ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    # Totals
    t = analysis["totals"]
    lines.append(f"  TOTALS")
    lines.append(f"  ─────────────────────────────────────────────────────────")
    lines.append(f"  Agents run:    {t['agents']}")
    lines.append(f"  Files read:    {t['files_read']}")
    lines.append(f"  Files written: {t['files_written']}")
    lines.append(f"  Tool calls:    {t['tool_calls']}")
    lines.append(f"  Est. tokens:   ~{analysis['estimated_tokens']:,}")
    lines.append(f"  Est. cost:     ~${analysis['estimated_cost_usd']:.2f}")
    lines.append("")

    # By agent
    lines.append(f"  BY AGENT")
    lines.append(f"  ─────────────────────────────────────────────────────────")
    lines.append(f"  {'Agent':<30} {'Read':>6} {'Write':>6} {'Calls':>6} {'Tasks':>5}")
    lines.append(f"  {'─'*30} {'─'*6} {'─'*6} {'─'*6} {'─'*5}")
    for agent, m in sorted(analysis["by_agent"].items(), key=lambda x: -x[1]["tool_calls"]):
        lines.append(f"  {agent:<30} {m['files_read']:>6} {m['files_written']:>6} {m['tool_calls']:>6} {m['tasks']:>5}")
    lines.append("")

    # By phase
    lines.append(f"  BY PHASE")
    lines.append(f"  ─────────────────────────────────────────────────────────")
    phase_order = ["INCEPTION", "SPRINT_PLANNING", "SPRINT_EXECUTION", "SPRINT_REVIEW", "SPRINT_RETRO", "SPRINT_CLOSE", "RELEASE", "COMPLETE"]
    for phase in phase_order:
        if phase in analysis["by_phase"]:
            m = analysis["by_phase"][phase]
            pct = (m["tool_calls"] / max(analysis["totals"]["tool_calls"], 1)) * 100
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            lines.append(f"  {phase:<10} {bar} {pct:>5.1f}%  ({m['agents']} agents, {m['tool_calls']} calls)")
    # Any phases not in standard order
    for phase, m in analysis["by_phase"].items():
        if phase not in phase_order:
            pct = (m["tool_calls"] / max(analysis["totals"]["tool_calls"], 1)) * 100
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            lines.append(f"  {phase:<10} {bar} {pct:>5.1f}%  ({m['agents']} agents, {m['tool_calls']} calls)")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Analyze token/effort usage from pipeline receipts")
    parser.add_argument("project_dir", nargs="?", default=".", help="Project root (default: current dir)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    receipts = load_receipts(project_dir)

    if not receipts:
        print("No receipts found in .synaptory/.orchestrator/receipts/", file=sys.stderr)
        sys.exit(1)

    analysis = analyze(receipts)

    if args.json:
        print(json.dumps(analysis, indent=2))
    else:
        print(format_terminal(analysis))


if __name__ == "__main__":
    main()
