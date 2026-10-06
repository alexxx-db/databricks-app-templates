"""Alert workflow (state machine + maker-checker), decision recording, and the SAR-draft prompt.

Status is never updated in place: it's derived from the append-only `alert_dispositions` log.
`query(sql, params) -> list[dict]` is injected (SQL warehouse in the app, local Spark in tests).
"""

from typing import Callable

Query = Callable[[str, dict], list[dict]]

# status -> {action: new_status}
TRANSITIONS = {
    "OPEN": {"ESCALATE": "ESCALATED", "CLOSE_FALSE_POSITIVE": "CLOSED_FALSE_POSITIVE",
             "CLOSE_EXPLAINED": "CLOSED_EXPLAINED"},
    "ESCALATED": {"APPROVE_SAR": "SAR_APPROVED", "RETURN": "RETURNED"},
}
TRANSITIONS["RETURNED"] = TRANSITIONS["OPEN"]  # back with the analyst
REVIEW_ACTIONS = {"APPROVE_SAR", "RETURN"}       # require a second person
TERMINAL = {"SAR_APPROVED", "CLOSED_FALSE_POSITIVE", "CLOSED_EXPLAINED"}
MIN_RATIONALE = 20
ACTION_LABELS = {
    "ESCALATE": "Escalate for SAR review", "CLOSE_FALSE_POSITIVE": "Close: false positive",
    "CLOSE_EXPLAINED": "Close: activity explained", "APPROVE_SAR": "Approve SAR filing",
    "RETURN": "Return to analyst",
}


def current_status(history: list[dict]) -> str:
    """history: decisions for one alert, oldest first."""
    return history[-1]["new_status"] if history else "OPEN"


def escalated_by(history: list[dict]) -> str | None:
    return next((h["actor"] for h in reversed(history) if h["action"] == "ESCALATE"), None)


def allowed_actions(history: list[dict], actor: str) -> list[str]:
    actions = list(TRANSITIONS.get(current_status(history), {}))
    if current_status(history) == "ESCALATED":
        # Maker-checker: whoever escalated can't approve or return their own escalation.
        actions = [] if escalated_by(history) == actor else actions
    return actions


def apply(history: list[dict], action: str, actor: str, rationale: str) -> str:
    """Validate a decision and return the new status. Raises ValueError if it isn't allowed."""
    status = current_status(history)
    if action not in TRANSITIONS.get(status, {}):
        raise ValueError(f"Can't {action} an alert that is {status}.")
    if action in REVIEW_ACTIONS and escalated_by(history) == actor:
        raise ValueError("Maker-checker: a different person must review this escalation.")
    if len(rationale.strip()) < MIN_RATIONALE:
        raise ValueError(f"Rationale must be at least {MIN_RATIONALE} characters.")
    if not actor:
        raise ValueError("Unknown user: can't record a decision without an identity.")
    return TRANSITIONS[status][action]


def get_history(query: Query, s: str, alert_id: str) -> list[dict]:
    return query(f"SELECT action, new_status, actor, rationale, ai_draft_used, decided_at "
                 f"FROM {s}.alert_dispositions WHERE alert_id = :a ORDER BY decided_at", {"a": alert_id})


def record_decision(query: Query, s: str, alert_id: str, action: str, actor: str, rationale: str,
                    ai_draft_used: bool) -> str:
    history = get_history(query, s, alert_id)
    new_status = apply(history, action, actor, rationale)
    # ponytail: check-then-append; two people acting in the same second could both write.
    # Upgrade path: serialize decisions per alert (e.g. a Lakebase row lock) if that matters.
    query(f"INSERT INTO {s}.alert_dispositions (alert_id, action, new_status, actor, rationale, ai_draft_used, decided_at) "
          f"VALUES (:a, :action, :status, :actor, :rationale, :ai, current_timestamp())",
          {"a": alert_id, "action": action, "status": new_status, "actor": actor,
           "rationale": rationale.strip()[:2000], "ai": bool(ai_draft_used)})
    return new_status


SAR_SYSTEM_PROMPT = """You draft Suspicious Activity Report (SAR) narratives for a bank's AML investigations team.
Write a factual narrative covering who, what, when, where, and why the activity appears suspicious, in this order:
1. Subject and relationship (customer, account, KYC profile).
2. Activity: the specific transactions (cite transaction IDs, dates, amounts, counterparties, countries).
3. Why it is unusual: compare with the customer's profile and normal activity, and name the red flags.
4. Anything that would mitigate the concern, if present in the data.
Use only the facts provided. Do not speculate about intent or state that a crime occurred.
Mark it as a DRAFT for analyst review."""


def build_case_context(alert: dict, rule: dict, customer: dict, evidence: list[dict], profile: list[dict],
                       prior: list[dict]) -> str:
    lines = [
        f"## Alert {alert['alert_id']}",
        f"Rule {rule['rule_id']} v{rule['version']}: {rule['description']}",
        f"Risk score {alert['score']}; flagged amount ${alert['total_amount']:,.2f}; "
        f"activity {alert['first_ts']} to {alert['last_ts']}.",
        "## Customer",
        f"{customer['name']} (ID {customer['customer_id']}), {customer['segment']}, occupation {customer['occupation']}, "
        f"KYC risk {customer['risk_rating']}{', politically exposed person' if customer['pep'] else ''}, "
        f"customer since {customer['onboarded']}.",
        "## Flagged transactions",
        *[f"{t['txn_id']} {t['ts']} {t['type']} ${t['amount']:,.2f} {t['counterparty'] or ''} ({t['counterparty_country']})"
          for t in evidence],
        "## 180-day activity by type (count, total)",
        *[f"{p['type']}: {p['n']}, ${p['total']:,.2f}" for p in profile],
        "## Prior alerts for this customer",
        *([f"{p['alert_id']} ({p['rule_id']}): {p['status']}" for p in prior] or ["none"]),
    ]
    return "\n".join(lines)
