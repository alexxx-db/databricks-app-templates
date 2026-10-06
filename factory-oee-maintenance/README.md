# Factory OEE & maintenance

A plant operations console: OEE (overall equipment effectiveness) with a loss breakdown, predictive-maintenance alerts from machine sensor data, and AI-drafted maintenance work orders with safety and citation guardrails.

| | |
|---|---|
| Stack | Streamlit app, Unity Catalog tables, SQL warehouse, Model Serving, Lakeflow Job |
| Data | Synthetic plant: 3 lines, 12 machines, 60 days of hourly sensor readings and per-shift production; fictional equipment manuals |
| Runs as | App service principal; work orders record the signed-in planner |

## What it shows

**OEE.** Plant, line, machine and daily OEE with availability, performance and quality, plus where planned production time went: productive time, slow cycles, downtime, scrap and breakdowns (the parts add up to planned time). The math in `oee.py`:

- Availability = run time ÷ planned time (planned breaks excluded); performance = ideal cycle × units ÷ run time; quality = good ÷ total, weighted by ideal cycle time; OEE = A × P × Q = ideal cycle × good units ÷ planned time.
- **Totals are computed from summed minutes and units, never by averaging percentages.** A big machine at 90% and a small one at 20% average to 55%, but the plant is actually at about 84%; the tests check exactly this case.

**Asset health.** Each machine is compared with its own baseline (median and median absolute deviation over its first 14 days). An hour is "hot" when vibration is more than 4 robust standard deviations above baseline, and an **alert needs 12 hot hours in a row**. On the synthetic data:

| Machine | What happened | Result |
|---|---|---|
| M04, M06 | Bearing wear, then breakdown and repair | Alerted ~7 days before each breakdown |
| M11 | Bearing wear developing now | Active alert (~2.8× baseline) |
| M02 | Vibration dropped after planned maintenance | No alert (only rises alert) |
| M07 | One-hour sensor spike to 4× | No alert (not sustained) |

The app shows each machine's vibration against its baseline, alert episodes, the maintenance log, and how much warning an alert gave before a breakdown repair.

**AI-drafted work orders.** For a flagged machine, the model gets the alert evidence, maintenance history and that machine type's manual sections, and drafts a work order. Two deterministic guardrails run on every draft:

1. **Lockout/tagout is always first.** If the draft doesn't include it, the manual's lockout/tagout procedure is inserted at the top, and the planner is told.
2. **Citations must be real.** Any section ID not in that machine's manual (an invented reference) is flagged, and filing is blocked until it's fixed.

The planner edits the plan, sets priority (defaulted from alert status), and files it; the work order records who filed it.

## Deploy

Prerequisites: a SQL warehouse, an existing catalog you can create schemas in, and a chat model endpoint (default: `databricks-meta-llama-3-3-70b-instruct`).

```bash
cd factory-oee-maintenance
databricks bundle deploy -t dev --var sql_warehouse_id=<warehouse-id>
databricks bundle run setup -t dev   # generate data, create work_orders, grant the app access
databricks bundle run app -t dev     # start the app
```

Tables go to `main.plant_ops` by default. To change it, pass `--var catalog=... --var schema=...` **and** set `PLANT_SCHEMA` in `app.yaml` to match. The app's service principal gets read access to the schema and `MODIFY` on `work_orders` only.

## Using it with real data

- Land historian/IoT data into `sensor_readings` (machine, timestamp, vibration, temperature, current) and MES shift records into `shifts` (planned minutes excluding breaks, unplanned downtime with reason, total and scrap units), with each machine's ideal cycle time in `machines`.
- Detection runs live in SQL, which is fine for one plant. For a fleet, run `health.py`'s SQL as a scheduled job or streaming table (marked `ponytail:` in the code). Tune the thresholds (4 robust SDs, 12 hours) per asset class with your reliability engineers.
- Load your real manuals as sections (`manuals`: machine type, section ID, title, content). The guardrails depend on every machine type having a lockout/tagout section.
- The OEE definitions follow common practice; align them (especially what counts as planned downtime) with your plant's standard before comparing sites.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: OEE, asset health, work orders |
| `oee.py` | OEE and loss-breakdown SQL |
| `health.py` | Baselines, sustained-deviation alerts, episodes, current status |
| `workorder.py` | Prompt, context, lockout/tagout and citation guardrails |
| `setup/generate_data.py` | Synthetic plant with planted failure patterns; `work_orders` table; grants |
| `databricks.yml` | App, setup job, variables |
| `tests/test_local.py` | Local end-to-end tests (below) |

## Testing

The tests build the plant in local Spark (ANSI mode, like Databricks SQL). They check that both breakdowns are caught at least 3 days ahead, that only the degrading machines alert, that OEE = A × P × Q and the losses add up, the sums-not-averages roll-up, and both guardrails. Then they run the real `app.py`: a draft missing lockout/tagout gets it added and is filed with the planner's name, and a draft with an invented citation can't be filed. Alerting on single hot hours, averaging OEE percentages, or disabling the lockout guardrail each makes the tests fail. Needs Java 17+.

```bash
uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
```

More: [Resources and permissions](../docs/resources-and-permissions.md) · [Production checklist](../docs/production-checklist.md)
