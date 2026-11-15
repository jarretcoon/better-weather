"""Tool schemas: what the model reads to pick a tool. First sentences are short on purpose
(Hermes' Tool Search lists only the opening of each description)."""

from __future__ import annotations

from typing import Any

_LOCATION = {
    "type": "string",
    "description": (
        "Where. Leave empty for the user's home. Accepts a place name ('Des Moines, IA', "
        "'Paris, France'), a US ZIP code, an airport code ('KDSM'), 'lat,lon', or a saved place name."
    ),
}
_UNITS = {
    "type": "string",
    "enum": ["us", "metric"],
    "description": "Override the user's unit setting (us = °F/mph/in, metric = °C/km/h/mm).",
}

WEATHER: dict[str, Any] = {
    "name": "weather",
    "description": (
        "Current weather, the next hours, today's forecast and active alerts for a place. "
        "Use this FIRST for any 'how's the weather', 'is it raining', 'do I need a jacket' question; "
        "it needs no arguments for the user's home. Answers include the source and time — mention them, "
        "and never leave out an alert."
    ),
    "parameters": {
        "type": "object",
        "properties": {"location": _LOCATION, "units": _UNITS},
        "required": [],
    },
}

FORECAST: dict[str, Any] = {
    "name": "weather_forecast",
    "description": (
        "Hourly or daily weather forecast for a place, up to 16 days ahead. "
        "Use for 'this weekend', 'next week', 'hour by hour', 'when will the rain start'. "
        "US places use the official NWS forecast (with forecaster wording) and Open-Meteo past day 7."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "location": _LOCATION,
            "days": {"type": "integer", "minimum": 1, "maximum": 16,
                     "description": "Days ahead for the daily forecast (default 7)."},
            "hourly": {"type": "boolean", "description": "Hour-by-hour instead of daily (default false)."},
            "hours": {"type": "integer", "minimum": 1, "maximum": 72,
                      "description": "How many hours for an hourly forecast (default 24)."},
            "units": _UNITS,
        },
        "required": [],
    },
}

ALERTS: dict[str, Any] = {
    "name": "weather_alerts",
    "description": (
        "Official weather warnings, watches and advisories in effect, with full text. "
        "US alerts come from the National Weather Service and include safety instructions. "
        "Use when the user asks about warnings, "
        "or when the weather tool reported an alert and they want details. Quote the instructions; "
        "never soften a warning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "location": _LOCATION,
            "state": {"type": "string",
                      "description": "Two-letter US state code for every alert in the state, e.g. 'IA'."},
        },
        "required": [],
    },
}

SEVERE: dict[str, Any] = {
    "name": "weather_severe",
    "description": (
        "Severe-storm risk for a place: the Storm Prediction Center outlook at that exact spot for "
        "days 1-3 (Marginal..High), today's tornado/wind/hail odds, active watches and warnings, and "
        "model instability for the next 48 hours. Use for 'any storms coming', 'tornado risk', "
        "'is it a severe weather day'."
    ),
    "parameters": {
        "type": "object",
        "properties": {"location": _LOCATION, "units": _UNITS},
        "required": [],
    },
}

PLACES: dict[str, Any] = {
    "name": "weather_places",
    "description": (
        "Show, save or remove the user's weather places, and set their home location. "
        "Use when the user says where home is, or names a place to remember ('save work as Ankeny')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "save", "remove", "set_home"],
                       "description": "What to do."},
            "name": {"type": "string",
                     "description": "Short name to save or remove, e.g. 'work', 'mom', 'cabin'."},
            "location": {"type": "string",
                         "description": "The place for save/set_home: name, ZIP, airport code or 'lat,lon'."},
        },
        "required": ["action"],
    },
}

ALL = (WEATHER, FORECAST, ALERTS, SEVERE, PLACES)
