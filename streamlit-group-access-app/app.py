import os
import re

import streamlit as st
from databricks.sdk import WorkspaceClient

# Defined in `app.yaml`.
ADMIN_GROUP = os.getenv("ADMIN_GROUP", "admins")


@st.cache_resource
def get_client() -> WorkspaceClient:
    return WorkspaceClient()  # the app's service principal


@st.cache_data(ttl=300, show_spinner=False)
def user_groups(email: str) -> list[str]:
    """The signed-in user's workspace groups, looked up by the app's service principal (SCIM)."""
    if not re.fullmatch(r"[^\s\"\\]+@[^\s\"\\]+", email):  # never pass odd input into the SCIM filter
        return []
    users = list(get_client().users.list(filter=f'userName eq "{email}"', attributes="userName,groups"))
    return sorted(g.display for g in (users[0].groups or []) if g.display) if users else []


st.set_page_config(page_title="Group access", layout="wide")
st.header("Group access")

# The Databricks Apps proxy sets this header to the signed-in user's email on every request.
email = st.context.headers.get("X-Forwarded-Email") or os.getenv("LOCAL_USER_EMAIL", "")
if not email:
    st.warning("No signed-in user. When running locally, set LOCAL_USER_EMAIL to try a user's groups.")
    st.stop()

groups = user_groups(email)
is_admin = ADMIN_GROUP in groups

st.write(f"Signed in as **{email}**")
st.write("Groups: " + (", ".join(f"`{g}`" for g in groups) or "none"))

st.subheader("Everyone")
st.write("Content every signed-in user can see.")

st.subheader("Admins")
if is_admin:
    st.success(f"You are in `{ADMIN_GROUP}`, so you see this section.")
    st.write("Put admin-only controls here.")
else:
    st.info(f"Only members of `{ADMIN_GROUP}` can see this section.")

# Hiding UI is not access control: enforce the same rule wherever data is read or written
# (Unity Catalog grants, or checks like `is_admin` in your handlers).
