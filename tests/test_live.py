"""Live checks against the real services (nightly in CI): `pytest -m live`."""

import json
import time

import pytest
from better_weather.core import commands, settings, tools

pytestmark = pytest.mark.live

# GitHub's runners often can't reach a source at all (a TLS handshake that times out), which says nothing about the
# plugin. Such a call is tried once more, then the test is skipped, so the nightly run stays red only for real changes.
_UNREACHABLE = ("network error", "timed out", "Timeout", "unavailable right now", "Weather service unavailable")


def _unreachable(r):
    return isinstance(r, dict) and "error" in r and any(s in str(r["error"]) for s in _UNREACHABLE)


def _call(name, **args):
    r = json.loads(dict(tools.TOOLS)[name](args))
    if _unreachable(r):
        time.sleep(20)
        r = json.loads(dict(tools.TOOLS)[name](args))
        if _unreachable(r):
            pytest.skip(f"{name}: source unreachable from this runner ({r['error']})")
    return r


def test_live_us_brief():
    settings.current().set("home", "Des Moines, IA")
    r = _call("weather")
    assert "error" not in r, r
    assert r["current"].get("temp")
    assert "NWS alerts" in r["sources"], r.get("notes")


def test_live_abroad():
    r = _call("weather_forecast", location="London, UK", days=3)
    assert "error" not in r and len(r["days"]) == 3


def test_live_severe_and_alerts():
    settings.current().set("home", "Des Moines, IA")
    assert len(_call("weather_severe")["spc_outlook"]) == 3
    assert "count" in _call("weather_alerts", state="IA")


def test_live_doctor():
    result = commands.doctor()
    assert result["ok"], json.dumps(result, indent=1, default=str)


def test_live_pictures(tmp_path):
    settings.set_data_dir(tmp_path)
    try:
        settings.current().set("home", "Des Moines, IA")
        radar = _call("weather_radar")
        sat = _call("weather_satellite")
        assert "error" not in radar and radar["media"].startswith("MEDIA:"), radar
        assert "error" not in sat and sat["media"].startswith("MEDIA:"), sat
    finally:
        settings.set_data_dir(None)


def test_live_air_and_history():
    settings.current().set("home", "Des Moines, IA")
    air = _call("weather_air")
    assert air.get("air_quality", {}).get("index") is not None, air
    hist = _call("weather_history", date="2025-07-04")
    assert hist["days"] and hist["normals_and_records"]["record_high"], hist


def test_live_confidence_discussion_best_time():
    settings.current().set("home", "Des Moines, IA")
    conf = _call("weather_confidence", days=3)
    assert conf["days"] and conf["days"][0]["runs"] >= 30, conf
    disc = _call("weather_discussion")
    assert disc.get("sections"), disc
    best = _call("weather_best_time", activity="walk")
    assert "best_windows" in best, best


def test_live_travel_and_water():
    settings.current().set("home", "Des Moines, IA")
    trip = _call("weather_travel", airport="DSM")
    assert trip["airport"]["now"].get("flight_category"), trip
    river = _call("weather_water")
    assert "rivers" in river, river


def test_live_science():
    settings.current().set("home", "Des Moines, IA")
    r = _call("weather_science")
    assert "error" not in r and r["parcels"]["surface_based"]["cape_jkg"] >= 0, r
