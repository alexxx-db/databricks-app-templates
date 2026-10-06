# Production checklist

What to check before a Databricks App leaves the demo stage. Each item says why it matters; skip the ones that don't apply.

## Configuration

- [ ] **Deploy with a bundle, not by hand.** Every template here ships a `databricks.yml` with `dev` and `prod` targets. Set `targets.prod.workspace.host` so production deploys can't land in the wrong workspace.
- [ ] **No workspace IDs in code.** Warehouse IDs, endpoint names, volume paths and so on come in through `valueFrom` env vars, so the same code runs in dev and prod. See [resources-and-permissions.md](resources-and-permissions.md).
- [ ] **Secrets come from secret resources**, never `value:` fields or code.
- [ ] **Least-privilege permissions** on each resource (e.g. `CAN_USE` not `CAN_MANAGE` on a warehouse, `READ_VOLUME` if the app never writes).
- [ ] **Decide the identity model** (service principal vs. on behalf of the user) on purpose. See [authentication.md](authentication.md).

## Dependencies

- [ ] **Bound every dependency.** The runtime installs `requirements.txt` fresh on every deploy, so an unbounded `dash>=2.14` silently became dash 4.x. Use `~=` or an upper bound (`<5`). `pyproject.toml` with `uv` also works.
- [ ] **Test against the runtime's Python (3.11).** Node.js apps run on Node 22.
- [ ] **Run a smoke test in CI.** `.scripts/classic-app-tests/` resolves dependencies, boots each app and validates its bundle. It found two templates that broke because of upstream releases: shiny/`htmltools` and gradio/`starlette`.

## Runtime behavior

- [ ] **Listen on `0.0.0.0:$DATABRICKS_APP_PORT`.** Streamlit, Gradio, Flask, Uvicorn and Dash pick this up automatically from env vars the platform sets; custom servers must read it.
- [ ] **Start fast, without calling APIs at import time.** Nine templates in this repo call Databricks at import (endpoint lookup, a SQL query) and hang if that call is slow. Do setup lazily (`@st.cache_resource`, first request) and keep heavy initialization out of startup.
- [ ] **Shut down within 15 seconds of `SIGTERM`**, or the process is killed.
- [ ] **Log to stdout/stderr.** That's all that's captured, and logs are not kept after the app's compute stops.
- [ ] **Treat the local filesystem as temporary.** Persist files in UC volumes and state in Lakebase or tables.
- [ ] **Reuse connections.** Streamlit reruns the whole script on each interaction; create SQL/Postgres connections once with `@st.cache_resource`, or connection setup alone can freeze the app.
- [ ] **Keep requests short.** The app proxy cuts off long requests (about 120 seconds at the time of writing, not configurable). Move long work to a Lakeflow Job (see `streamlit-jobs-app`) or a background task, and poll for the result.
- [ ] **Do heavy work elsewhere.** Run queries on a SQL warehouse, batch work in Jobs, inference on Model Serving; the app's own compute is small.

## Sizing and scaling

| Size | vCPU | Memory | DBU/hour | Use for |
|---|---|---|---|---|
| Medium (default) | up to 2 | 6 GB | 0.5 | Most apps: dashboards, forms, chat UIs |
| Large | up to 4 | 12 GB | 1 | Large in-memory data, high concurrency |
| XLarge | up to 12 | 48 GB | 3 | Memory-heavy or very concurrent apps |

Set the size in the app's **Settings** tab or with `compute_size: LARGE` in `databricks.yml`. The bundle schema also has `compute_min_instances` / `compute_max_instances` for running several instances. Before scaling out:

- [ ] **No state that must be shared lives in memory.** Each instance has its own caches, sessions and in-process state. For example, the Flask templates generate a random session key per process when `SECRET_KEY` is unset; with more than one instance, set `SECRET_KEY` from a secret.
- [ ] **Test with two instances in dev.** Gradio 4.44 worked on one instance and returned 502 on two until `starlette` was pinned (commit `ab30424`).

## Cost

- [ ] **Apps are billed per hour while running**, by size, whether or not anyone uses them. **Stopped apps cost nothing** and keep their configuration. Stop dev apps you aren't using.
- [ ] **Track spend** in `system.billing.usage`, and app activity in `system.access.audit`.
- [ ] **Right-size before scaling out:** a Medium app idling all month costs a fraction of a Large one.

## Limits

| Limit | Value |
|---|---|
| Apps per workspace | 100 (fixed) |
| App name | Lowercase letters, numbers and hyphens; keep it to 26 characters or fewer, so a `dev-` prefix still fits in the 30-character maximum. It becomes part of the URL, which can't be changed later. |
| Single file in app source | 10 MB, so install dependencies from `requirements.txt` / `package.json`, don't vendor them |
| Startup | About 10 minutes, including dependency installation |
| Request duration | About 120 seconds at the proxy |
| System packages | None (no `apt-get`); PyPI and npm only |
| GPU | None; use Model Serving |

The app-count limit is from the [resource limits page](https://docs.databricks.com/aws/en/resources/limits); the others are platform behavior observed at the time of writing and may change.

## Monitoring

- [ ] **Know where logs are:** the app's **Logs** tab, `<app-url>/logz`, or `databricks apps logs <name>` (OAuth login required, a PAT won't work). Logs are grouped as App, System, Build and HTTP.
- [ ] **Ship logs you need to keep** to a UC table/volume or an APM tool (Datadog, New Relic); app telemetry to Unity Catalog is in Beta.
- [ ] **Add a cheap health route** (e.g. `/healthz` returning 200 without calling Databricks) so a monitor can tell "app down" from "Databricks call slow".

## CI/CD

Deploy from GitHub Actions with workload identity federation (OIDC), so no long-lived secret sits in GitHub. Setup: create a service principal, add a [GitHub federation policy](https://docs.databricks.com/aws/en/dev-tools/auth/provider-github) for it, create a `prod` environment in the repo with `DATABRICKS_HOST` and `DATABRICKS_CLIENT_ID` variables, and give the service principal **Can manage** on the app and its resources.

```yaml
# .github/workflows/deploy-<app>.yml
name: Deploy <app>
on:
  workflow_dispatch:
  push:
    branches: [main]
    paths: ["<app-dir>/**"]

permissions:
  id-token: write   # OIDC
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: prod
    defaults:
      run:
        working-directory: <app-dir>
    env:
      DATABRICKS_AUTH_TYPE: github-oidc
      DATABRICKS_HOST: ${{ vars.DATABRICKS_HOST }}
      DATABRICKS_CLIENT_ID: ${{ vars.DATABRICKS_CLIENT_ID }}
      # One BUNDLE_VAR_<name> per variable in databricks.yml
      BUNDLE_VAR_sql_warehouse_id: ${{ vars.SQL_WAREHOUSE_ID }}
    steps:
      - uses: actions/checkout@v4
      - uses: databricks/setup-cli@main
      - run: databricks bundle validate -t prod
      - run: databricks bundle deploy -t prod
      # deploy uploads code and config but does not restart the app
      - run: databricks bundle run app -t prod
```

The agent templates ship a version of this workflow (`.github/workflows/deploy.yml`). Deploy on every push only once a staging target or manual approval (GitHub environment protection rules) is in place.

**Rollback:** redeploy a previous deployment from the app's **Deployments** tab, or revert the commit and let CI redeploy.
