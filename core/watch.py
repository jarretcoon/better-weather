"""Zero-token warning pushes.

A Hermes cron job in no-agent mode runs a tiny script every couple of minutes. The script calls
``run()``; whatever it prints is sent to the chat verbatim, and printing nothing sends nothing.
No model is involved, so a push costs no tokens and arrives within minutes of the NWS issuing it.

What gets pushed (``level`` in watch.json):
  warnings  new warnings (tornado, severe thunderstorm, flash flood, winter storm, …) and their end
  watches   the above plus watches (default)
  all       the above plus advisories and statements
Optional extras (off unless enabled): ``freeze`` (a heads-up at 3 PM when tonight drops to 32°F or
below) and ``rain`` (a heads-up when rain is likely to start within the hour).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import http, units
from .clock import now_utc
from .sources import nws, openmeteo

CONFIG_NAME = "watch.json"
STATE_NAME = "watch-state.json"
SCRIPT_NAME = "better-weather-alerts.py"
LEVELS = ("warnings", "watches", "all")
FAIL_NOTICE_AFTER = 10          # consecutive failed checks (~20 min at every 2m) before saying so
INSTRUCTION_CHARS = 450
_VTEC = re.compile(r"/[OTEX]\.([A-Z]{3})\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\.")


# --- small file helpers ---------------------------------------------------------------------------

def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# --- alert identity ---------------------------------------------------------------------------------

def event_key(alert: dict[str, Any], raw_params: dict[str, Any] | None = None) -> tuple[str, str | None]:
    """(stable key, VTEC action). One warning keeps the same key through all its updates."""
    for vtec in (raw_params or {}).get("VTEC") or []:
        match = _VTEC.search(str(vtec))
        if match:
            action, office, phen, sig, etn = match.groups()
            return f"{office}.{phen}.{sig}.{etn}", action
    return f"{alert['event']}|{alert.get('starts') or alert.get('id')}", None


def _wanted(event: str, level: str, life_threatening: bool) -> bool:
    lowered = event.lower()
    if life_threatening or "warning" in lowered or "emergency" in lowered:
        return True
    if level in {"watches", "all"} and "watch" in lowered:
        return True
    return level == "all"


def _push_ended(event: str) -> bool:
    lowered = event.lower()
    return "warning" in lowered or "emergency" in lowered or "watch" in lowered


# --- the check ------------------------------------------------------------------------------------

def _format_new(alert: dict[str, Any], place: str, fmt_time: Any) -> str:
    mark = "🚨" if alert.get("life_threatening") else "⚠️"
    until = fmt_time(alert.get("ends"))
    lines = [f"{mark} {alert['event'].upper()} — {place}" + (f", until {until}" if until else "")]
    details = alert.get("details") or {}
    extra = []
    if details.get("tornadoDetection"):
        extra.append(f"Tornado: {details['tornadoDetection'].lower()}")
    if details.get("tornadoDamageThreat"):
        extra.append(f"damage threat {details['tornadoDamageThreat'].lower()}")
    if details.get("maxHailSize"):
        extra.append(f"hail up to {details['maxHailSize'].lower()} in")
    if details.get("maxWindGust"):
        extra.append(f"gusts to {details['maxWindGust'].lower()}")
    if extra:
        lines.append(" · ".join(extra))
    if alert.get("headline"):
        lines.append(str(alert["headline"]))
    instruction = (alert.get("instruction") or "").strip()
    if instruction:
        cut = instruction[:INSTRUCTION_CHARS]
        lines.append(cut + ("…" if len(instruction) > INSTRUCTION_CHARS else ""))
    lines.append("— National Weather Service")
    return "\n".join(lines)


def check(config: dict[str, Any], state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One pass. Returns (message or "", new state). Pure apart from the network fetches."""
    place = str(config.get("name") or "home")
    lat, lon = float(config["lat"]), float(config["lon"])
    level = str(config.get("level")) if config.get("level") in LEVELS else "watches"
    tz = units.zone(config.get("timezone"))
    now = now_utc().astimezone(tz)

    def fmt_time(value: Any) -> str | None:
        dt = units.parse_time(value)
        return units.day_clock(dt.astimezone(tz), config.get("clock") or "12h", now) if dt else None

    messages: list[str] = []
    state = dict(state)
    sent: dict[str, Any] = dict(state.get("sent") or {})

    try:
        fetched = nws.alerts_at(lat, lon, ttl=0)  # always ask the NWS fresh
        if fetched.stale:
            raise http.FetchError(fetched.url, "only a stale copy was available")
    except http.FetchError as exc:
        failures = int(state.get("failures") or 0) + 1
        state["failures"] = failures
        if failures == FAIL_NOTICE_AFTER:
            messages.append(f"⚠️ Better Weather can't reach the NWS alert service ({exc}). "
                            "Warnings won't be pushed until it's back; check weather.gov if storms are near.")
        state["sent"] = sent
        return "\n\n".join(messages), state

    if int(state.get("failures") or 0) >= FAIL_NOTICE_AFTER:
        messages.append("✅ Alert checks are working again.")
    state["failures"] = 0

    raw_by_id = {str((f.get("properties") or {}).get("id")): (f.get("properties") or {}).get("parameters") or {}
                 for f in (fetched.data or {}).get("features") or []}
    active: dict[str, dict[str, Any]] = {}
    for alert in nws.normalize_alerts(fetched.data):
        if not _wanted(alert["event"], level, bool(alert.get("life_threatening"))):
            continue
        key, action = event_key(alert, raw_by_id.get(alert["id"]))
        active[key] = alert
        previous = sent.get(key)
        if previous is None:
            if action in {"CAN", "EXP"}:
                continue
            messages.append(_format_new(alert, place, fmt_time))
            sent[key] = {"event": alert["event"], "ends": alert.get("ends"), "at": time.time()}
            continue
        if action in {"EXT", "EXA", "EXB"} and alert.get("ends") and alert.get("ends") != previous.get("ends"):
            old, new = units.parse_time(previous.get("ends")), units.parse_time(alert.get("ends"))
            if old and new and new - old >= timedelta(minutes=10):
                messages.append(f"⏱ {alert['event']} for {place} extended until {fmt_time(alert['ends'])}.")
            previous["ends"] = alert.get("ends")

    for key in list(sent):
        if key not in active:
            gone = sent.pop(key)
            if _push_ended(str(gone.get("event") or "")):
                messages.append(f"✅ {gone['event']} for {place} has ended or expired.")

    state["sent"] = sent
    messages.extend(_extras(config, state, lat, lon, now))
    return "\n\n".join(messages), state


def _extras(config: dict[str, Any], state: dict[str, Any], lat: float, lon: float, now: Any) -> list[str]:
    if not (config.get("freeze") or config.get("rain")):
        return []
    try:
        data = openmeteo.forecast(lat, lon, 2).data or {}
    except http.FetchError:
        return []
    out: list[str] = []
    place = str(config.get("name") or "home")
    us = (config.get("units") or "us") == "us"
    if config.get("freeze") and now.hour >= 15 and state.get("freeze_day") != now.date().isoformat():
        daily = data.get("daily") or {}
        lows = daily.get("temperature_2m_min") or []
        tonight = units.num(lows[1]) if len(lows) > 1 else None  # tomorrow morning's low
        threshold = float(config.get("freeze_at_c", 0.0))
        if tonight is not None and tonight <= threshold:
            low = units.temp(tonight, "us" if us else "metric")
            out.append(f"🥶 Freeze tonight in {place}: low near {low}. Cover plants, bring pets in, "
                       "and drip faucets if it gets much colder.")
        state["freeze_day"] = now.date().isoformat()
    if config.get("rain"):
        hourly = data.get("hourly") or {}
        times, chance = hourly.get("time") or [], hourly.get("precipitation_probability") or []
        amount = hourly.get("precipitation") or []
        tz = now.tzinfo
        soon = False
        for stamp, p, mm in zip(times, chance, amount, strict=False):
            dt = units.parse_time(stamp)
            if dt is None:
                continue
            dt = dt.replace(tzinfo=tz)
            if now <= dt <= now + timedelta(minutes=75) and (units.num(p) or 0) >= 60 and (units.num(mm) or 0) >= 0.2:
                soon = True
                break
        last = float(state.get("rain_at") or 0)
        if soon and time.time() - last > 3 * 3600:
            out.append(f"☔ Rain likely to start in {place} within the hour.")
            state["rain_at"] = time.time()
    return out


def run(data_dir: Path) -> str:
    """Entry point for the cron script. Never raises for ordinary failures (prints nothing)."""
    config = _load(data_dir / CONFIG_NAME, None)
    if not config:
        return ("⚠️ Better Weather warning pushes are set up but have no home location. "
                "Run: hermes better-weather push setup")
    state_path = data_dir / STATE_NAME
    state = _load(state_path, {})
    try:
        message, state = check(config, state)
        state["crashes"] = 0
    except Exception as exc:  # a bug must say so once, not every 2 minutes
        state["crashes"] = int(state.get("crashes") or 0) + 1
        quiet_until = float(state.get("crash_notice_at") or 0) + 6 * 3600
        message = ""
        if state["crashes"] == 1 or time.time() > quiet_until:
            state["crash_notice_at"] = time.time()
            message = (f"⚠️ Better Weather's warning check hit an error ({type(exc).__name__}: {exc}). "
                       "It keeps retrying; check weather.gov if storms are near.")
    _save(state_path, state)
    return message


# --- setup -----------------------------------------------------------------------------------------

SCRIPT_TEMPLATE = '''"""Better Weather warning push. Written by `hermes better-weather push setup`; safe to re-create.
Prints only when there is something new to tell you (Hermes cron no-agent mode, zero tokens)."""
import importlib.util
import sys
from pathlib import Path

PLUGIN_CORE = Path({core!r})
DATA_DIR = Path({data!r})

spec = importlib.util.spec_from_file_location(
    "better_weather_push_core", PLUGIN_CORE / "__init__.py", submodule_search_locations=[str(PLUGIN_CORE)])
if spec is None or spec.loader is None:
    sys.exit("Better Weather plugin not found at " + str(PLUGIN_CORE))
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)
from better_weather_push_core import settings, watch  # noqa: E402

settings.set_data_dir(DATA_DIR)
message = watch.run(DATA_DIR)
if message:
    print(message)
'''


def write_setup(*, hermes_home: Path, data_dir: Path, core_dir: Path, place: dict[str, Any],
                level: str, freeze: bool, rain: bool, units_: str, clock: str) -> dict[str, Any]:
    config = {"name": place["name"], "lat": place["lat"], "lon": place["lon"],
              "timezone": place.get("timezone"), "level": level, "freeze": freeze, "rain": rain,
              "units": units_, "clock": clock}
    data_dir.mkdir(parents=True, exist_ok=True)
    _save(data_dir / CONFIG_NAME, config)
    scripts = hermes_home / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / SCRIPT_NAME
    script.write_text(SCRIPT_TEMPLATE.format(core=str(core_dir), data=str(data_dir)), encoding="utf-8")
    return {"config": config, "script": str(script),
            "cron_command": (f'hermes cron create "every 2m" --no-agent --script {SCRIPT_NAME} '
                             '--deliver telegram --name better-weather-alerts')}
