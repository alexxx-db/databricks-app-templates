"""Customer-service agent: tools over the retail tables plus a tool-calling loop.

`query(sql, params) -> list[dict]` is injected so the same tools run against a
SQL warehouse in the app and against local Spark in tests.
"""

import json
import uuid
from datetime import date, timedelta
from typing import Callable

Query = Callable[[str, dict], list[dict]]

RETURN_WINDOW_DAYS = {"GOLD": 60}
DEFAULT_RETURN_WINDOW_DAYS = 30

SYSTEM_PROMPT = """You are the customer service assistant for an online retailer.
- Before discussing orders, identify the customer with find_customer using their email.
- Only state order, product, or policy facts returned by a tool. If a tool returns nothing, say so.
- Before calling create_return_request, summarize the order and reason and get an explicit "yes" from the customer.
- Keep answers short and friendly. Use the policy tool for questions about returns, shipping, warranty, or price adjustments."""


def return_eligibility(order: dict, items: list[dict], loyalty_tier: str, has_return: bool,
                       today: date | None = None) -> dict:
    """Apply the returns policy to one order. Pure function: no I/O."""
    today = today or date.today()
    if order["status"] != "DELIVERED" or not order.get("delivered_date"):
        return {"eligible": False, "reason": f"Order is {order['status'].lower()}, not delivered yet."}
    if has_return:
        return {"eligible": False, "reason": "A return was already requested for this order."}
    returnable = [i for i in items if i["returnable"]]
    if not returnable:
        return {"eligible": False, "reason": "All items in this order are final sale."}
    window = RETURN_WINDOW_DAYS.get(loyalty_tier, DEFAULT_RETURN_WINDOW_DAYS)
    deadline = order["delivered_date"] + timedelta(days=window)
    if today > deadline:
        return {"eligible": False, "reason": f"The {window}-day return window ended on {deadline}."}
    note = "" if len(returnable) == len(items) else " Final-sale items in the order are excluded."
    return {"eligible": True, "deadline": str(deadline),
            "reason": f"Returnable until {deadline} ({window}-day window).{note}"}


class Tools:
    def __init__(self, query: Query, schema: str):
        self.q = query
        self.s = schema  # catalog.schema

    def find_customer(self, email: str) -> dict | None:
        rows = self.q(f"SELECT customer_id, name, email, city, state, loyalty_tier, signup_date "
                      f"FROM {self.s}.customers WHERE lower(email) = lower(:email)", {"email": email.strip()})
        return rows[0] if rows else None

    def list_recent_orders(self, customer_id: int, limit: int = 5) -> list[dict]:
        return self.q(f"SELECT order_id, order_date, status, delivered_date, total FROM {self.s}.orders "
                      f"WHERE customer_id = :cid ORDER BY order_date DESC LIMIT {int(min(limit, 20))}",
                      {"cid": int(customer_id)})

    def get_order(self, order_id: str) -> dict | None:
        orders = self.q(f"SELECT o.*, c.loyalty_tier, c.email FROM {self.s}.orders o "
                        f"JOIN {self.s}.customers c USING (customer_id) WHERE o.order_id = :oid",
                        {"oid": order_id.strip().upper()})
        if not orders:
            return None
        order = orders[0]
        order["items"] = self.q(f"SELECT p.name, p.category, p.returnable, i.quantity, i.unit_price "
                                f"FROM {self.s}.order_items i JOIN {self.s}.products p USING (product_id) "
                                f"WHERE i.order_id = :oid", {"oid": order["order_id"]})
        order["returns"] = self.q(f"SELECT return_id, reason, status, requested_at FROM {self.s}.returns "
                                  f"WHERE order_id = :oid", {"oid": order["order_id"]})
        return order

    def check_return_eligibility(self, order_id: str) -> dict:
        order = self.get_order(order_id)
        if not order:
            return {"eligible": False, "reason": f"No order {order_id}."}
        return return_eligibility(order, order["items"], order["loyalty_tier"], bool(order["returns"]))

    def create_return_request(self, order_id: str, reason: str) -> dict:
        # Re-check here: the model's say-so is never enough to write.
        check = self.check_return_eligibility(order_id)
        if not check["eligible"]:
            return {"created": False, **check}
        return_id = uuid.uuid4().hex[:12]
        self.q(f"INSERT INTO {self.s}.returns (return_id, order_id, reason, status, requested_at) "
               f"VALUES (:rid, :oid, :reason, 'REQUESTED', current_date())",
               {"rid": return_id, "oid": order_id.strip().upper(), "reason": reason[:200]})
        return {"created": True, "return_id": return_id, "status": "REQUESTED"}

    def search_products(self, query: str, max_price: float | None = None) -> list[dict]:
        sql = (f"SELECT product_id, name, category, price, returnable FROM {self.s}.products "
               f"WHERE (lower(name) LIKE lower(:q) OR lower(category) LIKE lower(:q))")
        params = {"q": f"%{query.strip()}%"}
        if max_price is not None:
            sql += " AND price <= :max_price"
            params["max_price"] = float(max_price)
        return self.q(sql + " ORDER BY price LIMIT 10", params)

    def get_policy(self, topic: str) -> str:
        rows = self.q(f"SELECT content FROM {self.s}.policies WHERE topic = :t", {"t": topic})
        return rows[0]["content"] if rows else f"No policy found for {topic!r}."


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOL_SPECS = [
    _fn("find_customer", "Look up a customer by email.", {"email": {"type": "string"}}, ["email"]),
    _fn("list_recent_orders", "List a customer's most recent orders.",
        {"customer_id": {"type": "integer"}, "limit": {"type": "integer"}}, ["customer_id"]),
    _fn("get_order", "Get an order with its items and any returns.", {"order_id": {"type": "string"}}, ["order_id"]),
    _fn("check_return_eligibility", "Check whether an order can be returned under the returns policy.",
        {"order_id": {"type": "string"}}, ["order_id"]),
    _fn("create_return_request", "File a return request. Only after the customer confirms.",
        {"order_id": {"type": "string"}, "reason": {"type": "string"}}, ["order_id", "reason"]),
    _fn("search_products", "Search products by name or category.",
        {"query": {"type": "string"}, "max_price": {"type": "number"}}, ["query"]),
    _fn("get_policy", "Get store policy text.",
        {"topic": {"type": "string", "enum": ["returns", "shipping", "warranty", "price_adjustment"]}}, ["topic"]),
]


def run_turn(client, model: str, tools: Tools, messages: list[dict], max_steps: int = 6) -> tuple[str, list[dict]]:
    """Run the model until it answers. Returns (answer, tool_calls_made). Appends to `messages`."""
    calls = []
    for _ in range(max_steps):
        resp = client.chat.completions.create(model=model, messages=messages, tools=TOOL_SPECS)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            return msg.content or "", calls
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = getattr(tools, name)(**args) if name in {t["function"]["name"] for t in TOOL_SPECS} \
                    else {"error": f"unknown tool {name}"}
            except Exception as e:  # surface tool errors to the model instead of crashing the chat
                result = {"error": str(e)}
            calls.append({"tool": name, "args": tc.function.arguments, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, default=str)})
    return "Sorry, I couldn't finish that request. Please try rephrasing.", calls
