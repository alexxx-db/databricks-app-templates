# Network care

A mobile operator's network-to-customer console: detect cell-site incidents from network KPIs, see which customers were affected and what they're owed under the credit policy, and send proactive SMS apologies drafted by AI, where the model never gets to state an amount.

| | |
|---|---|
| Stack | Streamlit app, Unity Catalog tables, SQL warehouse, Model Serving, Lakeflow Job |
| Data | Synthetic: 60 cell sites with 15-minute KPIs over 14 days, 6,000 customers, support tickets |
| Runs as | App service principal; queued outreach records the approving agent |

## What it shows

**Network.** Ongoing incidents (with an alert banner), a map of sites colored by incident status, the incident list, and per-site availability and dropped-call trends. Detection (`network.py`):

- A 15-minute interval is bad when availability < 95% or dropped calls > 2%.
- Consecutive bad intervals at a site are merged into one incident, and **only runs of 30+ minutes count**, so one-interval blips are ignored.
- OUTAGE if availability fell below 50%, otherwise DEGRADATION; incidents still in progress are marked ongoing.

On the synthetic data, the six planted incidents (four outages, two degradations, one ongoing) are found with exact start and end times, and none of the five planted blips are.

**Customer impact.** For any incident: affected customers (homed on that site), plan mix, who opened a ticket, total credits owed, and churn risk. Two rules in `care.py`, both illustrative:

- **Credit policy:** outages of 60+ minutes earn one day of service per started 4 hours; business plans get double (SLA); capped at half the monthly charge; degradations earn no automatic credit.
- **Churn risk:** HIGH if the customer was hit by 2+ incidents in 14 days (one site has an outage and a later degradation) or opened a ticket about this incident.

**Proactive outreach.** Pick an incident and audience (high churn risk, or everyone affected) and draft one SMS with AI:

1. The model is told the facts and must use a `{credit}` placeholder; it is **never given or allowed to write an amount**.
2. Every draft is checked: no money amounts of its own, the placeholder exactly once (or not at all when no credit applies), and at most 160 characters once the largest credit is filled in. A failing draft is replaced by a standard template, and the agent sees why.
3. Each customer's credit is filled in from the policy, the agent previews real messages, then approves. Queued messages record who approved them, and customers already contacted for that incident are skipped.

## Deploy

Prerequisites: a SQL warehouse, an existing catalog you can create schemas in, and a chat model endpoint (default: `databricks-meta-llama-3-3-70b-instruct`).

```bash
cd telco-network-care
databricks bundle deploy -t dev --var sql_warehouse_id=<warehouse-id>
databricks bundle run setup -t dev   # generate data, create outreach, grant the app access
databricks bundle run app -t dev     # start the app
```

Tables go to `main.network_care` by default. To change it, pass `--var catalog=... --var schema=...` **and** set `TELCO_SCHEMA` in `app.yaml` to match. The app's service principal gets read access to the schema and `MODIFY` on `outreach` only. Queued rows have status `QUEUED`; connect your SMS gateway to send them.

## Using it with real data

- Feed `kpis` from your performance-management system (site, interval, availability, dropped-call rate, users, throughput) and `customers.home_site_id` from your serving-cell or billing-address mapping. Real impact usually spans neighboring sites; extend `IMPACT_SQL` with a site-neighbor table if needed.
- Replace `credit_for()` with your actual credit policy and get it reviewed; the queued credit is what the customer is told.
- Thresholds (95% availability, 2% dropped calls, 2 intervals) are typical starting points; tune them per technology and region.
- SMS regulations (consent, quiet hours, opt-out wording) vary by country; add them to the template and validation before sending.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: network, customer impact, proactive outreach |
| `network.py` | Incident detection and impact SQL |
| `care.py` | Credit policy, churn risk, SMS prompt, validation, template |
| `setup/generate_data.py` | Synthetic network with planted incidents and blips; `outreach` table; grants |
| `databricks.yml` | App, setup job, variables |
| `tests/test_local.py` | Local end-to-end tests (below) |

## Testing

The tests build the network in local Spark (ANSI mode, like Databricks SQL) and check that detected incidents match the planted ones exactly, that repeat impact is counted, plus the credit policy, churn risk and SMS validation. Then they run the real `app.py`: a model draft that offers "$50 off" is rejected and replaced by the template, every queued SMS carries exactly that customer's policy credit within 160 characters, and nobody is contacted twice. Counting one-interval blips, dropping the business SLA, or letting drafts state amounts each makes the tests fail. Needs Java 17+.

```bash
uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
```

More: [Resources and permissions](../docs/resources-and-permissions.md) · [Production checklist](../docs/production-checklist.md)
