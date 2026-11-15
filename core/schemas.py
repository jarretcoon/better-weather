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
        "Weather now, next hours, today and alerts for a place. "
        "Use this FIRST for any 'how's the weather', 'is it raining', 'do I need a jacket' question; "
        "it needs no arguments for the user's home. Reply with the returned `card` as-is (it's formatted "
        "for chat), plus one short line if useful; never leave out an alert."
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
        "Hourly or daily forecast for a place, up to 16 days. "
        "Use for 'this weekend', 'next week', 'hour by hour', 'when will the rain start'. "
        "US places use the official NWS forecast (with forecaster wording) and Open-Meteo past day 7. "
        "Reply with the returned `card` as-is."
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
        "Official weather warnings and watches, full text. "
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
        "Severe-storm risk: SPC outlook, watches and warnings. "
        "The Storm Prediction Center outlook at that exact spot for days 1-3 (Marginal..High), today's "
        "tornado/wind/hail odds, active watches and warnings, and model instability for 48 hours. "
        "Use for 'any storms coming', 'tornado risk', 'is it a severe weather day'."
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
        "Set home and save named weather places. "
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

RADAR: dict[str, Any] = {
    "name": "weather_radar",
    "description": (
        "Radar picture of rain, snow and storms for a US place. "
        "Local, regional or national, still or an animated loop, with active warning boxes drawn in. "
        "Returns a `media` line; put it on its own line in your reply so the chat shows the image."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "location": _LOCATION,
            "view": {"type": "string", "enum": ["local", "regional", "national"],
                     "description": "local = the nearest radar (default), regional = several states, "
                                    "national = lower 48."},
            "loop": {"type": "boolean", "description": "Animated loop of the last hour (default false)."},
        },
        "required": [],
    },
}

SATELLITE: dict[str, Any] = {
    "name": "weather_satellite",
    "description": (
        "Satellite picture of clouds, storms and smoke. "
        "GOES imagery for the US and nearby, still or animated. Returns a `media` line; "
        "put it on its own line in your reply so the chat shows the image."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "location": _LOCATION,
            "kind": {"type": "string", "enum": ["geocolor", "infrared", "water_vapor"],
                     "description": "geocolor = true color by day / infrared at night (default), "
                                    "infrared = cloud-top temperatures, water_vapor = mid-level moisture."},
            "loop": {"type": "boolean", "description": "Animated loop (default false)."},
        },
        "required": [],
    },
}

AIR: dict[str, Any] = {
    "name": "weather_air",
    "description": (
        "Air quality, smoke, UV and pollen for a place. "
        "AQI with the main pollutant, PM2.5 smoke, ozone, UV and (Europe) pollen, now and the worst hour in "
        "the next 24. Use for 'is the air ok', 'smoke', 'UV', 'pollen', "
        "'can I run outside'."
    ),
    "parameters": {
        "type": "object",
        "properties": {"location": _LOCATION},
        "required": [],
    },
}

HISTORY: dict[str, Any] = {
    "name": "weather_history",
    "description": (
        "Past weather, normals and records for a place. "
        "Highs, lows, rain and snow for a day or up to 31 days. "
        "US places use official station records, with the 1991-2020 normal and the record high and low "
        "for a single day. Use for 'how cold was it yesterday', 'was last March wet', "
        "'is this warmer than normal', 'record high today'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "location": _LOCATION,
            "date": {"type": "string",
                     "description": "YYYY-MM-DD, 'today', 'yesterday' or 'N days ago' (default today)."},
            "end_date": {"type": "string", "description": "Optional end of a range, same formats."},
            "units": _UNITS,
        },
        "required": [],
    },
}

CONFIDENCE: dict[str, Any] = {
    "name": "weather_confidence",
    "description": (
        "How sure the forecast is, from 80+ model runs. "
        "Gives the odds of rain or snow (and of big amounts) and the likely range of highs and lows for each "
        "of the next days. Use for 'how sure are they', 'will it "
        "really snow', 'how much rain could we get', or before giving a confident answer about days 3+."
    ),
    "parameters": {"type": "object", "properties": {
        "location": _LOCATION,
        "days": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Days ahead (default 7)."},
        "units": _UNITS}, "required": []},
}

DISCUSSION: dict[str, Any] = {
    "name": "weather_discussion",
    "description": (
        "The NWS forecasters' own written discussion for a US place. "
        "Key messages, the reasoning behind the forecast and its uncertainty. Use for 'why', 'what are they thinking', "
        "'how confident is the weather service'. Summarize it in plain words."
    ),
    "parameters": {"type": "object", "properties": {"location": _LOCATION}, "required": []},
}

BEST_TIME: dict[str, Any] = {
    "name": "weather_best_time",
    "description": (
        "Best hours for an outdoor activity in the next days. "
        "Activities: walk, run, bike, golf, mow, grill, outdoor (patio/picnic), car_wash (needs a dry day "
        "after) or stargazing (clear night). Use for 'when should I…', 'best time to…', 'good day to…'."
    ),
    "parameters": {"type": "object", "properties": {
        "activity": {"type": "string",
                     "description": "walk, run, bike, golf, mow, grill, outdoor, car_wash or stargazing "
                                    "(close words like 'jog' or 'bbq' work too)."},
        "location": _LOCATION,
        "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "How many days to look (default 3)."},
        "units": _UNITS}, "required": ["activity"]},
}

SCORECARD: dict[str, Any] = {
    "name": "weather_scorecard",
    "description": (
        "Which forecast has been most accurate at home lately. "
        "Compares the NWS forecast with the GFS, ECMWF, ICON, GEM and blended models by average miss on "
        "highs and lows and rain yes/no, 1-3 days ahead. Learns daily on its own. "
        "Use for 'which forecast should I trust', 'who's been right'."
    ),
    "parameters": {"type": "object", "properties": {
        "days": {"type": "integer", "minimum": 7, "maximum": 365, "description": "Look-back window (default 30)."},
        "units": _UNITS}, "required": []},
}

TRAVEL: dict[str, Any] = {
    "name": "weather_travel",
    "description": (
        "Travel weather for airports and destinations. "
        "An airport's conditions, forecast and FAA delays, or the weather at a destination when you arrive. "
        "Give `airport` (DSM, KORD) for flight weather and delays, and/or `destination` plus `arrive` for "
        "what to expect there."
    ),
    "parameters": {"type": "object", "properties": {
        "airport": {"type": "string", "description": "Airport code, 3-letter (DSM) or 4-letter (KDSM)."},
        "destination": {"type": "string", "description": "Where you're going (any place form)."},
        "arrive": {"type": "string", "description": "Arrival time, local at the destination: YYYY-MM-DDTHH:MM."},
        "units": _UNITS}, "required": []},
}

WATER: dict[str, Any] = {
    "name": "weather_water",
    "description": (
        "Rivers, tides and waves near a place. "
        "River levels against flood stage (US), tide times (US coasts) and waves (coasts and big lakes). "
        "Use for 'is the river flooding', 'how high is the river', 'tides', 'waves'."
    ),
    "parameters": {"type": "object", "properties": {"location": _LOCATION, "units": _UNITS}, "required": []},
}

SCIENCE: dict[str, Any] = {
    "name": "weather_science",
    "description": (
        "Storm-science numbers from a model sounding for a place. "
        "CAPE/CIN (surface, mixed-layer, most-unstable), ECAPE, LCL/LFC/EL, lapse rate, 0-6 km shear, storm "
        "motion, 0-1/0-3 km helicity, precipitable water, freezing level and the tornado (STP) and supercell "
        "(SCP) composites, with a plain-language reading. Use for 'how unstable', 'CAPE', 'shear', 'tornado "
        "ingredients', 'sounding'. Pair with weather_severe for the official outlook."
    ),
    "parameters": {"type": "object", "properties": {
        "location": _LOCATION,
        "model": {"type": "string", "enum": ["gfs", "hrrr"],
                  "description": "gfs (worldwide, default) or hrrr (US, higher detail, next 48 h)."},
        "hours_ahead": {"type": "integer", "minimum": 0, "maximum": 48, "description": "Hours from now (default 0)."},
        "at_peak": {"type": "boolean",
                    "description": "Use the hour with the most model CAPE in the next 24 h (good for storm days)."},
        "units": _UNITS}, "required": []},
}

ALL = (WEATHER, FORECAST, ALERTS, SEVERE, PLACES, RADAR, SATELLITE, AIR, HISTORY, CONFIDENCE, DISCUSSION,
       BEST_TIME, SCORECARD, TRAVEL, WATER, SCIENCE)
