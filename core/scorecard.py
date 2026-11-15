"""Forecast scorecard: which forecast has been most accurate *here*?

Once a day it saves the next 3 days' highs, lows and precipitation from the NWS and five global
models, and checks earlier saved forecasts against what actually happened (official station
records in the US, ERA5 elsewhere). The report ranks the sources by average miss. It needs about two
weeks of history before the ranking means much; it says so until then."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, timedelta
from statistics import mean
from typing import Any

from . import climate, http, settings, units
from .clock import now_utc
from .places import Place
from .sources import acis, nws

MODELS = {"best_match": "Open-Meteo blend", "gfs_seamless": "GFS (US)", "ecmwf_ifs025": "ECMWF (Europe)",
          "icon_seamless": "ICON (Germany)", "gem_seamless": "GEM (Canada)"}
NWS = "nws"
LEADS = (1, 2, 3)
MIN_SAMPLES = 10
WET_MM = 0.25
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS predictions (
        target TEXT NOT NULL, lead INTEGER NOT NULL, source TEXT NOT NULL,
        high_c REAL, low_c REAL, precip_mm REAL, made TEXT NOT NULL,
        PRIMARY KEY (target, lead, source))""",
    """CREATE TABLE IF NOT EXISTS observed (
        target TEXT PRIMARY KEY, high_c REAL, low_c REAL, precip_mm REAL, origin TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)""",
)

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        try:
            from plugins.plugin_storage import plugin_db  # type: ignore[import-not-found]

            conn = plugin_db(settings.PLUGIN_NAME, "scorecard.db")
        except Exception:
            directory = settings.data_dir()
            conn = sqlite3.connect(str(directory / "scorecard.db") if directory else ":memory:",
                                   check_same_thread=False, timeout=5)
        for stmt in _SCHEMA:
            conn.execute(stmt)
        conn.commit()
        _conn = conn
    return _conn


def reset_for_tests() -> None:
    global _conn
    _conn = None


def _meta(key: str, value: str | None = None) -> str | None:
    db = _db()
    if value is not None:
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        db.commit()
        return value
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _today(place: Place) -> date:
    return now_utc().astimezone(units.zone(place.timezone)).date()


# --- collecting ---------------------------------------------------------------------------------

def _model_forecasts(place: Place) -> dict[str, dict[str, dict[str, float | None]]]:
    fetched = http.get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": round(place.lat, 2), "longitude": round(place.lon, 2),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum", "models": ",".join(MODELS),
        "forecast_days": 4, "timezone": "auto"}, ttl=3 * 3600, max_stale=12 * 3600)
    daily = (fetched.data or {}).get("daily") or {}
    out: dict[str, dict[str, dict[str, float | None]]] = {}
    for model in MODELS:
        per_day = {}
        for i, day in enumerate(daily.get("time") or []):
            def pick(var: str, idx: int = i, m: str = model) -> float | None:
                values = daily.get(f"{var}_{m}") or []
                return units.num(values[idx]) if idx < len(values) else None
            per_day[day] = {"high_c": pick("temperature_2m_max"), "low_c": pick("temperature_2m_min"),
                            "precip_mm": pick("precipitation_sum")}
        out[model] = per_day
    return out


def _nws_forecast(place: Place) -> dict[str, dict[str, float | None]]:
    """NWS highs (daytime periods) and lows (a night's low is credited to the next morning's date)."""
    point = nws.point(place.lat, place.lon) if place.maybe_us else None
    if not point:
        return {}
    periods = ((nws.forecast(point).data or {}).get("properties") or {}).get("periods") or []
    out: dict[str, dict[str, float | None]] = {}
    for p in periods:
        start = units.parse_time(p.get("startTime"))
        temp = units.num(p.get("temperature"))
        if start is None or temp is None:
            continue
        temp_c = (temp - 32) * 5 / 9 if str(p.get("temperatureUnit") or "F").upper() == "F" else temp
        if p.get("isDaytime"):
            out.setdefault(start.date().isoformat(), {})["high_c"] = temp_c
        else:
            day = start.date() + timedelta(days=1) if start.hour >= 12 else start.date()
            out.setdefault(day.isoformat(), {})["low_c"] = temp_c
    return out


def collect(place: Place) -> int:
    """Save today's forecasts for the next 3 days (once per local day). Returns rows written."""
    today = _today(place)
    if _meta("collected_day") == today.isoformat():
        return 0
    rows = []
    for model, per_day in _model_forecasts(place).items():
        for lead in LEADS:
            target = (today + timedelta(days=lead)).isoformat()
            v = per_day.get(target)
            if v and v.get("high_c") is not None:
                rows.append((target, lead, model, v["high_c"], v["low_c"], v["precip_mm"], today.isoformat()))
    try:
        official = _nws_forecast(place)
    except http.FetchError:
        official = {}
    for lead in LEADS:
        target = (today + timedelta(days=lead)).isoformat()
        v = official.get(target) or {}
        if v.get("high_c") is not None or v.get("low_c") is not None:
            rows.append((target, lead, NWS, v.get("high_c"), v.get("low_c"), None, today.isoformat()))
    with _lock:
        db = _db()
        db.executemany("INSERT OR REPLACE INTO predictions VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        db.commit()
    _meta("collected_day", today.isoformat())
    _meta("place", f"{place.name}|{place.lat:.3f}|{place.lon:.3f}")
    return len(rows)


def verify(place: Place) -> int:
    """Fill in what actually happened for past target dates. Returns days added."""
    today = _today(place)
    with _lock:
        pending = [r[0] for r in _db().execute(
            "SELECT DISTINCT target FROM predictions WHERE target < ? AND target NOT IN (SELECT target FROM observed)",
            (today.isoformat(),)).fetchall()]
    if not pending:
        return 0
    start, end = date.fromisoformat(min(pending)), date.fromisoformat(max(pending))
    found: dict[str, tuple[float | None, float | None, float | None, str]] = {}
    if place.maybe_us:
        try:
            point = nws.point(place.lat, place.lon)
            station = acis.station_for([s["id"] for s in nws.stations(point)], today) if point else None
            if station:
                for day, v in acis.daily(station[0], start, end).items():
                    hi_f, lo_f, pr_in = v.get("high_f"), v.get("low_f"), v.get("precip_in")
                    if hi_f is not None and lo_f is not None:
                        found[day] = ((hi_f - 32) * 5 / 9, (lo_f - 32) * 5 / 9,
                                      pr_in * 25.4 if pr_in is not None else None, f"station {station[0]}")
        except http.FetchError:
            pass
    missing = [d for d in pending if d not in found and date.fromisoformat(d) < today - timedelta(days=1)]
    if missing:
        try:
            model = climate._model_days(place.lat, place.lon, date.fromisoformat(min(missing)),
                                        date.fromisoformat(max(missing)), today)
            for day in missing:
                m = model.get(day) or {}
                hi, lo = units.num(m.get("temperature_2m_max")), units.num(m.get("temperature_2m_min"))
                if hi is not None and lo is not None:
                    found[day] = (hi, lo, units.num(m.get("precipitation_sum")), "ERA5")
        except http.FetchError:
            pass
    with _lock:
        db = _db()
        db.executemany("INSERT OR REPLACE INTO observed VALUES (?, ?, ?, ?, ?)",
                       [(d, *v) for d, v in found.items() if d in pending])
        db.commit()
    return len([d for d in found if d in pending])


def update(place: Place) -> str:
    """Daily job: collect + verify. Safe to call often."""
    added = collect(place)
    checked = verify(place)
    return f"saved {added} forecasts, checked {checked} past days"


# --- report ---------------------------------------------------------------------------------------

def report(place: Place, units_: str, days: int = 30) -> dict[str, Any]:
    today = _today(place)
    since = (today - timedelta(days=max(7, min(int(days or 30), 365)))).isoformat()
    with _lock:
        rows = _db().execute(
            """SELECT p.source, p.lead, p.high_c, p.low_c, p.precip_mm, o.high_c, o.low_c, o.precip_mm
               FROM predictions p JOIN observed o ON o.target = p.target WHERE p.target >= ?""",
            (since,)).fetchall()
        first = _db().execute("SELECT MIN(made) FROM predictions").fetchone()[0]
    scale = 1.0 if units_ == "metric" else 1.8
    unit = "°C" if units_ == "metric" else "°F"
    stats: dict[tuple[str, int], dict[str, list[float]]] = {}
    for source, lead, ph, pl, pp, oh, ol, op in rows:
        s = stats.setdefault((source, lead), {"hi": [], "lo": [], "bias": [], "rain": []})
        if ph is not None and oh is not None:
            s["hi"].append(abs(ph - oh) * scale)
            s["bias"].append((ph - oh) * scale)
        if pl is not None and ol is not None:
            s["lo"].append(abs(pl - ol) * scale)
        if pp is not None and op is not None:
            s["rain"].append(1.0 if (pp >= WET_MM) == (op >= WET_MM) else 0.0)
    table = []
    for (source, lead), s in sorted(stats.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        misses = s["hi"] + s["lo"]
        if not misses:
            continue
        table.append({k: v for k, v in {
            "source": "NWS official forecast" if source == NWS else MODELS.get(source, source),
            "days_ahead": lead, "samples": len(s["hi"]),
            "avg_miss": f"{mean(misses):.1f}{unit}",
            "high_miss": f"{mean(s['hi']):.1f}{unit}" if s["hi"] else None,
            "low_miss": f"{mean(s['lo']):.1f}{unit}" if s["lo"] else None,
            "runs_warm_or_cold": (f"{'warm' if mean(s['bias']) > 0 else 'cold'} by {abs(mean(s['bias'])):.1f}{unit}"
                                  if s["bias"] and abs(mean(s["bias"])) >= 0.5 * scale else None),
            "rain_yes_no_right_pct": round(100 * mean(s["rain"])) if s["rain"] else None,
            "_score": mean(misses)}.items() if v is not None})
    next_day = [r for r in table if r["days_ahead"] == 1 and r["samples"] >= MIN_SAMPLES]
    best = min(next_day, key=lambda r: r["_score"]) if next_day else None
    for r in table:
        r.pop("_score", None)
    result: dict[str, Any] = {
        "place": place.public(), "window_days": days, "collecting_since": first,
        "ranking": sorted(table, key=lambda r: (r["days_ahead"], float(r["avg_miss"].rstrip("°CF")))),
    }
    if best:
        result["most_accurate_tomorrow"] = best["source"]
    else:
        result["notes"] = [f"Still learning: needs about {MIN_SAMPLES} checked days per source "
                           "before naming a winner."]
    return result


def best_source(place: Place) -> str | None:
    """Name of the most accurate next-day source here, once there's enough history; else None."""
    try:
        return report(place, "us", 30).get("most_accurate_tomorrow")
    except Exception:
        return None
