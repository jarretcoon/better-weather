import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from better_weather.core import climate, http, places
from better_weather.core.sources import acis


def _place(country="US"):
    return places.Place("Des Moines, IA", 41.6, -93.61, country=country, timezone="America/Chicago")


def test_aqi_and_uv_bands():
    assert climate.band(42, climate.US_AQI_BANDS) == "Good"
    assert climate.band(101, climate.US_AQI_BANDS) == "Unhealthy for sensitive groups"
    assert climate.band(350, climate.US_AQI_BANDS) == "Hazardous"
    assert climate.band(8, climate.UV_BANDS) == "Very high"
    assert climate.pollen_level(0) == "none" and climate.pollen_level(150) == "high"


def test_uv_level_rounds_to_the_whole_index_first():
    assert [climate.uv_level(v) for v in (2.2, 2.5, 5.4, 5.5, 7.3, 10.4, 10.6, 0.0)] == [
        "Low", "Moderate", "Moderate", "High", "High", "Very high", "Extreme", "Low"]
    assert climate.uv_level(None) is None


def test_air_bands_the_rounded_uv():
    payload = {"timezone": "America/Chicago", "utc_offset_seconds": -21600, "current": {"uv_index": 2.2},
               "hourly": {"time": [], "us_aqi": [], "uv_index": []}}
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    assert climate.air(_place(), "12h")["uv_now"] == {"index": 2.2, "level": "Low"}


def test_air_flags_smoke(monkeypatch):
    payload = {"timezone": "America/Chicago", "utc_offset_seconds": -21600,
               "current": {"us_aqi": 160, "pm2_5": 70.0, "ozone": 40, "uv_index": 1.0, "us_aqi_pm2_5": 160,
                           "us_aqi_ozone": 30},
               "hourly": {"time": [], "us_aqi": [], "uv_index": []}}
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    r = climate.air(_place(), "12h")
    assert r["air_quality"]["category"] == "Unhealthy"
    assert r["air_quality"]["main_pollutant"].startswith("fine particles")
    assert any("smoke" in n for n in r["notes"])


def test_parse_day():
    today = date(2026, 11, 15)
    assert climate.parse_day("yesterday", today) == date(2026, 11, 14)
    assert climate.parse_day("3 days ago", today) == date(2026, 11, 12)
    assert climate.parse_day("2025-07-04", today) == date(2025, 7, 4)
    with pytest.raises(ValueError):
        climate.parse_day("last tuesday-ish", today)


def test_acis_value_parsing():
    assert acis.value("M") is None and acis.value("T") == 0.0
    assert acis.value("0.12A") == 0.12 and acis.value("-3") == -3.0
    assert acis.candidates("KDSM") == ["DSMthr", "DSM"] and acis.candidates("PAFA") == ["PAFA"]


def _history_transport(acis_daily, acis_years, model_daily=None):
    def handler(request):
        url = str(request.url)
        if "/points/" in url:
            return httpx.Response(200, json={"properties": {
                "observationStations": "https://api.weather.gov/gridpoints/DMX/1,1/stations",
                "timeZone": "America/Chicago"}})
        if "/stations" in url:
            return httpx.Response(200, json={"features": [
                {"properties": {"stationIdentifier": "KDSM", "name": "Des Moines"},
                 "geometry": {"coordinates": [-93.65, 41.53]}}]})
        if "rcc-acis" in url:
            params = json.loads(parse_qs(urlparse(url).query)["params"][0])
            meta = {"name": "Des Moines Area"}
            if params["elems"] == "maxt":
                return httpx.Response(200, json={"meta": meta, "data": [[params["sdate"], "50"]]})
            if params["sdate"].startswith("1850"):
                return httpx.Response(200, json={"meta": meta, "data": acis_years})
            return httpx.Response(200, json={"meta": meta, "data": acis_daily})
        if "open-meteo" in url:
            return httpx.Response(200, json={"daily": model_daily or {"time": []}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_history_prefers_station_records_and_reports_normals_and_records():
    http.set_transport_for_tests(_history_transport(
        acis_daily=[["2026-11-10", "65", "39", "0.25", "T", "51", "32"]],
        acis_years=[["1950-11-10", "75", "10"], ["1991-11-10", "60", "-2"], ["2026-11-10", "99", "-40"]]))
    r = climate.history(_place(), "2026-11-10", None, "us")
    day = r["days"][0]
    assert day == {"date": "2026-11-10", "high": 65, "low": 39, "precip": "0.25 in"}
    comp = r["normals_and_records"]
    assert comp["normal_high"] == 51 and comp["high_vs_normal"] == 14
    assert comp["record_high"] == "75° in 1950" and comp["record_low"] == "-2° in 1991"  # 2026 not final
    assert r["sources"][0].startswith("NOAA station records")


def test_history_fills_missing_station_days_with_marked_estimates():
    http.set_transport_for_tests(_history_transport(
        acis_daily=[["2026-11-13", "42", "25", "0.00", "0.0", "49", "31"],
                    ["2026-11-14", "M", "M", "M", "M", "49", "31"]],
        acis_years=[],
        model_daily={"time": ["2026-11-14"], "temperature_2m_max": [7.8], "temperature_2m_min": [-3.9],
                     "precipitation_sum": [0.0], "snowfall_sum": [0.0]}))
    r = climate.history(_place(), "2026-11-13", "2026-11-14", "us")
    assert r["days"][0].get("estimated") is None and r["days"][1]["estimated"] is True
    assert r["days"][1]["high"] == 46 and r["totals"]["coldest"] == 25


def test_history_limits():
    with pytest.raises(ValueError, match="future"):
        climate.history(_place(), "2099-01-01", None, "us")
    with pytest.raises(ValueError, match="31 days"):
        climate.history(_place(), "2026-01-01", "2026-03-01", "us")


def test_peak_gives_its_time_unformatted_too():
    from datetime import datetime, timedelta, timezone

    from better_weather.core import climate

    tz = timezone(timedelta(hours=-6))
    now = datetime(2026, 11, 17, 12, 0, tzinfo=tz)
    hourly = {"time": ["2026-11-17T13:00", "2026-11-17T19:00", "2026-11-18T09:00"], "us_aqi": [20, 69, 40]}
    peak = climate._peak(hourly, "us_aqi", now, tz, "12h")
    assert peak["value"] == 69 and peak["time"] == "2026-11-17T19:00:00-06:00"
    assert "7" in peak["at"], "the formatted time is still there for the plugin's own words"


def _model_transport(calls):
    def handler(request):
        url = str(request.url)
        calls.append(url)
        params = parse_qs(urlparse(url).query)
        if "archive-api" in url:
            days = [params["start_date"][0], params["end_date"][0]]
        else:  # the forecast API: the last few days and today
            days = ["2026-11-13", "2026-11-14", "2026-11-15"]
        return httpx.Response(200, json={"daily": {
            "time": days, "temperature_2m_max": [10.0] * len(days), "temperature_2m_min": [1.0] * len(days),
            "precipitation_sum": [0.0] * len(days), "snowfall_sum": [0.1] * len(days)}})

    return httpx.MockTransport(handler)


def test_history_says_which_model_each_day_came_from():
    paris = places.Place("Paris, FR", 48.85, 2.35, country="FR", timezone="Europe/Paris")  # today: 2026-11-15
    calls = []
    http.set_transport_for_tests(_model_transport(calls))
    old = climate.history(paris, "2026-11-01", None, "us")
    assert old["sources"] == [climate.ERA5_SOURCE] and "snow" not in old["days"][0]  # 0.1 cm: a trace
    recent = climate.history(paris, "2026-11-13", None, "us")
    assert recent["sources"] == [climate.RECENT_SOURCE] and climate.TODAY_NOTE not in recent.get("notes", [])
    today = climate.history(paris, "today", None, "us")
    assert today["sources"] == [climate.RECENT_SOURCE] and climate.TODAY_NOTE in today["notes"]
    both = climate.history(paris, "2026-11-08", "2026-11-14", "us")
    assert both["sources"] == [climate.ERA5_SOURCE, climate.RECENT_SOURCE]
