import os

import pandas as pd
import psycopg
import streamlit as st
from databricks import sdk
from psycopg import sql
from psycopg_pool import ConnectionPool

# Defined in `app.yaml`. The synced table is created from a Unity Catalog table
# (Catalog Explorer → table → Create → Synced table) into this Lakebase database.
ENDPOINT = os.getenv("PGENDPOINT", "")
SYNCED_TABLE = os.getenv("SYNCED_TABLE", "")
SEARCH_COLUMN = os.getenv("SEARCH_COLUMN", "")
assert SYNCED_TABLE and SEARCH_COLUMN, "SYNCED_TABLE and SEARCH_COLUMN must be set in app.yaml."


@st.cache_resource
def get_pool() -> ConnectionPool:
    workspace_client = sdk.WorkspaceClient()

    class OAuthConnection(psycopg.Connection):
        """Fetches a fresh Lakebase OAuth token for each new connection."""

        @classmethod
        def connect(cls, conninfo="", **kwargs):
            kwargs["password"] = workspace_client.postgres.generate_database_credential(
                endpoint=ENDPOINT
            ).token
            return super().connect(conninfo, **kwargs)

    conninfo = (
        f"dbname={os.getenv('PGDATABASE')} user={os.getenv('PGUSER')} "
        f"host={os.getenv('PGHOST')} port={os.getenv('PGPORT')} "
        f"sslmode={os.getenv('PGSSLMODE', 'require')} application_name={os.getenv('PGAPPNAME')}"
    )
    return ConnectionPool(conninfo, connection_class=OAuthConnection, min_size=1, max_size=10)


def search(prefix: str, limit: int) -> pd.DataFrame:
    schema, table = SYNCED_TABLE.split(".", 1)
    # Identifiers are quoted by psycopg; the user's text is a bound parameter.
    query = sql.SQL("SELECT * FROM {} WHERE {} ILIKE %s ORDER BY {} LIMIT %s").format(
        sql.Identifier(schema, table), sql.Identifier(SEARCH_COLUMN), sql.Identifier(SEARCH_COLUMN)
    )
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(query, (f"{prefix}%", limit))
        return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


st.set_page_config(page_title="Synced table lookup", layout="wide")
st.header("Lookup")
st.caption(f"`{SYNCED_TABLE}` · searching `{SEARCH_COLUMN}`")

prefix = st.text_input("Starts with", placeholder="Type to search")
limit = st.slider("Max rows", 5, 100, 20)

if prefix:
    df = search(prefix, limit)
    if df.empty:
        st.info("No matches.")
    else:
        st.dataframe(df, hide_index=True, use_container_width=True)
