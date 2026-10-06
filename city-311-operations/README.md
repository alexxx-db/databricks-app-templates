# City 311 operations

A city's 311 service-request console: operations dashboards, a **service-equity** analysis that checks whether some districts are served slower than they should be, and AI-assisted intake that routes residents' messages and drafts plain-language updates in their language, with personal data removed before anything reaches the model.

| | |
|---|---|
| Stack | Streamlit app, Unity Catalog tables, SQL warehouse, Model Serving, Lakeflow Job |
| Data | Synthetic: a fictional city with 8 districts and ~21,000 service requests over 180 days |
| Runs as | App service principal |

## What it shows

**Operations.** Open requests, overdue backlog, on-time rate (resolved within each category's target days), weekly opened vs. closed, on-time by department, a map of open requests, and request lookup with a drafted resident update.

**Service equity.** Comparing districts on raw numbers misleads, because districts ask for different things: sidewalk repair has a 90-day target and a 60% on-time rate, a water main leak a 1-day target and 95%. The equity view compares each district's on-time rate with what it **would be at citywide performance for its own mix of requests**, and flags districts more than N points below that. The synthetic city contains both traps:

| District | Raw median days | vs. citywide on-time rate | vs. expected for its mix | Reality |
|---|---|---|---|---|
| Oak Hills (3) | slowest by far | ~11 pts below | about even | Asks mostly for slow categories; served on time |
| Eastside (7) | looks normal | ~18 pts below | ~20 pts below | Genuinely slower Streets & Sanitation service |

Requests still open and not yet due are left out (counting them as on time would flatter districts with recent backlogs); open requests past their target count as late. A flag is a prompt to investigate, not a conclusion. The method is in `equity.py` and explained in the app.

**AI-assisted intake.** Staff paste a resident's message. Before it goes to the model:

1. **Personal data is redacted** (phone numbers, emails, SSN-like and card-like numbers become `[PHONE]`, `[EMAIL]`, ...). Street addresses and dates are kept because routing needs them.
2. The model suggests a category **from the city's fixed list**, a priority, and the location. Anything off-list or malformed becomes "needs manual review".
3. Staff confirm or change it before filing. The filed request stores the **redacted** text; contact details belong in the CRM, not the work-order table.

**Language access.** For any request, staff can draft a short plain-language update (about a 6th-grade reading level) in English, Spanish, Chinese, Vietnamese or Tagalog. The update is built from structured facts only (category, dates, status), never the resident's free text. Drafts are marked as machine-generated for review, especially translations.

## Deploy

Prerequisites: a SQL warehouse, an existing catalog you can create schemas in, and a chat model endpoint (default: `databricks-meta-llama-3-3-70b-instruct`).

```bash
cd city-311-operations
databricks bundle deploy -t dev --var sql_warehouse_id=<warehouse-id>
databricks bundle run setup -t dev   # generate data, grant the app access
databricks bundle run app -t dev     # start the app
```

Tables go to `main.city_311` by default. To change it, pass `--var catalog=... --var schema=...` **and** set `CITY_SCHEMA` in `app.yaml` to match. The setup job grants the app's service principal read access to the schema and `MODIFY` on `requests` (for filing from the intake tab).

## Using it with real data

- Many cities publish 311 data as open data. Map it to the `requests` columns (`request_id`, `created_at`, `closed_at`, `status`, `category`, `department`, `district_id`, `lat`, `lon`, `channel`, `language`, `description`) and define `categories` (with your service-level targets) and `districts` (with populations).
- Equity analysis on real data deserves review with your performance and equity teams: check category definitions, how duplicates and reopened requests are handled, and whether district boundaries match how services are delivered.
- Regex redaction catches structured identifiers, not names. For production, add a PII guardrail on the model endpoint (AI Gateway) or a named-entity redaction step, and keep the "what was sent" view.
- Keep the model endpoint inside your workspace (a Databricks-hosted model) unless your data-handling rules allow otherwise, and check machine translations with qualified reviewers before relying on them for language-access obligations.
- Accessibility: status is shown as text, not color alone. Review the app against your accessibility requirements (for example WCAG 2.1 AA / Section 508) before publishing it.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: operations, service equity, intake |
| `equity.py` | The mix-adjusted equity metric (SQL) and its explanation |
| `triage.py` | PII redaction, routing prompt and output validation, resident-update prompt |
| `setup/generate_data.py` | Synthetic city data with the planted equity patterns, plus grants |
| `databricks.yml` | App, setup job, variables |
| `tests/test_local.py` | Local end-to-end tests (below) |

## Testing

The tests generate the city in local Spark (ANSI mode, like Databricks SQL) and check that the equity metric flags Eastside but not Oak Hills, by department too, plus a hand-computed case for the open-request rules. They check redaction and output validation, then run the real `app.py`: the equity flag renders, a phone number in a pasted message never reaches the model, the filed request stores redacted text, and a Spanish update is drafted. Removing the mix adjustment, counting open requests as on time, or disabling redaction each makes the tests fail. Needs Java 17+.

```bash
uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
```

More: [Authentication](../docs/authentication.md) · [Resources and permissions](../docs/resources-and-permissions.md) · [Production checklist](../docs/production-checklist.md)
