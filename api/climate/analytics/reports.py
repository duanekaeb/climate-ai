"""Deterministic daily digest (the system writes it; Claude's nightly run adds judgment)."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from climate.analytics.baseline import (
    TRAIN_DAYS,
    BaselineFit,
    expected_covered_seconds,
    involved_modes,
    plain,
    pre_period_fits,
    residual_stats,
)
from climate.analytics.daily import DayRow, daily_rows, house_tz, is_complete, mode_seconds, unit_weights
from climate.analytics.metrics import actions_by_status, comfort_period
from climate.api.schemas import OPEN_METEO_ATTRIBUTION
from climate.events import publish
from climate.house import ROOM_BY_KEY, UNITS
from climate.store.orm import Report
from climate.timeutil import day_bounds_utc

COMFORT_TARGET_PCT = 97.0  # blueprint objective: occupied rooms in band >= 97% of minutes
MAX_ALERTS = 10


def _fmt_min(x: float | None) -> str:
    return "–" if x is None else f"{x:,.0f}"


def _unit_line(row: DayRow | None, fits: dict[tuple[str, str], BaselineFit], involved: set[tuple[str, str]],
               unit_key: str) -> dict[str, Any]:
    """Actual vs expected for one unit on the day, with a 90% range for a single day."""
    out: dict[str, Any] = {"unit_key": unit_key, "actual_min": None, "expected_min": None, "ci90_min": None,
                           "maxed_min": 0.0, "complete": False, "baseline_ok": False, "note": ""}
    if row is None:
        out["note"] = "no data"
        return out
    modes = [m for (u, m) in sorted(involved) if u == unit_key]
    actual = sum(mode_seconds(row, m) for m in modes) if modes else row.cool_s + row.heat_s
    out.update(actual_min=round(actual / 60.0, 1), maxed_min=row.maxed_min, complete=is_complete(row))
    if not modes:
        out["note"] = "idle"
        out["baseline_ok"] = True
        out["expected_min"] = 0.0
        return out
    missing = [m for m in modes if (unit_key, m) not in fits]
    failing = [m for m in modes if (unit_key, m) in fits and not fits[(unit_key, m)].passes]
    if missing:
        out["note"] = f"no {'/'.join(missing)} baseline yet"
        return out
    if failing:
        out["note"] = f"{'/'.join(failing)} baseline fails its checks"
        return out
    if not out["complete"]:
        out["note"] = f"partial day ({row.slots}/{row.expected_slots} slots)"
        return out
    exp = sum(expected_covered_seconds(fits[(unit_key, m)], row) for m in modes)
    if math.isnan(exp):
        out["note"] = "no outdoor data"
        return out
    st = residual_stats((fits[(unit_key, m)], 1.0) for m in modes)
    hw = st.halfwidth(1, exp) if st else 0.0
    out.update(expected_min=round(exp / 60.0, 1), baseline_ok=True,
               ci90_min=[round(max(exp - hw, 0.0) / 60.0, 1), round((exp + hw) / 60.0, 1)])
    return out


def build_daily_report(session: Session, day: date) -> int:
    """Write a reports row (kind='daily', author='system') for a local day: runtime per unit
    vs expected, maxed-out minutes, comfort misses, controller actions, alerts. Markdown body,
    numbers also in data. Idempotent per day (replace the system daily report for that day).
    Returns the report id.

    "Expected" comes from the baselines trained on the 90 days before the day (so the day
    never grades itself) with a 90% range for a single day (ASHRAE G14 form, m = 1)."""
    tz = house_tz(session)
    t0, t1 = day_bounds_utc(day, tz)
    fits = pre_period_fits(session, day, tz)
    rows = daily_rows(session, day, day, tz)
    by_unit = {r.unit_key: r for r in rows}
    involved = involved_modes(rows, fits)
    weights = unit_weights(session)

    units = [_unit_line(by_unit.get(u.key), fits, involved, u.key) for u in UNITS]
    names = {u.key: u.name for u in UNITS}

    # House total when every unit with data has a usable expectation.
    house: dict[str, Any] = {"actual_min": None, "expected_min": None, "ci90_min": None, "baseline_ok": False}
    with_data = [u for u in units if u["actual_min"] is not None]
    if with_data:
        house["actual_min"] = round(sum(weights.get(u["unit_key"], 1.0) * u["actual_min"] for u in with_data), 1)
    if with_data and all(u["baseline_ok"] and u["expected_min"] is not None for u in with_data):
        exp = sum(weights.get(u["unit_key"], 1.0) * u["expected_min"] for u in with_data)
        keys = [k for k in sorted(involved) if k in fits]
        st = residual_stats((fits[k], weights.get(k[0], 1.0)) for k in keys)
        hw = (st.halfwidth(1, exp * 60.0) / 60.0) if st else 0.0
        house.update(expected_min=round(exp, 1), ci90_min=[round(max(exp - hw, 0.0), 1), round(exp + hw, 1)],
                     baseline_ok=True)

    comfort_rows = comfort_period(session, t0, t1, tz)
    misses = [c for c in comfort_rows if c["in_band_pct"] is not None and c["in_band_pct"] < COMFORT_TARGET_PCT]
    actions = actions_by_status(session, t0, t1)
    alerts = [
        {"id": int(i), "level": lv, "title": ti}
        for i, lv, ti in session.execute(
            text("SELECT id, level, title FROM alerts WHERE resolved_at IS NULL ORDER BY ts DESC, id DESC")
        )
    ]
    sources = sorted({s for r in rows for s in r.weather_sources})
    any_row = rows[0] if rows else None
    weather = {
        "mean_f": any_row.outdoor_mean_f if any_row else None,
        "max_f": any_row.outdoor_max_f if any_row else None,
        "sources": sources,
    }
    open_meteo = "open-meteo" in sources

    # --- markdown -----------------------------------------------------------------------
    title = f"Daily report: {day.strftime('%A')} {day.day} {day.strftime('%B %Y')}"
    md = [f"## {title}", ""]
    if not rows:
        md += ["No runtime data was recorded for this day.", ""]
    else:
        md += ["**Runtime vs weather-expected** (stage-1 minutes; 90% range for one day)", "",
               "| Unit | Actual | Expected | Maxed out |", "|---|---:|---:|---:|"]
        for u in units:
            if u["expected_min"] is not None and u["ci90_min"] is not None:
                exp_s = f"{_fmt_min(u['expected_min'])} ({_fmt_min(u['ci90_min'][0])}–{_fmt_min(u['ci90_min'][1])})"
            elif u["expected_min"] is not None:
                exp_s = _fmt_min(u["expected_min"])
            else:
                exp_s = f"– ({u['note']})" if u["note"] else "–"
            maxed = "–" if u["actual_min"] is None else f"{u['maxed_min']:.0f} min"
            md.append(f"| {names[u['unit_key']]} | {_fmt_min(u['actual_min'])} | {exp_s} | {maxed} |")
        md.append("")
        if house["baseline_ok"] and house["expected_min"] is not None and house["ci90_min"] is not None:
            lo, hi = house["ci90_min"]
            verdict = ("inside the range the weather explains" if lo <= house["actual_min"] <= hi else
                       "below the weather-expected range" if house["actual_min"] < lo else
                       "above the weather-expected range")
            md += [(f"House: {_fmt_min(house['actual_min'])} min against {_fmt_min(house['expected_min'])} expected "
                    f"({_fmt_min(lo)}–{_fmt_min(hi)}), {verdict}. One day proves nothing on its own."), ""]
        else:
            md += [(f"House: {_fmt_min(house['actual_min'])} min; no weather-normalized comparison today (baselines "
                    f"come from the {TRAIN_DAYS} days before and need to pass their checks)."), ""]

    if misses:
        parts = []
        for c in misses:
            parts.append(f"{ROOM_BY_KEY[c['room_key']].name} {c['in_band_pct']:.0f}% in band "
                         f"({c['miss_min']:.0f} min out, worst {c['worst_excursion_f']:.1f}°F past the band)")
        md += [f"**Comfort:** below the {COMFORT_TARGET_PCT:.0f}% target: " + "; ".join(parts) + ".", ""]
    elif any(c["in_band_pct"] is not None for c in comfort_rows):
        md += [(f"**Comfort:** every occupied room with a sensor was in band at least {COMFORT_TARGET_PCT:.0f}% of "
                "its occupied minutes."), ""]
    else:
        md += ["**Comfort:** no occupied minutes with a temperature reading to score.", ""]

    n_act = sum(actions.values())
    if n_act:
        md += [f"**Controller:** {n_act} action{'s' if n_act != 1 else ''} ("
               + ", ".join(f"{c} {s}" for s, c in actions.items()) + ").", ""]
    else:
        md += ["**Controller:** no actions.", ""]

    if alerts:
        shown = "; ".join(f"{a['level']}: {a['title']}" for a in alerts[:MAX_ALERTS])
        more = f" (+{len(alerts) - MAX_ALERTS} more)" if len(alerts) > MAX_ALERTS else ""
        md += [f"**Open alerts ({len(alerts)}):** {shown}{more}.", ""]
    else:
        md += ["**Open alerts:** none.", ""]

    if weather["mean_f"] is not None:
        md += [f"Outdoor: mean {weather['mean_f']:.0f}°F, high {weather['max_f']:.0f}°F.", ""]
    if open_meteo:
        md += [OPEN_METEO_ATTRIBUTION]
    body = "\n".join(md).rstrip() + "\n"

    data: dict[str, Any] = {
        "day": day.isoformat(),
        "tz": tz,
        "units": units,
        "house": house,
        "comfort": comfort_rows,
        "comfort_misses": [c["room_key"] for c in misses],
        "actions": actions,
        "alerts": alerts,
        "weather": weather,
        "open_meteo": open_meteo,
        "attribution": OPEN_METEO_ATTRIBUTION if open_meteo else None,
        "baseline": f"the {TRAIN_DAYS} days before {day.isoformat()}",
    }

    existing = session.execute(
        select(Report)
        .where(Report.kind == "daily", Report.author == "system", Report.period_start == day, Report.period_end == day)
        .order_by(Report.id)
    ).scalars().all()
    if existing:
        report = existing[0]
        for extra in existing[1:]:
            session.delete(extra)
        report.title, report.body_md, report.data = title, body, plain(data)
    else:
        report = Report(kind="daily", author="system", period_start=day, period_end=day, title=title, body_md=body,
                        data=plain(data))
        session.add(report)
    session.flush()
    publish(session, "report", int(report.id))
    return int(report.id)
