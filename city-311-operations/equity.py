"""Service-equity metric: are some districts served slower than their mix of requests explains?

Raw comparisons mislead because districts request different things (tree trimming has a 60-day
target, a traffic signal 1 day). So for each district we compare its actual on-time rate with the
rate it would have if every request were handled at the citywide on-time rate for its category:

    expected = sum over categories (district's requests in category x citywide on-time rate) / district's requests
    gap      = actual - expected   (percentage points; negative = slower than its mix explains)

Requests still open and not yet due are excluded (counting them as on time would flatter
districts with recent backlogs). Open requests past their target count as late.
"""

EQUITY_SQL = """
WITH r AS (
  SELECT r.district_id, r.category, r.closed_at, r.created_at,
         CASE WHEN r.closed_at IS NOT NULL
                THEN r.closed_at <= timestampadd(DAY, c.sla_days, r.created_at)
              WHEN current_timestamp() > timestampadd(DAY, c.sla_days, r.created_at) THEN false
         END on_time
  FROM {s}.requests r JOIN {s}.categories c USING (category)
  WHERE :department = 'All' OR c.department = :department),
city AS (SELECT category, avg(CAST(on_time AS INT)) rate FROM r WHERE on_time IS NOT NULL GROUP BY category),
dist AS (
  SELECT r.district_id, count(*) decided, avg(CAST(r.on_time AS INT)) actual, avg(city.rate) expected
  FROM r JOIN city USING (category) WHERE r.on_time IS NOT NULL GROUP BY r.district_id),
raw AS (
  SELECT district_id, percentile_approx((unix_timestamp(closed_at) - unix_timestamp(created_at)) / 86400, 0.5) median_days
  FROM r WHERE closed_at IS NOT NULL GROUP BY district_id)
SELECT dist.district_id, ds.name district, ds.population, dist.decided,
       round(100 * dist.actual, 1) on_time_pct, round(100 * dist.expected, 1) expected_pct,
       round(100 * (dist.actual - dist.expected), 1) gap_pts, round(raw.median_days, 1) raw_median_days,
       100 * (dist.actual - dist.expected) <= -:threshold flagged
FROM dist JOIN {s}.districts ds USING (district_id) JOIN raw USING (district_id)
ORDER BY gap_pts"""


def equity_sql(schema: str) -> str:
    return EQUITY_SQL.format(s=schema)
