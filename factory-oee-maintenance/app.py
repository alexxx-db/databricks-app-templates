import os
import uuid

import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sdk.core import Config
from openai import OpenAI

from health import episodes_sql, scored_sql, status_sql
from oee import DIMENSIONS, losses_sql, oee_sql
from workorder import SYSTEM_PROMPT, build_context, check_citations, ensure_lockout, priority_for

# Defined in `app.yaml`.
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
SERVING_ENDPOINT = os.getenv("SERVING_ENDPOINT", "")
S = os.getenv("PLANT_SCHEMA", "main.plant_ops")
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


@st.cache_data(ttl=120, show_spinner=False)
def query(statement: str, params: dict | None = None) -> list[dict]:
    return execute(statement, params)


def llm(system: str, user: str) -> str:
    cfg = Config()
    token = cfg.authenticate()["Authorization"].removeprefix("Bearer ")
    client = OpenAI(base_url=f"{cfg.host}/serving-endpoints", api_key=token)
    resp = client.chat.completions.create(model=SERVING_ENDPOINT, temperature=0.1,
                                          messages=[{"role": "system", "content": system},
                                                    {"role": "user", "content": user}])
    return resp.choices[0].message.content or ""


def current_user() -> str:
    return st.context.headers.get("X-Forwarded-Email") or os.getenv("LOCAL_USER", "planner@example.com")


st.set_page_config(page_title="Factory OEE & maintenance", layout="wide")
st.title("Factory OEE & maintenance")
st.caption("Synthetic plant: 3 lines, 12 machines, 60 days of hourly sensor data and shift records.")
status = query(status_sql(S))
tab_oee, tab_health, tab_wo = st.tabs(["OEE", "Asset health", "Work orders"])

# --- OEE --------------------------------------------------------------------------
with tab_oee:
    o1, o2 = st.columns(2)
    days = o1.select_slider("Period", [7, 14, 30, 60], value=30, format_func=lambda d: f"Last {d} days")
    by = o2.radio("Break down by", list(DIMENSIONS), horizontal=True)
    rows = pd.DataFrame(query(oee_sql(S, by), {"days": days}))
    if not rows.empty:
        planned = rows["planned_min"].sum()
        k = query(losses_sql(S), {"days": days})[0]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("OEE (plant)", f"{100 * k['productive'] / planned:.1f}%")
        m2.metric("Availability loss", f"{100 * (k['breakdowns'] + k['other_downtime']) / planned:.1f}%")
        m3.metric("Performance loss", f"{100 * k['slow_cycles'] / planned:.1f}%")
        m4.metric("Quality loss", f"{100 * k['scrap'] / planned:.1f}%")

        st.subheader("Where planned production time went (minutes)")
        losses = pd.DataFrame([{"bucket": b.replace("_", " "), "minutes": k[b]} for b in
                               ["productive", "slow_cycles", "other_downtime", "scrap", "breakdowns"]])
        st.bar_chart(losses, x="bucket", y="minutes", horizontal=True)

        st.subheader(f"OEE by {by.lower()}")
        if by == "Day":
            st.line_chart(rows, x="dim", y=["oee_pct", "availability_pct", "performance_pct", "quality_pct"])
        st.dataframe(rows, hide_index=True, use_container_width=True)
        st.caption("OEE = availability × performance × quality = ideal cycle time × good units ÷ planned time. "
                   "Totals are computed from summed minutes and units, never by averaging percentages.")

# --- Asset health ------------------------------------------------------------------
with tab_health:
    st.markdown("Each machine is compared with **its own** baseline (first 14 days). An alert needs 12 hours in a row "
                "far above baseline, so short glitches and post-maintenance changes don't trigger it.")
    grid = pd.DataFrame(status)
    labels = {"ALERT": "🔴 Alert", "WATCH": "🟠 Watch", "OK": "🟢 OK"}
    grid["status"] = grid["status"].map(labels)
    st.dataframe(grid, hide_index=True, use_container_width=True)

    machine = st.selectbox("Machine", [s["machine_id"] for s in status],
                           index=next((i for i, s in enumerate(status) if s["status"] == "ALERT"), 0))
    trend = pd.DataFrame(query(f"SELECT ts, vibration_rms, baseline, alert_hour FROM ({scored_sql(S)}) "
                               f"WHERE machine_id = :m ORDER BY ts", {"m": machine}))
    if not trend.empty:
        st.line_chart(trend, x="ts", y=["vibration_rms", "baseline"])
    episodes = query(f"SELECT * FROM ({episodes_sql(S)}) WHERE machine_id = :m", {"m": machine})
    repairs = query(f"SELECT ts, type, note FROM {S}.maintenance_log WHERE machine_id = :m ORDER BY ts", {"m": machine})
    left, right = st.columns(2)
    with left:
        st.markdown("**Alert episodes**")
        if episodes:
            st.dataframe(pd.DataFrame(episodes), hide_index=True)
        else:
            st.caption("None")
    with right:
        st.markdown("**Maintenance log**")
        if repairs:
            st.dataframe(pd.DataFrame(repairs), hide_index=True)
            for e in episodes:  # how much warning the alert gave before a breakdown
                fixes = [r for r in repairs if r["type"] == "CORRECTIVE" and r["ts"] > e["started_at"]]
                if fixes:
                    lead = (fixes[0]["ts"] - e["started_at"]).total_seconds() / 86400
                    st.info(f"Alert from {e['started_at']:%b %d} preceded the breakdown repair by {lead:.1f} days.")
        else:
            st.caption("None")

# --- Work orders --------------------------------------------------------------------
with tab_wo:
    flagged = [s for s in status if s["status"] != "OK"]
    st.markdown("Draft a work order for a flagged machine. The draft cites the machine's manual; lockout/tagout is "
                "always included; you review before filing.")
    if not flagged:
        st.success("No machines are flagged right now.")
    else:
        pick = st.selectbox("Flagged machine", flagged, format_func=lambda s: f"{s['machine_id']} · {s['status']}")
        mid = pick["machine_id"]
        machine_row = query(f"SELECT * FROM {S}.machines WHERE machine_id = :m", {"m": mid})[0]
        manual = query(f"SELECT section_id, title, content FROM {S}.manuals WHERE machine_type = :t ORDER BY section_id",
                       {"t": machine_row["machine_type"]})
        context = build_context(
            machine_row, pick, query(f"SELECT * FROM ({episodes_sql(S)}) WHERE machine_id = :m", {"m": mid}),
            query(f"SELECT ts, type, note FROM {S}.maintenance_log WHERE machine_id = :m ORDER BY ts", {"m": mid}), manual)
        with st.expander("Exactly what will be sent to the model"):
            st.text(context)
        key = f"wo_{mid}"
        if st.button("Draft work order with AI"):
            plan, added = ensure_lockout(llm(SYSTEM_PROMPT, context), manual)
            st.session_state[key] = plan
            st.session_state[f"{key}_loto_added"] = added
        if key in st.session_state:
            if st.session_state.get(f"{key}_loto_added"):
                st.info("The draft didn't include lockout/tagout, so the manual's procedure was added at the top.")
            invalid = check_citations(st.session_state[key], {m["section_id"] for m in manual})
            if invalid:
                st.warning(f"The draft cites sections that aren't in this machine's manual: {', '.join(invalid)}. "
                           "Fix or remove them before filing.")
            with st.form(f"file_{mid}"):
                summary = st.text_input("Summary", value=f"{mid}: sustained vibration {pick['vibration_x_baseline']}x baseline")
                plan = st.text_area("Plan (edit before filing)", key=key, height=280)
                priority = st.selectbox("Priority", ["HIGH", "MEDIUM", "LOW"],
                                        index=["HIGH", "MEDIUM", "LOW"].index(priority_for(pick["status"])))
                if st.form_submit_button("File work order", type="primary", disabled=bool(invalid)):
                    wo_id = f"WO-{uuid.uuid4().hex[:8].upper()}"
                    execute(f"""INSERT INTO {S}.work_orders VALUES
                                (:id, :m, current_timestamp(), :by, :p, :summary, :plan, true, 'OPEN')""",
                            {"id": wo_id, "m": mid, "by": current_user(), "p": priority,
                             "summary": summary[:300], "plan": plan[:5000]})
                    st.cache_data.clear()
                    st.success(f"Filed {wo_id} ({priority}).")

    st.subheader("Open work orders")
    orders = query(f"SELECT wo_id, machine_id, priority, summary, created_by, created_at, status "
                   f"FROM {S}.work_orders ORDER BY created_at DESC LIMIT 50")
    if orders:
        st.dataframe(pd.DataFrame(orders), hide_index=True, use_container_width=True)
    else:
        st.caption("None yet.")
