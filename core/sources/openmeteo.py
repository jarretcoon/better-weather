"""Open-Meteo: worldwide forecasts and place search (free, no key; personal use)."""

from __future__ import annotations

from typing import Any

from .. import http

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

CURRENT_VARS = (
    "temperature_2m,relative_humidity_2m,dew_point_2m,apparent_temperature,is_day,precipitation,"
    "weather_code,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
)
HOURLY_VARS = (
    "temperature_2m,apparent_temperature,dew_point_2m,precipitation_probability,precipitation,"
    "snowfall,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,cape,uv_index"
)
DAILY_VARS = (
    "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
    "apparent_temperature_min,precipitation_sum,snowfall_sum,precipitation_probability_max,"
    "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,sunrise,sunset,uv_index_max"
)


def _round(value: float, places: int = 2) -> float:
    return round(float(value), places)


def forecast(lat: float, lon: float, days: int = 7, past_days: int = 0) -> http.Fetched:
    """Current, hourly and daily data. Always fetched in metric (°C, km/h, mm) and converted
    for display, so one cached copy serves both unit settings."""
    params = {
        "latitude": _round(lat),
        "longitude": _round(lon),
        "current": CURRENT_VARS,
        "hourly": HOURLY_VARS,
        "daily": DAILY_VARS,
        "timezone": "auto",
        "forecast_days": max(1, min(int(days), 16)),
        "wind_speed_unit": "kmh",
    }
    if past_days:
        params["past_days"] = max(0, min(int(past_days), 92))
    return http.get_json(FORECAST_URL, params, ttl=http.TTL_MODEL, max_stale=12 * 3600)


def geocode(name: str, count: int = 10) -> list[dict[str, Any]]:
    params = {"name": name, "count": count, "language": "en", "format": "json"}
    fetched = http.get_json(GEOCODE_URL, params, ttl=http.TTL_GEOCODE, max_stale=60 * 24 * 3600)
    data = fetched.data or {}
    results = data.get("results") if isinstance(data, dict) else None
    return list(results or [])


# WMO weather interpretation codes (used by Open-Meteo).
WMO_CODES: dict[int, str] = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Light freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Rain showers", 82: "Violent rain showers",
    85: "Light snow showers", 86: "Snow showers",
    95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Severe thunderstorms with hail",
}


def describe(code: Any) -> str | None:
    try:
        return WMO_CODES.get(int(code))
    except (TypeError, ValueError):
        return None
