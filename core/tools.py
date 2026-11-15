"""Tool handlers. Each returns a JSON string and never raises (Hermes plugin contract)."""

from __future__ import annotations

import functools
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from . import http, places, report, settings

logger = logging.getLogger(__name__)

Handler = Callable[..., str]
_STATE = re.compile(r"^[A-Za-z]{2}$")


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def _error(message: str, hint: str = "") -> str:
    out = {"error": message}
    if hint:
        out["hint"] = hint
    return _dump(out)


def safe(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Handler:
    @functools.wraps(fn)
    def wrapper(args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
        try:
            return _dump(fn(dict(args or {})))
        except places.PlaceError as exc:
            return _error(str(exc), exc.hint)
        except http.FetchError as exc:
            return _error(f"Weather service unavailable: {exc}",
                          "Try again in a minute; say the data couldn't be fetched rather than guessing.")
        except Exception as exc:  # contract: tools never raise into the agent loop
            logger.exception("better-weather: %s failed", fn.__name__)
            return _error(f"{type(exc).__name__}: {exc}", "This is a bug in Better Weather.")

    return wrapper


def _units(args: dict[str, Any]) -> str:
    value = str(args.get("units") or "").strip().lower()
    if value in {"us", "metric"}:
        return value
    return settings.current().units


def _place(args: dict[str, Any]) -> places.Place:
    return places.resolve(args.get("location"), args.get("lat"), args.get("lon"))


@safe
def weather(args: dict[str, Any]) -> dict[str, Any]:
    return report.now(_place(args), _units(args), settings.current().clock)


@safe
def weather_forecast(args: dict[str, Any]) -> dict[str, Any]:
    return report.forecast(
        _place(args), _units(args), settings.current().clock,
        days=int(args.get("days") or 7),
        hourly=bool(args.get("hourly")),
        hours=int(args.get("hours") or 24),
    )


@safe
def weather_alerts(args: dict[str, Any]) -> dict[str, Any]:
    state = str(args.get("state") or "").strip()
    if state:
        if not _STATE.match(state):
            raise places.PlaceError("state must be a two-letter US code like 'IA'.")
        return report.alerts(None, state, settings.current().clock)
    return report.alerts(_place(args), None, settings.current().clock)


@safe
def weather_severe(args: dict[str, Any]) -> dict[str, Any]:
    return report.severe(_place(args), _units(args), settings.current().clock)


@safe
def weather_places(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "list").strip().lower()
    name = str(args.get("name") or "").strip()
    location = str(args.get("location") or "").strip()
    current = settings.current()

    if action == "list":
        return {"home": current.home or None, "saved": places.saved_places(),
                "units": current.units, "clock": current.clock}

    if action == "set_home":
        if not location:
            raise places.PlaceError("set_home needs a location.")
        place = places.resolve(location)  # check it resolves before saving
        current.set("home", location)
        return {"ok": True, "home": location, "resolved_to": place.public(),
                "note": place.note or "Saved. 'weather' with no location now means this place."}

    if action == "save":
        if not name or not location:
            raise places.PlaceError("save needs both a name and a location.")
        if name.lower() in {"home", "here"}:
            raise places.PlaceError("Use action='set_home' to change home.")
        place = places.resolve(location)
        return {"ok": True, "saved": name.lower(), "place": places.save_place(name, place)}

    if action == "remove":
        if not name:
            raise places.PlaceError("remove needs a name.")
        return {"ok": places.remove_place(name), "removed": name.lower()}

    raise places.PlaceError(f"Unknown action '{action}'.", "Use list, save, remove or set_home.")


TOOLS: tuple[tuple[str, Handler], ...] = (
    ("weather", weather),
    ("weather_forecast", weather_forecast),
    ("weather_alerts", weather_alerts),
    ("weather_severe", weather_severe),
    ("weather_places", weather_places),
)
