"""Shared template configuration for sync scripts."""

TEMPLATES = {
    "agent-langgraph": {
        "sdk": "langgraph",
        "bundle_name": "agent_langgraph",
        "has_actions": True,
    },
    "agent-langgraph-advanced": {
        "sdk": "langgraph",
        "bundle_name": "agent_langgraph_advanced",
        "has_memory": True,
        "has_actions": True,
    },
    "agent-openai-agents-sdk": {
        "sdk": "openai",
        "bundle_name": "agent_openai_agents_sdk",
        "has_actions": True,
    },
    "agent-openai-agents-sdk-multiagent": {
        "sdk": "openai",
        "bundle_name": "agent_openai_agents_sdk_multiagent",
        "has_actions": True,
    },
    "agent-openai-advanced": {
        "sdk": "openai",
        "bundle_name": "agent_openai_advanced",
        "has_memory": True,
        "has_actions": True,
    },
    "agent-non-conversational": {
        "sdk": "langgraph",
        "bundle_name": "agent_non_conversational",
        "exclude_scripts": ["start_app.py", "evaluate_agent.py", "preflight.py"],
        "exclude_load_testing": True,
    },
    "agent-migration-from-model-serving": {
        "sdk": ["langgraph", "openai"],
        "bundle_name": "agent_migration",
    },
}

# Classic single-file Python templates (requirements.txt + app.yaml, no pyproject).
# Used by generate-classic-docs.py and classic-app-tests/.
CLASSIC_TEMPLATES = [
    "dash-chatbot-app",
    "dash-data-app",
    "dash-data-app-obo-user",
    "dash-database-app",
    "dash-hello-world-app",
    "dash-postgres-app",
    "e2e-chatbot-app",
    "flask-database-app",
    "flask-hello-world-app",
    "flask-postgres-app",
    "gradio-chatbot-app",
    "gradio-data-app",
    "gradio-data-app-obo-user",
    "gradio-hello-world-app",
    "nodejs-fastapi-hello-world-app",
    "shiny-chatbot-app",
    "shiny-data-app",
    "shiny-data-app-obo-user",
    "shiny-hello-world-app",
    "streamlit-chatbot-app",
    "streamlit-data-app",
    "streamlit-data-app-obo-user",
    "streamlit-database-app",
    "streamlit-files-app",
    "streamlit-group-access-app",
    "streamlit-hello-world-app",
    "streamlit-jobs-app",
    "streamlit-postgres-app",
    "streamlit-synced-table-app",
    "streamlit-vector-search-app",
]

# Python showcase apps with their own README/databricks.yml (not generated) and local Spark tests.
# Smoke-tested alongside CLASSIC_TEMPLATES; their own tests live in <template>/tests/.
PYTHON_SHOWCASES = [
    "aml-alert-triage",
    "city-311-operations",
    "factory-oee-maintenance",
    "hls-clinical-explorer",
    "retail-customer-assistant",
    "telco-network-care",
]
