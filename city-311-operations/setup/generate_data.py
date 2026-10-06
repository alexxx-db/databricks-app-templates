"""Generate a synthetic city's 311 service requests into Unity Catalog and grant the app access.

The city, districts and coordinates are fictional. Two patterns are planted for the equity view:
District 7 gets genuinely slower Streets & Sanitation service; District 3 requests mostly
slow-to-fix categories, so it looks worse on raw averages but is on time once category mix is
accounted for.

Parameters: --catalog, --schema, --app-name.
"""

import argparse
import math
import random
from datetime import datetime, timedelta

HISTORY_DAYS = 180

# category: (department, sla_days, typical close as a fraction of SLA, priority)
CATEGORIES = {
    "Pothole": ("Streets", 7, 0.6, "MEDIUM"),
    "Streetlight Out": ("Streets", 10, 0.6, "MEDIUM"),
    "Traffic Signal Malfunction": ("Streets", 1, 0.5, "HIGH"),
    "Missed Trash Pickup": ("Sanitation", 3, 0.5, "MEDIUM"),
    "Illegal Dumping": ("Sanitation", 7, 0.6, "MEDIUM"),
    "Graffiti": ("Sanitation", 14, 0.6, "LOW"),
    "Tree Trimming": ("Parks", 60, 0.9, "LOW"),
    "Fallen Tree": ("Parks", 2, 0.5, "HIGH"),
    "Sidewalk Repair": ("Streets", 90, 0.9, "LOW"),
    "Noise Complaint": ("Code Enforcement", 5, 0.5, "LOW"),
    "Abandoned Vehicle": ("Code Enforcement", 10, 0.6, "LOW"),
    "Water Main Leak": ("Water", 1, 0.4, "HIGH"),
}
SLOW_CATEGORIES = ["Tree Trimming", "Sidewalk Repair"]

# id: (name, population, center lat, lon)
DISTRICTS = {
    1: ("Downtown", 62000, 39.100, -94.580),
    2: ("Riverside", 48000, 39.120, -94.620),
    3: ("Oak Hills", 55000, 39.060, -94.640),
    4: ("Northgate", 71000, 39.160, -94.560),
    5: ("Westfield", 66000, 39.090, -94.680),
    6: ("Harbor", 39000, 39.130, -94.520),
    7: ("Eastside", 58000, 39.080, -94.510),
    8: ("Southpark", 74000, 39.030, -94.570),
}
SLOW_DISTRICT, SLOW_DEPARTMENTS, SLOW_FACTOR = 7, {"Streets", "Sanitation"}, 1.9
MIX_SKEWED_DISTRICT = 3

STREETS = ["Main St", "Oak Ave", "5th St", "Elm St", "Grand Blvd", "Park Rd", "Lincoln Ave", "Maple Dr", "River Rd"]
TEMPLATES = {
    "Pothole": "Large pothole on {street} near {n}, cars are swerving around it.",
    "Streetlight Out": "Streetlight out on {street} by number {n}. Very dark at night.",
    "Traffic Signal Malfunction": "Traffic light at {street} and {street2} is stuck on red.",
    "Missed Trash Pickup": "Trash wasn't picked up on {street} this week.",
    "Illegal Dumping": "Someone dumped a mattress and furniture in the alley behind {n} {street}.",
    "Graffiti": "Graffiti on the wall of the building at {n} {street}.",
    "Tree Trimming": "Tree branches on {street} are blocking the sidewalk and the street sign.",
    "Fallen Tree": "A tree fell across {street} after the storm, blocking a lane.",
    "Sidewalk Repair": "Sidewalk in front of {n} {street} is cracked and uneven, a trip hazard.",
    "Noise Complaint": "Loud music every night after midnight at {n} {street}.",
    "Abandoned Vehicle": "Car parked on {street} for three weeks without moving, flat tires.",
    "Water Main Leak": "Water bubbling up through the street on {street} near {n}.",
}
CHANNELS = [("Phone", 0.45), ("Mobile App", 0.30), ("Web", 0.20), ("Walk-in", 0.05)]
LANGUAGES = [("en", 0.82), ("es", 0.11), ("zh", 0.03), ("vi", 0.02), ("tl", 0.02)]


def pick(rng: random.Random, pairs):
    return rng.choices([v for v, _ in pairs], [w for _, w in pairs])[0]


def generate(seed: int = 5, now: datetime | None = None, requests_per_1k_per_day: float = 0.25) -> dict:
    rng = random.Random(seed)
    now = (now or datetime.now()).replace(microsecond=0)
    start = now - timedelta(days=HISTORY_DAYS)
    cats = list(CATEGORIES)

    requests = []
    for d_id, (_, pop, lat, lon) in DISTRICTS.items():
        weights = [1.0] * len(cats)
        if d_id == MIX_SKEWED_DISTRICT:
            weights = [12.0 if c in SLOW_CATEGORIES else 0.5 for c in cats]
        n = int(pop / 1000 * requests_per_1k_per_day * HISTORY_DAYS)
        for _ in range(n):
            cat = rng.choices(cats, weights)[0]
            dept, sla, typical, priority = CATEGORIES[cat]
            created = start + timedelta(seconds=rng.uniform(0, HISTORY_DAYS * 86400))
            # Lognormal close time around the category's typical duration; 15-20% miss the SLA.
            days = typical * sla * math.exp(rng.gauss(0, 0.55))
            if d_id == SLOW_DISTRICT and dept in SLOW_DEPARTMENTS:
                days *= SLOW_FACTOR
            closed = created + timedelta(days=days)
            requests.append({
                "request_id": "",  # set after sorting
                "created_at": created,
                "closed_at": closed if closed <= now else None,
                "status": "CLOSED" if closed <= now else "OPEN",
                "category": cat,
                "department": dept,
                "district_id": d_id,
                "lat": round(lat + rng.gauss(0, 0.012), 5),
                "lon": round(lon + rng.gauss(0, 0.015), 5),
                "channel": pick(rng, CHANNELS),
                "language": pick(rng, LANGUAGES),
                "description": TEMPLATES[cat].format(street=rng.choice(STREETS), street2=rng.choice(STREETS),
                                                     n=rng.randint(100, 4999)),
            })
    requests.sort(key=lambda r: r["created_at"])
    for i, r in enumerate(requests, 1):
        r["request_id"] = f"SR-{r['created_at'].year}-{i:06d}"

    categories = [{"category": c, "department": d, "sla_days": s, "priority": p}
                  for c, (d, s, _, p) in CATEGORIES.items()]
    districts = [{"district_id": i, "name": n, "population": p, "lat": la, "lon": lo}
                 for i, (n, p, la, lo) in DISTRICTS.items()]
    return {"requests": requests, "categories": categories, "districts": districts}


def main() -> None:
    from pyspark.sql import SparkSession

    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--app-name", default="")
    args = p.parse_args()

    spark = SparkSession.builder.getOrCreate()
    fqn = f"`{args.catalog}`.`{args.schema}`"
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {fqn}")
    data = generate()
    for table, rows in data.items():
        spark.createDataFrame(rows).write.mode("overwrite").option("overwriteSchema", "true") \
            .saveAsTable(f"{fqn}.{table}")
        print(f"{table}: {len(rows)} rows")

    if args.app_name:
        from databricks.sdk import WorkspaceClient

        sp = WorkspaceClient().apps.get(args.app_name).service_principal_client_id
        try:  # needs MANAGE on the catalog; many catalogs already grant USE CATALOG to account users
            spark.sql(f"GRANT USE CATALOG ON CATALOG `{args.catalog}` TO `{sp}`")
        except Exception as e:
            print(f"Warning: couldn't grant USE CATALOG on {args.catalog} ({e.__class__.__name__}). "
                  f"Make sure the app's service principal {sp} has USE CATALOG there.")
        spark.sql(f"GRANT USE SCHEMA, SELECT ON SCHEMA {fqn} TO `{sp}`")
        spark.sql(f"GRANT MODIFY ON TABLE {fqn}.requests TO `{sp}`")  # staff file new requests from the intake tab
        print(f"Granted access to {sp}")


if __name__ == "__main__":
    main()
