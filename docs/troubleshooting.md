# Troubleshooting

Start with the logs: the app's **Logs** tab, `<app-url>/logz`, or `databricks apps logs <app-name> --profile <profile>` (needs an OAuth login, not a PAT). **Build** entries show dependency installation; **App** entries show your process's stdout/stderr.

## The app won't start or shows 502

| Symptom | Cause | Fix |
|---|---|---|
| Brief 502 right after deploy | The app is still starting | Wait a minute; Streamlit apps commonly show this |
| 502 that never clears, logs show the server running | Listening on the wrong host/port | Bind `0.0.0.0:$DATABRICKS_APP_PORT`. Don't hardcode `8000`/`8080` in a custom server |
| Build log shows `pip` resolution errors | Conflicting or yanked dependency versions | Reproduce locally: `uv pip compile --python-version 3.11 requirements.txt` |
| App crashed after a redeploy with no code change | An unbounded dependency picked up a breaking release | Bound the version (`<next-major`); see the two cases below |
| Shiny app: `TypeError: Expected a fully tagified value` | `htmltools` 0.7 with shiny 1.1 | Add `htmltools<0.7` |
| Gradio 4.44: works on one instance, 502 on two | `starlette>=0.38` changed `TemplateResponse` | Pin `starlette==0.37.2` |
| Starts, then hangs; never serves | Calling a Databricks API at import time (endpoint lookup, query, auth) that's slow or failing | Move it to first request / `@st.cache_resource`, and check that resource's permission |
| `File is larger than 10485760 bytes` | A vendored package or data file in the source | Install from `requirements.txt` / `package.json`; keep data in volumes |
| `ModuleNotFoundError` only when deployed | Package missing from `requirements.txt` (installed locally by hand) | Add it; test in a fresh venv: `uv run --isolated --with-requirements requirements.txt ...` |

## Permission errors

| Symptom | Cause | Fix |
|---|---|---|
| `PERMISSION_DENIED` calling an API | Resource not declared or permission too weak | Declare it in `databricks.yml` with the right permission and redeploy |
| `INSUFFICIENT_PRIVILEGES` / `TABLE_OR_VIEW_NOT_FOUND` on a query | Service principal (or user, with OBO) lacks Unity Catalog grants | `GRANT USE CATALOG / USE SCHEMA / SELECT` (see [resources-and-permissions.md](resources-and-permissions.md)) |
| Postgres `permission denied for table` | Table owned by another role (e.g. a synced table) | `GRANT SELECT` to the app service principal's Postgres role |
| `does not have required scopes` | OBO scope missing | Add it to `user_api_scopes`; use current names (`genie`, `files`, `model-serving`) |
| `user token passthrough not enabled` | User authorization isn't enabled for apps in this workspace | Ask a workspace admin |
| OBO worked, then broke after a redeploy | Updating resources replaced the app config and dropped scopes | Check scopes after each deploy; keep them in `databricks.yml` |
| OBO token is `None` | Running locally (no proxy), or scopes not configured | Test OBO paths on the deployed app |
| OBO calls start failing on a long-open Streamlit tab | Streamlit captured the token at session start and it expired | Reload the page |

## Deploys

| Symptom | Cause | Fix |
|---|---|---|
| `bundle deploy` succeeded but the app runs old code | `deploy` uploads code and config; it doesn't restart the app | Run `databricks bundle run app -t <target>` after deploying |
| `App already exists` on first bundle deploy | An app with that name was created in the UI | `databricks bundle deployment bind app <existing-app-name> -t <target>`, or rename |
| Name rejected | Too long or invalid characters | Lowercase letters, numbers, hyphens; 26 characters or fewer |
| `${var.xxx}` shows up literally in an env var | Bundle variables used in `app.yaml` | Bundle variables resolve in `databricks.yml` only; use `valueFrom` in `app.yaml` |
| Required variable missing | No value for a `databricks.yml` variable | `--var name=value`, `BUNDLE_VAR_name` env var, or a target default |

## Runtime

| Symptom | Cause | Fix |
|---|---|---|
| `504` on long requests, nothing in app logs | The proxy cut the request off (~120 s) | Run long work in a Job or background task and poll for the result |
| Streamlit freezes for minutes after a few interactions | A new SQL connection on every rerun | Create connections once with `@st.cache_resource` |
| Users see each other's data | A cache shared across users with OBO | Key caches by user, or don't cache OBO results |
| Files written by the app disappear | Local disk is temporary | Write to a UC volume (see `streamlit-files-app`) |
| Logs gone after a restart | Logs aren't persisted | Ship logs to a table/volume or an APM tool |
| Flask flash messages / sessions lost across restarts or instances | No `SECRET_KEY`, so each process uses a random key | Set `SECRET_KEY` from a secret resource |
| Streaming responses arrive in chunks, not token by token | Proxy buffering | Expected; use WebSockets if smooth streaming matters |

## Still stuck

1. Reproduce locally against the same workspace (see each template's README, *Run locally*).
2. Run the template's smoke test: `cd .scripts/classic-app-tests && uv run pytest -k <template>`.
3. Compare with the unmodified template; resource and permission mismatches are the most common cause.
