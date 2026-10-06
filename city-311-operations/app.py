import os
import uuid

import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sdk.core import Config
from openai import OpenAI

from equity import equity_sql
from triage import LANGUAGES, PRIORITIES, UPDATE_PROMPT, parse_triage, redact, triage_prompt, update_context

# Defined in `app.yaml`.
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
SERVING_ENDPOINT = os.getenv("SERVING_ENDPOINT", "")
S = os.getenv("CITY_SCHEMA", "main.city_311")
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
    resp = client.chat.completions.create(model=SERVING_ENDPOINT, temperature=0.1,
                                          messages=[{"role": "system", "content": system},
                                                    {"role": "user", "content": user}])
    return resp.choices[0].message.content or ""


# Request-level on-time flag shared by several views: NULL while open and not yet due.
ON_TIME = """CASE WHEN r.closed_at IS NOT NULL THEN r.closed_at <= timestampadd(DAY, c.sla_days, r.created_at)
                 WHEN current_timestamp() > timestampadd(DAY, c.sla_days, r.created_at) THEN false END"""

st.set_page_config(page_title="City 311 operations", layout="wide")
st.title("City 311 operations")
st.caption("Fictional city, synthetic data. On time = resolved within the category's target days.")
categories = query(f"SELECT * FROM {S}.categories ORDER BY department, category")
districts = query(f"SELECT * FROM {S}.districts ORDER BY district_id")
tab_ops, tab_equity, tab_intake = st.tabs(["Operations", "Service equity", "Intake (AI-assisted)"])

# --- Operations -------------------------------------------------------------------
with tab_ops:
    k = query(f"""SELECT count_if(r.status = 'OPEN') open_requests,
                         count_if(r.created_at >= current_timestamp() - INTERVAL 7 DAYS) opened_7d,
                         count_if(r.status = 'OPEN' AND current_timestamp() > timestampadd(DAY, c.sla_days, r.created_at)) overdue,
                         round(100 * avg(CASE WHEN r.created_at >= current_timestamp() - INTERVAL 30 DAYS
                                              THEN CAST({ON_TIME} AS INT) END), 1) on_time_30d
                  FROM {S}.requests r JOIN {S}.categories c USING (category)""")[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open requests", f"{k['open_requests']:,}")
    c2.metric("Opened in the last 7 days", f"{k['opened_7d']:,}")
    c3.metric("Open and overdue", f"{k['overdue']:,}")
    c4.metric("On time (last 30 days)", f"{k['on_time_30d']}%")

    left, right = st.columns(2)
    with left:
        st.subheader("Requests opened vs. closed per week")
        weekly = pd.DataFrame(query(f"""
            SELECT week, sum(opened) opened, sum(closed) closed FROM (
              SELECT date_trunc('week', created_at) week, 1 opened, 0 closed FROM {S}.requests
              UNION ALL
              SELECT date_trunc('week', closed_at), 0, 1 FROM {S}.requests WHERE closed_at IS NOT NULL)
            GROUP BY week ORDER BY week"""))
        st.line_chart(weekly, x="week", y=["opened", "closed"])
    with right:
        st.subheader("On time by department")
        by_dept = pd.DataFrame(query(f"""
            SELECT c.department, round(100 * avg(CAST({ON_TIME} AS INT)), 1) on_time_pct
            FROM {S}.requests r JOIN {S}.categories c USING (category)
            GROUP BY c.department ORDER BY on_time_pct"""))
        st.bar_chart(by_dept, x="department", y="on_time_pct", horizontal=True)

    st.subheader("Open backlog")
    backlog = pd.DataFrame(query(f"""
        SELECT r.category, c.department, c.sla_days target_days, count(*) open,
               count_if(current_timestamp() > timestampadd(DAY, c.sla_days, r.created_at)) overdue
        FROM {S}.requests r JOIN {S}.categories c USING (category) WHERE r.status = 'OPEN'
        GROUP BY r.category, c.department, c.sla_days ORDER BY overdue DESC"""))
    st.dataframe(backlog, hide_index=True, use_container_width=True)

    st.subheader("Open requests map")
    show_overdue = st.toggle("Overdue only")
    pins = pd.DataFrame(query(f"""
        SELECT r.lat, r.lon FROM {S}.requests r JOIN {S}.categories c USING (category)
        WHERE r.status = 'OPEN' AND (NOT :overdue OR current_timestamp() > timestampadd(DAY, c.sla_days, r.created_at))""",
                              {"overdue": show_overdue}))
    if not pins.empty:
        st.map(pins, size=20)

    st.subheader("Look up a request")
    rid = st.text_input("Request ID", placeholder="SR-2026-000123")
    if rid:
        found = query(f"SELECT r.*, c.sla_days FROM {S}.requests r JOIN {S}.categories c USING (category) "
                      f"WHERE r.request_id = :r", {"r": rid.strip().upper()})
        if not found:
            st.info("No request with that ID.")
        else:
            req = found[0]
            st.write(f"**{req['category']}** · {req['department']} · {req['status']} · reported {req['created_at']:%Y-%m-%d}"
                     + (f" · resolved {req['closed_at']:%Y-%m-%d}" if req["closed_at"] else ""))
            st.write(f"Resident's description: {req['description']}")
            lang = st.selectbox("Update language", list(LANGUAGES), format_func=LANGUAGES.get,
                                index=list(LANGUAGES).index(req["language"]) if req["language"] in LANGUAGES else 0)
            if st.button("Draft resident update"):
                text = llm(UPDATE_PROMPT.format(language=LANGUAGES[lang]), update_context(req, req["sla_days"]))
                st.session_state[f"update_{rid}"] = text
            if f"update_{rid}" in st.session_state:
                st.text_area("Draft update (machine-drafted; review before sending, especially translations)",
                             key=f"update_{rid}", height=140)

# --- Service equity -----------------------------------------------------------------
with tab_equity:
    st.markdown("Is any district served slower than **its mix of requests** explains? Each district's on-time rate "
                "is compared with what it would be at citywide on-time rates for the same categories.")
    e1, e2 = st.columns(2)
    dept = e1.selectbox("Department", ["All"] + sorted({c["department"] for c in categories}))
    threshold = e2.slider("Flag districts this many points below expected", 1, 25, 10)
    eq = pd.DataFrame(query(equity_sql(S), {"department": dept, "threshold": threshold}))
    if not eq.empty:
        eq["status"] = eq["flagged"].map({True: "⚠ Below expected", False: "Within range"})
        flagged = eq[eq["flagged"]]
        if flagged.empty:
            st.success(f"No district is more than {threshold} points below its expected on-time rate.")
        else:
            st.warning("Below expected: " + ", ".join(f"{r.district} ({r.gap_pts:+.1f} pts)" for r in flagged.itertuples()))
        st.bar_chart(eq, x="district", y="gap_pts", horizontal=True)
        st.dataframe(eq.drop(columns=["flagged"]), hide_index=True, use_container_width=True, column_config={
            "on_time_pct": st.column_config.NumberColumn("On time %"),
            "expected_pct": st.column_config.NumberColumn("Expected % (for its mix)"),
            "gap_pts": st.column_config.NumberColumn("Gap (pts)"),
            "raw_median_days": st.column_config.NumberColumn("Raw median days (not mix-adjusted)")})
        with st.expander("How this is calculated"):
            st.markdown(
                "- **On time:** resolved within the category's target days. Open requests past target count as late; "
                "open requests not yet due are left out, so recent backlogs don't look good by default.\n"
                "- **Expected:** the on-time rate the district would have if each of its requests were handled at the "
                "citywide on-time rate for that category.\n"
                "- **Gap:** on time minus expected. Raw median days is shown for contrast: a district that mostly "
                "requests slow categories (like sidewalk repair) looks slow on raw medians even when it's on time.\n"
                "- A flag is a prompt to investigate (staffing, contractor coverage, routing), not a conclusion.")

# --- Intake -------------------------------------------------------------------------
with tab_intake:
    st.markdown("Paste a resident's message. Personal details are removed **before** the text reaches the model; "
                "you confirm the routing before anything is filed.")
    message = st.text_area("Resident's message", height=120,
                           placeholder="There's a big pothole on Main St near 1200, my number is 816-555-0142")
    i1, i2, i3 = st.columns(3)
    district = i1.selectbox("District", districts, format_func=lambda d: f"{d['district_id']}. {d['name']}")
    channel = i2.selectbox("Channel", ["Phone", "Mobile App", "Web", "Walk-in"])
    language = i3.selectbox("Resident's language", list(LANGUAGES), format_func=LANGUAGES.get)

    if st.button("Suggest routing", disabled=not message.strip()):
        clean, removed = redact(message)
        st.session_state["intake"] = {"clean": clean, "removed": removed,
                                      "suggestion": parse_triage(llm(triage_prompt(categories), clean), categories)}

    intake = st.session_state.get("intake")
    if intake:
        if intake["removed"]:
            st.info(f"Removed before sending to the model: {', '.join(sorted(set(intake['removed'])))}")
        with st.expander("Exactly what was sent to the model"):
            st.text(intake["clean"])
        sug = intake["suggestion"]
        if not sug["valid"]:
            st.warning("The model's answer didn't match a known category. Choose one manually.")
        names = [c["category"] for c in categories]
        with st.form("file_request"):
            f1, f2 = st.columns(2)
            category = f1.selectbox("Category", names, index=names.index(sug["category"]) if sug["valid"] else 0)
            priority = f2.selectbox("Priority", PRIORITIES, index=PRIORITIES.index(sug["priority"]))
            st.caption(f"Suggested with confidence {sug['confidence']:.0%}. {sug['summary']}"
                       + (f" Location: {sug['location']}" if sug["location"] else ""))
            if st.form_submit_button("File request", type="primary"):
                dept_of = {c["category"]: c["department"] for c in categories}
                new_id = f"SR-NEW-{uuid.uuid4().hex[:8].upper()}"
                # Stores the redacted text: contact details belong in the CRM, not the work-order table.
                execute(f"""INSERT INTO {S}.requests (request_id, created_at, closed_at, status, category, department,
                                                      district_id, lat, lon, channel, language, description)
                            VALUES (:id, current_timestamp(), NULL, 'OPEN', :cat, :dept, :d, :lat, :lon, :ch, :lang, :desc)""",
                        {"id": new_id, "cat": category, "dept": dept_of[category], "d": district["district_id"],
                         "lat": district["lat"], "lon": district["lon"], "ch": channel, "lang": language,
                         "desc": intake["clean"][:1000]})
                st.cache_data.clear()
                del st.session_state["intake"]
                st.success(f"Filed {new_id}: {category} ({priority}) for {district['name']}.")
