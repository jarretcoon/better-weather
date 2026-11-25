"""0.9.4: error pages stay out of notes, each hour's day or night from the sun, worded alert times also as ISO."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
from better_weather.core import http, report


def test_an_error_page_never_becomes_words_in_a_note():
    page = httpx.Response(403, text="<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>…</BODY></HTML>")
    assert http._problem_detail(page) == ""
    assert http._problem_detail(httpx.Response(503, json={"detail": "Grid expired"})) == "Grid expired"
    assert http._problem_detail(httpx.Response(500, text="upstream timeout")) == "upstream timeout"


def _fmt(when: str) -> "report._Fmt":
    fmt = report._Fmt("us", "12h", ZoneInfo("America/Chicago"))
    fmt.now = datetime.fromisoformat(when)
    return fmt


def test_nws_hours_take_day_or_night_from_the_sun_not_6_to_6():
    # Des Moines, 2026-11-24: sunrise 7:15, sunset 4:47. The NWS calls 5 PM and 6 AM "daytime".
    fmt = _fmt("2026-11-24T16:10:00-06:00")
    periods = [{"startTime": f"2026-11-24T{h:02d}:00:00-06:00", "endTime": f"2026-11-24T{h + 1:02d}:00:00-06:00",
                "isDaytime": 6 <= h < 18, "temperature": 40} for h in (16, 17)]
    rows = report._nws_hours(SimpleNamespace(data={"properties": {"periods": periods}}), fmt, 4, 41.587, -93.625)
    assert [r["_day"] for r in rows] == [True, False], "4–5 PM is day (sunset 4:47), 5–6 PM is night"
    no_place = report._nws_hours(SimpleNamespace(data={"properties": {"periods": periods}}), fmt, 4)
    assert [r["_day"] for r in no_place] == [True, True], "without a place, the NWS's own flag"


def test_model_hours_without_sun_times_use_the_sun_at_the_place():
    assert report._is_daytime(None, 17, datetime(2026, 11, 24, 17, tzinfo=ZoneInfo("America/Chicago")),
                              41.587, -93.625) is False
    assert report._is_daytime(None, 12, datetime(2026, 11, 24, 12, tzinfo=ZoneInfo("America/Chicago")),
                              41.587, -93.625) is True
    assert report._is_daytime(("07:15", "16:47"), 17) is False
    # polar night: Open-Meteo gives the same time for both; the sun decides (Utqiagvik, noon, 2026-12-21)
    assert report._is_daytime(("00:00", "00:00"), 12, datetime(2026, 12, 21, 12, tzinfo=ZoneInfo("America/Anchorage")),
                              71.29, -156.79) is False


def test_worded_alert_times_also_come_as_iso():
    item = {"starts": "2026-11-25T03:00:00+01:00", "ends": "2026-11-25T18:00:00+01:00"}
    report._iso_times(item, _fmt("2026-11-24T16:10:00-06:00"))
    assert item["starts_iso"] == "2026-11-25T03:00:00+01:00" and item["ends_iso"] == "2026-11-25T18:00:00+01:00"
    empty: dict = {"starts": None}
    report._iso_times(empty, _fmt("2026-11-24T16:10:00-06:00"))
    assert "starts_iso" not in empty
