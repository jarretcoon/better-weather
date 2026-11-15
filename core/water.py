"""Water: river levels and flood stages (NOAA National Water Prediction Service), tides (NOAA
CO-OPS) and waves (Open-Meteo marine). Each part only appears where it applies."""

from __future__ import annotations

import math
from datetime import UTC, timedelta
from typing import Any

from . import http, units
from .clock import now_utc
from .places import Place

NWPS = "https://api.water.noaa.gov/nwps/v1/gauges"
TIDE_STATIONS = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
TIDES = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
MARINE = "https://marine-api.open-meteo.com/v1/marine"
RIVER_RADIUS_DEG = 0.35
TIDE_MAX_KM = 60
FLOOD_ORDER = {"major": 4, "moderate": 3, "minor": 2, "action": 1, "no_flooding": 0}
FLOOD_WORDS = {"major": "MAJOR FLOODING", "moderate": "moderate flooding", "minor": "minor flooding",
               "action": "near flood stage", "no_flooding": "normal"}


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def rivers(place: Place) -> dict[str, Any]:
    r = RIVER_RADIUS_DEG
    fetched = http.get_json(NWPS, {"bbox.xmin": round(place.lon - r, 3), "bbox.ymin": round(place.lat - r, 3),
                                   "bbox.xmax": round(place.lon + r, 3), "bbox.ymax": round(place.lat + r, 3),
                                   "srid": "EPSG_4326"}, ttl=15 * 60, max_stale=6 * 3600)
    gauges = []
    for g in (fetched.data or {}).get("gauges") or []:
        obs = (g.get("status") or {}).get("observed") or {}
        fc = (g.get("status") or {}).get("forecast") or {}
        cat = str(obs.get("floodCategory") or "")
        if cat not in FLOOD_ORDER:  # out of service, not defined (no flood stages), or unknown
            continue
        stage = units.num(obs.get("primary"))
        if stage is None or stage <= -999:
            continue
        fcat = str(fc.get("floodCategory") or "")
        gauges.append({
            "lid": g.get("lid"), "name": g.get("name"),
            "distance_km": round(_km(place.lat, place.lon, g["latitude"], g["longitude"]), 1),
            "level_now": f"{stage:.1f} {obs.get('primaryUnit') or 'ft'}",
            "status": FLOOD_WORDS[cat], "_rank": FLOOD_ORDER[cat],
            "forecast": (f"{units.num(fc.get('primary')):.1f} {fc.get('primaryUnit') or 'ft'}, {FLOOD_WORDS[fcat]}"
                         if fcat in FLOOD_ORDER and (units.num(fc.get("primary")) or -999) > -999 else None),
            "_frank": FLOOD_ORDER.get(fcat, 0),
        })
    gauges.sort(key=lambda g: (-max(g["_rank"], g["_frank"]), g["distance_km"]))
    top = gauges[:5]
    for g in top[:3]:
        try:
            detail = http.get_json(f"{NWPS}/{g['lid']}", ttl=24 * 3600, max_stale=7 * 24 * 3600).data or {}
            cats = (detail.get("flood") or {}).get("categories") or {}
            stages = {k: cats[k]["stage"] for k in ("action", "minor", "moderate", "major")
                      if isinstance(cats.get(k), dict) and (units.num(cats[k].get("stage")) or -1) > 0}
            if stages:
                g["flood_stages_ft"] = stages
        except http.FetchError:
            pass
    worst = max((max(g["_rank"], g["_frank"]) for g in gauges), default=0)
    for g in top:
        g.pop("_rank", None)
        g.pop("_frank", None)
        if g.get("forecast") is None:
            g.pop("forecast", None)
    return {"gauges": top, "flooding_nearby": worst >= 2, "near_flood_stage": worst == 1}


def tides(place: Place, clock: str) -> dict[str, Any] | None:
    stations = http.get_json(TIDE_STATIONS, {"type": "tidepredictions"}, ttl=30 * 24 * 3600,
                             max_stale=180 * 24 * 3600).data or {}
    best: tuple[float, dict[str, Any]] | None = None
    for s in stations.get("stations") or []:
        lat, lon = units.num(s.get("lat")), units.num(s.get("lng"))
        if lat is None or lon is None or abs(lat - place.lat) > 1 or abs(lon - place.lon) > 1.5:
            continue
        d = _km(place.lat, place.lon, lat, lon)
        if d <= TIDE_MAX_KM and (best is None or d < best[0]):
            best = (d, s)
    if best is None:
        return None
    station = best[1]
    data = http.get_json(TIDES, {"product": "predictions", "station": station["id"], "date": "today", "range": 48,
                                 "datum": "MLLW", "units": "english", "time_zone": "gmt", "interval": "hilo",
                                 "format": "json", "application": "better-weather"},
                         ttl=6 * 3600, max_stale=24 * 3600).data or {}
    tz = units.zone(place.timezone)
    now = now_utc().astimezone(tz)
    out = []
    for p in data.get("predictions") or []:
        dt = units.parse_time(str(p.get("t", "")).replace(" ", "T"))
        if dt is None:
            continue
        dt = dt.replace(tzinfo=UTC).astimezone(tz)  # asked in GMT: the station's own zone isn't known here
        if dt < now - timedelta(hours=1):
            continue
        out.append({"time": units.day_clock(dt, clock, now), "type": "high" if p.get("type") == "H" else "low",
                    "height_ft": units.num(p.get("v"))})
    return {"station": f"{station.get('name')} ({round(best[0])} km away)", "next": out[:5]}


def waves(place: Place, units_: str) -> dict[str, Any] | None:
    data = http.get_json(MARINE, {"latitude": round(place.lat, 2), "longitude": round(place.lon, 2),
                                  "current": "wave_height,wave_direction,wave_period",
                                  "hourly": "wave_height", "forecast_days": 2, "timezone": "auto"},
                         ttl=60 * 60, max_stale=6 * 3600).data or {}
    cur = data.get("current") or {}
    height = units.num(cur.get("wave_height"))
    if height is None:
        return None  # inland: the marine model has no water here
    peak = max((units.num(v) or 0 for v in (data.get("hourly") or {}).get("wave_height") or []), default=height)

    def show(m: float) -> str:
        return f"{m:.1f} m" if units_ == "metric" else f"{m * 3.281:.1f} ft"

    return {"height_now": show(height), "from": units.compass(cur.get("wave_direction")),
            "period_s": units.num(cur.get("wave_period")), "highest_next_48h": show(peak)}


def water(place: Place, units_: str, clock: str) -> dict[str, Any]:
    result: dict[str, Any] = {"place": place.public(), "sources": []}
    notes: list[str] = []
    if place.maybe_us:
        try:
            result["rivers"] = rivers(place)
            result["sources"].append("NOAA National Water Prediction Service")
        except http.FetchError as exc:
            notes.append(f"River levels unavailable ({exc})")
        try:
            tide = tides(place, clock)
            if tide:
                result["tides"] = tide
                result["sources"].append("NOAA Tides and Currents")
        except http.FetchError as exc:
            notes.append(f"Tides unavailable ({exc})")
    else:
        notes.append("River levels and tides are US-only so far")
    try:
        wave = waves(place, units_)
        if wave:
            result["waves"] = wave
            result["sources"].append("Open-Meteo marine")
    except http.FetchError as exc:
        notes.append(f"Waves unavailable ({exc})")
    if notes:
        result["notes"] = notes
    return result
