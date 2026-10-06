"""Asset health: per-machine robust vibration baseline, and alerts only on *sustained* deviations.

z = (vibration - baseline median) / (1.4826 x median absolute deviation), baseline = each machine's
first 14 days. An hour is "hot" when z > 4; an alert needs 12 hot hours in a row, so a one-hour
sensor glitch never alerts, and a drop in vibration (e.g. after maintenance) never alerts.
"""

# ponytail: computed live from readings, fine for one plant. For a fleet, run it as a scheduled
# job or streaming table and read the results instead.
SCORED_SQL = """
WITH r AS (SELECT * FROM {s}.sensor_readings),
start AS (SELECT min(ts) t0 FROM r),
med AS (SELECT machine_id, percentile_approx(vibration_rms, 0.5) med
        FROM r, start WHERE ts < t0 + INTERVAL 14 DAYS GROUP BY machine_id),
base AS (SELECT r.machine_id, m.med, percentile_approx(abs(r.vibration_rms - m.med), 0.5) mad
         FROM r JOIN med m USING (machine_id), start WHERE r.ts < t0 + INTERVAL 14 DAYS GROUP BY r.machine_id, m.med),
z AS (SELECT r.*, b.med baseline, (r.vibration_rms - b.med) / (1.4826 * b.mad) z
      FROM r JOIN base b USING (machine_id))
SELECT *,
       sum(CASE WHEN z > 4 THEN 1 ELSE 0 END) OVER w12 = 12 alert_hour,
       sum(CASE WHEN z > 2.5 THEN 1 ELSE 0 END) OVER w6 = 6 watch_hour
FROM z
WINDOW w12 AS (PARTITION BY machine_id ORDER BY ts ROWS BETWEEN 11 PRECEDING AND CURRENT ROW),
       w6 AS (PARTITION BY machine_id ORDER BY ts ROWS BETWEEN 5 PRECEDING AND CURRENT ROW)"""

# Alert episodes: consecutive alert hours merged (gaps-and-islands).
EPISODES_SQL = """
WITH s AS ({scored}),
a AS (SELECT machine_id, ts, alert_hour,
             sum(CASE WHEN alert_hour THEN 0 ELSE 1 END) OVER (PARTITION BY machine_id ORDER BY ts) grp FROM s)
SELECT machine_id, min(ts) started_at, max(ts) last_seen_at, count(*) hours
FROM a WHERE alert_hour GROUP BY machine_id, grp ORDER BY started_at"""

# Current status per machine from its latest 24 hours.
STATUS_SQL = """
WITH s AS ({scored}),
latest AS (SELECT machine_id, max(ts) last_ts FROM s GROUP BY machine_id)
SELECT s.machine_id, m.line, m.machine_type,
       CASE WHEN max(CASE WHEN s.alert_hour THEN 1 ELSE 0 END) = 1 THEN 'ALERT'
            WHEN max(CASE WHEN s.watch_hour THEN 1 ELSE 0 END) = 1 THEN 'WATCH' ELSE 'OK' END status,
       round(avg(s.vibration_rms) / max(s.baseline), 2) vibration_x_baseline,
       round(avg(s.temperature_c), 1) temperature_c, max(s.ts) last_reading
FROM s JOIN latest l ON s.machine_id = l.machine_id AND s.ts > l.last_ts - INTERVAL 24 HOURS
JOIN {s}.machines m ON m.machine_id = s.machine_id
GROUP BY s.machine_id, m.line, m.machine_type ORDER BY s.machine_id"""


def scored_sql(s: str) -> str:
    return SCORED_SQL.format(s=s)


def episodes_sql(s: str) -> str:
    return EPISODES_SQL.format(scored=scored_sql(s))


def status_sql(s: str) -> str:
    return STATUS_SQL.format(scored=scored_sql(s), s=s)
