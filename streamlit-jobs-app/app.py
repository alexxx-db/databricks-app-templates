import os
from datetime import datetime

import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient

# Defined in `app.yaml` from the `job` resource.
JOB_ID = int(os.getenv("DATABRICKS_JOB_ID", "0"))
assert JOB_ID, "DATABRICKS_JOB_ID must be set in app.yaml."


@st.cache_resource
def get_client() -> WorkspaceClient:
    # Runs as the app's service principal, which has CAN_MANAGE_RUN on the job.
    return WorkspaceClient()


def fmt_time(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def run_state(run) -> str:
    if run.status and run.status.state:
        state = run.status.state.value
        term = run.status.termination_details
        return f"{state} ({term.code.value})" if term and term.code else state
    return run.state.life_cycle_state.value if run.state and run.state.life_cycle_state else "UNKNOWN"


st.set_page_config(page_title="Jobs app", layout="wide")
w = get_client()

job = w.jobs.get(JOB_ID)
st.header(f"Job: {job.settings.name}")

# --- Trigger -----------------------------------------------------------------
with st.form("run"):
    defaults = {p.name: p.default for p in (job.settings.parameters or [])}
    params = {
        name: st.text_input(name, value=default or "") for name, default in defaults.items()
    }
    if not defaults:
        st.caption("This job defines no job parameters.")
    if st.form_submit_button("Run now", type="primary"):
        # run_now returns immediately; the run list below picks it up.
        waiter = w.jobs.run_now(JOB_ID, job_parameters=params or None)
        st.session_state["selected_run"] = waiter.run_id
        st.toast(f"Started run {waiter.run_id}")


# --- Runs (auto-refreshing) ------------------------------------------------
@st.fragment(run_every="5s")
def runs_panel():
    runs = list(w.jobs.list_runs(job_id=JOB_ID, limit=10))
    if not runs:
        st.info("No runs yet.")
        return
    st.subheader("Recent runs")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "run_id": r.run_id,
                    "state": run_state(r),
                    "started": fmt_time(r.start_time),
                    "ended": fmt_time(r.end_time),
                    "link": r.run_page_url,
                }
                for r in runs
            ]
        ),
        column_config={"link": st.column_config.LinkColumn("link", display_text="open")},
        hide_index=True,
        use_container_width=True,
    )


runs_panel()

# --- Output of one run -------------------------------------------------------
st.subheader("Run output")
run_id = st.number_input("Run ID", value=st.session_state.get("selected_run") or 0, step=1)
if run_id:
    run = w.jobs.get_run(int(run_id))
    st.write(f"**State:** {run_state(run)}  ·  [Open in Databricks]({run.run_page_url})")
    # Output is stored per task run, not on the parent job run.
    for task in run.tasks or []:
        with st.expander(f"Task `{task.task_key}`: {run_state(task)}"):
            out = w.jobs.get_run_output(task.run_id)
            if out.error:
                st.error(out.error)
                if out.error_trace:
                    st.code(out.error_trace)
            elif out.notebook_output and out.notebook_output.result:
                # Set with dbutils.notebook.exit("...") in the notebook.
                st.code(out.notebook_output.result)
            elif out.logs:
                st.code(out.logs)
            else:
                st.caption("No output yet (or the task type doesn't produce any).")
