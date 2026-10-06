"""OEE (overall equipment effectiveness) from shift records, aggregated correctly.

Per standard definitions, with planned breaks already excluded from planned time:
  availability = run time / planned time                         (run = planned - unplanned downtime)
  performance  = ideal cycle x total units / run time
  quality      = ideal cycle x good units / ideal cycle x total units
  OEE          = availability x performance x quality = ideal cycle x good units / planned time

Roll-ups sum minutes and units first, then divide. Averaging per-machine percentages is wrong
whenever machines run different hours or cycle times. Quality is weighted by ideal cycle time
so the identity OEE = A x P x Q still holds across mixed machines.
"""

DIMENSIONS = {"Line": "m.line", "Machine": "m.machine_id", "Day": "CAST(sh.shift_start AS DATE)"}

OEE_SQL = """
SELECT {dim} dim,
       sum(sh.planned_min) planned_min,
       sum(sh.downtime_min) downtime_min,
       round(100 * try_divide(sum(sh.planned_min - sh.downtime_min), sum(sh.planned_min)), 1) availability_pct,
       round(100 * try_divide(sum(sh.total_units * m.ideal_cycle_s), sum((sh.planned_min - sh.downtime_min) * 60)), 1) performance_pct,
       round(100 * try_divide(sum((sh.total_units - sh.scrap_units) * m.ideal_cycle_s), sum(sh.total_units * m.ideal_cycle_s)), 1) quality_pct,
       round(100 * try_divide(sum((sh.total_units - sh.scrap_units) * m.ideal_cycle_s), sum(sh.planned_min * 60)), 1) oee_pct
FROM {s}.shifts sh JOIN {s}.machines m USING (machine_id)
WHERE sh.shift_start >= current_timestamp() - make_interval(0, 0, 0, :days)
GROUP BY 1 ORDER BY 1"""

# Where planned time went, in minutes. The parts add up to planned time.
LOSSES_SQL = """
SELECT round(sum(CASE WHEN sh.downtime_reason = 'Breakdown' THEN sh.downtime_min ELSE 0 END)) breakdowns,
       round(sum(CASE WHEN sh.downtime_reason != 'Breakdown' THEN sh.downtime_min ELSE 0 END)) other_downtime,
       round(sum((sh.planned_min - sh.downtime_min) - sh.total_units * m.ideal_cycle_s / 60)) slow_cycles,
       round(sum(sh.scrap_units * m.ideal_cycle_s / 60)) scrap,
       round(sum((sh.total_units - sh.scrap_units) * m.ideal_cycle_s / 60)) productive
FROM {s}.shifts sh JOIN {s}.machines m USING (machine_id)
WHERE sh.shift_start >= current_timestamp() - make_interval(0, 0, 0, :days)"""


def oee_sql(s: str, by: str) -> str:
    return OEE_SQL.format(s=s, dim=DIMENSIONS[by])  # whitelist: `by` never reaches SQL directly


def losses_sql(s: str) -> str:
    return LOSSES_SQL.format(s=s)
