"""Unit conversion and display formatting (US or metric), plus time helpers that work the same
on macOS, Linux and Windows (no platform-specific strftime flags, no hard tzdata requirement)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any

_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out  # NaN → None


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def kmh_to_mph(kmh: float) -> float:
    return kmh / 1.609344


def pa_to_inhg(pa: float) -> float:
    return pa / 3386.389


def m_to_mi(m: float) -> float:
    return m / 1609.344


def compass(deg: Any) -> str | None:
    d = num(deg)
    if d is None:
        return None
    return _COMPASS[int((d % 360) / 22.5 + 0.5) % 16]


def temp(c: Any, units: str) -> str | None:
    v = num(c)
    if v is None:
        return None
    return f"{round(c_to_f(v))}°F" if units == "us" else f"{round(v)}°C"


def temp_value(c: Any, units: str) -> int | None:
    v = num(c)
    if v is None:
        return None
    return round(c_to_f(v)) if units == "us" else round(v)


def speed(kmh: Any, units: str) -> str | None:
    v = num(kmh)
    if v is None:
        return None
    return f"{round(kmh_to_mph(v))} mph" if units == "us" else f"{round(v)} km/h"


def wind(direction_deg: Any, speed_kmh: Any, gust_kmh: Any, units: str) -> str | None:
    s = num(speed_kmh)
    if s is None:
        return None
    if s < 1.5:
        return "calm"
    text = " ".join(p for p in (compass(direction_deg), speed(s, units)) if p)
    g = num(gust_kmh)
    if g is not None and g > s + 5:
        text += f", gusts {speed(g, units)}"
    return text


def pressure(pa: Any, units: str) -> str | None:
    v = num(pa)
    if v is None:
        return None
    return f"{pa_to_inhg(v):.2f} inHg" if units == "us" else f"{v / 100:.0f} hPa"


def distance(m: Any, units: str) -> str | None:
    v = num(m)
    if v is None:
        return None
    if units == "us":
        mi = m_to_mi(v)
        return "10+ mi" if mi >= 9.95 else f"{mi:.1f} mi".replace(".0 mi", " mi")
    km = v / 1000
    return "10+ km" if km >= 9.95 else f"{km:.1f} km".replace(".0 km", " km")


def heat_or_chill_c(temp_c: float | None, rh: float | None, wind_kmh: float | None) -> float | None:
    """Feels-like temperature using the NWS wind chill and heat index formulas (°C in/out)."""
    if temp_c is None:
        return None
    t_f = c_to_f(temp_c)
    if t_f <= 50 and wind_kmh is not None and kmh_to_mph(wind_kmh) > 3:
        v = kmh_to_mph(wind_kmh) ** 0.16
        wc = 35.74 + 0.6215 * t_f - 35.75 * v + 0.4275 * t_f * v
        return (wc - 32) * 5 / 9
    if t_f >= 80 and rh is not None:
        hi = (-42.379 + 2.04901523 * t_f + 10.14333127 * rh - 0.22475541 * t_f * rh
              - 0.00683783 * t_f * t_f - 0.05481717 * rh * rh + 0.00122874 * t_f * t_f * rh
              + 0.00085282 * t_f * rh * rh - 0.00000199 * t_f * t_f * rh * rh)
        return (hi - 32) * 5 / 9
    return None


# --- time ------------------------------------------------------------------------------------

def zone(name: str | None, offset_seconds: int | None = None) -> tzinfo:
    """A tzinfo for an IANA name; falls back to a fixed offset (Windows without tzdata), then UTC."""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    if offset_seconds is not None:
        return timezone(timedelta(seconds=int(offset_seconds)))
    return UTC


def parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def clock(dt: datetime, fmt: str = "12h") -> str:
    if fmt == "24h":
        return f"{dt.hour:02d}:{dt.minute:02d}"
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {suffix}" if dt.minute else f"{hour} {suffix}"


def day_clock(dt: datetime, fmt: str = "12h", ref: datetime | None = None) -> str:
    """'3 PM', 'Tomorrow 3 PM' or 'Wed 3 PM' relative to *ref* (both in the place's zone)."""
    base = clock(dt, fmt)
    if ref is None:
        return base
    delta_days = (dt.date() - ref.date()).days
    if delta_days == 0:
        return base
    if delta_days == 1:
        return f"tomorrow {base}"
    if delta_days == -1:
        return f"yesterday {base}"
    return f"{dt.strftime('%a')} {base}"


def minutes_ago(dt: datetime | None, now: datetime | None = None) -> int | None:
    if dt is None:
        return None
    from .clock import now_utc

    reference = now or now_utc()
    return max(0, int((reference - dt).total_seconds() // 60))
