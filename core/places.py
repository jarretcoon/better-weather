"""Turn whatever the user said ("home", "Des Moines, IA", "50309", "KDSM", "41.6,-93.6",
a saved place name) into coordinates."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import http, settings
from .sources import nws, openmeteo

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    "PR": "Puerto Rico", "GU": "Guam", "VI": "U.S. Virgin Islands", "AS": "American Samoa",
    "MP": "Northern Mariana Islands",
}
_STATE_NAMES = {v.lower(): k for k, v in US_STATES.items()}
CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
}
_COUNTRY_ALIASES = {
    "usa": "US", "us": "US", "u.s.": "US", "u.s.a.": "US", "united states": "US", "america": "US",
    "uk": "GB", "u.k.": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "britain": "GB",
    "great britain": "GB", "united kingdom": "GB",
}
_HOME_WORDS = {"", "home", "here", "my house", "my place", "house", "local", "my area", "me"}
_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_ZIP = re.compile(r"^\d{5}(?:-\d{4})?$")
_ICAO = re.compile(r"^[A-Z][A-Z0-9]{3}$")
PLACES_FILE = "places.json"  # in the plugin's data folder, so the Desktop backend sees the same places
_places_lock = threading.RLock()
logger = logging.getLogger(__name__)


class PlaceError(Exception):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    country: str | None = None
    region: str | None = None
    timezone: str | None = None
    kind: str = "search"
    note: str | None = None
    district: str | None = None  # county/department, used to match European warnings by region name

    @property
    def maybe_us(self) -> bool:
        return self.country is None or self.country.upper() in nws.NWS_COUNTRIES

    def public(self) -> dict[str, Any]:
        out = {k: v for k, v in asdict(self).items() if v is not None}
        out["lat"] = round(self.lat, 3)
        out["lon"] = round(self.lon, 3)
        return out


def _label(result: dict[str, Any]) -> str:
    name = str(result.get("name") or "")
    country = str(result.get("country_code") or "").upper()
    admin1 = str(result.get("admin1") or "")
    if country == "US" and admin1:
        return f"{name}, {_STATE_NAMES.get(admin1.lower(), admin1)}"
    if admin1 and admin1 != name:
        return f"{name}, {admin1}, {country}" if country else f"{name}, {admin1}"
    return f"{name}, {country}" if country else name


def _from_result(result: dict[str, Any], kind: str, note: str | None = None) -> Place:
    return Place(
        name=_label(result),
        lat=float(result["latitude"]),
        lon=float(result["longitude"]),
        country=(str(result.get("country_code") or "").upper() or None),
        region=result.get("admin1"),
        timezone=result.get("timezone"),
        kind=kind,
        note=note,
        district=result.get("admin2"),
    )


def _matches(result: dict[str, Any], qualifier: str) -> bool:
    q = qualifier.strip().lower().rstrip(".")
    if not q:
        return True
    country = str(result.get("country_code") or "").upper()
    admin1 = str(result.get("admin1") or "").lower()
    country_name = str(result.get("country") or "").lower()
    if q.upper() in US_STATES and len(q) == 2:
        return country == "US" and admin1 == US_STATES[q.upper()].lower()
    if q.upper() in CA_PROVINCES and len(q) == 2:
        return country == "CA" and admin1 == CA_PROVINCES[q.upper()].lower()
    if q in _STATE_NAMES:
        return country == "US" and admin1 == q
    if q in _COUNTRY_ALIASES:
        return country == _COUNTRY_ALIASES[q]
    return q in {admin1, country_name, country.lower()} or admin1.startswith(q) or country_name.startswith(q)


def _search(text: str) -> Place:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise PlaceError("Empty place name.")
    head, qualifiers = parts[0], parts[1:]
    try:
        results = openmeteo.geocode(head)
        if not results and qualifiers:
            results = openmeteo.geocode(text)
    except http.FetchError as exc:
        raise PlaceError(f"Place search is unavailable right now ({exc}).",
                         "Try again, or pass coordinates like '41.6,-93.6'.") from exc
    if not results:
        raise PlaceError(f"Couldn't find a place called '{text}'.",
                         "Try 'City, ST', a ZIP code, an airport code like KDSM, or coordinates.")
    if qualifiers:
        filtered = [r for r in results if all(_matches(r, q) for q in qualifiers)]
        if filtered:
            return _from_result(filtered[0], "search")
        return _from_result(results[0], "search",
                            note=f"No exact match for '{text}'; using the closest result.")
    return _from_result(results[0], "search")


def _zip(text: str) -> Place:
    code = text[:5]
    try:
        results = openmeteo.geocode(code)
    except http.FetchError as exc:
        raise PlaceError(f"ZIP lookup is unavailable right now ({exc}).") from exc
    us = [r for r in results if str(r.get("country_code") or "").upper() == "US"]
    if not us:
        raise PlaceError(f"Couldn't find US ZIP code {code}.")
    place = _from_result(us[0], "zip")
    return Place(f"{place.name} ({code})", place.lat, place.lon, place.country, place.region,
                 place.timezone, "zip", district=place.district)


def _airport(code: str) -> Place | None:
    try:
        info = nws.station_info(code)
    except http.FetchError:
        info = None
    if not info:
        return None
    name = str(info.get("name") or code)
    return Place(f"{name} ({code})", float(info["lat"]), float(info["lon"]), None, None,
                 info.get("timezone"), "airport")


def _coords(lat: float, lon: float) -> Place:
    """Coordinates: the US grid lookup gives the timezone and nearest town; elsewhere ask Open-Meteo
    for the timezone. Either lookup failing leaves the field empty; the tools still work."""
    name = f"{lat:.3f}, {lon:.3f}"
    try:
        point = nws.point(lat, lon)
    except http.FetchError:
        return Place(name, lat, lon, kind="coords")
    if point:
        near = (point.get("relativeLocation") or {}).get("properties") or {}
        town = ", ".join(str(near[k]) for k in ("city", "state") if near.get(k))
        return Place(f"{name} (near {town})" if town else name, lat, lon, "US", US_STATES.get(str(near.get("state"))),
                     point.get("timeZone"), "coords")
    try:
        tz = openmeteo.timezone_at(lat, lon)
    except http.FetchError:
        tz = None
    return Place(name, lat, lon, timezone=tz, kind="coords")


def _places_path() -> Path | None:
    directory = settings.data_dir()
    return directory / PLACES_FILE if directory else None


def _write_places(path: Path, places: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".places.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(places, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def saved_places() -> dict[str, dict[str, Any]]:
    path = _places_path()
    if path is None:  # outside Hermes (tests, a bare import): in-memory state
        value = settings.current().state_get("places", {})
        return dict(value) if isinstance(value, dict) else {}
    with _places_lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            legacy = settings.current().state_get("places", {})  # 0.8.x kept them in Hermes plugin state
            if isinstance(legacy, dict) and legacy:
                _write_places(path, legacy)
                return dict(legacy)
            return {}
        except (OSError, ValueError) as exc:
            raise PlaceError(f"The saved places file can't be read ({exc}).", f"Check {path}.") from exc
    return data if isinstance(data, dict) else {}


def _store_places(places: dict[str, Any]) -> None:
    path = _places_path()
    if path is None:
        settings.current().state_set("places", places)
        return
    _write_places(path, places)


def save_place(name: str, place: Place) -> dict[str, Any]:
    record = place.public()
    record["kind"] = "saved"
    with _places_lock:
        places = saved_places()
        places[name.strip().lower()] = record
        _store_places(places)
    return record


def remove_place(name: str) -> bool:
    with _places_lock:
        places = saved_places()
        if places.pop(name.strip().lower(), None) is None:
            return False
        _store_places(places)
    return True


def resolve(text: str | None = None, lat: Any = None, lon: Any = None, *, _depth: int = 0) -> Place:
    """Resolve a location. Raises PlaceError with a user-facing hint when it can't."""
    if lat is not None and lon is not None:
        try:
            la, lo = float(lat), float(lon)
        except (TypeError, ValueError) as exc:
            raise PlaceError("lat/lon must be numbers.") from exc
        if not (-90 <= la <= 90 and -180 <= lo <= 180):
            raise PlaceError("lat/lon out of range.")
        return _coords(la, lo)

    raw = (text or "").strip()
    if raw.lower() in _HOME_WORDS:
        home = settings.current().home
        if not home:
            raise PlaceError(
                "No home location is set yet.",
                "Ask the user where home is, then call weather_places with action='set_home'.",
            )
        if _depth > 2 or home.lower() in _HOME_WORDS:
            raise PlaceError("The saved home location is invalid.", "Set it again with weather_places.")
        place = resolve(home, _depth=_depth + 1)
        return Place(place.name, place.lat, place.lon, place.country, place.region, place.timezone,
                     "home", place.note, place.district)

    saved = saved_places().get(raw.lower())
    if saved:
        return Place(str(saved.get("name") or raw), float(saved["lat"]), float(saved["lon"]),
                     saved.get("country"), saved.get("region"), saved.get("timezone"), "saved",
                     district=saved.get("district"))

    match = _COORDS.match(raw)
    if match:
        return resolve(None, match.group(1), match.group(2), _depth=_depth + 1)

    if _ZIP.match(raw):
        return _zip(raw)

    if _ICAO.match(raw):  # only exact upper-case codes, so "Rome" or "Oslo" stay place names
        airport = _airport(raw)
        if airport is not None:
            return airport

    try:
        return _search(raw)
    except PlaceError:
        # "kdsm" typed in lower case: give the airport reading a chance before giving up.
        if len(raw) == 4 and raw.isalnum() and not _ICAO.match(raw):
            airport = _airport(raw.upper())
            if airport is not None:
                return airport
        raise
