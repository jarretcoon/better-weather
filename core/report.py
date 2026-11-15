"""Builds the four answers: weather now, forecast, alerts, severe risk.

Sources are fetched in parallel. Every answer lists its sources and adds a note whenever
something fell back (stale copy, other source, missing data), so nothing fails silently.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, TypeVar

from . import display, http, units
from .clock import now_utc
from .places import Place
from .sources import intl_alerts, nws, openmeteo, radar, spc

T = TypeVar("T")

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
FETCH_WAIT_S = 45.0
SEVERE_EVENTS = ("tornado", "severe thunderstorm", "flash flood", "extreme wind",
                 "special weather statement")


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="better-weather")
    return _pool


class _Job:
    """A background fetch whose failure becomes a note instead of an exception."""

    def __init__(self, label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self.label = label
        self.future: Future[Any] = _executor().submit(fn, *args, **kwargs)

    def result(self, notes: list[str]) -> Any:
        try:
            return self.future.result(timeout=FETCH_WAIT_S)
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

    def at(self, value: Any) -> str | None:
        dt = value if isinstance(value, datetime) else units.parse_time(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return units.day_clock(dt.astimezone(self.tz), self.clock, self.now)

    def precip(self, mm: Any) -> str | None:
        v = units.num(mm)
        if v is None or v < 0.05:  # nothing measurable: leave it out instead of "0.00 in"
            return None
        return f"{v / 25.4:.2f} in" if self.units == "us" else f"{v:.1f} mm"

    def snow(self, cm: Any) -> str | None:
        v = units.num(cm)
        if v is None:
            return None
        return f"{v / 2.54:.1f} in" if self.units == "us" else f"{v:.1f} cm"


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
        return nws.point(place.lat, place.lon), True
    except http.FetchError as exc:
        notes.append(f"NWS lookup failed ({exc}); using Open-Meteo instead")
        return None, False


def _stale_note(label: str, fetched: http.Fetched | None, notes: list[str]) -> None:
    if fetched is not None and fetched.stale:
        notes.append(f"{label} could not be refreshed; showing a copy from "
                     f"{round(fetched.age_seconds / 60)} min ago")


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


def _nws_periods(fetched: http.Fetched | None, count: int, detailed_for: int) -> list[dict[str, Any]]:
    periods = (((fetched.data if fetched else None) or {}).get("properties") or {}).get("periods") or []
    out = []
    for i, p in enumerate(periods[:count]):
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


def _om_index_after(om: http.Fetched | None, last_day: Any) -> int:
    """Index of the first Open-Meteo day after the last NWS period (7 if unknown)."""
    times = [str(t)[:10] for t in ((((om.data if om else None) or {}).get("daily")) or {}).get("time") or []]
    return times.index(str(last_day)) + 1 if last_day and str(last_day) in times else 7


def _om_days(om: http.Fetched | None, fmt: _Fmt, start: int, count: int) -> list[dict[str, Any]]:
    daily = (((om.data if om else None) or {}).get("daily")) or {}
    times = daily.get("time") or []
    out = []
    for i in range(start, min(len(times), start + count)):

        def pick(key: str, index: int = i) -> Any:
            values = daily.get(key) or []
            return values[index] if index < len(values) else None

        day = units.parse_time(times[i])
        label = day.strftime("%a %b ") + str(day.day) if day else times[i]
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


def _compact_alerts(alerts: list[dict[str, Any]], fmt: _Fmt) -> list[dict[str, Any]]:
    return [{
        "event": a["event"],
        "severity": a.get("severity"),
        "until": fmt.at(a.get("ends")),
        "headline": a.get("headline"),
        "life_threatening": a.get("life_threatening") or None,
    } for a in alerts]


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _hpa_to_pa(value: Any) -> float | None:
    hpa = units.num(value)
    return hpa * 100 if hpa is not None else None


# --- weather now ------------------------------------------------------------------------------

def now(place: Place, units_: str, clock: str) -> dict[str, Any]:
    notes: list[str] = [place.note] if place.note else []
    sources: list[str] = []
    point, nws_known = _nws_lookup(place, notes)
    metric = units_ == "metric"

    om_job = _Job("Open-Meteo forecast", openmeteo.forecast, place.lat, place.lon, 3)
    intl_job = _Job("Official alerts", _intl_alerts, place, clock) if not point and nws_known else None
    obs_job = fc_job = al_job = None
    if point:
        obs_job = _Job("NWS observation", nws.latest_observation, point)
        fc_job = _Job("NWS forecast", nws.forecast, point, metric=metric)
        al_job = _Job("NWS alerts", nws.alerts_at, place.lat, place.lon)

    om = om_job.result(notes)
    obs = obs_job.result(notes) if obs_job else None
    fc = fc_job.result(notes) if fc_job else None
    al = al_job.result(notes) if al_job else None
    fmt = _Fmt(units_, clock, _place_tz(place, point, om))

    # Current conditions: a real station report if it's recent, else the model's estimate.
    current: dict[str, Any] = {}
    om_current = ((om.data if om else None) or {}).get("current") or {}
    if obs and (obs.get("age_min") or 0) <= nws.MAX_OBS_AGE_MIN:
        feels_c = obs.get("wind_chill_c") or obs.get("heat_index_c") or units.heat_or_chill_c(
            obs.get("temp_c"), obs.get("rh"), obs.get("wind_kmh"))
        current = {
            "temp": units.temp(obs.get("temp_c"), units_),
            "feels_like": units.temp(feels_c, units_) if feels_c is not None else None,
            "sky": obs.get("text") or openmeteo.describe(om_current.get("weather_code")),
            "humidity_pct": round(obs["rh"]) if obs.get("rh") is not None else None,
            "dewpoint": units.temp(obs.get("dewpoint_c"), units_),
            "wind": units.wind(obs.get("wind_dir"), obs.get("wind_kmh"), obs.get("gust_kmh"), units_),
            "pressure": units.pressure(obs.get("pressure_pa"), units_),
            "visibility": units.distance(obs.get("visibility_m"), units_),
            "observed": fmt.at(obs.get("observed_at")),
            "station": f"{obs.get('station')} ({obs.get('station_name')})",
            "source": "NWS station report",
        }
        sources.append(f"NWS observation {obs.get('station')}")
        if obs.get("stale_copy"):
            notes.append("Latest station report could not be refreshed; showing the last one received")
    elif om_current:
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
        notes.append(f"Only an old station report is available ({obs.get('age_min')} min old)")

    next_hours = _om_hours(om, fmt, 8)  # for the card's "next hours" line only

    periods = _nws_periods(fc, 3, detailed_for=2) if fc else []
    if periods:
        sources.append(f"NWS forecast ({(point or {}).get('gridId', 'NWS')})")
        _stale_note("NWS forecast", fc, notes)
    else:
        periods = _om_days(om, fmt, 0, 2)

    if om is not None:
        sources.append("Open-Meteo")
        _stale_note("Open-Meteo", om, notes)

    alerts: list[dict[str, Any]] = []
    alerts_checked = False
    if al is not None:
        alerts = nws.normalize_alerts(al.data)
        alerts_checked = True
        sources.append("NWS alerts")
        _stale_note("NWS alerts", al, notes)
    elif intl_job is None:
        notes.append("⚠️ Alerts could not be checked right now — don't assume there are none")
    else:
        intl = intl_job.result(notes)
        if intl is None:
            notes.append("⚠️ Official alerts could not be checked — don't assume there are none")
        else:
            alerts = [{"event": a.get("event"), "severity": a.get("severity"), "ends": None,
                       "headline": a.get("headline"), "life_threatening": a.get("life_threatening")}
                      for a in intl.get("alerts") or []]
            alerts_checked = bool(intl.get("_exact"))
            if intl.get("source"):
                sources.append(str(intl["source"]))
            notes.extend(intl.get("notes") or [])

    daily = ((om.data if om else None) or {}).get("daily") or {}
    sun = {}
    if daily.get("sunrise"):
        sun = {"sunrise": fmt.at(daily["sunrise"][0]), "sunset": fmt.at((daily.get("sunset") or [None])[0])}

    result = {
        "place": place.public(),
        "as_of": units.clock(fmt.now, fmt.clock),
        "current": _clean(current),
        "forecast": periods,
        "alerts": _compact_alerts(alerts, fmt),
        "units": units_,
        "_next_hours": next_hours,
        "_sun": sun,
        "_alerts_checked": alerts_checked,
        "sources": sources,
        "notes": notes,
    }
    is_day = om_current.get("is_day")
    if is_day is not None:
        result["_daylight"] = bool(is_day)
    elif sun:
        result["_daylight"] = None
    card = display.now_card(result)
    return display.card_first(display.strip_private(_clean(result)), card)


# --- forecast ---------------------------------------------------------------------------------

def forecast(place: Place, units_: str, clock: str, *, days: int = 7, hourly: bool = False,
             hours: int = 24) -> dict[str, Any]:
    notes: list[str] = [place.note] if place.note else []
    sources: list[str] = []
    days = max(1, min(int(days or 7), 16))
    hours = max(1, min(int(hours or 24), 72))
    point = _nws_point(place, notes)
    metric = units_ == "metric"

    om_job = _Job("Open-Meteo forecast", openmeteo.forecast, place.lat, place.lon,
                  max(days, 4 if hourly else days))
    nws_job = _Job("NWS forecast", nws.forecast, point, hourly=hourly, metric=metric) if point else None
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
        periods = _nws_periods(nws_fc, min(days * 2, 14), detailed_for=14) if nws_fc else []
        if periods:
            sources.append(f"NWS forecast ({(point or {}).get('gridId', 'NWS')})")
            _stale_note("NWS forecast", nws_fc, notes)
            result["periods"] = periods
            if days > 7:
                first = _om_index_after(om, periods[-1].get("_date"))
                extra = _om_days(om, fmt, first, days - first)
                if extra:
                    result["extended_days"] = extra
                    sources.append("Open-Meteo (days 8+)")
        else:
            result["days"] = _om_days(om, fmt, 0, days)
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
    if area:
        fetched = nws.alerts_in_area(area)
        label = f"{area.upper()} (statewide)"
        tz = _offset_of_first_alert(fetched.data)
    elif place is None:
        raise ValueError("alerts() needs a place or an area")
    else:
        lookup_notes: list[str] = []
        point, known = _nws_lookup(place, lookup_notes)
        if point is None and known:
            intl = _intl_alerts(place, clock)
            return display.card_first(display.strip_private(intl), display.alerts_card(intl))
        notes.extend(lookup_notes)
        fetched = nws.alerts_at(place.lat, place.lon)
        label = place.name
        tz = units.zone((point or {}).get("timeZone") or place.timezone)
    _stale_note("NWS alerts", fetched, notes)
    fmt = _Fmt("us", clock, tz)
    items = nws.normalize_alerts(fetched.data)
    for item in items:
        # statewide lists span time zones: show each alert in its own office's offset
        f = _Fmt("us", clock, _own_zone(item)) if area else fmt
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
    result = {
        "where": label,
        "place": place.public() if place else None,
        "count": len(items),
        "alerts": [_clean(a) for a in items],
        "checked": units.clock(fmt.now, fmt.clock),
        "source": "National Weather Service",
        "notes": notes,
    }
    if not items:
        result["summary"] = f"No active NWS alerts for {label}."
    return display.card_first(_clean(result) | {"count": len(items), "alerts": result["alerts"]},
                              display.alerts_card(result))


def _intl_alerts(place: Place, clock: str) -> dict[str, Any]:
    fmt = _Fmt("us", clock, units.zone(place.timezone))
    country = (place.country or "").upper()
    if country == "CA":
        items = intl_alerts.canada(place.lat, place.lon)
        for a in items:
            for key in ("starts", "ends", "next_update_by"):
                a[key] = fmt.at(a.get(key))
        out = {"where": place.name, "place": place.public(), "count": len(items), "alerts": [_clean(a) for a in items],
               "checked": units.clock(fmt.now, fmt.clock), "source": "Environment and Climate Change Canada",
               "_exact": True}
        if not items:
            out["summary"] = f"No active Environment Canada alerts for {place.name}."
        return out
    if country in intl_alerts.EU_SLUGS:
        town = place.name.split(",")[0].split("(")[0]
        eu = intl_alerts.europe(country, place.lat, place.lon, (town, place.district, place.region))
        for group in ("exact_matches", "region_name_matches", "orange_and_red_in_country"):
            for a in eu[group]:
                a["starts"], a["ends"] = fmt.at(a.get("starts")), fmt.at(a.get("ends"))
        found = eu["exact_matches"] + eu["region_name_matches"]
        big = eu["orange_and_red_in_country"]
        notes = []
        if eu["exact"]:  # every active warning had a map area, so "none here" is a real answer
            if not found:
                notes.append(f"No active warning in {eu['country']} covers this exact spot.")
            if big:
                notes.append(f"{len(big)} orange/red warning(s) elsewhere in {eu['country']}.")
        else:
            notes.append(f"⚠️ MeteoAlarm lists {eu['country']}'s warnings by area name, not map, so only "
                         "warnings naming this town or area are shown"
                         + (f"; {len(big)} more orange/red elsewhere in the country (weather_alerts lists "
                            "the areas)" if big else ""))
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
    point, known = _nws_lookup(place, notes)
    om_job = _Job("Open-Meteo forecast", openmeteo.forecast, place.lat, place.lon, 3)
    # SPC is its own service: with the NWS grid lookup down, still try it for a lower-48 spot
    in_spc_area = (bool(point) or not known) and radar.in_conus(place.lat, place.lon)
    spc_job = _Job("SPC outlook", spc.risk_at, place.lat, place.lon) if in_spc_area else None
    al_job = _Job("NWS alerts", nws.alerts_at, place.lat, place.lon) if point else None
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
        notes.append("⚠️ Official warnings weren't checked here; weather_alerts checks Canada and Europe")

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
                                      "NWS alerts" if al is not None else None,
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
