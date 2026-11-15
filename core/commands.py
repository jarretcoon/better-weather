"""/weather (in any chat, no AI involved) and `hermes better-weather …` (terminal)."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from typing import Any

from . import cache, http, places, report, settings
from .sources import nws, openmeteo, spc

USAGE = ("/weather [place] · /weather forecast [place] · /weather hourly [place] · "
         "/weather alerts [place] · /weather severe [place]")


def _forecast_text(r: dict[str, Any]) -> str:
    lines = [f"{r['place']['name']} forecast"]
    for p in r.get("periods") or []:
        rain = f", {p['precip_chance_pct']}% precip" if p.get("precip_chance_pct") else ""
        lines.append(f"{p['name']}: {p.get('sky')}, {p.get('high_or_low')} {p.get('temp')}°{rain}")
    for d in (r.get("days") or []) + (r.get("extended_days") or []):
        rain = f", {d['precip_chance_pct']}% precip" if d.get("precip_chance_pct") else ""
        lines.append(f"{d['day']}: {d.get('sky')}, {d.get('high')}°/{d.get('low')}°{rain}")
    for h in r.get("hours") or []:
        rain = f", {h['precip_chance_pct']}%" if h.get("precip_chance_pct") else ""
        lines.append(f"{h['time']}: {h.get('temp')}° {h.get('sky') or ''}{rain}".rstrip())
    lines.extend(n for n in r.get("notes") or [] if n.startswith("⚠️"))
    return "\n".join(lines)


def _alerts_text(r: dict[str, Any]) -> str:
    if not r.get("alerts"):
        return r.get("summary") or "\n".join(r.get("notes") or ["No active alerts."])
    lines = [f"{r['count']} active alert(s) for {r.get('where')}"]
    for a in r["alerts"]:
        mark = "🚨" if a.get("life_threatening") else "⚠️"
        lines.append(f"{mark} {a['event']}" + (f" until {a['ends']}" if a.get("ends") else ""))
        if a.get("headline"):
            lines.append(f"   {a['headline']}")
    return "\n".join(lines)


def run(raw_args: str) -> str:
    """Shared by the slash command and the CLI. Returns plain text."""
    words = (raw_args or "").strip().split(maxsplit=1)
    sub = words[0].lower() if words else ""
    rest = words[1] if len(words) > 1 else ""
    s = settings.current()
    try:
        if sub in {"help", "-h", "--help", "?"}:
            return USAGE
        if sub == "forecast":
            return _forecast_text(report.forecast(places.resolve(rest), s.units, s.clock, days=7))
        if sub == "hourly":
            return _forecast_text(report.forecast(places.resolve(rest), s.units, s.clock,
                                                  hourly=True, hours=12))
        if sub == "alerts":
            return _alerts_text(report.alerts(places.resolve(rest), None, s.clock))
        if sub == "severe":
            r = report.severe(places.resolve(rest), s.units, s.clock)
            return f"{r['place']['name']} severe weather\n{r.get('summary', '')}"
        return report.now(places.resolve(raw_args), s.units, s.clock)["summary"]
    except places.PlaceError as exc:
        return f"{exc}" + (f"\n{exc.hint}" if exc.hint else "") + f"\nUsage: {USAGE}"
    except http.FetchError as exc:
        return f"Weather service unavailable right now: {exc}"


def slash_command(raw_args: str = "", **_kwargs: Any) -> str:
    return run(raw_args)


# --- terminal: hermes better-weather … --------------------------------------------------------

def _check(name: str, fn: Callable[[], Any]) -> dict[str, Any]:
    start = time.monotonic()
    try:
        detail = fn()
        return {"check": name, "ok": True, "ms": round((time.monotonic() - start) * 1000),
                "detail": detail}
    except Exception as exc:
        return {"check": name, "ok": False, "ms": round((time.monotonic() - start) * 1000),
                "detail": f"{type(exc).__name__}: {exc}"}


def doctor() -> dict[str, Any]:
    s = settings.current()
    try:
        home = places.resolve("home") if s.home else None
    except places.PlaceError:
        home = None
    lat, lon = (home.lat, home.lon) if home else (41.59, -93.62)

    def fresh(url: str, params: dict[str, Any] | None = None) -> http.Fetched:
        return http.get_json(url, params, ttl=0, max_stale=0, allow_404=True)

    checks = [
        _check("NWS points", lambda: fresh(f"{nws.BASE}/points/{lat:.3f},{lon:.3f}").status),
        _check("NWS alerts", lambda: len((fresh(f"{nws.BASE}/alerts/active",
                                                {"point": f"{lat:.3f},{lon:.3f}"}).data or {})
                                         .get("features", []))),
        _check("Open-Meteo forecast", lambda: fresh(openmeteo.FORECAST_URL, {
            "latitude": round(lat, 2), "longitude": round(lon, 2), "current": "temperature_2m"}).status),
        _check("Open-Meteo place search", lambda: len(openmeteo.geocode("London"))),
        _check("SPC day 1 outlook", lambda: len((fresh(f"{spc.BASE}/day1otlk_cat.nolyr.geojson").data or {})
                                                .get("features", []))),
    ]
    return {
        "ok": all(c["ok"] for c in checks),
        "home": home.public() if home else (s.home or "(not set)"),
        "units": s.units,
        "clock": s.clock,
        "cache": cache.get_cache().stats(),
        "checks": checks,
    }


def setup_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="bw_command")
    for name, help_text in (("now", "Weather now"), ("forecast", "7-day forecast"),
                            ("hourly", "Next 12 hours"), ("alerts", "Active alerts"),
                            ("severe", "Severe-storm risk")):
        sp = subs.add_parser(name, help=help_text)
        sp.add_argument("place", nargs="*", help="Place (default: home)")
    subs.add_parser("doctor", help="Check every weather source and the settings")
    subs.add_parser("clear-cache", help="Forget cached responses")
    parser.set_defaults(func=handle_cli)


def handle_cli(args: argparse.Namespace) -> int:
    command = getattr(args, "bw_command", None) or "now"
    if command == "doctor":
        result = doctor()
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["ok"] else 1
    if command == "clear-cache":
        cache.get_cache().clear()
        print("Cache cleared.")
        return 0
    place = " ".join(getattr(args, "place", None) or [])
    prefix = "" if command == "now" else command + " "
    print(run(prefix + place))
    return 0
