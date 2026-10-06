"""Run the transaction-monitoring rules and write alerts. Safe to rerun (e.g. nightly).

Detection is deterministic Spark SQL, versioned per rule, so every alert can be traced to the
exact rule and threshold that produced it. The LLM in the app never decides what gets flagged.

Parameters: --catalog, --schema, --app-name (grants), --seed-history (demo decision history).
"""

import argparse
import random
from datetime import datetime, timedelta

# Illustrative list for the demo. Use your institution's jurisdiction-risk list in practice.
HIGH_RISK_COUNTRIES = ["IR", "KP", "MM", "SY", "YE"]

# Each rule's SQL returns: customer_id, evidence (array<string> of txn_ids), total_amount, first_ts, last_ts.
# {s} is the schema. Thresholds are inline so the rule text *is* the documentation.
RULES = [
    {
        "rule_id": "STRUCTURING", "version": 1, "severity": 70,
        "description": "3+ cash deposits of $8,000-$9,999.99 within 7 days (just under the $10,000 CTR threshold).",
        "sql": """
            WITH d AS (SELECT * FROM {s}.transactions WHERE type = 'CASH_DEPOSIT' AND amount BETWEEN 8000 AND 9999.99),
            w AS (SELECT customer_id, ts, count(*) OVER (PARTITION BY customer_id ORDER BY ts
                    RANGE BETWEEN INTERVAL 7 DAYS PRECEDING AND CURRENT ROW) n FROM d),
            ev AS (SELECT DISTINCT d.* FROM w JOIN d ON d.customer_id = w.customer_id
                     AND d.ts BETWEEN w.ts - INTERVAL 7 DAYS AND w.ts WHERE w.n >= 3)
            SELECT customer_id, collect_list(txn_id) evidence, sum(amount) total_amount, min(ts) first_ts, max(ts) last_ts
            FROM ev GROUP BY customer_id""",
    },
    {
        "rule_id": "RAPID_MOVEMENT", "version": 1, "severity": 85,
        "description": "Incoming wire of $20,000+ with 80%+ of it sent out again within 48 hours.",
        "sql": """
            WITH hits AS (
              SELECT i.customer_id, i.txn_id in_id, i.ts in_ts, collect_list(o.txn_id) out_ids,
                     i.amount moved, max(o.ts) out_ts
              FROM {s}.transactions i
              JOIN {s}.transactions o ON o.customer_id = i.customer_id AND o.type IN ('WIRE_OUT', 'ACH_OUT')
                   AND o.ts > i.ts AND o.ts <= i.ts + INTERVAL 48 HOURS
              WHERE i.type = 'WIRE_IN' AND i.amount >= 20000
              GROUP BY i.customer_id, i.txn_id, i.ts, i.amount
              HAVING sum(o.amount) >= 0.8 * i.amount)
            SELECT customer_id, array_distinct(flatten(collect_list(array_union(array(in_id), out_ids)))) evidence,
                   sum(moved) total_amount, min(in_ts) first_ts, max(out_ts) last_ts
            FROM hits GROUP BY customer_id""",
    },
    {
        "rule_id": "HIGH_RISK_JURISDICTION", "version": 1, "severity": 60,
        "description": "$10,000+ in total wires to or from high-risk jurisdictions (" + ", ".join(HIGH_RISK_COUNTRIES) + ").",
        "sql": """
            SELECT customer_id, collect_list(txn_id) evidence, sum(amount) total_amount, min(ts) first_ts, max(ts) last_ts
            FROM {s}.transactions
            WHERE type IN ('WIRE_IN', 'WIRE_OUT') AND counterparty_country IN (""" +
               ", ".join(f"'{c}'" for c in HIGH_RISK_COUNTRIES) + """)
            GROUP BY customer_id HAVING sum(amount) >= 10000""",
    },
    {
        "rule_id": "DORMANT_REACTIVATION", "version": 1, "severity": 65,
        "description": "No activity for 60+ days, then $25,000+ moved within 7 days.",
        "sql": """
            WITH t AS (SELECT *, lag(ts) OVER (PARTITION BY customer_id ORDER BY ts) prev_ts FROM {s}.transactions),
            wake AS (SELECT customer_id, ts wake_ts FROM t WHERE prev_ts IS NOT NULL AND ts >= prev_ts + INTERVAL 60 DAYS),
            burst AS (
              SELECT w.customer_id, w.wake_ts, collect_list(t.txn_id) evidence, sum(t.amount) total_amount, max(t.ts) last_ts
              FROM wake w JOIN t ON t.customer_id = w.customer_id AND t.ts BETWEEN w.wake_ts AND w.wake_ts + INTERVAL 7 DAYS
              GROUP BY w.customer_id, w.wake_ts HAVING sum(t.amount) >= 25000)
            SELECT customer_id, array_distinct(flatten(collect_list(evidence))) evidence, sum(total_amount) total_amount,
                   min(wake_ts) first_ts, max(last_ts) last_ts
            FROM burst GROUP BY customer_id""",
    },
    {
        "rule_id": "ROUND_AMOUNTS", "version": 1, "severity": 40,
        "description": "5+ outgoing transfers of exact round thousands ($5,000+) within 30 days.",
        "sql": """
            WITH r AS (SELECT * FROM {s}.transactions
                       WHERE type IN ('WIRE_OUT', 'ACH_OUT') AND amount >= 5000 AND amount % 1000 = 0),
            w AS (SELECT customer_id, ts, count(*) OVER (PARTITION BY customer_id ORDER BY ts
                    RANGE BETWEEN INTERVAL 30 DAYS PRECEDING AND CURRENT ROW) n FROM r),
            ev AS (SELECT DISTINCT r.* FROM w JOIN r ON r.customer_id = w.customer_id
                     AND r.ts BETWEEN w.ts - INTERVAL 30 DAYS AND w.ts WHERE w.n >= 5)
            SELECT customer_id, collect_list(txn_id) evidence, sum(amount) total_amount, min(ts) first_ts, max(ts) last_ts
            FROM ev GROUP BY customer_id""",
    },
]


def run_rules(spark, s: str):
    """Return the alerts DataFrame: one alert per customer per rule, scored by customer risk.

    Alert IDs are stable (rule + customer), so reruns keep their decision history.
    """
    # ponytail: one alert per customer per rule, so new activity after a closed alert stays under it.
    # Upgrade path: key alerts by detection window (e.g. include first_ts) and link related alerts into a case.
    parts = []
    for rule in RULES:
        parts.append(spark.sql(rule["sql"].format(s=s)).selectExpr(
            f"'{rule['rule_id']}' rule_id", f"{rule['version']} rule_version", f"{rule['severity']} severity", "*"))
    hits = parts[0]
    for p in parts[1:]:
        hits = hits.unionByName(p)
    hits.createOrReplaceTempView("rule_hits")
    return spark.sql(f"""
        SELECT concat('AL-', h.rule_id, '-', h.customer_id) alert_id, h.rule_id, h.rule_version, h.customer_id,
               CAST(least(100, round(h.severity
                 * CASE c.risk_rating WHEN 'HIGH' THEN 1.4 WHEN 'MEDIUM' THEN 1.15 ELSE 1.0 END
                 * CASE WHEN c.pep THEN 1.3 ELSE 1.0 END)) AS INT) score,
               h.evidence evidence_txn_ids, round(h.total_amount, 2) total_amount, h.first_ts, h.last_ts,
               h.last_ts created_at
        FROM rule_hits h JOIN {s}.customers c USING (customer_id)""")


def rules_catalog(spark):
    """The rule definitions as a table, so reviewers can see exactly what produced each alert."""
    return spark.createDataFrame([
        {**{k: r[k] for k in ("rule_id", "version", "severity", "description")}, "logic_sql": " ".join(r["sql"].split())}
        for r in RULES])


def seed_history(alerts: list[dict], truth: set[tuple[int, str]], seed: int = 3) -> list[dict]:
    """Plausible past decisions for ~60% of alerts so the program metrics aren't empty (demo only).

    Uses the planted ground truth with some analyst error, and always respects maker-checker.
    """
    rng = random.Random(seed)
    analysts = ["analyst.kim@example.com", "analyst.ruiz@example.com", "analyst.osei@example.com"]
    approvers = ["lead.patel@example.com", "lead.novak@example.com"]
    out = []
    for a in sorted(alerts, key=lambda a: a["alert_id"]):
        if rng.random() > 0.6:
            continue
        analyst = rng.choice(analysts)
        now = datetime.now()
        t0 = min(a["created_at"] + timedelta(hours=rng.uniform(2, 72)), now)
        t1 = min(t0 + timedelta(hours=rng.uniform(4, 96)), now + timedelta(seconds=1))  # review strictly after
        true_hit = (a["customer_id"], a["rule_id"]) in truth
        escalate = rng.random() < (0.85 if true_hit else 0.08)
        if escalate:
            out.append(dict(alert_id=a["alert_id"], action="ESCALATE", new_status="ESCALATED", actor=analyst,
                            rationale="Pattern consistent with the rule typology; no documented business purpose.",
                            ai_draft_used=rng.random() < 0.5, decided_at=t0))
            if rng.random() < 0.8:
                approve = true_hit or rng.random() < 0.3
                out.append(dict(alert_id=a["alert_id"], action="APPROVE_SAR" if approve else "RETURN",
                                new_status="SAR_APPROVED" if approve else "RETURNED", actor=rng.choice(approvers),
                                rationale="Reviewed evidence and narrative; agree with escalation." if approve
                                else "Insufficient evidence; obtain source-of-funds documentation first.",
                                ai_draft_used=False, decided_at=t1))
        else:
            explained = rng.random() < 0.4
            out.append(dict(alert_id=a["alert_id"],
                            action="CLOSE_EXPLAINED" if explained else "CLOSE_FALSE_POSITIVE",
                            new_status="CLOSED_EXPLAINED" if explained else "CLOSED_FALSE_POSITIVE", actor=analyst,
                            rationale="Activity matches the customer's known business profile." if explained
                            else "Rule threshold met by routine activity; no suspicious indicators.",
                            ai_draft_used=False, decided_at=t0))
    return out


DISPOSITION_COLUMNS = ["alert_id", "action", "new_status", "actor", "rationale", "ai_draft_used", "decided_at"]
DISPOSITIONS_DDL = """
    CREATE TABLE IF NOT EXISTS {s}.alert_dispositions (
      alert_id STRING NOT NULL, action STRING NOT NULL, new_status STRING NOT NULL, actor STRING NOT NULL,
      rationale STRING NOT NULL, ai_draft_used BOOLEAN, decided_at TIMESTAMP NOT NULL)
    TBLPROPERTIES ('delta.appendOnly' = 'true')"""


def main() -> None:
    from pyspark.sql import SparkSession

    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--app-name", default="")
    p.add_argument("--seed-history", default="true")
    args = p.parse_args()

    spark = SparkSession.builder.getOrCreate()
    s = f"`{args.catalog}`.`{args.schema}`"
    alerts = run_rules(spark, s)
    alerts.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{s}.alerts")
    rules_catalog(spark).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{s}.rules")
    print(spark.sql(f"SELECT rule_id, count(*) n FROM {s}.alerts GROUP BY 1 ORDER BY 1").collect())

    # The audit trail is append-only at the storage layer; a rerun keeps existing decisions.
    spark.sql(DISPOSITIONS_DDL.format(s=s))
    if args.seed_history == "true" and spark.table(f"{s}.alert_dispositions").limit(1).count() == 0:
        truth = {(r.customer_id, r.typology) for r in spark.table(f"{s}.ground_truth").collect()}
        history = seed_history([r.asDict() for r in spark.table(f"{s}.alerts").collect()], truth)
        if history:
            spark.createDataFrame(history).select(*DISPOSITION_COLUMNS) \
                .write.mode("append").saveAsTable(f"{s}.alert_dispositions")
        print(f"Seeded {len(history)} historical decisions")

    if args.app_name:
        from databricks.sdk import WorkspaceClient

        sp = WorkspaceClient().apps.get(args.app_name).service_principal_client_id
        spark.sql(f"GRANT USE CATALOG ON CATALOG `{args.catalog}` TO `{sp}`")
        spark.sql(f"GRANT USE SCHEMA, SELECT ON SCHEMA {s} TO `{sp}`")
        spark.sql(f"GRANT MODIFY ON TABLE {s}.alert_dispositions TO `{sp}`")  # append decisions only
        print(f"Granted access to {sp}")


if __name__ == "__main__":
    main()
