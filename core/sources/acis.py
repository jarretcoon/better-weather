"""NOAA Regional Climate Centers' ACIS: official US station history, 1991-2020 normals and
calendar-day records (keyless). Major cities use "threaded" records that join a city's old
downtown station with today's airport station, going back to the 1870s in many places."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .. import http

BASE = "https://data.rcc-acis.org/StnData"


def _call(params: dict[str, Any], ttl: float) -> dict[str, Any]:
    query = {"params": json.dumps(params, separators=(",", ":"), sort_keys=True)}
    data = http.get_json(BASE, query, ttl=ttl, max_stale=30 * 24 * 3600).data or {}
    if not isinstance(data, dict) or data.get("error"):
        raise http.FetchError(BASE, str((data or {}).get("error") or "no data"))
    return data


def value(raw: Any) -> float | None:
    """ACIS values: numbers, 'M' missing, 'T' trace, sometimes a trailing flag letter ('0.12A')."""
    text = str(raw or "").strip()
    if not text or text == "M":
        return None
    if text == "T":
        return 0.0
    while text and text[-1].isalpha():
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def candidates(icao: str) -> list[str]:
    code = icao.strip().upper()
    if len(code) == 4 and code.startswith("K"):
        return [f"{code[1:]}thr", code[1:]]
    return [code]


def station_for(icaos: list[str], today: date) -> tuple[str, str] | None:
    """(ACIS station id, name) for the first nearby station ACIS has temperatures for."""
    probe_day = (today - timedelta(days=2)).isoformat()
    for icao in icaos[:3]:
        for sid in candidates(icao):
            try:
                data = _call({"sid": sid, "sdate": probe_day, "edate": probe_day, "elems": "maxt"},
                             ttl=30 * 24 * 3600)
            except http.FetchError:
                continue
            if data.get("data"):
                return sid, str((data.get("meta") or {}).get("name") or sid)
    return None


def daily(sid: str, start: date, end: date) -> dict[str, dict[str, float | None]]:
    """Observed high/low (°F), precip and snow (in), and the 1991-2020 normal high/low, by date."""
    data = _call({"sid": sid, "sdate": start.isoformat(), "edate": end.isoformat(),
                  "elems": [{"name": "maxt"}, {"name": "mint"}, {"name": "pcpn"}, {"name": "snow"},
                            {"name": "maxt", "normal": "1"}, {"name": "mint", "normal": "1"}]},
                 ttl=3 * 3600)
    out = {}
    for row in data.get("data") or []:
        out[row[0]] = {"high_f": value(row[1]), "low_f": value(row[2]), "precip_in": value(row[3]),
                       "snow_in": value(row[4]), "normal_high_f": value(row[5]), "normal_low_f": value(row[6])}
    return out


def records(sid: str, day: date, *, current_year: int) -> dict[str, Any]:
    """Record high and low for this calendar day, with years, over every completed year on file
    (the current, unfinished year is left out so today can't "set a record" that isn't final)."""
    if day.month == 2 and day.day == 29:
        return {}
    last = f"{current_year - 1}-{day.month:02d}-{day.day:02d}"
    data = _call({"sid": sid, "sdate": f"1850-{day.month:02d}-{day.day:02d}", "edate": last,
                  "elems": [{"name": "maxt", "interval": [1, 0, 0]}, {"name": "mint", "interval": [1, 0, 0]}]},
                 ttl=7 * 24 * 3600)
    highs, lows = [], []
    for row in data.get("data") or []:
        year = int(str(row[0])[:4])
        if year >= current_year:
            continue
        hi, lo = value(row[1]), value(row[2])
        if hi is not None:
            highs.append((hi, year))
        if lo is not None:
            lows.append((lo, year))
    if not highs or not lows:
        return {}
    top = max(highs, key=lambda x: (x[0], x[1]))
    bottom = min(lows, key=lambda x: (x[0], -x[1]))
    return {"record_high_f": top[0], "record_high_year": top[1], "record_low_f": bottom[0],
            "record_low_year": bottom[1], "since": min(y for _, y in highs)}
