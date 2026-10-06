"""Proactive care: service-credit policy, churn-risk flag, and validation of AI-drafted SMS.

The model never states money. It drafts one message with a {credit} placeholder; the app fills
in each customer's credit from `credit_for()` and checks the result. Drafts that break the rules
fall back to a fixed template.
"""

import math
import re

SMS_MAX = 160
PLACEHOLDER = "{credit}"


def credit_for(plan: str, monthly_charge: float, kind: str, duration_min: int) -> float:
    """Service-credit policy (illustrative): outages of 60+ minutes earn one day of service per started
    4 hours; business plans get double (SLA); capped at half the monthly charge. Degradations: no credit."""
    if kind != "OUTAGE" or duration_min < 60:
        return 0.0
    days = math.ceil(duration_min / 240)
    credit = monthly_charge / 30 * days * (2 if plan == "BUSINESS" else 1)
    return round(min(credit, monthly_charge / 2), 2)


def churn_risk(customer: dict) -> str:
    """HIGH if hit by 2+ incidents in the period or they already contacted us about this one."""
    return "HIGH" if customer["incidents_in_period"] >= 2 or customer["opened_ticket"] else "NORMAL"


SYSTEM_PROMPT = f"""You write SMS messages from a mobile operator to customers affected by a network incident.
Write ONE message, plain and apologetic, at most 140 characters, no emojis, no links.
Say what happened and that it is resolved (or being fixed, if ongoing).
If a credit applies, include the placeholder {PLACEHOLDER} exactly once where the amount goes, e.g. "a {PLACEHOLDER} credit".
Never write any dollar amount or number of days yourself. Never promise anything else."""


def build_brief(incident: dict, with_credit: bool) -> str:
    state = "ongoing, engineers are working on it" if incident["ongoing"] else f"resolved at {incident['ended_at']:%H:%M on %b %d}"
    what = "no service" if incident["kind"] == "OUTAGE" else "dropped calls and slow data"
    return (f"Incident: {what} in the {incident['area']} area from {incident['started_at']:%H:%M on %b %d}, {state}. "
            f"Credit applies: {'yes, use ' + PLACEHOLDER if with_credit else 'no, do not mention credit'}.")


def template(incident: dict, with_credit: bool) -> str:
    status = "We're fixing it now." if incident["ongoing"] else "It's resolved."
    what = "a service outage" if incident["kind"] == "OUTAGE" else "network issues"
    credit = f" We've added a {PLACEHOLDER} credit to your account." if with_credit else ""
    return f"Sorry for {what} in {incident['area']} on {incident['started_at']:%b %d}. {status}{credit}"


def validate_draft(draft: str, with_credit: bool, max_credit: float) -> list[str]:
    """Problems with a model draft. Empty list = OK to use."""
    problems = []
    if re.search(r"\$\s?\d|\d+\s?(dollars|usd)\b", draft, re.I):
        problems.append("states a money amount itself")
    count = draft.count(PLACEHOLDER)
    if with_credit and count != 1:
        problems.append(f"needs the {PLACEHOLDER} placeholder exactly once")
    if not with_credit and count:
        problems.append("mentions a credit when none applies")
    if len(render(draft, max_credit)) > SMS_MAX:
        problems.append(f"longer than {SMS_MAX} characters once the credit is filled in")
    return problems


def render(message: str, credit: float) -> str:
    return message.replace(PLACEHOLDER, f"${credit:.2f}")
