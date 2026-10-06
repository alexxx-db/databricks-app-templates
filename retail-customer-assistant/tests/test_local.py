"""Local end-to-end checks: generated data in local Spark, the real app.py and tools on top.

Spark SQL stands in for the SQL warehouse, and a scripted fake stands in for the LLM.
Run from the template directory:

    uv run --with pyspark~=4.0 --with pytest -r requirements.txt pytest tests -q
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "setup")]
from assistant import Tools, return_eligibility, run_turn  # noqa: E402
from generate_data import generate, write_tables  # noqa: E402

TODAY = date.today()


@pytest.fixture(scope="session")
def spark(tmp_path_factory):
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[2]").appName("retail-test")
             .config("spark.sql.warehouse.dir", str(tmp_path_factory.mktemp("wh")))
             .config("spark.sql.ansi.enabled", "true")  # Databricks SQL runs in ANSI mode
             .config("spark.ui.enabled", "false").getOrCreate())
    spark.sql("CREATE DATABASE IF NOT EXISTS retail")
    write_tables(spark, generate(today=TODAY), "retail")
    yield spark
    spark.stop()


@pytest.fixture
def query(spark):
    def run(sql: str, params: dict | None = None) -> list[dict]:
        return [r.asDict() for r in spark.sql(sql, args=params or {}).collect()]
    return run


@pytest.fixture
def tools(query):
    return Tools(query, "retail")


# --- data --------------------------------------------------------------------------
def test_generated_data_is_consistent():
    data = generate(today=TODAY)
    assert data == generate(today=TODAY), "same seed + date must give the same data"
    customer_ids = {c["customer_id"] for c in data["customers"]}
    order_ids = {o["order_id"] for o in data["orders"]}
    product_ids = {p["product_id"] for p in data["products"]}
    assert all(o["customer_id"] in customer_ids for o in data["orders"])
    assert all(i["order_id"] in order_ids and i["product_id"] in product_ids for i in data["order_items"])
    assert all(r["order_id"] in order_ids for r in data["returns"])
    assert len({c["email"] for c in data["customers"]}) == len(data["customers"])
    assert {o["status"] for o in data["orders"]} == {"PLACED", "SHIPPED", "DELIVERED", "CANCELLED"}


# --- policy ------------------------------------------------------------------------
def order(status="DELIVERED", days_ago=10):
    return {"status": status, "delivered_date": TODAY - timedelta(days=days_ago) if status == "DELIVERED" else None}


ITEM = {"returnable": True}
FINAL_SALE = {"returnable": False}


@pytest.mark.parametrize("o, items, tier, has_return, eligible", [
    (order(), [ITEM], "STANDARD", False, True),
    (order(days_ago=31), [ITEM], "STANDARD", False, False),   # past 30 days
    (order(days_ago=31), [ITEM], "GOLD", False, True),        # Gold gets 60
    (order(days_ago=61), [ITEM], "GOLD", False, False),
    (order(status="SHIPPED"), [ITEM], "STANDARD", False, False),
    (order(), [FINAL_SALE], "STANDARD", False, False),
    (order(), [ITEM, FINAL_SALE], "STANDARD", False, True),   # mixed order: still returnable
    (order(), [ITEM], "STANDARD", True, False),               # one return per order
])
def test_return_eligibility(o, items, tier, has_return, eligible):
    assert return_eligibility(o, items, tier, has_return, today=TODAY)["eligible"] is eligible


# --- tools against Spark -------------------------------------------------------------
def first_order(query, where: str) -> str:
    return query(f"SELECT o.order_id FROM retail.orders o JOIN retail.customers c USING (customer_id) "
                 f"WHERE {where} ORDER BY o.order_id LIMIT 1")[0]["order_id"]


def test_find_customer_is_case_insensitive(tools, query):
    email = query("SELECT email FROM retail.customers LIMIT 1")[0]["email"]
    assert tools.find_customer(email.upper())["email"] == email
    assert tools.find_customer("nobody@example.com") is None


def test_create_return_request_enforces_policy(tools, query):
    eligible = first_order(query, f"o.status = 'DELIVERED' AND c.loyalty_tier = 'STANDARD' "
                                  f"AND o.delivered_date >= date'{TODAY - timedelta(days=20)}' "
                                  f"AND o.order_id NOT IN (SELECT order_id FROM retail.returns) "
                                  f"AND o.order_id IN (SELECT i.order_id FROM retail.order_items i "
                                  f"JOIN retail.products p USING (product_id) WHERE p.returnable)")
    created = tools.create_return_request(eligible, "Wrong size")
    assert created["created"], created
    assert query("SELECT status FROM retail.returns WHERE return_id = :r", {"r": created["return_id"]})
    second = tools.create_return_request(eligible, "Again")
    assert not second["created"] and "already" in second["reason"]

    placed = first_order(query, "o.status = 'PLACED'")
    before = query("SELECT count(*) AS n FROM retail.returns")[0]["n"]
    assert not tools.create_return_request(placed, "Changed my mind")["created"]
    assert query("SELECT count(*) AS n FROM retail.returns")[0]["n"] == before


def test_search_products_and_policy(tools):
    assert all(p["price"] <= 50 for p in tools.search_products("tee", max_price=50))
    assert "30 days" in tools.get_policy("returns")


# --- agent loop with a scripted model ------------------------------------------------
def fake_llm(*steps):
    """Each step is ("tool", name, args) or ("text", content)."""
    replies = iter(steps)

    def create(**_):
        step = next(replies)
        if step[0] == "tool":
            call = SimpleNamespace(id="call-1", function=SimpleNamespace(name=step[1], arguments=json.dumps(step[2])))
            msg = SimpleNamespace(content="", tool_calls=[call])
        else:
            msg = SimpleNamespace(content=step[1], tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_run_turn_executes_tools_and_feeds_results_back(tools, query):
    email = query("SELECT email FROM retail.customers LIMIT 1")[0]["email"]
    llm = fake_llm(("tool", "find_customer", {"email": email}), ("text", "Found you."))
    messages = [{"role": "user", "content": "hi"}]
    answer, calls = run_turn(llm, "m", tools, messages)
    assert answer == "Found you."
    assert calls[0]["result"]["email"] == email
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]


def test_run_turn_reports_tool_errors_to_the_model(tools):
    llm = fake_llm(("tool", "get_order", {"wrong_arg": 1}), ("text", "Sorry."))
    _, calls = run_turn(llm, "m", tools, [])
    assert "error" in calls[0]["result"]


# --- the real app.py ---------------------------------------------------------------
class SparkCursor:
    def __init__(self, spark):
        self.spark, self.description, self._rows = spark, None, []

    def execute(self, sql, params=None):
        df = self.spark.sql(sql, args=params or {})
        self._rows = [tuple(r) for r in df.collect()]
        self.description = [(f.name,) for f in df.schema.fields] or None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_app_renders_all_tabs_and_chats(spark, query, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "test")
    monkeypatch.setenv("SERVING_ENDPOINT", "test-model")
    monkeypatch.setenv("RETAIL_SCHEMA", "retail")
    monkeypatch.chdir(ROOT)
    email = query("SELECT email FROM retail.customers LIMIT 1")[0]["email"]
    conn = SimpleNamespace(cursor=lambda: SparkCursor(spark))
    llm = fake_llm(("tool", "find_customer", {"email": email}), ("text", "Hi! I found your account."))
    with mock.patch("databricks.sql.connect", return_value=conn), \
         mock.patch("databricks.sdk.core.Config"), \
         mock.patch("openai.OpenAI", return_value=llm):
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
        assert not at.exception, at.exception
        assert len(at.metric) >= 4  # operations KPIs rendered from Spark

        at.text_input[0].set_value(email).run()
        assert not at.exception, at.exception
        assert any("Return:" in (m.value or "") for m in [*at.success, *at.warning])

        at.chat_input[0].set_value(f"I'm {email}").run()
        assert not at.exception, at.exception
        assert any("found your account" in m.value for m in at.markdown)
