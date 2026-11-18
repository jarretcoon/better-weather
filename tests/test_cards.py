"""What the chat cards say and how fresh their data is, replaying the recorded Des Moines / London answers."""

import copy
import json

import httpx
import pytest
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


# --- 3: an alert that hasn't started yet says when it starts ------------------------------------------------

DSM_ALERTS = "api.weather.gov/alerts/active?point=41.601%2C-93.609&status=actual"


def _nws_alert(event, onset, ends, ident="a1", area="Polk"):
    return {"type": "Feature", "properties": {
        "id": ident, "event": event, "severity": "Moderate", "status": "Actual", "messageType": "Alert",
        "onset": onset, "effective": "2026-11-15T02:00:00-06:00", "ends": ends, "expires": ends,
        "headline": f"{event} issued", "areaDesc": area}}


def test_an_alert_starting_later_reads_from_and_to(home):
    # recorded at 2:38 AM: one advisory runs 3-8 AM tomorrow, the other is on now
    serve({DSM_ALERTS: {"features": [
        _nws_alert("Wind Advisory", "2026-11-16T03:00:00-06:00", "2026-11-16T08:00:00-06:00"),
        _nws_alert("Dense Fog Advisory", "2026-11-15T01:00:00-06:00", "2026-11-15T09:00:00-06:00", "a2")]}})
    r = call("weather")
    assert "⚠️ **Wind Advisory** from tomorrow 3 AM to 8 AM" in r["card"]
    assert "⚠️ **Dense Fog Advisory** until 9 AM" in r["card"]
    wind = next(a for a in r["alerts"] if a["event"] == "Wind Advisory")
    assert wind["upcoming"] is True and wind["starts"] == "tomorrow 3 AM" and wind["until"] == "tomorrow 8 AM"
    fog = next(a for a in r["alerts"] if a["event"] == "Dense Fog Advisory")
    assert "upcoming" not in fog and "starts" not in fog


def test_the_us_alerts_card_says_when_a_later_alert_starts(home):
    from better_weather.core import places, report

    serve({DSM_ALERTS: {"features": [
        _nws_alert("Wind Advisory", "2026-11-16T03:00:00-06:00", "2026-11-16T08:00:00-06:00"),
        _nws_alert("Dense Fog Advisory", "2026-11-15T01:00:00-06:00", "2026-11-15T09:00:00-06:00", "a2")]}})
    r = report.alerts(places.resolve("Des Moines, IA"), None, "12h")
    assert "⚠️ **Wind Advisory** from tomorrow 3 AM to 8 AM" in r["card"]
    assert "⚠️ **Dense Fog Advisory** until 9 AM" in r["card"]
    assert [a.get("upcoming") for a in r["alerts"]] == [True, None]


def test_when_wording():
    from better_weather.core import display

    assert display._when({"until": "9 AM"}) == " until 9 AM"
    assert display._when({"upcoming": True, "starts": "3 PM", "ends": "Fri 4 AM"}) == " from 3 PM to Fri 4 AM"
    assert display._when({"upcoming": True, "starts": "Fri 03:00", "ends": "Fri 08:00"}) == " from Fri 03:00 to 08:00"
    assert display._when({"upcoming": True, "starts": "tomorrow 3 AM"}) == " from tomorrow 3 AM"
    assert display._when({"starts": "1 AM", "ends": "9 AM"}) == " until 9 AM"  # on now: no "from"
    card = display.alerts_card({"where": "Ojai, CA", "alerts": [
        {"event": "Wind Advisory", "upcoming": True, "starts": "tomorrow 3 AM", "ends": "tomorrow 8 AM"}]})
    assert "⚠️ **Wind Advisory** from tomorrow 3 AM to 8 AM" in card


def _ma_warning(event, level, regions, onset="2026-11-15T12:00:00Z", expires="2026-11-16T00:00:00Z"):
    return {"alert": {"msgType": "Alert", "info": [
        {"language": "en-GB", "event": event, "headline": event, "severity": "Severe", "expires": expires,
         "onset": onset, "senderName": "Met Service", "description": "Gusts to 100 km/h.",
         "parameter": [{"valueName": "awareness_level", "value": f"{level}; colour; x"}],
         "area": [{"areaDesc": r} for r in regions]}]}}


def _meteoalarm(warnings):
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"warnings": warnings})))


def test_european_warnings_keep_their_end_and_say_when_they_start(home):
    from better_weather.core import places, report

    _meteoalarm([_ma_warning("Gale-force gusts", 3, ["Berlin"], onset="2026-11-15T07:00:00Z"),
                 _ma_warning("Heavy rain", 2, ["Berlin"], onset="2026-11-15T15:00:00Z")])
    berlin = places.Place("Berlin, DE", 52.52, 13.41, country="DE", timezone="Europe/Berlin")
    r = report.alerts(berlin, None, "12h")  # 9:38 AM in Berlin
    assert "⚠️ **Gale-force gusts** until tomorrow 1 AM" in r["card"]  # on since 8 AM
    assert "⚠️ **Heavy rain** from 4 PM to tomorrow 1 AM" in r["card"]
    now = report.now(berlin, "us", "12h")  # the now card kept no end before
    assert "⚠️ **Gale-force gusts** until tomorrow 1 AM" in now["card"]
    assert "⚠️ **Heavy rain** from 4 PM to tomorrow 1 AM" in now["card"]
    card = report._intl_alerts(berlin, "12h")
    assert [a.get("upcoming") for a in card["alerts"]] == [None, True]


# --- 4: no tool names on cards, and a short MeteoAlarm note whose areas the alerts card names -----------------

def test_the_alerts_card_lists_orange_and_red_elsewhere_compactly(home):
    from better_weather.core import places, report

    many = [f"Kreis {n}" for n in "ABCDEFGHIJK"]  # 11 areas: MeteoAlarm keeps 8 names, counts the rest
    _meteoalarm([_ma_warning("gale-force gusts", 3, ["Berlin"]),
                 _ma_warning("heavy persistent rain", 4, many[:9]),
                 _ma_warning("heavy persistent rain", 4, ["Kreis Calw", "Kreis A"]),
                 _ma_warning("storm-force gusts", 3, ["Kreis Miesbach"]),
                 _ma_warning("thunderstorms", 2, ["Kreis Z"])])  # yellow: not counted
    berlin = places.Place("Berlin, State of Berlin, DE", 52.52, 13.41, country="DE", timezone="Europe/Berlin")
    r = report.alerts(berlin, None, "12h")
    assert r["card"].splitlines() == [
        "**Alerts · Berlin, State of Berlin, DE**",
        "⚠️ **Gale-force gusts** from 1 PM to tomorrow 1 AM",
        "⚠️ Germany lists warnings by area name: only ones naming Berlin or its area are shown "
        "(3 orange/red elsewhere)",
        "🔴 Heavy persistent rain · Kreis A, Kreis B +8",
        "🟠 Storm-force gusts · Kreis Miesbach",
    ]
    assert all(len(line) <= 110 for line in r["card"].splitlines())
    now = report.now(berlin, "us", "12h")
    assert "(3 orange/red elsewhere)" in now["card"] and "Kreis" not in now["card"]


def test_no_tool_names_reach_a_person():
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "core"
    for name in ("report.py", "display.py", "climate.py", "sources/intl_alerts.py"):
        text = (root / name).read_text(encoding="utf-8")
        quoted = re.findall(r'"[^"\n]*"', text)
        leaks = [q for q in quoted if re.search(r"\bweather_(alerts|forecast|severe|places|air|history)\b", q)]
        assert not leaks, (name, leaks)


# --- 5: MeteoAlarm's list says when it's getting old ---------------------------------------------------------

def test_an_old_meteoalarm_copy_is_flagged_and_keeps_its_real_age(home):
    import time

    from better_weather.core import cache, places, report
    from better_weather.core.sources import intl_alerts

    url = intl_alerts.METEOALARM.format(slug="germany")
    feed = {"warnings": [_ma_warning("gale-force gusts", 3, ["Berlin"])]}
    cache.get_cache().put(http.cache_key(url, None), 200, json.dumps(feed), time.time() - 25 * 60)
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(503, json={})))  # MeteoAlarm down
    berlin = places.Place("Berlin, DE", 52.52, 13.41, country="DE", timezone="Europe/Berlin")
    r = report.alerts(berlin, None, "12h")
    note = "⚠️ MeteoAlarm's list is from 25 min ago; newer warnings may be missing"
    assert note in r["notes"] and note in r["card"] and "Gale-force gusts" in r["card"]
    # the in-process copy keeps MeteoAlarm's own time, not the moment it was read: it's due a refresh already
    assert time.time() - intl_alerts._feeds[url][0] >= 25 * 60 - 5


def test_a_fresh_meteoalarm_list_needs_no_note(home):
    from better_weather.core import places, report

    _meteoalarm([_ma_warning("gale-force gusts", 3, ["Berlin"])])
    r = report.alerts(places.Place("Berlin, DE", 52.52, 13.41, country="DE", timezone="Europe/Berlin"), None, "12h")
    assert not any("MeteoAlarm's list" in n for n in r["notes"])


# --- 6: a hanging source can't hold the answer up for minutes -------------------------------------------------

def test_one_deadline_for_all_of_an_answers_fetches(home, monkeypatch):
    import threading
    import time

    from better_weather.core import report
    from better_weather.core.sources import nws

    hang = threading.Event()

    def stuck(*_a, **_k):
        hang.wait(10)
        raise http.FetchError("https://api.weather.gov/", "hung")

    monkeypatch.setattr(report, "REPORT_WAIT_S", 0.5)
    monkeypatch.setattr(nws, "forecast", stuck)
    monkeypatch.setattr(nws, "alerts_at", stuck)
    monkeypatch.setattr(nws, "latest_observation", stuck)
    try:
        t = time.monotonic()
        r = call("weather")
        took = time.monotonic() - t
    finally:
        hang.set()
    # three sources hung: one shared half second in all, not half a second each
    assert took < 0.9, took
    assert {"NWS observation timed out", "NWS forecast timed out", "NWS alerts timed out"} <= set(r["notes"])
    assert r["current"]["source"] == "Open-Meteo model estimate" and "⚠️ Alerts could not be checked" in r["card"]


def test_interactive_fetches_try_twice_with_shorter_timeouts():
    seen = []

    def down(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(503, json={})

    http.set_transport_for_tests(httpx.MockTransport(down))
    with http.interactive(), pytest.raises(http.FetchError):
        http.get_json("https://example.test/slow", ttl=60)
    assert len(seen) == 2 and seen[0]["read"] == http.INTERACTIVE_TIMEOUT.read
    seen.clear()
    with pytest.raises(http.FetchError):  # outside (the cron pushes, background refreshes): the patient defaults
        http.get_json("https://example.test/slow2", ttl=60)
    assert len(seen) == http.ATTEMPTS == 3 and seen[0]["read"] == http.TIMEOUT.read


# --- 8: the hourly card's rows span the hours its header names ---------------------------------------------

def test_the_72_hour_card_reaches_the_third_day(home):
    r = call("weather_forecast", hourly=True, hours=72)
    lines = r["card"].splitlines()
    assert lines[0] == "**Des Moines, IA** · next 72 hours" and len(lines) == 19  # every 4th hour
    assert lines[-1].startswith(r["hours"][68]["time"] + " ")  # 68 h out, not 45
    for hours, rows in ((12, 12), (24, 12), (48, 16), (54, 18)):
        assert len(call("weather_forecast", hourly=True, hours=hours)["card"].splitlines()) == rows + 1


# --- 9: a statewide list says where each alert is -------------------------------------------------------------

def test_statewide_alerts_say_where(home):
    area = ("West Slope Northern Sierra Nevada; Western Plumas County/Lassen Park; Lake County; "
            "Northern Sacramento Valley")
    storms = {"features": [
        _nws_alert("Winter Storm Warning", "2026-11-15T01:00:00-06:00", "2026-11-17T04:00:00-06:00", "w1", area),
        _nws_alert("Winter Storm Warning", "2026-11-15T01:00:00-06:00", "2026-11-17T04:00:00-06:00", "w2", "Mono")]}
    serve({"api.weather.gov/alerts/active?area=IA&status=actual": storms, DSM_ALERTS: storms})
    card = call("weather_alerts", state="IA")["card"].splitlines()
    assert card[3] == "   📍 West Slope Northern Sierra Nevada; Western Plumas County/Lassen Park +2"
    assert card[6] == "   📍 Mono"
    here = call("weather_alerts")["card"]
    assert "Winter Storm Warning" in here and "📍" not in here  # one place: no area line


# --- 10: smaller ones -------------------------------------------------------------------------------------------

def test_a_trace_of_snow_or_rain_is_left_out_not_shown_as_zero():
    from zoneinfo import ZoneInfo

    from better_weather.core import report

    us, metric = report._Fmt("us", "12h", ZoneInfo("UTC")), report._Fmt("metric", "12h", ZoneInfo("UTC"))
    assert us.snow(0.1) is None and us.snow(0.0) is None and us.snow(0.3) == "0.1 in"
    assert metric.snow(0.04) is None and metric.snow(0.3) == "0.3 cm"
    assert us.precip(0.1) is None and us.precip(0.3) == "0.01 in"
    assert metric.precip(0.04) is None and metric.precip(0.3) == "0.3 mm"


def test_a_forecast_that_starts_tonight_ends_without_a_dangling_low(home):
    _at("2026-11-16T00:30:00+00:00")  # 6:30 PM: "Sunday Night" leads, the list ends on a day
    lines = call("weather_forecast")["card"].splitlines()
    assert lines[1].startswith("**Sunday Night** ") and " low " in lines[1]
    assert not any("/ –" in line for line in lines)
    assert " high · " in lines[-1]


def test_a_hung_station_server_is_not_asked_three_times_over(home):
    asked = []

    def kdsm_fails(how):
        def override(request):
            if request.url.path.endswith("/observations/latest"):
                asked.append(request.url.path.split("/")[2])
                if "KDSM" in request.url.path:
                    if how == "hang":
                        raise httpx.ReadTimeout("hung", request=request)
                    return httpx.Response(500, json={})
            return None
        return override

    http.set_transport_for_tests(replay_transport(kdsm_fails("hang")))
    call("weather")
    assert set(asked) == {"KDSM"}  # no answer at all: the other stations are on the same server
    asked.clear()
    from better_weather.core import cache

    cache.use_memory_cache_for_tests()
    http.set_transport_for_tests(replay_transport(kdsm_fails("500")))
    call("weather")
    assert "KIKV" in asked  # one station's own error: the next one is tried


def test_the_severe_answer_says_when_a_later_alert_starts():
    from better_weather.core import report

    text = report.severe_text({"storm_alerts": [
        {"event": "Flash Flood Watch", "upcoming": True, "starts": "tomorrow 6 AM", "until": "tomorrow 6 PM"},
        {"event": "Severe Thunderstorm Warning", "until": "4:45 PM", "life_threatening": True}]})
    assert text.splitlines() == ["⚠️ Flash Flood Watch from tomorrow 6 AM to 6 PM",
                                 "🚨 Severe Thunderstorm Warning until 4:45 PM"]
