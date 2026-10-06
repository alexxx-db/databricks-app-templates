import io
import os
import posixpath

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient

# Defined in `app.yaml` from the `volume` resource, e.g. /Volumes/main/default/uploads
VOLUME_PATH = os.getenv("DATABRICKS_VOLUME_PATH", "").rstrip("/")
assert VOLUME_PATH.startswith("/Volumes/"), "DATABRICKS_VOLUME_PATH must be set in app.yaml."


@st.cache_resource
def get_client() -> WorkspaceClient:
    # Runs as the app's service principal, which has WRITE_VOLUME on the volume.
    return WorkspaceClient()


def safe_join(directory: str, name: str) -> str:
    """Join a user-supplied name to a directory without letting it escape the volume."""
    path = posixpath.normpath(posixpath.join(directory, name))
    if path != VOLUME_PATH and not path.startswith(VOLUME_PATH + "/"):
        raise ValueError(f"{name!r} is outside the volume")
    return path


st.set_page_config(page_title="Files app", layout="wide")
w = get_client()

st.header("Files")
subdir = st.text_input("Folder", value="", placeholder="optional/sub/folder")
try:
    directory = safe_join(VOLUME_PATH, subdir)
except ValueError as e:
    st.error(str(e))
    st.stop()
st.caption(f"`{directory}`")

# --- Upload ------------------------------------------------------------------
uploaded = st.file_uploader("Upload files", accept_multiple_files=True)
if uploaded and st.button("Upload", type="primary"):
    for f in uploaded:
        w.files.upload(safe_join(directory, f.name), io.BytesIO(f.getvalue()), overwrite=True)
    st.success(f"Uploaded {len(uploaded)} file(s)")

# --- Browse ------------------------------------------------------------------
try:
    entries = list(w.files.list_directory_contents(directory))
except Exception as e:  # e.g. folder does not exist yet
    st.info(f"Nothing to list: {e}")
    st.stop()

if not entries:
    st.info("This folder is empty.")
    st.stop()

st.dataframe(
    pd.DataFrame(
        [
            {
                "name": e.name + ("/" if e.is_directory else ""),
                "size (bytes)": e.file_size,
                "modified": pd.to_datetime(e.last_modified, unit="ms") if e.last_modified else None,
            }
            for e in entries
        ]
    ),
    hide_index=True,
    use_container_width=True,
)

# --- Download / delete -------------------------------------------------------
files = [e for e in entries if not e.is_directory]
if files:
    name = st.selectbox("File", [e.name for e in files])
    path = safe_join(directory, name)
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Prepare download"):
            st.download_button(
                f"Download {name}", w.files.download(path).contents.read(), file_name=name
            )
    with col2:
        confirm = st.checkbox(f"Yes, permanently delete {name}")
        if st.button("Delete", type="secondary", disabled=not confirm):
            w.files.delete(path)
            st.success(f"Deleted {name}")
            st.rerun()
