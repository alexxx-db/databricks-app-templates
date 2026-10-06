import os

import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sdk.core import Config
from openai import OpenAI

from care import SYSTEM_PROMPT, build_brief, churn_risk, credit_for, render, template, validate_draft
from network import impact_sql, incidents_sql

# Defined in `app.yaml`.
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
SERVING_ENDPOINT = os.getenv("SERVING_ENDPOINT", "")
S = os.getenv("TELCO_SCHEMA", "main.network_care")
assert WAREHOUSE_ID and SERVING_ENDPOINT, "DATABRICKS_WAREHOUSE_ID and SERVING_ENDPOINT must be set in app.yaml."


@st.cache_resource
def get_connection():
    cfg = Config()  # the app's service principal
    return sql.connect(server_hostname=cfg.host, http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
                       credentials_provider=lambda: cfg.authenticate)


def execute(statement: str, params: dict | None = None) -> list[dict]:
    with get_connection().cursor() as cur:
        cur.execute(statement, params or {})
        cols = [d[0] for d in cur.description or []]
        return [dict(zip(cols, row)) for row in cur.fetchall()] if cols else []


@st.cache_data(ttl=60, show_spinner=False)
def query(statement: str, params: dict | None = None) -> list[dict]:
    return execute(statement, params)


def llm(system: str, user: str) -> str:
    cfg = Config()
    token = cfg.authenticate()["Authorization"].removeprefix("Bearer ")
    client = OpenAI(base_url=f"{cfg.host}/serving-endpoints", api_key=token)
    resp = client.chat.completions.create(model=SERVING_ENDPOINT, temperature=0.2,
                                          messages=[{"role": "system", "content": system},
                                                    {"role": "user", "content": user}])
    return (resp.choices[0].message.content or "").strip().strip('"')


def current_user() -> str:
    return st.context.headers.get("X-Forwarded-Email") or os.getenv("LOCAL_USER", "care.agent@example.com")


def with_policy(customers: list[dict], inc: dict) -> pd.DataFrame:
    df = pd.DataFrame(customers)
    if not df.empty:
        df["credit"] = [credit_for(c["plan"], c["monthly_charge"], inc["kind"], inc["duration_min"]) for c in customers]
        df["churn_risk"] = [churn_risk(c) for c in customers]
    return df


st.set_page_config(page_title="Network care", layout="wide")
st.title("Network care")
st.caption("Synthetic network: 60 cell sites, 15-minute KPIs over 14 days, 6,000 customers.")
incidents = query(incidents_sql(S))
tab_net, tab_impact, tab_care = st.tabs(["Network", "Customer impact", "Proactive outreach"])

# --- Network ------------------------------------------------------------------------
with tab_net:
    ongoing = [i for i in incidents if i["ongoing"]]
    n1, n2, n3, n4 = st.columns(4)
    n1.metric("Ongoing incidents", len(ongoing))
    n2.metric("Incidents (14 days)", len(incidents))
    n3.metric("Outages", sum(i["kind"] == "OUTAGE" for i in incidents))
    n4.metric("Outage minutes", sum(i["duration_min"] for i in incidents if i["kind"] == "OUTAGE"))
    for i in ongoing:
        st.error(f"Ongoing {i['kind'].lower()} at {i['site_id']} ({i['area']}) since {i['started_at']:%H:%M}: "
                 f"dropped calls up to {i['max_drop_call_pct']}%")

    sites = pd.DataFrame(query(f"SELECT site_id, area, technology, lat, lon FROM {S}.sites"))
    hot = {i["site_id"]: ("#d62728" if i["ongoing"] else "#ff7f0e") for i in incidents}
    sites["color"] = sites["site_id"].map(hot).fillna("#2ca02c")
    st.subheader("Cell sites")
    st.map(sites, latitude="lat", longitude="lon", color="color", size=120)
    st.caption("Red: ongoing incident · orange: incident in the last 14 days · green: no incidents")

    st.subheader("Incidents")
    st.dataframe(pd.DataFrame(incidents), hide_index=True, use_container_width=True)
    st.caption("An incident is 2+ consecutive 15-minute intervals with availability under 95% or dropped calls over 2%. "
               "Single-interval blips are ignored.")

    site = st.selectbox("Site KPIs", sorted(sites["site_id"]),
                        index=sorted(sites["site_id"]).index(incidents[0]["site_id"]) if incidents else 0)
    kpi = pd.DataFrame(query(f"SELECT ts, availability_pct, drop_call_rate_pct, users FROM {S}.kpis "
                             f"WHERE site_id = :s AND ts >= (SELECT max(ts) FROM {S}.kpis) - INTERVAL 3 DAYS ORDER BY ts",
                             {"s": site}))
    k1, k2 = st.columns(2)
    k1.line_chart(kpi, x="ts", y="availability_pct")
    k2.line_chart(kpi, x="ts", y="drop_call_rate_pct")

# --- Customer impact ----------------------------------------------------------------
def pick_incident(key: str) -> dict | None:
    if not incidents:
        st.info("No incidents in the period.")
        return None
    return st.selectbox("Incident", incidents, key=key,
                        format_func=lambda i: f"{i['incident_id']} · {i['kind']} · {i['area']} · {i['duration_min']} min"
                        + (" · ongoing" if i["ongoing"] else ""))


with tab_impact:
    inc = pick_incident("impact_incident")
    if inc:
        df = with_policy(query(impact_sql(S), {"incident": inc["incident_id"]}), inc)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Customers affected", len(df))
        m2.metric("High churn risk", int((df["churn_risk"] == "HIGH").sum()) if not df.empty else 0)
        m3.metric("Opened a ticket", int(df["opened_ticket"].sum()) if not df.empty else 0)
        m4.metric("Credits by policy", f"${df['credit'].sum():,.2f}" if not df.empty else "$0.00")
        if not df.empty:
            st.bar_chart(df.groupby("plan").size().rename("customers").reset_index(), x="plan", y="customers")
            st.dataframe(df.sort_values(["churn_risk", "monthly_charge"], ascending=[True, False]),
                         hide_index=True, use_container_width=True)
        st.caption("Credit policy: outages of 60+ minutes earn one day of service per started 4 hours (business plans "
                   "double), capped at half the monthly charge; degradations earn no automatic credit. "
                   "High churn risk = hit by 2+ incidents in 14 days, or opened a ticket about this one.")

# --- Proactive outreach --------------------------------------------------------------
with tab_care:
    st.markdown("Draft one SMS for an incident. The model never writes amounts: it uses a `{credit}` placeholder that "
                "is filled per customer from the credit policy, and every draft is checked before use.")
    inc = pick_incident("care_incident")
    if inc:
        df = with_policy(query(impact_sql(S), {"incident": inc["incident_id"]}), inc)
        audience = st.radio("Send to", ["High churn risk only", "All affected customers"], horizontal=True)
        if audience.startswith("High") and not df.empty:
            df = df[df["churn_risk"] == "HIGH"]
        already = {r["customer_id"] for r in query(f"SELECT customer_id FROM {S}.outreach WHERE incident_id = :i",
                                                   {"i": inc["incident_id"]})}
        if not df.empty:
            df = df[~df["customer_id"].isin(already)]
        with_credit = bool(not df.empty and df["credit"].max() > 0)
        st.write(f"**{len(df)}** customers to contact" + (f" ({len(already)} already queued)" if already else ""))

        key = f"sms_{inc['incident_id']}_{audience}"
        if st.button("Draft SMS with AI", disabled=df.empty):
            draft = llm(SYSTEM_PROMPT, build_brief(inc, with_credit))
            problems = validate_draft(draft, with_credit, float(df["credit"].max()))
            st.session_state[key] = draft if not problems else template(inc, with_credit)
            st.session_state[f"{key}_problems"] = problems
        if key in st.session_state:
            problems = st.session_state.get(f"{key}_problems") or []
            if problems:
                st.warning(f"The AI draft was rejected ({'; '.join(problems)}), so the standard template is used.")
            message = st.text_area("Message (edit if needed; keep the {credit} placeholder)", key=key)
            edit_problems = validate_draft(message, with_credit, float(df["credit"].max())) if not df.empty else []
            for p in edit_problems:
                st.error(f"Message {p}.")
            preview = df.head(5).assign(sms=lambda d: [render(message, c) for c in d["credit"]])
            st.dataframe(preview[["customer_id", "first_name", "plan", "credit", "sms"]], hide_index=True,
                         use_container_width=True)
            if st.button("Approve and queue", type="primary", disabled=bool(edit_problems) or df.empty):
                rows = df.to_dict("records")
                values, params = [], {"i": inc["incident_id"], "by": current_user()}
                for n, c in enumerate(rows):
                    values.append(f"(:i, :c{n}, 'SMS', :m{n}, :cr{n}, :by, current_timestamp(), 'QUEUED')")
                    params |= {f"c{n}": int(c["customer_id"]), f"m{n}": render(message, c["credit"]),
                               f"cr{n}": float(c["credit"])}
                execute(f"INSERT INTO {S}.outreach VALUES {', '.join(values)}", params)
                st.cache_data.clear()  # queued customers drop out of the list on the next run
                st.success(f"Queued {len(rows)} messages for {inc['incident_id']}.")

    st.subheader("Queued outreach")
    queued = query(f"SELECT incident_id, count(*) messages, round(sum(credit), 2) credits, max(approved_by) approved_by, "
                   f"max(queued_at) queued_at FROM {S}.outreach GROUP BY incident_id ORDER BY queued_at DESC")
    if queued:
        st.dataframe(pd.DataFrame(queued), hide_index=True, use_container_width=True)
    else:
        st.caption("Nothing queued yet.")
