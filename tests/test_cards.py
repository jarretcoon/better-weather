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
