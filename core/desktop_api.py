"""What the Hermes Desktop status-bar chip shows: home temperature, sky and any alerts."""

from __future__ import annotations

import threading
import time
from typing import Any

from . import http, places, report, settings

CACHE_SECONDS = 5 * 60
_lock = threading.Lock()
_cached: tuple[float, dict[str, Any]] | None = None


def now_payload(force: bool = False) -> dict[str, Any]:
    global _cached
    with _lock:
        if not force and _cached and time.time() - _cached[0] < CACHE_SECONDS:
            return _cached[1]
    s = settings.current()
    try:
        place = places.resolve("home")
        r = report.now(place, s.units, s.clock)
    except places.PlaceError as exc:
        return {"error": str(exc), "hint": exc.hint}
    except http.FetchError as exc:
        return {"error": f"Weather service unavailable: {exc}"}
    current = r.get("current") or {}
    alerts = r.get("alerts") or []
    payload = {
        "place": r["place"]["name"],
        "temp": current.get("temp"),
        "sky": current.get("sky"),
        "as_of": r.get("as_of"),
        "alerts": [a["event"] for a in alerts],
        "life_threatening": any(a.get("life_threatening") for a in alerts),
        "summary": str(r.get("card") or "").replace("**", ""),
    }
    with _lock:
        _cached = (time.time(), payload)
    return payload
