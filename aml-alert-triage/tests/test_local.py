"""Local end-to-end checks: synthetic data + detection in local Spark, the workflow, and the real app.py.

Spark SQL (ANSI mode) stands in for the SQL warehouse; a fake stands in for the LLM.
Run from the template directory (needs Java 17+):

    uv run --no-project --with "pyspark~=4.0" --with pytest --with-requirements requirements.txt pytest tests -q
"""

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "setup")]
from detect import DISPOSITION_COLUMNS, DISPOSITIONS_DDL, RULES, rules_catalog, run_rules, seed_history  # noqa: E402
from generate_data import TYPOLOGIES, generate  # noqa: E402
from investigation import allowed_actions, apply, current_status, get_history, record_decision  # noqa: E402

TODAY = date.today()
RATIONALE = "Reviewed the evidence against the customer profile."


@pytest.fixture(scope="session")
def data():
    return generate(today=TODAY)


@pytest.fixture(scope="session")
def spark(tmp_path_factory, data):
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[2]").appName("aml-test")
             .config("spark.sql.warehouse.dir", str(tmp_path_factory.mktemp("wh")))
             .config("spark.sql.ansi.enabled", "true").config("spark.ui.enabled", "false").getOrCreate())
    spark.sql("CREATE DATABASE IF NOT EXISTS aml")
    for table, rows in data.items():
        spark.createDataFrame(rows).write.mode("overwrite").saveAsTable(f"aml.{table}")
    run_rules(spark, "aml").write.mode("overwrite").saveAsTable("aml.alerts")
    rules_catalog(spark).write.mode("overwrite").saveAsTable("aml.rules")
    spark.sql(DISPOSITIONS_DDL.format(s="aml"))
    yield spark
    spark.stop()


@pytest.fixture
def query(spark):
    def run(sql: str, params: dict | None = None) -> list[dict]:
        return [r.asDict() for r in spark.sql(sql, args=params or {}).collect()]
    return run


@pytest.fixture(scope="session")
def alerts(spark):
    return [r.asDict() for r in spark.table("aml.alerts").collect()]


# --- data + detection ------------------------------------------------------------------
def test_generator_is_deterministic_and_consistent(data):
    assert data["transactions"][:50] == generate(today=TODAY)["transactions"][:50]
    customers = {c["customer_id"] for c in data["customers"]}
    assert {t["customer_id"] for t in data["transactions"]} <= customers
    assert {g["typology"] for g in data["ground_truth"]} == set(TYPOLOGIES)
    assert all(isinstance(t["amount"], float) for t in data["transactions"])  # Spark can't merge int + float


@pytest.mark.parametrize("rule_id", [r["rule_id"] for r in RULES])
def test_every_planted_pattern_is_detected(data, alerts, rule_id):
    planted = {g["customer_id"] for g in data["ground_truth"] if g["typology"] == rule_id}
    flagged = {a["customer_id"] for a in alerts if a["rule_id"] == rule_id}
    assert planted <= flagged, f"missed: {planted - flagged}"


def test_alerts_are_explainable_and_bounded(spark, data, alerts):
    assert len(alerts) < 0.15 * len(data["customers"])  # an alert queue a team could work
    assert all(0 < a["score"] <= 100 and a["evidence_txn_ids"] for a in alerts)
    # Every evidence transaction belongs to the alerted customer.
    bad = spark.sql("""SELECT e.alert_id FROM (SELECT alert_id, customer_id, explode(evidence_txn_ids) txn_id
                                             FROM aml.alerts) e
                       JOIN aml.transactions t ON t.txn_id = e.txn_id WHERE t.customer_id != e.customer_id""").count()
    assert bad == 0


# --- workflow ------------------------------------------------------------------------
def h(action, status, actor):
    return {"action": action, "new_status": status, "actor": actor}


def test_state_machine_and_maker_checker():
    hist = []
    assert current_status(hist) == "OPEN"
    assert apply(hist, "ESCALATE", "ana", RATIONALE) == "ESCALATED"
    hist = [h("ESCALATE", "ESCALATED", "ana")]
    assert allowed_actions(hist, "ana") == []                    # can't review own escalation
    assert set(allowed_actions(hist, "ben")) == {"APPROVE_SAR", "RETURN"}
    with pytest.raises(ValueError, match="Maker-checker"):
        apply(hist, "APPROVE_SAR", "ana", RATIONALE)
    assert apply(hist, "RETURN", "ben", RATIONALE) == "RETURNED"
    hist.append(h("RETURN", "RETURNED", "ben"))
    assert "ESCALATE" in allowed_actions(hist, "ana")            # back with the analyst
    with pytest.raises(ValueError, match="Rationale"):
        apply([], "CLOSE_FALSE_POSITIVE", "ana", "too short")
    with pytest.raises(ValueError, match="Can't"):
        apply([h("CLOSE_FALSE_POSITIVE", "CLOSED_FALSE_POSITIVE", "ana")], "ESCALATE", "ana", RATIONALE)


def test_seeded_history_obeys_the_workflow(data, alerts):
    truth = {(g["customer_id"], g["typology"]) for g in data["ground_truth"]}
    history = seed_history(alerts, truth)
    assert history
    by_alert: dict[str, list] = {}
    for d in history:
        by_alert.setdefault(d["alert_id"], []).append(d)
    for decisions in by_alert.values():
        replay = []
        for d in decisions:  # replaying through apply() raises if any step was illegal
            assert apply(replay, d["action"], d["actor"], d["rationale"]) == d["new_status"]
            replay.append(d)
        assert [d["decided_at"] for d in decisions] == sorted(d["decided_at"] for d in decisions)


def test_record_decision_appends_to_the_audit_log(query, alerts):
    alert_id = alerts[0]["alert_id"]
    assert record_decision(query, "aml", alert_id, "ESCALATE", "ana@x.com", RATIONALE, ai_draft_used=True) == "ESCALATED"
    with pytest.raises(ValueError, match="Maker-checker"):
        record_decision(query, "aml", alert_id, "APPROVE_SAR", "ana@x.com", RATIONALE, False)
    assert record_decision(query, "aml", alert_id, "APPROVE_SAR", "ben@x.com", RATIONALE, False) == "SAR_APPROVED"
    hist = get_history(query, "aml", alert_id)
    assert [x["new_status"] for x in hist] == ["ESCALATED", "SAR_APPROVED"]
    assert hist[0]["ai_draft_used"] is True


# --- the real app.py -----------------------------------------------------------------
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


def fake_llm(calls):
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="DRAFT narrative ..."))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_app_triage_flow_with_maker_checker(spark, alerts, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_data.clear()
    st.cache_resource.clear()
    calls = []
    for k, v in {"DATABRICKS_WAREHOUSE_ID": "t", "SERVING_ENDPOINT": "m", "AML_SCHEMA": "aml",
                 "LOCAL_USER": "analyst.a@example.com"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.chdir(ROOT)
    conn = SimpleNamespace(cursor=lambda: SparkCursor(spark))
    monkeypatch.setattr("databricks.sql.connect", lambda **_: conn)
    monkeypatch.setattr("databricks.sdk.core.Config", mock.MagicMock())
    monkeypatch.setattr("openai.OpenAI", lambda **_: fake_llm(calls))

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    assert not at.exception, at.exception
    assert [m.label for m in at.metric][:4] == ["Open alerts", "Awaiting SAR review", "Overdue (> 30 days)", "SARs approved"]

    # Pick an alert with no decisions yet and open it.
    decided = {r.alert_id for r in spark.table("aml.alert_dispositions").select("alert_id").distinct().collect()}
    target = next(a["alert_id"] for a in sorted(alerts, key=lambda a: -a["score"]) if a["alert_id"] not in decided)
    at.selectbox[0].set_value(target).run()
    next(b for b in at.button if b.label == "Open investigation").click().run()
    assert not at.exception, at.exception
    assert any(target in s.value for s in at.subheader)

    next(b for b in at.button if b.label == "Draft narrative with AI").click().run()
    assert calls and "## Flagged transactions" in calls[-1]["messages"][1]["content"]

    # Analyst A escalates.
    at.radio[0].set_value("ESCALATE")
    rationale_box = next(t for t in at.text_area if t.label.startswith("Rationale"))
    rationale_box.set_value("Four sub-threshold cash deposits in five days; no business purpose.")
    next(b for b in at.button if b.label == "Record decision").click().run()
    assert not at.exception, at.exception
    rows = spark.sql("SELECT actor, new_status, ai_draft_used FROM aml.alert_dispositions WHERE alert_id = :a",
                     args={"a": target}).collect()
    assert [(r.actor, r.new_status, r.ai_draft_used) for r in rows] == [("analyst.a@example.com", "ESCALATED", True)]
    assert any("different reviewer" in i.value for i in at.info)   # A can't approve their own escalation

    # Reviewer B approves.
    monkeypatch.setenv("LOCAL_USER", "lead.b@example.com")
    at.run()
    at.radio[0].set_value("APPROVE_SAR")
    next(t for t in at.text_area if t.label.startswith("Rationale")).set_value("Evidence supports filing; narrative reviewed.")
    next(b for b in at.button if b.label == "Record decision").click().run()
    assert not at.exception, at.exception
    assert spark.sql("SELECT max_by(new_status, decided_at) s FROM aml.alert_dispositions WHERE alert_id = :a",
                     args={"a": target}).first().s == "SAR_APPROVED"
