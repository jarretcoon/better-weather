from datetime import UTC, datetime, timedelta

from better_weather.core import geo, units


def test_temperature_and_speed_formatting():
    assert units.temp(0, "us") == "32°F"
    assert units.temp(-1.1, "us") == "30°F"
    assert units.temp(21.4, "metric") == "21°C"
    assert units.temp(None, "us") is None
    assert units.speed(16.09344, "us") == "10 mph"
    assert units.speed(10, "metric") == "10 km/h"


def test_wind_text():
    assert units.wind(100, 5.4, None, "us") == "E 3 mph"
    assert units.wind(315, 30, 50, "us") == "NW 19 mph, gusts 31 mph"
    assert units.wind(315, 30, 32, "us") == "NW 19 mph"  # gust barely above sustained: not worth saying
    assert units.wind(0, 0.5, None, "us") == "calm"
    assert units.wind(0, None, None, "us") is None


def test_compass_wraps():
    assert units.compass(0) == "N"
    assert units.compass(359) == "N"
    assert units.compass(225) == "SW"
    assert units.compass("x") is None


def test_feels_like_matches_nws_tables():
    # NWS wind chill chart: 0°F with 15 mph wind -> -19°F
    wc = units.heat_or_chill_c((0 - 32) * 5 / 9, None, 15 * 1.609344)
    assert wc is not None and round(units.c_to_f(wc)) == -19
    # NWS heat index chart: 90°F at 60% humidity -> 100°F
    hi = units.heat_or_chill_c((90 - 32) * 5 / 9, 60, 0)
    assert hi is not None and round(units.c_to_f(hi)) == 100
    assert units.heat_or_chill_c(15, 50, 10) is None  # mild: no adjustment


def test_clock_formats_are_platform_independent():
    t = datetime(2026, 11, 15, 15, 5, tzinfo=UTC)
    assert units.clock(t, "12h") == "3:05 PM"
    assert units.clock(t.replace(minute=0), "12h") == "3 PM"
    assert units.clock(t.replace(hour=0, minute=0), "12h") == "12 AM"
    assert units.clock(t, "24h") == "15:05"
    assert units.day_clock(t + timedelta(days=1), "12h", t) == "tomorrow 3:05 PM"
    assert units.day_clock(t + timedelta(days=3), "12h", t).startswith("Wed ")


def test_zone_falls_back_without_tz_database():
    assert units.zone("Not/AZone", -21600).utcoffset(None) == timedelta(hours=-6)
    assert units.zone(None) == UTC


def test_point_in_polygon_with_hole_and_multipolygon():
    square = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]
    hole = [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]
    poly = {"type": "Polygon", "coordinates": [square[0], hole]}
    assert geo.contains(poly, 2, 2)
    assert not geo.contains(poly, 5, 5)  # inside the hole
    assert not geo.contains(poly, 11, 5)
    multi = {"type": "MultiPolygon", "coordinates": [square, [[[20, 20], [30, 20], [30, 30], [20, 20]]]]}
    assert geo.contains(multi, 28, 22)
    assert not geo.contains({"type": "Point", "coordinates": [1, 1]}, 1, 1)
