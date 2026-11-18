"""US National Weather Service API (api.weather.gov): observations, forecasts, alerts."""

from __future__ import annotations

import threading
import time
from typing import Any

from .. import http, units
from ..clock import now_utc
from . import iem

BASE = "https://api.weather.gov"

# Places where the NWS API answers (US states and territories).
NWS_COUNTRIES = {"US", "PR", "GU", "VI", "AS", "MP", "UM"}

# Observations older than this are skipped in favour of the next-nearest station.
MAX_OBS_AGE_MIN = 90


def _q(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


_failed_points: dict[str, tuple[float, http.FetchError]] = {}
_failed_lock = threading.Lock()
FAILED_POINT_S = 120  # one answer may look a point up several times; don't wait out the retries again


def forget_failures() -> None:
    with _failed_lock:
        _failed_points.clear()


def point(lat: float, lon: float) -> dict[str, Any] | None:
    """Grid metadata for a point, or None if the point is outside NWS coverage (cached either way)."""
    url = f"{BASE}/points/{_q(round(lat, 3))},{_q(round(lon, 3))}"
    with _failed_lock:
        failed = _failed_points.get(url)
    if failed and time.monotonic() - failed[0] < FAILED_POINT_S:
        raise failed[1]
    try:
        fetched = http.get_json(url, ttl=http.TTL_POINT, max_stale=90 * 24 * 3600, allow_404=True)
    except http.FetchError as exc:
        with _failed_lock:
            _failed_points[url] = (time.monotonic(), exc)
        raise
    if not fetched.found:
        return None
    props = (fetched.data or {}).get("properties")
    return props if isinstance(props, dict) else None


def stations(point_props: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    url = point_props.get("observationStations")
    if not url:
        return []
    fetched = http.get_json(url, {"limit": limit}, ttl=http.TTL_STATIONS, max_stale=60 * 24 * 3600)
    out = []
    for feature in (fetched.data or {}).get("features", [])[:limit]:
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]
        out.append({
            "id": props.get("stationIdentifier"),
            "name": props.get("name"),
            "lon": coords[0],
            "lat": coords[1],
        })
    return [s for s in out if s["id"]]


def station_info(station_id: str) -> dict[str, Any] | None:
    fetched = http.get_json(f"{BASE}/stations/{station_id.upper()}", ttl=http.TTL_STATIONS,
                            max_stale=90 * 24 * 3600, allow_404=True)
    if not fetched.found:
        return None
    props = (fetched.data or {}).get("properties") or {}
    coords = ((fetched.data or {}).get("geometry") or {}).get("coordinates") or [None, None]
    if coords[0] is None:
        return None
    return {"id": props.get("stationIdentifier"), "name": props.get("name"), "lon": coords[0],
            "lat": coords[1], "timezone": props.get("timeZone")}


def _value(props: dict[str, Any], key: str) -> float | None:
    item = props.get(key)
    if isinstance(item, dict):
        return units.num(item.get("value"))
    return units.num(item)


# A station only stands for a place this close and at about the same height: across the mountains (San Juan's
# reading came from Ponce, 70 km away) or 800 m up the ridge (Ojai's from Rose Valley) the model's estimate for the
# spot itself is closer to the truth.
MAX_STATION_KM = 25
MAX_STATION_RISE_M = 300
OBS_TRIES = 3  # station reports fetched at most per answer


def latest_observation(point_props: dict[str, Any], *, lat: float | None = None, lon: float | None = None,
                       elevation_m: float | None = None) -> dict[str, Any] | None:
    """Newest usable observation from the nearest stations (skips silent or stale stations).

    Given the place (*lat*, *lon*, *elevation_m*), stations more than MAX_STATION_KM away or MAX_STATION_RISE_M
    higher or lower are skipped too; None then tells the caller to use the model's estimate instead."""
    import math

    def km_to(station: dict[str, Any]) -> float | None:
        s_lat, s_lon = units.num(station.get("lat")), units.num(station.get("lon"))
        if lat is None or lon is None or s_lat is None or s_lon is None:
            return None
        p1, p2 = math.radians(lat), math.radians(s_lat)
        a = (math.sin((p2 - p1) / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(math.radians(s_lon - lon) / 2) ** 2)
        return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))

    now = now_utc()
    fallback: dict[str, Any] | None = None
    tries = 0
    for station in stations(point_props):
        km = km_to(station)
        if km is not None and km > MAX_STATION_KM:
            continue
        if tries >= OBS_TRIES:
            break
        tries += 1
        try:
            fetched = http.get_json(f"{BASE}/stations/{station['id']}/observations/latest",
                                    ttl=http.TTL_OBS, max_stale=6 * 3600, allow_404=True)
        except http.FetchError:
            continue
        if not fetched.found:
            continue
        props = (fetched.data or {}).get("properties") or {}
        height = _value(props, "elevation")
        if elevation_m is not None and height is not None and abs(height - elevation_m) > MAX_STATION_RISE_M:
            continue
        observed = units.parse_time(props.get("timestamp"))
        sea_level = _value(props, "seaLevelPressure")
        obs = {
            "station": station["id"],
            "station_name": station.get("name"),
            "distance_km": round(km, 1) if km is not None else None,
            "elevation_m": height,
            "observed_at": observed,
            "age_min": units.minutes_ago(observed, now),
            "text": (props.get("textDescription") or "").strip() or None,
            "temp_c": _value(props, "temperature"),
            "dewpoint_c": _value(props, "dewpoint"),
            "rh": _value(props, "relativeHumidity"),
            "wind_dir": _value(props, "windDirection"),
            "wind_kmh": _value(props, "windSpeed"),
            "gust_kmh": _value(props, "windGust"),
            "pressure_pa": sea_level if sea_level is not None else _value(props, "barometricPressure"),
            "visibility_m": _value(props, "visibility"),
            "wind_chill_c": _value(props, "windChill"),
            "heat_index_c": _value(props, "heatIndex"),
            "stale_copy": fetched.stale,
            "url": fetched.url,
        }
        if obs["temp_c"] is None:
            continue
        if obs["age_min"] is not None and obs["age_min"] <= MAX_OBS_AGE_MIN:
            return obs
        if fallback is None:
            fallback = obs
    return fallback


def forecast(point_props: dict[str, Any], *, hourly: bool = False, metric: bool = False) -> http.Fetched:
    url = point_props.get("forecastHourly" if hourly else "forecast")
    if not url:
        raise http.FetchError(str(point_props.get("@id", "")), "no forecast URL for this point")
    params = {"units": "si"} if metric else None
    return http.get_json(url, params, ttl=http.TTL_FORECAST, max_stale=12 * 3600)


# --- alerts ----------------------------------------------------------------------------------

# Most urgent first. Anything not listed falls back on its NWS severity.
_TOP_EVENTS = (
    "Tornado Warning", "Extreme Wind Warning", "Tsunami Warning", "Storm Surge Warning",
    "Hurricane Warning", "Flash Flood Warning", "Severe Thunderstorm Warning",
    "Blizzard Warning", "Ice Storm Warning", "Dust Storm Warning",
)
_LIFE_THREATENING = {
    "Tornado Warning", "Extreme Wind Warning", "Tsunami Warning", "Storm Surge Warning",
    "Hurricane Warning", "Flash Flood Warning",
}
_SEVERITY_RANK = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}
_DETAIL_KEYS = (
    "tornadoDetection", "tornadoDamageThreat", "thunderstormDamageThreat", "flashFloodDetection",
    "flashFloodDamageThreat", "maxWindGust", "maxHailSize", "windThreat", "hailThreat",
    "snowSquallDetection", "waterspoutDetection",
)


def _kind(event: str) -> int:
    if event in _TOP_EVENTS:
        return 0
    lowered = event.lower()
    if "emergency" in lowered or "warning" in lowered:
        return 1
    if "watch" in lowered:
        return 2
    if "advisory" in lowered:
        return 3
    return 4


def alerts_at(lat: float, lon: float, *, ttl: float = http.TTL_ALERTS, backup: bool = True) -> http.Fetched:
    """Active alerts at a spot. When the NWS doesn't answer — or only a saved copy of its answer is left — the
    Iowa Environmental Mesonet's copy of its warnings (iem.py; `iem.is_copy(fetched.data)` says so), if that
    answers; otherwise the NWS's error. The NWS is asked first every time, so it's back the moment it answers."""
    try:
        fetched = http.get_json(f"{BASE}/alerts/active",
                                {"point": f"{_q(round(lat, 3))},{_q(round(lon, 3))}", "status": "actual"},
                                ttl=ttl, max_stale=10 * 60)
    except http.FetchError as exc:
        if not backup:
            raise
        try:
            return iem.alerts_at(lat, lon)
        except http.FetchError as backup_exc:
            raise exc from backup_exc
    if fetched.stale and backup:   # a fresh copy beats a saved answer (a saved copy doesn't: it lacks the texts)
        try:
            copy = iem.alerts_at(lat, lon)
        except http.FetchError:
            return fetched
        return fetched if copy.stale else copy
    return fetched


def alerts_in_area(area: str) -> http.Fetched:
    return http.get_json(f"{BASE}/alerts/active", {"area": area.upper(), "status": "actual"},
                         ttl=http.TTL_ALERTS, max_stale=10 * 60)


def normalize_alerts(data: Any, *, text_limit: int = 3000) -> list[dict[str, Any]]:
    """Active alerts, de-duplicated (an update replaces the alert it references), most urgent first.

    Updates are kept: NWS re-issues an ongoing warning as an "Update", and dropping updates
    (as some tools do) silently loses warnings that are still in effect."""
    features = (data or {}).get("features") or []
    superseded: set[str] = set()
    for feature in features:
        for ref in (feature.get("properties") or {}).get("references") or []:
            ident = ref.get("identifier") if isinstance(ref, dict) else None
            if ident:
                superseded.add(ident)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for feature in features:
        p = feature.get("properties") or {}
        ident = str(p.get("id") or p.get("@id") or "")
        if ident in superseded or ident in seen:
            continue
        if p.get("status") not in (None, "Actual") or p.get("messageType") == "Cancel":
            continue
        seen.add(ident)
        event = str(p.get("event") or "Alert")
        params = p.get("parameters") or {}
        nws_headline = (params.get("NWSheadline") or [None])[0]
        details = {k: ", ".join(map(str, params[k])) for k in _DETAIL_KEYS if params.get(k)}
        description = (p.get("description") or "").strip()
        instruction = (p.get("instruction") or "").strip()
        out.append({
            "id": ident,
            "event": event,
            "severity": p.get("severity"),
            "urgency": p.get("urgency"),
            "certainty": p.get("certainty"),
            "life_threatening": event in _LIFE_THREATENING or p.get("severity") == "Extreme"
            or details.get("tornadoDamageThreat", "").upper() in {"CONSIDERABLE", "CATASTROPHIC"},
            "headline": nws_headline or p.get("headline"),
            "issued_by": p.get("senderName"),
            "area": p.get("areaDesc"),
            "starts": p.get("onset") or p.get("effective"),
            # `ends` is None for "until further notice"; `expires` is only when the next update is due.
            "ends": p.get("ends"),
            "expires": p.get("expires"),
            "references": [r.get("identifier") for r in p.get("references") or []
                           if isinstance(r, dict) and r.get("identifier")],
            "message_type": p.get("messageType"),
            "details": details,
            "description": description[:text_limit] + ("…" if len(description) > text_limit else ""),
            "instruction": instruction[:text_limit] + ("…" if len(instruction) > text_limit else ""),
            "web": p.get("web") or None,
        })
    out.sort(key=lambda a: (
        0 if a["life_threatening"] else 1,
        _kind(a["event"]),
        _TOP_EVENTS.index(a["event"]) if a["event"] in _TOP_EVENTS else 99,
        _SEVERITY_RANK.get(str(a["severity"]), 5),
    ))
    return out
