# AML alert triage

Anti-money-laundering transaction monitoring with an analyst workflow: deterministic rules flag suspicious activity, analysts investigate and decide, a second reviewer approves suspicious activity report (SAR) filings, and every decision lands in an append-only audit trail. An LLM drafts SAR narratives; people make every decision.

| | |
|---|---|
| Stack | Streamlit app, Unity Catalog (Delta) tables, SQL warehouse, Model Serving, Lakeflow Jobs |
| Data | Synthetic: 1,000 customers, ~150k transactions over 180 days, with planted laundering patterns and benign look-alikes |
| Runs as | App service principal; decisions are attributed to the signed-in user |

## Why it's built this way

Banks have to be able to explain every alert and every decision to auditors and regulators. So:

| Requirement | How the showcase meets it |
|---|---|
| Explainable detection | Five versioned rules in plain Spark SQL (`setup/detect.py`). Each alert records the rule, version, score and the exact transactions that triggered it. The rule catalog (with SQL) is visible in the app. |
| AI assists, people decide | The LLM only drafts SAR narratives, on request, from the evidence shown on screen. It never flags, scores, or closes anything. Drafts are editable and marked as AI-generated, and whether one was used is recorded with the decision. |
| Four-eyes principle | Escalations need approval from a **different** person (maker-checker), enforced in `investigation.py`, not just in the UI. |
| Tamper-evident audit trail | Decisions go to `alert_dispositions`, created with `delta.appendOnly = true`, so rows can't be updated or deleted (a table owner could turn that off, but the change shows in the table's Delta history). Alert status is always derived from that log, never stored separately. The app's service principal can only append to it. |
| Accountability | Each decision records who (the signed-in user's email from the `X-Forwarded-Email` header set by the Databricks Apps proxy), when, the action, and a mandatory rationale. Without a user identity, decisions are disabled. |
| Program oversight | Rule performance (SAR rate from decisions), open-alert aging against an SLA, AI-draft usage. |

## Detection rules

| Rule | Flags | Severity |
|---|---|---|
| `STRUCTURING` | 3+ cash deposits of $8,000–$9,999.99 within 7 days (just under the $10,000 CTR threshold) | 70 |
| `RAPID_MOVEMENT` | Incoming wire of $20,000+ with 80%+ sent out again within 48 hours | 85 |
| `HIGH_RISK_JURISDICTION` | $10,000+ in wires to/from high-risk jurisdictions (illustrative list) | 60 |
| `DORMANT_REACTIVATION` | No activity for 60+ days, then $25,000+ moved within 7 days | 65 |
| `ROUND_AMOUNTS` | 5+ outgoing transfers of exact round thousands ($5,000+) within 30 days | 40 |

Score = severity × customer risk (×1.4 high, ×1.15 medium) × 1.3 for politically exposed persons, capped at 100.

On the synthetic data every planted pattern is caught, and benign look-alikes (a cash-heavy business, round-amount supplier payments, legitimate trade with a high-risk country) produce the false positives. Real programs see far lower precision; most production AML alerts are false positives, which is why the rule performance view exists.

## Workflow

```
OPEN ──escalate──▶ ESCALATED ──approve (different person)──▶ SAR_APPROVED
  │  ▲                  │
  │  └────return────────┘   (RETURNED behaves like OPEN)
  ├──close: false positive──▶ CLOSED_FALSE_POSITIVE
  └──close: explained───────▶ CLOSED_EXPLAINED
```

Every action needs a rationale of at least 20 characters. The setup job seeds a plausible decision history for ~60% of alerts (respecting the same rules) so the metrics aren't empty; turn it off with `--seed-history false`.

## Deploy

Prerequisites: a SQL warehouse, an existing catalog you can create schemas in, and a chat model endpoint (default: `databricks-meta-llama-3-3-70b-instruct`).

```bash
cd aml-alert-triage
databricks bundle deploy -t dev --var sql_warehouse_id=<warehouse-id>
databricks bundle run setup -t dev   # generate data -> run detection -> grant the app access
databricks bundle run app -t dev     # start the app
```

Tables go to `main.aml_monitoring` by default. To change it, pass `--var catalog=... --var schema=...` **and** set `AML_SCHEMA` in `app.yaml` to match. The setup job grants the app's service principal read access to the schema and `MODIFY` on `alert_dispositions` only.

The `nightly_detection` job shows how detection runs in production: it re-scans transactions on a schedule (paused by default). Alert IDs are stable per customer and rule, so reruns keep their decision history.

To try maker-checker, open the app as two different users: one escalates, the other approves.

## Using it with real data

- Replace `setup/generate_data.py` with your transaction feed (same `customers`, `accounts`, `transactions` columns, or adapt the rule SQL) and delete `ground_truth`.
- Replace `HIGH_RISK_COUNTRIES` with your institution's jurisdiction-risk list, and calibrate thresholds with your compliance team; these are illustrative.
- Restrict who can use the app to the investigations team, and gate SAR approval to supervisors (see `streamlit-group-access-app` for group-based checks).
- Customer data sent for narrative drafts stays in your workspace only if the model endpoint does; prefer a Databricks-hosted model, and check your data-handling requirements before using an external model endpoint.
- Known simplifications (marked `ponytail:` in code): one alert per customer per rule, and an optimistic check-then-append when recording decisions.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: alert queue, investigation, program metrics |
| `investigation.py` | Workflow state machine, maker-checker, decision recording, SAR prompt and case context |
| `setup/generate_data.py` | Synthetic customers, accounts and transactions with planted typologies |
| `setup/detect.py` | Detection rules, scoring, rule catalog, audit-trail table, seeded history, grants |
| `databricks.yml` | App, setup job, nightly detection job, variables |
| `tests/test_local.py` | Local end-to-end tests (below) |

## Testing

The tests generate the data, run detection in local Spark (ANSI mode, like Databricks SQL), and check that every planted pattern is caught and every evidence transaction belongs to the alerted customer. They check the workflow and maker-checker rules (including replaying the seeded history through them), the audit log, and the real `app.py`: an analyst escalates with an AI draft, can't approve their own escalation, and a second reviewer approves. Needs Java 17+.

```bash
uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
```

More: [Authentication](../docs/authentication.md) · [Resources and permissions](../docs/resources-and-permissions.md) · [Production checklist](../docs/production-checklist.md)
