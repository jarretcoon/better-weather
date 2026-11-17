"""The Iowa Environmental Mesonet's copy of NWS warnings: the backup when api.weather.gov doesn't answer.

On 2026-11-17 the NWS's alerts API returned 503 from its own edge for hours: pushes stopped and every card said
alerts couldn't be checked. Iowa State's mesonet keeps a near-real-time copy of every NWS warning, watch and
advisory (VTEC) from every office. This turns its answer for one spot into the NWS API's own shape — the same
VTEC codes, so a warning keeps the key it was pushed under when the NWS comes back — with what it lacks said
plainly: the full text of each (a link to it instead) and the few products that aren't VTEC (special weather
statements). Only the products in effect now are kept: the spot's events joined with the live list.
"""

from __future__ import annotations

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
_GONE = {"CAN", "EXP", "UPG"}   # an event's last action that ends it
_LOOKBACK_DAYS = 45              # long-running warnings (river floods) were issued weeks ago
# The VTEC office of the NWS offices outside the lower 48 (the mesonet calls every office K + its 3 letters).
_OFFICE = {"AFC": "PAFC", "AFG": "PAFG", "AJK": "PAJK", "HFO": "PHFO", "GUM": "PGUM", "SJU": "TJSJ",
           "PPG": "NSTU"}


def office(wfo: str) -> str:
    """The 4-letter office in the NWS's own VTEC codes for a 3-letter office id."""
    wfo = str(wfo or "").upper()
    return _OFFICE.get(wfo, f"K{wfo}")


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

    Raises FetchError when it can't say — including an answer that doesn't look right (nothing in effect in the
    whole country is never true) — so a caller never takes "couldn't check" for "no alerts"."""
    now = now_utc()
    spot = http.get_json(f"{BASE}/json/vtec_events_bypoint.py",
                         {"lat": round(lat, 3), "lon": round(lon, 3),
                          "sdate": (now - timedelta(days=_LOOKBACK_DAYS)).date().isoformat(),
                          "edate": (now + timedelta(days=1)).date().isoformat()},
                         ttl=TTL, max_stale=15 * 60)
    live = http.get_json(f"{BASE}/api/1/vtec/events_status.json", ttl=TTL, max_stale=15 * 60)
    rows = live.data.get("data") if isinstance(live.data, dict) else None
    events = spot.data.get("events") if isinstance(spot.data, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(events, list):
        raise http.FetchError(spot.url, "the mesonet's answer didn't look right")
    status: dict[tuple[Any, ...], str] = {}
    for r in rows:
        if isinstance(r, dict):
            status[(r.get("wfo"), r.get("phenomena"), r.get("significance"), r.get("eventid"), r.get("year"))] = \
                str(r.get("status") or "")
    features: dict[str, dict[str, Any]] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        issued, ends = _when(e.get("issue")), _when(e.get("expire"))
        key = (e.get("wfo"), e.get("phenomena"), e.get("significance"), e.get("eventid"), issued and issued.year)
        action = status.get(key)
        if not action or action in _GONE or (ends and ends <= now):
            continue
        try:
            etn = int(str(e.get("eventid") or ""))
        except (TypeError, ValueError):
            continue
        code = f"{office(str(e.get('wfo') or ''))}.{e.get('phenomena')}.{e.get('significance')}.{etn:04d}"
        ident = f"iem:{code}.{issued.year if issued else ''}"
        name = str(e.get("name") or "Alert")
        web = f"{BASE}{e.get('url') or '/vtec/'}"
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
