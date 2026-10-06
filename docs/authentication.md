# Authentication: app service principal vs. on behalf of the user

Every Databricks App can call Databricks APIs as one of two identities. Picking the right one is the most important design decision for an app that reads data.

| | App service principal | On behalf of the user (OBO) |
|---|---|---|
| Who the call runs as | The app's own service principal, the same for every user | The signed-in user |
| What data users see | Everything the service principal can see | Only what each user is entitled to in Unity Catalog |
| Credentials | `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`, injected automatically | `x-forwarded-access-token` request header |
| Setup | Declare resources; grant the service principal data access | Add user API scopes to the app |
| Works locally? | Yes (your CLI profile stands in for the service principal) | No forwarded header locally; test once deployed |
| Templates | Most templates | `*-data-app-obo-user`, `streamlit-group-access-app` |

**Rule of thumb:** use the service principal when every user should see the same data (shared dashboards, a team todo list, a chatbot), or for background work. Use OBO when different users must see different rows or tables, so Unity Catalog stays the single place where access is decided.

You can mix both in one app: for example, read a shared config table as the service principal and query sales data as the user.

## App service principal

Each app gets a dedicated service principal. Databricks injects its OAuth credentials, and the SDKs pick them up with no code:

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

w = WorkspaceClient()   # authenticates as the app's service principal
cfg = Config()          # same credentials, for connectors that need them
```

```python
from databricks import sql

conn = sql.connect(
    server_hostname=cfg.host,
    http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_WAREHOUSE_ID']}",
    credentials_provider=lambda: cfg.authenticate,
)
```

**Declaring a resource is not the same as granting data access.** A declared SQL warehouse gets `CAN_USE`, which lets the service principal run queries, but the tables it queries need their own Unity Catalog grants. Find the service principal on the app's **Authorization** tab, then:

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<app-service-principal-id>`;
GRANT USE SCHEMA ON SCHEMA <catalog>.<schema> TO `<app-service-principal-id>`;
GRANT SELECT ON TABLE <catalog>.<schema>.<table> TO `<app-service-principal-id>`;
```

See [resources-and-permissions.md](resources-and-permissions.md) for what each resource type grants.

## On behalf of the user

When user authorization is configured, Databricks forwards the signed-in user's token to the app in the `x-forwarded-access-token` header. Calls made with that token are limited by both the user's own permissions **and** the scopes the app declared.

### 1. Declare scopes

In `databricks.yml` (or *Add scope* in the app's UI settings):

```yaml
resources:
  apps:
    app:
      user_api_scopes:
        - sql
```

Current scope names ([docs](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth)):

| Scope | Allows |
|---|---|
| `sql` | Query SQL warehouses |
| `sql:restricted-query` | Read-only SQL queries |
| `genie` | Genie spaces |
| `files` | Files and directories (UC volumes) |
| `model-serving` | Serving endpoints |
| `vector-search` | Vector Search indexes |
| `postgres` | Lakebase |
| `apps`, `ai-functions`, `ai-gateway` | Apps, AI Functions, AI Gateway |
| `catalog.catalogs`, `catalog.schemas`, `catalog.tables`, `catalog.connections`, `workspace.workspace` | SDK access to those objects (add `:read` for read-only) |
| `iam.current-user:read`, `iam.access-control:read` | Granted by default; who the user is and their permissions, no data access |

**Renamed scopes.** Older names are deprecated: `dashboards.genie` → `genie`, `files.files` → `files`, `serving.serving-endpoints` → `model-serving`, and the `sql.*` variants → `sql`. Several AppKit and showcase templates in this repo still use the old names; prefer the new ones in new apps.

Workspace admins can restrict which scopes apps may request. If deploying fails with `user token passthrough not enabled`, user authorization isn't enabled for apps in that workspace; ask an admin.

### 2. Read the token

| Framework | Code |
|---|---|
| Streamlit | `st.context.headers.get("x-forwarded-access-token")` |
| Flask / Dash | `flask.request.headers.get("x-forwarded-access-token")` |
| Gradio | `request.headers.get("x-forwarded-access-token")` (declare a `request: gr.Request` parameter) |
| Shiny | `session.http_conn.headers.get("x-forwarded-access-token")` |
| FastAPI | `request.headers.get("x-forwarded-access-token")` |

### 3. Call APIs with it

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

# auth_type="pat" stops the SDK from also picking up the service principal's env vars
user_client = WorkspaceClient(host=Config().host, token=user_token, auth_type="pat")
me = user_client.current_user.me()
```

```python
conn = sql.connect(
    server_hostname=Config().host,
    http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_WAREHOUSE_ID']}",
    access_token=user_token,
)
```

### Things that catch people out

- **Scopes, not just permissions.** A user who can query a table still gets an error if the app didn't declare `sql`.
- **The token only works in the app's workspace.**
- **Streamlit reads headers once per session**, then switches to a WebSocket, so on a tab left open for a long time the token can expire. Reloading the page gets a fresh one.
- **Redeploys can drop scopes.** Updating an app's resources replaces its configuration; check the scopes are still set after deploying.
- **Hiding UI is not access control.** If you show or hide features by group (see `streamlit-group-access-app`), enforce the same rule wherever data is read or written, ideally with Unity Catalog grants.
- **Don't cache user data across users.** A cache keyed only by query text will serve one user's rows to another. Key caches by user (or by token), or don't cache OBO results.

## Local development

Locally there is no service principal and no forwarded header. Authenticate the CLI and point the SDK at that profile:

```bash
databricks auth login --host https://<workspace-url> --profile <profile>
export DATABRICKS_CONFIG_PROFILE=<profile>
```

`WorkspaceClient()` and `Config()` then use your own identity. Service-principal code paths work (as you, not the service principal); OBO code paths need a deployed app.
