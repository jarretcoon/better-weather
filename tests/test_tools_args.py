"""Tool arguments: a user's slip gets a plain answer, never "a bug in Better Weather"."""

import json

import pytest
from better_weather.core import tools


def call(tool, **args):
    return json.loads(dict(tools.TOOLS)[tool](args))


@pytest.mark.parametrize(("tool", "args"), [
    ("weather_forecast", {"days": "five"}),
    ("weather_forecast", {"hourly": True, "hours": "a few"}),
    ("weather_confidence", {"days": 2.5}),
    ("weather_best_time", {"activity": "run", "days": "x"}),
    ("weather_scorecard", {"days": True}),
    ("weather_science", {"hours_ahead": "soon"}),
])
def test_a_number_that_isnt_one_is_a_plain_mistake(home, tool, args):
    r = call(tool, **args)
    name = next(k for k in args if k not in {"activity", "hourly"})
    assert r["error"] == f"{name} must be a whole number, like 3." and "bug" not in json.dumps(r)


def test_numbers_and_yes_no_given_as_text(home):
    r = call("weather_forecast", days="9", hourly="false")  # the same answer as days=9
    assert len(r["periods"]) == 14 and "hours" not in r


def test_science_model_names(monkeypatch):
    from better_weather.core import places, science

    seen = []
    monkeypatch.setattr(places, "resolve", lambda *a, **k: places.Place("X", 41.6, -93.6, country="US"))
    monkeypatch.setattr(science, "sounding", lambda place, u, c, **kw: seen.append(kw) or {"ok": True})
    assert call("weather_science", model="HRRR", hours_ahead="6", at_peak="no") == {"ok": True}
    assert seen == [{"model": "hrrr", "hours_ahead": 6, "at_peak": False}]
    assert call("weather_science", model="nam")["error"] == "model must be 'gfs' or 'hrrr'."
