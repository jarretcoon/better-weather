"""0.9.5: the severe answer names every other alert in effect, not only the storm ones."""

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from better_weather.core import clock, report
from better_weather.core.places import Place


def _alert(ident, event, onset, ends):
    return {"properties": {"id": ident, "event": event, "status": "Actual", "messageType": "Alert",
                           "severity": "Severe", "onset": onset, "effective": onset, "ends": ends,
                           "headline": f"{event} issued", "description": "…", "instruction": ""}}


def test_severe_names_the_other_alerts_too(monkeypatch):
    # Hatteras, NC, 2026-11-25 ~1 AM EST: a marginal SPC risk, plus wind, flood and surf alerts that aren't storms
    alerts = {"features": [
        _alert("a1", "High Wind Warning", "2026-11-25T16:00:00-05:00", "2026-11-26T05:00:00-05:00"),
        _alert("a2", "Coastal Flood Warning", "2026-11-25T00:00:00-05:00", "2026-11-27T15:00:00-05:00"),
        _alert("a3", "Severe Thunderstorm Watch", "2026-11-25T00:30:00-05:00", "2026-11-25T06:00:00-05:00"),
    ]}
    monkeypatch.setattr(report, "_nws_lookup", lambda place, notes: ({"tz": "America/New_York"}, True))
    monkeypatch.setattr(report.nws, "alerts_at", lambda lat, lon: SimpleNamespace(data=alerts))
    monkeypatch.setattr(report.spc, "risk_at", lambda lat, lon: {"days": []})
    monkeypatch.setattr(report.openmeteo, "forecast", lambda *a, **k: SimpleNamespace(data={}))
    monkeypatch.setattr(report, "_place_tz", lambda place, point, om: ZoneInfo("America/New_York"))
    clock.freeze_for_tests(datetime(2026, 11, 25, 6, 10, tzinfo=UTC))  # 1:10 AM EST; the autouse fixture resets it
    place = Place(name="Hatteras, NC", lat=35.219, lon=-75.69, country="US", region="North Carolina",
                  timezone="America/New_York", kind="search")
    r = report.severe(place, "us", "12h")
    assert [a["event"] for a in r["storm_alerts"]] == ["Severe Thunderstorm Watch"]
    assert [a["event"] for a in r["other_alerts"]] == ["High Wind Warning", "Coastal Flood Warning"]
    wind = r["other_alerts"][0]
    assert wind["upcoming"] and wind["starts"] == "4 PM" and wind["until"] == "tomorrow 5 AM"
    lines = r["summary"].splitlines()
    assert lines[0].startswith("⚠️ Severe Thunderstorm Watch until")
    assert lines[1] == "⚠️ High Wind Warning from 4 PM to tomorrow 5 AM"
    assert lines[2].startswith("⚠️ Coastal Flood Warning until Fri 3 PM")
    assert "No severe weather signals" not in r["summary"]


def test_the_severe_text_lists_other_alerts_after_the_storm_ones():
    text = report.severe_text({
        "storm_alerts": [{"event": "Tornado Warning", "until": "4:45 PM", "life_threatening": True}],
        "other_alerts": [{"event": "Wind Advisory", "until": "6 PM"}]})
    assert text.splitlines() == ["🚨 Tornado Warning until 4:45 PM", "⚠️ Wind Advisory until 6 PM"]
    assert report.severe_text({"other_alerts": [], "storm_alerts": []}) == "No severe weather signals found."
