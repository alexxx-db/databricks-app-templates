"""Incident detection from 15-minute cell-site KPIs.

An interval is bad when availability < 95% or the dropped-call rate > 2%. Consecutive bad
intervals at a site are merged into one incident (gaps-and-islands), and only runs of 2+
intervals (30+ minutes) count, so single-interval blips are ignored. An incident is an OUTAGE
if availability fell below 50% at any point, otherwise a DEGRADATION.
"""

INCIDENTS_SQL = """
WITH k AS (
  SELECT site_id, ts, availability_pct, drop_call_rate_pct,
         availability_pct < 95 OR drop_call_rate_pct > 2 bad
  FROM {s}.kpis),
runs AS (
  SELECT *, sum(CASE WHEN bad THEN 0 ELSE 1 END) OVER (PARTITION BY site_id ORDER BY ts) island FROM k),
latest AS (SELECT max(ts) last_ts FROM {s}.kpis)
SELECT concat(r.site_id, '-', date_format(min(r.ts), 'yyyyMMddHHmm')) incident_id, r.site_id, s.area,
       CASE WHEN min(r.availability_pct) < 50 THEN 'OUTAGE' ELSE 'DEGRADATION' END kind,
       min(r.ts) started_at, max(r.ts) + INTERVAL 15 MINUTES ended_at,
       count(*) * 15 duration_min, max(r.ts) = max(l.last_ts) ongoing,
       round(min(r.availability_pct), 1) min_availability_pct, round(max(r.drop_call_rate_pct), 2) max_drop_call_pct
FROM runs r JOIN {s}.sites s USING (site_id) CROSS JOIN latest l
WHERE r.bad
GROUP BY r.site_id, s.area, r.island
HAVING count(*) >= 2
ORDER BY started_at DESC"""

# Customers homed on the incident's site, with how many incidents hit them in the period and
# whether they opened a ticket during or within a day after this incident.
IMPACT_SQL = """
WITH inc AS ({incidents}),
this AS (SELECT * FROM inc WHERE incident_id = :incident),
hits AS (SELECT c.customer_id, count(*) incidents_in_period
         FROM {s}.customers c JOIN inc ON inc.site_id = c.home_site_id GROUP BY c.customer_id)
SELECT c.customer_id, c.first_name, c.plan, c.monthly_charge, c.tenure_months, h.incidents_in_period,
       EXISTS (SELECT 1 FROM {s}.tickets t WHERE t.customer_id = c.customer_id
               AND t.opened_at BETWEEN this.started_at AND this.ended_at + INTERVAL 24 HOURS) opened_ticket
FROM this JOIN {s}.customers c ON c.home_site_id = this.site_id
JOIN hits h USING (customer_id)
ORDER BY c.customer_id"""


def incidents_sql(s: str) -> str:
    return INCIDENTS_SQL.format(s=s)


def impact_sql(s: str) -> str:
    return IMPACT_SQL.format(incidents=incidents_sql(s), s=s)
