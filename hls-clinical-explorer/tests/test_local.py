"""Local end-to-end checks on the real open MIMIC demos (downloaded from PhysioNet, ~12 MB, cached).

The loader runs in local Spark (ANSI mode, like Databricks), then the real app.py runs on top
with Spark standing in for the SQL warehouse and a fake LLM. Notes tests use synthetic note
files, since no open MIMIC notes exist. Run from the template directory:

    uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
"""

import csv
import gzip
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "setup")]
from load_mimic import build_tables, download_demo  # noqa: E402
from summary import LLM_OVERRIDE_ENV, build_context, llm_allowed  # noqa: E402

CACHE = Path(os.getenv("MIMIC_DEMO_CACHE", Path(tempfile.gettempdir()) / "mimic-demo-cache"))


@pytest.fixture(scope="session")
def spark(tmp_path_factory):
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[2]").appName("hls-test")
             .config("spark.sql.warehouse.dir", str(tmp_path_factory.mktemp("wh")))
             .config("spark.sql.ansi.enabled", "true").config("spark.ui.enabled", "false").getOrCreate())
    yield spark
    spark.stop()


def demo_root(dataset: str) -> str:
    try:
        return download_demo(dataset, str(CACHE / dataset))
    except OSError as e:  # offline
        pytest.skip(f"cannot download {dataset}: {e}")


def load(spark, dataset: str, db: str) -> dict:
    tables = build_tables(spark, dataset, demo_root(dataset))
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {db}")
    for name, frame in tables.items():
        frame.write.mode("overwrite").saveAsTable(f"{db}.{name}")
    spark.createDataFrame([{"dataset": dataset, "credentialed": False, "has_notes": "notes" in tables,
                            "license": "test"}]).write.mode("overwrite").saveAsTable(f"{db}.dataset_info")
    return tables


@pytest.fixture(scope="session")
def iv(spark):
    return load(spark, "mimic-iv-demo", "mimic_iv")


# --- loader -------------------------------------------------------------------------
@pytest.mark.parametrize("dataset, admissions", [("mimic-iv-demo", 275), ("mimic-iii-demo", 129)])
def test_loader_produces_common_schema(spark, dataset, admissions):
    t = build_tables(spark, dataset, demo_root(dataset))
    assert set(t) == {"patients", "admissions", "diagnoses", "labs", "prescriptions", "icustays"}
    a = t["admissions"]
    assert a.count() == admissions
    assert a.filter("age IS NULL OR age < 0 OR age > 90 OR admittime IS NULL OR los_days < 0").count() == 0
    assert t["patients"].count() == 100
    assert a.join(t["patients"], "subject_id", "left_anti").count() == 0
    assert t["labs"].filter("label IS NULL").count() == 0
    # Same columns whichever family was loaded, so the app doesn't care.
    assert a.columns == build_tables(spark, "mimic-iv-demo", demo_root("mimic-iv-demo"))["admissions"].columns


def write_csv(path: Path, header: list[str], rows: list[list]):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_mimic_iv_notes_are_loaded_from_note_module(spark, tmp_path, iv):
    hadm = iv["admissions"].first()
    text = 'Discharge summary\nLine two, with "quotes" and commas.'
    write_csv(tmp_path / "discharge.csv.gz",
              ["note_id", "subject_id", "hadm_id", "note_type", "note_seq", "charttime", "storetime", "text"],
              [["1-DS-1", hadm.subject_id, hadm.hadm_id, "DS", 1, "2180-01-01 10:00:00", "", text]])
    notes = build_tables(spark, "mimic-iv-demo", demo_root("mimic-iv-demo"), str(tmp_path))["notes"].collect()
    assert len(notes) == 1 and notes[0].text == text and notes[0].hadm_id == hadm.hadm_id


def test_mimic_iii_notes_skip_error_rows(spark, tmp_path):
    root = demo_root("mimic-iii-demo")
    cred = tmp_path / "mimic-iii"
    for f in Path(root).glob("*.csv"):  # stand-in for a credentialed copy, uppercase headers like the full data
        rows = list(csv.reader(open(f)))
        write_csv(cred / f"{f.stem}.csv.gz", [c.upper() for c in rows[0]], rows[1:])
    write_csv(cred / "NOTEEVENTS.csv.gz",
              ["ROW_ID", "SUBJECT_ID", "HADM_ID", "CHARTDATE", "CHARTTIME", "STORETIME", "CATEGORY",
               "DESCRIPTION", "CGID", "ISERROR", "TEXT"],
              [[1, 10006, 142345, "2164-10-23", "", "", "Discharge summary", "Report", "", "", "ok\nmultiline"],
               [2, 10006, 142345, "2164-10-24", "", "", "Nursing", "Note", "", "1", "entered in error"]])
    t = build_tables(spark, "mimic-iii", str(cred))
    notes = t["notes"].collect()
    assert [n.text for n in notes] == ["ok\nmultiline"]
    assert t["admissions"].count() == 129  # uppercase headers handled


# --- summary --------------------------------------------------------------------------
ADM = {"age": 90, "gender": "F", "admission_type": "URGENT", "admission_location": "ER", "los_days": 4.2,
       "discharge_location": "HOME", "hospital_expire_flag": 0}


def test_build_context_includes_facts_and_respects_budget():
    ctx = build_context(ADM, [{"seq_num": 1, "long_title": "Sepsis", "icd_code": "A419", "icd_version": 10}],
                        [], [], [], notes=[{"note_type": "DS", "charttime": "t", "text": "x" * 50000}], max_chars=3000)
    assert "Age 90+" in ctx and "Sepsis" in ctx and "(none recorded)" in ctx
    assert len(ctx) <= 3000


def test_llm_gate(monkeypatch):
    assert llm_allowed(False)[0]
    monkeypatch.delenv(LLM_OVERRIDE_ENV, raising=False)
    assert not llm_allowed(True)[0]
    monkeypatch.setenv(LLM_OVERRIDE_ENV, "true")
    assert llm_allowed(True)[0]


# --- the real app.py ---------------------------------------------------------------
class SparkCursor:
    def __init__(self, spark):
        self.spark, self.description, self._rows = spark, None, []

    def execute(self, sql, params=None):
        frame = self.spark.sql(sql, args=params or {})
        self._rows = [tuple(r) for r in frame.collect()]
        self.description = [(f.name,) for f in frame.schema.fields] or None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def run_app(spark, monkeypatch, llm):
    """Run the real app.py; patches stay active for the whole test (monkeypatch undoes them)."""
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_data.clear()  # query() results would otherwise leak between tests
    st.cache_resource.clear()
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "test")
    monkeypatch.setenv("SERVING_ENDPOINT", "test-model")
    monkeypatch.setenv("HLS_SCHEMA", "mimic_iv")
    monkeypatch.chdir(ROOT)
    conn = SimpleNamespace(cursor=lambda: SparkCursor(spark))
    monkeypatch.setattr("databricks.sql.connect", lambda **_: conn)
    monkeypatch.setattr("databricks.sdk.core.Config", mock.MagicMock())
    monkeypatch.setattr("openai.OpenAI", lambda **_: llm)
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()


def fake_llm(calls: list):
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="SUMMARY-OK"))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_app_cohort_timeline_and_summary(spark, iv, monkeypatch):
    calls = []
    at = run_app(spark, monkeypatch, fake_llm(calls))
    assert not at.exception, at.exception
    assert [m.label for m in at.metric][:4] == ["Patients", "Admissions", "In-hospital mortality", "Median length of stay"]
    assert at.metric[1].value == "275"  # default age filter 18-90 covers every demo admission

    at.text_input[0].set_value("sepsis").run()
    assert not at.exception, at.exception
    assert 0 < int(at.metric[1].value) < 275

    next(b for b in at.button if b.label == "Generate summary").click().run()
    assert not at.exception, at.exception
    assert any("SUMMARY-OK" in m.value for m in at.markdown)
    assert calls and "## Diagnoses" in calls[-1]["messages"][1]["content"]


def set_dataset_info(spark, dataset: str, credentialed: bool):
    spark.createDataFrame([{"dataset": dataset, "credentialed": credentialed, "has_notes": False,
                            "license": "test"}]).write.mode("overwrite").saveAsTable("mimic_iv.dataset_info")


def test_app_blocks_llm_for_credentialed_data(spark, iv, monkeypatch):
    set_dataset_info(spark, "mimic-iv", credentialed=True)
    try:
        monkeypatch.delenv(LLM_OVERRIDE_ENV, raising=False)
        at = run_app(spark, monkeypatch, fake_llm([]))
        assert not at.exception, at.exception
        assert not [b for b in at.button if b.label == "Generate summary"]
        assert any("AI summaries are off" in w.value for w in at.warning)
        assert any("Credentialed PhysioNet data" in w.value for w in at.warning)
    finally:
        set_dataset_info(spark, "mimic-iv-demo", credentialed=False)
