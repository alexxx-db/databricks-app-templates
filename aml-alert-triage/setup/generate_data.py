"""Generate synthetic banking customers, accounts and transactions with planted AML typologies.

Normal behavior per segment, plus a few customers per typology (recorded in `ground_truth`,
for demo evaluation only) and benign look-alikes so the rules produce realistic false positives.
All data is synthetic.

Parameters: --catalog, --schema.
"""

import argparse
import random
from datetime import date, datetime, timedelta

HISTORY_DAYS = 180
# Illustrative list for the demo. Use your institution's jurisdiction-risk list in practice.
HIGH_RISK_COUNTRIES = ["IR", "KP", "MM", "SY", "YE"]
LOW_RISK_COUNTRIES = ["CA", "GB", "DE", "FR", "JP", "MX", "AU", "NL"]
TYPOLOGIES = ["STRUCTURING", "RAPID_MOVEMENT", "HIGH_RISK_JURISDICTION", "DORMANT_REACTIVATION", "ROUND_AMOUNTS"]

FIRST = ["Ana", "Ben", "Chloe", "Dev", "Elena", "Femi", "Grace", "Hiro", "Isla", "Jon", "Kara", "Leo", "Maya", "Nate"]
LAST = ["Alvarez", "Brooks", "Cohen", "Diallo", "Evans", "Fischer", "Gupta", "Haddad", "Ito", "Jensen", "Kowalski"]
BUSINESS = ["Bakery", "Auto Repair", "Dental", "Logistics", "Imports", "Cafe", "Salon", "Hardware", "Consulting"]
OCCUPATIONS = ["Teacher", "Engineer", "Nurse", "Retired", "Student", "Sales", "Contractor", "Accountant"]


class Gen:
    def __init__(self, seed: int, today: date):
        self.rng = random.Random(seed)
        self.end = datetime.combine(today, datetime.min.time())
        self.start = self.end - timedelta(days=HISTORY_DAYS)
        self.txns: list[dict] = []

    def ts(self, day: float) -> datetime:
        """day = days after the start of history (fractional for time of day)."""
        return self.start + timedelta(days=day)

    def add(self, cust: dict, day: float, type_: str, amount: float, cp: str = "", country: str = "US",
            channel: str | None = None):
        self.txns.append({
            "txn_id": f"T{len(self.txns) + 1:07d}",
            "account_id": cust["account_id"],
            "customer_id": cust["customer_id"],
            "ts": self.ts(day),
            "type": type_,
            "amount": round(float(amount), 2),
            "counterparty": cp,
            "counterparty_country": country,
            "channel": channel or {"CARD": "POS", "CASH_DEPOSIT": "BRANCH", "CASH_WITHDRAWAL": "ATM"}.get(type_, "ONLINE"),
        })

    def daytime(self, day: int) -> float:
        return day + self.rng.uniform(0.35, 0.8)

    # --- normal behavior --------------------------------------------------------------
    def retail(self, c: dict, quiet: range | None = None):
        r = self.rng
        salary = r.uniform(2000, 7000)
        for day in range(HISTORY_DAYS):
            if quiet and day in quiet:
                continue
            if day % 14 == 0:
                self.add(c, day + 0.3, "ACH_IN", salary * r.uniform(0.97, 1.03), "Employer Payroll")
            for _ in range(r.choices([0, 1, 2], [0.55, 0.35, 0.10])[0]):
                self.add(c, self.daytime(day), "CARD", r.uniform(5, 250), r.choice(["Grocer", "Fuel", "Online Store", "Restaurant"]))
            if r.random() < 0.15:
                self.add(c, self.daytime(day), "ACH_OUT", r.uniform(50, 1800), r.choice(["Utility", "Mortgage", "Insurance", "Card Payment"]))
            if r.random() < 0.05:
                self.add(c, self.daytime(day), "CASH_WITHDRAWAL", r.choice([40, 60, 100, 200, 300]))
            if r.random() < 0.004:
                self.add(c, self.daytime(day), "WIRE_OUT", r.uniform(1000, 4000), "Family", r.choice(LOW_RISK_COUNTRIES))

    def business(self, c: dict, lookalike: str | None = None):
        r = self.rng
        for day in range(HISTORY_DAYS):
            if r.random() < 0.4:
                # Benign look-alike: a cash-heavy business that sometimes banks just under $10k.
                hi = 9800 if lookalike == "cash" and r.random() < 0.25 else 7500
                self.add(c, self.daytime(day), "CASH_DEPOSIT", r.uniform(300, hi))
            if r.random() < 0.3:
                self.add(c, self.daytime(day), "ACH_IN", r.uniform(500, 12000), "Customer Payment")
            if r.random() < 0.06:
                amount = r.choice([5000, 10000, 15000]) if lookalike == "round" else r.uniform(2000, 30000)
                country = r.choice(HIGH_RISK_COUNTRIES) if lookalike == "trade" and r.random() < 0.3 \
                    else r.choice(["US"] * 6 + LOW_RISK_COUNTRIES)
                self.add(c, self.daytime(day), "WIRE_OUT", amount, "Supplier", country)
            if r.random() < 0.2:
                self.add(c, self.daytime(day), "ACH_OUT", r.uniform(200, 8000), r.choice(["Payroll", "Rent", "Tax"]))

    # --- typologies -------------------------------------------------------------------
    def plant(self, c: dict, typology: str):
        r = self.rng
        day = r.randint(100, HISTORY_DAYS - 10)
        if typology == "STRUCTURING":
            for d in sorted(r.sample(range(6), r.randint(4, 5))):
                self.add(c, self.daytime(day + d), "CASH_DEPOSIT", r.uniform(8200, 9900))
        elif typology == "RAPID_MOVEMENT":
            amount = r.uniform(30000, 150000)
            self.add(c, day + 0.4, "WIRE_IN", amount, "Unrelated LLC", r.choice(LOW_RISK_COUNTRIES))
            out_share = r.uniform(0.85, 0.98)
            parts = r.randint(1, 3)
            for i in range(parts):
                self.add(c, day + 0.45 + r.uniform(0.05, 1.4), "WIRE_OUT", amount * out_share / parts,
                         "Offshore Holdings", r.choice(LOW_RISK_COUNTRIES))
        elif typology == "HIGH_RISK_JURISDICTION":
            for _ in range(r.randint(2, 4)):
                self.add(c, self.daytime(day + r.randint(0, 20) - 10), "WIRE_OUT", r.uniform(3000, 25000),
                         "Trading Co", r.choice(HIGH_RISK_COUNTRIES))
        elif typology == "ROUND_AMOUNTS":
            for d in sorted(r.sample(range(25), r.randint(6, 8))):
                self.add(c, self.daytime(day - 15 + d), r.choice(["WIRE_OUT", "ACH_OUT"]),
                         r.choice([5000, 10000, 20000]), "Consulting Ltd", r.choice(["US"] + LOW_RISK_COUNTRIES))
        elif typology == "DORMANT_REACTIVATION":
            day = r.randint(HISTORY_DAYS - 25, HISTORY_DAYS - 8)
            amount = r.uniform(30000, 80000)
            self.add(c, day + 0.4, "WIRE_IN", amount, "Unknown Sender", r.choice(LOW_RISK_COUNTRIES))
            self.add(c, day + 3.5, "CASH_WITHDRAWAL", amount * r.uniform(0.3, 0.5))
            self.add(c, day + 4.5, "WIRE_OUT", amount * r.uniform(0.3, 0.5), "New Payee", "US")


def generate(seed: int = 11, today: date | None = None, n_customers: int = 1000) -> dict:
    today = today or date.today()
    g = Gen(seed, today)
    r = g.rng
    customers, accounts, truth = [], [], []
    for i in range(1, n_customers + 1):
        is_business = r.random() < 0.15
        name = f"{r.choice(LAST)} {r.choice(BUSINESS)}" if is_business else f"{r.choice(FIRST)} {r.choice(LAST)}"
        c = {
            "customer_id": i,
            "account_id": f"AC{100000 + i}",
            "name": name,
            "segment": "SMALL_BUSINESS" if is_business else "RETAIL",
            "occupation": "Business owner" if is_business else r.choice(OCCUPATIONS),
            "country": "US",
            "risk_rating": r.choices(["LOW", "MEDIUM", "HIGH"], [0.7, 0.22, 0.08])[0],
            "pep": r.random() < 0.01,
            "onboarded": today - timedelta(days=r.randint(200, 4000)),
        }
        customers.append(c)
        accounts.append({"account_id": c["account_id"], "customer_id": i,
                         "product": "Business Checking" if is_business else "Checking",
                         "opened": c["onboarded"]})

    # Assign roles: ~12 customers per typology, plus benign look-alikes among businesses.
    pool = r.sample(range(n_customers), n_customers)
    planted = {t: pool[k * 12:(k + 1) * 12] for k, t in enumerate(TYPOLOGIES)}
    role = {idx: t for t, idxs in planted.items() for idx in idxs}
    businesses = [i for i, c in enumerate(customers) if c["segment"] == "SMALL_BUSINESS" and i not in role]
    lookalike = {i: kind for i, kind in zip(r.sample(businesses, min(24, len(businesses))), ["cash", "round", "trade"] * 8)}

    for idx, c in enumerate(customers):
        typology = role.get(idx)
        if typology == "DORMANT_REACTIVATION":
            # Quiet for months before the burst (plant() puts it in the last ~25 days).
            g.retail(c, quiet=range(40, HISTORY_DAYS))
        elif c["segment"] == "SMALL_BUSINESS":
            g.business(c, lookalike.get(idx))
        else:
            g.retail(c)
        if typology:
            g.plant(c, typology)
            truth.append({"customer_id": c["customer_id"], "typology": typology})

    for c in customers:
        del c["account_id"]
    g.txns.sort(key=lambda t: t["ts"])
    return {"customers": customers, "accounts": accounts, "transactions": g.txns, "ground_truth": truth}


def main() -> None:
    from pyspark.sql import SparkSession

    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    args = p.parse_args()

    spark = SparkSession.builder.getOrCreate()
    fqn = f"`{args.catalog}`.`{args.schema}`"
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {fqn}")
    data = generate()
    for table, rows in data.items():
        spark.createDataFrame(rows).write.mode("overwrite").option("overwriteSchema", "true") \
            .saveAsTable(f"{fqn}.{table}")
        print(f"{table}: {len(rows)} rows")


if __name__ == "__main__":
    main()
