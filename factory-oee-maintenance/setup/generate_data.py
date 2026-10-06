"""Generate a synthetic plant: machines, hourly sensor readings, shift production, maintenance log, manuals.

Planted for the asset-health view: two machines develop bearing wear and fail (repaired afterwards),
one machine is degrading right now, one healthy machine's vibration steps *down* after maintenance,
and one has a single-hour sensor spike. Only the degrading machines should alert.
All data and manual text are fictional.

Parameters: --catalog, --schema, --app-name.
"""

import argparse
import random
from datetime import datetime, timedelta

HISTORY_DAYS = 60
# type: (ideal cycle seconds, vibration mm/s, temperature C, current A)
TYPES = {"CNC_MILL": (45.0, 2.0, 55.0, 18.0), "PRESS": (12.0, 3.5, 48.0, 30.0), "PACKAGER": (3.0, 1.2, 40.0, 6.0)}
LINES = {"L1": "CNC_MILL", "L2": "PRESS", "L3": "PACKAGER"}
FAILS = {"M04": 40, "M06": 52}          # machine: failure day (degradation starts 10 days earlier)
DEGRADING_NOW = "M11"                    # degradation starts 6 days before "now", no failure yet
MAINTAINED = ("M02", 30)                 # vibration drops 15% after planned maintenance on day 30
SPIKE = ("M07", 40)                      # one-hour sensor spike on day 40
DEGRADE_DAYS = 10
SHIFT_HOURS = 8
PLANNED_BREAK_MIN = 30
REPAIR_HOURS = 10

MANUALS = {
    "CNC_MILL": [
        ("CNC-1", "Lockout/tagout", "Before any inspection: stop the spindle, isolate main power at the disconnect, apply your personal lock and tag, bleed pneumatic pressure, and verify zero energy by attempting a start."),
        ("CNC-3.1", "Spindle bearing wear", "Rising spindle vibration (sustained above 2x baseline) with temperature increase indicates bearing wear. Inspect grease condition and listen for roughness at low speed."),
        ("CNC-3.2", "Spindle bearing replacement", "Replace bearing kit SB-200 as a set. Torque housing bolts to 45 Nm. Run in at 25% speed for 30 minutes and confirm vibration below 1.5x baseline."),
        ("CNC-5", "Coolant and chip clearance", "Clogged coolant lines raise spindle temperature without a vibration increase. Flush lines and clear chips before suspecting bearings."),
    ],
    "PRESS": [
        ("PRS-1", "Lockout/tagout", "Lower the ram onto safety blocks, isolate electrical and hydraulic energy, apply lock and tag, release accumulator pressure, and verify zero energy."),
        ("PRS-2.4", "Main bearing and crank wear", "Sustained vibration rise on the crank side with rising temperature indicates main bearing wear. Check lubrication flow at the manifold."),
        ("PRS-2.5", "Bearing replacement", "Replace main bearing set PB-55. Shim to 0.05-0.08 mm clearance. Verify vibration below 1.5x baseline after 1 hour of cycling."),
        ("PRS-4", "Die alignment", "Misaligned dies cause cyclic vibration and scrap spikes. Check alignment before mechanical teardown if scrap rises first."),
    ],
    "PACKAGER": [
        ("PKG-1", "Lockout/tagout", "Stop the line, isolate power at the cabinet, lock and tag, clear stored energy in the film tensioner, and verify zero energy."),
        ("PKG-3.3", "Drive bearing wear", "Increasing drive vibration with motor current rise indicates bearing or belt wear. Inspect belt tension first, then bearings."),
        ("PKG-3.4", "Drive bearing replacement", "Replace bearing pair DB-12 and the drive belt together. Re-tension to spec and run for 15 minutes before restart."),
        ("PKG-6", "Film jams", "Frequent minor stops with normal vibration usually come from film tension, not mechanical wear."),
    ],
}


def generate(seed: int = 9, now: datetime | None = None) -> dict:
    rng = random.Random(seed)
    now = (now or datetime.now()).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=HISTORY_DAYS)

    machines, readings, shifts, maint, failures = [], [], [], [], []
    for line, mtype in LINES.items():
        for k in range(4):
            mid = f"M{len(machines) + 1:02d}"
            machines.append({"machine_id": mid, "line": line, "machine_type": mtype,
                             "ideal_cycle_s": TYPES[mtype][0], "installed": (start - timedelta(days=rng.randint(400, 3000))).date()})

    for m in machines:
        mid, (cycle, vib0, temp0, cur0) = m["machine_id"], TYPES[m["machine_type"]]
        vib0 *= rng.uniform(0.9, 1.1)
        # Degradation windows in hours since start: (begin, end, fails_at_end)
        windows = []
        if mid in FAILS:
            f = FAILS[mid] * 24
            windows.append((f - DEGRADE_DAYS * 24, f, True))
            failures.append({"machine_id": mid, "failed_at": start + timedelta(hours=f)})
        if mid == DEGRADING_NOW:
            windows.append((HISTORY_DAYS * 24 - 6 * 24, HISTORY_DAYS * 24 + 1, False))
        down_until = -1

        for h in range(HISTORY_DAYS * 24):
            ts = start + timedelta(hours=h)
            wear = 0.0
            for b, e, fails in windows:
                if b <= h < e:
                    wear = ((h - b) / (e - b)) ** 1.6  # accelerating
                if fails and h == e:
                    down_until = h + REPAIR_HOURS
                    maint.append({"machine_id": mid, "ts": ts + timedelta(hours=REPAIR_HOURS), "type": "CORRECTIVE",
                                  "note": "Bearing failure. Replaced bearing set, ran in, returned to service."})
            if h < down_until:
                continue  # machine down for repair: no readings
            base = vib0 * (0.85 if mid == MAINTAINED[0] and h >= MAINTAINED[1] * 24 else 1.0)
            vib = base * (1 + 2.2 * wear) * (1 + rng.gauss(0, 0.05))
            if mid == SPIKE[0] and h == SPIKE[1] * 24 + 10:
                vib *= 4  # sensor glitch
            readings.append({"machine_id": mid, "ts": ts, "vibration_rms": round(vib, 3),
                             "temperature_c": round(temp0 + 15 * wear + rng.gauss(0, 0.8), 2),
                             "current_a": round(cur0 * (1 + 0.15 * wear) * (1 + rng.gauss(0, 0.03)), 2)})

        if mid == MAINTAINED[0]:
            maint.append({"machine_id": mid, "ts": start + timedelta(days=MAINTAINED[1]), "type": "PREVENTIVE",
                          "note": "Scheduled maintenance: lubrication and alignment check."})

        # Production per 8-hour shift.
        for s in range(HISTORY_DAYS * 24 // SHIFT_HOURS):
            h0 = s * SHIFT_HOURS
            planned = SHIFT_HOURS * 60 - PLANNED_BREAK_MIN
            wear = max([((h0 - b) / (e - b)) ** 1.6 if b <= h0 < e else 0.0 for b, e, _ in windows] or [0.0])
            breakdown = sum(1 for b, e, fails in windows if fails and h0 <= e < h0 + SHIFT_HOURS) > 0
            repairing = any(fails and e < h0 < e + REPAIR_HOURS for b, e, fails in windows)
            if breakdown or repairing:
                downtime, reason = planned, "Breakdown"
            else:
                downtime = max(0, int(rng.gauss(40, 20)))
                reason = rng.choice(["Changeover", "Material shortage", "Minor stops"])
            run_s = max(0, planned - downtime) * 60
            perf = rng.uniform(0.76, 0.92) * (1 - 0.1 * wear)
            total = int(run_s * perf / cycle)
            scrap = int(total * min(0.2, rng.uniform(0.01, 0.025) + 0.06 * wear))
            shifts.append({"machine_id": mid, "shift_start": start + timedelta(hours=h0), "planned_min": planned,
                           "downtime_min": min(downtime, planned), "downtime_reason": reason,
                           "total_units": total, "scrap_units": scrap})

    manuals = [{"machine_type": t, "section_id": sid, "title": title, "content": text}
               for t, sections in MANUALS.items() for sid, title, text in sections]
    return {"machines": machines, "sensor_readings": readings, "shifts": shifts,
            "maintenance_log": maint, "manuals": manuals, "failures": failures}


WORK_ORDERS_DDL = """
    CREATE TABLE IF NOT EXISTS {s}.work_orders (
      wo_id STRING NOT NULL, machine_id STRING NOT NULL, created_at TIMESTAMP NOT NULL, created_by STRING NOT NULL,
      priority STRING NOT NULL, summary STRING NOT NULL, plan STRING NOT NULL, ai_drafted BOOLEAN, status STRING NOT NULL)"""


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
    spark.sql(WORK_ORDERS_DDL.format(s=s))

    if args.app_name:
        from databricks.sdk import WorkspaceClient

        sp = WorkspaceClient().apps.get(args.app_name).service_principal_client_id
        try:  # needs MANAGE on the catalog; many catalogs already grant USE CATALOG to account users
            spark.sql(f"GRANT USE CATALOG ON CATALOG `{args.catalog}` TO `{sp}`")
        except Exception as e:
            print(f"Warning: couldn't grant USE CATALOG on {args.catalog} ({e.__class__.__name__}). "
                  f"Make sure the app's service principal {sp} has USE CATALOG there.")
        spark.sql(f"GRANT USE SCHEMA, SELECT ON SCHEMA {s} TO `{sp}`")
        spark.sql(f"GRANT MODIFY ON TABLE {s}.work_orders TO `{sp}`")  # planners file work orders from the app
        print(f"Granted access to {sp}")


if __name__ == "__main__":
    main()
