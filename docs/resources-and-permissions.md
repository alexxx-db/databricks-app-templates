# Resources and permissions

A **resource** is a Databricks object your app uses: a SQL warehouse, serving endpoint, job, volume, secret, database, and so on. Declaring it does two things on deploy:

1. **Grants** the app's service principal the permission you list on that object.
2. **Injects** an identifier for it into an environment variable (`valueFrom` in `app.yaml`), so no workspace-specific ID is hardcoded in code.

The person adding the resource needs **Can manage** on both the resource and the app.

```yaml
# app.yaml: wire a resource into an env var
env:
  - name: DATABRICKS_WAREHOUSE_ID
    valueFrom: sql-warehouse          # the resource key
```

```yaml
# databricks.yml: declare the resource and the permission to grant
resources:
  apps:
    app:
      resources:
        - name: sql-warehouse         # must match valueFrom
          sql_warehouse:
            id: ${var.sql_warehouse_id}
            permission: CAN_USE
```

## Matrix

| Resource | `databricks.yml` block | Permissions (least privilege first) | Injected via `valueFrom` | Example template |
|---|---|---|---|---|
| SQL warehouse | `sql_warehouse: {id}` | `CAN_USE`, `CAN_MANAGE`, `IS_OWNER` | Warehouse ID | `streamlit-data-app` |
| Serving endpoint | `serving_endpoint: {name}` | `CAN_QUERY`, `CAN_VIEW`, `CAN_MANAGE` | Endpoint name | `streamlit-chatbot-app` |
| Lakeflow Job | `job: {id}` | `CAN_VIEW`, `CAN_MANAGE_RUN`, `CAN_MANAGE`, `IS_OWNER` | Job ID | `streamlit-jobs-app` |
| UC volume | `uc_securable: {securable_full_name, securable_type: VOLUME}` | `READ_VOLUME`, `WRITE_VOLUME` | `/Volumes/<catalog>/<schema>/<volume>` path | `streamlit-files-app` |
| Vector Search index | `uc_securable: {securable_full_name, securable_type: TABLE}` | `SELECT` | Index full name* | `streamlit-vector-search-app` |
| UC table | `uc_securable: {..., securable_type: TABLE}` | `SELECT`, `MODIFY` | Table full name* | |
| UC function | `uc_securable: {..., securable_type: FUNCTION}` | `EXECUTE` | Function full name* | |
| UC connection | `uc_securable: {..., securable_type: CONNECTION}` | `USE_CONNECTION` | Connection name* | `mcp-server-open-api-spec` |
| Secret | `secret: {scope, key}` | `READ`, `WRITE`, `MANAGE` | The secret's value | |
| Lakebase Autoscaling | `postgres: {branch, database}` | `CAN_CONNECT_AND_CREATE` | Endpoint resource name (`projects/.../endpoints/...`); plus `PGHOST`, `PGDATABASE`, `PGUSER`, ... | `streamlit-postgres-app` |
| Lakebase Provisioned | `database: {instance_name, database_name}` | `CAN_CONNECT_AND_CREATE` | `PGHOST`, `PGDATABASE`, `PGUSER`, ... | `streamlit-database-app` |
| Genie space | `genie_space: {name, space_id}` | `CAN_VIEW`, `CAN_RUN`, `CAN_EDIT`, `CAN_MANAGE` | Space ID | `appkit-genie` |
| MLflow experiment | `experiment: {experiment_id}` | `CAN_READ`, `CAN_EDIT`, `CAN_MANAGE` | Experiment ID | `agent-langgraph` |
| Another app | `app: {name}` | `CAN_USE` | App name* | `agent-openai-agents-sdk-multiagent` |

\* Inferred from the resource's naming; confirm by printing the env var in a deployed app.

Permission names come from the bundle schema (`databricks bundle schema`). The [resources docs](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/resources) list the same set.

## What a resource does *not* grant

| You declared | The service principal can | It still needs |
|---|---|---|
| SQL warehouse (`CAN_USE`) | Run queries on the warehouse | `USE CATALOG`, `USE SCHEMA`, `SELECT` on every table it queries |
| Lakebase database (`CAN_CONNECT_AND_CREATE`) | Connect, and create schemas/tables it then owns | `GRANT SELECT` in Postgres on tables someone else created (e.g. synced tables) |
| Serving endpoint (`CAN_QUERY`) | Call the endpoint | Nothing more, unless the model itself calls other resources |
| Job (`CAN_MANAGE_RUN`) | Trigger and cancel runs | Nothing; the job runs as its own *run as* identity, not the app |

```sql
-- Unity Catalog (SQL editor)
GRANT USE CATALOG ON CATALOG <catalog> TO `<app-service-principal-id>`;
GRANT USE SCHEMA ON SCHEMA <catalog>.<schema> TO `<app-service-principal-id>`;
GRANT SELECT ON TABLE <catalog>.<schema>.<table> TO `<app-service-principal-id>`;

-- Lakebase (Postgres SQL editor, as the table owner)
GRANT USAGE ON SCHEMA <schema> TO "<app-service-principal-client-id>";
GRANT SELECT ON <schema>.<table> TO "<app-service-principal-client-id>";
```

With [on-behalf-of-user auth](authentication.md), user calls are checked against the **user's** permissions plus the app's scopes, so the per-table grants go to users or groups instead of the service principal.

## Secrets

Never put credentials in `app.yaml` `value:` fields or in code. Store them in a secret scope and inject them:

```bash
databricks secrets create-scope my-app
databricks secrets put-secret my-app api-key
```

```yaml
# databricks.yml
resources:
  - name: api-key
    secret:
      scope: my-app
      key: api-key
      permission: READ
```

```yaml
# app.yaml
env:
  - name: API_KEY
    valueFrom: api-key
```

The deploying user needs `MANAGE` on the scope.

## When permissions are wrong

| Symptom | Likely cause |
|---|---|
| `PERMISSION_DENIED` / `403` calling an API | Resource not declared, or declared with too weak a permission |
| Warehouse queries fail with `TABLE_OR_VIEW_NOT_FOUND` or `INSUFFICIENT_PRIVILEGES` | Missing Unity Catalog grants for the service principal (or the user, with OBO) |
| `permission denied for table` in Postgres | Table owned by another role; grant `SELECT` to the service principal's role |
| Deploy fails adding a resource | The deploying user lacks **Can manage** on that resource |
| `does not have required scopes` (OBO) | Add the scope to `user_api_scopes`, then redeploy |

More in [troubleshooting.md](troubleshooting.md).
