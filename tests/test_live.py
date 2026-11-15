"""Live checks against the real services (nightly in CI): `pytest -m live`."""

import json

import pytest
from better_weather.core import commands, settings, tools

pytestmark = pytest.mark.live


def _call(name, **args):
    return json.loads(dict(tools.TOOLS)[name](args))


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
