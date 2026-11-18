"""Official alerts outside the US.

Canada (Environment and Climate Change Canada, GeoMet): every alert carries its map area, so it is
matched to the exact spot. Europe (MeteoAlarm, 38 countries): warnings with a map area are matched to
the spot; most countries list warnings by area name only, so those count when an area's name is the
town, county or region itself. A spot is only "clear" when every active warning had a map area."""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from collections.abc import Iterable
from typing import Any

from .. import geo, http, units
from ..clock import now_utc

ECCC = "https://api.weather.gc.ca/collections/weather-alerts/items"
METEOALARM = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{slug}"
TEXT_LIMIT = 2500
REGIONS_KEPT = 8  # area names kept per warning; "more_regions" counts the rest
# A country's MeteoAlarm feed is downloaded again after FEED_FRESH. Some are big (Germany's is ~8 MB: 6-20 s), so
# until FEED_KEEP the last copy keeps answering while the new one downloads in the background: only the very first
# visitor (a fresh process) waits for it. Each copy keeps the time MeteoAlarm actually answered (a saved copy can be
# older than the moment it was read), and europe() gives its age so answers can say when it's getting old.
FEED_FRESH = 10 * 60
FEED_KEEP = 30 * 60
_feeds: dict[str, tuple[float, dict[str, Any]]] = {}  # url -> (when MeteoAlarm answered, time.time(); the feed)
_refreshing: set[str] = set()
_feeds_lock = threading.Lock()
logger = logging.getLogger(__name__)

EU_SLUGS = {
    "AT": "austria", "BE": "belgium", "BA": "bosnia-herzegovina", "BG": "bulgaria", "HR": "croatia",
    "CY": "cyprus", "CZ": "czechia", "DK": "denmark", "EE": "estonia", "FI": "finland", "FR": "france",
    "DE": "germany", "GR": "greece", "HU": "hungary", "IS": "iceland", "IE": "ireland", "IL": "israel",
    "IT": "italy", "LV": "latvia", "LT": "lithuania", "LU": "luxembourg", "MT": "malta", "MD": "moldova",
    "ME": "montenegro", "NL": "netherlands", "MK": "republic-of-north-macedonia", "NO": "norway",
    "PL": "poland", "PT": "portugal", "RO": "romania", "RS": "serbia", "SK": "slovakia", "SI": "slovenia",
    "ES": "spain", "SE": "sweden", "CH": "switzerland", "UA": "ukraine", "GB": "united-kingdom",
}
_LEVELS = {"1": "green", "2": "yellow", "3": "orange", "4": "red"}
_CA_SEVERITY = {"warning": "Severe", "watch": "Moderate", "advisory": "Minor", "statement": "Minor"}


def canada(lat: float, lon: float) -> list[dict[str, Any]]:
    d = 0.02
    bbox = f"{lon - d:.3f},{lat - d:.3f},{lon + d:.3f},{lat + d:.3f}"
    data = http.get_json(ECCC, {"f": "json", "limit": 200, "bbox": bbox}, ttl=120, max_stale=10 * 60).data or {}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for f in data.get("features") or []:
        if not geo.contains(f.get("geometry"), lon, lat):
            continue
        p = f.get("properties") or {}
        key = (str(p.get("alert_code")), str(p.get("validity_datetime")))
        if key in seen or str(p.get("status_en") or "").lower() in {"ended", "terminée"}:
            continue
        if _expired(p.get("event_end_datetime") or p.get("expiration_datetime")):
            continue
        seen.add(key)
        kind = str(p.get("alert_type") or "").lower()
        name = str(p.get("alert_name_en") or p.get("alert_short_name_en") or "Alert")
        text = str(p.get("alert_text_en") or "").strip()
        out.append({k: v for k, v in {
            "event": name[:1].upper() + name[1:],
            "severity": _CA_SEVERITY.get(kind, "Unknown"),
            "life_threatening": kind == "warning"
            and any(w in name.lower() for w in ("tornado", "hurricane", "storm surge")),
            "headline": f"{p.get('alert_short_name_en') or name} for {p.get('feature_name_en') or 'this area'}",
            "colour": p.get("risk_colour_en"),
            "starts": p.get("validity_datetime"),
            "ends": p.get("event_end_datetime") or None,
            "next_update_by": p.get("expiration_datetime"),
            "issued_by": "Environment and Climate Change Canada",
            "description": text[:TEXT_LIMIT] + ("…" if len(text) > TEXT_LIMIT else ""),
        }.items() if v not in (None, "")})
    rank = {"Severe": 0, "Moderate": 1, "Minor": 2}
    out.sort(key=lambda a: (0 if a.get("life_threatening") else 1, rank.get(a["severity"], 3)))
    return out


def _expired(value: Any) -> bool:
    end = units.parse_time(value)
    return end is not None and end.tzinfo is not None and end < now_utc()


def _info_en(alert: dict[str, Any]) -> dict[str, Any] | None:
    infos = alert.get("info") or []
    for info in infos:
        if str(info.get("language") or "").lower().startswith("en"):
            return info
    return infos[0] if infos else None


def _level(info: dict[str, Any]) -> tuple[int, str]:
    for p in info.get("parameter") or []:
        if p.get("valueName") == "awareness_level":
            code = str(p.get("value") or "").split(";")[0].strip()
            return int(code) if code.isdigit() else 0, _LEVELS.get(code, "unknown")
    return 0, "unknown"


def _polygon(text: str) -> dict[str, Any] | None:
    ring = []
    for pair in text.split():
        try:
            la, lo = pair.split(",")
            ring.append([float(lo), float(la)])
        except ValueError:
            return None
    return {"type": "Polygon", "coordinates": [ring]} if len(ring) >= 3 else None


def _fold(text: Any) -> str:
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain.lower()).split())


_AREA_WORDS = {"kreis", "landkreis", "stadtkreis", "stadt", "city", "county", "province", "provincia", "region"}


def _area_key(text: Any) -> str:
    """'Stadt Berlin' / 'Kreis Pinneberg' / 'Paris (City)' -> 'berlin' / 'pinneberg' / 'paris'."""
    return " ".join(w for w in _fold(text).split() if w not in _AREA_WORDS)


def _names_in(area: str, names: list[str]) -> bool:
    """Whole-name match only: 'Madrid' is not 'Sierra de Madrid', 'Flanders' is not 'West Flanders'."""
    key = _area_key(area)
    return bool(key) and key in names


def _download(url: str) -> tuple[float, dict[str, Any]]:
    """(when MeteoAlarm answered, the feed): a saved copy keeps its own time."""
    fetched = http.get_json(url, ttl=FEED_FRESH, max_stale=FEED_KEEP)
    return fetched.fetched_at, fetched.data or {}


def _keep(url: str, copy: tuple[float, dict[str, Any]]) -> None:
    with _feeds_lock:
        kept = _feeds.get(url)
        if kept is None or copy[0] >= kept[0]:  # a failed refresh's saved copy never replaces a newer one
            _feeds[url] = copy


def _refresh(url: str) -> None:
    try:
        _keep(url, _download(url))
    except Exception as exc:  # the older copy keeps answering until FEED_KEEP; after that a visitor waits again
        logger.warning("MeteoAlarm refresh of %s failed: %s", url, exc)
    finally:
        with _feeds_lock:
            _refreshing.discard(url)


def _feed(url: str) -> tuple[float, dict[str, Any]]:
    """(when MeteoAlarm answered, the feed)."""
    now = time.time()
    with _feeds_lock:
        kept = _feeds.get(url)
        if kept and now - kept[0] < FEED_FRESH:
            return kept
        if kept and now - kept[0] < FEED_KEEP:
            if url not in _refreshing:
                _refreshing.add(url)
                threading.Thread(target=_refresh, args=(url,), name="meteoalarm-refresh", daemon=True).start()
            return kept
    copy = _download(url)
    _keep(url, copy)
    return copy


def europe(country: str, lat: float, lon: float, names: Iterable[str | None] = ()) -> dict[str, Any]:
    """*names* (town, county/department, region) match warnings that list areas by name only."""
    slug = EU_SLUGS[country]
    wanted = [n for n in (_area_key(x) for x in names) if len(n) >= 3]
    named: list[dict[str, Any]] = []
    unmapped = 0  # active warnings with no map area: a spot can't be called clear while any exist
    fetched_at, data = _feed(METEOALARM.format(slug=slug))
    exact: list[dict[str, Any]] = []
    big: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    has_shapes = False
    for w in data.get("warnings") or []:
        alert = w.get("alert") or {}
        if alert.get("msgType") == "Cancel":
            continue
        info = _info_en(alert)
        if not info or _expired(info.get("expires")):
            continue
        rank, colour = _level(info)
        if rank <= 1:
            continue
        counts[colour] = counts.get(colour, 0) + 1
        areas = info.get("area") or []
        has_shapes = has_shapes or any(a.get("polygon") for a in areas)
        if not any(a.get("polygon") for a in areas):
            unmapped += 1
        descs = [str(a.get("areaDesc") or "") for a in areas]
        hits = [d for d in descs if wanted and _names_in(d, wanted)]
        event = str(info.get("event") or info.get("headline") or "Warning")
        entry = {"event": event[:1].upper() + event[1:],  # DWD's are lower case: "gale-force gusts"
                 "level": colour,
                 "severity": info.get("severity"), "ends": info.get("expires"), "starts": info.get("onset"),
                 "issued_by": info.get("senderName"),
                 "regions": (hits + [d for d in descs if d not in hits])[:REGIONS_KEPT]}
        if len(descs) > REGIONS_KEPT:
            entry["more_regions"] = len(descs) - REGIONS_KEPT
        text = str(info.get("description") or "").strip()
        full = {**entry, "headline": info.get("headline"), "description": text[:TEXT_LIMIT],
                "instruction": str(info.get("instruction") or "").strip()}
        if any(geo.contains(_polygon(poly), lon, lat) for a in areas for poly in (a.get("polygon") or [])):
            exact.append(full)
        elif hits and not any(a.get("polygon") for a in areas):
            named.append({**full, "matched_by": "region name"})
        elif rank >= 3:
            big.append(entry)
    big.sort(key=lambda e: 0 if e["level"] == "red" else 1)
    return {"exact_matches": exact, "region_name_matches": named, "orange_and_red_in_country": big[:12],
            "country_counts": counts,
            "has_shapes": has_shapes, "exact": unmapped == 0,
            "age_min": max(0, round((time.time() - fetched_at) / 60)),
            "country": slug.replace("-", " ").title(), "source": "MeteoAlarm (EUMETNET)"}
