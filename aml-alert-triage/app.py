import os

import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sdk.core import Config
from openai import OpenAI

from investigation import (ACTION_LABELS, SAR_SYSTEM_PROMPT, TERMINAL, allowed_actions, build_case_context,
                           current_status, get_history, record_decision)

# Defined in `app.yaml`.
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
SERVING_ENDPOINT = os.getenv("SERVING_ENDPOINT", "")
S = os.getenv("AML_SCHEMA", "main.aml_monitoring")
SLA_DAYS = int(os.getenv("ALERT_SLA_DAYS", "30"))
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


def get_llm() -> OpenAI:
    cfg = Config()
    token = cfg.authenticate()["Authorization"].removeprefix("Bearer ")
    return OpenAI(base_url=f"{cfg.host}/serving-endpoints", api_key=token)


def current_user() -> str | None:
    """The signed-in user, from the header the Databricks Apps proxy sets. Used for the audit trail."""
    email = st.context.headers.get("X-Forwarded-Email")
    if email:
        return email
    # Running locally (no proxy): use a stand-in. Deployed without the header: no identity, no decisions.
    return None if os.getenv("DATABRICKS_APP_NAME") else os.getenv("LOCAL_USER", "local.dev@example.com")


STATUS_SQL = f"""
    WITH latest AS (SELECT alert_id, max_by(new_status, decided_at) status FROM {S}.alert_dispositions GROUP BY alert_id)
    SELECT a.*, c.name, c.risk_rating, coalesce(l.status, 'OPEN') status,
           datediff(current_date(), a.created_at) age_days
    FROM {S}.alerts a JOIN {S}.customers c USING (customer_id) LEFT JOIN latest l USING (alert_id)"""

st.set_page_config(page_title="AML alert triage", layout="wide")
user = current_user()
st.title("AML alert triage")
st.caption(f"Signed in as {user or 'unknown user (decisions disabled)'} · synthetic data · alert SLA {SLA_DAYS} days")
tab_queue, tab_case, tab_metrics = st.tabs(["Alert queue", "Investigation", "Program metrics"])

# --- Alert queue -------------------------------------------------------------------
with tab_queue:
    rules = query(f"SELECT rule_id, version, severity, description, logic_sql FROM {S}.rules ORDER BY severity DESC")
    statuses = ["OPEN", "RETURNED", "ESCALATED", "SAR_APPROVED", "CLOSED_FALSE_POSITIVE", "CLOSED_EXPLAINED"]
    f1, f2 = st.columns(2)
    want_status = f1.multiselect("Status", statuses, default=["OPEN", "RETURNED", "ESCALATED"])
    want_rules = f2.multiselect("Rule", [r["rule_id"] for r in rules])
    where, params = ["1 = 1"], {}
    for col, values, prefix in (("status", want_status, "s"), ("rule_id", want_rules, "r")):
        if values:
            where.append(f"{col} IN ({', '.join(f':{prefix}{i}' for i in range(len(values)))})")
            params |= {f"{prefix}{i}": v for i, v in enumerate(values)}
    queue = pd.DataFrame(query(f"""SELECT alert_id, score, rule_id, name customer, risk_rating, total_amount,
                                          status, age_days FROM ({STATUS_SQL}) WHERE {' AND '.join(where)}
                                   ORDER BY score DESC, age_days DESC""", params))
    st.write(f"**{len(queue)}** alerts")
    if not queue.empty:
        st.dataframe(queue, hide_index=True, use_container_width=True,
                     column_config={"score": st.column_config.ProgressColumn("score", min_value=0, max_value=100),
                                    "total_amount": st.column_config.NumberColumn(format="$%.2f")})
        pick = st.selectbox("Investigate", queue["alert_id"].tolist())
        if st.button("Open investigation", type="primary"):
            st.session_state["alert_id"] = pick
            st.info("Opened. Switch to the **Investigation** tab.")

# --- Investigation -----------------------------------------------------------------
with tab_case:
    alert_id = st.session_state.get("alert_id")
    if not alert_id:
        st.info("Pick an alert in the **Alert queue** tab.")
    else:
        a = query(f"SELECT * FROM ({STATUS_SQL}) WHERE alert_id = :a", {"a": alert_id})[0]
        rule = next(r for r in rules if r["rule_id"] == a["rule_id"])
        cust = query(f"SELECT * FROM {S}.customers WHERE customer_id = :c", {"c": a["customer_id"]})[0]
        c_param = {"c": a["customer_id"]}

        st.subheader(f"{alert_id} · {a['status']}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Risk score", a["score"])
        m2.metric("Flagged amount", f"${a['total_amount']:,.0f}")
        m3.metric("Customer risk", cust["risk_rating"] + (" · PEP" if cust["pep"] else ""))
        m4.metric("Age", f"{a['age_days']} days", delta="overdue" if a["age_days"] > SLA_DAYS
                  and a["status"] not in TERMINAL else None, delta_color="inverse")
        st.markdown(f"**Rule {rule['rule_id']} v{rule['version']}:** {rule['description']}")
        st.markdown(f"**Customer:** {cust['name']} (#{cust['customer_id']}), {cust['segment'].replace('_', ' ').lower()}, "
                    f"{cust['occupation']}, customer since {cust['onboarded']}")

        evidence = query(f"""SELECT txn_id, ts, type, amount, counterparty, counterparty_country FROM {S}.transactions
                             WHERE txn_id IN (SELECT explode(evidence_txn_ids) FROM {S}.alerts WHERE alert_id = :a)
                             ORDER BY ts""", {"a": alert_id})
        st.markdown("**Flagged transactions**")
        st.dataframe(pd.DataFrame(evidence), hide_index=True, use_container_width=True)

        left, right = st.columns([2, 1])
        with left:
            st.markdown("**Daily money in / out (180 days)**")
            flows = pd.DataFrame(query(f"""
                SELECT CAST(ts AS DATE) day,
                       sum(CASE WHEN type IN ('ACH_IN', 'WIRE_IN', 'CASH_DEPOSIT') THEN amount ELSE 0 END) money_in,
                       sum(CASE WHEN type IN ('ACH_OUT', 'WIRE_OUT', 'CASH_WITHDRAWAL', 'CARD') THEN amount ELSE 0 END) money_out
                FROM {S}.transactions WHERE customer_id = :c GROUP BY 1 ORDER BY 1""", c_param))
            if not flows.empty:
                st.bar_chart(flows, x="day", y=["money_in", "money_out"])
        with right:
            profile = query(f"""SELECT type, count(*) n, round(sum(amount), 2) total FROM {S}.transactions
                                WHERE customer_id = :c GROUP BY type ORDER BY total DESC""", c_param)
            st.markdown("**Activity by type**")
            st.dataframe(pd.DataFrame(profile), hide_index=True)
            prior = query(f"SELECT alert_id, rule_id, status FROM ({STATUS_SQL}) WHERE customer_id = :c "
                          f"AND alert_id != :a", c_param | {"a": alert_id})
            st.markdown("**Other alerts for this customer**")
            if prior:
                st.dataframe(pd.DataFrame(prior), hide_index=True)
            else:
                st.caption("None")

        # AI draft: generated on request, editable, and recorded as used if the analyst escalates with it.
        st.markdown("**SAR narrative draft**")
        context = build_case_context(a, rule, cust, evidence, profile, prior)
        with st.expander("Exactly what will be sent to the model"):
            st.text(context)
        draft_key = f"draft_{alert_id}"
        if st.button("Draft narrative with AI"):
            with st.spinner("Drafting..."):
                resp = get_llm().chat.completions.create(
                    model=SERVING_ENDPOINT, temperature=0.1,
                    messages=[{"role": "system", "content": SAR_SYSTEM_PROMPT}, {"role": "user", "content": context}])
            st.session_state[draft_key] = resp.choices[0].message.content
        if draft_key in st.session_state:
            st.text_area("Draft (edit before use; AI-generated, verify every fact)", key=draft_key, height=260)

        history = get_history(execute, S, alert_id)
        st.markdown("**Audit trail**")
        if history:
            st.dataframe(pd.DataFrame(history), hide_index=True, use_container_width=True)
        else:
            st.caption("No decisions yet.")

        st.markdown("**Decision**")
        actions = allowed_actions(history, user) if user else []
        if a["status"] in TERMINAL:
            st.success(f"Closed: {a['status']}.")
        elif not user:
            st.warning("No signed-in user identity, so decisions can't be recorded.")
        elif not actions:
            st.info("You escalated this alert, so a different reviewer must approve or return it (maker-checker).")
        else:
            with st.form(f"decide_{alert_id}"):
                action = st.radio("Action", actions, format_func=ACTION_LABELS.get, horizontal=True)
                rationale = st.text_area("Rationale (required, recorded in the audit trail)")
                if st.form_submit_button("Record decision", type="primary"):
                    try:
                        new = record_decision(execute, S, alert_id, action, user, rationale,
                                              ai_draft_used=draft_key in st.session_state)
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        st.cache_data.clear()
                        st.success(f"Recorded: {alert_id} is now {new}.")
                        st.rerun()

# --- Program metrics ---------------------------------------------------------------
with tab_metrics:
    all_alerts = pd.DataFrame(query(STATUS_SQL))
    open_mask = ~all_alerts["status"].isin(TERMINAL)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Open alerts", int((all_alerts["status"].isin(["OPEN", "RETURNED"])).sum()))
    k2.metric("Awaiting SAR review", int((all_alerts["status"] == "ESCALATED").sum()))
    k3.metric(f"Overdue (> {SLA_DAYS} days)", int((open_mask & (all_alerts["age_days"] > SLA_DAYS)).sum()))
    k4.metric("SARs approved", int((all_alerts["status"] == "SAR_APPROVED").sum()))

    st.subheader("Rule performance")
    perf = pd.DataFrame(query(f"""
        WITH s AS ({STATUS_SQL})
        SELECT s.rule_id, count(*) alerts,
               count_if(status IN ('SAR_APPROVED', 'CLOSED_FALSE_POSITIVE', 'CLOSED_EXPLAINED')) decided,
               count_if(status = 'SAR_APPROVED') sar_approved,
               count_if(status = 'CLOSED_FALSE_POSITIVE') false_positives,
               round(100 * try_divide(count_if(status = 'SAR_APPROVED'),
                     count_if(status IN ('SAR_APPROVED', 'CLOSED_FALSE_POSITIVE', 'CLOSED_EXPLAINED'))), 1) sar_rate_pct,
               round(100 * try_divide(count_if(g.customer_id IS NOT NULL), count(*)), 1) planted_hit_pct
        FROM s LEFT JOIN {S}.ground_truth g ON g.customer_id = s.customer_id AND g.typology = s.rule_id
        GROUP BY s.rule_id ORDER BY alerts DESC"""))
    st.dataframe(perf, hide_index=True, use_container_width=True, column_config={
        "sar_rate_pct": st.column_config.NumberColumn("SAR rate % (of decided)"),
        "planted_hit_pct": st.column_config.NumberColumn("Planted-pattern hit % (synthetic demo only)")})
    st.caption("Low SAR rates point to rules worth re-tuning. In production, SAR rate from analyst decisions is the "
               "signal; the planted-pattern column only exists because this data is synthetic.")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Open alert age")
        if open_mask.any():
            bands = pd.cut(all_alerts.loc[open_mask, "age_days"], [-1, 7, 14, SLA_DAYS, 10_000],
                           labels=["0-7", "8-14", f"15-{SLA_DAYS}", f">{SLA_DAYS}"])
            st.bar_chart(bands.value_counts(sort=False).rename_axis("days").reset_index(name="alerts"),
                         x="days", y="alerts")
    with c2:
        st.subheader("AI draft use")
        use = query(f"""SELECT count(*) escalations, count_if(ai_draft_used) with_ai_draft
                        FROM {S}.alert_dispositions WHERE action = 'ESCALATE'""")[0]
        st.metric("Escalations using an AI draft", f"{use['with_ai_draft']} of {use['escalations']}")

    st.subheader("Rule catalog")
    for r in rules:
        with st.expander(f"{r['rule_id']} v{r['version']} · severity {r['severity']}"):
            st.write(r["description"])
            st.code(r["logic_sql"], language="sql")
