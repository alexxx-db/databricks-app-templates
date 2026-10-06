"""Generate a synthetic mobile network: cell sites, 15-minute KPIs, customers, and support tickets.

Planted: four outages and two degradations (one ongoing now, one at a site that also had an
outage, so its customers are hit twice), plus five single-interval KPI blips that should *not*
become incidents. Ticket volume rises for affected customers. All data is synthetic; the metro
area and coordinates are illustrative.

Parameters: --catalog, --schema, --app-name.
"""

import argparse
import math
import random
from datetime import datetime, timedelta

HISTORY_DAYS = 14
STEP = timedelta(minutes=15)
N_SITES = 60
N_CUSTOMERS = 6000
AREAS = ["Downtown", "Midtown", "University", "Airport", "Harborview", "Northside", "Lakeshore", "Westgate"]
PLANS = [("PREPAID", 0.40, 25.0), ("POSTPAID", 0.50, 65.0), ("BUSINESS", 0.10, 140.0)]
FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Riley", "Casey", "Morgan", "Jamie", "Avery", "Quinn", "Drew", "Reese"]

# (site index, start hours ago, duration hours, kind). Hours ago from "now".
INCIDENTS = [
    (3, 300, 4.0, "OUTAGE"),
    (17, 220, 1.5, "OUTAGE"),
    (29, 140, 6.0, "OUTAGE"),
    (42, 60, 2.5, "OUTAGE"),
    (29, 30, 3.0, "DEGRADATION"),   # same site as an outage: repeat impact
    (51, 2, 2.0, "DEGRADATION"),    # still ongoing at "now"
]
BLIPS = [(8, 250), (21, 180), (35, 96), (47, 40), (55, 12)]  # (site index, hours ago): one bad interval each


def generate(seed: int = 21, now: datetime | None = None) -> dict:
    rng = random.Random(seed)
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    now = now - timedelta(minutes=now.minute % 15)
    start = now - timedelta(days=HISTORY_DAYS)
    steps = HISTORY_DAYS * 96

    sites = []
    for i in range(N_SITES):
        area = AREAS[i % len(AREAS)]
        sites.append({"site_id": f"S{i + 1:03d}", "area": area, "technology": rng.choice(["4G", "5G", "5G"]),
                      "lat": round(41.88 + rng.uniform(-0.12, 0.12), 5), "lon": round(-87.63 + rng.uniform(-0.15, 0.15), 5)})

    def step_of(hours_ago: float) -> int:
        return steps - int(hours_ago * 4)

    bad = {}  # (site index, step) -> kind
    truth = []
    for idx, ago, dur, kind in INCIDENTS:
        s0 = step_of(ago)
        s1 = min(s0 + int(dur * 4), steps)
        for s in range(s0, s1):
            bad[(idx, s)] = kind
        truth.append({"site_id": sites[idx]["site_id"], "kind": kind, "started_at": start + s0 * STEP,
                      "ended_at": start + s1 * STEP, "ongoing": s0 + int(dur * 4) >= steps})
    for idx, ago in BLIPS:
        bad.setdefault((idx, step_of(ago)), "BLIP")

    kpis = []
    for i, site in enumerate(sites):
        cap = 110.0 if site["technology"] == "5G" else 45.0
        for s in range(steps):
            ts = start + s * STEP
            hour = ts.hour + ts.minute / 60
            load = 0.35 + 0.65 * max(0.0, math.sin(math.pi * (hour - 6) / 16))  # busy 06:00-22:00
            avail, dcr = 100.0, max(0.05, rng.gauss(0.45, 0.12))
            kind = bad.get((i, s))
            if kind == "OUTAGE":
                avail, dcr = 0.0, 0.0
            elif kind == "DEGRADATION":
                dcr = rng.uniform(3.0, 6.0)
            elif kind == "BLIP":
                avail, dcr = (rng.uniform(70, 90), dcr) if rng.random() < 0.5 else (100.0, rng.uniform(2.5, 4.0))
            users = 0 if kind == "OUTAGE" else int(400 * load * rng.uniform(0.85, 1.15))
            kpis.append({"site_id": site["site_id"], "ts": ts, "availability_pct": round(avail, 2),
                         "drop_call_rate_pct": round(dcr, 3), "users": users,
                         "throughput_mbps": 0.0 if kind == "OUTAGE" else round(cap * (1.1 - 0.5 * load) * rng.uniform(0.9, 1.1), 1)})

    customers = []
    for c in range(1, N_CUSTOMERS + 1):
        plan = rng.choices([p for p, _, _ in PLANS], [w for _, w, _ in PLANS])[0]
        base = dict((p, price) for p, _, price in PLANS)[plan]
        customers.append({"customer_id": c, "first_name": rng.choice(FIRST), "plan": plan,
                          "monthly_charge": round(base * rng.uniform(0.8, 1.3), 2),
                          "tenure_months": rng.randint(1, 120), "home_site_id": rng.choice(sites)["site_id"]})

    # Tickets: a quiet baseline, plus a spike from customers of affected sites.
    tickets = []
    for _ in range(int(N_CUSTOMERS * 0.03)):
        c = rng.choice(customers)
        tickets.append({"customer_id": c["customer_id"], "opened_at": start + timedelta(seconds=rng.uniform(0, HISTORY_DAYS * 86400)),
                        "category": rng.choice(["Billing", "Device", "Coverage", "Plan change"])})
    by_site = {}
    for c in customers:
        by_site.setdefault(c["home_site_id"], []).append(c)
    for t in truth:
        share = 0.25 if t["kind"] == "OUTAGE" else 0.10
        for c in by_site.get(t["site_id"], []):
            if rng.random() < share:
                span = max((t["ended_at"] - t["started_at"]).total_seconds(), 900)
                opened = min(t["started_at"] + timedelta(seconds=rng.uniform(300, span + 7200)), now)
                tickets.append({"customer_id": c["customer_id"], "opened_at": opened,
                                "category": "No service" if t["kind"] == "OUTAGE" else "Dropped calls"})
    for i, t in enumerate(sorted(tickets, key=lambda t: t["opened_at"]), 1):
        t["ticket_id"] = f"TK{i:06d}"

    return {"sites": sites, "kpis": kpis, "customers": customers, "tickets": tickets, "incidents_truth": truth}


OUTREACH_DDL = """
    CREATE TABLE IF NOT EXISTS {s}.outreach (
      incident_id STRING NOT NULL, customer_id BIGINT NOT NULL, channel STRING NOT NULL, message STRING NOT NULL,
      credit DOUBLE NOT NULL, approved_by STRING NOT NULL, queued_at TIMESTAMP NOT NULL, status STRING NOT NULL)"""


def main() -> None:
    from pyspark.sql import SparkSession

    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--app-name", default="")
    args = p.parse_args()

    spark = SparkSession.builder.getOrCreate()
    s = f"`{args.catalog}`.`{args.schema}`"
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {s}")
    for table, rows in generate().items():
        spark.createDataFrame(rows).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{s}.{table}")
        print(f"{table}: {len(rows)} rows")
    spark.sql(OUTREACH_DDL.format(s=s))

    if args.app_name:
        from databricks.sdk import WorkspaceClient

        sp = WorkspaceClient().apps.get(args.app_name).service_principal_client_id
        try:  # needs MANAGE on the catalog; many catalogs already grant USE CATALOG to account users
            spark.sql(f"GRANT USE CATALOG ON CATALOG `{args.catalog}` TO `{sp}`")
        except Exception as e:
            print(f"Warning: couldn't grant USE CATALOG on {args.catalog} ({e.__class__.__name__}). "
                  f"Make sure the app's service principal {sp} has USE CATALOG there.")
        spark.sql(f"GRANT USE SCHEMA, SELECT ON SCHEMA {s} TO `{sp}`")
        spark.sql(f"GRANT MODIFY ON TABLE {s}.outreach TO `{sp}`")  # agents queue approved outreach
        print(f"Granted access to {sp}")


if __name__ == "__main__":
    main()
