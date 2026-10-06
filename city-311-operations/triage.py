"""AI-assisted intake and resident updates: PII redaction, prompts, and validation of model output.

Nothing here files or sends anything: staff confirm every suggestion in the app.
"""

import json
import re

# Redacted before any text reaches the model. Deliberately broad: a false redaction costs little.
PII_PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
]

LANGUAGES = {"en": "English", "es": "Spanish", "zh": "Chinese (Simplified)", "vi": "Vietnamese", "tl": "Tagalog"}
PRIORITIES = ["HIGH", "MEDIUM", "LOW"]


def redact(text: str) -> tuple[str, list[str]]:
    """Replace personal data with [TYPE] placeholders. Returns (redacted text, types found)."""
    found = []
    for label, pattern in PII_PATTERNS:
        text, n = pattern.subn(f"[{label}]", text)
        found += [label] * n
    return text, found


def triage_prompt(taxonomy: list[dict]) -> str:
    cats = "\n".join(f"- {t['category']} ({t['department']}, default priority {t['priority']})" for t in taxonomy)
    return f"""You route city 311 service requests. Read the resident's message and reply with JSON only:
{{"category": "<one category from the list, exactly as written>", "priority": "HIGH|MEDIUM|LOW",
  "location": "<street address or intersection mentioned, or empty>", "summary": "<one neutral sentence>",
  "confidence": <0.0-1.0>}}
Use HIGH only for immediate safety hazards. If no category fits, use "Needs manual review".
Categories:
{cats}"""


def parse_triage(raw: str, taxonomy: list[dict]) -> dict:
    """Validate the model's JSON. Anything off-list or malformed becomes a manual-review suggestion."""
    fallback = {"category": "Needs manual review", "priority": "MEDIUM", "location": "", "summary": "",
                "confidence": 0.0, "valid": False}
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return fallback
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return fallback
    names = {t["category"]: t for t in taxonomy}
    category = data.get("category")
    if category not in names:
        return fallback | {"summary": str(data.get("summary", ""))[:300]}
    priority = data.get("priority") if data.get("priority") in PRIORITIES else names[category]["priority"]
    try:
        confidence = min(max(float(data.get("confidence", 0)), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {"category": category, "department": names[category]["department"], "priority": priority,
            "location": str(data.get("location", ""))[:200], "summary": str(data.get("summary", ""))[:300],
            "confidence": confidence, "valid": True}


UPDATE_PROMPT = """You write status updates from the city to residents about their 311 service requests.
Write 2-4 short sentences in plain language (about a 6th-grade reading level), in {language}.
Say what was reported, the current status, and what happens next or when it was resolved, using only the facts given.
Don't promise dates that aren't in the facts. No greeting line or signature."""


def update_context(request: dict, sla_days: int) -> str:
    """Facts for the resident update. Deliberately excludes the free-text description (may contain PII)."""
    lines = [f"Request {request['request_id']}: {request['category']} ({request['department']} department).",
             f"Reported {request['created_at']:%B %d, %Y}. Target time to resolve: {sla_days} days."]
    if request["status"] == "CLOSED":
        lines.append(f"Resolved on {request['closed_at']:%B %d, %Y}.")
    else:
        lines.append("Status: open, assigned to the department's work queue.")
    return "\n".join(lines)
