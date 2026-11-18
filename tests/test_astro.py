"""The sun and moon math (core/astro.py) against the US Naval Observatory: moonrise and moonset to within 3 minutes
for a mid-latitude US city, the far north (the moon up all day, the sun not at all) and two southern cities, and the
moon's phases to within half an hour. The same table as weather.lou.zone's tests/astro.test.mjs."""

import re
from datetime import UTC, datetime

import pytest
from better_weather.core import astro

# aa.usno.navy.mil/api/rstt/oneday, local standard time; a missing rise or set: none that day
PLACES = {"dsm": (41.587, -93.625, -6), "utq": (71.291, -156.789, -9), "ush": (-54.811, -68.316, -3),
          "syd": (-33.868, 151.209, 11)}
USNO = {
    "dsm 2026-11-17": {"rise": "13:13"}, "dsm 2026-11-24": {"set": "07:32", "rise": "16:34"},
    "dsm 2026-12-01": {"set": "12:50"},
    "utq 2026-11-17": {"rise": "16:04"}, "utq 2026-11-24": {}, "utq 2026-12-01": {"rise": "00:21", "set": "14:27"},
    "ush 2026-11-17": {"set": "02:51", "rise": "12:32"}, "ush 2026-11-24": {"set": "04:14", "rise": "22:47"},
    "ush 2026-12-01": {"rise": "02:14", "set": "13:27"},
    "syd 2026-11-17": {"set": "01:09", "rise": "11:58"}, "syd 2026-11-24": {"set": "04:40", "rise": "19:34"},
    "syd 2026-12-01": {"rise": "00:55", "set": "12:24"},
}


def _ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _minutes(hm: str) -> int:
    return int(hm[:2]) * 60 + int(hm[3:])


@pytest.mark.parametrize("key", sorted(USNO))
def test_moonrise_and_moonset_match_the_naval_observatory(key):
    place, date = key.split()
    lat, lon, tz = PLACES[place]
    start = _ts(f"{date}T00:00:00Z") - tz * 3600
    got: dict[str, str] = {}
    for kind, t in astro.moon_events(start, start + astro.DAY, lat, lon):
        got.setdefault(kind, datetime.fromtimestamp(t + tz * 3600, UTC).strftime("%H:%M"))
    for kind in ("rise", "set"):
        if kind not in USNO[key]:
            assert kind not in got, f"no moon{kind} that day"
        else:
            assert abs(_minutes(got[kind]) - _minutes(USNO[key][kind])) <= 3, (kind, got, USNO[key])


def test_the_far_north_moon_up_all_day_sun_down():
    lat, lon, _ = PLACES["utq"]
    noon = _ts("2026-11-24T21:00:00Z")
    assert astro.moon_above_horizon(noon, lat, lon) > 0
    assert astro.moon_pass(noon, lat, lon)["up"] is True
    assert astro.sun_altitude(noon, lat, lon) < 0


def test_phases_fall_within_half_an_hour_of_the_naval_observatory():
    for at, phase in (("2026-11-17T11:48Z", 0.25), ("2026-11-24T14:53Z", 0.5), ("2026-12-01T06:08Z", 0.75),
                      ("2026-12-09T00:52Z", 0)):
        got = astro.next_phase(_ts(at) - 5 * astro.DAY, phase)
        assert abs(got - _ts(at)) <= 30 * 60, (phase, at)
    assert astro.moon_phase_at(_ts("2026-11-24T14:53Z"))[1] > 0.999
    assert astro.moon_phase_at(_ts("2026-12-09T00:52Z"))[1] < 0.001


def test_the_next_phase_is_always_after_now():
    full = astro.next_phase(_ts("2026-11-20T00:00Z"), 0.5)
    after = astro.next_phase(full + 60, 0.5)
    assert full + 25 * astro.DAY < after < full + 32 * astro.DAY


def test_sunrise_and_sunset_worked_out_locally_match_the_naval_observatory():
    # USNO, Des Moines 2026-11-17 (CST): sunrise 7:06, sunset 16:52
    start = _ts("2026-11-17T06:00:00Z")
    events = astro.sun_events(start, start + astro.DAY, 41.587, -93.625)
    got = {k: datetime.fromtimestamp(v - 6 * 3600, UTC).strftime("%H:%M") for k, v in events}
    assert abs(_minutes(got["rise"]) - _minutes("07:06")) <= 2 and abs(_minutes(got["set"]) - _minutes("16:52")) <= 2
    assert astro.sun_events(_ts("2026-12-21T09:00:00Z"), _ts("2026-12-22T09:00:00Z"), 71.291, -156.789) == []


def test_names_and_emoji():
    day = _ts("2026-11-17T06:00:00Z")  # Des Moines' Nov 17 (first quarter at 11:48 UTC)
    assert astro.moon_name(day + 12 * 3600, day, day + astro.DAY) == "First quarter"
    assert astro.moon_name(_ts("2026-11-20T18:00:00Z"), _ts("2026-11-20T06:00:00Z"),
                           _ts("2026-11-21T06:00:00Z")) == "Waxing gibbous"
    assert astro.moon_emoji("Waxing gibbous", 41.6) == "🌔"
    assert astro.moon_emoji("Waxing gibbous", -33.9) == "🌖", "the south sees it mirrored"
    assert astro.moon_emoji("Full moon", 41.6) == "🌕" and astro.moon_emoji("First quarter", -33.9) == "🌗"


# ---------------------------------------------------------------- the now card's sun and moon lines


def _sky_at(when_utc: str, lat=41.587, lon=-93.625, tz="America/Chicago"):
    from zoneinfo import ZoneInfo

    from better_weather.core import report
    from better_weather.core.places import Place

    fmt = report._Fmt("us", "12h", ZoneInfo(tz))
    fmt.now = datetime.fromisoformat(when_utc.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    daily = {"time": ["2026-11-17", "2026-11-18", "2026-11-19"],
             "sunrise": ["2026-11-17T07:06", "2026-11-18T07:07", "2026-11-19T07:08"],
             "sunset": ["2026-11-17T16:52", "2026-11-18T16:51", "2026-11-19T16:51"]}
    return report._sky(Place("Des Moines, IA", lat, lon, "US"), fmt, daily)


def test_daytime_card_says_sunrise_and_sunset_in_words_and_the_moon_next():
    from better_weather.core import display

    sky = _sky_at("2026-11-17T18:00:00Z")  # noon in Des Moines; the moon rises 1:13 PM (USNO)
    assert sky["mode"] == "sun"
    lines = display.sky_lines(sky)
    assert lines[0] == "🌅 Sunrise 7:06 AM · 🌇 Sunset 4:52 PM"
    assert lines[1].startswith("🌓 First quarter moon · ") and "% lit · rises 1:1" in lines[1]


def test_night_card_leads_with_the_moon_then_the_next_sunrise():
    from better_weather.core import display

    sky = _sky_at("2026-11-18T04:00:00Z")  # 10 PM: the moon is up and sets after midnight
    assert sky["mode"] == "moon" and sky["moon"]["up"] is True
    lines = display.sky_lines(sky)
    assert "· up now, sets 1" in lines[0] or "· up now, sets 12" in lines[0]
    assert lines[1] == "🌅 Sunrise 7:07 AM · 🌕 Full moon Tue, Nov 24"


def test_without_the_forecast_the_sun_lines_are_still_right():
    from zoneinfo import ZoneInfo

    from better_weather.core import display, report
    from better_weather.core.places import Place

    fmt = report._Fmt("us", "12h", ZoneInfo("America/Chicago"))
    fmt.now = datetime.fromisoformat("2026-11-17T18:00:00+00:00").astimezone(ZoneInfo("America/Chicago"))
    sky = report._sky(Place("Des Moines, IA", 41.587, -93.625, "US"), fmt, {})  # Open-Meteo didn't answer
    assert sky["mode"] == "sun" and not sky["sun_all_day"], "noon in November isn't the midnight sun"
    # USNO: 7:06 AM and 4:52 PM; worked out here to within a minute
    assert re.fullmatch(r"🌅 Sunrise 7:0[5-7] AM · 🌇 Sunset 4:5[1-3] PM", display.sky_lines(sky)[0])
    fmt.now = datetime.fromisoformat("2026-11-18T04:00:00+00:00").astimezone(ZoneInfo("America/Chicago"))
    night = report._sky(Place("Des Moines, IA", 41.587, -93.625, "US"), fmt, {})
    assert night["mode"] == "moon" and night["next_sunrise"] in ("7:06 AM", "7:07 AM", "7:08 AM")


def test_a_moonrise_more_than_a_day_off_says_which_day():
    from zoneinfo import ZoneInfo

    from better_weather.core import report
    from better_weather.core.places import Place

    fmt = report._Fmt("us", "12h", ZoneInfo("America/Anchorage"))
    fmt.now = datetime.fromisoformat("2026-11-24T21:00:00+00:00").astimezone(ZoneInfo("America/Anchorage"))
    sky = report._sky(Place("Utqiagvik, AK", 71.291, -156.789, "US"), fmt, {})  # the moon up all day around full
    when = sky["moon"].get("sets") or sky["moon"].get("rises")
    assert when is None or not when[0].isdigit(), when


def test_card_lines_for_the_edges():
    from better_weather.core import display

    new_moon = {"mode": "moon", "next_sunrise": "7:20 AM",
                "moon": {"phase": "New moon", "emoji": "🌑", "lit_pct": 0, "up": False, "rises": "7:01 AM",
                         "next": {"phase": "Full moon", "emoji": "🌕", "date": "Wed, Dec 23"}}}
    assert display.sky_lines(new_moon) == ["🌑 New moon", "🌅 Sunrise 7:20 AM · 🌕 Full moon Wed, Dec 23"]
    polar_day = {"mode": "sun", "sun_all_day": True,
                 "moon": {"phase": "Full moon", "emoji": "🌕", "lit_pct": 100, "up": True, "sets": None,
                          "next": {"phase": "New moon", "emoji": "🌑", "date": "Mon, Jul 6"}}}
    assert display.sky_lines(polar_day) == ["☀️ Sun up all day", "🌕 Full moon · 100% lit · up all day"]
    full_tonight = {"mode": "moon", "next_sunrise": "Fri 11:52 AM",
                    "moon": {"phase": "Full moon", "emoji": "🌕", "lit_pct": 100, "up": True, "sets": "9:40 AM",
                             "next": {"phase": "Full moon", "emoji": "🌕", "date": "Tue, Nov 24"}}}
    lines = display.sky_lines(full_tonight)
    assert lines == ["🌕 Full moon · 100% lit · up now, sets 9:40 AM", "🌅 Sunrise Fri 11:52 AM"], \
        "no 'Full moon … next' when tonight is the full moon"
