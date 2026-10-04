import time, cProfile, pstats
from datetime import date, timedelta
from sqlalchemy import text
from tests.test_analytics_units import TZ
def test_prof(db):
    from climate.analytics.daily import daily_rows, hourly_runtime, hourly_outdoor
    from climate.timeutil import day_bounds_utc
    end = date(2026, 9, 30); start = end - timedelta(days=364)
    t0, t1 = day_bounds_utc(start, TZ)[0], day_bounds_utc(end, TZ)[1]
    db.execute(text("""INSERT INTO runtime_5m (ts, unit_key, comp_cool1, hvac_mode, zone_temp_f, outdoor_temp_f, source)
        SELECT t, u, 100, 'cool', 76, 80, 'test' FROM generate_series(CAST(:t0 AS timestamptz), CAST(:t1 AS timestamptz) - interval '5 minutes', interval '5 minutes') t
        CROSS JOIN unnest(ARRAY['main','up','bed']) u"""), {"t0": t0, "t1": t1})
    db.execute(text("""INSERT INTO weather_hourly (ts, source, kind, temp_f) SELECT t, 'open-meteo', 'observed', 80 FROM generate_series(CAST(:t0 AS timestamptz), CAST(:t1 AS timestamptz) - interval '1 hour', interval '1 hour') t"""), {"t0": t0, "t1": t1})
    db.commit(); db.execute(text("ANALYZE"))
    a = day_bounds_utc(end - timedelta(days=89), TZ)[0]
    for q in ["SELECT count(*) FROM runtime_5m WHERE ts >= :t0 AND ts < :t1"]:
        t=time.perf_counter(); db.execute(text(q), {"t0": a, "t1": t1}).all(); print("\ncount", time.perf_counter()-t)
    t=time.perf_counter(); hr = hourly_runtime(db, a, t1, TZ); print("hourly_runtime", time.perf_counter()-t, hr.hour.size)
    t=time.perf_counter(); hourly_outdoor(db, a, t1, hr); print("hourly_outdoor", time.perf_counter()-t)
    pr = cProfile.Profile(); pr.enable(); daily_rows(db, end - timedelta(days=89), end, TZ); pr.disable()
    pstats.Stats(pr).sort_stats("cumulative").print_stats(18)
    from climate.analytics.daily import _HOURLY_SQL
    plan = db.execute(text("EXPLAIN ANALYZE " + _HOURLY_SQL.format(unit_filter="")), {"t0": a, "t1": t1, "tz": TZ}).all()
    print("\n".join(r[0] for r in plan))
