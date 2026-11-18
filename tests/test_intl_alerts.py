import httpx
from better_weather.core import http, places, report
from better_weather.core.sources import intl_alerts

SQUARE = {"type": "Polygon", "coordinates": [[[-123.3, 49.0], [-122.9, 49.0], [-122.9, 49.3], [-123.3, 49.3],
                                               [-123.3, 49.0]]]}
ELSEWHERE = {"type": "Polygon", "coordinates": [[[-120, 50], [-119, 50], [-119, 51], [-120, 51], [-120, 50]]]}


def _ca_feature(code, kind, name, geometry, area="Metro Vancouver"):
    return {"type": "Feature", "geometry": geometry, "properties": {
        "alert_code": code, "alert_type": kind, "alert_name_en": name, "alert_short_name_en": name.title(),
        "feature_name_en": area, "validity_datetime": "2026-11-15T10:00:00Z",
        "event_end_datetime": "2026-11-16T03:00:00Z",
        "expiration_datetime": "2026-11-15T22:00:00Z", "alert_text_en": "Strong winds expected.", "status_en": "active",
        "risk_colour_en": "yellow"}}


def test_canada_matches_the_exact_spot_and_sorts_warnings_first():
    feats = [_ca_feature("SPS", "statement", "special weather statement", SQUARE),
             _ca_feature("WW", "warning", "wind warning", SQUARE),
             _ca_feature("SW", "warning", "snowfall warning", ELSEWHERE)]
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"features": feats})))
    out = intl_alerts.canada(49.15, -123.1)
    assert [a["event"] for a in out] == ["Wind warning", "Special weather statement"]
    assert out[0]["issued_by"].startswith("Environment and Climate Change Canada")


def _ma_warning(event, level, regions, polygon=None, lang="en-GB"):
    area = [{"areaDesc": r, "geocode": [{"valueName": "EMMA_ID", "value": "XX001"}]} for r in regions]
    if polygon:
        area[0]["polygon"] = [polygon]
    return {"alert": {"msgType": "Alert", "info": [
        {"language": "de-DE", "event": "Sturm", "parameter": []},
        {"language": lang, "event": event, "headline": event, "severity": "Severe", "expires": "2026-11-16T00:00:00Z",
         "onset": "2026-11-15T12:00:00Z", "senderName": "Met Service", "description": "Gusts to 100 km/h.",
         "parameter": [{"valueName": "awareness_level", "value": f"{level}; colour; x"}], "area": area}]}}


def test_europe_exact_polygon_match_and_country_summary():
    warnings = [
        _ma_warning("Wind warning", 3, ["Oslo"], polygon="59.8,10.6 59.8,10.9 60.0,10.9 60.0,10.6 59.8,10.6"),
        _ma_warning("Rain warning", 4, ["Bergen", "Hordaland"]),
        _ma_warning("Fog", 2, ["Tromsø"]),
        _ma_warning("Nothing", 1, ["Anywhere"]),
    ]
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"warnings": warnings})))
    out = intl_alerts.europe("NO", 59.91, 10.75)
    assert [a["event"] for a in out["exact_matches"]] == ["Wind warning"]
    assert out["orange_and_red_in_country"][0]["level"] == "red"
    assert out["country_counts"] == {"orange": 1, "red": 1, "yellow": 1}


def test_alerts_tool_explains_region_only_matching():
    warnings = [_ma_warning("Rain warning", 3, ["Gard"])]
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"warnings": warnings})))
    place = places.Place("Nîmes, Occitanie, FR", 43.84, 4.36, country="FR", timezone="Europe/Paris")
    r = report.alerts(place, None, "12h")
    assert r["count"] == 0 and r["orange_and_red_in_country"][0]["regions"] == ["Gard"]
    note = "⚠️ France lists warnings by area name: only ones naming Nîmes or its area are shown (1 orange/red elsewhere)"
    assert r["notes"] == [note]
    assert "✅" not in r["card"] and note in r["card"]  # never "no alerts" when it can't tell
    assert r["card"].splitlines()[-1] == "🟠 Rain warning · Gard"  # and the card names the area


def test_region_name_matches_count_when_the_country_has_no_map():
    warnings = [_ma_warning("Rain warning", 3, ["Hérault", "Gard"]), _ma_warning("Wind", 2, ["Var"])]
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"warnings": warnings})))
    place = places.Place("Nîmes, Occitanie, FR", 43.84, 4.36, country="FR", timezone="Europe/Paris",
                         district="Gard")
    r = report.alerts(place, None, "12h")
    assert [a["event"] for a in r["alerts"]] == ["Rain warning"]
    assert r["alerts"][0]["regions"][0] == "Gard" and r["alerts"][0]["matched_by"] == "region name"
    assert "Rain warning" in r["card"]


def test_unknown_country_is_not_reported_as_clear():
    place = places.Place("10.000, 20.000", 10.0, 20.0, country="TD")
    r = report.alerts(place, None, "12h")
    assert r["count"] == 0 and "⚠️" in r["card"] and "✅" not in r["card"]


def test_expired_warnings_are_dropped():
    from datetime import UTC, datetime

    from better_weather.core import clock

    clock.freeze_for_tests(datetime(2026, 11, 20, 0, 0, tzinfo=UTC))  # after the warnings' 2026-11-16 expiry
    warnings = [_ma_warning("Rain warning", 4, ["Bergen"])]
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"warnings": warnings})))
    assert intl_alerts.europe("NO", 60.39, 5.32)["orange_and_red_in_country"] == []


def test_one_mapped_warning_does_not_make_the_whole_country_exact():
    nice = "43.6,7.1 43.6,7.4 43.8,7.4 43.8,7.1 43.6,7.1"
    warnings = [_ma_warning("Coastal event", 2, ["Alpes-Maritimes coast"], polygon=nice),
                _ma_warning("Rain-flood", 2, ["Haute-Corse"])]
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"warnings": warnings})))
    bastia = places.Place("Bastia, Corsica, FR", 42.70, 9.45, country="FR", timezone="Europe/Paris",
                          district="Upper Corsica")
    r = report.alerts(bastia, None, "12h")
    assert r["count"] == 0 and "✅" not in r["card"] and "⚠️" in r["card"]


def test_a_quiet_country_is_clear():
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"warnings": []})))
    r = report.alerts(places.Place("Oslo, NO", 59.91, 10.75, country="NO", timezone="Europe/Oslo"), None, "12h")
    assert r["card"].startswith("✅") and not any(n.startswith("⚠️") for n in r["notes"])


def test_area_names_match_whole_names_only():
    names = [intl_alerts._area_key(n) for n in ("Madrid", "Comunidad de Madrid", "Berlin")]
    assert not intl_alerts._names_in("Sierra de Madrid", names)
    assert not intl_alerts._names_in("West Flanders", [intl_alerts._area_key("Flanders")])
    assert intl_alerts._names_in("Stadt Berlin", names) and intl_alerts._names_in("Madrid", names)


def test_a_big_feed_keeps_answering_while_a_new_copy_downloads(monkeypatch):
    import threading
    import time

    url = intl_alerts.METEOALARM.format(slug="germany")
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow(u):
        calls.append(u)
        started.set()
        release.wait(5)
        return {"warnings": []}

    old = {"warnings": [{"alert": {"msgType": "Alert"}}]}
    monkeypatch.setattr(intl_alerts, "_download", slow)
    intl_alerts._feeds[url] = (time.monotonic() - intl_alerts.FEED_FRESH - 5, old)
    t = time.monotonic()
    assert intl_alerts._feed(url) is old and time.monotonic() - t < 0.5, "no waiting for the download"
    assert intl_alerts._feed(url) is old and len(calls) <= 1, "one download at a time"
    assert started.wait(2)
    release.set()
    for _ in range(100):
        if url not in intl_alerts._refreshing:
            break
        time.sleep(0.01)
    assert intl_alerts._feeds[url][1] == {"warnings": []}
    # past FEED_KEEP the copy is too old to show: the visitor waits for a fresh one
    intl_alerts._feeds[url] = (time.monotonic() - intl_alerts.FEED_KEEP - 5, old)
    assert intl_alerts._feed(url) == {"warnings": []}
