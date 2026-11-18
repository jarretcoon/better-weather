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
import tempfile
import threading
import time
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from . import http, units
from .clock import now_utc
from .sources import iem, nws, openmeteo

logger = logging.getLogger(__name__)

CONFIG_NAME = "watch.json"
STATE_NAME = "watch-state.json"
SCRIPT_NAME = "better-weather-alerts.py"
LEVELS = ("warnings", "watches", "all")
FAIL_NOTICE_AFTER = 10          # consecutive failed checks (~20 min at every 2m) before saying so
INSTRUCTION_CHARS = 450
STARTS_LATER = timedelta(minutes=5)   # an alert starting later than this says "from … to …", not "until …"
LOCK_NAME = "watch.lock"
LOCK_STALE_S = 10 * 60          # a lock this old was left by a run that died or hung: the next run takes it over
RUN_BUDGET_S = 100              # a run gives up after this (the job runs every 2 minutes) and counts as a failed check
EXTRAS_MIN_S = 10               # the freeze/rain heads-ups only start with this much of the budget left
_VTEC = re.compile(r"/[OTEX]\.([A-Z]{3})\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\.")


# --- small file helpers ---------------------------------------------------------------------------

def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _lock(path: Path) -> str | None:
    """Take the run lock (an O_EXCL file): a token to release it with, or None while another run holds it."""
    token = f"{os.getpid()} {time.time()}"
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue   # released just now
            if age < LOCK_STALE_S:
                return None
            path.unlink(missing_ok=True)   # left by a run that died or hung: take it over
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
        return token
    return None


def _unlock(path: Path, token: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == token:   # still ours (not taken over after a very long run)
            path.unlink()
    except OSError:
        pass


class _OutOfTime(Exception):
    pass


def _within(seconds: float, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """fn(*args, **kwargs), or _OutOfTime after `seconds` (the call is left to finish in a daemon thread, which
    the cron script's exit ends: nothing it does after that is used)."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=work, name="better-weather-push", daemon=True)
    worker.start()
    worker.join(max(0.0, seconds))
    if worker.is_alive():
        raise _OutOfTime
    if "error" in box:
        raise box["error"]
    return box["result"]


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

def _format_new(alert: dict[str, Any], place: str, fmt_time: Any, now: datetime) -> str:
    mark = "🚨" if alert.get("life_threatening") else "⚠️"
    until = fmt_time(alert.get("ends"))
    starts = units.parse_time(alert.get("starts"))
    if starts is not None and starts.tzinfo is not None and starts > now + STARTS_LATER:   # a watch for tomorrow
        to = fmt_time(alert.get("ends"), alert.get("starts"))
        when = f", from {fmt_time(alert.get('starts'))}" + (f" to {to}" if to else "")
    else:
        when = f", until {until}" if until else ""
    lines = [f"{mark} {alert['event'].upper()} — {place}{when}"]
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
        lines.append(_flat(alert["headline"]))
    instruction = _flat(alert.get("instruction") or "", INSTRUCTION_CHARS)
    if instruction:
        lines.append(instruction)
    lines.append("— National Weather Service")
    return "\n".join(lines)


def _flat(text: Any, limit: int | None = None) -> str:
    """NWS text comes hard-wrapped at ~70 columns ("…possible Flood\nWarnings…"): join it, cut at a word."""
    flat = " ".join(str(text).split())
    if limit is None or len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + "…"


# VTEC actions that mean the event is over or has been replaced by another one. (The NWS's feed sends CAN and UPG as
# "Cancel" messages, which nws.normalize_alerts drops and /alerts/active doesn't list: those alerts just vanish.)
_OVER = {"CAN": "was cancelled", "UPG": "was replaced by a stronger alert", "EXP": "has expired"}
MISSES_BEFORE_ENDED = 2  # an alert must be missing from 2 checks in a row before we say it ended
ENDED = "has ended or expired"
# An upgrade: the old alert vanishes as a stronger one from the same office appears (the new one doesn't reference
# it). Stronger = the warning of the same hazard (a watch or advisory), or higher up one of these ladders.
UPGRADE_WINDOW_S = 20 * 60        # the stronger alert first seen this close to when the old one went missing
_LADDERS = (
    ("WW.Y", "WS.A", "LE.A", "WS.W", "LE.W", "IS.W", "BZ.W"),   # winter
    ("WI.Y", "HW.A", "HW.W"),                                   # wind
    ("SC.Y", "GL.A", "GL.W", "SR.A", "SR.W", "HF.A", "HF.W"),   # marine wind
    ("TR.A", "TR.W", "HU.W"), ("TR.W", "TY.W"),                 # tropical
    ("HT.Y", "EH.A", "EH.W"), ("CW.Y", "EC.A", "EC.W"), ("FR.Y", "FZ.A", "FZ.W"),
)
# Watches and advisories that run alongside their own warnings (a county watch, storm-drawn warnings inside it)
_ALONGSIDE = {"TO", "SV", "FF", "FA", "FL", "EW", "SQ", "MA", "DS"}


def _stronger(old: str, new: str) -> bool:
    """True when VTEC hazard `new` ("GL.W") replaces `old` ("GL.A") as an upgrade."""
    (old_ph, old_sig), (new_ph, new_sig) = old.split("."), new.split(".")
    if new_sig != "W":
        return False
    if old_ph == new_ph and old_sig in {"A", "Y"} and old_ph not in _ALONGSIDE:
        return True
    return any(old in ladder and new in ladder and ladder.index(new) > ladder.index(old) for ladder in _LADDERS)


def _upgraded_to(key: str, entry: dict[str, Any], current: dict[str, dict[str, Any]],
                 sent: dict[str, dict[str, Any]]) -> str | None:
    """The event that replaced the vanished alert under `key`, if a stronger one from its office just appeared."""
    parts = key.split(".")
    if "|" in key or len(parts) != 4:
        return None
    office, hazard = parts[0], f"{parts[1]}.{parts[2]}"
    since = float(entry.get("gone_at") or time.time()) - UPGRADE_WINDOW_S
    for other, alert in current.items():
        bits = other.split(".")
        if "|" in other or len(bits) != 4 or bits[0] != office or not _stronger(hazard, f"{bits[1]}.{bits[2]}"):
            continue
        if float((sent.get(other) or {}).get("at") or 0) >= since:   # new, not one that was already up
            return str(alert["event"])
    return None


def _failed(state: dict[str, Any]) -> str | None:
    """Count one failed check; the notice when the alert service has been out for about 20 minutes."""
    state["failures"] = int(state.get("failures") or 0) + 1
    if state["failures"] == FAIL_NOTICE_AFTER:
        return ("⚠️ Better Weather can't reach the NWS alert service. Warnings won't be pushed until it's back; "
                "check weather.gov if storms are near.")
    return None


def _zone(config: dict[str, Any], lat: float, lon: float) -> tzinfo | None:
    """The place's own zone: the saved one, else looked up (the NWS's grid, then Open-Meteo; both kept for weeks).
    Never UTC by default: that shifted the rain hours and the 3 PM freeze check. None only when nothing answers."""
    name = config.get("timezone")
    if not name:
        try:
            name = (nws.point(lat, lon) or {}).get("timeZone")
        except http.FetchError:
            name = None
    if not name:
        try:
            name = openmeteo.timezone_at(lat, lon)
        except http.FetchError:
            name = None
    return units.zone(str(name)) if name else None


def check(config: dict[str, Any], state: dict[str, Any], *,
          deadline: float | None = None) -> tuple[str, dict[str, Any]]:
    """One pass. Returns (message or "", new state). Pure apart from the network fetches. With a `deadline`
    (time.monotonic()), the optional extras get only the time left before it: they never cost a warning."""
    place = str(config.get("name") or "home")
    lat, lon = float(config["lat"]), float(config["lon"])
    level = str(config.get("level")) if config.get("level") in LEVELS else "watches"
    tz = _zone(config, lat, lon)
    now = now_utc().astimezone(tz or UTC)
    clock = config.get("clock") or "12h"

    def local(dt: datetime) -> datetime:   # zone unknown: the time's own offset (the NWS's times are local)
        return dt.astimezone(tz) if tz is not None else dt

    def fmt_time(value: Any, after: Any = None) -> str | None:
        """'3 PM', 'tomorrow 3 PM', 'Wed 3 PM'; just '9 PM' on the day of `after` ('from tomorrow 3 AM to 9 PM')."""
        dt, first = units.parse_time(value), units.parse_time(after)
        if dt is None:
            return None
        when = local(dt)
        if first is not None and first.tzinfo is not None and local(first).date() == when.date():
            return units.clock(when, clock)
        return units.day_clock(when, clock, now.astimezone(when.tzinfo) if when.tzinfo else now)

    messages: list[str] = []
    state = copy.deepcopy(state)  # never mutate the caller's copy (it is what gets saved on a crash)
    sent: dict[str, dict[str, Any]] = state.get("sent") or {}

    try:
        fetched = nws.alerts_at(lat, lon, ttl=0)  # always ask the NWS fresh (else the mesonet's copy: iem.py)
        if fetched.stale:
            raise http.FetchError(fetched.url, "only a stale copy was available")
    except nws.NotCovered:   # abroad (set up before push setup checked): say so once a day, never "no alerts"
        if _rate_limited(state, "coverage_notice_at", 24):
            messages.append(f"⚠️ Better Weather warning pushes are US-only, and {place} is outside the National "
                            "Weather Service's area, so nothing will be pushed. Run: hermes better-weather push "
                            "setup (with a US place), or remove the job: hermes cron remove better-weather-alerts")
        state["sent"] = sent
        return "\n\n".join(messages), state
    except http.FetchError as exc:
        logger.info("better-weather: alert check failed: %s", exc)
        notice = _failed(state)
        if notice:
            messages.append(notice)
        state["sent"] = sent
        return "\n\n".join(messages), state

    via_copy = iem.is_copy(fetched.data)   # the NWS didn't answer; the mesonet's copy of its warnings did
    if int(state.get("failures") or 0) >= FAIL_NOTICE_AFTER:
        messages.append("✅ Alert checks are working again" + (" (from Iowa State's copy of NWS warnings while "
                                                               "the NWS is down)." if via_copy else "."))
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
            source = "\n(From Iowa State's copy of NWS warnings: the NWS didn't answer.)" if via_copy else ""
            messages.append(_format_new(alert, place, fmt_time, now) + source)
            sent[key] = {"event": alert["event"], "ends": alert.get("ends"), "id": alert["id"],
                         "at": time.time(), "missed": 0}
            continue
        previous["missed"] = 0
        previous.pop("gone_at", None)
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
        if reason is None and via_copy and "|" in key:
            continue   # not a VTEC product: the copy can't list it, so its absence says nothing
        if reason is None:  # just missing from this response: wait for a second miss before believing it
            entry["missed"] = int(entry.get("missed") or 0) + 1
            entry.setdefault("gone_at", time.time())
            if entry["missed"] < MISSES_BEFORE_ENDED:
                continue
            reason = ENDED
        upgrade = _upgraded_to(key, entry, current, sent) if reason in (ENDED, _OVER["UPG"]) else None
        del sent[key]
        if not _push_ended(str(entry.get("event") or "")):
            continue
        if upgrade:   # not an all-clear: the stronger alert is in effect (and was pushed as new)
            messages.append(f"⬆️ {entry['event']} for {place} was upgraded to {upgrade}.")
        else:
            messages.append(f"✅ {entry['event']} for {place} {reason}.")

    state["sent"] = sent
    try:  # the optional extras must never cost a warning, nor the time a run has for it
        if deadline is None:
            messages.extend(_extras(config, state, lat, lon, now))
        elif deadline - time.monotonic() >= EXTRAS_MIN_S and (config.get("freeze") or config.get("rain")):
            mine = copy.deepcopy(state)   # a slow forecast left running must not change the state being saved
            messages.extend(_within(deadline - time.monotonic(), _extras, config, mine, lat, lon, now))
            state = mine
    except _OutOfTime:
        logger.info("better-weather: freeze/rain extras ran out of time")
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
    recent = [r for r in rows if now - timedelta(days=FREEZE_QUIET_NIGHTS) <= r["dt"] <= now - timedelta(hours=3)]
    limit = 0.0 if threshold is None else threshold
    temps = (units.num(r.get("temperature_2m")) for r in recent)
    if any(t is not None and t <= limit for t in temps):   # (0.0 °C is a freeze, not "no reading")
        return None  # it already froze in the last few nights (works even with no saved history)
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
            temp = units.num(row.get("temperature_2m"))   # (0.0 °C is cold enough, not "no reading")
            snowy = (units.num(row.get("snowfall")) or 0) > 0 or (temp is not None and temp <= 0.5)
            kind = "🌨️ Snow" if snowy else "☔ Rain"
            return f"{kind} likely to start in {config.get('name') or 'home'} within the hour."
    return None


def _extras(config: dict[str, Any], state: dict[str, Any], lat: float, lon: float, now: Any) -> list[str]:
    if not (config.get("freeze") or config.get("rain")):
        return []
    try:
        data = openmeteo.forecast(lat, lon, 2, past_days=FREEZE_QUIET_NIGHTS).data or {}
    except http.FetchError:
        return []
    if data.get("timezone") or data.get("utc_offset_seconds") is not None:   # its hours are in its own zone
        now = now.astimezone(units.zone(data.get("timezone"), data.get("utc_offset_seconds")))
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
    """Entry point for the cron script. Prints nothing on a quiet tick; problems are reported rarely.

    One run at a time (a lock file; a run that finds it held stays quiet), and each gives up after RUN_BUDGET_S:
    a slow NWS can't pile runs up on the 2-minute schedule."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / LOCK_NAME
    token = _lock(lock)
    if token is None:
        return ""   # the run before is still going: it says whatever there is to say
    try:
        return _run(data_dir)
    finally:
        _unlock(lock, token)


def _run(data_dir: Path) -> str:
    started = time.monotonic()
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
        deadline = started + RUN_BUDGET_S
        message, state = _within(deadline - time.monotonic(), check, config, state, deadline=deadline - 5)
        state["crashes"] = 0
    except _OutOfTime:   # the NWS (and its copy) too slow to answer: a failed check, said like any other
        logger.info("better-weather: alert check ran out of time")
        message = _failed(state) or ""
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
