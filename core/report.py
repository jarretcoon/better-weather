"""Builds the four answers: weather now, forecast, alerts, severe risk.

Sources are fetched in parallel. Every answer lists its sources and adds a note whenever
something fell back (stale copy, other source, missing data), so nothing fails silently.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from time import monotonic
from typing import Any, TypeVar

from . import astro, display, http, units
from .clock import now_utc
from .places import Place
from .sources import iem, intl_alerts, nws, openmeteo, radar, spc

T = TypeVar("T")

# (shown on a card under the alerts line when the NWS didn't answer and the mesonet's copy did: sources/iem.py)
COPY_NOTE = "⚠️ The NWS didn't answer: alerts are from Iowa State's copy of its warnings (no full texts)"

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
# One deadline for all of an answer's fetches (they run side by side): a source that misses it becomes a note. Was
# 45 s per source, waited for one after another: a hanging NWS held the now card up for 100 s.
REPORT_WAIT_S = 40.0
SEVERE_EVENTS = ("tornado", "severe thunderstorm", "flash flood", "extreme wind",
                 "special weather statement")
# The now card names where its reading came from unless it's a station right next door with an hour-fresh report.
NEXT_DOOR_KM = 5.0
FRESH_OBS_MIN = 60
ELEVATION_WAIT_S = 10.0  # how long the station search waits for the model's answer to learn the place's height
STALE_WARN_S = 30 * 60  # a saved copy older than this is pointed out on the card itself
FEED_WARN_MIN = 20  # likewise MeteoAlarm's list (copies of it answer for up to 30 min: sources/intl_alerts.py)


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="better-weather")
    return _pool


def _interactive(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    with http.interactive():  # someone is waiting: fewer, shorter tries (http.py)
        return fn(*args, **kwargs)


class _Jobs:
    """An answer's background fetches, all sharing one deadline (set when the answer starts)."""

    def __init__(self) -> None:
        self.deadline = monotonic() + REPORT_WAIT_S

    def start(self, label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> _Job:
        return _Job(label, self.deadline, fn, *args, **kwargs)


class _Job:
    """A background fetch whose failure becomes a note instead of an exception."""

    def __init__(self, label: str, deadline: float, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self.label = label
        self.deadline = deadline
        self.future: Future[Any] = _executor().submit(_interactive, fn, *args, **kwargs)

    def result(self, notes: list[str]) -> Any:
        try:
            return self.future.result(timeout=max(0.0, self.deadline - monotonic()))
        except http.FetchError as exc:
            notes.append(f"{self.label} unavailable ({exc})")
        except TimeoutError:
            notes.append(f"{self.label} timed out")
        except Exception as exc:  # defensive: a source bug must not sink the whole answer
            notes.append(f"{self.label} failed ({type(exc).__name__}: {exc})")
        return None


class _Fmt:
    def __init__(self, units_: str, clock: str, tz: tzinfo) -> None:
        self.units = units_
        self.clock = clock
        self.tz = tz
        self.now = now_utc().astimezone(tz)

    def moment(self, value: Any) -> datetime | None:
        """A time from a source (ISO text or datetime), in the place's zone when it came without one."""
        dt = value if isinstance(value, datetime) else units.parse_time(value)
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt

    def at(self, value: Any) -> str | None:
        dt = self.moment(value)
        if dt is None:
            return None
        return units.day_clock(dt.astimezone(self.tz), self.clock, self.now)

    def precip(self, mm: Any) -> str | None:
        v = units.num(mm)
        shown = None if v is None else round(v / 25.4, 2) if self.units == "us" else round(v, 1)
        if not shown or shown < 0:  # nothing measurable: leave it out instead of "0.00 in"
            return None
        return f"{shown:.2f} in" if self.units == "us" else f"{shown:.1f} mm"

    def snow(self, cm: Any) -> str | None:
        v = units.num(cm)
        shown = None if v is None else round(v / 2.54 if self.units == "us" else v, 1)
        if not shown or shown < 0:  # a trace: leave it out instead of "0.0 in"
            return None
        return f"{shown:.1f} in" if self.units == "us" else f"{shown:.1f} cm"


def _place_tz(place: Place, point: dict[str, Any] | None, om: http.Fetched | None) -> tzinfo:
    name = (point or {}).get("timeZone") or place.timezone
    offset = None
    if om is not None and isinstance(om.data, dict):
        name = name or om.data.get("timezone")
        offset = om.data.get("utc_offset_seconds")
    return units.zone(name, offset)


def _nws_point(place: Place, notes: list[str]) -> dict[str, Any] | None:
    return _nws_lookup(place, notes)[0]


def _nws_lookup(place: Place, notes: list[str]) -> tuple[dict[str, Any] | None, bool]:
    """(grid point or None, whether we know the answer). False = NWS was unreachable for a maybe-US spot."""
    if not place.maybe_us:
        return None, True
    try:
        with http.interactive():
            return nws.point(place.lat, place.lon), True
    except http.FetchError as exc:
        notes.append(f"NWS lookup failed ({exc}); using Open-Meteo instead")
        return None, False


def _ago(minutes: float) -> str:
    """'12 min ago' up to two hours, then '3 hr ago'."""
    m = max(1, round(minutes))
    return f"{m} min ago" if m < 120 else f"{round(m / 60)} hr ago"


def _observation(point: dict[str, Any], place: Place, om: Future[Any]) -> dict[str, Any] | None:
    """The now card's station report, from a station that stands for the place (nws.latest_observation). The model's
    answer, fetched alongside, gives the place's height; without it in ELEVATION_WAIT_S, distance alone decides."""
    try:
        elevation = units.num((om.result(timeout=ELEVATION_WAIT_S).data or {}).get("elevation"))
    except Exception:  # the model's own job reports its failure
        elevation = None
    return nws.latest_observation(point, lat=place.lat, lon=place.lon, elevation_m=elevation)


def _reading_from(obs: dict[str, Any]) -> str | None:
    """'KDSM 12 min ago' for the card when the station isn't right next door or its report is over an hour old."""
    km, age = obs.get("distance_km"), obs.get("age_min")
    far = km is not None and km > NEXT_DOOR_KM
    old = age is not None and age > FRESH_OBS_MIN
    if not (far or old):
        return None
    return f"{obs.get('station')} {_ago(age)}" if age is not None else str(obs.get("station"))


def _distance(km: Any, units_: str) -> str | None:
    v = units.num(km)
    if v is None:
        return None
    return f"{max(1, round(v * 0.621371))} mi" if units_ == "us" else f"{max(1, round(v))} km"


def _stale_note(label: str, fetched: http.Fetched | None, notes: list[str]) -> None:
    """Say when a saved copy stood in for a source that didn't answer; past STALE_WARN_S with ⚠️, so it's on the card."""
    if fetched is not None and fetched.stale:
        text = f"{label} could not be refreshed; showing a copy from {_ago(fetched.age_seconds / 60)}"
        notes.append(f"⚠️ {text}" if fetched.age_seconds >= STALE_WARN_S else text)


def _is_daytime(sun: tuple[str, str] | None, hour: int) -> bool:
    """Whole-hour check against that day's sunrise/sunset ("07:03", "16:54"); 7–18 when unknown."""
    if not sun:
        return 7 <= hour < 18
    try:
        up, down = (int(x[:2]) * 60 + int(x[3:5]) for x in sun)
    except ValueError:
        return 7 <= hour < 18
    return up <= hour * 60 + 30 < down


def _om_hours(om: http.Fetched | None, fmt: _Fmt, count: int) -> list[dict[str, Any]]:
    data = (om.data if om else None) or {}
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    daily = data.get("daily") or {}
    sun_by_day = {str(d)[:10]: (str(up)[11:16], str(down)[11:16])
                  for d, up, down in zip(daily.get("time") or [], daily.get("sunrise") or [],
                                         daily.get("sunset") or [], strict=False) if up and down}
    out = []
    for i, stamp in enumerate(times):
        dt = units.parse_time(stamp)
        if dt is None:
            continue
        dt = dt.replace(tzinfo=fmt.tz)
        if dt < fmt.now.replace(minute=0, second=0, microsecond=0):
            continue

        def pick(key: str, index: int = i) -> Any:
            values = hourly.get(key) or []
            return values[index] if index < len(values) else None

        out.append({
            "time": units.day_clock(dt, fmt.clock, fmt.now),
            "_day": _is_daytime(sun_by_day.get(str(stamp)[:10]), dt.hour),
            "temp": units.temp_value(pick("temperature_2m"), fmt.units),
            "feels_like": units.temp_value(pick("apparent_temperature"), fmt.units),
            "sky": openmeteo.describe(pick("weather_code")),
            "precip_chance_pct": pick("precipitation_probability"),
            "precip": fmt.precip(pick("precipitation")),
            "wind": units.wind(pick("wind_direction_10m"), pick("wind_speed_10m"),
                               pick("wind_gusts_10m"), fmt.units),
        })
        if len(out) >= count:
            break
    return out


def _nws_hours(fetched: http.Fetched | None, fmt: _Fmt, count: int) -> list[dict[str, Any]]:
    periods = (((fetched.data if fetched else None) or {}).get("properties") or {}).get("periods") or []
    out = []
    for p in periods:
        end = units.parse_time(p.get("endTime"))
        if end is not None and end <= fmt.now:
            continue
        start = units.parse_time(p.get("startTime"))
        out.append({
            "time": fmt.at(start),
            "_day": bool(p.get("isDaytime")),
            "temp": p.get("temperature"),
            "sky": p.get("shortForecast"),
            "precip_chance_pct": (p.get("probabilityOfPrecipitation") or {}).get("value") or 0,
            "wind": f"{p.get('windDirection') or ''} {p.get('windSpeed') or ''}".strip() or None,
        })
        if len(out) >= count:
            break
    return out


def _nws_periods(fetched: http.Fetched | None, fmt: _Fmt, count: int, detailed_for: int) -> list[dict[str, Any]]:
    """The forecast's periods that haven't ended yet (a saved copy can be hours old: no "This Afternoon" at 9 PM)."""
    periods = (((fetched.data if fetched else None) or {}).get("properties") or {}).get("periods") or []
    ahead = [p for p in periods if (end := units.parse_time(p.get("endTime"))) is None or end > fmt.now]
    out = []
    for i, p in enumerate(ahead[:count]):
        start = units.parse_time(p.get("startTime"))
        entry = {
            "name": p.get("name"),
            "_date": start.date().isoformat() if start else None,
            "_dow": start.strftime("%a") if start else None,
            "temp": p.get("temperature"),
            "temp_unit": p.get("temperatureUnit"),
            "high_or_low": "high" if p.get("isDaytime") else "low",
            "sky": p.get("shortForecast"),
            "precip_chance_pct": (p.get("probabilityOfPrecipitation") or {}).get("value") or 0,
            "wind": f"{p.get('windDirection') or ''} {p.get('windSpeed') or ''}".strip() or None,
        }
        if i < detailed_for and p.get("detailedForecast"):
            entry["detail"] = p.get("detailedForecast")
        out.append(entry)
    return out


def _om_days(om: http.Fetched | None, fmt: _Fmt, days: int, after: date | None = None) -> list[dict[str, Any]]:
    """Open-Meteo's days from the place's today for *days* days (only those after *after*, when given). A saved
    copy can be from yesterday: its first day is then dropped rather than shown as today."""
    daily = (((om.data if om else None) or {}).get("daily")) or {}
    times = daily.get("time") or []
    today = fmt.now.date()
    last = today + timedelta(days=days - 1)
    out = []
    for i, stamp in enumerate(times):
        day = units.parse_time(stamp)
        if day is None or not today <= day.date() <= last or (after is not None and day.date() <= after):
            continue

        def pick(key: str, index: int = i) -> Any:
            values = daily.get(key) or []
            return values[index] if index < len(values) else None

        label = day.strftime("%a %b ") + str(day.day)
        snow = units.num(pick("snowfall_sum"))
        entry = {
            "day": label,
            "high": units.temp_value(pick("temperature_2m_max"), fmt.units),
            "low": units.temp_value(pick("temperature_2m_min"), fmt.units),
            "sky": openmeteo.describe(pick("weather_code")),
            "precip_chance_pct": pick("precipitation_probability_max"),
            "precip": fmt.precip(pick("precipitation_sum")),
            "wind_max": units.speed(pick("wind_speed_10m_max"), fmt.units),
            "gusts_max": units.speed(pick("wind_gusts_10m_max"), fmt.units),
            "uv_index_max": round(uv, 1) if (uv := units.num(pick("uv_index_max"))) is not None else None,
        }
        if snow:
            entry["snow"] = fmt.snow(snow)
        out.append({k: v for k, v in entry.items() if v is not None})
    return out


def _mark_upcoming(item: dict[str, Any], fmt: _Fmt) -> None:
    """Flag an alert that hasn't started yet, while its "starts" is still a raw time: the cards then say
    "from tomorrow 3 AM to 8 AM" rather than "until tomorrow 8 AM", which reads as if it were on now."""
    start = fmt.moment(item.get("starts"))
    if start is not None and start > fmt.now:
        item["upcoming"] = True


def _compact_alerts(alerts: list[dict[str, Any]], fmt: _Fmt) -> list[dict[str, Any]]:
    """The now card's alerts (raw NWS times in, the place's own clock out)."""
    out = []
    for a in alerts:
        item = {**a}
        _mark_upcoming(item, fmt)
        out.append(_brief_alert(item | {"starts": fmt.at(a.get("starts")), "until": fmt.at(a.get("ends"))}))
    return out


def _brief_alert(a: dict[str, Any]) -> dict[str, Any]:
    """One alert as the now card lists it, its times already on the place's clock. "starts" only when it's ahead."""
    entry: dict[str, Any] = {"event": a.get("event"), "severity": a.get("severity")}
    if a.get("upcoming"):
        entry |= {"upcoming": True, "starts": a.get("starts")}
    return entry | {"until": a.get("until"), "headline": a.get("headline"),
                    "life_threatening": a.get("life_threatening") or None}


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _hpa_to_pa(value: Any) -> float | None:
    hpa = units.num(value)
    return hpa * 100 if hpa is not None else None


# --- weather now ------------------------------------------------------------------------------

def now(place: Place, units_: str, clock: str) -> dict[str, Any]:
    notes: list[str] = [place.note] if place.note else []
    sources: list[str] = []
    jobs = _Jobs()
    point, nws_known = _nws_lookup(place, notes)
    metric = units_ == "metric"

    om_job = jobs.start("Open-Meteo forecast", openmeteo.forecast, place.lat, place.lon, 3)
    intl_job = jobs.start("Official alerts", _intl_alerts, place, clock) if not point and nws_known else None
    obs_job = fc_job = al_job = None
    if point:
        obs_job = jobs.start("NWS observation", _observation, point, place, om_job.future)
        fc_job = jobs.start("NWS forecast", nws.forecast, point, metric=metric)
        al_job = jobs.start("NWS alerts", nws.alerts_at, place.lat, place.lon)

    om = om_job.result(notes)
    obs = obs_job.result(notes) if obs_job else None
    fc = fc_job.result(notes) if fc_job else None
    al = al_job.result(notes) if al_job else None
    fmt = _Fmt(units_, clock, _place_tz(place, point, om))

    # Current conditions: a real station report if it's recent, else the model's estimate.
    current: dict[str, Any] = {}
    reading_from: str | None = None  # the card's "· KDSM 12 min ago" / "· model estimate"
    om_current = ((om.data if om else None) or {}).get("current") or {}
    if obs and (obs.get("age_min") or 0) <= nws.MAX_OBS_AGE_MIN:
        feels_c = next((v for v in (obs.get("wind_chill_c"), obs.get("heat_index_c")) if v is not None), None)
        if feels_c is None:
            feels_c = units.heat_or_chill_c(obs.get("temp_c"), obs.get("rh"), obs.get("wind_kmh"))
        wind = units.wind(obs.get("wind_dir"), obs.get("wind_kmh"), obs.get("gust_kmh"), units_)
        if wind is None:  # some stations leave wind out of a report: the model's, rather than no wind at all
            wind = units.wind(om_current.get("wind_direction_10m"), om_current.get("wind_speed_10m"),
                              om_current.get("wind_gusts_10m"), units_)
        current = {
            "temp": units.temp(obs.get("temp_c"), units_),
            "feels_like": units.temp(feels_c, units_) if feels_c is not None else None,
            "sky": obs.get("text") or openmeteo.describe(om_current.get("weather_code")),
            "humidity_pct": round(obs["rh"]) if obs.get("rh") is not None else None,
            "dewpoint": units.temp(obs.get("dewpoint_c"), units_),
            "wind": wind,
            "pressure": units.pressure(obs.get("pressure_pa"), units_),
            "visibility": units.distance(obs.get("visibility_m"), units_),
            "observed": fmt.at(obs.get("observed_at")),
            "station": f"{obs.get('station')} ({obs.get('station_name')})",
            "station_distance": _distance(obs.get("distance_km"), units_),
            "source": "NWS station report",
        }
        reading_from = _reading_from(obs)
        sources.append(f"NWS observation {obs.get('station')}")
        if obs.get("stale_copy"):
            notes.append("Latest station report could not be refreshed; showing the last one received")
    elif om_current:
        reading_from = "model estimate"
        current = {
            "temp": units.temp(om_current.get("temperature_2m"), units_),
            "feels_like": units.temp(om_current.get("apparent_temperature"), units_),
            "sky": openmeteo.describe(om_current.get("weather_code")),
            "humidity_pct": om_current.get("relative_humidity_2m"),
            "dewpoint": units.temp(om_current.get("dew_point_2m"), units_),
            "wind": units.wind(om_current.get("wind_direction_10m"), om_current.get("wind_speed_10m"),
                               om_current.get("wind_gusts_10m"), units_),
            "pressure": units.pressure(_hpa_to_pa(om_current.get("pressure_msl")), units_),
            "observed": fmt.at(om_current.get("time")),
            "source": "Open-Meteo model estimate",
        }
        if point and obs:
            notes.append(f"Nearest station report is {obs.get('age_min')} min old; "
                         "showing the model estimate")
        elif point:
            notes.append("No recent station report nearby; showing the model estimate")
    elif obs:  # old report and no model data: better than nothing, clearly labelled
        current = {
            "temp": units.temp(obs.get("temp_c"), units_),
            "sky": obs.get("text"),
            "observed": fmt.at(obs.get("observed_at")),
            "station": obs.get("station"),
            "source": "NWS station report (old)",
        }
        reading_from = _reading_from(obs)
        notes.append(f"Only an old station report is available ({obs.get('age_min')} min old)")

    next_hours = _om_hours(om, fmt, 8)  # for the card's "next hours" line only

    periods = _nws_periods(fc, fmt, 3, detailed_for=2) if fc else []
    if periods:
        sources.append(f"NWS forecast ({(point or {}).get('gridId', 'NWS')})")
        _stale_note("NWS forecast", fc, notes)
    else:
        periods = _om_days(om, fmt, 2)

    if om is not None:
        sources.append("Open-Meteo")
        _stale_note("Open-Meteo", om, notes)

    alerts: list[dict[str, Any]] = []  # as the card lists them: times on the place's clock
    alerts_checked = False
    if al is not None:
        alerts = _compact_alerts(nws.normalize_alerts(al.data), fmt)
        alerts_checked = True
        sources.append(iem.SOURCE if iem.is_copy(al.data) else "NWS alerts")
        _stale_note("NWS alerts", al, notes)
        if iem.is_copy(al.data):
            notes.append(COPY_NOTE)
    elif intl_job is None:
        notes.append("⚠️ Alerts could not be checked right now — don't assume there are none")
    else:
        intl = intl_job.result(notes)
        if intl is None:
            notes.append("⚠️ Official alerts could not be checked — don't assume there are none")
        else:
            alerts = [_brief_alert(a | {"until": a.get("ends")}) for a in intl.get("alerts") or []]
            alerts_checked = bool(intl.get("_exact"))
            if intl.get("source"):
                sources.append(str(intl["source"]))
            notes.extend(intl.get("notes") or [])

    daily = ((om.data if om else None) or {}).get("daily") or {}
    sun = {}
    days_listed = [str(t)[:10] for t in daily.get("time") or []]
    if daily.get("sunrise") and (today := fmt.now.date().isoformat()) in days_listed:  # a saved copy starts earlier
        i = days_listed.index(today)
        rises, sets = daily.get("sunrise") or [], daily.get("sunset") or []
        sun = {"sunrise": fmt.at(rises[i] if i < len(rises) else None),
               "sunset": fmt.at(sets[i] if i < len(sets) else None)}
    sky = _sky(place, fmt, daily)

    result = {
        "place": place.public(),
        "as_of": units.clock(fmt.now, fmt.clock),
        "current": _clean(current),
        "forecast": periods,
        "alerts": alerts,
        "units": units_,
        "_next_hours": next_hours,
        "_reading_from": reading_from,
        "_sun": sun,
        "_alerts_checked": alerts_checked,
        "_sky": sky,
        "sources": sources,
        "notes": notes,
    }
    if sun.get("sunrise") and sun.get("sunset"):
        result["sun"] = dict(sun)  # for the agent too, not only the card
    if sky:
        m = sky["moon"]
        result["moon"] = {k: v for k, v in {"phase": m["phase"], "lit_pct": m["lit_pct"], "up_now": m["up"],
                                            "sets": m.get("sets"), "rises": m.get("rises"),
                                            "next": f"{m['next']['phase']} {m['next']['date']}"}.items()
                          if v is not None}
    is_day = om_current.get("is_day")
    if is_day is not None:
        result["_daylight"] = bool(is_day)
    elif sun:
        result["_daylight"] = None
    card = display.now_card(result)
    return display.card_first(display.strip_private(_clean(result)), card)


def _sky(place: Place, fmt: _Fmt, daily: dict[str, Any]) -> dict[str, Any] | None:
    """The now card's sun and moon: by day the sun's times and today's moon; by night the moon (its phase, when it
    sets or rises) and the next sunrise. All worked out locally (core/astro.py), so it never fails a request."""
    now = fmt.now
    t = now.timestamp()
    try:
        lat, lon = float(place.lat), float(place.lon)
    except (TypeError, ValueError):
        return None

    def at(ts: float) -> str:
        return units.clock(datetime.fromtimestamp(ts, fmt.tz), fmt.clock)

    def local(text: Any) -> datetime | None:
        dt = units.parse_time(text)
        return dt.replace(tzinfo=fmt.tz) if dt is not None and dt.tzinfo is None else dt

    days = [(d, local(up), local(down)) for d, up, down in
            zip(daily.get("time") or [], daily.get("sunrise") or [], daily.get("sunset") or [], strict=False)]
    today = now.date()
    day_start = datetime.combine(today, time(0), tzinfo=fmt.tz).timestamp()
    day_end = datetime.combine(today + timedelta(days=1), time(0), tzinfo=fmt.tz).timestamp()
    # Open-Meteo gives a day with no sunrise or sunset (far north or south) the same time for both
    with_sun = [(d, up, down) for d, up, down in days if up is not None and down is not None and up != down]
    todays = next(((up, down) for d, up, down in with_sun if d == today.isoformat()), None)
    sun_up = todays[0] <= now < todays[1] if todays else astro.sun_altitude(t, lat, lon) > 0

    _, lit = astro.moon_phase_at(t)
    name = astro.moon_name(t, day_start, day_end)
    moon: dict[str, Any] = {"phase": name, "emoji": astro.moon_emoji(name, lat), "lit_pct": round(lit * 100)}
    # what the moon does next: up now and when it sets, or when it rises (or neither, far north or south)
    p = astro.moon_pass(t, lat, lon)
    rise, down = p["rise"], p["set"]
    if p["up"]:
        moon["up"] = True
        moon["sets"] = at(float(down)) if isinstance(down, float) else None
    else:
        moon["up"] = False
        moon["rises"] = at(float(rise)) if isinstance(rise, float) else None
    full, new = astro.next_phase(t, 0.5), astro.next_phase(t, 0)
    when = datetime.fromtimestamp(min(full, new), fmt.tz)
    moon["next"] = {"phase": "Full moon" if full < new else "New moon", "emoji": "🌕" if full < new else "🌑",
                    "date": f"{when:%a}, {when:%b} {when.day}"}
    if sun_up:
        return {"mode": "sun", "sunrise": at(todays[0].timestamp()) if todays else None,
                "sunset": at(todays[1].timestamp()) if todays else None, "sun_all_day": todays is None,
                "moon": moon}
    next_sun = next((up for _d, up, _down in with_sun if up > now), None)
    sun = None
    if next_sun is not None:
        sun = at(next_sun.timestamp())
        if (next_sun.date() - today).days > 1:  # far north, after the polar night: say which day
            sun = f"{next_sun:%a} {sun}"
    return {"mode": "moon", "next_sunrise": sun, "moon": moon}


# --- forecast ---------------------------------------------------------------------------------

def forecast(place: Place, units_: str, clock: str, *, days: int = 7, hourly: bool = False,
             hours: int = 24) -> dict[str, Any]:
    notes: list[str] = [place.note] if place.note else []
    sources: list[str] = []
    days = max(1, min(int(days or 7), 16))
    hours = max(1, min(int(hours or 24), 72))
    jobs = _Jobs()
    point = _nws_point(place, notes)
    metric = units_ == "metric"

    om_job = jobs.start("Open-Meteo forecast", openmeteo.forecast, place.lat, place.lon,
                        max(days, 4 if hourly else days))
    nws_job = jobs.start("NWS forecast", nws.forecast, point, hourly=hourly, metric=metric) if point else None
    om = om_job.result(notes)
    nws_fc = nws_job.result(notes) if nws_job else None
    fmt = _Fmt(units_, clock, _place_tz(place, point, om))

    result: dict[str, Any] = {"place": place.public(), "as_of": units.clock(fmt.now, fmt.clock),
                              "units": units_}
    if hourly:
        rows = _nws_hours(nws_fc, fmt, hours) if nws_fc else []
        if rows:
            sources.append("NWS hourly forecast")
            _stale_note("NWS hourly forecast", nws_fc, notes)
        else:
            rows = _om_hours(om, fmt, hours)
            if rows:
                sources.append("Open-Meteo")
        result["hours"] = rows
    else:
        periods = _nws_periods(nws_fc, fmt, min(days * 2, 14), detailed_for=14) if nws_fc else []
        if periods:
            sources.append(f"NWS forecast ({(point or {}).get('gridId', 'NWS')})")
            _stale_note("NWS forecast", nws_fc, notes)
            result["periods"] = periods
            if days > 7:
                last = periods[-1].get("_date")  # Open-Meteo picks up the day after the NWS's last one
                extra = _om_days(om, fmt, days, after=date.fromisoformat(last) if last
                                 else fmt.now.date() + timedelta(days=6))
                if extra:
                    result["extended_days"] = extra
                    sources.append("Open-Meteo (days 8+)")
        else:
            result["days"] = _om_days(om, fmt, days)
            if result["days"]:
                sources.append("Open-Meteo")
    if om is not None:
        _stale_note("Open-Meteo", om, notes)
    if not (result.get("hours") or result.get("periods") or result.get("days")):
        notes.append("⚠️ No forecast source answered; try again in a minute")
    result["sources"] = sources
    result["notes"] = notes
    return display.card_first(display.strip_private(_clean(result)), display.forecast_card(result))


# --- alerts -----------------------------------------------------------------------------------

def alerts(place: Place | None, area: str | None, clock: str) -> dict[str, Any]:
    notes: list[str] = [place.note] if place and place.note else []
    unsure = False   # the copy answered for coordinates the NWS couldn't place: they may not be in the US at all
    if area:
        fetched = nws.alerts_in_area(area)
        label = f"{area.upper()} (statewide)"
        tz = _offset_of_first_alert(fetched.data)
    elif place is None:
        raise ValueError("alerts() needs a place or an area")
    else:
        lookup_notes: list[str] = []
        point, known = _nws_lookup(place, lookup_notes)
        try:
            found = nws.alerts_at(place.lat, place.lon) if point is not None or not known else None
        except nws.NotCovered:   # the NWS's alert area doesn't reach it: abroad after all
            found = None
        if found is None:
            intl = _intl_alerts(place, clock)
            return display.card_first(display.strip_private(intl), display.alerts_card(intl))
        fetched = found
        notes.extend(lookup_notes)
        label = place.name
        tz = units.zone((point or {}).get("timeZone") or place.timezone)
        unsure = not known and place.country is None and iem.is_copy(fetched.data)
    _stale_note("NWS alerts", fetched, notes)
    fmt = _Fmt("us", clock, tz)
    items = nws.normalize_alerts(fetched.data)
    for item in items:
        # statewide lists span time zones: show each alert in its own office's offset
        f = _Fmt("us", clock, _own_zone(item)) if area else fmt
        _mark_upcoming(item, f)
        item["starts"] = f.at(item.get("starts"))
        item["ends"] = f.at(item.get("ends"))  # None = no set end ("until further notice")
        item["next_update_by"] = f.at(item.pop("expires", None))
        item.pop("id", None)
        item.pop("references", None)
    if len(items) > 5:  # an outbreak can list dozens: keep each one short, the headline says the most
        short = len(items) > 12
        for item in items:
            for key, limit in (("description", 250 if short else 600), ("instruction", 200 if short else 300)):
                if item.get(key):
                    item[key] = _shorten(item[key], limit)
        notes.append(f"{len(items)} alerts: long texts shortened; ask for one place to get its full text")
    unsure = unsure and not items   # none in the copy says nothing about coordinates that may be abroad
    if unsure:
        notes.append("⚠️ Alerts couldn't be checked: the NWS didn't answer, and these coordinates may be outside "
                     "the US")
    if iem.is_copy(fetched.data):   # the card shows the ⚠️ line; the agent also gets what the copy lacks
        notes += [COPY_NOTE, iem.LIMITS]
    result = {
        "where": label,
        "place": place.public() if place else None,
        "count": len(items),
        "alerts": [_clean(a) for a in items],
        "checked": units.clock(fmt.now, fmt.clock),
        "source": iem.SOURCE if iem.is_copy(fetched.data) else "National Weather Service",
        "notes": notes,
        "_exact": not unsure,
    }
    if not items and not unsure:
        result["summary"] = f"No active NWS alerts for {label}."
    return display.card_first(display.strip_private(_clean(result)) | {"count": len(items), "alerts": result["alerts"]},
                              display.alerts_card(result))


def _intl_alerts(place: Place, clock: str) -> dict[str, Any]:
    fmt = _Fmt("us", clock, units.zone(place.timezone))
    country = (place.country or "").upper()
    if country == "CA":
        items = intl_alerts.canada(place.lat, place.lon)
        for a in items:
            _mark_upcoming(a, fmt)
            for key in ("starts", "ends", "next_update_by"):
                a[key] = fmt.at(a.get(key))
        out = {"where": place.name, "place": place.public(), "count": len(items), "alerts": [_clean(a) for a in items],
               "checked": units.clock(fmt.now, fmt.clock), "source": "Environment and Climate Change Canada",
               "_exact": True}
        if not items:
            out["summary"] = f"No active Environment Canada alerts for {place.name}."
        return out
    if country in intl_alerts.EU_SLUGS:
        town = place.name.split(",")[0].split("(")[0].strip()
        eu = intl_alerts.europe(country, place.lat, place.lon, (town, place.district, place.region))
        for group in ("exact_matches", "region_name_matches", "orange_and_red_in_country"):
            for a in eu[group]:
                _mark_upcoming(a, fmt)
                a["starts"], a["ends"] = fmt.at(a.get("starts")), fmt.at(a.get("ends"))
        found = eu["exact_matches"] + eu["region_name_matches"]
        big = eu["orange_and_red_in_country"]  # the first dozen: count them from the country's totals
        counts = eu["country_counts"]
        elsewhere = (counts.get("orange", 0) + counts.get("red", 0)
                     - sum(1 for a in found if a.get("level") in ("orange", "red")))
        notes = []
        if eu["age_min"] > FEED_WARN_MIN:  # the last copy answers while a new one downloads, or when it can't
            notes.append(f"⚠️ MeteoAlarm's list is from {_ago(eu['age_min'])}; newer warnings may be missing")
        if eu["exact"]:  # every active warning had a map area, so "none here" is a real answer
            if not found:
                notes.append(f"No active warning in {eu['country']} covers this exact spot.")
            if elsewhere:
                notes.append(f"{elsewhere} orange/red warning(s) elsewhere in {eu['country']}.")
        else:  # the alerts card lists the other orange and red areas under this
            here = town if any(ch.isalpha() for ch in town) else "this place"  # not "naming 52.520"
            notes.append(f"⚠️ {eu['country']} lists warnings by area name: only ones naming {here} or its area "
                         "are shown" + (f" ({elsewhere} orange/red elsewhere)" if elsewhere else ""))
        return {"where": place.name, "place": place.public(), "count": len(found),
                "alerts": [_clean(a) for a in found],
                "orange_and_red_in_country": [_clean(a) for a in big],
                "country_counts": eu["country_counts"], "checked": units.clock(fmt.now, fmt.clock),
                "source": eu["source"], "notes": notes, "_exact": bool(eu["exact"])}
    if country in nws.NWS_COUNTRIES:
        why = ("⚠️ This spot is outside the National Weather Service's forecast zones (offshore or a remote "
               "island), so official alerts weren't checked.")
    elif not country:
        why = ("⚠️ Official alerts weren't checked: these coordinates are outside the US and no country is "
               "known for them. Ask with a place name to check.")
    else:
        why = ("⚠️ No official alert feed for this country yet (US, Canada and 38 European countries so far). "
               "Check the national weather service there.")
    return {"where": place.name, "place": place.public(), "count": 0, "alerts": [], "notes": [why],
            "_exact": False}


def _shorten(text: Any, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + "…"


def _own_zone(item: dict[str, Any]) -> tzinfo:
    for key in ("starts", "ends", "expires"):
        dt = units.parse_time(item.get(key))
        if dt is not None and dt.tzinfo is not None:
            return dt.tzinfo
    return UTC


def _offset_of_first_alert(data: Any) -> tzinfo:
    """Statewide lists have no single place; show times in the issuing office's own offset."""
    for feature in (data or {}).get("features") or []:
        sent = units.parse_time((feature.get("properties") or {}).get("sent"))
        if sent is not None and sent.tzinfo is not None:
            return sent.tzinfo
    return UTC


# --- severe -----------------------------------------------------------------------------------

def severe(place: Place, units_: str, clock: str) -> dict[str, Any]:
    notes: list[str] = [place.note] if place.note else []
    jobs = _Jobs()
    point, known = _nws_lookup(place, notes)
    om_job = jobs.start("Open-Meteo forecast", openmeteo.forecast, place.lat, place.lon, 3)
    # SPC is its own service: with the NWS grid lookup down, still try it for a lower-48 spot
    in_spc_area = (bool(point) or not known) and radar.in_conus(place.lat, place.lon)
    spc_job = jobs.start("SPC outlook", spc.risk_at, place.lat, place.lon) if in_spc_area else None
    al_job = jobs.start("NWS alerts", nws.alerts_at, place.lat, place.lon) if point else None
    om = om_job.result(notes)
    risk = spc_job.result(notes) if spc_job else None
    al = al_job.result(notes) if al_job else None
    fmt = _Fmt(units_, clock, _place_tz(place, point, om))

    result: dict[str, Any] = {"place": place.public(), "as_of": units.clock(fmt.now, fmt.clock)}
    if risk:
        for day in risk.get("days", []):
            day["valid_from"] = fmt.at(day.get("valid_from"))
            day["valid_to"] = fmt.at(day.get("valid_to"))
            day.pop("issued", None)
            if day.pop("stale_copy", False):
                notes.append(f"Day {day['day']} outlook could not be refreshed; showing an older copy")
        result["spc_outlook"] = risk.get("days", [])
        notes.extend(risk.get("notes", []))
    elif not in_spc_area and known:
        notes.append("SPC severe outlooks cover the lower 48 US states only")

    if al is not None:
        storm = [a for a in nws.normalize_alerts(al.data)
                 if any(word in a["event"].lower() for word in SEVERE_EVENTS)]
        result["storm_alerts"] = [_clean({
            "event": a["event"], "until": fmt.at(a.get("ends")), "headline": a.get("headline"),
            "details": a.get("details"), "instruction": a.get("instruction"),
            "life_threatening": a.get("life_threatening") or None,
        }) for a in storm]
    elif point or not known:
        notes.append("⚠️ Watches and warnings could not be checked right now — don't assume there are none")
    else:
        notes.append("⚠️ Official warnings weren't checked here; ask for alerts to check Canada and Europe")

    hourly = ((om.data if om else None) or {}).get("hourly") or {}
    times = hourly.get("time") or []
    if times:
        window = []
        start = fmt.now.replace(minute=0, second=0, microsecond=0)
        end = fmt.now + timedelta(hours=48)
        for i, stamp in enumerate(times):
            dt = units.parse_time(stamp)
            if dt is None:
                continue
            dt = dt.replace(tzinfo=fmt.tz)
            if start <= dt <= end:
                window.append((i, dt))
        cape = hourly.get("cape") or []
        gust = hourly.get("wind_gusts_10m") or []
        code = hourly.get("weather_code") or []
        if window:
            best_cape = max(window, key=lambda w: units.num(cape[w[0]] if w[0] < len(cape) else 0) or 0)
            best_gust = max(window, key=lambda w: units.num(gust[w[0]] if w[0] < len(gust) else 0) or 0)
            thunder = [w for w in window if w[0] < len(code) and (units.num(code[w[0]]) or 0) >= 95]
            result["model_next_48h"] = _clean({
                "max_instability_cape_jkg": round(units.num(cape[best_cape[0]]) or 0),
                "max_instability_at": units.day_clock(best_cape[1], fmt.clock, fmt.now),
                "max_gust": units.speed(gust[best_gust[0]], units_),
                "max_gust_at": units.day_clock(best_gust[1], fmt.clock, fmt.now),
                "thunderstorm_hours": len(thunder),
                "first_thunder": units.day_clock(thunder[0][1], fmt.clock, fmt.now) if thunder else None,
                "source": "Open-Meteo model guidance",
            })
    result["notes"] = notes
    result["summary"] = severe_text(result, alerts_checked=al is not None)
    result["sources"] = [s for s in ("NOAA Storm Prediction Center" if risk else None,
                                      (iem.SOURCE if iem.is_copy(al.data) else "NWS alerts") if al is not None
                                      else None,
                                      "Open-Meteo" if om is not None else None) if s]
    return _clean(result)


def severe_text(r: dict[str, Any], alerts_checked: bool = True) -> str:
    lines = []
    for a in r.get("storm_alerts") or []:
        mark = "🚨" if a.get("life_threatening") else "⚠️"
        lines.append(f"{mark} {a['event']}" + (f" until {a['until']}" if a.get("until") else ""))
    for day in r.get("spc_outlook") or []:
        line = f"Day {day['day']}: {day['risk']}"
        hz = day.get("hazards") or {}
        odds = [f"{name} {v['chance_within_25_miles_pct']}%" for name, v in hz.items()
                if isinstance(v, dict) and v.get("chance_within_25_miles_pct")]
        if odds:
            line += " — " + ", ".join(odds) + " within 25 mi"
        lines.append(line)
    m = r.get("model_next_48h") or {}
    if m.get("thunderstorm_hours"):
        lines.append(f"Models show thunderstorms starting around {m.get('first_thunder')}")
    if lines:
        return "\n".join(lines)
    if alerts_checked:
        return "No severe weather signals found."
    return "Models show no thunderstorms in the next 48 hours (official warnings weren't checked here)."
