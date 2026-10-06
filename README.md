# Databricks App Templates

Pre-built templates for creating [Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/).

See [Create an App from a Template](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/create-app-template) to get started.

## Which template should I pick?

**1. What are you building?**

| Goal | Start from |
|------|-----------|
| Learn the platform / minimal starting point | a `*-hello-world-app` |
| Dashboard over Unity Catalog tables | `appkit-analytics` (TypeScript) or a `*-data-app` (Python) |
| CRUD app with its own database | `appkit-lakebase` (TypeScript) or a `*-postgres-app` (Python, Lakebase Autoscaling) |
| Chat UI over a model or agent endpoint | `e2e-chatbot-app-next` (full-featured) or a `*-chatbot-app` (minimal Python) |
| Agent with tools, memory, evaluation | `agent-langgraph` or `agent-openai-agents-sdk` (add `-advanced` for memory) |
| Natural-language questions over data | `appkit-genie` |
| Trigger and monitor a Lakeflow Job | `streamlit-jobs-app` |
| Upload / download files in a UC volume | `appkit-files` (TypeScript) or `streamlit-files-app` (Python) |
| Semantic search over documents | `streamlit-vector-search-app` |
| Sub-second lookups / typeahead over lakehouse data | `streamlit-synced-table-app` (Lakebase synced table) |
| Show features by user group | `streamlit-group-access-app` |
| Expose tools to AI clients | `mcp-server-hello-world` |
| See a complete reference solution | a [showcase example](#showcase-examples) |

**2. Python or TypeScript?** AppKit (`appkit-*`) is the recommended TypeScript/React stack: typed SQL, built-in charts, plugins for Genie/Lakebase/Serving. The Python templates come in Streamlit, Dash, Gradio, Shiny and Flask flavors; pick the framework your team already knows; they cover the same use cases.

**3. Whose permissions should queries use?**

- **App service principal** (default): every user sees the same data; you grant the app's service principal access once.
- **On behalf of the user** (`*-obo-user` templates, `user_api_scopes`): queries run with each signed-in user's Unity Catalog permissions. Use this when users must only see data they are entitled to.

**4. Lakebase Provisioned or Autoscaling?** New apps should use Autoscaling (`*-postgres-app`, `appkit-lakebase`). The `*-database-app` templates target Lakebase Provisioned instances.

Every Python template's README covers its resources, permissions, local run and deploy steps, and each ships a `databricks.yml` for CLI/CI deploys (`databricks bundle deploy -t dev`).

## Guides

Cross-cutting guides for building and running apps, whichever template you start from:

- [Authentication](docs/authentication.md): app service principal vs. on behalf of the user, scopes, code per framework
- [Resources and permissions](docs/resources-and-permissions.md): what each resource grants, what's injected, what you still have to grant
- [Production checklist](docs/production-checklist.md): configuration, dependencies, runtime behavior, sizing, cost, limits, monitoring, CI/CD
- [Troubleshooting](docs/troubleshooting.md): symptoms, causes and fixes

## Templates

### Hello World

| Template | Description | Dependencies |
|----------|-------------|--------------|
| `streamlit-hello-world-app` | Simple Streamlit app | None |
| `dash-hello-world-app` | Simple Dash app | None |
| `gradio-hello-world-app` | Simple Gradio app | None |
| `shiny-hello-world-app` | Simple Shiny app | None |
| `flask-hello-world-app` | Simple Flask app | None |
| `nodejs-fastapi-hello-world-app` | Simple Node.js app | None |

### Agents

| Template | Description | Dependencies |
|----------|-------------|--------------|
| `agent-langgraph` | A conversational agent using LangGraph and MLflow AgentServer | MLflow experiment |
| `agent-langgraph-advanced` | LangGraph agent with short-term memory, long-term memory, and long-running background tasks | MLflow experiment, Database |
| `agent-openai-agents-sdk` | A conversational agent using OpenAI Agents SDK and MLflow AgentServer | MLflow experiment |
| `agent-openai-advanced` | OpenAI Agents SDK agent with short-term memory and long-running background tasks | MLflow experiment, Database |
| `agent-openai-agents-sdk-multiagent` | Multi-agent orchestrator using OpenAI Agents SDK with Genie and serving endpoint subagents | MLflow experiment |
| `agent-non-conversational` | A non-conversational agent that processes structured questions and provides answers with detailed reasoning | MLflow experiment |
| `agent-migration-from-model-serving` | Template for migrating a ResponsesAgent from Model Serving to Databricks Apps | MLflow experiment |
| `e2e-chatbot-app-next` | A chat UI that queries a remote agent endpoint or foundation model | Serving endpoint |
| `mcp-server-hello-world` | A basic MCP server | None |
| `mcp-server-open-api-spec` | An MCP server that exposes REST API operations from an OpenAPI specification stored in a Unity Catalog volume | UC volume |

### Dashboard

| Template | Description | Dependencies |
|----------|-------------|--------------|
| `streamlit-data-app` | An app that reads from a SQL warehouse and visualizes data | SQL warehouse |
| `dash-data-app` | An app that reads from a SQL warehouse and visualizes data | SQL warehouse |
| `gradio-data-app` | An app that reads from a SQL warehouse and visualizes data | SQL warehouse |
| `shiny-data-app` | An app that reads from a SQL warehouse and visualizes data | SQL warehouse |
| `streamlit-data-app-obo-user` | Same as `streamlit-data-app`, but queries run with the signed-in user's permissions | SQL warehouse, user API scope `sql` |
| `dash-data-app-obo-user` | Same as `dash-data-app`, but queries run with the signed-in user's permissions | SQL warehouse, user API scope `sql` |
| `gradio-data-app-obo-user` | Same as `gradio-data-app`, but queries run with the signed-in user's permissions | SQL warehouse, user API scope `sql` |
| `shiny-data-app-obo-user` | Same as `shiny-data-app`, but queries run with the signed-in user's permissions | SQL warehouse, user API scope `sql` |

### Database

| Template | Description | Dependencies |
|----------|-------------|--------------|
| `streamlit-database-app` | A todo app that stores tasks in a Postgres database hosted on Databricks (Lakebase Provisioned) | Database |
| `dash-database-app` | A todo app that stores tasks in a Postgres database hosted on Databricks (Lakebase Provisioned) | Database |
| `flask-database-app` | A todo app that stores tasks in a Postgres database hosted on Databricks (Lakebase Provisioned) | Database |
| `streamlit-postgres-app` | A todo app that stores tasks in a Lakebase Autoscaling Postgres database | Database |
| `dash-postgres-app` | A todo app that stores tasks in a Lakebase Autoscaling Postgres database | Database |
| `flask-postgres-app` | A todo app that stores tasks in a Lakebase Autoscaling Postgres database | Database |

### Chatbot

| Template | Description | Dependencies |
|----------|-------------|--------------|
| `streamlit-chatbot-app` | A minimal chat UI for an LLM on Databricks Model Serving | Serving endpoint |
| `dash-chatbot-app` | A minimal chat UI for an LLM on Databricks Model Serving | Serving endpoint |
| `gradio-chatbot-app` | A minimal chat UI for an LLM on Databricks Model Serving | Serving endpoint |
| `shiny-chatbot-app` | A minimal chat UI for an LLM on Databricks Model Serving | Serving endpoint |
| `e2e-chatbot-app` | Earlier Streamlit chat UI for agent and foundation-model endpoints; prefer `e2e-chatbot-app-next` for new work | Serving endpoint |

### Integrations

Python examples of individual platform features, each in a single `app.py`.

| Template | Description | Dependencies |
|----------|-------------|--------------|
| `streamlit-jobs-app` | Trigger a Lakeflow Job with parameters, watch its runs, and read task output | Job |
| `streamlit-files-app` | Browse, upload, download, and delete files in a Unity Catalog volume | UC volume |
| `streamlit-vector-search-app` | Semantic search over a Databricks Vector Search index | Vector Search index |
| `streamlit-synced-table-app` | Sub-second typeahead search over a UC table synced into Lakebase | Database |
| `streamlit-group-access-app` | Show or hide features based on the signed-in user's workspace groups | User API scope `iam.current-user:read` |

### AppKit

A collection of templates for building full-stack Databricks Apps with [AppKit](https://github.com/databricks/appkit).

<!-- appkit-start -->

| Template | Description | Dependencies |
|----------|-------------|--------------|
| `appkit-all-in-one` | Full-stack Node.js app with SQL analytics dashboards, file browser, Genie AI conversations, Lakebase Autoscaling (Postgres) CRUD, and Model Serving | SQL warehouse, Volume, Genie Space, Database, Serving Endpoint |
| `appkit-analytics` | Node.js app with SQL analytics dashboards and charts | SQL warehouse |
| `appkit-genie` | Node.js app with AI/BI Genie for natural language data queries | Genie Space |
| `appkit-files` | Node.js app with file browser for Databricks Volumes | Volume |
| `appkit-serving` | Node.js app with Databricks Model Serving endpoint integration | Serving Endpoint |
| `appkit-lakebase` | Node.js app with Lakebase Autoscaling (Postgres) CRUD operations | Database |

<!-- appkit-end -->

### Showcase Examples

End-to-end example apps that bundle a full Databricks App with seed data, SQL queries, and (where applicable) Lakeflow pipelines and provisioning scripts. See each template's `README.md` for the runbook.

| Template | Description | Dependencies |
|----------|-------------|--------------|
| `agentic-support-console` | End-to-end AI-powered support console combining Lakebase, Lakehouse Sync, a medallion pipeline, an LLM agent job, reverse sync, and a Databricks App with Genie analytics. | SQL warehouse, Database, Genie Space, MLflow experiment |
| `city-311-operations` | City 311 operations: service-level dashboards, a mix-adjusted service-equity analysis across districts, and AI-assisted intake with PII redaction and multilingual resident updates. Synthetic data. | SQL warehouse, Serving endpoint |
| `content-moderator` | Internal content moderation tool with per-channel guidelines, AI-powered compliance scoring via Model Serving, and a moderator review workflow backed by Lakebase and Genie analytics. | SQL warehouse, Database, Genie Space, Serving endpoint |
| `inventory-intelligence` | Retail inventory management with AI-powered demand forecasting, replenishment recommendations, and optional Genie analytics. Built on a live medallion pipeline synced to Lakebase. | SQL warehouse, Database, Genie Space |
| `rag-chat` | Streaming Retrieval-Augmented Generation chat app with pgvector retrieval from Lakebase, Wikipedia seed corpus, Model Serving generation, and Lakebase-backed chat history. Consumed via `databricks apps init`. | Database, Serving endpoint |
| `saas-tracker` | Internal tool for tracking team SaaS subscriptions, owners, costs, and renewals with Lakebase persistence and Genie spend analytics. | SQL warehouse, Database, Genie Space |
| `retail-customer-assistant` | Customer-service agent for an online retailer with governed tools (orders, return eligibility, returns), Customer 360 and operations views. Synthetic data generated by a setup job. | SQL warehouse, Serving endpoint |
| `aml-alert-triage` | AML transaction monitoring: versioned detection rules, analyst investigation with AI-drafted SAR narratives, maker-checker approval and an append-only audit trail. Synthetic data with planted typologies. | SQL warehouse, Serving endpoint |
| `hls-clinical-explorer` | Cohort explorer, admission timeline and AI summaries over MIMIC-IV / MIMIC-III. Runs on the open MIMIC-IV demo; credentialed MIMIC (with notes) supported, with AI summaries gated by PhysioNet's data use terms. | SQL warehouse, Serving endpoint |
| `vacation-rentals` | Vacation rental ops dashboard with revenue analytics from a SQL Warehouse, a booking queue with Lakebase-backed flags and agent notes, and an embedded Genie chat panel. | SQL warehouse, Database, Genie Space |
