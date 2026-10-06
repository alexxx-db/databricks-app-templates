import os

import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sdk.core import Config
from openai import OpenAI

from assistant import SYSTEM_PROMPT, Tools, run_turn

# Defined in `app.yaml`.
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
SERVING_ENDPOINT = os.getenv("SERVING_ENDPOINT", "")
SCHEMA = os.getenv("RETAIL_SCHEMA", "main.retail_assistant")
assert WAREHOUSE_ID and SERVING_ENDPOINT, "DATABRICKS_WAREHOUSE_ID and SERVING_ENDPOINT must be set in app.yaml."


@st.cache_resource
def get_connection():
    cfg = Config()  # the app's service principal
    return sql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
        credentials_provider=lambda: cfg.authenticate,
    )


def query(statement: str, params: dict | None = None) -> list[dict]:
    with get_connection().cursor() as cur:
        cur.execute(statement, params or {})
        if not cur.description:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@st.cache_data(ttl=60, show_spinner=False)
def cached_query(statement: str) -> list[dict]:
    """For dashboard queries: Streamlit reruns every tab on each interaction."""
    return query(statement)


def get_llm() -> OpenAI:
    """OpenAI-compatible client for Model Serving. Built per turn so the OAuth token is fresh."""
    cfg = Config()
    token = cfg.authenticate()["Authorization"].removeprefix("Bearer ")
    return OpenAI(base_url=f"{cfg.host}/serving-endpoints", api_key=token)


tools = Tools(query, SCHEMA)
st.set_page_config(page_title="Retail customer assistant", layout="wide")
st.title("Retail customer assistant")
tab_chat, tab_customer, tab_ops = st.tabs(["Assistant", "Customer 360", "Operations"])

# --- Assistant -------------------------------------------------------------------
with tab_chat:
    st.caption("Try: *I'm ava.garcia.1@example.com, where is my latest order?* or *Can I return my last order?*")
    reset = st.button("New conversation")
    if reset or "messages" not in st.session_state:
        st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        st.session_state.transcript = []  # (role, text, tool_calls) for display

    for role, text, calls in st.session_state.transcript:
        with st.chat_message(role):
            for c in calls:
                with st.expander(f"🔧 {c['tool']}({c['args']})"):
                    st.json(c["result"], expanded=False)
            st.markdown(text)

    if prompt := st.chat_input("Ask about an order, a return, or a product"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.transcript.append(("user", prompt, []))
        with st.spinner("Thinking..."):
            answer, calls = run_turn(get_llm(), SERVING_ENDPOINT, tools, st.session_state.messages)
        st.session_state.transcript.append(("assistant", answer, calls))
        st.rerun()

# --- Customer 360 ----------------------------------------------------------------
with tab_customer:
    email = st.text_input("Customer email", placeholder="name@example.com")
    if email:
        customer = tools.find_customer(email)
        if not customer:
            st.info("No customer with that email.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Customer", customer["name"])
            c2.metric("Loyalty tier", customer["loyalty_tier"])
            c3.metric("Location", f"{customer['city']}, {customer['state']}")
            orders = tools.list_recent_orders(customer["customer_id"], limit=20)
            st.subheader("Orders")
            st.dataframe(pd.DataFrame(orders), hide_index=True, use_container_width=True)
            if orders:
                order_id = st.selectbox("Order details", [o["order_id"] for o in orders])
                order = tools.get_order(order_id)
                st.dataframe(pd.DataFrame(order["items"]), hide_index=True, use_container_width=True)
                verdict = tools.check_return_eligibility(order_id)
                (st.success if verdict["eligible"] else st.warning)(f"Return: {verdict['reason']}")
                if order["returns"]:
                    st.write("Returns on this order:")
                    st.dataframe(pd.DataFrame(order["returns"]), hide_index=True)

# --- Operations ------------------------------------------------------------------
with tab_ops:
    kpi = cached_query(f"""
        SELECT
          count_if(order_date >= current_date() - INTERVAL 30 DAYS) AS orders_30d,
          round(sum(CASE WHEN order_date >= current_date() - INTERVAL 30 DAYS THEN total END), 2) AS revenue_30d,
          (SELECT count(*) FROM {SCHEMA}.returns WHERE status = 'REQUESTED') AS open_returns,
          round(100 * try_divide((SELECT count(DISTINCT order_id) FROM {SCHEMA}.returns),
                                 count_if(status = 'DELIVERED')), 1) AS return_rate_pct
        FROM {SCHEMA}.orders""")[0]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Orders (30 days)", f"{kpi['orders_30d']:,}")
    k2.metric("Revenue (30 days)", f"${kpi['revenue_30d'] or 0:,.0f}")
    k3.metric("Open return requests", kpi["open_returns"])
    k4.metric("Return rate", f"{kpi['return_rate_pct']}%")

    st.subheader("Return rate by category")
    by_cat = pd.DataFrame(cached_query(f"""
        WITH order_cat AS (
          SELECT DISTINCT i.order_id, p.category
          FROM {SCHEMA}.order_items i JOIN {SCHEMA}.products p USING (product_id))
        SELECT oc.category,
               round(100 * try_divide(count(DISTINCT r.order_id), count(DISTINCT o.order_id)), 1) AS return_rate_pct
        FROM order_cat oc
        JOIN {SCHEMA}.orders o ON o.order_id = oc.order_id AND o.status = 'DELIVERED'
        LEFT JOIN {SCHEMA}.returns r ON r.order_id = oc.order_id
        GROUP BY oc.category ORDER BY return_rate_pct DESC"""))
    if not by_cat.empty:
        st.bar_chart(by_cat, x="category", y="return_rate_pct", horizontal=True)

    left, right = st.columns(2)
    with left:
        st.subheader("Late deliveries (> 7 days)")
        st.dataframe(pd.DataFrame(cached_query(f"""
            SELECT order_id, order_date, delivered_date, datediff(delivered_date, order_date) AS days
            FROM {SCHEMA}.orders WHERE datediff(delivered_date, order_date) > 7
            ORDER BY order_date DESC LIMIT 50""")), hide_index=True, use_container_width=True)
    with right:
        st.subheader("Open return requests")
        st.dataframe(pd.DataFrame(cached_query(f"""
            SELECT r.return_id, r.order_id, r.reason, r.requested_at, c.email
            FROM {SCHEMA}.returns r JOIN {SCHEMA}.orders o USING (order_id)
            JOIN {SCHEMA}.customers c USING (customer_id)
            WHERE r.status = 'REQUESTED' ORDER BY r.requested_at DESC LIMIT 50""")),
            hide_index=True, use_container_width=True)
