"""Claude's tools as plain async functions over the Climate AI API.

Each tool calls the API with the agent's bearer token (an ``agent``-role API token from the
app, or the legacy ``CLIMATE_AGENT_TOKEN``) and returns a SHORT text summary: numbers rounded,
lists capped (about 20 rows), units spelled out (°F, minutes), every 90% interval the API
reports, and the Open-Meteo attribution wherever weather is summarized. None of these tools
can reach a thermostat: the agent role reads, proposes and signs off inside its ranges; it
cannot call any control write (holds, mode, settings), set-up step or token endpoint.

``TOOLS`` is the registry both servers are built from (``tools.py`` for the Agent SDK,
``mcp_server.py`` for Claude Code / Desktop). A tool's parameters are its method signature;
``args_model`` turns that into a pydantic model and JSON schema.
"""

from __future__ import annotations

import inspect
import json
import math
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as Date
from functools import cache
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from climate_agent.api import ApiClient, ApiError

OPEN_METEO_ATTRIBUTION = "Weather data by Open-Meteo.com"
MAX_ROWS = 20
COMPUTE_TIMEOUT_S = 120.0
ACTIONS_MAX = 500  # list_actions summarizes up to this many (the API allows 1000)
ACTIONS_LISTED = 15  # anomalies listed in full
ACTIONS_NEWEST = 5  # plus the newest few others

UnitKey = Literal["main", "up", "bed"]
RoomKey = Literal[
    "hallway", "school_room", "living_room", "kitchen", "twins_room", "olive_room",
    "toy_room", "girls_room", "bedroom", "office", "foyer",
]
UNIT_NAMES: dict[str, str] = {"main": "Main floor", "up": "Upstairs", "bed": "Bed / Office wing"}

# Mirrors climate.control.policy.PolicyParams (field -> JSON type). The API validates
# values and limits; this pre-check catches misspelled names before a round trip.
POLICY_PARAM_TYPES: dict[str, type] = {
    "linked_floors_enabled": bool,
    "linked_offset_f": float,
    "linked_heat_gap_f": float,
    "setback_gap_f": float,
    "recovery_lead_min": int,
    "precool_enabled": bool,
    "precool_degrees_f": float,
    "precool_start_hour": int,
    "precool_min_forecast_high_f": float,
    "bed_wing_independent": bool,
}


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False


class ToolInputError(ValueError):
    """The arguments are well-formed but unusable; reported to Claude as a tool error."""


# ---------------------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------------------


def num(x: Any, nd: int = 0, unit: str = "", signed: bool = False) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    if isinstance(x, bool):
        return str(x)
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    sign = "+" if signed and v > 0 else ""
    text = f"{v:,.{nd}f}" if abs(v) >= 1000 else f"{v:.{nd}f}"
    if text in ("-0", "-0.0", "-0.00"):
        text = text[1:]
    return f"{sign}{text}{unit}"


def temp(x: Any, nd: int = 1) -> str:
    return "unknown" if x is None else num(x, nd, "°F")


def minutes(x: Any) -> str:
    return num(x, 0, " min")


def pct(x: Any, nd: int = 1, signed: bool = False) -> str:
    return num(x, nd, "%", signed=signed)


def frac_pct(x: Any, nd: int = 1) -> str:
    """A fraction (0.14) shown as a percentage (14.0%)."""
    return "n/a" if x is None else pct(float(x) * 100.0, nd)


def interval(lo: Any, hi: Any, nd: int = 1, unit: str = "") -> str:
    if lo is None or hi is None:
        return "90% interval n/a"
    return f"90% interval [{num(lo, nd, unit)}, {num(hi, nd, unit)}]"


def pair_interval(ci: Any, nd: int = 1, unit: str = "") -> str:
    if isinstance(ci, (list, tuple)) and len(ci) == 2:
        return interval(ci[0], ci[1], nd, unit)
    return "90% interval n/a"


def spans_zero(lo: Any, hi: Any) -> bool:
    return lo is not None and hi is not None and float(lo) <= 0.0 <= float(hi)


def clip(text: Any, n: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def yes_no(value: Any) -> str:
    """Booleans spelled out (``clip(False)`` would print nothing)."""
    return "yes" if value else "no"


def metric(value: Any) -> str:
    """One model-fit metric: booleans as yes/no, numbers rounded, anything else clipped."""
    if isinstance(value, bool):
        return yes_no(value)
    if isinstance(value, int):
        return num(value, 0)
    if isinstance(value, float):
        return num(value, 3)
    if value is None:
        return "n/a"
    return clip(value, 30)


def compact(value: Any, n: int = 300) -> str:
    """Compact JSON for small dicts (numbers rounded), clipped."""

    def _round(v: Any) -> Any:
        if isinstance(v, float):
            return round(v, 2)
        if isinstance(v, dict):
            return {k: _round(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_round(x) for x in v[:12]]
        return v

    return clip(json.dumps(_round(value), default=str, separators=(",", ":")), n)


def capped(rows: list[str], limit: int = MAX_ROWS) -> list[str]:
    if len(rows) <= limit:
        return rows
    return rows[:limit] + [f"(+{len(rows) - limit} more not shown)"]


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def row_date(row: dict[str, Any]) -> str:
    """The API's date fields are named ``Date`` (pydantic field) - accept either spelling."""
    return str(row.get("Date") or row.get("date") or row.get("day") or "?")


def unit_name(key: Any) -> str:
    return UNIT_NAMES.get(str(key), str(key))


def stats(values: Iterable[Any]) -> tuple[float, float, float] | None:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return min(vals), sum(vals) / len(vals), max(vals)


def check_policy_params(params: dict[str, Any], what: str = "params") -> dict[str, Any]:
    """Names must be PolicyParams fields and values the right JSON type. Limits are the API's job."""
    if not params:
        raise ToolInputError(f"{what} must change at least one policy parameter.")
    unknown = sorted(set(params) - set(POLICY_PARAM_TYPES))
    if unknown:
        raise ToolInputError(
            f"Unknown policy parameter(s) in {what}: {', '.join(unknown)}. Valid: {', '.join(POLICY_PARAM_TYPES)}."
        )
    clean: dict[str, Any] = {}
    for key, value in params.items():
        want = POLICY_PARAM_TYPES[key]
        if want is bool:
            if not isinstance(value, bool):
                raise ToolInputError(f"{key} must be true or false (owner-only switch).")
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolInputError(f"{key} must be a number.")
        elif want is int:
            if float(value) != int(value):
                raise ToolInputError(f"{key} must be a whole number.")
            value = int(value)
        else:
            value = float(value)
        clean[key] = value
    return clean


def format_api_error(exc: ApiError) -> str:
    """The text Claude reads for a failed call. A refused token (401, or a 403/503 from the
    API's auth layer) says how the owner fixes it; a plain 403 is the API declining the
    request itself (e.g. a sign-off outside Claude's ranges) and is left to the owner."""
    if exc.status_code is None:
        return f"The API could not be reached ({exc.detail}). Carry on without this data and say so in the report."
    hint = exc.auth_hint
    if hint and exc.status_code == 401:
        return (
            f"The API refused this tool's token (401: {exc.detail}). Every API tool will fail until the owner "
            f"fixes it; stop and tell the owner: {hint}"
        )
    if hint and exc.status_code == 403:
        return (
            f"Refused (403): {exc.detail}. This tool's API token may not do that; leave it for the owner. "
            f"If Claude should be able to: {hint}"
        )
    if hint:
        return f"API error {exc.status_code}: {exc.detail}. {hint}"
    if exc.status_code == 403:
        return f"Refused (403): {exc.detail}. The API does not let Claude do that; leave it for the owner."
    if exc.status_code == 404:
        return f"Not found (404): {exc.detail}."
    if exc.status_code == 409:
        return f"Conflict (409): {exc.detail}."
    if exc.status_code in (400, 422):
        return f"Rejected ({exc.status_code}): {exc.detail}. Fix the arguments and try again."
    if exc.is_client_error:
        return f"API error {exc.status_code}: {exc.detail}."
    return f"API server error {exc.status_code}: {exc.detail}. Try once more later, or carry on without it."


class ArmIn(BaseModel):
    """One experiment arm: a short key, a label, and the partial PolicyParams it runs."""

    model_config = ConfigDict(extra="forbid")
    key: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,24}$", description="Short id, e.g. 'offset_1' (a-z, 0-9, _)")]
    label: Annotated[str, Field(min_length=1, max_length=80, description="Human label, e.g. 'Main 1°F under upstairs'")]
    params: Annotated[
        dict[str, Any],
        Field(default_factory=dict, description="Partial PolicyParams for this arm; {} = current policy"),
    ]


# ---------------------------------------------------------------------------------------
# the toolkit
# ---------------------------------------------------------------------------------------


class Toolkit:
    """Tools bound to one API client. ``run_id`` tags reports published during an agent run."""

    def __init__(self, api: ApiClient, run_id: int | None = None):
        self.api = api
        self.run_id = run_id
        self._tz: ZoneInfo | None = None

    # -- plumbing ----------------------------------------------------------------------

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Validate arguments against the tool's signature and run it. Never raises for
        API or input problems: those come back as ``is_error`` results Claude can read."""
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            return ToolResult(f"Unknown tool {name!r}.", is_error=True)
        model = args_model(spec.name)
        try:
            parsed = model.model_validate(arguments or {})
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}" for err in exc.errors()[:6]
            )
            return ToolResult(f"Invalid arguments for {name}: {problems}", is_error=True)
        kwargs = {field: getattr(parsed, field) for field in model.model_fields}
        try:
            text = await spec.method(self, **kwargs)
        except ToolInputError as exc:
            return ToolResult(str(exc), is_error=True)
        except ApiError as exc:
            return ToolResult(format_api_error(exc), is_error=True)
        return ToolResult(text)

    async def tz(self) -> ZoneInfo:
        if self._tz is None:
            name = "UTC"
            try:
                settings = await self.api.get("/control/settings")
                name = (settings.get("location") or {}).get("tz") or "UTC"
            except ApiError:
                name = "UTC"
            try:
                self._tz = ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                self._tz = ZoneInfo("UTC")
        return self._tz

    def _remember_tz(self, name: Any) -> None:
        if isinstance(name, str) and name:
            try:
                self._tz = ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                pass

    async def local(self, value: Any, fmt: str = "%m-%d %H:%M") -> str:
        dt = parse_ts(value)
        if dt is None:
            return "?"
        return dt.astimezone(await self.tz()).strftime(fmt)

    # -- read --------------------------------------------------------------------------

    async def get_house_status(self) -> str:
        s = await self.api.get("/status")
        self._remember_tz(s.get("tz"))
        tz = await self.tz()
        now = parse_ts(s.get("now"))
        src = s.get("source") or {}
        ctl = s.get("controller") or {}
        lines = [
            (f"Now {now.astimezone(tz).strftime('%Y-%m-%d %H:%M') if now else '?'} ({s.get('tz')}). "
            f"Source {src.get('kind')} ({'ok' if src.get('ok') else 'NOT OK: ' + clip(src.get('detail'), 80)}). "
            f"Controller {ctl.get('mode')}, policy v{ctl.get('policy_version_id')}."),
            f"House: {'EMPTY' if s.get('house_empty') else 'occupied'} ({clip(s.get('house_empty_reason'), 100) or 'no reason given'}).",
        ]
        w = s.get("weather")
        if w:
            lines.append(
                f"Weather now {temp(w.get('temp_f'))}, RH {pct(w.get('rh'), 0)}, cloud {pct(w.get('cloud_cover'), 0)}, "
                f"forecast high {temp(w.get('forecast_high_f'), 0)} / low {temp(w.get('forecast_low_f'), 0)}. "
                f"{w.get('attribution') or OPEN_METEO_ATTRIBUTION}"
            )
        lines.append("Units:")
        events: dict[str, tuple[dict[str, Any], list[str]]] = {}
        ahead: list[str] = []
        for u in s.get("units") or []:
            hold_txt = await self._hold_text(u)
            tgt = u.get("target")
            tgt_txt = ""
            if tgt:
                tgt_txt = f"; policy wants {temp(tgt.get('heat_f'))}/{temp(tgt.get('cool_f'))} [{tgt.get('rule')}: {clip(tgt.get('reason'), 90)}]"
            lines.append(
                f"- {u.get('name')} [{u.get('unit_key')}]: {u.get('call')}, {temp(u.get('zone_temp_f'))}, "
                f"RH {pct(u.get('zone_humidity'), 0)}, setpoints {temp(u.get('heat_sp_f'))}/{temp(u.get('cool_sp_f'))}"
                f"{hold_txt}; today {minutes(u.get('today_runtime_min'))}, duty last hour {pct(u.get('duty_last_hour_pct'), 0)}, "
                f"maxed {minutes(u.get('maxed_minutes_today'))}{'' if u.get('connected', True) else ', DISCONNECTED'}{tgt_txt}"
            )
            ev = u.get("utility_event")
            if isinstance(ev, dict):
                key = str(ev.get("event_key") or ev.get("id"))
                events.setdefault(key, (ev, []))[1].append(str(u.get("name")))
            for e in u.get("upcoming_events") or []:
                if e.get("event_type") != "demandResponse":  # utility events are listed below
                    ahead.append(f"{e.get('event_type')} on {u.get('name')} {await self.local(e.get('start'), '%a %m-%d %H:%M')}"
                                 f"–{await self.local(e.get('end'), '%a %m-%d %H:%M')}")
        if events:
            lines.append("Utility events (the controller stands aside while one runs; only the owner or the owner's skip rules opt out):")
            lines += [await self._event_line(ev, names) for ev, names in list(events.values())[:5]]
        if ahead:
            lines.append("Ahead: " + "; ".join(ahead[:5]))
        by_unit: dict[str, list[str]] = defaultdict(list)
        for r in s.get("rooms") or []:
            if r.get("has_sensor"):
                t = temp(r.get("temp_f"))
                if r.get("stale"):
                    t += " (stale)"
            else:
                t = "no sensor (temp unknown)"
            star = "*" if r.get("is_priority") else ""
            by_unit[str(r.get("unit_key"))].append(f"{r.get('name')}{star} {t} {r.get('state')}")
        if by_unit:
            lines.append("Rooms (* = room the unit steers for now):")
            for key, rows in by_unit.items():
                lines.append(f"- {unit_name(key)}: " + "; ".join(rows))
        alerts = s.get("alerts") or []
        if alerts:
            lines.append(f"Open alerts ({len(alerts)}): " + "; ".join(f"[{a.get('level')}] {clip(a.get('title'), 70)}" for a in alerts[:5]))
        agent = s.get("agent") or {}
        if agent.get("token_warning"):
            lines.append(f"Agent: {clip(agent.get('token_warning'), 160)}")
        return "\n".join(lines)

    async def _hold_text(self, u: dict[str, Any]) -> str:
        """Whose hold runs on a unit (the API's label), a person's hold and a resume back-off."""
        hold = u.get("hold") or {}
        label = u.get("hold_label")
        if label:
            sp = f" {temp(hold.get('heat_f'), 1)}/{temp(hold.get('cool_f'), 1)}" if hold.get("heat_f") is not None else ""
            text = f", hold{sp} [{u.get('hold_owner') or '?'}]: {clip(label, 80)}"
        elif hold:  # an API from before hold labels
            end = await self.local(hold.get("end"), "%H:%M") if hold.get("end") else "no end"
            text = f", hold {temp(hold.get('heat_f'), 1)}/{temp(hold.get('cool_f'), 1)} until {end}{' (ours)' if hold.get('set_by_us') else ' (NOT ours)'}"
        else:
            text = ""
        if u.get("person_hold"):
            text += " (a person's hold: the controller writes nothing to this unit until it ends or the owner taps Back to automatic)"
        if u.get("resume_backoff_until"):
            text += f"; after a Resume the ecobee schedule runs until {await self.local(u.get('resume_backoff_until'), '%a %H:%M')} (controller waits)"
        return text

    async def _event_line(self, ev: dict[str, Any], unit_names: list[str]) -> str:
        status = {"opted_out": "skipped (opted out)"}.get(str(ev.get("status")), str(ev.get("status")))
        window = f"{await self.local(ev.get('start_at'), '%a %m-%d %H:%M')}–{await self.local(ev.get('end_at'), '%H:%M')}"
        name = clip(ev.get("name") or "utility event", 40)
        parts = [f"- {name} {status}, {window} on {', '.join(unit_names)}: {clip(ev.get('change_label'), 40)}"]
        if ev.get("is_optional") is False:
            parts.append("mandatory (cannot be skipped)")
        if ev.get("skip"):
            why = f" ({clip(ev.get('skip_reason'), 60)})" if ev.get("skip_reason") else ""
            parts.append(f"skip {ev.get('skip')} by {ev.get('skip_by') or '?'}{why}")
        if ev.get("prep_label"):
            parts.append(clip(ev.get("prep_label"), 110))
        return "; ".join(parts)

    async def query_runtime(
        self,
        days: Annotated[int, Field(ge=1, le=120, description="Days back from today")] = 14,
        unit_key: Annotated[UnitKey | None, Field(description="Limit to one unit; omit for the whole house")] = None,
    ) -> str:
        rows = await self.api.get("/runtime/daily", {"days": days})
        if unit_key:
            rows = [r for r in rows if r.get("unit_key") == unit_key]
        if not rows:
            return f"No runtime rows in the last {days} days."
        # Expected minutes from a baseline that fails its checks are not comparable.
        failing: set[tuple[str, str]] = set()
        baselines_read = True
        try:
            for b in await self.api.get("/analytics/baselines") or []:
                if isinstance(b, dict) and not b.get("passes"):
                    failing.add((str(b.get("unit_key")), str(b.get("mode"))))
        except ApiError:
            baselines_read = False
        per_unit: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        failing_modes: dict[str, set[str]] = defaultdict(set)
        per_day: dict[str, dict[str, Any]] = {}
        for r in rows:
            run = float(r.get("cool_min") or 0) + float(r.get("heat_min") or 0)
            key = str(r.get("unit_key"))
            u = per_unit[key]
            u["actual"] += run
            u["aux"] += float(r.get("aux_min") or 0)
            u["maxed"] += float(r.get("maxed_min") or 0)
            fails = (key, str(r.get("mode"))) in failing
            comparable = r.get("expected_min") is not None and not fails
            if comparable:
                u["actual_with_exp"] += run
                u["expected"] += float(r["expected_min"])
                u["days_with_exp"] += 1
            elif fails:
                u["days_failing"] += 1
                failing_modes[key].add(str(r.get("mode")))
            d = per_day.setdefault(row_date(r), {"actual": 0.0, "expected": 0.0, "exp_n": 0, "units": 0,
                                                   "modes": set(), "out": r.get("outdoor_mean_f"),
                                                   "max": r.get("outdoor_max_f"), "maxed": 0.0, "failing": 0})
            d["actual"] += run
            d["units"] += 1
            d["maxed"] += float(r.get("maxed_min") or 0)
            if r.get("mode"):
                d["modes"].add(r["mode"])
            if comparable:
                d["expected"] += float(r["expected_min"])
                d["exp_n"] += 1
            elif fails:
                d["failing"] += 1
        scope = unit_name(unit_key) if unit_key else "house"
        lines = [f"Runtime, last {days} days, {scope} (stage-1 minutes; expected = weather-normalized baseline):"]
        for key, u in per_unit.items():
            parts = []
            if u["days_with_exp"]:
                diff = (u["actual_with_exp"] - u["expected"]) / u["expected"] * 100 if u["expected"] else None
                parts.append(f"{minutes(u['actual_with_exp'])} vs {minutes(u['expected'])} expected on {int(u['days_with_exp'])} baseline days ({pct(diff, 1, signed=True)})")
            if u["days_failing"]:
                modes = "/".join(sorted(failing_modes[key]))
                parts.append(f"{int(u['days_failing'])} day(s) not comparable: the {modes} baseline fails its checks")
            exp_txt = "; ".join(parts) or "no baseline"
            aux = f", aux {minutes(u['aux'])}" if u["aux"] else ""
            lines.append(f"- {unit_name(key)}: {minutes(u['actual'])} total{aux}, maxed {minutes(u['maxed'])}; {exp_txt}")
        lines.append("Daily totals (newest first):")
        day_rows = []
        for day in sorted(per_day, reverse=True):
            d = per_day[day]
            if d["exp_n"] == d["units"] and d["exp_n"]:
                exp = minutes(d["expected"])
            elif d["failing"]:
                exp = "n/a, a baseline fails its checks"
            else:
                exp = "n/a"
            modes = "/".join(sorted(d["modes"])) or "-"
            day_rows.append(
                f"{day} {modes}: {minutes(d['actual'])} (exp {exp}), outdoor mean {temp(d['out'], 0)} max {temp(d['max'], 0)}, maxed {minutes(d['maxed'])}"
            )
        lines += capped(day_rows)
        if not baselines_read:
            lines.append("Could not read the baseline checks: treat expected minutes as unverified (see baseline_report).")
        lines.append("Raw actual-vs-expected is not a savings claim; use savings_report for the 90% interval.")
        return "\n".join(lines)

    async def query_room(
        self,
        room_key: Annotated[RoomKey, Field(description="Room key")],
        hours: Annotated[int, Field(ge=1, le=168, description="Hours of history")] = 24,
    ) -> str:
        h = await self.api.get(f"/rooms/{room_key}/history", {"hours": hours})
        pts = h.get("points") or []
        sps = h.get("setpoints") or []
        tz = await self.tz()
        lines = [f"{room_key}, last {hours} h ({len(pts)} points):"]
        st = stats(p.get("temp_f") for p in pts)
        if st is None:
            lines.append("Temperature: unknown (no sensor or no readings). Do not estimate it.")
        else:
            last = next((p for p in reversed(pts) if p.get("temp_f") is not None), None)
            last_txt = f", latest {temp(last.get('temp_f'))} at {await self.local(last.get('ts'), '%H:%M')}" if last else ""
            lines.append(f"Temperature: min {temp(st[0])} / mean {temp(st[1])} / max {temp(st[2])}{last_txt}.")
        occ = [p.get("occupied") for p in pts if p.get("occupied") is not None]
        if occ:
            states = Counter(str(p.get("state") or "unknown") for p in pts)
            lines.append(
                f"Occupied {pct(sum(1 for o in occ if o) / len(occ) * 100, 0)} of points; states: "
                + ", ".join(f"{k} {v}" for k, v in states.most_common())
            )
        hs = stats(p.get("heat_sp_f") for p in sps)
        cs = stats(p.get("cool_sp_f") for p in sps)
        if hs or cs:
            lines.append(
                f"Unit setpoints: heat {temp(hs[0]) if hs else 'n/a'}–{temp(hs[2]) if hs else 'n/a'}, "
                f"cool {temp(cs[0]) if cs else 'n/a'}–{temp(cs[2]) if cs else 'n/a'} ({len(sps)} points)."
            )
        if pts and st is not None:
            bucket_h = max(1, math.ceil(hours / 12))
            buckets: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
            for p in pts:
                ts = parse_ts(p.get("ts"))
                if ts is None:
                    continue
                lt = ts.astimezone(tz)
                key = lt.replace(minute=0, second=0, microsecond=0, hour=(lt.hour // bucket_h) * bucket_h) if bucket_h < 24 else lt.replace(hour=0, minute=0, second=0, microsecond=0)
                buckets[key].append(p)
            rows = []
            for key in sorted(buckets):
                b = buckets[key]
                bt = stats(p.get("temp_f") for p in b)
                bo = [p.get("occupied") for p in b if p.get("occupied") is not None]
                occ_txt = f" occ {pct(sum(1 for o in bo if o) / len(bo) * 100, 0)}" if bo else ""
                rows.append(f"{key.strftime('%m-%d %H:%M')} {temp(bt[1]) if bt else 'unknown'}{occ_txt}")
            lines.append(f"{bucket_h} h buckets (local): " + " | ".join(rows[-12:]))
        return "\n".join(lines)

    async def get_weather(
        self,
        hours_back: Annotated[int, Field(ge=0, le=168, description="Observed hours back")] = 24,
        hours_ahead: Annotated[int, Field(ge=0, le=168, description="Forecast hours ahead")] = 48,
    ) -> str:
        w = await self.api.get("/weather", {"hours_back": hours_back, "hours_ahead": hours_ahead})
        pts = w.get("points") or []
        tz = await self.tz()
        lines = [f"Weather (source {w.get('source')}):"]
        for kind, label in (("observed", f"Observed, last {hours_back} h"), ("forecast", f"Forecast, next {hours_ahead} h")):
            sel = [p for p in pts if p.get("kind") == kind]
            st = stats(p.get("temp_f") for p in sel)
            if not sel or st is None:
                lines.append(f"{label}: no data.")
                continue
            cloud = stats(p.get("cloud_cover") for p in sel)
            sun = stats(p.get("shortwave_wm2") for p in sel)
            lines.append(
                f"{label}: {temp(st[0], 0)}–{temp(st[2], 0)} (mean {temp(st[1], 0)}), "
                f"mean cloud {pct(cloud[1], 0) if cloud else 'n/a'}, peak sun {num(sun[2] if sun else None, 0, ' W/m²')}."
            )
        days: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in pts:
            ts = parse_ts(p.get("ts"))
            if ts is not None:
                days[ts.astimezone(tz).strftime("%m-%d")].append(p)
        rows = []
        for day in sorted(days):
            st = stats(p.get("temp_f") for p in days[day])
            cloud = stats(p.get("cloud_cover") for p in days[day])
            kinds = {p.get("kind") for p in days[day]}
            tag = "fcst" if kinds == {"forecast"} else ("obs" if kinds == {"observed"} else "obs+fcst")
            if st:
                rows.append(f"{day} ({tag}) high {temp(st[2], 0)} low {temp(st[0], 0)} cloud {pct(cloud[1], 0) if cloud else 'n/a'}")
        if rows:
            lines.append("By local day: " + " | ".join(rows[-10:]))
        lines.append(w.get("attribution") or OPEN_METEO_ATTRIBUTION)
        return "\n".join(lines)

    async def baseline_report(self) -> str:
        rows = await self.api.get("/analytics/baselines")
        if not rows:
            return "No baselines fitted yet: no weather-normalized claims are possible."
        lines = ["Baselines (pass = CV(RMSE) ≤ 20% and |NMBE| ≤ 0.5%):"]
        for b in rows:
            lines.append(
                f"- {b.get('unit_key')}/{b.get('mode')}: {'PASS' if b.get('passes') else 'FAIL'}, balance {temp(b.get('balance_point_f'), 0)}, "
                f"{num(b.get('intercept_min'), 0)} min/day + {num(b.get('slope_min_per_dd'), 1)} min per degree-day, "
                f"R² {num(b.get('r2'), 2)}, CV(RMSE) {frac_pct(b.get('cvrmse'))}, NMBE {frac_pct(b.get('nmbe'), 2)}, "
                f"{b.get('n_days')} days ({b.get('train_start')}..{b.get('train_end')})"
            )
        return "\n".join(capped(lines))

    async def savings_report(
        self,
        start: Annotated[Date | None, Field(description="First day (YYYY-MM-DD); default 14 days ago")] = None,
        end: Annotated[Date | None, Field(description="Last day (YYYY-MM-DD); default yesterday")] = None,
    ) -> str:
        params = {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None}
        s = await self.api.get("/analytics/savings", params)
        lo, hi = s.get("ci90_low_pct"), s.get("ci90_high_pct")
        lines = [f"Savings {s.get('start')}..{s.get('end')} ({s.get('n_days')} days), whole house, weather-normalized:"]
        lines.append(
            f"Expected {minutes(s.get('expected_min'))}, actual {minutes(s.get('actual_min'))}, "
            f"difference {minutes(s.get('savings_min'))} ({pct(s.get('savings_pct'), 1, signed=True)} = less runtime than the weather predicts), "
            f"{interval(lo, hi, 1, '%')}."
        )
        if not s.get("baseline_ok"):
            lines.append("NO SAVINGS CLAIM: the baseline does not pass its checks.")
        elif lo is None or hi is None:
            lines.append("NO SAVINGS CLAIM: no 90% interval yet.")
        elif spans_zero(lo, hi):
            lines.append("The interval includes zero: not distinguishable from no change.")
        by_unit = s.get("by_unit") or []
        if by_unit:
            lines.append("By unit: " + "; ".join(
                f"{u.get('unit_key')} exp {minutes(u.get('expected_min'))} act {minutes(u.get('actual_min'))} ({pct(u.get('savings_pct'), 1, signed=True)})"
                for u in by_unit
            ))
        if s.get("note"):
            lines.append(f"Note: {clip(s.get('note'), 300)}")
        return "\n".join(lines)

    async def waterfall_report(
        self,
        week_start: Annotated[Date | None, Field(description="Monday of the week (YYYY-MM-DD); default last full week")] = None,
    ) -> str:
        w = await self.api.get("/analytics/waterfall", {"week_start": week_start.isoformat() if week_start else None})
        parts = []
        ci = w.get("strategy_ci90_min")
        for item in w.get("items") or []:
            label = item.get("label")
            if item.get("kind") == "delta":
                txt = f"{label} {num(item.get('minutes'), 0, ' min', signed=True)}"
                if str(label).lower().startswith("strategy"):
                    txt += f" ({pair_interval(ci, 0, ' min')})"
            else:
                txt = f"{label} {minutes(item.get('minutes'))}"
            parts.append(txt)
        lines = [f"Week of {w.get('week_start')} vs {w.get('prev_week_start')} (stage-1 minutes): " + " | ".join(parts)]
        if isinstance(ci, (list, tuple)) and len(ci) == 2 and spans_zero(ci[0], ci[1]):
            lines.append("The strategy interval includes zero: no strategy effect can be claimed this week.")
        if w.get("note"):
            lines.append(f"Note: {clip(w.get('note'), 300)}")
        return "\n".join(lines)

    async def coupling_report(
        self,
        days: Annotated[int, Field(ge=7, le=365, description="Days of hourly data")] = 30,
    ) -> str:
        c = await self.api.get("/analytics/coupling", {"days": days})
        lines = [
            f"Floor coupling, last {c.get('days')} days ({c.get('n_hours')} hours):",
            (f"Upstairs runtime {num(c.get('coef_min_per_degf'), 2, signed=True)} min/hour per °F the main floor sits above upstairs, "
            f"{pair_interval(c.get('ci90'), 2)}."),
            f"Placebo (bed wing, should be ~0): {num(c.get('placebo_coef'), 2, signed=True)}, {pair_interval(c.get('placebo_ci90'), 2)}.",
        ]
        if c.get("interpretation"):
            lines.append(f"Interpretation: {clip(c.get('interpretation'), 400)}")
        return "\n".join(lines)

    async def comfort_report(
        self,
        days: Annotated[int, Field(ge=1, le=60, description="Days back")] = 7,
    ) -> str:
        rows = await self.api.get("/analytics/comfort", {"days": days})
        if not rows:
            return f"No comfort data for the last {days} days."
        lines = [f"Comfort, last {days} days (target: occupied rooms in band ≥ 97% of occupied minutes):"]
        for r in rows:
            band = r.get("in_band_pct")
            if band is None:
                lines.append(f"- {r.get('room_key')}: in-band unknown (no sensor or no occupied minutes), occupied {minutes(r.get('occupied_min'))}")
                continue
            flag = "" if float(band) >= 97.0 else " BELOW TARGET"
            lines.append(
                f"- {r.get('room_key')}: {pct(band)} of {minutes(r.get('occupied_min'))} occupied, "
                f"worst excursion {num(r.get('worst_excursion_f'), 1, '°F')}{flag}"
            )
        return "\n".join(capped(lines))

    async def drift_report(self) -> str:
        d = await self.api.get("/analytics/drift")
        units = d.get("units") or []
        lines = ["Baseline drift (recent residuals vs the weather baseline):"]
        for u in units:
            lines.append(
                f"- {u.get('unit_key')}/{u.get('mode')}: {'DRIFTING' if u.get('drifting') else 'stable'}, "
                f"residual mean {pct(u.get('resid_mean_pct'), 1, signed=True)}, z {num(u.get('z'), 1, signed=True)} over {u.get('recent_days')} days"
            )
        if not units:
            lines.append("- no units checked (no baselines yet)")
        if d.get("note"):
            lines.append(f"Note: {clip(d.get('note'), 300)}")
        return "\n".join(lines)

    async def natural_experiment_report(
        self,
        days: Annotated[int, Field(ge=14, le=365, description="Days of history to mine")] = 90,
    ) -> str:
        n = await self.api.get("/analytics/natural-experiments", {"days": days})
        events = n.get("events") or []
        lines = [
            f"Natural experiments (warm main-floor afternoons), last {n.get('days')} days: {len(events)} events.",
            f"Estimate: {num(n.get('estimate_min_per_event'), 0, ' min', signed=True)} upstairs runtime per event, {pair_interval(n.get('ci90'), 0, ' min')}.",
            (f"Checks (both should be ~0): placebo with fake event times {num(n.get('placebo_estimate'), 0, ' min', signed=True)}; "
            f"bed wing {num(n.get('bed_wing_estimate'), 0, ' min', signed=True)}."),
        ]
        rows = [
            f"{row_date(e)} main floated {num(e.get('main_floor_float_f'), 1, '°F', signed=True)}, up {minutes(e.get('up_runtime_min'))} vs {minutes(e.get('expected_up_runtime_min'))} expected"
            for e in sorted(events, key=row_date, reverse=True)
        ]
        if rows:
            lines.append("Events (newest first):")
            lines += capped(rows, 10)
        if n.get("note"):
            lines.append(f"Note: {clip(n.get('note'), 300)}")
        return "\n".join(lines)

    async def _action_line(self, a: dict[str, Any]) -> str:
        rb = "readback ok" if a.get("readback_ok") else ("READBACK FAILED" if a.get("readback_ok") is False else "no readback")
        err = f", error: {clip(a.get('error'), 60)}" if a.get("error") else ""
        return (
            f"#{a.get('id')} {await self.local(a.get('ts'))} {a.get('unit_key')} {a.get('action')} {a.get('status')} "
            f"({a.get('mode')}, {a.get('channel')}, rule {a.get('rule') or '-'}) {rb}{err}: {clip(a.get('reason'), 80)}"
        )

    async def list_actions(
        self,
        limit: Annotated[int, Field(ge=1, le=ACTIONS_MAX, description="How many recent actions to summarize (1-500)")] = 20,
        unit_key: Annotated[UnitKey | None, Field(description="Only this unit")] = None,
    ) -> str:
        rows = await self.api.get("/control/actions", {"limit": limit, "unit_key": unit_key})
        if not rows:
            return "No control actions recorded."
        # Counts cover EVERY fetched row; only anomalies and the newest few are listed.
        by_unit: dict[str, Counter[str]] = defaultdict(Counter)
        readback_failed = blocked = skipped = failed = 0
        anomalies: list[dict[str, Any]] = []
        for a in rows:
            status = str(a.get("status"))
            by_unit[str(a.get("unit_key"))][status] += 1
            is_blocked = status == "failed" and str(a.get("error") or "").startswith("Not sent")
            blocked += int(is_blocked)
            failed += int(status == "failed" and not is_blocked)
            skipped += int(status == "skipped")
            readback_failed += int(a.get("readback_ok") is False)
            if status in ("failed", "skipped") or a.get("readback_ok") is False:
                anomalies.append(a)
        first, last = rows[-1].get("ts"), rows[0].get("ts")
        lines = [
            (f"Control actions: the newest {len(rows)} (asked for up to {limit}), "
            f"{await self.local(first)} to {await self.local(last)} local time."),
            "By unit and status: " + "; ".join(
                f"{key} " + ", ".join(f"{st} {n}" for st, n in sorted(c.items())) for key, c in sorted(by_unit.items())
            ) + ".",
            (f"Read-back failures {readback_failed}; blocked by guardrails (not sent) {blocked}; other failures {failed}; "
            f"skipped {skipped}."),
        ]
        if anomalies:
            lines.append(f"Anomalies (failed, read-back mismatch or skipped; newest first, {len(anomalies)}):")
            lines += [await self._action_line(a) for a in anomalies[:ACTIONS_LISTED]]
            if len(anomalies) > ACTIONS_LISTED:
                lines.append(f"(+{len(anomalies) - ACTIONS_LISTED} more anomalies not shown; counted above)")
        else:
            lines.append("No anomalies: nothing failed, skipped or mismatched on read-back.")
        listed = {a.get("id") for a in anomalies[:ACTIONS_LISTED]}
        newest = [a for a in rows[:ACTIONS_NEWEST] if a.get("id") not in listed]
        if newest:
            lines.append("Newest other actions:")
            lines += [await self._action_line(a) for a in newest]
        return "\n".join(lines)

    async def explain_action(
        self,
        action_id: Annotated[int, Field(ge=1, description="Control action id (from list_actions)")],
    ) -> str:
        rows = await self.api.get("/control/actions", {"limit": 500})
        a = next((r for r in rows if r.get("id") == action_id), None)
        if a is None:
            raise ToolInputError(f"Action #{action_id} is not among the 500 most recent actions.")
        lines = [
            f"Action #{a.get('id')} at {await self.local(a.get('ts'), '%Y-%m-%d %H:%M')} on {unit_name(a.get('unit_key'))}:",
            f"{a.get('action')} by {a.get('actor')} in {a.get('mode')} mode via {a.get('channel')}; status {a.get('status')}; rule {a.get('rule') or '-'}.",
            f"Why: {clip(a.get('reason'), 300)}",
            f"Before: {compact(a.get('before'))}",
            f"Request: {compact(a.get('request'))}",
            f"Read-back: {compact(a.get('readback'))} ({'matches' if a.get('readback_ok') else 'MISMATCH' if a.get('readback_ok') is False else 'not checked'})",
        ]
        if a.get("error"):
            lines.append(f"Error: {clip(a.get('error'), 300)}")
        if a.get("completed_at"):
            lines.append(f"Completed {await self.local(a.get('completed_at'))}.")
        return "\n".join(lines)

    async def get_plan(self) -> str:
        p = await self.api.get("/control/plan")
        lines = [f"Controller plan at {await self.local(p.get('at'))} (mode {p.get('mode')}):"]
        for row in p.get("rows") or []:
            t = row.get("target") or {}
            g = row.get("guard") or {}
            guard = "ok" if g.get("ok") else f"BLOCKED: {clip(g.get('blocked_reason'), 80)}"
            if g.get("clamped"):
                guard += f", clamped to {temp(g.get('heat_f'))}/{temp(g.get('cool_f'))}"
            if g.get("violations"):
                guard += f", violations: {clip('; '.join(map(str, g.get('violations'))), 100)}"
            lines.append(
                f"- {unit_name(t.get('unit_key'))}: wants {temp(t.get('heat_f'))}/{temp(t.get('cool_f'))} ({t.get('rule')}, {t.get('desired')}) "
                f"vs current {temp(row.get('current_heat_f'))}/{temp(row.get('current_cool_f'))}; guard {guard}; "
                f"{'would write' if row.get('would_write') else 'no write'}. {clip(t.get('reason'), 100)}"
            )
        return "\n".join(lines)

    async def list_reports(
        self,
        kind: Annotated[Literal["daily", "weekly", "nightly", "anomaly", "note"] | None, Field(description="Filter by kind")] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="How many")] = 10,
    ) -> str:
        rows = await self.api.get("/reports", {"kind": kind, "limit": limit})
        if not rows:
            return "No reports yet."
        out = [
            f"#{r.get('id')} {await self.local(r.get('created_at'), '%Y-%m-%d')} {r.get('kind')} by {r.get('author')}: "
            f"{clip(r.get('title'), 90)}" + (f" ({r.get('period_start')}..{r.get('period_end')})" if r.get("period_start") else "")
            for r in rows
        ]
        return "\n".join(["Reports (newest first):"] + capped(out))

    async def get_report(
        self,
        report_id: Annotated[int, Field(ge=1, description="Report id (from list_reports)")],
    ) -> str:
        r = await self.api.get(f"/reports/{report_id}")
        body = str(r.get("body_md") or "")
        cut = "" if len(body) <= 3000 else f"\n…(truncated, {len(body)} chars in all)"
        data_keys = ", ".join(sorted((r.get("data") or {}).keys())[:15])
        return (
            f"Report #{r.get('id')} ({r.get('kind')} by {r.get('author')}, {await self.local(r.get('created_at'), '%Y-%m-%d %H:%M')}): "
            f"{r.get('title')}\n{body[:3000]}{cut}" + (f"\nData keys: {data_keys}" if data_keys else "")
        )

    async def get_settings(self) -> str:
        s = await self.api.get("/control/settings")
        ctl = s.get("control") or {}
        occ = s.get("occupancy") or {}
        loc = s.get("location") or {}
        self._remember_tz(loc.get("tz"))
        pol = s.get("policy") or {}
        lim = ctl.get("limits") or {}
        ranges = s.get("signoff_ranges") or {}
        backoff = ctl.get("resume_backoff_hours", ctl.get("manual_backoff_hours"))  # older APIs: the old name
        reminder = ctl.get("manual_hold_reminder_hours")
        lines = [
            (f"Controller mode {ctl.get('mode')}; act units {', '.join(ctl.get('act_units') or []) or 'none'}; holds {ctl.get('hold_hours')} h. "
            f"A person's hold always wins, however long it runs; after someone presses Resume the controller waits "
            f"{num(backoff, 1)} h; hold reminder {'after ' + num(reminder, 1) + ' h' if reminder else 'off'}."),
            f"Policy v{s.get('policy_version_id')}: " + ", ".join(f"{k}={v}" for k, v in pol.items()),
            "Claude may sign off model-queued changes only inside: "
            + ", ".join(f"{k} {num(v[0], 1)}–{num(v[1], 1)}" for k, v in ranges.items() if isinstance(v, (list, tuple)) and len(v) == 2)
            + f". Owner-only: {', '.join(s.get('owner_only_params') or [])}.",
            (f"Hard limits (owner-only): heat {temp(lim.get('min_heat_f'), 0)}–{temp(lim.get('max_heat_f'), 0)}, "
            f"cool {temp(lim.get('min_cool_f'), 0)}–{temp(lim.get('max_cool_f'), 0)}, deadband ≥ {num(lim.get('min_deadband_f'), 1, '°F')}, "
            f"≤ {num(lim.get('max_step_f'), 1, '°F')} per change, ≥ {lim.get('min_minutes_between_changes')} min between changes, "
            f"holds {lim.get('min_hold_hours')}–{lim.get('max_hold_hours')} h, indoor RH ≤ {pct(lim.get('max_indoor_rh'), 0)}."),
        ]
        bands = []
        for unit, c in (ctl.get("comfort") or {}).items():
            parts = [f"{period} {num((c.get(period) or {}).get('heat_f'), 0)}/{num((c.get(period) or {}).get('cool_f'), 0)}" for period in ("day", "night", "away")]
            bands.append(f"{unit} " + " ".join(parts))
        if bands:
            lines.append("Comfort bands heat/cool °F: " + "; ".join(bands))
        windows = []
        for room, ws in (occ.get("sleep_windows") or {}).items():
            windows.append(f"{room} " + ", ".join(f"{w.get('start')}–{w.get('end')}" for w in ws))
        if windows:
            lines.append("Sleep windows (count as occupied): " + "; ".join(windows))
        ue = s.get("utility_events")
        if isinstance(ue, dict):
            rules = [r for r in (
                "someone asleep" if ue.get("skip_when_asleep") else "",
                f"a room ≥ {temp(ue.get('skip_above_f'))} cooling" if ue.get("skip_above_f") is not None else "",
                f"a room ≤ {temp(ue.get('skip_below_f'))} heating" if ue.get("skip_below_f") is not None else "",
            ) if r]
            skip = f"on ({', '.join(rules) or 'no rule set'})" if ue.get("auto_skip") else "off"
            prep = (f"on: {num(ue.get('precondition_degrees_f'), 1, '°F')} for up to {ue.get('precondition_hours')} h, "
                    f"ending {ue.get('precondition_end_gap_min')} min before the start") if ue.get("precondition") else "off"
            lines.append(f"Utility events: alerts {'on' if ue.get('alerts') else 'off'}; automatic skip {skip}; "
                         f"pre-cool/pre-heat {prep}.")
        phones = occ.get("phones_away")
        lines.append(
            f"Occupancy: empty after {occ.get('empty_after_min')} min, house empty after {occ.get('house_empty_after_min')} min; "
            f"adults' phones {'away' if phones else 'home' if phones is False else 'unknown (treated as home)'}. "
            f"Location tz {loc.get('tz')}, confirmed {'yes' if loc.get('confirmed') else 'no'}."
        )
        return "\n".join(lines)

    async def list_experiments(self) -> str:
        rows = await self.api.get("/experiments")
        if not rows:
            return "No experiments yet."
        out = []
        for e in rows:
            cps = ", ".join(f"day {c.get('day')}" for c in e.get("checkpoints") or [])
            arms = ", ".join(str(a.get("key")) for a in e.get("arms") or [])
            dates = f" {e.get('start_date')}..{e.get('end_date')}" if e.get("start_date") else ""
            out.append(f"#{e.get('id')} {e.get('status')}{dates}: {clip(e.get('name'), 70)} (arms {arms}; {e.get('n_days')} days in {e.get('block_days')}-day blocks; checkpoints {cps or 'none'})")
        return "\n".join(["Experiments (metric: weather-normalized whole-house runtime):"] + capped(out))

    async def experiment_detail(
        self,
        experiment_id: Annotated[int, Field(ge=1, description="Experiment id")],
    ) -> str:
        d = await self.api.get(f"/experiments/{experiment_id}")
        e = d.get("experiment") or {}
        a = d.get("analysis") or {}
        sched = d.get("schedule") or []
        lines = [
            f"Experiment #{e.get('id')} '{clip(e.get('name'), 80)}' ({e.get('status')}, proposed by {e.get('proposed_by')}): {clip(e.get('hypothesis'), 200)}",
            "Arms: " + "; ".join(f"{x.get('key')} = {clip(x.get('label'), 40)} {compact(x.get('params'), 80)}" for x in e.get("arms") or []),
            "Checkpoints: " + ", ".join(
                f"day {c.get('day')} (info {num(c.get('info_fraction'), 2)}, alpha spent {num(c.get('alpha_spent'), 3)}, z* {num(c.get('z_crit'), 2)})"
                for c in e.get("checkpoints") or []
            ),
            (f"Analysis: {a.get('days_observed')} days observed, effect {pct(a.get('effect_pct'), 1, signed=True)} "
            f"({interval(a.get('ci_low_pct'), a.get('ci_high_pct'), 1, '%')}), checkpoint reached {a.get('checkpoint_reached') or 'none'}, "
            f"decision {a.get('decision')}."),
        ]
        if a.get("note"):
            lines.append(f"Note: {clip(a.get('note'), 300)}")
        if sched:
            per_arm: dict[str, list[int]] = defaultdict(lambda: [0, 0])
            for day in sched:
                per_arm[str(day.get("arm"))][0] += 1
                if day.get("included") and day.get("actual_min") is not None:
                    per_arm[str(day.get("arm"))][1] += 1
            lines.append("Days per arm (scheduled / observed and included): " + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in per_arm.items()))
        return "\n".join(lines)

    async def model_fits(self) -> str:
        rows = await self.api.get("/models")
        if not rows:
            return "No model fits yet."
        out = []
        for m in rows:
            metrics = ", ".join(f"{k} {metric(v)}" for k, v in list((m.get("metrics") or {}).items())[:6])
            scope = "/".join(str(x) for x in (m.get("unit_key"), m.get("mode")) if x)
            notes = f" Notes: {clip(m.get('notes'), 120)}" if m.get("notes") else ""
            out.append(f"#{m.get('id')} {m.get('kind')} {scope or 'house'} [{m.get('status')}] trained {m.get('train_start')}..{m.get('train_end')}: {metrics or 'no metrics'}.{notes}")
        return "\n".join(["Latest model fits:"] + capped(out))

    # -- compute -----------------------------------------------------------------------

    async def run_backtest(
        self,
        params: Annotated[dict[str, Any], Field(description="Partial PolicyParams to test against the current policy, e.g. {\"linked_offset_f\": 1.5}")],
        days: Annotated[int, Field(ge=7, le=120, description="Days of history to replay")] = 28,
    ) -> str:
        clean = check_policy_params(params)
        b = await self.api.post("/models/backtest", {"params": clean, "days": days}, timeout_s=COMPUTE_TIMEOUT_S)
        lines = [
            f"Backtest over {b.get('days')} days with the {b.get('model')} model, candidate {compact(clean, 200)}:",
            (f"Runtime current {minutes(b.get('current_runtime_min'))} vs candidate {minutes(b.get('candidate_runtime_min'))}: "
            f"{pct(b.get('delta_pct'), 1, signed=True)} ({pair_interval(b.get('ci90_pct'), 1, '%')})."),
            f"Comfort violations: current {minutes(b.get('comfort_violation_min_current'))}, candidate {minutes(b.get('comfort_violation_min_candidate'))}.",
            f"Beats the model's own uncertainty: {'YES' if b.get('beats_model_uncertainty') else 'NO (not worth a real test day)'}.",
        ]
        if b.get("note"):
            lines.append(f"Note: {clip(b.get('note'), 300)}")
        return "\n".join(lines)

    async def simulate_plan(
        self,
        params: Annotated[dict[str, Any] | None, Field(description="Partial PolicyParams; omit to simulate the current policy")] = None,
        date: Annotated[Date | None, Field(description="Day to simulate (YYYY-MM-DD); default tomorrow with the forecast")] = None,
    ) -> str:
        clean = check_policy_params(params) if params else {}
        body: dict[str, Any] = {"params": clean}
        if date is not None:
            body["date"] = date.isoformat()
        s = await self.api.post("/models/simulate", body, timeout_s=COMPUTE_TIMEOUT_S)
        lines = [(f"Simulation of {row_date(s)} with the {s.get('model')} model{' and ' + compact(clean, 150) if clean else ' (current policy)'}: "
                 f"total runtime {minutes(s.get('total_runtime_min'))}.")]
        for unit, pts in (s.get("units") or {}).items():
            st = stats(p.get("temp_f") for p in pts)
            run_min = sum(float(p.get("runtime_s") or 0) for p in pts) / 60.0
            lines.append(f"- {unit_name(unit)}: {minutes(run_min)}, temp {temp(st[0]) if st else 'n/a'}–{temp(st[2]) if st else 'n/a'}")
        if s.get("note"):
            lines.append(f"Note: {clip(s.get('note'), 300)}")
        return "\n".join(lines)

    async def estimate_power(
        self,
        effect_pct: Annotated[float, Field(gt=0, le=50, description="Effect to detect, % of whole-house runtime")] = 10.0,
        alpha: Annotated[float, Field(gt=0, lt=0.5, description="False-win rate")] = 0.1,
        power: Annotated[float, Field(gt=0.5, lt=1, description="Chance to detect a real effect")] = 0.8,
    ) -> str:
        p = await self.api.get("/experiments/power", {"effect_pct": effect_pct, "alpha": alpha, "power": power})
        return (
            f"To detect a {pct(p.get('effect_pct'), 1)} change at alpha {num(p.get('alpha'), 2)} with power {num(p.get('power'), 2)}: "
            f"{p.get('days_per_arm') or 'n/a'} days per arm, {p.get('total_days') or 'n/a'} days in all "
            f"(residual CV {frac_pct(p.get('resid_cv'))}). {clip(p.get('note'), 300)}"
        )

    async def request_refit(self) -> str:
        j = await self.api.post("/models/refit")
        return f"Refit queued as job #{j.get('id')} ({j.get('status')}). The worker runs it; check it later with job_status."

    async def job_status(
        self,
        job_id: Annotated[int, Field(ge=1, description="Job id from request_refit")],
    ) -> str:
        j = await self.api.get(f"/jobs/{job_id}")
        txt = f"Job #{j.get('id')} {j.get('kind')}: {j.get('status')}"
        if j.get("result"):
            txt += f"; result {compact(j.get('result'), 400)}"
        if j.get("error"):
            txt += f"; error {clip(j.get('error'), 300)}"
        return txt + "."

    # -- gated -------------------------------------------------------------------------

    async def review_pending_changes(self) -> str:
        changes = await self.api.get("/changes", {"limit": 1000})
        mine = [c for c in changes if c.get("needs") == "claude"]
        owners = [c for c in changes if c.get("needs") == "owner"]
        ranges: dict[str, Any] = {}
        owner_only: set[str] = set()
        if mine:
            try:
                settings = await self.api.get("/control/settings")
                ranges = settings.get("signoff_ranges") or {}
                owner_only = set(settings.get("owner_only_params") or [])
            except ApiError:
                ranges = {}
        if not mine:
            return f"No changes await your sign-off. {len(owners)} await the owner."
        lines = [f"{len(mine)} change(s) await your sign-off ({len(owners)} more await the owner). Approve or hold each; only the owner can reject:"]
        for c in mine[:MAX_ROWS]:
            payload = c.get("payload") or {}
            params = payload.get("params", payload) if isinstance(payload, dict) else {}
            inside: list[str] = []
            outside: list[str] = []
            if isinstance(params, dict) and ranges:
                for k, v in params.items():
                    r = ranges.get(k)
                    if k in owner_only:
                        outside.append(f"{k} (owner-only)")
                    elif isinstance(r, (list, tuple)) and len(r) == 2 and isinstance(v, (int, float)) and not isinstance(v, bool) and r[0] <= v <= r[1]:
                        inside.append(k)
                    else:
                        outside.append(f"{k}={v} (range {compact(r, 30) if r else 'none'})")
            range_txt = ""
            if outside:
                range_txt = " OUTSIDE your sign-off ranges: " + ", ".join(outside) + " -> hold it for the owner, do not approve."
            elif inside:
                range_txt = " All changed keys are inside your sign-off ranges."
            gates = c.get("gates") or {}
            gate_txt = "; ".join(f"{k}: {compact(v, 140)}" for k, v in list(gates.items())[:6]) or "no gate results"
            lines.append(
                f"- #{c.get('id')} [{c.get('kind')}, {c.get('status')}, by {c.get('proposed_by')}] {clip(c.get('title'), 90)}. "
                f"Change: {compact(params if params else payload, 200)}. Gates: {gate_txt}. "
                f"Rationale: {clip(c.get('rationale'), 160)}.{range_txt}"
            )
        if len(mine) > MAX_ROWS:
            lines.append(f"(+{len(mine) - MAX_ROWS} more not shown)")
        return "\n".join(lines)

    async def sign_off_change(
        self,
        change_id: Annotated[int, Field(ge=1, description="Change id from review_pending_changes")],
        decision: Annotated[
            Literal["approve", "hold"],
            Field(description="approve = start its trial window; hold = wait for more evidence or the owner. You cannot "
                              "reject: if the evidence shows harm, hold it with that evidence in the reason and the owner decides."),
        ],
        reason: Annotated[str, Field(min_length=3, max_length=2000, description="Why, citing the gate numbers and intervals")],
    ) -> str:
        c = await self.api.post(f"/changes/{change_id}/decision", {"decision": decision, "reason": reason})
        trial = ""
        if c.get("trial_start"):
            trial = f"; trial {await self.local(c.get('trial_start'))} to {await self.local(c.get('trial_end'))}"
        return f"Recorded {decision} on change #{c.get('id')} '{clip(c.get('title'), 80)}': status now {c.get('status')}, next decision by {c.get('needs')}{trial}."

    async def propose_policy_change(
        self,
        title: Annotated[str, Field(min_length=3, max_length=200, description="The change, e.g. 'Main floor 1.5°F under upstairs'")],
        rationale: Annotated[str, Field(max_length=4000, description="Evidence: numbers with 90% intervals, backtest result")],
        params: Annotated[dict[str, Any], Field(description="Only the PolicyParams keys being changed, e.g. {\"linked_offset_f\": 1.5}")],
    ) -> str:
        clean = check_policy_params(params)
        c = await self.api.post("/changes", {"title": title, "rationale": rationale, "params": clean})
        return (
            f"Proposed change #{c.get('id')} '{clip(c.get('title'), 80)}' ({compact(clean, 150)}): status {c.get('status')}. "
            f"It goes through backtest, shadow days and then the owner decides (next: {c.get('needs')})."
        )

    async def propose_experiment(
        self,
        name: Annotated[str, Field(min_length=3, max_length=120, description="Short name")],
        hypothesis: Annotated[str, Field(min_length=3, max_length=2000, description="What should change and by how much, with the power estimate")],
        arms: Annotated[list[ArmIn], Field(min_length=2, max_length=3, description="2-3 arms; the first is usually the current policy with params {}")],
        n_days: Annotated[int, Field(ge=6, le=180, description="Total days")] = 28,
        block_days: Annotated[int, Field(ge=1, le=7, description="Days per switchback block")] = 2,
        n_checkpoints: Annotated[int, Field(ge=1, le=3, description="Pre-planned looks (alpha spending)")] = 3,
        alpha: Annotated[float, Field(gt=0, lt=0.5, description="Overall false-win rate")] = 0.1,
    ) -> str:
        arm_dicts = []
        for arm in arms:
            arm_params = check_policy_params(arm.params, f"arm {arm.key}") if arm.params else {}
            arm_dicts.append({"key": arm.key, "label": arm.label, "params": arm_params})
        if len({a["key"] for a in arm_dicts}) != len(arm_dicts):
            raise ToolInputError("Arm keys must be unique.")
        e = await self.api.post("/experiments", {
            "name": name, "hypothesis": hypothesis, "arms": arm_dicts, "n_days": n_days,
            "block_days": block_days, "n_checkpoints": n_checkpoints, "alpha": alpha,
        })
        cps = ", ".join(f"day {c.get('day')}" for c in e.get("checkpoints") or [])
        return (
            f"Proposed experiment #{e.get('id')} '{clip(e.get('name'), 80)}': status {e.get('status')}, {e.get('n_days')} days, "
            f"checkpoints {cps or 'n/a'}, metric {e.get('metric')}. The owner approves or rejects it."
        )

    async def publish_report(
        self,
        kind: Annotated[Literal["nightly", "weekly", "anomaly", "note"], Field(description="Report kind")],
        title: Annotated[str, Field(min_length=3, max_length=200, description="Title")],
        body_md: Annotated[str, Field(min_length=1, max_length=40000, description="Markdown body")],
        data: Annotated[dict[str, Any] | None, Field(description="Optional key numbers for charts")] = None,
        period_start: Annotated[Date | None, Field(description="First day covered (YYYY-MM-DD)")] = None,
        period_end: Annotated[Date | None, Field(description="Last day covered (YYYY-MM-DD)")] = None,
    ) -> str:
        body: dict[str, Any] = {"kind": kind, "title": title, "body_md": body_md, "data": data or {}}
        if period_start:
            body["period_start"] = period_start.isoformat()
        if period_end:
            body["period_end"] = period_end.isoformat()
        if self.run_id is not None:
            body["agent_run_id"] = self.run_id
        r = await self.api.post("/reports", body)
        return f"Published {r.get('kind')} report #{r.get('id')} '{clip(r.get('title'), 80)}'."


# ---------------------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------------------

ToolKind = Literal["read", "compute", "gated"]
ToolMethod = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: ToolKind
    description: str
    method: ToolMethod
    read_only: bool


def _spec(method: ToolMethod, kind: ToolKind, description: str, read_only: bool | None = None) -> ToolSpec:
    return ToolSpec(
        name=method.__name__,
        kind=kind,
        description=description,
        method=method,
        read_only=(kind == "read") if read_only is None else read_only,
    )


TOOLS: tuple[ToolSpec, ...] = (
    # read
    _spec(Toolkit.get_house_status, "read",
          "Live house status: per unit call, zone temp, setpoints, whose hold runs (ours, a person's, a utility event, "
          "vacation...), a resume back-off, today's runtime, duty, maxed minutes and what the policy wants and why; utility "
          "events (announced, running or skipped); every room's temp (or 'no sensor') and occupancy state; house empty "
          "or not; weather now; open alerts. Start most runs here."),
    _spec(Toolkit.query_runtime, "read",
          "Daily stage-1 runtime (minutes) per unit and for the house over N days (1-120) with the weather-normalized "
          "expected minutes (only where that unit/mode baseline passes its checks), outdoor temps and maxed-out "
          "minutes. Lists up to 20 days. Not a savings claim."),
    _spec(Toolkit.query_room, "read",
          "One room's history over N hours (1-168): temperature min/mean/max (or unknown when it has no sensor), "
          "occupancy share and states, the unit's setpoint range, and up to 12 time buckets."),
    _spec(Toolkit.get_weather, "read",
          "Observed and forecast outdoor weather: temperature range, cloud cover, peak sun, and highs/lows by local day. "
          "Cite 'Weather data by Open-Meteo.com' whenever you use it."),
    _spec(Toolkit.baseline_report, "read",
          "Weather baselines per unit and mode: balance point, minutes per degree-day, R², CV(RMSE), NMBE and PASS/FAIL. "
          "No savings claim is valid for a unit whose baseline fails."),
    _spec(Toolkit.savings_report, "read",
          "Whole-house savings vs the weather-expected runtime for a date range (default last 14 days): minutes, percent "
          "and the 90% interval, per-unit split, and whether a claim is allowed."),
    _spec(Toolkit.waterfall_report, "read",
          "Weekly attribution waterfall (last week -> weather -> strategy & other -> this week) in minutes, with the 90% "
          "interval on the strategy step."),
    _spec(Toolkit.coupling_report, "read",
          "Floor coupling: upstairs runtime minutes/hour per °F the main floor runs above upstairs, with 90% interval and "
          "the bed-wing placebo (should be ~0)."),
    _spec(Toolkit.comfort_report, "read",
          "Per-room share of occupied minutes inside the comfort band (target ≥ 97%) and worst excursion, last N days (1-60)."),
    _spec(Toolkit.drift_report, "read",
          "Baseline drift per unit and mode: recent residual mean (%), z-score and a drifting flag."),
    _spec(Toolkit.natural_experiment_report, "read",
          "History study of warm-main-floor afternoons: extra upstairs runtime per event with 90% interval, the placebo "
          "and bed-wing checks, and up to 10 events."),
    _spec(Toolkit.list_actions, "read",
          "Recent controller actions (up to 500): counts per unit and status over all of them, read-back failures, "
          "guardrail blocks and skips, then the anomalies (failed, read-back mismatch, skipped) and the newest few in "
          "full (unit, action, status, mode, channel, rule, read-back result, reason)."),
    _spec(Toolkit.explain_action, "read",
          "Full detail of one controller action: why, before/request/read-back values, errors."),
    _spec(Toolkit.get_plan, "read",
          "What the controller wants each unit to hold right now, the guardrail verdict and whether it would write."),
    _spec(Toolkit.list_reports, "read", "Recent reports (id, date, kind, author, title). Read one with get_report."),
    _spec(Toolkit.get_report, "read", "One report's markdown body (first 3000 characters)."),
    _spec(Toolkit.get_settings, "read",
          "Controller mode, the wait after a Resume and the hold reminder, active policy parameters, your sign-off ranges "
          "and owner-only switches, hard limits, comfort bands, utility-event settings (skip rules, pre-cooling), sleep "
          "windows, occupancy settings and time zone."),
    _spec(Toolkit.list_experiments, "read", "All experiments with status, arms, length and checkpoints."),
    _spec(Toolkit.experiment_detail, "read",
          "One experiment: arms, checkpoints with alpha spending, current effect with 90% interval, and the pre-planned "
          "decision rule's verdict. Judge only at checkpoints, never on a daily peek."),
    _spec(Toolkit.model_fits, "read", "Latest model fits (baseline, RC, room offsets, occupancy priors) with key metrics and notes."),
    # compute
    _spec(Toolkit.run_backtest, "compute",
          "Walk-forward backtest of partial PolicyParams vs the current policy over 7-120 days: runtime change with 90% "
          "interval, comfort violation minutes, and whether it beats the model's uncertainty. Read-only.", read_only=True),
    _spec(Toolkit.simulate_plan, "compute",
          "Simulate one day (default tomorrow, with the forecast) under the current policy or partial PolicyParams: total "
          "and per-unit runtime and temperature range. Read-only.", read_only=True),
    _spec(Toolkit.estimate_power, "compute",
          "How many test days an experiment needs to detect an effect of X% of whole-house runtime.", read_only=True),
    _spec(Toolkit.request_refit, "compute", "Queue a model refit on the worker; returns a job id."),
    _spec(Toolkit.job_status, "compute", "Status and result of a queued job.", read_only=True),
    # gated
    _spec(Toolkit.review_pending_changes, "gated",
          "Changes waiting for YOUR sign-off, with their gate results (backtest, shadow days) and whether every changed "
          "parameter sits inside your sign-off ranges. Read-only."),
    _spec(Toolkit.sign_off_change, "gated",
          "Approve or hold one model-proposed change that awaits your sign-off, with a reason citing the numbers. You "
          "cannot reject: when the evidence shows harm, hold it with the evidence in the reason; the owner decides "
          "rejections. The API refuses (403) anything outside your ranges; approval starts a limited trial window, it "
          "never writes a thermostat."),
    _spec(Toolkit.propose_policy_change, "gated",
          "Propose a policy parameter change (only the keys being changed). It must pass backtest and shadow days, and "
          "the owner decides. Back it with numbers and 90% intervals."),
    _spec(Toolkit.propose_experiment, "gated",
          "Propose a randomized switchback experiment (2-3 arms, fixed length, pre-planned checkpoints). The owner approves. "
          "Size it with estimate_power first."),
    _spec(Toolkit.publish_report, "gated",
          "Publish a markdown report (nightly, weekly, anomaly or note). Keep it short; every savings number needs its "
          "90% interval; cite Open-Meteo if weather is discussed."),
)
TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def tool_parameters(spec: ToolSpec) -> list[inspect.Parameter]:
    """The tool's parameters (its method signature without ``self``), annotations resolved."""
    sig = inspect.signature(spec.method, eval_str=True)
    return [p for name, p in sig.parameters.items() if name != "self"]


@cache
def args_model(name: str) -> type[BaseModel]:
    """A pydantic model of the tool's arguments (unknown keys rejected)."""
    spec = TOOLS_BY_NAME[name]
    fields: dict[str, Any] = {}
    for p in tool_parameters(spec):
        default = ... if p.default is inspect.Parameter.empty else p.default
        fields[p.name] = (p.annotation, default)
    model_name = "".join(part.title() for part in name.split("_")) + "Args"
    return create_model(model_name, __config__=ConfigDict(extra="forbid"), **fields)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = dict(defs[ref.split("/")[-1]])
                rest = {k: v for k, v in node.items() if k != "$ref"}
                return walk({**target, **rest})
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


@cache
def _input_schema_json(name: str) -> str:
    schema = args_model(name).model_json_schema()
    schema = _inline_refs(schema)
    schema.pop("title", None)
    schema.setdefault("properties", {})
    schema["type"] = "object"
    return json.dumps(schema)


def input_schema(name: str) -> dict[str, Any]:
    """Self-contained JSON schema (no $refs) for a tool's arguments."""
    return json.loads(_input_schema_json(name))


__all__ = [
    "OPEN_METEO_ATTRIBUTION",
    "POLICY_PARAM_TYPES",
    "TOOLS",
    "TOOLS_BY_NAME",
    "ArmIn",
    "ToolInputError",
    "ToolResult",
    "ToolSpec",
    "Toolkit",
    "args_model",
    "check_policy_params",
    "input_schema",
    "tool_parameters",
]
