"""Forecast confidence (ensembles), the NWS forecasters' discussion, and "best time to …" windows."""

from __future__ import annotations

import re
from datetime import timedelta
from statistics import median
from typing import Any

from . import http, units
from .clock import now_utc
from .places import Place
from .sources import nws

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLES = (("gfs_seamless", "GFS ensemble"), ("ecmwf_ifs025", "ECMWF ensemble"))
WET_MM = 0.25          # "measurable" precipitation (0.01 in)
SOAKING_MM = 12.7      # 0.5 in
SNOW_CM = 0.5          # measurable snow
BIG_SNOW_CM = 5.08     # 2 in


# --- confidence ----------------------------------------------------------------------------------

def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _members(daily: dict[str, Any], var: str, index: int) -> list[float]:
    out = []
    for key, values in daily.items():
        if (key == var or key.startswith(var + "_member")) and isinstance(values, list) and index < len(values):
            v = units.num(values[index])
            if v is not None:
                out.append(v)
    return out


def _amount(mm: float, metric: bool) -> str:
    return f"{mm:.1f} mm" if metric else f"{mm / 25.4:.2f} in"


def confidence(place: Place, units_: str, days: int = 7) -> dict[str, Any]:
    days = max(1, min(int(days or 7), 10))
    notes: list[str] = []
    runs: dict[str, dict[str, Any]] = {}
    used: list[str] = []
    for model, label in ENSEMBLES:
        try:
            fetched = http.get_json(ENSEMBLE_URL, {
                "latitude": round(place.lat, 2), "longitude": round(place.lon, 2),
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum",
                "models": model, "forecast_days": days, "timezone": "auto"}, ttl=2 * 3600, max_stale=12 * 3600)
        except http.FetchError as exc:
            notes.append(f"{label} unavailable ({exc})")
            continue
        runs[model] = (fetched.data or {}).get("daily") or {}
        used.append(label)
    if not runs:
        raise http.FetchError(ENSEMBLE_URL, "; ".join(notes) or "no ensemble data")
    times = next(iter(runs.values())).get("time") or []
    metric = units_ == "metric"

    def t(c: float) -> int:
        return round(c) if metric else round(units.c_to_f(c))

    out_days = []
    for i, day in enumerate(times[:days]):
        highs = [v for d in runs.values() for v in _members(d, "temperature_2m_max", i)]
        lows = [v for d in runs.values() for v in _members(d, "temperature_2m_min", i)]
        rain = [v for d in runs.values() for v in _members(d, "precipitation_sum", i)]
        snow = [v for d in runs.values() for v in _members(d, "snowfall_sum", i)]
        if not highs:
            continue
        n = len(highs)
        spread_c = _pct(highs, 0.9) - _pct(highs, 0.1)
        label = "high" if spread_c <= 3.5 else "medium" if spread_c <= 7 else "low"
        dt = units.parse_time(day)
        entry: dict[str, Any] = {
            "day": f"{dt.strftime('%a %b')} {dt.day}" if dt else day,
            "runs": n,
            "high_likely_range": f"{t(_pct(highs, 0.1))}–{t(_pct(highs, 0.9))}°",
            "low_likely_range": f"{t(_pct(lows, 0.1))}–{t(_pct(lows, 0.9))}°" if lows else None,
            "temperature_confidence": label,
            "chance_any_precip_pct": round(100 * sum(v >= WET_MM for v in rain) / len(rain)) if rain else None,
            "chance_half_inch_plus_pct": round(100 * sum(v >= SOAKING_MM for v in rain) / len(rain)) if rain else None,
            "typical_amount": _amount(median(rain), metric) if rain else None,
        }
        if snow and max(snow) >= SNOW_CM:
            entry["chance_any_snow_pct"] = round(100 * sum(v >= SNOW_CM for v in snow) / len(snow))
            entry["chance_two_inches_plus_pct"] = round(100 * sum(v >= BIG_SNOW_CM for v in snow) / len(snow))
        out_days.append({k: v for k, v in entry.items() if v is not None})
    result: dict[str, Any] = {
        "place": place.public(), "units": units_, "days": out_days,
        "source": " + ".join(used) + " via Open-Meteo",
        "how_to_read": ("Each model run is one possible future; percentages are the share of runs showing it. "
                        "'Likely range' covers the middle 80% of runs."),
    }
    if notes:
        result["notes"] = notes
    return result


# --- forecasters' discussion ------------------------------------------------------------------------

_SECTION = re.compile(r"^\.([A-Z][A-Z0-9 /&,-]+?)\.\.\.", re.M)
KEEP = ("KEY MESSAGES", "SYNOPSIS", "SHORT TERM", "NEAR TERM", "DISCUSSION", "LONG TERM", "UPDATE")
SECTION_CHARS = 2500


def _sections(text: str) -> dict[str, str]:
    matches = list(_SECTION.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        name = m.group(1).split("/")[0].strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].replace("&&", "").strip()
        body = re.sub(r"[ \t]+\n", "\n", body)
        body = re.sub(r"(?<!\n)\n(?!\n)", " ", body)  # unwrap the 66-column lines
        body = re.sub(r" {2,}", " ", body)
        if any(name.startswith(k) for k in KEEP) and body:
            out[name.title()] = body[:SECTION_CHARS] + ("…" if len(body) > SECTION_CHARS else "")
    return out


def discussion(place: Place, clock: str) -> dict[str, Any]:
    if not place.maybe_us:
        return {"place": place.public(), "error": "Forecaster discussions are a US National Weather Service product."}
    point = nws.point(place.lat, place.lon)
    if not point or not point.get("gridId"):
        return {"place": place.public(), "error": "No NWS office covers this spot."}
    office = str(point["gridId"])
    listing = http.get_json(f"{nws.BASE}/products/types/AFD/locations/{office}", ttl=10 * 60, max_stale=6 * 3600)
    graph = (listing.data or {}).get("@graph") or []
    if not graph:
        raise http.FetchError(listing.url, f"no forecast discussion found for {office}")
    latest = graph[0]
    product = http.get_json(f"{nws.BASE}/products/{latest['id']}", ttl=24 * 3600, max_stale=24 * 3600)
    text = str((product.data or {}).get("productText") or "")
    issued = units.parse_time(latest.get("issuanceTime"))
    tz = units.zone(point.get("timeZone") or place.timezone)
    now = now_utc().astimezone(tz)
    sections = _sections(text)
    result = {
        "place": place.public(), "office": f"NWS {office}",
        "issued": units.day_clock(issued.astimezone(tz), clock, now) if issued else None,
        "issued_minutes_ago": units.minutes_ago(issued),
        "sections": sections or {"Text": text[:SECTION_CHARS * 2]},
        "how_to_use": ("Written by NWS meteorologists for professionals. Summarize in plain words, keep their "
                       "uncertainty, and don't add details they didn't say."),
    }
    return result


# --- best time to … ----------------------------------------------------------------------------------

# temp_ok = comfortable feels-like range (°C); max_wind km/h; max_pop %; daylight / night requirements.
ACTIVITIES: dict[str, dict[str, Any]] = {
    "walk": {"temp_ok": (7, 27), "max_wind": 25, "max_pop": 30, "light": "day"},
    "run": {"temp_ok": (4, 20), "max_wind": 30, "max_pop": 30, "light": "day", "max_dew": 17},
    "bike": {"temp_ok": (10, 28), "max_wind": 20, "max_pop": 20, "light": "day"},
    "golf": {"temp_ok": (10, 29), "max_wind": 22, "max_pop": 20, "light": "day", "min_hours": 3},
    "mow": {"temp_ok": (5, 31), "max_wind": 40, "max_pop": 20, "light": "day", "dry_before_h": 6},
    "grill": {"temp_ok": (10, 30), "max_wind": 25, "max_pop": 20, "light": "any", "prefer_hours": (16, 21)},
    "outdoor": {"temp_ok": (13, 28), "max_wind": 22, "max_pop": 20, "light": "any", "prefer_hours": (10, 21)},
    "car_wash": {"temp_ok": (3, 35), "max_wind": 40, "max_pop": 20, "light": "day", "dry_after_h": 36},
    "stargazing": {"temp_ok": (-10, 30), "max_wind": 30, "max_pop": 10, "light": "night", "max_cloud": 25},
}
ALIASES = {"walking": "walk", "jog": "run", "running": "run", "jogging": "run", "biking": "bike",
           "cycling": "bike", "ride": "bike", "mowing": "mow", "mow the lawn": "mow", "lawn": "mow",
           "bbq": "grill", "barbecue": "grill", "grilling": "grill", "patio": "outdoor", "picnic": "outdoor",
           "outside": "outdoor", "wash the car": "car_wash", "car wash": "car_wash", "carwash": "car_wash",
           "stars": "stargazing", "astronomy": "stargazing"}
HOURLY = ("temperature_2m,apparent_temperature,dew_point_2m,precipitation_probability,precipitation,"
          "wind_speed_10m,wind_gusts_10m,cloud_cover,uv_index,is_day,weather_code")


def activity_name(text: Any) -> str:
    raw = str(text or "outdoor").strip().lower().replace("-", " ")
    raw = ALIASES.get(raw, raw).replace(" ", "_")
    return raw if raw in ACTIVITIES else "outdoor"


def _score(hour: dict[str, Any], rule: dict[str, Any]) -> tuple[int, list[str]]:
    score = 100.0
    why: list[str] = []
    feels = units.num(hour.get("apparent_temperature"))
    lo, hi = rule["temp_ok"]
    if feels is not None:
        if feels < lo:
            score -= min(60, (lo - feels) * 6)
            why.append("cold")
        elif feels > hi:
            score -= min(60, (feels - hi) * 6)
            why.append("hot")
    pop = units.num(hour.get("precipitation_probability")) or 0
    if pop > rule["max_pop"]:
        score -= min(70, (pop - rule["max_pop"]) * 1.2)
        why.append(f"{round(pop)}% rain chance")
    gust = max(units.num(hour.get("wind_speed_10m")) or 0, (units.num(hour.get("wind_gusts_10m")) or 0) * 0.7)
    if gust > rule["max_wind"]:
        score -= min(40, (gust - rule["max_wind"]) * 2)
        why.append("windy")
    if rule.get("max_dew") and (units.num(hour.get("dew_point_2m")) or 0) > rule["max_dew"]:
        score -= 15
        why.append("muggy")
    if rule.get("max_cloud") is not None and (units.num(hour.get("cloud_cover")) or 0) > rule["max_cloud"]:
        score -= min(80, ((units.num(hour.get("cloud_cover")) or 0) - rule["max_cloud"]) * 1.5)
        why.append("cloudy")
    uv = units.num(hour.get("uv_index")) or 0
    if uv >= 8 and rule["light"] == "day":
        score -= 10
        why.append("strong sun")
    return max(0, round(score)), why


def best_time(place: Place, activity: Any, units_: str, clock: str, days: int = 3) -> dict[str, Any]:
    name = activity_name(activity)
    rule = ACTIVITIES[name]
    days = max(1, min(int(days or 3), 7))
    fetched = http.get_json(FORECAST_URL, {
        "latitude": round(place.lat, 2), "longitude": round(place.lon, 2), "hourly": HOURLY,
        "forecast_days": days + 2, "past_days": 1, "timezone": "auto", "wind_speed_unit": "kmh"},
        ttl=30 * 60, max_stale=6 * 3600)
    data = fetched.data or {}
    tz = units.zone(data.get("timezone"), data.get("utc_offset_seconds"))
    now = now_utc().astimezone(tz)
    hourly = data.get("hourly") or {}
    rows = []
    for i, stamp in enumerate(hourly.get("time") or []):
        dt = units.parse_time(stamp)
        if dt is None:
            continue
        row = {k: (v[i] if i < len(v) else None) for k, v in hourly.items() if isinstance(v, list)}
        row["dt"] = dt.replace(tzinfo=tz)
        rows.append(row)
    horizon = now + timedelta(days=days)
    scored = []
    for idx, row in enumerate(rows):
        if not (now - timedelta(minutes=30) <= row["dt"] <= horizon):
            continue
        is_day = bool(units.num(row.get("is_day")))
        if (rule["light"] == "day" and not is_day) or (rule["light"] == "night" and is_day):
            continue
        score, why = _score(row, rule)
        if rule.get("prefer_hours") and not (rule["prefer_hours"][0] <= row["dt"].hour < rule["prefer_hours"][1]):
            score -= 15
        if rule.get("dry_before_h"):
            before = [units.num(r.get("precipitation")) or 0 for r in rows[max(0, idx - rule["dry_before_h"]):idx]]
            if sum(before) >= 0.5:
                score -= 40
                why.append("wet from recent rain")
        if rule.get("dry_after_h"):
            after = rows[idx:idx + rule["dry_after_h"]]
            wet = sum(units.num(r.get("precipitation")) or 0 for r in after)
            if wet >= 0.5 or any((units.num(r.get("precipitation_probability")) or 0) >= 40 for r in after):
                score -= 50
                why.append(f"rain within {rule['dry_after_h']} h")
        scored.append((row, max(0, score), why))

    windows = _windows(scored, rule.get("min_hours", 1))
    metric = units_ == "metric"

    def describe(win: list[tuple[dict[str, Any], int, list[str]]]) -> dict[str, Any]:
        start, end = win[0][0]["dt"], win[-1][0]["dt"] + timedelta(hours=1)
        feels = [units.num(r.get("apparent_temperature")) for r, _, _ in win]
        feels_v = [f for f in feels if f is not None]
        pop = max((units.num(r.get("precipitation_probability")) or 0) for r, _, _ in win)
        wind = max((units.num(r.get("wind_speed_10m")) or 0) for r, _, _ in win)
        rng = (f"{round(min(feels_v))}–{round(max(feels_v))}°C" if metric else
               f"{round(units.c_to_f(min(feels_v)))}–{round(units.c_to_f(max(feels_v)))}°F") if feels_v else None
        issues = sorted({w for _, _, ws in win for w in ws})
        return {k: v for k, v in {
            "from": units.day_clock(start, clock, now), "to": units.clock(end, clock),
            "hours": len(win), "score": round(sum(s for _, s, _ in win) / len(win)),
            "feels_like": rng, "max_rain_chance_pct": round(pop),
            "max_wind": units.speed(wind, units_), "watch_out_for": issues or None}.items() if v is not None}

    result: dict[str, Any] = {"place": place.public(), "activity": name, "days": days,
                              "best_windows": [describe(w) for w in windows[:3]],
                              "source": "Open-Meteo hourly forecast"}
    if not result["best_windows"]:
        result["notes"] = [f"No good {name.replace('_', ' ')} window in the next {days} day(s)."]
    elif result["best_windows"][0]["score"] < 70:
        result["notes"] = ["Nothing great; these are the least bad times."]
    return result


def _windows(scored: list[tuple[dict[str, Any], int, list[str]]], min_hours: int) -> list[Any]:
    """Consecutive good hours grouped into windows, best first. Falls back to the best single hours."""
    good = 70
    windows: list[list[Any]] = []
    current: list[Any] = []
    for item in scored:
        if item[1] >= good and (not current or item[0]["dt"] - current[-1][0]["dt"] <= timedelta(hours=1)):
            current.append(item)
            continue
        if len(current) >= min_hours:
            windows.append(current)
        current = [item] if item[1] >= good else []
    if len(current) >= min_hours:
        windows.append(current)
    windows.sort(key=lambda w: (sum(s for _, s, _ in w) / len(w), len(w)), reverse=True)
    if windows:
        return windows
    best = sorted(scored, key=lambda x: x[1], reverse=True)[:3]
    return [[b] for b in sorted(best, key=lambda x: x[0]["dt"])]
