"""Zero-token warning pushes.

A Hermes cron job in no-agent mode runs a tiny script every couple of minutes. The script calls
``run()``; whatever it prints is sent to the chat verbatim, and printing nothing sends nothing.
No model is involved, so a push costs no tokens and arrives within minutes of the NWS issuing it.

What gets pushed (``level`` in watch.json):
  warnings  new warnings (tornado, severe thunderstorm, flash flood, winter storm, …) and their end
  watches   the above plus watches (default)
  all       the above plus advisories and statements
Optional extras (off unless enabled): ``freeze`` (at 3 PM, a heads-up for the first freezing night
after a milder stretch, not every night of winter) and ``rain`` (rain or snow likely to start within
the hour while it's dry now; 7 AM-10 PM only, at most every 3 hours).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import http, units
from .clock import now_utc
from .sources import nws, openmeteo

logger = logging.getLogger(__name__)

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
    return f"{alert['event']}|{alert.get('id')}", None


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


# VTEC actions that mean the event is over or has been replaced by another one.
_OVER = {"CAN": "was cancelled", "UPG": "was replaced by a stronger alert", "EXP": "has expired"}
MISSES_BEFORE_ENDED = 2  # an alert must be missing from 2 checks in a row before we say it ended


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
    state = copy.deepcopy(state)  # never mutate the caller's copy (it is what gets saved on a crash)
    sent: dict[str, dict[str, Any]] = state.get("sent") or {}

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

    raw_params = {str((f.get("properties") or {}).get("id")): (f.get("properties") or {}).get("parameters") or {}
                  for f in (fetched.data or {}).get("features") or []}
    current: dict[str, dict[str, Any]] = {}
    over: dict[str, str] = {}
    for alert in nws.normalize_alerts(fetched.data):
        key, action = event_key(alert, raw_params.get(alert["id"]))
        if action is None:  # no VTEC: follow references so an update keeps the key it was pushed under
            refs = set(alert.get("references") or []) | {alert["id"]}
            key = next((k for k, entry in sent.items() if entry.get("id") in refs), key)
        if action in _OVER:
            over[key] = _OVER[action]
            continue
        if _wanted(alert["event"], level, bool(alert.get("life_threatening"))):
            current.setdefault(key, alert)  # alerts arrive most urgent first
    for key in over:
        current.pop(key, None)

    for key, alert in current.items():
        previous = sent.get(key)
        if previous is None:
            messages.append(_format_new(alert, place, fmt_time))
            sent[key] = {"event": alert["event"], "ends": alert.get("ends"), "id": alert["id"],
                         "at": time.time(), "missed": 0}
            continue
        previous["missed"] = 0
        previous["id"] = alert["id"]
        new_end = alert.get("ends")
        if new_end and new_end != previous.get("ends"):
            old_dt, new_dt = units.parse_time(previous.get("ends")), units.parse_time(new_end)
            if old_dt and new_dt and new_dt - old_dt >= timedelta(minutes=10):
                messages.append(f"⏱ {alert['event']} for {place} extended until {fmt_time(new_end)}.")
            previous["ends"] = new_end

    for key in list(sent):
        if key in current:
            continue
        entry = sent[key]
        reason = over.get(key)
        if reason is None:  # just missing from this response: wait for a second miss before believing it
            entry["missed"] = int(entry.get("missed") or 0) + 1
            if entry["missed"] < MISSES_BEFORE_ENDED:
                continue
            reason = "has ended or expired"
        del sent[key]
        if _push_ended(str(entry.get("event") or "")):
            messages.append(f"✅ {entry['event']} for {place} {reason}.")

    state["sent"] = sent
    try:  # the optional extras must never cost a warning
        messages.extend(_extras(config, state, lat, lon, now))
    except Exception:
        logger.warning("better-weather: freeze/rain extras failed", exc_info=True)
    return "\n\n".join(messages), state


FREEZE_CHECK_HOUR = 15            # look at tonight's low once a day, mid-afternoon
FREEZE_QUIET_NIGHTS = 3          # only the first freezing night after this many milder nights
RAIN_HOURS = (7, 22)             # rain/snow heads-ups only between 7 AM and 10 PM local
RAIN_GAP_SECONDS = 3 * 3600      # at most one rain/snow heads-up every 3 hours
_WET_CODES = set(range(51, 68)) | set(range(71, 87)) | {95, 96, 99}


def _rows(hourly: dict[str, Any], now: Any) -> list[dict[str, Any]]:
    out = []
    times = hourly.get("time") or []
    for i, stamp in enumerate(times):
        dt = units.parse_time(stamp)
        if dt is None:
            continue
        row = {k: (v[i] if i < len(v) else None) for k, v in hourly.items() if isinstance(v, list)}
        row["dt"] = dt.replace(tzinfo=now.tzinfo)
        out.append(row)
    return out


def _freeze(config: dict[str, Any], state: dict[str, Any], rows: list[dict[str, Any]], now: Any) -> str | None:
    today = now.date()
    if now.hour < FREEZE_CHECK_HOUR or state.get("freeze_day") == today.isoformat():
        return None
    state["freeze_day"] = today.isoformat()
    night_end = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    night = [r for r in rows if now <= r["dt"] <= night_end and units.num(r.get("temperature_2m")) is not None]
    if not night:
        return None
    coldest = min(night, key=lambda r: units.num(r["temperature_2m"]) or 0)
    low = units.num(coldest["temperature_2m"]) or 0
    threshold = units.num(config.get("freeze_at_c"))
    if low > (0.0 if threshold is None else threshold):
        return None
    last = state.get("last_freeze_night")
    state["last_freeze_night"] = today.isoformat()
    if last:
        try:
            if (today - date.fromisoformat(str(last))).days <= FREEZE_QUIET_NIGHTS:
                return None  # still in a cold spell: it already knows
        except ValueError:
            pass
    shown = units.temp(low, "metric" if config.get("units") == "metric" else "us")
    when = units.clock(coldest["dt"], config.get("clock") or "12h")
    return (f"🥶 First freeze in a while tonight in {config.get('name') or 'home'}: low near {shown} around "
            f"{when}. Cover plants, bring pets in, and unhook garden hoses.")


def _rain_soon(config: dict[str, Any], state: dict[str, Any], data: dict[str, Any], rows: list[dict[str, Any]],
               now: Any) -> str | None:
    if not (RAIN_HOURS[0] <= now.hour < RAIN_HOURS[1]):
        return None
    current = data.get("current") or {}
    if (units.num(current.get("precipitation")) or 0) >= 0.1 or units.num(current.get("weather_code")) in _WET_CODES:
        state["rain_at"] = time.time()  # already wet: no "starting soon", and no repeat right after it stops
        return None
    if time.time() - float(state.get("rain_at") or 0) < RAIN_GAP_SECONDS:
        return None
    for row in rows:
        if not (now <= row["dt"] <= now + timedelta(minutes=75)):
            continue
        likely = (units.num(row.get("precipitation_probability")) or 0) >= 60
        if likely and (units.num(row.get("precipitation")) or 0) >= 0.2:
            state["rain_at"] = time.time()
            snowy = (units.num(row.get("snowfall")) or 0) > 0 or (units.num(row.get("temperature_2m")) or 5) <= 0.5
            kind = "🌨️ Snow" if snowy else "☔ Rain"
            return f"{kind} likely to start in {config.get('name') or 'home'} within the hour."
    return None


def _extras(config: dict[str, Any], state: dict[str, Any], lat: float, lon: float, now: Any) -> list[str]:
    if not (config.get("freeze") or config.get("rain")):
        return []
    try:
        data = openmeteo.forecast(lat, lon, 2).data or {}
    except http.FetchError:
        return []
    rows = _rows(data.get("hourly") or {}, now)
    out = []
    if config.get("freeze"):
        out.append(_freeze(config, state, rows, now))
    if config.get("rain"):
        out.append(_rain_soon(config, state, data, rows, now))
    return [m for m in out if m]


def _rate_limited(state: dict[str, Any], key: str, hours: float) -> bool:
    """True when a notice under *key* may be sent now (and records that it was)."""
    last = float(state.get(key) or 0)
    if time.time() - last < hours * 3600:
        return False
    state[key] = time.time()
    return True


def run(data_dir: Path) -> str:
    """Entry point for the cron script. Prints nothing on a quiet tick; problems are reported rarely."""
    state_path = data_dir / STATE_NAME
    state = _load(state_path, {})
    config = _load(data_dir / CONFIG_NAME, None)
    if not isinstance(config, dict) or "lat" not in config or "lon" not in config:
        message = ""
        if _rate_limited(state, "config_notice_at", 24):
            message = ("⚠️ Better Weather warning pushes have no place to watch. Run: hermes better-weather push "
                       "setup (or remove the job: hermes cron remove better-weather-alerts)")
        _save(state_path, state)
        return message
    if config.get("paused"):
        return ""
    try:
        message, state = check(config, state)
        state["crashes"] = 0
    except Exception as exc:  # a bug must say so once, not every 2 minutes
        state["crashes"] = int(state.get("crashes") or 0) + 1
        message = ""
        if state["crashes"] == 1 or _rate_limited(state, "crash_notice_at", 6):
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
