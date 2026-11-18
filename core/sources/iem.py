"""The Iowa Environmental Mesonet's copy of NWS warnings: the backup when api.weather.gov doesn't answer.

On 2026-11-17 the NWS's alerts API returned 503 from its own edge for hours: pushes stopped and every card said
alerts couldn't be checked. Iowa State's mesonet keeps a near-real-time copy of every NWS warning, watch and
advisory (VTEC) from every office. This turns its answer for one spot into the NWS API's own shape — the same
VTEC codes, so a warning keeps the key it was pushed under when the NWS comes back — with what it lacks said
plainly: the full text of each (a link to it instead) and the few products that aren't VTEC (special weather
statements). Only the products in effect now are kept: the spot's events joined with the live list.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import http
from ..clock import now_utc

BASE = "https://mesonet.agron.iastate.edu"
SOURCE = "NWS warnings via Iowa State's mesonet"
LIMITS = ("The National Weather Service's alert service didn't answer, so these come from the Iowa Environmental "
          "Mesonet's copy of its warnings, watches and advisories: special weather statements and the full text "
          "of each aren't in it.")
TTL = 120
_GONE = {"CAN", "EXP", "UPG"}    # an event's last action that ends it (in every zone it covers)
_LOOKBACK_DAYS = 45              # if the live list can't say how far back its oldest event goes
_AHEAD_DAYS = 10                 # at least this far ahead: events that start later than the live list says
_STALE_AFTER = timedelta(hours=2)  # newest update anywhere in the country older than this: the copy has stalled
# Storm-based warnings drawn as polygons: the mesonet's by-point list matches them by county, so they need a hit in
# its polygon lookup too — the NWS matches them by polygon. Flood advisories are polygons as well, and a wet day
# brings a dozen per county (Homestead FL, 2026-11-18: 4 by county, 1 from the NWS). (Flood warnings stay matched
# by county: over-warning is the safe side, and they don't flap.)
_POLYGON = {("TO", "W"), ("SV", "W"), ("FF", "W"), ("EW", "W"), ("SQ", "W"), ("MA", "W"), ("DS", "W"), ("FA", "Y")}
# The VTEC office of offices outside the lower 48, when the event's own link doesn't give it (the mesonet's links do;
# its 3-letter ids don't always match: San Juan is JSJ there).
_OFFICE = {"AFC": "PAFC", "AFG": "PAFG", "AJK": "PAJK", "HFO": "PHFO", "GUM": "PGUM", "SJU": "TJSJ", "JSJ": "TJSJ",
           "AAQ": "PAAQ", "HEB": "PHEB", "PPG": "NSTU"}
_LINK_WFO = re.compile(r"wfo=([A-Z]{4})")


def office(wfo: str, link: str = "") -> str:
    """The 4-letter office in the NWS's own VTEC codes: from the event's link, else from its 3-letter id."""
    found = _LINK_WFO.search(link or "")
    if found:
        return found.group(1)
    wfo = str(wfo or "").upper()
    return _OFFICE.get(wfo, f"K{wfo}")


def _year(link: str, issued: datetime | None) -> int | None:
    """The event's VTEC year: from its link (its start time can fall in the next year), else its start."""
    found = re.search(r"year=(\d{4})", link or "")
    if found:
        return int(found.group(1))
    return issued.year if issued else None


def _when(text: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _stamp(when: datetime | None) -> str:
    return when.astimezone(UTC).strftime("%y%m%dT%H%MZ") if when else "000000T0000Z"


def is_copy(data: Any) -> bool:
    """True when an alerts answer is this copy, not the NWS's own."""
    return isinstance(data, dict) and data.get("_source") == SOURCE


def alerts_at(lat: float, lon: float) -> http.Fetched:
    """The NWS alerts in effect at the spot, from the mesonet, as an api.weather.gov `alerts/active` answer.

    Raises FetchError when it can't say — an answer that doesn't look right, a copy that has stalled, or a surprise in
    it — so a caller never takes "couldn't check" for "no alerts"."""
    try:
        return _read(lat, lon)
    except http.FetchError:
        raise
    except Exception as exc:   # a surprise in the mesonet's answer is "can't say", never a crash or "no alerts"
        raise http.FetchError(BASE, f"the mesonet's answer couldn't be read ({type(exc).__name__})") from exc


def _read(lat: float, lon: float) -> http.Fetched:
    now = now_utc()
    live = http.get_json(f"{BASE}/api/1/vtec/events_status.json", ttl=TTL, max_stale=15 * 60)
    rows = live.data.get("data") if isinstance(live.data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise http.FetchError(live.url, "the mesonet's answer didn't look right")
    newest = max((t for t in (_when(r.get("updated")) for r in rows if isinstance(r, dict)) if t), default=None)
    if newest is None or now - newest > _STALE_AFTER:
        raise http.FetchError(live.url, "the mesonet's copy hasn't been updated lately")
    # in effect: any of an event's rows (one per group of zones) is still going
    active = {(r.get("wfo"), r.get("phenomena"), r.get("significance"), r.get("eventid"), r.get("year"))
              for r in rows if isinstance(r, dict) and str(r.get("status") or "") not in _GONE}
    starts = [t for t in (_when(r.get("issue")) for r in rows if isinstance(r, dict)) if t]
    oldest = min(starts, default=now - timedelta(days=_LOOKBACK_DAYS))
    # The by-point list picks events by their START, and its end date is exclusive: it must reach past the latest
    # start of anything in effect (a watch can start days out), or an alert starting after the next UTC midnight is
    # missed — a false "has ended" push during an NWS outage, then a second "new" push when the NWS is back.
    latest = max(starts, default=now)
    window = {"lat": round(lat, 3), "lon": round(lon, 3),
              "sdate": (min(oldest, now) - timedelta(days=1)).date().isoformat(),
              "edate": (max(latest + timedelta(days=2), now + timedelta(days=_AHEAD_DAYS))).date().isoformat()}
    spot = http.get_json(f"{BASE}/json/vtec_events_bypoint.py", window, ttl=TTL, max_stale=15 * 60)
    events = spot.data.get("events") if isinstance(spot.data, dict) else None
    if not isinstance(events, list):
        raise http.FetchError(spot.url, "the mesonet's answer didn't look right")
    in_polygon = _polygon_hits(window, spot.fetched_at)
    features: dict[str, dict[str, Any]] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        link = str(e.get("url") or "")
        issued, ends = _when(e.get("issue")), _when(e.get("expire"))
        year = _year(link, issued)
        ph, sig, eid = e.get("phenomena"), e.get("significance"), e.get("eventid")
        if (e.get("wfo"), ph, sig, eid, year) not in active or (ends and ends <= now):
            continue
        if (ph, sig) in _POLYGON and in_polygon is not None and (e.get("wfo"), ph, sig, eid, year) not in in_polygon:
            continue   # a storm-based warning for this county, but its polygon misses the spot
        try:
            etn = int(str(eid or ""))
        except ValueError:
            continue   # one odd event doesn't cost the others
        code = f"{office(str(e.get('wfo') or ''), link)}.{ph}.{sig}.{etn:04d}"
        ident = f"iem:{code}.{year}"
        name = str(e.get("name") or "Alert")
        web = f"{BASE}{link or '/vtec/'}"
        action = "NEW" if issued and now - issued < timedelta(minutes=15) else "CON"
        features[ident] = {"geometry": None, "properties": {
            "id": ident,
            "event": name,
            "status": "Actual",
            "messageType": "Alert" if action == "NEW" else "Update",
            "senderName": f"NWS {e.get('wfo')}, copied by Iowa State's mesonet",
            "effective": e.get("issue"),
            "onset": e.get("issue"),
            "ends": e.get("expire"),
            "expires": e.get("expire"),
            # the NWS's own code for the event: a warning keeps its push key whichever service reported it
            "parameters": {"VTEC": [f"/O.{action}.{code}.{_stamp(issued)}-{_stamp(ends)}/"]},
            "description": "The National Weather Service's own alert service isn't answering right now, so this "
                           "comes from the Iowa Environmental Mesonet's copy: the times, not the full text. "
                           f"Full text: {web}",
            "web": web,
        }}
    stale = spot.stale or live.stale
    fetched_at = min(spot.fetched_at, live.fetched_at) if stale else max(spot.fetched_at, live.fetched_at)
    data = {"type": "FeatureCollection", "features": list(features.values()), "_source": SOURCE, "_limits": LIMITS}
    return http.Fetched(data, spot.url, fetched_at, stale, 200)


def _polygon_hits(window: dict[str, Any], events_at: float) -> set[tuple[Any, ...]] | None:
    """The storm-based warnings whose polygon covers the spot, over the same window as the events (None if the lookup
    failed or only an old copy is left: county matching then, which can over-warn but never misses one). Never older
    than the event list it filters: a warning newer than the polygons would be dropped."""
    url = f"{BASE}/json/sbw_by_point.py"
    try:
        found = http.get_json(url, window, ttl=TTL, max_stale=15 * 60)
        if not found.stale and found.fetched_at < events_at:
            found = http.get_json(url, window, ttl=0, max_stale=15 * 60)
    except http.FetchError:
        return None
    if found.stale:
        return None
    hits = found.data.get("sbws") if isinstance(found.data, dict) else None
    if not isinstance(hits, list):
        return None
    out = set()
    for h in hits:
        if isinstance(h, dict):
            out.add((h.get("wfo"), h.get("phenomena"), h.get("significance"), h.get("eventid"),
                     _year(str(h.get("url") or ""), _when(h.get("issue")))))
    return out
