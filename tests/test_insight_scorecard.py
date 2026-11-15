import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from better_weather.core import clock, http, insight, places, scorecard, settings

DSM = places.Place("Des Moines, IA", 41.6, -93.61, country="US", timezone="America/Chicago")


def test_ensemble_odds_and_ranges():
    members = {"time": ["2026-11-20"], "temperature_2m_max": [10.0], "temperature_2m_min": [0.0],
               "precipitation_sum": [0.0], "snowfall_sum": [0.0]}
    for i in range(1, 10):  # 10 runs: 3 wet (one soaking), highs spread 10..19 C
        members[f"temperature_2m_max_member{i:02d}"] = [10.0 + i]
        members[f"temperature_2m_min_member{i:02d}"] = [0.0 + i / 2]
        members[f"precipitation_sum_member{i:02d}"] = [20.0 if i == 1 else 2.0 if i in (2, 3) else 0.0]
        members[f"snowfall_sum_member{i:02d}"] = [0.0]
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"daily": members})))
    r = insight.confidence(DSM, "us", 1)
    day = r["days"][0]
    assert day["runs"] == 20  # two ensembles, 10 runs each
    assert day["chance_any_precip_pct"] == 30 and day["chance_half_inch_plus_pct"] == 10
    assert day["temperature_confidence"] == "low"  # 9 C spread between the 10th and 90th percentile
    assert day["high_likely_range"].endswith("°")


AFD = """000
FXUS63 KDMX 150806
AFDDMX

Area Forecast Discussion
National Weather Service Des Moines IA

.KEY MESSAGES...

- Gradual warming trend continues today through the first half
  of the week.

&&

.SHORT TERM /THROUGH MONDAY NIGHT/...
Issued at 202 AM CST Sun Nov 15 2026

High pressure continues to drive the weather
pattern over Iowa.

&&

.AVIATION /FOR THE 06Z TAFS THROUGH 06Z MONDAY/...
VFR.

&&

.DMX WATCHES/WARNINGS/ADVISORIES...
None.
"""


def test_discussion_sections_are_unwrapped_and_filtered():
    sections = insight._sections(AFD)
    assert set(sections) == {"Key Messages", "Short Term"}
    assert "first half of the week" in sections["Key Messages"]
    assert "the weather pattern over Iowa" in sections["Short Term"]


def _hourly(start_local, rows):
    base = datetime.fromisoformat(start_local)
    keys = ("temperature_2m", "apparent_temperature", "dew_point_2m", "precipitation_probability", "precipitation",
            "wind_speed_10m", "wind_gusts_10m", "cloud_cover", "uv_index", "is_day", "weather_code")
    hourly = {"time": [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(len(rows))]}
    for k in keys:
        hourly[k] = [r.get(k, 0) for r in rows]
    return {"timezone": "America/Chicago", "utc_offset_seconds": -21600, "hourly": hourly}


def test_best_time_finds_the_dry_mild_window():
    clock.freeze_for_tests(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))  # 6 AM CST... (UTC-6 in the fixture)
    rows = []
    for h in range(48):
        hour = h % 24
        day = 7 <= hour < 19
        wet = 30 <= h < 34   # rain tomorrow late morning
        rows.append({"apparent_temperature": 15 if day else 5, "precipitation_probability": 90 if wet else 5,
                     "precipitation": 2.0 if wet else 0, "wind_speed_10m": 8, "is_day": 1 if day else 0})
    payload = _hourly("2026-05-02T00:00", rows)
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    r = insight.best_time(DSM, "jog", "us", "12h", days=2)
    assert r["activity"] == "run"
    best = r["best_windows"][0]
    assert best["max_rain_chance_pct"] <= 30 and best["score"] >= 70
    assert insight.activity_name("BBQ") == "grill" and insight.activity_name("frisbee") == "outdoor"


@pytest.fixture
def card(tmp_path):
    settings.set_data_dir(tmp_path)
    scorecard.reset_for_tests()
    yield
    scorecard.reset_for_tests()
    settings.set_data_dir(None)


def test_scorecard_ranks_sources_by_average_miss(card):
    clock.freeze_for_tests(datetime(2026, 12, 20, 18, 0, tzinfo=UTC))
    db = scorecard._db()
    for d in range(12):
        target = (datetime(2026, 12, 1) + timedelta(days=d)).date().isoformat()
        db.execute("INSERT INTO observed VALUES (?, 0, -10, 0, 'test')", (target,))
        db.execute("INSERT INTO predictions VALUES (?, 1, 'ecmwf_ifs025', 0.5, -10.5, 0, '2026-11-30')", (target,))
        db.execute("INSERT INTO predictions VALUES (?, 1, 'gfs_seamless', 3, -7, 5, '2026-11-30')", (target,))
        db.execute("INSERT INTO predictions VALUES (?, 1, 'nws', 1, -9, NULL, '2026-11-30')", (target,))
    db.commit()
    r = scorecard.report(DSM, "us", 30)
    assert r["most_accurate_tomorrow"] == "ECMWF (Europe)"
    gfs = next(x for x in r["ranking"] if x["source"] == "GFS (US)")
    assert gfs["runs_warm_or_cold"].startswith("warm") and gfs["rain_yes_no_right_pct"] == 0


def test_scorecard_says_it_is_still_learning(card):
    clock.freeze_for_tests(datetime(2026, 12, 20, 18, 0, tzinfo=UTC))
    r = scorecard.report(DSM, "us", 30)
    assert r["ranking"] == [] and "Still learning" in r["notes"][0]


def test_scorecard_collects_once_a_day(card):
    clock.freeze_for_tests(datetime(2026, 11, 15, 18, 0, tzinfo=UTC))
    daily = {"time": ["2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18"]}
    for m in scorecard.MODELS:
        daily[f"temperature_2m_max_{m}"] = [10, 11, 12, 13]
        daily[f"temperature_2m_min_{m}"] = [0, 1, 2, 3]
        daily[f"precipitation_sum_{m}"] = [0, 0, 1, 0]
    http.set_transport_for_tests(httpx.MockTransport(
        lambda r: httpx.Response(200, json={"daily": daily}) if "open-meteo" in str(r.url)
        else httpx.Response(404, json={})))
    assert scorecard.collect(DSM) == 15  # 5 models x 3 days (NWS unreachable here)
    assert scorecard.collect(DSM) == 0
    assert json.loads(json.dumps(scorecard._meta("collected_day"))) == "2026-11-15"


def test_scorecard_starts_over_only_after_a_confirmed_move(card):
    clock.freeze_for_tests(datetime(2026, 12, 20, 18, 0, tzinfo=UTC))
    scorecard._meta("place", "Des Moines, IA|41.600|-93.610")  # how 0.8.x recorded home
    db = scorecard._db()
    db.execute("INSERT INTO predictions VALUES ('2026-12-10', 1, 'nws', 1, -9, NULL, '2026-12-09')")
    db.execute("INSERT INTO observed VALUES ('2026-12-10', 0, -10, 0, 'test')")
    db.commit()
    assert scorecard._same_home(DSM)
    elsewhere = places.Place("Kord, IR", 31.0, 50.0, country="IR", timezone="Asia/Tehran")

    def rows():
        return scorecard._db().execute("SELECT COUNT(*) FROM predictions").fetchone()[0]

    r = scorecard.report(elsewhere, "us", 30)  # reading never deletes
    assert r["ranking"] == [] and "Home changed" in r["notes"][0] and rows() == 1
    assert scorecard.collect(elsewhere) == 0 and rows() == 1  # one odd lookup: kept
    http.set_transport_for_tests(httpx.MockTransport(
        lambda r: httpx.Response(200, json={"daily": {"time": []}}) if "open-meteo" in str(r.url)
        else httpx.Response(404, json={})))
    scorecard.collect(DSM)  # home is back to normal: the odd one out is forgotten
    assert scorecard._meta("home_candidate") is None and rows() == 1
    assert not scorecard._home_moved(elsewhere)
    clock.freeze_for_tests(datetime(2026, 12, 21, 1, 0, tzinfo=UTC))  # 7 h later, still elsewhere
    assert scorecard._home_moved(elsewhere)
