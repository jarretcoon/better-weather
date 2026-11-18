"""Turn whatever the user said ("home", "Des Moines, IA", "50309", "KDSM", "41.6,-93.6",
a saved place name) into coordinates."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from . import http, settings, units
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
_IATA = re.compile(r"^[A-Za-z]{3}$")
# US territories: the geocoder files them under their own country code, not under "US"
_TERRITORIES = {"PR", "GU", "VI", "AS", "MP"}
# places renamed since the geocoder's name list was made, asked for by their old name with their state
_RENAMED = {("barrow", "AK"): "Utqiagvik"}  # 2016
# common US shorthands, typed on their own (dots dropped: "L.A.", "D.C.")
_ALIASES = {"la": "Los Angeles, CA", "nyc": "New York, NY", "sf": "San Francisco, CA", "dc": "Washington, DC",
            "philly": "Philadelphia, PA", "vegas": "Las Vegas, NV", "district of columbia": "Washington, DC"}
# a well-known place in each state, for "Which place in Iowa? Try 'Des Moines, IA'"
_STATE_EXAMPLES = {
    "AL": "Birmingham", "AK": "Anchorage", "AZ": "Phoenix", "AR": "Little Rock", "CA": "Los Angeles",
    "CO": "Denver", "CT": "Hartford", "DE": "Wilmington", "FL": "Miami", "GA": "Atlanta", "HI": "Honolulu",
    "ID": "Boise", "IL": "Chicago", "IN": "Indianapolis", "IA": "Des Moines", "KS": "Wichita", "KY": "Louisville",
    "LA": "New Orleans", "ME": "Portland", "MD": "Baltimore", "MA": "Boston", "MI": "Detroit",
    "MN": "Minneapolis", "MS": "Jackson", "MO": "Kansas City", "MT": "Billings", "NE": "Omaha",
    "NV": "Las Vegas", "NH": "Manchester", "NJ": "Newark", "NM": "Albuquerque", "NY": "Buffalo",
    "NC": "Charlotte", "ND": "Fargo", "OH": "Columbus", "OK": "Oklahoma City", "OR": "Portland",
    "PA": "Philadelphia", "RI": "Providence", "SC": "Charleston", "SD": "Sioux Falls", "TN": "Nashville",
    "TX": "Houston", "UT": "Salt Lake City", "VT": "Burlington", "VA": "Richmond", "WA": "Seattle",
    "WV": "Charleston", "WI": "Milwaukee", "WY": "Cheyenne", "DC": "Washington", "PR": "San Juan",
    "GU": "Hagatna", "VI": "Charlotte Amalie", "AS": "Pago Pago", "MP": "Saipan",
}
# the geocoder knows each place under one spelling: "St. Petersburg" (Florida), "Saint Paul", "Fort Myers"
_SPELLINGS = {"st": ("St.", "Saint"), "saint": ("St.",), "ft": ("Fort",), "mt": ("Mount", "Mt.")}
_SHORT_WORDS = {"st": "saint", "ft": "fort", "mt": "mount"}
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


def _state_code(text: str) -> str | None:
    """'IA', 'ia', 'Iowa', 'puerto rico' -> the US state or territory code; anything else -> None."""
    q = text.strip().rstrip(".")
    if len(q) == 2 and q.upper() in US_STATES:
        return q.upper()
    return _STATE_NAMES.get(q.lower())


def _in_state(result: dict[str, Any], code: str) -> bool:
    country = str(result.get("country_code") or "").upper()
    admin1 = str(result.get("admin1") or "").lower()
    return (country == "US" and admin1 == US_STATES[code].lower()) or (code in _TERRITORIES and country == code)


def _plain(name: Any) -> str:
    """'St. Paul' and 'st paul' compare equal."""
    return " ".join(str(name or "").lower().replace(".", " ").split())


def _same_name(a: Any, b: Any) -> bool:
    """'St Paul' and 'Saint Paul', 'Ft Myers' and 'Fort Myers', 'Mt Vernon' and 'Mt. Vernon' are one name."""
    def canon(name: Any) -> list[str]:
        words = _plain(name).split()
        return [_SHORT_WORDS.get(words[0], words[0]), *words[1:]] if words else []
    return canon(a) == canon(b)


def _matches(result: dict[str, Any], qualifier: str) -> bool:
    q = qualifier.strip().lower().rstrip(".")
    if not q:
        return True
    country = str(result.get("country_code") or "").upper()
    admin1 = str(result.get("admin1") or "").lower()
    country_name = str(result.get("country") or "").lower()
    code = _state_code(q)
    if code:
        return _in_state(result, code)
    if q.upper() in CA_PROVINCES and len(q) == 2:
        return country == "CA" and admin1 == CA_PROVINCES[q.upper()].lower()
    if q in _COUNTRY_ALIASES:
        return country == _COUNTRY_ALIASES[q]
    return q in {admin1, country_name, country.lower()} or admin1.startswith(q) or country_name.startswith(q)


def _geocode(name: str) -> list[dict[str, Any]]:
    try:
        return openmeteo.geocode(name)
    except http.FetchError as exc:
        raise PlaceError(f"Place search is unavailable right now ({exc}).",
                         "Try again, or pass coordinates like '41.6,-93.6'.") from exc


def _respellings(head: str) -> list[str]:
    """'St Paul' -> 'St. Paul', 'Saint Paul'; 'Ft Myers' -> 'Fort Myers'; 'Saint Louis' -> 'St. Louis'."""
    first, _, rest = head.strip().partition(" ")
    rest = rest.strip()
    alternatives = _SPELLINGS.get(first.lower().rstrip("."), ()) if rest else ()
    return [f"{a} {rest}" for a in alternatives if f"{a} {rest}" != head.strip()]


def _town(result: dict[str, Any]) -> bool:
    """A city, town or country, not the airport, park or mountain named after it."""
    return str(result.get("feature_code") or "PPL").startswith(("PPL", "PCL"))


def _found(results: list[dict[str, Any]], head: str, qualifiers: list[str]) -> bool:
    """Whether the search found what was asked: a town matching every qualifier, or else a place by that very name."""
    if qualifiers:
        return any(_town(r) and all(_matches(r, q) for q in qualifiers) for r in results)
    return any(_plain(r.get("name")) == _plain(head) for r in results)


def _abroad_note(head: str, picked: dict[str, Any], results: list[dict[str, Any]]) -> str | None:
    """'Paris' with nothing after it is Paris, France: say so, and how to get the Paris in the US instead."""
    if str(picked.get("country_code") or "").upper() in ("US", *_TERRITORIES):
        return None
    us = next((r for r in results if _plain(r.get("name")) == _plain(head)
               and str(r.get("country_code") or "").upper() == "US"
               and str(r.get("admin1") or "").lower() in _STATE_NAMES), None)
    if us is None:
        return None
    name = str(us.get("name"))
    return (f"Using {_label(picked)}. For {name} in the US, add the state, "
            f"like '{name}, {_STATE_NAMES[str(us['admin1']).lower()]}'.")


def _search(text: str) -> Place:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise PlaceError("Empty place name.")
    head, qualifiers = parts[0], parts[1:]
    state = next((c for c in map(_state_code, qualifiers) if c), None)
    head = _RENAMED.get((head.lower(), state), head) if state else head
    results = _geocode(head)
    if not results and qualifiers:
        results = _geocode(text)
    others = _respellings(head) if not _found(results, head, qualifiers) else []
    if others:
        # "St Paul, MN" finds nothing in Minnesota; "Saint Paul, MN" does. Of the other spellings' answers, the
        # biggest town by that name (in that state): Saint Paul, MN for a bare "St Paul", not the one in Alaska.
        pool = [r for other in others for r in _geocode(other)]
        named = [r for r in pool if _town(r) and _same_name(r.get("name"), head)
                 and all(_matches(r, q) for q in qualifiers)]
        if named:
            return _from_result(max(named, key=lambda r: units.num(r.get("population")) or 0), "search")
        results = results or pool
    if not results:
        raise PlaceError(f"Couldn't find a place called '{text}'.",
                         "Try 'City, ST', a ZIP code, an airport code like KDSM, or coordinates.")
    if qualifiers:
        filtered = sorted((r for r in results if all(_matches(r, q) for q in qualifiers)),
                          key=lambda r: not _town(r))  # the town before its airport
        if filtered:
            return _from_result(filtered[0], "search")
        if state:
            # a US state was named: the closest US place, never one in another country ("Barrow, AK" isn't England)
            us = [r for r in results if str(r.get("country_code") or "").upper() in ("US", state)]
            if not us:
                raise PlaceError(f"Couldn't find '{parts[0]}' in {US_STATES[state]}.",
                                 "Check the spelling, or try a ZIP code, an airport code like KDSM, or coordinates.")
            return _from_result(us[0], "search", note=f"No exact match for '{text}'; using the closest US result.")
        return _from_result(results[0], "search",
                            note=f"No exact match for '{text}'; using the closest result.")
    return _from_result(results[0], "search", note=_abroad_note(head, results[0], results))


def _state_example(code: str) -> str:
    return f"{_STATE_EXAMPLES[code]}, {code}"


def _bare_state(text: str, code: str) -> Place:
    """'Iowa' on its own: a whole state isn't a place to forecast, so ask which place in it. A few state names
    are also a big city or a country by that name ('New York', 'Washington' for DC, 'Georgia'): those resolve,
    with a note when the place isn't in that state."""
    results = _geocode(text) if len(text.strip()) > 2 else []
    for r in results:
        name, feature = _plain(r.get("name")), str(r.get("feature_code") or "")
        big = feature.startswith("PCL") or (units.num(r.get("population")) or 0) >= 100_000  # country or big city
        capital = feature == "PPLC" and name.startswith(_plain(text))  # "Washington" -> Washington D.C.
        if (name == _plain(text) and big) or capital:
            place = _from_result(r, "search")
            if _in_state(r, code):
                return place
            return replace(place, note=f"Using {place.name}. For the US state of {US_STATES[code]}, name a place "
                                       f"in it, like '{_state_example(code)}'.")
    raise PlaceError(f"Which place in {US_STATES[code]}?",
                     f"Name a city or town there, like '{_state_example(code)}', or give a ZIP code.")


def _with_state_comma(text: str) -> str | None:
    """'clarion iowa' -> 'clarion, iowa', 'des moines ia' -> 'des moines, ia': a US state at the end without a comma."""
    if "," in text or _state_code(text):
        return None
    words = text.split()
    for n in (2, 1):
        if len(words) > n and _state_code(" ".join(words[-n:])):
            return f"{' '.join(words[:-n])}, {' '.join(words[-n:])}"
    return None


def us_airport_station(code: str) -> tuple[str, dict[str, Any]] | None:
    """'ORD' -> KORD, 'HNL' -> PHNL: a US airport's weather station by its 3-letter code (K + the code, or P + it
    in Alaska, Hawaii and the Pacific), with the station's details. None when neither is a station."""
    for icao in ("K" + code.upper(), "P" + code.upper()):
        info = _station(icao)
        if info is None:
            continue
        # P codes count only out in Alaska, Hawaii and the Pacific: PRIO, say, is a weather station in Oregon
        if icao.startswith("P") and -129 < float(info["lon"]) < 130:
            continue
        return icao, info
    return None


def _us_airport(code: str) -> Place | None:
    """'ORD', 'HNL': a US airport by its 3-letter code."""
    found = us_airport_station(code)
    return _airport_place(*found) if found else None


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


def _station(code: str) -> dict[str, Any] | None:
    try:
        return nws.station_info(code) or None
    except http.FetchError:
        return None


def _airport_place(code: str, info: dict[str, Any]) -> Place:
    name = str(info.get("name") or code)
    return Place(f"{name} ({code})", float(info["lat"]), float(info["lon"]), None, None,
                 info.get("timezone"), "airport")


def _airport(code: str) -> Place | None:
    info = _station(code)
    return _airport_place(code, info) if info else None


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

    raw = "" if text is None else str(text).strip()  # a ZIP code can arrive as a number
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
        # no note: the user saw it when they set home, and it isn't news on every question after that
        return replace(place, kind="home", note=None)

    saved = saved_places().get(raw.lower())
    if saved:
        return Place(str(saved.get("name") or raw), float(saved["lat"]), float(saved["lon"]),
                     saved.get("country"), saved.get("region"), saved.get("timezone"), "saved",
                     district=saved.get("district"))

    alias = _ALIASES.get(" ".join(raw.lower().replace(".", "").split()))
    if alias:  # "LA", "NYC": not La in Cambodia, not the Central Park weather station
        return _search(alias)

    state = _state_code(raw)
    if state:  # "Iowa", "IA" on their own
        return _bare_state(raw, state)

    match = _COORDS.match(raw)
    if match:
        return resolve(None, match.group(1), match.group(2), _depth=_depth + 1)

    if _ZIP.match(raw):
        return _zip(raw)

    if _ICAO.match(raw):  # only exact upper-case codes, so "Rome" or "Oslo" stay place names
        airport = _airport(raw)
        if airport is not None:
            return airport
    if _IATA.match(raw) and raw.isupper():  # "ORD", "LAX": an airport code first
        airport = _us_airport(raw)
        if airport is not None:
            return airport

    with_comma = _with_state_comma(raw)  # "clarion iowa": only when that finds the place in that state
    if with_comma:
        try:
            place = _search(with_comma)
            if place.note is None:
                return place
        except PlaceError:
            pass

    try:
        place = _search(raw)
        # "ord" typed in lower case: a 3-letter word that is also a US airport code, when the search went abroad
        if _IATA.match(raw) and raw.islower() and (place.country or "").upper() not in ("US", *_TERRITORIES):
            airport = _us_airport(raw)
            if airport is not None:
                return airport
        return place
    except PlaceError:
        # "kdsm" typed in lower case: give the airport reading a chance before giving up.
        if len(raw) == 4 and raw.isalnum() and not _ICAO.match(raw):
            airport = _airport(raw.upper())
            if airport is not None:
                return airport
        raise
