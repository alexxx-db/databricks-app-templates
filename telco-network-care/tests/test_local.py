"""Local end-to-end checks: synthetic network in local Spark, incident detection, credit policy, SMS validation, real app.py.

Spark SQL (ANSI mode) stands in for the SQL warehouse; a fake stands in for the LLM.
Run from the template directory (needs Java 17+):

    uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
"""

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "setup")]
from care import churn_risk, credit_for, render, template, validate_draft  # noqa: E402
from generate_data import OUTREACH_DDL, generate  # noqa: E402
from network import impact_sql, incidents_sql  # noqa: E402


@pytest.fixture(scope="session")
def data():
    return generate()


@pytest.fixture(scope="session")
def spark(tmp_path_factory, data):
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[2]").appName("telco-test")
             .config("spark.sql.warehouse.dir", str(tmp_path_factory.mktemp("wh")))
             .config("spark.sql.ansi.enabled", "true").config("spark.ui.enabled", "false").getOrCreate())
    spark.sql("CREATE DATABASE IF NOT EXISTS net")
    for table, rows in data.items():
        spark.createDataFrame(rows).write.mode("overwrite").saveAsTable(f"net.{table}")
    spark.sql(OUTREACH_DDL.format(s="net"))
    yield spark
    spark.stop()


@pytest.fixture(scope="session")
def incidents(spark):
    return [r.asDict() for r in spark.sql(incidents_sql("net")).collect()]


# --- detection --------------------------------------------------------------------
def test_detected_incidents_match_planted_ones_exactly(data, incidents):
    key = lambda x: (x["site_id"], x["kind"], x["started_at"], x["ended_at"], x["ongoing"])
    assert sorted(map(key, incidents)) == sorted(map(key, data["incidents_truth"]))  # and no blips


def test_repeat_impact(spark, incidents):
    outage = next(i for i in incidents if i["site_id"] == "S030" and i["kind"] == "OUTAGE")
    customers = spark.sql(impact_sql("net"), args={"incident": outage["incident_id"]}).collect()
    assert customers and all(c.incidents_in_period == 2 for c in customers)  # same site degraded later


# --- policy --------------------------------------------------------------------------
@pytest.mark.parametrize("plan, charge, kind, minutes, credit", [
    ("POSTPAID", 60.0, "OUTAGE", 59, 0.0),          # under an hour: nothing
    ("POSTPAID", 60.0, "OUTAGE", 90, 2.0),          # 1 day of service
    ("POSTPAID", 60.0, "OUTAGE", 360, 4.0),         # 6 h -> 2 started 4-hour blocks
    ("BUSINESS", 150.0, "OUTAGE", 240, 10.0),       # business doubles
    ("PREPAID", 30.0, "OUTAGE", 24 * 60 * 20, 15.0),  # capped at half the monthly charge
    ("POSTPAID", 60.0, "DEGRADATION", 600, 0.0),    # no automatic credit
])
def test_credit_policy(plan, charge, kind, minutes, credit):
    assert credit_for(plan, charge, kind, minutes) == credit


def test_churn_risk():
    assert churn_risk({"incidents_in_period": 2, "opened_ticket": False}) == "HIGH"
    assert churn_risk({"incidents_in_period": 1, "opened_ticket": True}) == "HIGH"
    assert churn_risk({"incidents_in_period": 1, "opened_ticket": False}) == "NORMAL"


def test_sms_validation():
    assert validate_draft("Sorry for the outage. A {credit} credit is on your account.", True, 9.99) == []
    assert "states a money amount itself" in validate_draft("Here is $5 off. {credit}", True, 1.0)
    assert any("exactly once" in p for p in validate_draft("Sorry for the outage.", True, 1.0))
    assert any("none applies" in p for p in validate_draft("Sorry. {credit} credit.", False, 0.0))
    assert any("160" in p for p in validate_draft("x" * 155 + " {credit}", True, 99.99))
    inc = {"kind": "OUTAGE", "area": "Harborview", "started_at": datetime(2026, 9, 30, 14), "ongoing": False,
           "ended_at": datetime(2026, 9, 30, 20)}
    assert validate_draft(template(inc, True), True, 70.0) == []   # the fallback itself always passes
    assert render("a {credit} credit", 2.5) == "a $2.50 credit"


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

    def create(**_):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    for k, v in {"DATABRICKS_WAREHOUSE_ID": "t", "SERVING_ENDPOINT": "m", "TELCO_SCHEMA": "net",
                 "LOCAL_USER": "agent.a@example.com"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(ROOT)
    conn = SimpleNamespace(cursor=lambda: SparkCursor(spark))
    monkeypatch.setattr("databricks.sql.connect", lambda **_: conn)
    monkeypatch.setattr("databricks.sdk.core.Config", mock.MagicMock())
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("openai.OpenAI", lambda **_: fake)
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()


def test_app_rejects_model_amounts_and_queues_policy_credits(spark, incidents, monkeypatch):
    at = run_app(spark, monkeypatch, "Sorry about the outage! We're giving you $50 off your next bill.")
    assert not at.exception, at.exception
    assert at.metric[0].label == "Ongoing incidents" and at.metric[0].value == "1"
    assert any("Ongoing degradation" in e.value for e in at.error)

    outage = next(i for i in incidents if i["site_id"] == "S004")
    next(s for s in at.selectbox if s.key == "care_incident").set_value(outage).run()
    at.radio[0].set_value("All affected customers").run()
    next(b for b in at.button if b.label == "Draft SMS with AI").click().run()
    assert not at.exception, at.exception
    assert any("rejected" in w.value and "money amount" in w.value for w in at.warning)  # fell back to template
    next(b for b in at.button if b.label == "Approve and queue").click().run()
    assert not at.exception, at.exception

    queued = spark.sql("SELECT o.*, c.plan, c.monthly_charge FROM net.outreach o JOIN net.customers c USING (customer_id) "
                       "WHERE incident_id = :i", args={"i": outage["incident_id"]}).collect()
    affected = spark.sql(impact_sql("net"), args={"incident": outage["incident_id"]}).count()
    assert len(queued) == affected > 0
    for q in queued:
        expected = credit_for(q.plan, q.monthly_charge, "OUTAGE", outage["duration_min"])
        assert q.credit == expected and f"${expected:.2f}" in q.message and "$50" not in q.message
        assert len(q.message) <= 160 and q.approved_by == "agent.a@example.com"

    # Re-running for the same incident doesn't contact anyone twice.
    at.run()
    assert any("0** customers to contact" in m.value for m in at.markdown)


def test_app_uses_a_valid_ai_draft(spark, incidents, monkeypatch):
    at = run_app(spark, monkeypatch, "We're sorry for the outage in your area. It's fixed, and a {credit} credit is on your account.")
    outage = next(i for i in incidents if i["site_id"] == "S018")
    next(s for s in at.selectbox if s.key == "care_incident").set_value(outage).run()
    next(b for b in at.button if b.label == "Draft SMS with AI").click().run()
    assert not at.exception, at.exception
    assert not any("rejected" in w.value for w in at.warning)
    assert next(t for t in at.text_area if t.label.startswith("Message")).value.startswith("We're sorry")
