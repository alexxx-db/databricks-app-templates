import os

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient

# Defined in `app.yaml` from the `vector-search-index` resource (catalog.schema.index).
INDEX_NAME = os.getenv("VECTOR_SEARCH_INDEX", "")
assert INDEX_NAME, "VECTOR_SEARCH_INDEX must be set in app.yaml."


@st.cache_resource
def get_client() -> WorkspaceClient:
    # Runs as the app's service principal, which has SELECT on the index.
    return WorkspaceClient()


@st.cache_data(ttl=600)
def default_columns() -> list[str]:
    """Primary key + embedding source column(s), unless VECTOR_SEARCH_COLUMNS overrides."""
    if os.getenv("VECTOR_SEARCH_COLUMNS"):
        return [c.strip() for c in os.environ["VECTOR_SEARCH_COLUMNS"].split(",") if c.strip()]
    index = get_client().vector_search_indexes.get_index(INDEX_NAME)
    cols = [index.primary_key]
    spec = index.delta_sync_index_spec
    if spec and spec.embedding_source_columns:
        cols += [c.name for c in spec.embedding_source_columns]
    return cols


st.set_page_config(page_title="Vector Search app", layout="wide")
st.header("Semantic search")
st.caption(f"Index: `{INDEX_NAME}`")

query = st.text_input("Search", placeholder="Describe what you're looking for")
num_results = st.slider("Results", 1, 50, 10)

if query:
    columns = default_columns()
    # query_text needs a Delta Sync index with managed embeddings; for self-managed
    # embeddings, compute the vector yourself and pass query_vector instead.
    resp = get_client().vector_search_indexes.query_index(
        index_name=INDEX_NAME,
        columns=columns,
        query_text=query,
        num_results=num_results,
    )
    names = [c.name for c in resp.manifest.columns]  # requested columns + "score"
    rows = resp.result.data_array or []
    if rows:
        st.dataframe(pd.DataFrame(rows, columns=names), hide_index=True, use_container_width=True)
    else:
        st.info("No matches.")
