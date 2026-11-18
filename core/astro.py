"""Where the sun and moon are, and the moon's rise, set and phase, for a place and a moment.

Worked out from the date and the place's latitude and longitude alone: no network call. The moon uses the
Astronomical Almanac's low-precision series (about 0.3° in longitude, its parallax to 0.003°) and Meeus's rise/set
rule (Astronomical Algorithms, ch. 15: the top of the disc on the horizon, with refraction and parallax), good to a
few minutes; tests/test_astro.py checks it against the US Naval Observatory. The same functions run weather.lou.zone's
Sun & Moon card (web/js/astro.js there) and the Android app, so all three say the same thing.

Times are Unix seconds (floats) in and out.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

RAD = math.pi / 180
DAY = 86400.0
J2000 = datetime(2000, 1, 1, 12, tzinfo=UTC).timestamp()  # the epoch the series count from
SYNODIC = 29.530588853  # a lunation, new moon to new moon (days)

# the 8 phase emoji, new moon first; as seen from the northern hemisphere (the south sees them mirrored)
PHASE_EMOJI = ("🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘")


def _sin(deg: float) -> float:
    return math.sin(deg * RAD)


def _cos(deg: float) -> float:
    return math.cos(deg * RAD)


def _norm(deg: float) -> float:
    return deg % 360


def _centuries(t: float) -> float:
    return (t - J2000) / DAY / 36525


def _sun_longitude(T: float) -> float:
    m = 357.52911 + 35999.05029 * T
    c = (1.914602 - 0.004817 * T) * _sin(m) + 0.019993 * _sin(2 * m) + 0.000289 * _sin(3 * m)
    return _norm(280.46646 + 36000.76983 * T + c)


def _moon_ecliptic(T: float) -> tuple[float, float, float]:
    """The moon's ecliptic longitude and latitude and its horizontal parallax (degrees)."""
    lon = (218.32 + 481267.881 * T + 6.29 * _sin(135.0 + 477198.87 * T) - 1.27 * _sin(259.3 - 413335.36 * T)
           + 0.66 * _sin(235.7 + 890534.22 * T) + 0.21 * _sin(269.9 + 954397.74 * T) - 0.19 * _sin(357.5 + 35999.05 * T)
           - 0.11 * _sin(186.5 + 966404.03 * T))
    lat = (5.13 * _sin(93.3 + 483202.02 * T) + 0.28 * _sin(228.2 + 960400.89 * T) - 0.28 * _sin(318.3 + 6003.15 * T)
           - 0.17 * _sin(217.6 - 407332.21 * T))
    par = (0.9508 + 0.0518 * _cos(135.0 + 477198.87 * T) + 0.0095 * _cos(259.3 - 413335.36 * T)
           + 0.0078 * _cos(235.7 + 890534.22 * T) + 0.0028 * _cos(269.9 + 954397.74 * T))
    return _norm(lon), lat, par


def _equatorial(lon: float, lat: float, T: float) -> tuple[float, float]:
    eps = 23.439291 - 0.0130042 * T
    ra = math.atan2(_sin(lon) * _cos(eps) - math.tan(lat * RAD) * _sin(eps), _cos(lon)) / RAD
    dec = math.asin(_sin(lat) * _cos(eps) + _cos(lat) * _sin(eps) * _sin(lon)) / RAD
    return _norm(ra), dec


def _altitude(ra: float, dec: float, t: float, lat: float, lon: float) -> float:
    d = (t - J2000) / DAY
    T = d / 36525
    lst = 280.46061837 + 360.98564736629 * d + 0.000387933 * T * T + lon  # local sidereal time
    return math.asin(_sin(lat) * _sin(dec) + _cos(lat) * _cos(dec) * _cos(lst - ra)) / RAD


def sun_altitude(t: float, lat: float, lon: float) -> float:
    """The sun's altitude (degrees, its centre, no refraction): above 0 means up."""
    T = _centuries(t)
    ra, dec = _equatorial(_sun_longitude(T), 0, T)
    return _altitude(ra, dec, t, lat, lon)


def moon_above_horizon(t: float, lat: float, lon: float) -> float:
    """How far the moon is above its rise/set line (degrees): above 0, the top of its disc is above the horizon."""
    T = _centuries(t)
    mlon, mlat, par = _moon_ecliptic(T)
    ra, dec = _equatorial(mlon, mlat, T)
    return _altitude(ra, dec, t, lat, lon) - (0.7275 * par - 0.5667)


def moon_phase_at(t: float) -> tuple[float, float]:
    """(phase, lit): phase 0 new, 0.25 first quarter, 0.5 full, 0.75 last quarter; lit = share of the disc lit."""
    T = _centuries(t)
    elong = _norm(_moon_ecliptic(T)[0] - _sun_longitude(T))
    return elong / 360, (1 - _cos(elong)) / 2


def next_phase(t: float, target: float) -> float:
    """The first moment after *t* the moon reaches phase *target* (0 new, 0.25, 0.5 full, 0.75)."""
    def refine(x: float) -> float:
        for _ in range(6):
            diff = target - moon_phase_at(x)[0]
            diff -= round(diff)  # the nearer way round, -0.5…0.5
            x += diff * SYNODIC * DAY
        return x

    x = refine(t + ((target - moon_phase_at(t)[0]) % 1) * SYNODIC * DAY)
    if x <= t:
        x = refine(x + SYNODIC * DAY)
    return x


def moon_events(start: float, end: float, lat: float, lon: float, step: float = 600) -> list[tuple[str, float]]:
    """Every moonrise and moonset between two moments, in order: [("rise" | "set", t)]."""
    out: list[tuple[str, float]] = []
    t0, h0 = start, moon_above_horizon(start, lat, lon)
    while t0 < end:
        t1 = min(t0 + step, end)
        h1 = moon_above_horizon(t1, lat, lon)
        if (h0 < 0) != (h1 < 0):
            out.append(("rise" if h1 >= 0 else "set", t0 + (t1 - t0) * h0 / (h0 - h1)))
        t0, h0 = t1, h1
    return out


SUN_HORIZON = -0.833  # sunrise and sunset: the top of the disc on the horizon, with refraction (degrees, its centre)


def sun_events(start: float, end: float, lat: float, lon: float, step: float = 600) -> list[tuple[str, float]]:
    """Every sunrise and sunset between two moments, in order: [("rise" | "set", t)], to about a minute."""
    out: list[tuple[str, float]] = []
    t0, h0 = start, sun_altitude(start, lat, lon) - SUN_HORIZON
    while t0 < end:
        t1 = min(t0 + step, end)
        h1 = sun_altitude(t1, lat, lon) - SUN_HORIZON
        if (h0 < 0) != (h1 < 0):
            out.append(("rise" if h1 >= 0 else "set", t0 + (t1 - t0) * h0 / (h0 - h1)))
        t0, h0 = t1, h1
    return out


def moon_pass(t: float, lat: float, lon: float) -> dict[str, float | bool | None]:
    """The moon's pass that matters at *t*: while it's up, the rise before and the set after; while it's down, the
    next rise and the set after that. A missing end is None (up or down for more than a day: far north or south)."""
    up = moon_above_horizon(t, lat, lon) >= 0
    events = moon_events(t - 30 * 3600, t + 54 * 3600, lat, lon)
    before = [e for e in events if e[1] <= t]
    after = [e for e in events if e[1] > t]
    if up:
        rise = next((e[1] for e in reversed(before) if e[0] == "rise"), None)
        down = next((e[1] for e in after if e[0] == "set"), None)
        return {"up": True, "rise": rise, "set": down}
    rise = next((e[1] for e in after if e[0] == "rise"), None)
    down = None if rise is None else next((e[1] for e in after if e[0] == "set" and e[1] > rise), None)
    return {"up": False, "rise": rise, "set": down}


def moon_name(t: float, day_start: float, day_end: float) -> str:
    """'Full moon' etc. on the day a primary phase falls (in the place's day), else 'Waxing gibbous' and the like."""
    for target, name in ((0, "New moon"), (0.25, "First quarter"), (0.5, "Full moon"), (0.75, "Last quarter")):
        if next_phase(day_start - 1, target) < day_end:
            return name
    phase, lit = moon_phase_at(t)
    return f"{'Waxing' if phase < 0.5 else 'Waning'} {'crescent' if lit < 0.5 else 'gibbous'}"


_NAMES = ("New moon", "Waxing crescent", "First quarter", "Waxing gibbous", "Full moon", "Waning gibbous",
          "Last quarter", "Waning crescent")


def moon_emoji(name: str, lat: float) -> str:
    """The emoji for a phase name (so the picture always matches the words), mirrored for the southern hemisphere,
    where a waxing moon is lit on the left."""
    index = _NAMES.index(name) if name in _NAMES else 0
    return PHASE_EMOJI[(8 - index) % 8 if lat < 0 else index]
