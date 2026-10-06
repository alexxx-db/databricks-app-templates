# Clinical explorer (MIMIC-IV / MIMIC-III)

A clinical research app over the MIMIC critical-care databases: build cohorts by diagnosis, age and admission type, walk through one admission's diagnoses, ICU stays, labs and medications, and generate an AI summary of the admission.

It runs out of the box on the **open MIMIC-IV demo** (100 patients, no credentialing). Credentialed users can point the same app at the full MIMIC-IV or MIMIC-III, including clinical notes.

| | |
|---|---|
| Stack | Streamlit app, Unity Catalog tables, SQL warehouse, Model Serving, Lakeflow Job |
| Data | Downloaded or read at setup time; **no MIMIC data is stored in this repository** |
| Runs as | App service principal |

> **For research and education only, not clinical care.** MIMIC is de-identified and dates are shifted. AI summaries can be wrong; verify against the record.

## Datasets

The setup job loads one dataset family and normalizes it into the same tables (`patients`, `admissions`, `diagnoses`, `labs`, `prescriptions`, `icustays`, optional `notes`, and `dataset_info`), so the app works the same on any of them.

| `dataset` | Access | Source | Notes |
|---|---|---|---|
| `mimic-iv-demo` (default) | Open, [ODbL](https://physionet.org/content/mimic-iv-demo/view-license/2.2/) | Downloaded from PhysioNet | None (the demo has none) |
| `mimic-iii-demo` | Open, ODbL | Downloaded from PhysioNet | None |
| `mimic-iv` | [Credentialed](https://physionet.org/content/mimiciv/) | Your copy in a volume (`hosp/`, `icu/`) | MIMIC-IV-Note `discharge`/`radiology` via `notes_path` |
| `mimic-iii` | [Credentialed](https://physionet.org/content/mimiciii/) | Your copy in a volume | `NOTEEVENTS` from the same folder |

MIMIC-III and MIMIC-IV use different patient IDs, so notes always come from the same family as the structured data. Ages over 89 appear as `90+`.

## Deploy

Prerequisites: a SQL warehouse, an existing catalog you can create schemas and volumes in, and a chat model endpoint (default: `databricks-meta-llama-3-3-70b-instruct`). The setup job needs outbound access to `physionet.org` for the demos.

```bash
cd hls-clinical-explorer
databricks bundle deploy -t dev --var sql_warehouse_id=<warehouse-id>
databricks bundle run setup -t dev   # download + load the MIMIC-IV demo, grant the app read access
databricks bundle run app -t dev     # start the app
```

Tables go to `main.mimic_explorer` by default. To change it, pass `--var catalog=... --var schema=...` **and** set `HLS_SCHEMA` in `app.yaml` to match.

If the job can't reach PhysioNet, download the demo files yourself and upload them to `/Volumes/<catalog>/<schema>/raw/mimic-iv-demo/` (keeping the `hosp/` and `icu/` folders); the job skips files that are already there.

### Credentialed MIMIC

1. Get access on PhysioNet (credentialing, CITI training, signed data use agreement) and download the dataset.
2. Upload it to a Unity Catalog volume that **only credentialed users** can read, e.g. `/Volumes/<catalog>/<restricted_schema>/mimic/mimic-iv/3.1/`.
3. Load it into a schema with the same restriction:

   ```bash
   databricks bundle deploy -t dev --var sql_warehouse_id=<id> --var schema=<restricted_schema> \
     --var dataset=mimic-iv --var source_path=/Volumes/.../mimic-iv/3.1 \
     --var notes_path=/Volumes/.../mimic-iv-note/2.2/note
   databricks bundle run setup -t dev
   ```

4. Set `HLS_SCHEMA` in `app.yaml` and **limit who can use the app** (app permissions → *Can use*) to credentialed users. The app shows a reminder banner for credentialed data.

## AI summaries and the PhysioNet data use agreement

PhysioNet's credentialed data use agreement forbids sharing the data with third parties, including through APIs, and its [guidance on LLMs](https://physionet.org/news/post/llm-responsible-use) says online services may only be used if they guarantee **no data retention, no training on the data, and no human review**.

So the app:

- **Allows** AI summaries on the open demos.
- **Disables** AI summaries on credentialed data until you set `ALLOW_LLM_WITH_CREDENTIALED_DATA: "true"` in `app.yaml`. Only do that after confirming your model endpoint meets those terms; PhysioNet doesn't certify any service, and the responsibility is yours.
- **Shows exactly what will be sent** to the model before you generate a summary.

The default endpoint is a Databricks-hosted model. Check your workspace's terms for the endpoint you use, and avoid external model endpoints (which forward data to another provider) for credentialed data.

## What it shows

- **Cohort explorer**: patients, admissions, in-hospital mortality and median length of stay for the filtered cohort; age bands; most common diagnoses; open any admission.
- **Admission timeline**: diagnoses by priority, ICU stays, medications, lab trends for any lab, and clinical notes when loaded.
- **AI summary**: a structured context (diagnoses, ICU stays, abnormal labs with ranges and last values, medications, note excerpts) sent to the model with a prompt that forbids treatment recommendations and invented facts.

All queries are parameterized; filter text never becomes SQL.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: cohort explorer, admission timeline, AI summary |
| `summary.py` | Summary context, prompt, and the credentialed-data gate |
| `setup/load_mimic.py` | Downloads the demos / reads your copy, normalizes MIMIC-IV and MIMIC-III, grants access (Lakeflow Job task) |
| `databricks.yml` | App, setup job, variables |
| `app.yaml` | App command and env wiring |
| `tests/test_local.py` | Local end-to-end tests (below) |

## Testing

The tests download both open demos (~12 MB, cached), run the loader in local Spark (ANSI mode, like Databricks), then run the real `app.py` on the resulting tables with a fake model. They also check notes loading with synthetic note files (there are no open MIMIC notes) and that AI summaries stay off for credentialed data. Needs Java 17+.

```bash
uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
```

## Citations

If you use this with MIMIC, cite the datasets as PhysioNet requires:

- Johnson, A., Bulgarelli, L., Pollard, T., Horng, S., Celi, L. A., & Mark, R. (2023). MIMIC-IV Clinical Database Demo (version 2.2). PhysioNet. https://doi.org/10.13026/dp1f-ex47
- Johnson, A., Pollard, T., & Mark, R. (2019). MIMIC-III Clinical Database Demo (version 1.4). PhysioNet. https://doi.org/10.13026/C2HM2Q
- Johnson, A. E. W., Pollard, T. J., Shen, L., et al. (2016). MIMIC-III, a freely accessible critical care database. *Scientific Data*, 3, 160035.
- For full MIMIC-IV / MIMIC-IV-Note, use the citations on their PhysioNet pages, plus the standard PhysioNet citation.

The demos are licensed under the Open Database License: tables you derive from them and share publicly must also be offered under the ODbL, with attribution.

More: [Authentication](../docs/authentication.md) · [Resources and permissions](../docs/resources-and-permissions.md) · [Production checklist](../docs/production-checklist.md)
