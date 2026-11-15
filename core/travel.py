"""Travel weather: airport conditions (METAR), airport forecast (TAF), FAA delays and closures, and
the weather at a destination when you arrive."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

from . import http, units
from .clock import now_utc
from .places import Place
from .sources import nws, openmeteo

AWC = "https://aviationweather.gov/api/data"
FAA_STATUS = "https://nasstatus.faa.gov/api/airport-status-information"
CATEGORY_MEANING = {"VFR": "good flying weather", "MVFR": "marginal (lower clouds or haze)",
                    "IFR": "poor (low clouds or fog)", "LIFR": "very poor (very low clouds or thick fog)"}
_ICAO = re.compile(r"^[A-Z][A-Z0-9]{3}$")
_IATA = re.compile(r"^[A-Z0-9]{3}$")


def icao_for(code: str) -> str:
    code = code.strip().upper()
    if _ICAO.match(code):
        return code
    if _IATA.match(code):
        return "K" + code  # lower 48; Alaska/Hawaii users can pass the 4-letter code
    raise ValueError(f"'{code}' doesn't look like an airport code (try DSM or KDSM).")


def _ceiling_ft(clouds: list[dict[str, Any]]) -> int | None:
    bases = [units.num(c.get("base")) for c in clouds or [] if c.get("cover") in {"BKN", "OVC", "OVX"}]
    bases = [b for b in bases if b is not None]
    return int(min(bases)) if bases else None


def _category(visib: Any, ceiling: int | None) -> str:
    vis = units.num(str(visib).rstrip("+")) if visib is not None else None
    vis = 10.0 if vis is None else vis
    ceil = 99999 if ceiling is None else ceiling
    if ceil < 500 or vis < 1:
        return "LIFR"
    if ceil < 1000 or vis < 3:
        return "IFR"
    if ceil <= 3000 or vis <= 5:
        return "MVFR"
    return "VFR"


def _wind(wdir: Any, wspd: Any, wgst: Any) -> str | None:
    speed = units.num(wspd)
    if speed is None:
        return None
    if speed == 0:
        return "calm"
    direction = "variable" if str(wdir).upper() == "VRB" else f"{units.compass(wdir) or wdir}"
    text = f"{direction} {round(speed)} kt"
    gust = units.num(wgst)
    return text + (f", gusts {round(gust)} kt" if gust else "")


def airport(code: str, units_: str, clock: str) -> dict[str, Any]:
    icao = icao_for(code)
    metar = http.get_json(f"{AWC}/metar", {"ids": icao, "format": "json"}, ttl=5 * 60, max_stale=3 * 3600)
    rows = metar.data if isinstance(metar.data, list) else []
    if not rows:
        guessed = icao != code.strip().upper()
        hint = ("Outside the lower 48 use the 4-letter ICAO code, e.g. EGLL (Heathrow), PHNL (Honolulu), "
                "PANC (Anchorage), CYYZ (Toronto)." if guessed
                else "Check the code, or ask for weather at the city instead.")
        return {"airport": icao, "error": f"No current report for {icao}.", "hint": hint}
    m = rows[0]
    observed = datetime.fromtimestamp(int(m["obsTime"]), UTC) if m.get("obsTime") else None
    tz = units.zone(_airport_zone(icao, m.get("lat"), m.get("lon")))
    now = now_utc().astimezone(tz)
    ceiling = _ceiling_ft(m.get("clouds") or [])
    cat = m.get("fltCat") or _category(m.get("visib"), ceiling)
    result: dict[str, Any] = {
        "airport": f"{icao} ({m.get('name') or ''})".replace(" ()", ""),
        "now": {k: v for k, v in {
            "observed": units.day_clock(observed.astimezone(tz), clock, now) if observed else None,
            "flight_category": f"{cat}: {CATEGORY_MEANING.get(cat, '')}".rstrip(": "),
            "temp": units.temp(m.get("temp"), units_),
            "wind": _wind(m.get("wdir"), m.get("wspd"), m.get("wgst")),
            "visibility_mi": m.get("visib"),
            "ceiling_ft": ceiling,
            "weather": m.get("wxString"),
            "raw": m.get("rawOb"),
        }.items() if v is not None},
        "source": "aviationweather.gov (METAR/TAF), FAA NAS status",
    }
    try:
        result["forecast"] = _taf(icao, tz, clock, now)
    except http.FetchError as exc:
        result["notes"] = [f"Airport forecast unavailable ({exc})"]
    try:
        status = faa_status(icao[1:] if icao.startswith("K") else icao)
        result["faa_status"] = status or "No delays or closures reported"
    except http.FetchError as exc:
        result.setdefault("notes", []).append(f"FAA status unavailable ({exc})")
    return result


def _airport_zone(icao: str, lat: Any, lon: Any) -> str | None:
    try:
        name = (nws.station_info(icao) or {}).get("timezone")
        if name:
            return str(name)
    except http.FetchError:
        pass
    la, lo = units.num(lat), units.num(lon)
    if la is None or lo is None:
        return None
    try:
        return openmeteo.timezone_at(la, lo)
    except http.FetchError:
        return None


def _taf(icao: str, tz: Any, clock: str, now: datetime) -> list[dict[str, Any]]:
    fetched = http.get_json(f"{AWC}/taf", {"ids": icao, "format": "json"}, ttl=20 * 60, max_stale=6 * 3600)
    rows = fetched.data if isinstance(fetched.data, list) else []
    if not rows:
        return []
    periods = []
    for f in rows[0].get("fcsts") or []:
        start = datetime.fromtimestamp(int(f["timeFrom"]), UTC).astimezone(tz) if f.get("timeFrom") else None
        end = datetime.fromtimestamp(int(f["timeTo"]), UTC).astimezone(tz) if f.get("timeTo") else None
        if end is not None and end < now:
            continue
        ceiling = _ceiling_ft(f.get("clouds") or [])
        periods.append({k: v for k, v in {
            "from": ("now" if start <= now else units.day_clock(start, clock, now)) if start else None,
            "to": units.day_clock(end, clock, now) if end else None,
            "type": {"FM": None, "TEMPO": "temporarily", "BECMG": "becoming", "PROB": "possibly"}.get(
                str(f.get("fcstChange") or "FM"), f.get("fcstChange")),
            "chance_pct": f.get("probability"),
            "flight_category": _category(f.get("visib"), ceiling),
            "wind": _wind(f.get("wdir"), f.get("wspd"), f.get("wgst")),
            "visibility_mi": f.get("visib"),
            "ceiling_ft": ceiling,
            "weather": f.get("wxString"),
        }.items() if v is not None})
    return periods[:8]


def faa_status(code: str) -> list[dict[str, Any]]:
    """Delays, ground stops and closures the FAA lists for one airport (3-letter code)."""
    data = _faa_xml()
    root = ET.fromstring(data)
    out = []
    for delay_type in root.iter("Delay_type"):
        kind = (delay_type.findtext("Name") or "").strip()
        for el in delay_type.iter():
            if el.findtext("ARPT", "").strip().upper() != code.upper():
                continue
            fields = {child.tag: " ".join((child.text or "").split()) for child in el if (child.text or "").strip()}
            for child in el:
                for sub in child:
                    text = (sub.text or "").strip()
                    if text:
                        fields[f"{child.tag}_{sub.tag}".replace("Arrival_Departure_", "")] = text
            fields.pop("ARPT", None)
            out.append({"type": kind, **fields})
    return out


_faa_cache: dict[str, Any] = {"at": 0.0, "body": b""}


def _faa_xml() -> bytes:
    if time.time() - _faa_cache["at"] < 5 * 60 and _faa_cache["body"]:
        return _faa_cache["body"]
    body, _ = http.download(FAA_STATUS)
    if not body.lstrip().startswith(b"<"):
        raise http.FetchError(FAA_STATUS, "unexpected response")
    _faa_cache.update(at=time.time(), body=body)
    return body


# --- destination on arrival ---------------------------------------------------------------------

def arrival(place: Place, arrive: str | None, units_: str, clock: str) -> dict[str, Any]:
    data = openmeteo.forecast(place.lat, place.lon, 7).data or {}
    tz = units.zone(data.get("timezone"), data.get("utc_offset_seconds"))
    now = now_utc().astimezone(tz)
    when = _parse_arrival(arrive, tz, now)
    hourly = data.get("hourly") or {}
    best: tuple[float, int] | None = None
    for i, stamp in enumerate(hourly.get("time") or []):
        dt = units.parse_time(stamp)
        if dt is None:
            continue
        gap = abs((dt.replace(tzinfo=tz) - when).total_seconds())
        if best is None or gap < best[0]:
            best = (gap, i)
    if best is None or best[0] > 3 * 3600:
        return {"place": place.public(), "error": "That arrival time is beyond the 7-day forecast."}
    i = best[1]

    def pick(key: str) -> Any:
        values = hourly.get(key) or []
        return values[i] if i < len(values) else None

    result: dict[str, Any] = {
        "place": place.public(),
        "arriving": units.day_clock(when, clock, now),
        "expected": {k: v for k, v in {
            "temp": units.temp(pick("temperature_2m"), units_),
            "feels_like": units.temp(pick("apparent_temperature"), units_),
            "sky": openmeteo.describe(pick("weather_code")),
            "precip_chance_pct": pick("precipitation_probability"),
            "wind": units.wind(pick("wind_direction_10m"), pick("wind_speed_10m"), pick("wind_gusts_10m"), units_),
        }.items() if v is not None},
        "source": "Open-Meteo hourly forecast",
    }
    if place.maybe_us:
        try:
            alerts = nws.normalize_alerts(nws.alerts_at(place.lat, place.lon).data)
            result["alerts_there_now"] = [a["event"] for a in alerts]
        except http.FetchError:
            result["notes"] = ["Couldn't check alerts at the destination"]
    return result


def _parse_arrival(text: str | None, tz: Any, now: datetime) -> datetime:
    if not text:
        return now
    dt = units.parse_time(str(text).strip())
    if dt is None:
        raise ValueError("Give the arrival time as YYYY-MM-DDTHH:MM (local time at the destination).")
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)
