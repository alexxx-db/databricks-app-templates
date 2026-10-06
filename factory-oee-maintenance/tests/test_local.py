"""Local end-to-end checks: synthetic plant in local Spark, detection, OEE math, work-order guardrails, real app.py.

Spark SQL (ANSI mode) stands in for the SQL warehouse; a fake stands in for the LLM.
Run from the template directory (needs Java 17+):

    uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "setup")]
from generate_data import DEGRADING_NOW, MAINTAINED, SPIKE, WORK_ORDERS_DDL, generate  # noqa: E402
from health import episodes_sql, status_sql  # noqa: E402
from oee import losses_sql, oee_sql  # noqa: E402
from workorder import check_citations, ensure_lockout  # noqa: E402


@pytest.fixture(scope="session")
def data():
    return generate()


@pytest.fixture(scope="session")
def spark(tmp_path_factory, data):
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[2]").appName("factory-test")
             .config("spark.sql.warehouse.dir", str(tmp_path_factory.mktemp("wh")))
             .config("spark.sql.ansi.enabled", "true").config("spark.ui.enabled", "false").getOrCreate())
    spark.sql("CREATE DATABASE IF NOT EXISTS plant")
    for table, rows in data.items():
        spark.createDataFrame(rows).write.mode("overwrite").saveAsTable(f"plant.{table}")
    spark.sql(WORK_ORDERS_DDL.format(s="plant"))
    yield spark
    spark.stop()


# --- asset health -------------------------------------------------------------------
def test_failures_are_caught_days_ahead_and_healthy_machines_stay_quiet(spark, data):
    episodes = spark.sql(episodes_sql("plant")).collect()
    alerted = {e.machine_id for e in episodes}
    assert alerted == {f["machine_id"] for f in data["failures"]} | {DEGRADING_NOW}
    assert MAINTAINED[0] not in alerted and SPIKE[0] not in alerted  # step down + one-hour glitch
    for f in data["failures"]:
        first = min(e.started_at for e in episodes if e.machine_id == f["machine_id"])
        assert f["failed_at"] - first >= timedelta(days=3), f


def test_current_status(spark):
    status = {r.machine_id: r for r in spark.sql(status_sql("plant")).collect()}
    assert status[DEGRADING_NOW].status == "ALERT" and status[DEGRADING_NOW].vibration_x_baseline > 2
    assert all(r.status == "OK" for m, r in status.items() if m != DEGRADING_NOW)  # repaired machines recovered


# --- OEE math -------------------------------------------------------------------------
def test_oee_identity_and_losses_add_up(spark):
    for r in spark.sql(oee_sql("plant", "Machine"), args={"days": 30}).collect():
        assert r.oee_pct == pytest.approx(r.availability_pct * r.performance_pct * r.quality_pct / 10000, abs=0.15)
    lines = spark.sql(oee_sql("plant", "Line"), args={"days": 30}).collect()
    planned = sum(r.planned_min for r in lines)
    k = spark.sql(losses_sql("plant"), args={"days": 30}).first()
    assert sum(k.asDict().values()) == pytest.approx(planned, abs=5)  # every minute accounted for
    assert 55 < 100 * k.productive / planned < 85                      # a plausible plant, not "world class"


def test_oee_rolls_up_from_sums_not_averages(spark):
    """Two machines: a big one at 90% OEE and a small one at 20%. Averaging says 55%; the true plant OEE is ~84%."""
    now = datetime.now()
    spark.sql("CREATE DATABASE IF NOT EXISTS mini")
    spark.createDataFrame([{"machine_id": "A", "line": "X", "ideal_cycle_s": 60.0},
                           {"machine_id": "B", "line": "X", "ideal_cycle_s": 60.0}]).write.mode("overwrite").saveAsTable("mini.machines")
    spark.createDataFrame([
        {"machine_id": "A", "shift_start": now - timedelta(days=1), "planned_min": 1000, "downtime_min": 0,
         "downtime_reason": "Minor stops", "total_units": 900, "scrap_units": 0},   # 900 good min / 1000
        {"machine_id": "B", "shift_start": now - timedelta(days=1), "planned_min": 100, "downtime_min": 0,
         "downtime_reason": "Minor stops", "total_units": 20, "scrap_units": 0},    # 20 good min / 100
    ]).write.mode("overwrite").saveAsTable("mini.shifts")
    line = spark.sql(oee_sql("mini", "Line"), args={"days": 7}).first()
    assert line.oee_pct == pytest.approx(100 * 920 / 1100, abs=0.1)


# --- work-order guardrails ------------------------------------------------------------
MANUAL = [{"section_id": "CNC-1", "title": "Lockout/tagout", "content": "Isolate power and apply your lock."},
          {"section_id": "CNC-3.1", "title": "Spindle bearing wear", "content": "..."}]


def test_lockout_is_always_included():
    plan, added = ensure_lockout("Steps: 1. Replace bearing [CNC-3.1]", MANUAL)
    assert added and plan.startswith("Safety first [CNC-1]")
    plan, added = ensure_lockout("1. Apply lockout per [CNC-1]. 2. Inspect [CNC-3.1]", MANUAL)
    assert not added


def test_invented_citations_are_flagged():
    assert check_citations("See [CNC-3.1] and [CNC-9.9] and [PRS-2.4]", {"CNC-1", "CNC-3.1"}) == ["CNC-9.9", "PRS-2.4"]


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


def run_app(spark, monkeypatch, reply: str):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_data.clear()
    st.cache_resource.clear()
    sent = []

    def create(**kwargs):
        sent.append(kwargs["messages"][1]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    for k, v in {"DATABRICKS_WAREHOUSE_ID": "t", "SERVING_ENDPOINT": "m", "PLANT_SCHEMA": "plant",
                 "LOCAL_USER": "planner.a@example.com"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(ROOT)
    conn = SimpleNamespace(cursor=lambda: SparkCursor(spark))
    monkeypatch.setattr("databricks.sql.connect", lambda **_: conn)
    monkeypatch.setattr("databricks.sdk.core.Config", mock.MagicMock())
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("openai.OpenAI", lambda **_: fake)
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run(), sent


def test_app_oee_health_and_work_order(spark, monkeypatch):
    # The model "forgets" lockout/tagout: the app must add it before the plan can be filed.
    at, sent = run_app(spark, monkeypatch, "Summary: bearing wear.\nSteps: 1. Replace bearing kit SB-200 [PKG-3.4]")
    assert not at.exception, at.exception
    assert at.metric[0].label == "OEE (plant)"

    next(b for b in at.button if b.label == "Draft work order with AI").click().run()
    assert not at.exception, at.exception
    assert sent and "## Manual sections" in sent[-1] and "PKG-1" in sent[-1]   # M11 is a packager
    plan = next(t for t in at.text_area if t.label.startswith("Plan")).value
    assert plan.startswith("Safety first [PKG-1]")
    next(b for b in at.button if b.label == "File work order").click().run()
    assert not at.exception, at.exception
    wo = spark.sql("SELECT * FROM plant.work_orders").collect()
    assert len(wo) == 1 and wo[0].machine_id == DEGRADING_NOW and wo[0].priority == "HIGH"
    assert wo[0].created_by == "planner.a@example.com" and wo[0].plan.startswith("Safety first")


def test_app_blocks_filing_with_invented_citations(spark, monkeypatch):
    at, _ = run_app(spark, monkeypatch, "Lockout per [PKG-1]. Replace part per [PKG-9.9].")
    next(b for b in at.button if b.label == "Draft work order with AI").click().run()
    assert any("PKG-9.9" in w.value for w in at.warning)
    assert next(b for b in at.button if b.label == "File work order").disabled
