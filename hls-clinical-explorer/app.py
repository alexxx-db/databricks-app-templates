import os

import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sdk.core import Config
from openai import OpenAI

from summary import SYSTEM_PROMPT, build_context, llm_allowed

# Defined in `app.yaml`.
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
SERVING_ENDPOINT = os.getenv("SERVING_ENDPOINT", "")
S = os.getenv("HLS_SCHEMA", "main.mimic_explorer")
assert WAREHOUSE_ID and SERVING_ENDPOINT, "DATABRICKS_WAREHOUSE_ID and SERVING_ENDPOINT must be set in app.yaml."


@st.cache_resource
def get_connection():
    cfg = Config()  # the app's service principal
    return sql.connect(server_hostname=cfg.host, http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
                       credentials_provider=lambda: cfg.authenticate)


@st.cache_data(ttl=300, show_spinner=False)
def query(statement: str, params: dict | None = None) -> list[dict]:
    with get_connection().cursor() as cur:
        cur.execute(statement, params or {})
        cols = [d[0] for d in cur.description or []]
        return [dict(zip(cols, row)) for row in cur.fetchall()] if cols else []


def df(statement: str, params: dict | None = None) -> pd.DataFrame:
    return pd.DataFrame(query(statement, params))


def get_llm() -> OpenAI:
    cfg = Config()
    token = cfg.authenticate()["Authorization"].removeprefix("Bearer ")
    return OpenAI(base_url=f"{cfg.host}/serving-endpoints", api_key=token)


st.set_page_config(page_title="Clinical explorer (MIMIC)", layout="wide")
info = query(f"SELECT * FROM {S}.dataset_info")[0]
st.title("Clinical explorer")
st.caption(f"Data: {info['license']}. De-identified; dates are shifted. For research and education, not clinical care.")
if info["credentialed"]:
    st.warning("Credentialed PhysioNet data: only users who are credentialed for this dataset and have signed its "
               "data use agreement may use this app. Restrict app access accordingly.")

tab_cohort, tab_patient, tab_ai = st.tabs(["Cohort explorer", "Admission timeline", "AI summary"])

# --- Cohort explorer --------------------------------------------------------------
with tab_cohort:
    types = [r["admission_type"] for r in query(f"SELECT DISTINCT admission_type FROM {S}.admissions ORDER BY 1")]
    f1, f2, f3, f4 = st.columns([3, 2, 3, 1])
    dx = f1.text_input("Diagnosis contains (title or ICD code prefix)", placeholder="e.g. sepsis, I50, 428")
    age_min, age_max = f2.slider("Age", 0, 90, (18, 90))
    chosen = f3.multiselect("Admission type", types)
    icu_only = f4.checkbox("ICU stay")

    where = ["a.age BETWEEN :age_min AND :age_max"]
    params: dict = {"age_min": age_min, "age_max": age_max}
    if dx.strip():
        where.append(f"EXISTS (SELECT 1 FROM {S}.diagnoses d WHERE d.hadm_id = a.hadm_id "
                     "AND (lower(d.long_title) LIKE lower(:dx_like) OR upper(d.icd_code) LIKE upper(:dx_prefix)))")
        params |= {"dx_like": f"%{dx.strip()}%", "dx_prefix": f"{dx.strip().replace('.', '')}%"}
    if chosen:
        names = [f":t{i}" for i in range(len(chosen))]
        where.append(f"a.admission_type IN ({', '.join(names)})")
        params |= {f"t{i}": t for i, t in enumerate(chosen)}
    if icu_only:
        where.append(f"EXISTS (SELECT 1 FROM {S}.icustays i WHERE i.hadm_id = a.hadm_id)")
    cohort = f"SELECT a.* FROM {S}.admissions a WHERE {' AND '.join(where)}"

    stats = query(f"""SELECT count(DISTINCT subject_id) patients, count(*) admissions,
                             round(100 * avg(hospital_expire_flag), 1) mortality_pct,
                             percentile_approx(los_days, 0.5) median_los
                      FROM ({cohort})""", params)[0]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Patients", stats["patients"])
    m2.metric("Admissions", stats["admissions"])
    m3.metric("In-hospital mortality", f"{stats['mortality_pct'] or 0}%")
    m4.metric("Median length of stay", f"{stats['median_los'] or 0:.1f} days")

    if stats["admissions"]:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Age at admission")
            ages = df(f"SELECT CAST(floor(age / 10) * 10 AS INT) age_band, count(*) admissions "
                      f"FROM ({cohort}) GROUP BY 1 ORDER BY 1", params)
            st.bar_chart(ages, x="age_band", y="admissions")
        with c2:
            st.subheader("Most common diagnoses")
            top = df(f"""SELECT coalesce(d.long_title, d.icd_code) diagnosis, count(DISTINCT d.hadm_id) admissions
                         FROM {S}.diagnoses d JOIN ({cohort}) c ON d.hadm_id = c.hadm_id
                         GROUP BY 1 ORDER BY 2 DESC LIMIT 10""", params)
            st.bar_chart(top, x="diagnosis", y="admissions", horizontal=True)

        st.subheader("Admissions")
        rows = df(f"""SELECT hadm_id, subject_id, admittime, age, admission_type, los_days,
                             hospital_expire_flag died FROM ({cohort}) ORDER BY admittime LIMIT 500""", params)
        st.dataframe(rows, hide_index=True, use_container_width=True)
        picked = st.selectbox("Open admission", rows["hadm_id"].tolist(), key="cohort_pick")
        if st.button("Open in timeline"):
            st.session_state["hadm_id"] = int(picked)
            st.info("Opened. Switch to the **Admission timeline** tab.")

# --- Admission timeline -------------------------------------------------------------
if "hadm_id" not in st.session_state:
    st.session_state["hadm_id"] = int(query(f"SELECT min(hadm_id) h FROM {S}.admissions")[0]["h"])
with tab_patient:
    st.number_input("Admission ID (hadm_id)", step=1, key="hadm_id")  # owns st.session_state["hadm_id"]
p = {"h": int(st.session_state["hadm_id"])}
found = query(f"""SELECT a.*, pt.gender FROM {S}.admissions a JOIN {S}.patients pt USING (subject_id)
                  WHERE a.hadm_id = :h""", p)
if not found:
    with tab_patient:
        st.warning(f"No admission {p['h']}.")
    st.stop()
adm = found[0]

with tab_patient:
    st.subheader(f"Admission {adm['hadm_id']} · patient {adm['subject_id']}")
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Age / sex", f"{adm['age']}{'+' if adm['age'] >= 90 else ''} / {adm['gender']}")
    a2.metric("Type", adm["admission_type"])
    a3.metric("Length of stay", f"{adm['los_days']} days")
    a4.metric("Outcome", "Died in hospital" if adm["hospital_expire_flag"] else "Discharged")

    left, right = st.columns(2)
    with left:
        st.markdown("**Diagnoses**")
        st.dataframe(df(f"SELECT seq_num, icd_code, icd_version, long_title FROM {S}.diagnoses "
                        f"WHERE hadm_id = :h ORDER BY seq_num", p), hide_index=True, use_container_width=True)
        st.markdown("**ICU stays**")
        st.dataframe(df(f"SELECT stay_id, first_careunit, intime, outtime, round(los, 1) los_days "
                        f"FROM {S}.icustays WHERE hadm_id = :h ORDER BY intime", p), hide_index=True)
    with right:
        st.markdown("**Medications**")
        st.dataframe(df(f"SELECT starttime, drug, dose_val_rx, dose_unit_rx, route FROM {S}.prescriptions "
                        f"WHERE hadm_id = :h ORDER BY starttime LIMIT 300", p), hide_index=True, use_container_width=True)

    st.markdown("**Lab trends**")
    labels = [r["label"] for r in query(f"""SELECT label, count(*) n FROM {S}.labs
                                            WHERE hadm_id = :h AND valuenum IS NOT NULL
                                            GROUP BY label ORDER BY n DESC LIMIT 30""", p)]
    if labels:
        lab = st.selectbox("Lab", labels)
        series = df(f"SELECT charttime, valuenum FROM {S}.labs WHERE hadm_id = :h AND label = :l "
                    f"AND valuenum IS NOT NULL ORDER BY charttime", p | {"l": lab})
        st.line_chart(series, x="charttime", y="valuenum")
    else:
        st.caption("No numeric labs recorded for this admission.")

    if info["has_notes"]:
        st.markdown("**Clinical notes**")
        for n in query(f"SELECT note_type, charttime, text FROM {S}.notes WHERE hadm_id = :h ORDER BY charttime", p):
            with st.expander(f"{n['note_type']} · {n['charttime']}"):
                st.text(n["text"])

# --- AI summary --------------------------------------------------------------------
with tab_ai:
    allowed, notice = llm_allowed(bool(info["credentialed"]))
    st.write(f"Summarize admission **{adm['hadm_id']}** with `{SERVING_ENDPOINT}`.")
    if notice:
        (st.info if allowed else st.warning)(notice)
    if allowed:
        context = build_context(
            adm,
            query(f"SELECT seq_num, icd_code, icd_version, long_title FROM {S}.diagnoses "
                  f"WHERE hadm_id = :h ORDER BY seq_num LIMIT 25", p),
            query(f"""SELECT label, count(*) n_abnormal, min(valuenum) min_value, max(valuenum) max_value,
                             max_by(valuenum, charttime) last_value, max(valueuom) unit
                      FROM {S}.labs WHERE hadm_id = :h AND flag = 'abnormal'
                      GROUP BY label ORDER BY n_abnormal DESC LIMIT 25""", p),
            query(f"SELECT drug, count(*) n, max(route) route FROM {S}.prescriptions WHERE hadm_id = :h "
                  f"GROUP BY drug ORDER BY n DESC LIMIT 25", p),
            query(f"SELECT first_careunit, los FROM {S}.icustays WHERE hadm_id = :h ORDER BY intime", p),
            query(f"SELECT note_type, charttime, text FROM {S}.notes WHERE hadm_id = :h ORDER BY charttime", p)
            if info["has_notes"] else None,
        )
        with st.expander("Exactly what will be sent to the model"):
            st.text(context)
        if st.button("Generate summary", type="primary"):
            with st.spinner("Summarizing..."):
                resp = get_llm().chat.completions.create(
                    model=SERVING_ENDPOINT, temperature=0.1,
                    messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context}])
            st.markdown(resp.choices[0].message.content)
            st.caption("AI-generated from the data above. Verify against the record; not for clinical use.")
