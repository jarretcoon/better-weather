"""What the chat cards say and how fresh their data is, replaying the recorded Des Moines / London answers."""

import copy
import json

import httpx
from better_weather.core import http, tools
from fixturekit import FIXTURES, load_index, replay_transport

KDSM_OBS = "api.weather.gov/stations/KDSM/observations/latest"
DSM_STATIONS = "api.weather.gov/gridpoints/DMX/74,50/stations?limit=5"


def call(tool, **args):
    out = dict(tools.TOOLS)[tool](args)
    assert isinstance(out, str)
    return json.loads(out)


def fixture(key):
    return json.loads((FIXTURES / str(load_index()[key]["file"])).read_text(encoding="utf-8"))


def serve(changes, log=None):
    """Replay the fixtures, with *changes* {request key: edited JSON, or a function editing a copy}."""

    def override(request):
        from fixturekit import request_key

        key = request_key(request)
        if key not in changes:
            return None
        change = changes[key]
        body = change(copy.deepcopy(fixture(key))) if callable(change) else change
        return httpx.Response(200, json=body)

    http.set_transport_for_tests(replay_transport(override, log))


def _obs(**props):
    def edit(data):
        for k, v in props.items():
            data["properties"][k] = v if isinstance(v, dict) or v is None else {"value": v}
        return data
    return edit


# --- 1: which station speaks for the place ----------------------------------------------------------------------

def test_a_station_a_few_miles_off_is_named_on_the_card(home):
    r = call("weather")
    assert r["current"]["source"] == "NWS station report" and r["current"]["station_distance"] == "5 mi"
    temp_line = r["card"].splitlines()[1]
    assert temp_line.endswith("· KDSM 44 min ago"), temp_line  # 8 km from downtown, report from 7:54 UTC


def test_a_station_far_above_the_place_gives_way_to_the_model(home):
    log = []
    serve({KDSM_OBS: _obs(elevation=1015)}, log)  # Rose Valley over Ojai: 750 m above the place
    r = call("weather")
    assert r["current"]["source"] == "Open-Meteo model estimate"
    assert r["card"].splitlines()[1].endswith("· model estimate")
    # the next stations within 25 km were tried; the ones 40-50 km away weren't even asked
    asked = [k.split("/")[2] for k in log if k.endswith("/observations/latest")]
    assert asked == ["KDSM", "KIKV"]


def test_a_station_across_the_mountains_is_never_asked(home):
    def far_first(data):
        ponce = copy.deepcopy(data["features"][0])
        ponce["properties"]["stationIdentifier"] = "TJPS"
        ponce["geometry"]["coordinates"] = [-94.45, 41.6]  # 70 km west
        data["features"].insert(0, ponce)
        return data

    log = []
    serve({DSM_STATIONS: far_first}, log)
    r = call("weather")
    assert not any("TJPS" in k for k in log)
    assert r["current"]["station"].startswith("KDSM")


def test_station_next_door_with_a_fresh_report_needs_no_label(home):
    from better_weather.core import report

    assert report._reading_from({"station": "KDSM", "distance_km": 2.0, "age_min": 20}) is None
    assert report._reading_from({"station": "KDSM", "distance_km": 2.0, "age_min": 82}) == "KDSM 82 min ago"
    assert report._reading_from({"station": "TJSJ", "distance_km": 11.0, "age_min": 3}) == "TJSJ 3 min ago"


def test_no_wind_in_the_report_falls_back_to_the_models_wind(home):
    serve({KDSM_OBS: _obs(windSpeed=None, windDirection=None, windGust=None)})
    r = call("weather")
    assert r["current"]["source"] == "NWS station report"
    assert r["current"]["wind"] and "💨" in r["card"]


def test_a_wind_chill_of_exactly_zero_is_kept(home):
    serve({KDSM_OBS: _obs(windChill=0.0, temperature=1.0)})
    r = call("weather")
    assert r["current"]["feels_like"] == "32°F"


# --- 2: nothing that has ended shows as if it were ahead ------------------------------------------------------

def _at(iso):
    from datetime import datetime

    from better_weather.core import clock

    clock.freeze_for_tests(datetime.fromisoformat(iso))


def test_ended_forecast_periods_are_dropped(home):
    _at("2026-11-16T03:41:00+00:00")  # 9:41 PM in Des Moines: "Overnight" and "Sunday" are over
    r = call("weather")
    assert r["forecast"][0]["name"] == "Sunday Night"
    assert "**Sunday** " not in r["card"] and "**Sunday Night**" in r["card"]
    daily = call("weather_forecast")
    assert daily["periods"][0]["name"] == "Sunday Night"


def test_yesterday_is_not_shown_as_the_first_day(home):
    _at("2026-11-16T00:20:00+00:00")  # just after midnight in London; the copy starts on the 15th
    r = call("weather_forecast", location="London, UK", days=5)
    assert r["days"][0]["day"] == "Mon Nov 16" and len(r["days"]) == 4
    now = call("weather", location="London, UK")
    assert now["forecast"][0]["day"] == "Mon Nov 16"
    assert "Sun Nov 15" not in now["card"]


def _stale(key_url, hours, log=None):
    """A copy of *key_url* saved *hours* ago, and the live source failing."""
    import time

    from better_weather.core import cache

    key = key_url.split("?")[0]
    cache.get_cache().put("https://" + key_url, 200, json.dumps(fixture(key_url)), time.time() - hours * 3600)

    def down(request):
        return httpx.Response(503, json={}) if str(request.url).startswith("https://" + key) else None

    http.set_transport_for_tests(replay_transport(down, log))


def test_an_old_saved_forecast_is_pointed_out_on_the_card(home):
    _stale("api.weather.gov/gridpoints/DMX/74,50/forecast", 3)
    r = call("weather")
    warning = "⚠️ NWS forecast could not be refreshed; showing a copy from 3 hr ago"
    assert warning in r["notes"] and warning in r["card"]
    assert warning in call("weather_forecast")["card"]


def test_a_recent_saved_copy_stays_off_the_card(home):
    om = next(k for k in load_index() if "forecast_days=3" in k and "latitude=41.6&" in k)
    _stale(om, 20 / 60)  # Open-Meteo's copies count as fresh for 15 min
    r = call("weather")
    assert "Open-Meteo could not be refreshed; showing a copy from 20 min ago" in r["notes"]
    assert "could not be refreshed" not in r["card"]
