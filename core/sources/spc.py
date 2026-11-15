"""Storm Prediction Center convective outlooks, checked against the user's exact location."""

from __future__ import annotations

from typing import Any

from .. import geo, http

BASE = "https://www.spc.noaa.gov/products/outlook"

CATEGORIES = {
    "TSTM": (1, "General thunderstorms (no severe risk)"),
    "MRGL": (2, "Marginal risk (1 of 5)"),
    "SLGT": (3, "Slight risk (2 of 5)"),
    "ENH": (4, "Enhanced risk (3 of 5)"),
    "MDT": (5, "Moderate risk (4 of 5)"),
    "HIGH": (6, "High risk (5 of 5)"),
}
HAZARDS = {"torn": "tornado", "wind": "wind", "hail": "hail"}


def _outlook(day: int, kind: str) -> http.Fetched:
    return http.get_json(f"{BASE}/day{day}otlk_{kind}.nolyr.geojson",
                         ttl=http.TTL_OUTLOOK, max_stale=6 * 3600)


def _containing(data: Any, lat: float, lon: float) -> list[dict[str, Any]]:
    hits = []
    for feature in (data or {}).get("features") or []:
        if geo.contains(feature.get("geometry"), lon, lat):
            hits.append(feature.get("properties") or {})
    return hits


def _prob(label: Any) -> float | None:
    try:
        value = float(str(label))
    except (TypeError, ValueError):
        return None
    return value * 100 if value <= 1 else value


def risk_at(lat: float, lon: float) -> dict[str, Any]:
    """Day 1-3 categorical risk at the point, plus day-1 tornado/wind/hail odds."""
    days: list[dict[str, Any]] = []
    notes: list[str] = []
    for day in (1, 2, 3):
        try:
            fetched = _outlook(day, "cat")
        except http.FetchError as exc:
            notes.append(f"Day {day} outlook unavailable ({exc})")
            continue
        features = (fetched.data or {}).get("features") or []
        valid = (features[0].get("properties") or {}) if features else {}
        best: tuple[int, str, str] = (0, "NONE", "No thunderstorms expected")
        for props in _containing(fetched.data, lat, lon):
            label = str(props.get("LABEL") or "").upper()
            rank, text = CATEGORIES.get(label, (0, str(props.get("LABEL2") or label)))
            if rank > best[0]:
                best = (rank, label, text)
        entry: dict[str, Any] = {
            "day": day,
            "category": best[1],
            "risk": best[2],
            "level": best[0],
            "valid_from": valid.get("VALID_ISO"),
            "valid_to": valid.get("EXPIRE_ISO"),
            "issued": valid.get("ISSUE_ISO"),
            "stale_copy": fetched.stale,
        }
        if day == 1:
            hazards: dict[str, Any] = {}
            for code, name in HAZARDS.items():
                try:
                    prob_fetch = _outlook(1, code)
                except http.FetchError:
                    hazards[name] = "unavailable"
                    continue
                pct = 0.0
                flags: list[str] = []
                for props in _containing(prob_fetch.data, lat, lon):
                    value = _prob(props.get("LABEL"))
                    if value is not None:
                        pct = max(pct, value)
                    else:  # hatched "significant" or intensity areas
                        flags.append(str(props.get("LABEL2") or props.get("LABEL") or "").strip())
                hazards[name] = {"chance_within_25_miles_pct": round(pct),
                                 "intensity": [f for f in flags if f]}
            entry["hazards"] = hazards
        days.append(entry)
    return {"days": days, "notes": notes, "source": "NOAA Storm Prediction Center"}
