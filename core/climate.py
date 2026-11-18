"""Air quality (incl. smoke, UV, European pollen) and weather history with normals and records.

US history comes from official station records (NOAA ACIS: observed values, 1991-2020 normals,
calendar-day records). Days a station hasn't reported yet, and places outside the US, use the ERA5
reanalysis via Open-Meteo, which is modelled; answers mark those values as estimates."""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean
from typing import Any

from . import http, units
from .clock import now_utc
from .places import Place
from .sources import acis, nws

AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# --- air ---------------------------------------------------------------------------------------

US_AQI_BANDS = ((50, "Good"), (100, "Moderate"), (150, "Unhealthy for sensitive groups"),
                (200, "Unhealthy"), (300, "Very unhealthy"), (10_000, "Hazardous"))
EU_AQI_BANDS = ((20, "Good"), (40, "Fair"), (60, "Moderate"), (80, "Poor"), (100, "Very poor"),
                (10_000, "Extremely poor"))
POLLUTANTS = {"us_aqi_pm2_5": "fine particles (PM2.5)", "us_aqi_pm10": "coarse particles (PM10)",
              "us_aqi_ozone": "ozone", "us_aqi_nitrogen_dioxide": "nitrogen dioxide",
              "us_aqi_sulphur_dioxide": "sulphur dioxide", "us_aqi_carbon_monoxide": "carbon monoxide"}
POLLEN = ("alder_pollen", "birch_pollen", "grass_pollen", "mugwort_pollen", "olive_pollen", "ragweed_pollen")
UV_BANDS = ((2, "Low"), (5, "Moderate"), (7, "High"), (10, "Very high"), (100, "Extreme"))


def band(value: float | None, bands: tuple[tuple[float, str], ...]) -> str | None:
    if value is None:
        return None
    for top, label in bands:
        if value <= top:
            return label
    return bands[-1][1]


def uv_level(index: float | None) -> str | None:
    """WHO's band for a UV index, read as the whole number it's reported as: 2.2 is Low, 7.3 High (not Very high)."""
    return None if index is None else band(math.floor(index + 0.5), UV_BANDS)


def pollen_level(grains: float | None) -> str | None:
    if grains is None:
        return None
    if grains < 1:
        return "none"
    return "low" if grains < 20 else "moderate" if grains < 100 else "high" if grains < 500 else "very high"


def air(place: Place, clock: str) -> dict[str, Any]:
    europe = place.country is not None and place.country not in {"US", "CA", "MX"} and -30 <= place.lon <= 45 \
        and 34 <= place.lat <= 72
    current = ["us_aqi", "european_aqi", "pm2_5", "pm10", "ozone", "nitrogen_dioxide", "uv_index",
               *POLLUTANTS.keys()]
    if europe:
        current += list(POLLEN)
    params = {"latitude": round(place.lat, 2), "longitude": round(place.lon, 2),
              "current": ",".join(current), "hourly": "us_aqi,european_aqi,pm2_5,uv_index",
              "timezone": "auto", "forecast_days": 2}
    fetched = http.get_json(AIR_URL, params, ttl=30 * 60, max_stale=6 * 3600)
    data = fetched.data or {}
    cur = data.get("current") or {}
    tz = units.zone(data.get("timezone"), data.get("utc_offset_seconds"))
    now = now_utc().astimezone(tz)
    us = place.country in (None, "US", "PR", "GU", "VI", "AS", "MP")
    aqi = units.num(cur.get("us_aqi" if us else "european_aqi"))
    scale = "US AQI" if us else "European AQI"
    sub = {k: units.num(cur.get(k)) for k in POLLUTANTS}
    dominant = max((k for k in sub if sub[k] is not None), key=lambda k: sub[k] or 0, default=None)
    result: dict[str, Any] = {
        "place": place.public(),
        "air_quality": {
            "index": round(aqi) if aqi is not None else None,
            "scale": scale,
            "category": band(aqi, US_AQI_BANDS if us else EU_AQI_BANDS),
            "main_pollutant": POLLUTANTS.get(dominant) if dominant and (sub.get(dominant) or 0) > 50 else None,
            "pm2_5_ugm3": units.num(cur.get("pm2_5")),
            "ozone_ugm3": units.num(cur.get("ozone")),
        },
        "uv_now": {"index": units.num(cur.get("uv_index")), "level": uv_level(units.num(cur.get("uv_index")))},
        "source": "Open-Meteo air quality (CAMS model forecast)",
        "notes": [],
    }
    if fetched.stale:
        result["notes"].append(f"Couldn't refresh; data is {round(fetched.age_seconds / 60)} min old")
    hourly = data.get("hourly") or {}
    peak_aqi = _peak(hourly, "us_aqi" if us else "european_aqi", now, tz, clock)
    peak_uv = _peak(hourly, "uv_index", now, tz, clock)
    if peak_aqi:
        result["next_24h_worst_air"] = {**peak_aqi, "category": band(peak_aqi["value"],
                                                                      US_AQI_BANDS if us else EU_AQI_BANDS)}
    if peak_uv:
        result["next_24h_peak_uv"] = {**peak_uv, "level": uv_level(peak_uv["value"])}
    pm25 = units.num(cur.get("pm2_5"))
    if pm25 is not None and pm25 >= 35:
        result["notes"].append("Fine-particle levels this high usually mean smoke or haze; "
                               "sensitive people should limit time outside")
    if europe:
        result["pollen"] = {k.replace("_pollen", ""): pollen_level(units.num(cur.get(k))) for k in POLLEN}
    else:
        result["notes"].append("Pollen forecasts are only available for Europe so far")
    result["air_quality"] = {k: v for k, v in result["air_quality"].items() if v is not None}
    return result


def _peak(hourly: dict[str, Any], key: str, now: Any, tz: Any, clock: str) -> dict[str, Any] | None:
    times, values = hourly.get("time") or [], hourly.get(key) or []
    best: tuple[float, Any] | None = None
    for stamp, value in zip(times, values, strict=False):
        dt = units.parse_time(stamp)
        v = units.num(value)
        if dt is None or v is None:
            continue
        dt = dt.replace(tzinfo=tz)
        if now - timedelta(hours=1) <= dt <= now + timedelta(hours=24) and (best is None or v > best[0]):
            best = (v, dt)
    if best is None:
        return None
    return {"value": round(best[0], 1) if key == "uv_index" else round(best[0]),
            "at": units.day_clock(best[1], clock, now),
            "time": best[1].isoformat()}  # the same moment unformatted, for apps that write times in their own language


# --- history -----------------------------------------------------------------------------------

DAILY = "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum"
MAX_RANGE_DAYS = 31


def parse_day(text: Any, today: date) -> date:
    raw = str(text or "").strip().lower()
    if raw in {"", "today"}:
        return today
    if raw == "yesterday":
        return today - timedelta(days=1)
    if raw.endswith(" days ago") and raw.split()[0].isdigit():
        return today - timedelta(days=int(raw.split()[0]))
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Couldn't read the date '{text}'. Use YYYY-MM-DD, 'yesterday' or 'N days ago'.") \
            from exc


def _model_days(lat: float, lon: float, start: date, end: date, today: date) -> dict[str, dict[str, Any]]:
    """ERA5 rows by ISO date (°C, mm, cm). Recent days use the forecast API (the archive lags ~5 days)."""
    out: dict[str, dict[str, Any]] = {}
    recent_cut = today - timedelta(days=6)
    if start <= recent_cut:
        fetched = http.get_json(ARCHIVE_URL, {
            "latitude": round(lat, 2), "longitude": round(lon, 2), "start_date": start.isoformat(),
            "end_date": min(end, recent_cut).isoformat(), "daily": DAILY, "timezone": "auto"},
            ttl=30 * 24 * 3600, max_stale=365 * 24 * 3600)
        out.update(_daily_rows(fetched.data))
    if end > recent_cut:
        past = max(1, min(92, (today - max(start, recent_cut + timedelta(days=1))).days + 1))
        fetched = http.get_json(FORECAST_URL, {
            "latitude": round(lat, 2), "longitude": round(lon, 2), "past_days": past, "forecast_days": 1,
            "daily": DAILY, "timezone": "auto"}, ttl=60 * 60, max_stale=24 * 3600)
        out.update({k: v for k, v in _daily_rows(fetched.data).items()
                    if start.isoformat() <= k <= end.isoformat()})
    return out


def _daily_rows(data: Any) -> dict[str, dict[str, Any]]:
    daily = (data or {}).get("daily") or {}
    times = daily.get("time") or []
    rows: dict[str, dict[str, Any]] = {}
    for i, day in enumerate(times):
        rows[day] = {key: (daily.get(key) or [None] * len(times))[i] for key in DAILY.split(",")}
    return rows


def _f_to_c(f: float | None) -> float | None:
    return None if f is None else (f - 32) * 5 / 9


def history(place: Place, start_text: Any, end_text: Any, units_: str) -> dict[str, Any]:
    tz = units.zone(place.timezone)
    today = now_utc().astimezone(tz).date()
    start = parse_day(start_text, today)
    end = parse_day(end_text, today) if end_text else start
    if end < start:
        start, end = end, start
    if start < date(1850, 1, 1):
        raise ValueError("History goes back to the 1800s at best.")
    if end > today:
        raise ValueError("That date is in the future; use the forecast instead.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"Ask for {MAX_RANGE_DAYS} days or fewer at a time.")
    metric = units_ == "metric"
    notes: list[str] = []
    sources: list[str] = []

    # 1) Official station record (US).
    station: tuple[str, str] | None = None
    observed: dict[str, dict[str, float | None]] = {}
    if place.maybe_us:
        try:
            point = nws.point(place.lat, place.lon)
            if point:
                icaos = [s["id"] for s in nws.stations(point)]
                station = acis.station_for(icaos, today)
            if station:
                observed = acis.daily(station[0], start, end)
                sources.append(f"NOAA station records (ACIS), {station[1]}")
        except http.FetchError as exc:
            notes.append(f"Station records unavailable ({exc}); using model estimates")

    # 2) Model estimates for whatever the station doesn't have (and everywhere outside the US).
    all_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    missing = [d for d in all_days if (observed.get(d.isoformat()) or {}).get("high_f") is None]
    model: dict[str, dict[str, Any]] = {}
    if missing:
        try:
            model = _model_days(place.lat, place.lon, min(missing), max(missing), today)
            sources.append("ERA5 reanalysis via Open-Meteo (modelled)")
        except http.FetchError as exc:
            notes.append(f"Model history unavailable ({exc})")

    def temp(c: float | None) -> int | None:
        return units.temp_value(c, units_)

    def rain(mm: float | None) -> str | None:
        if mm is None:
            return None
        return f"{mm:.1f} mm" if metric else f"{mm / 25.4:.2f} in"

    def snow(cm: float | None) -> str | None:
        if not cm:
            return None
        return f"{cm:.1f} cm" if metric else f"{cm / 2.54:.1f} in"

    days_out: list[dict[str, Any]] = []
    for d in all_days:
        iso = d.isoformat()
        obs = observed.get(iso) or {}
        if obs.get("high_f") is not None:
            precip_in, snow_in = obs.get("precip_in"), obs.get("snow_in")
            entry = {"date": iso, "high": temp(_f_to_c(obs["high_f"])), "low": temp(_f_to_c(obs.get("low_f"))),
                     "precip": rain(precip_in * 25.4 if precip_in is not None else None),
                     "snow": snow(snow_in * 2.54 if snow_in else None)}
        elif iso in model:
            m = model[iso]
            entry = {"date": iso, "high": temp(units.num(m.get("temperature_2m_max"))),
                     "low": temp(units.num(m.get("temperature_2m_min"))),
                     "precip": rain(units.num(m.get("precipitation_sum"))),
                     "snow": snow(units.num(m.get("snowfall_sum"))), "estimated": True}
        else:
            continue
        days_out.append({k: v for k, v in entry.items() if v is not None})

    result: dict[str, Any] = {"place": place.public(), "from": start.isoformat(), "to": end.isoformat(),
                              "units": units_, "days": days_out, "sources": sources}
    if not days_out:
        notes.append("No data came back for those dates")

    if start == end:
        normal = observed.get(start.isoformat()) or {}
        comp: dict[str, Any] = {"normal_high": temp(_f_to_c(normal.get("normal_high_f"))),
                                "normal_low": temp(_f_to_c(normal.get("normal_low_f")))}
        if station:
            try:
                rec = acis.records(station[0], start, current_year=today.year)
            except http.FetchError as exc:
                rec = {}
                notes.append(f"Records unavailable ({exc})")
            if rec:
                comp["record_high"] = f"{temp(_f_to_c(rec['record_high_f']))}° in {rec['record_high_year']}"
                comp["record_low"] = f"{temp(_f_to_c(rec['record_low_f']))}° in {rec['record_low_year']}"
                comp["records_since"] = rec["since"]
                day_high = (observed.get(start.isoformat()) or {}).get("high_f")
                day_low = (observed.get(start.isoformat()) or {}).get("low_f")
                if day_high is not None and day_high >= rec["record_high_f"] and start.year < today.year:
                    comp["this_day_set_the_record_high"] = True
                if day_low is not None and day_low <= rec["record_low_f"] and start.year < today.year:
                    comp["this_day_set_the_record_low"] = True
        else:
            notes.append("Normals and records are only available for US places so far")
        if days_out and days_out[0].get("high") is not None and comp.get("normal_high") is not None:
            comp["high_vs_normal"] = days_out[0]["high"] - comp["normal_high"]
        comp = {k: v for k, v in comp.items() if v is not None}
        if comp:
            result["normals_and_records"] = comp
    elif days_out:
        highs = [d["high"] for d in days_out if "high" in d]
        lows = [d["low"] for d in days_out if "low" in d]
        precip_total = sum((units.num((observed.get(d["date"]) or {}).get("precip_in")) or 0) * 25.4
                           if (observed.get(d["date"]) or {}).get("high_f") is not None
                           else units.num((model.get(d["date"]) or {}).get("precipitation_sum")) or 0
                           for d in days_out)
        result["totals"] = {k: v for k, v in {
            "avg_high": round(mean(highs)) if highs else None, "avg_low": round(mean(lows)) if lows else None,
            "warmest": max(highs) if highs else None, "coldest": min(lows) if lows else None,
            "precip_total": rain(precip_total)}.items() if v is not None}
    if notes:
        result["notes"] = notes
    return result
