"""AI-drafted maintenance work orders, with deterministic guardrails.

- The model may only cite manual sections it was given; other citations are reported.
- Every plan starts with the machine type's lockout/tagout section, added if the model left it out.
- A planner reviews and files; nothing is filed automatically.
"""

import re

SYSTEM_PROMPT = """You are a maintenance planner's assistant at a manufacturing plant.
Draft a work order for the flagged machine using only the evidence and manual sections provided.
Format:
Summary: <one sentence>
Probable cause: <short, cite manual sections like [CNC-3.1]>
Steps: <numbered steps, each citing the manual section it comes from>
Parts: <part numbers from the manual, if any>
Start with the lockout/tagout section. Never invent part numbers, torque values or section IDs."""

CITATION = re.compile(r"\[([A-Z]{2,4}-\d+(?:\.\d+)?)\]")


def build_context(machine: dict, status: dict, episodes: list[dict], maintenance: list[dict],
                  manual: list[dict]) -> str:
    lines = [
        f"## Machine {machine['machine_id']} ({machine['machine_type']}, line {machine['line']})",
        f"Current status {status['status']}: vibration {status['vibration_x_baseline']}x its baseline "
        f"over the last 24 h, temperature {status['temperature_c']} C.",
        "## Alert history",
        *([f"Sustained high vibration from {e['started_at']} for {e['hours']} h" for e in episodes] or ["none"]),
        "## Maintenance history",
        *([f"{m['ts']} {m['type']}: {m['note']}" for m in maintenance] or ["none"]),
        "## Manual sections",
        *[f"[{m['section_id']}] {m['title']}: {m['content']}" for m in manual],
    ]
    return "\n".join(lines)


def check_citations(plan: str, allowed: set[str]) -> list[str]:
    """Section IDs cited in the plan that weren't in the provided manual (likely invented)."""
    return sorted({c for c in CITATION.findall(plan) if c not in allowed})


def ensure_lockout(plan: str, manual: list[dict]) -> tuple[str, bool]:
    """Guarantee the plan opens with lockout/tagout. Returns (plan, whether it had to be added)."""
    loto = next(m for m in manual if m["title"].lower().startswith("lockout"))
    if re.search(r"lock\s*-?\s*out|\[" + re.escape(loto["section_id"]) + r"\]", plan, re.I):
        return plan, False
    return f"Safety first [{loto['section_id']}]: {loto['content']}\n\n{plan}", True


def priority_for(status: str) -> str:
    return {"ALERT": "HIGH", "WATCH": "MEDIUM"}.get(status, "LOW")
