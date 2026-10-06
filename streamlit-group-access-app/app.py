import os

import streamlit as st
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

# Defined in `app.yaml`.
ADMIN_GROUP = os.getenv("ADMIN_GROUP", "admins")


@st.cache_data(ttl=300, show_spinner=False)
def user_groups(user_token: str) -> tuple[str, list[str]]:
    """Look up the signed-in user's groups with *their* token (scope: iam.current-user:read)."""
    w = WorkspaceClient(host=Config().host, token=user_token, auth_type="pat")
    me = w.current_user.me()
    return me.user_name, sorted(g.display for g in (me.groups or []) if g.display)


st.set_page_config(page_title="Group access", layout="wide")
st.header("Group access")

# Databricks Apps forwards the signed-in user's token on every request.
# Streamlit reads headers once per session; reload the page if the token expires.
user_token = st.context.headers.get("X-Forwarded-Access-Token")
if not user_token:
    st.warning(
        "No user token in the request. This happens when running locally: "
        "deploy the app, and make sure user authorization is enabled with the "
        "`iam.current-user:read` scope."
    )
    st.stop()

user, groups = user_groups(user_token)
is_admin = ADMIN_GROUP in groups

st.write(f"Signed in as **{user}**")
st.write("Groups: " + (", ".join(f"`{g}`" for g in groups) or "none"))

st.subheader("Everyone")
st.write("Content every signed-in user can see.")

st.subheader("Admins")
if is_admin:
    st.success(f"You are in `{ADMIN_GROUP}`, so you see this section.")
    st.write("Put admin-only controls here.")
else:
    st.info(f"Only members of `{ADMIN_GROUP}` can see this section.")

# Hiding UI is not access control: enforce the same rule wherever data is read or
# written (Unity Catalog grants, or checks like `is_admin` in your handlers).
