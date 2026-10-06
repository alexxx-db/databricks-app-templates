"""Local end-to-end checks: synthetic city data in local Spark, the equity metric, intake safety, and the real app.py.

Spark SQL (ANSI mode) stands in for the SQL warehouse; a fake stands in for the LLM.
Run from the template directory (needs Java 17+):

    uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "setup")]
from equity import equity_sql  # noqa: E402
from generate_data import MIX_SKEWED_DISTRICT, SLOW_DISTRICT, generate  # noqa: E402
from triage import parse_triage, redact, update_context  # noqa: E402


@pytest.fixture(scope="session")
def spark(tmp_path_factory):
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[2]").appName("city-test")
             .config("spark.sql.warehouse.dir", str(tmp_path_factory.mktemp("wh")))
             .config("spark.sql.ansi.enabled", "true").config("spark.ui.enabled", "false").getOrCreate())
    spark.sql("CREATE DATABASE IF NOT EXISTS city")
    for table, rows in generate().items():
        spark.createDataFrame(rows).write.mode("overwrite").saveAsTable(f"city.{table}")
    yield spark
    spark.stop()


def equity(spark, schema="city", department="All", threshold=10) -> dict:
    rows = spark.sql(equity_sql(schema), args={"department": department, "threshold": threshold}).collect()
    return {r.district_id: r.asDict() for r in rows}


# --- data + equity ----------------------------------------------------------------
def test_generator_is_deterministic():
    now = datetime(2026, 6, 1)
    assert generate(now=now)["requests"][:20] == generate(now=now)["requests"][:20]


def test_equity_flags_the_slow_district_not_the_mix_skewed_one(spark):
    e = equity(spark)
    assert e[SLOW_DISTRICT]["flagged"], e[SLOW_DISTRICT]
    assert not e[MIX_SKEWED_DISTRICT]["flagged"], e[MIX_SKEWED_DISTRICT]
    assert [d for d, r in e.items() if r["flagged"]] == [SLOW_DISTRICT]
    # The traps: on raw medians the mix-skewed district looks slowest by far, and against the plain citywide
    # on-time rate it would be flagged too. Only the mix adjustment clears it.
    assert max(e.values(), key=lambda r: r["raw_median_days"])["district_id"] == MIX_SKEWED_DISTRICT
    total = sum(r["decided"] for r in e.values())
    city_rate = sum(r["on_time_pct"] * r["decided"] for r in e.values()) / total
    assert e[MIX_SKEWED_DISTRICT]["on_time_pct"] - city_rate < -10


def test_equity_by_department(spark):
    assert equity(spark, department="Streets")[SLOW_DISTRICT]["flagged"]
    assert not equity(spark, department="Parks")[SLOW_DISTRICT]["flagged"]  # Parks service there is normal


def test_equity_arithmetic_and_censoring(spark):
    """Tiny hand-checked case: open-and-not-yet-due requests are excluded, open-and-overdue count as late."""
    now = datetime.now()
    spark.sql("CREATE DATABASE IF NOT EXISTS mini")
    spark.createDataFrame([{"category": "A", "department": "X", "sla_days": 5, "priority": "LOW"}]) \
        .write.mode("overwrite").saveAsTable("mini.categories")
    spark.createDataFrame([{"district_id": 1, "name": "One", "population": 1, "lat": 0.0, "lon": 0.0}]) \
        .write.mode("overwrite").saveAsTable("mini.districts")
    req = lambda rid, created, closed: {"request_id": rid, "district_id": 1, "category": "A",
                                        "created_at": created, "closed_at": closed}
    spark.createDataFrame([
        req("on-time", now - timedelta(days=20), now - timedelta(days=18)),   # closed in 2 of 5 days
        req("late", now - timedelta(days=20), now - timedelta(days=10)),      # closed in 10 of 5 days
        req("overdue", now - timedelta(days=9), None),                        # open past target: late
        req("not-due", now - timedelta(days=1), None),                        # open within target: excluded
    ], "request_id string, district_id long, category string, created_at timestamp, closed_at timestamp") \
        .write.mode("overwrite").saveAsTable("mini.requests")
    r = equity(spark, schema="mini")[1]
    assert r["decided"] == 3 and r["on_time_pct"] == pytest.approx(33.3, abs=0.1)
    assert r["gap_pts"] == 0.0  # only district, so it *is* the citywide rate


# --- intake safety -----------------------------------------------------------------
TAXONOMY = [{"category": "Pothole", "department": "Streets", "priority": "MEDIUM"},
            {"category": "Water Main Leak", "department": "Water", "priority": "HIGH"}]


@pytest.mark.parametrize("text, kinds", [
    ("call 816-555-0142 or (816) 555 0199", ["PHONE", "PHONE"]),
    ("email me: jo.doe+311@mail.example.org", ["EMAIL"]),
    ("my SSN is 123-45-6789", ["SSN"]),
    ("pothole near 1200 Main St on 2026-05-01", []),   # addresses and dates are kept for routing
])
def test_redact(text, kinds):
    clean, found = redact(text)
    assert sorted(found) == sorted(kinds)
    if kinds:
        assert "@" not in clean and not any(ch.isdigit() for ch in clean), clean


def test_parse_triage_validates_model_output():
    ok = parse_triage('Sure! {"category": "Pothole", "priority": "LOW", "confidence": 0.9}', TAXONOMY)
    assert ok["valid"] and ok["department"] == "Streets" and ok["priority"] == "LOW"
    assert not parse_triage('{"category": "Alien Invasion"}', TAXONOMY)["valid"]      # off-list
    assert not parse_triage("not json at all", TAXONOMY)["valid"]
    bad_fields = parse_triage('{"category": "Water Main Leak", "priority": "URGENT", "confidence": "high"}', TAXONOMY)
    assert bad_fields["priority"] == "HIGH" and bad_fields["confidence"] == 0.0     # falls back to defaults


def test_resident_update_excludes_free_text():
    req = {"request_id": "SR-1", "category": "Pothole", "department": "Streets", "status": "OPEN",
           "created_at": datetime(2026, 5, 1), "closed_at": None, "description": "call me at 816-555-0142"}
    assert "816" not in update_context(req, 7)


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


def test_app_operations_equity_and_intake(spark, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_data.clear()
    st.cache_resource.clear()
    sent = []

    def create(**kwargs):
        sent.append(kwargs["messages"][1]["content"])
        if "route city 311" in kwargs["messages"][0]["content"]:
            content = json.dumps({"category": "Pothole", "priority": "MEDIUM", "location": "Main St near 1200",
                                  "summary": "Pothole reported.", "confidence": 0.92})
        else:
            content = "We received your report and crews are scheduled."
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    for k, v in {"DATABRICKS_WAREHOUSE_ID": "t", "SERVING_ENDPOINT": "m", "CITY_SCHEMA": "city"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(ROOT)
    conn = SimpleNamespace(cursor=lambda: SparkCursor(spark))
    monkeypatch.setattr("databricks.sql.connect", lambda **_: conn)
    monkeypatch.setattr("databricks.sdk.core.Config", mock.MagicMock())
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("openai.OpenAI", lambda **_: fake)

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    assert not at.exception, at.exception
    assert [m.label for m in at.metric][:4] == ["Open requests", "Opened in the last 7 days", "Open and overdue",
                                                "On time (last 30 days)"]
    assert any("Eastside" in w.value for w in at.warning)          # equity flag rendered

    # Intake: personal data never reaches the model, and the filed request stores redacted text.
    next(t for t in at.text_area if t.label == "Resident's message").set_value(
        "Huge pothole on Main St near 1200. Call me at 816-555-0142.").run()
    next(b for b in at.button if b.label == "Suggest routing").click().run()
    assert not at.exception, at.exception
    assert sent and "816-555-0142" not in sent[-1] and "[PHONE]" in sent[-1]
    next(b for b in at.button if b.label == "File request").click().run()
    assert not at.exception, at.exception
    filed = spark.sql("SELECT * FROM city.requests WHERE request_id LIKE 'SR-NEW-%'").collect()
    assert len(filed) == 1 and filed[0].category == "Pothole" and "816" not in filed[0].description

    # Lookup + resident update in Spanish.
    rid = spark.sql("SELECT request_id FROM city.requests WHERE status = 'OPEN' LIMIT 1").first().request_id
    next(t for t in at.text_input if t.label == "Request ID").set_value(rid).run()
    next(s for s in at.selectbox if s.label == "Update language").set_value("es").run()
    next(b for b in at.button if b.label == "Draft resident update").click().run()
    assert not at.exception, at.exception
    assert rid in sent[-1] and any("crews are scheduled" in (t.value or "") for t in at.text_area)
