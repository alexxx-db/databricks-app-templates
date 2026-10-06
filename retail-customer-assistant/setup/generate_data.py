"""Generate the retail demo dataset into Unity Catalog and grant the app access.

Runs as a Lakeflow Job task (see databricks.yml). All data is synthetic and
generated here, so nothing third-party is redistributed.

Parameters: --catalog, --schema, --app-name (grants go to that app's service principal).
"""

import argparse
import random
import uuid
from datetime import date, timedelta

CATEGORIES = {
    # category: (price range, returnable, product nouns)
    "Apparel": ((15, 120), True, ["Tee", "Hoodie", "Denim Jacket", "Rain Shell", "Chinos", "Sweater"]),
    "Footwear": ((40, 180), True, ["Trail Runner", "Leather Boot", "Slip-On", "Court Sneaker"]),
    "Home": ((10, 250), True, ["Throw Blanket", "Ceramic Mug Set", "Table Lamp", "Linen Sheets"]),
    "Electronics": ((25, 600), True, ["Wireless Earbuds", "Smart Speaker", "Fitness Tracker", "Power Bank"]),
    "Beauty": ((8, 80), True, ["Face Serum", "Hand Cream", "Hair Oil", "Lip Balm Trio"]),
    "Outdoors": ((20, 400), True, ["Camp Chair", "Daypack", "Insulated Bottle", "2-Person Tent"]),
    "Clearance": ((5, 60), False, ["Mystery Box", "Sample Pack", "Last-Season Tee"]),  # final sale
}
ADJECTIVES = ["Classic", "Everyday", "Pro", "Coastal", "Alpine", "Urban", "Essential", "Heritage"]
FIRST = ["Ava", "Liam", "Mia", "Noah", "Zoe", "Ethan", "Ivy", "Lucas", "Nora", "Omar", "Priya", "Sam"]
LAST = ["Garcia", "Chen", "Patel", "Smith", "Okafor", "Novak", "Rossi", "Kim", "Silva", "Brown"]
CITIES = [("Austin", "TX"), ("Denver", "CO"), ("Seattle", "WA"), ("Chicago", "IL"), ("Atlanta", "GA"),
          ("Boston", "MA"), ("Phoenix", "AZ"), ("Portland", "OR")]
TIERS = [("STANDARD", 0.7), ("SILVER", 0.2), ("GOLD", 0.1)]
RETURN_REASONS = ["Wrong size", "Changed my mind", "Arrived damaged", "Not as described", "Better price elsewhere"]

POLICIES = {
    "returns": (
        "Most items can be returned within 30 days of delivery for a full refund; Gold members get 60 days. "
        "Clearance items are final sale and cannot be returned. Each order can have one return request. "
        "Refunds go back to the original payment method within 5-7 business days of the return being received."
    ),
    "shipping": (
        "Standard shipping takes 3-7 business days and is free on orders over $50 ($5.99 otherwise). "
        "Express shipping takes 1-2 business days for $14.99. Orders placed before 2pm local time ship the same day."
    ),
    "warranty": (
        "Electronics carry a 1-year limited warranty against manufacturing defects. Outdoor gear carries a "
        "2-year warranty. Warranty claims need the order ID and a photo of the defect."
    ),
    "price_adjustment": (
        "If an item's price drops within 14 days of purchase, we refund the difference once per item. "
        "Clearance items and items bought with another promotion are excluded."
    ),
}


def weighted(rng: random.Random, pairs):
    return rng.choices([v for v, _ in pairs], weights=[w for _, w in pairs])[0]


def generate(seed: int = 7, today: date | None = None, n_customers: int = 800, n_orders: int = 4000) -> dict:
    """Return the dataset as {table: list[dict]}. Deterministic for a given seed and date."""
    rng = random.Random(seed)
    today = today or date.today()

    products = []
    for category, ((lo, hi), returnable, nouns) in CATEGORIES.items():
        for noun in nouns:
            for adj in rng.sample(ADJECTIVES, 3):
                products.append({
                    "product_id": len(products) + 1,
                    "name": f"{adj} {noun}",
                    "category": category,
                    "price": round(rng.uniform(lo, hi), 2),
                    "returnable": returnable,
                })

    customers = []
    for i in range(1, n_customers + 1):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        city, state = rng.choice(CITIES)
        customers.append({
            "customer_id": i,
            "name": f"{first} {last}",
            "email": f"{first}.{last}.{i}@example.com".lower(),
            "city": city,
            "state": state,
            "loyalty_tier": weighted(rng, TIERS),
            "signup_date": today - timedelta(days=rng.randint(30, 1500)),
        })

    orders, items, returns = [], [], []
    for i in range(1, n_orders + 1):
        customer = rng.choice(customers)
        order_date = today - timedelta(days=rng.randint(0, 120))
        age = (today - order_date).days
        delivered_date = None
        if rng.random() < 0.03:
            status = "CANCELLED"
        elif age < 2:
            status = "PLACED"
        elif age < 5:
            status = "SHIPPED"
        else:
            status = "DELIVERED"
            delivered_date = min(order_date + timedelta(days=rng.randint(2, 9)), today)
        order_id = f"ORD-{100000 + i}"
        total = 0.0
        for product in rng.sample(products, rng.randint(1, 4)):
            qty = rng.choice([1, 1, 1, 2, 3])
            items.append({"order_id": order_id, "product_id": product["product_id"],
                          "quantity": qty, "unit_price": product["price"]})
            total += qty * product["price"]
        orders.append({
            "order_id": order_id,
            "customer_id": customer["customer_id"],
            "order_date": order_date,
            "status": status,
            "delivered_date": delivered_date,
            "total": round(total, 2),
        })
        if status == "DELIVERED" and rng.random() < 0.08:
            requested = min(delivered_date + timedelta(days=rng.randint(1, 20)), today)
            returns.append({
                "return_id": uuid.UUID(int=rng.getrandbits(128)).hex[:12],
                "order_id": order_id,
                "reason": rng.choice(RETURN_REASONS),
                "status": rng.choice(["REQUESTED", "RECEIVED", "REFUNDED"]),
                "requested_at": requested,
            })

    policies = [{"topic": k, "content": v} for k, v in POLICIES.items()]
    return {"customers": customers, "products": products, "orders": orders,
            "order_items": items, "returns": returns, "policies": policies}


def write_tables(spark, data: dict, schema_fqn: str) -> None:
    for table, rows in data.items():
        spark.createDataFrame(rows).write.mode("overwrite").option("overwriteSchema", "true") \
            .saveAsTable(f"{schema_fqn}.{table}")


def grant_app_access(spark, catalog: str, schema: str, app_name: str) -> str:
    from databricks.sdk import WorkspaceClient

    sp = WorkspaceClient().apps.get(app_name).service_principal_client_id
    for stmt in [
        f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{sp}`",
        f"GRANT USE SCHEMA, SELECT ON SCHEMA `{catalog}`.`{schema}` TO `{sp}`",
        # The assistant files return requests.
        f"GRANT MODIFY ON TABLE `{catalog}`.`{schema}`.returns TO `{sp}`",
    ]:
        spark.sql(stmt)
    return sp


def main() -> None:
    from pyspark.sql import SparkSession

    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--app-name", default="")
    args = parser.parse_args()

    spark = SparkSession.builder.getOrCreate()
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{args.catalog}`.`{args.schema}`")
    data = generate()
    write_tables(spark, data, f"`{args.catalog}`.`{args.schema}`")
    print({table: len(rows) for table, rows in data.items()})
    if args.app_name:
        print(f"Granted access to {grant_app_access(spark, args.catalog, args.schema, args.app_name)}")


if __name__ == "__main__":
    main()
