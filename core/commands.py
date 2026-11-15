"""/weather (in any chat, no AI involved) and `hermes better-weather …` (terminal)."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import cache, climate, http, places, report, settings, visuals, watch
from .sources import acis, nws, openmeteo, spc
from .sources import radar as radar_src
from .sources import satellite as sat_src

USAGE = ("/weather [place] · /weather forecast [place] · /weather hourly [place] · "
         "/weather alerts [place] · /weather severe [place] · /weather radar [loop] [place] · "
         "/weather satellite [loop] [place] · /weather air [place] · /weather history [YYYY-MM-DD]")


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
        if sub in {"radar", "satellite"}:
            loop = rest.lower().startswith("loop")
            where = rest[4:].strip() if loop else rest
            if sub == "radar":
                r = visuals.radar(places.resolve(where), loop=loop, clock=s.clock)
            else:
                r = visuals.satellite(places.resolve(where), loop=loop, clock=s.clock)
            if "error" in r:
                return str(r["error"])
            when = f", {r['image_time']}" if r.get("image_time") else ""
            notes = "".join(f"\n{n}" for n in r.get("notes") or [])
            return f"{r['source']}{when}{notes}\n{r['media']}"
        if sub == "air":
            return _air_text(climate.air(places.resolve(rest), s.clock))
        if sub == "history":
            day, _, where = rest.partition(" ")
            try:
                r = climate.history(places.resolve(where), day or "yesterday", None, s.units)
            except ValueError as exc:
                return str(exc)
            return _history_text(r)
        return report.now(places.resolve(raw_args), s.units, s.clock)["summary"]
    except places.PlaceError as exc:
        return f"{exc}" + (f"\n{exc.hint}" if exc.hint else "") + f"\nUsage: {USAGE}"
    except http.FetchError as exc:
        return f"Weather service unavailable right now: {exc}"


def _air_text(r: dict[str, Any]) -> str:
    aq = r.get("air_quality") or {}
    lines = [f"{r['place']['name']} air"]
    if aq.get("index") is not None:
        main = f", mostly {aq['main_pollutant']}" if aq.get("main_pollutant") else ""
        lines.append(f"{aq.get('scale')} {aq['index']}: {aq.get('category')}{main}")
    worst = r.get("next_24h_worst_air")
    if worst:
        lines.append(f"Worst next 24 h: {worst['value']} ({worst.get('category')}) around {worst['at']}")
    uv = r.get("next_24h_peak_uv")
    if uv:
        lines.append(f"UV peaks at {uv['value']} ({uv.get('level')}) around {uv['at']}")
    if r.get("pollen"):
        active = ", ".join(f"{k} {v}" for k, v in r["pollen"].items() if v and v != "none")
        lines.append(f"Pollen: {active or 'none'}")
    lines.extend(n for n in r.get("notes") or [] if "smoke" in n)
    return "\n".join(lines)


def _history_text(r: dict[str, Any]) -> str:
    lines = [f"{r['place']['name']}, {r['from']}" + (f" to {r['to']}" if r["to"] != r["from"] else "")]
    for d in r.get("days") or []:
        bits = [f"high {d.get('high')}°", f"low {d.get('low')}°"]
        if d.get("precip"):
            bits.append(f"precip {d['precip']}")
        if d.get("snow"):
            bits.append(f"snow {d['snow']}")
        lines.append(f"{d['date']}: " + ", ".join(bits) + (" (estimate)" if d.get("estimated") else ""))
    comp = r.get("normals_and_records") or {}
    if comp.get("normal_high") is not None:
        lines.append(f"Normal {comp.get('normal_high')}°/{comp.get('normal_low')}°")
    if comp.get("record_high"):
        lines.append(f"Records: high {comp['record_high']}, low {comp.get('record_low')}"
                     + (f" (since {comp['records_since']})" if comp.get("records_since") else ""))
    if r.get("sources"):
        lines.append("Source: " + "; ".join(r["sources"]))
    return "\n".join(lines)


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
        _check("NWS radar images", lambda: len(http.download(f"{radar_src.RIDGE}/CONUS_0.gif")[0])),
        _check("GOES satellite images", lambda: len(http.download(
            f"{sat_src.BASE}/GOES19/ABI/CONUS/GEOCOLOR/1250x750.jpg")[0])),
        _check("Open-Meteo air quality", lambda: fresh(climate.AIR_URL, {
            "latitude": round(lat, 2), "longitude": round(lon, 2), "current": "us_aqi"}).status),
        _check("NOAA ACIS climate records", lambda: len(fresh(acis.BASE, {"params": json.dumps(
            {"sid": "DSMthr", "sdate": "2025-07-04", "edate": "2025-07-04", "elems": "maxt"})}).data.get("data")
            or [])),
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
    for name, help_text in (("radar", "Radar picture (path)"), ("satellite", "Satellite picture (path)"),
                            ("air", "Air quality and UV"), ("history", "Past weather: DATE [place]")):
        sp = subs.add_parser(name, help=help_text)
        sp.add_argument("place", nargs="*", help="Place (default: home)")
    subs.add_parser("doctor", help="Check every weather source and the settings")
    subs.add_parser("clear-cache", help="Forget cached responses")
    push = subs.add_parser("push", help="Zero-token warning pushes to your chat")
    push_subs = push.add_subparsers(dest="push_command")
    setup = push_subs.add_parser("setup", help="Write the push script and settings")
    setup.add_argument("place", nargs="*", help="Place to watch (default: home)")
    setup.add_argument("--level", choices=watch.LEVELS, default="watches",
                       help="warnings, watches (default) or all (adds advisories)")
    setup.add_argument("--freeze", action="store_true", help="Also a 3 PM heads-up when tonight freezes")
    setup.add_argument("--rain", action="store_true", help="Also a heads-up when rain starts within the hour")
    push_subs.add_parser("status", help="Show what is being watched")
    push_subs.add_parser("off", help="Pause pushes (the cron job stays, but stays silent)")
    push_subs.add_parser("on", help="Resume pushes")
    push_subs.add_parser("check", help="Run one check now and print what would be sent (sends nothing)")
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
    if command == "push":
        return _push_cli(args)
    place = " ".join(getattr(args, "place", None) or [])
    prefix = "" if command == "now" else command + " "
    print(run(prefix + place))
    return 0


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _push_cli(args: argparse.Namespace) -> int:
    sub = getattr(args, "push_command", None) or "status"
    data_dir = settings.data_dir()
    if data_dir is None:
        print("Run this through Hermes: hermes better-weather push ...")
        return 1
    if sub == "setup":
        s = settings.current()
        try:
            place = places.resolve(" ".join(getattr(args, "place", None) or []))
        except places.PlaceError as exc:
            print(f"{exc} {exc.hint}".strip())
            return 1
        result = watch.write_setup(hermes_home=_hermes_home(), data_dir=data_dir,
                                   core_dir=Path(__file__).resolve().parent, place=place.public(),
                                   level=args.level, freeze=args.freeze, rain=args.rain,
                                   units_=s.units, clock=s.clock)
        print(f"Watching {place.name} ({args.level}).")
        print(f"Script: {result['script']}")
        print("If the cron job doesn't exist yet, create it once with:")
        print(f"  {result['cron_command']}")
        return 0
    config = watch._load(data_dir / watch.CONFIG_NAME, None)
    if not config:
        print("Not set up. Run: hermes better-weather push setup")
        return 1
    if sub in {"off", "on"}:
        config["paused"] = sub == "off"
        watch._save(data_dir / watch.CONFIG_NAME, config)
        print("Pushes paused. To remove the job entirely: hermes cron remove better-weather-alerts"
              if sub == "off" else f"Pushes on for {config.get('name')}.")
        return 0
    state = watch._load(data_dir / watch.STATE_NAME, {})
    if sub == "status":
        print(json.dumps({"config": config, "tracking": state.get("sent", {}),
                          "failures": state.get("failures", 0)}, indent=2, default=str))
        return 0
    message, _ = watch.check(config, state)  # dry run: state is not saved
    print(message or "(nothing new — a real run would stay silent)")
    return 0
