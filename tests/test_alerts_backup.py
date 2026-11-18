"""The backup for NWS alerts (2026-11-17): api.weather.gov's alerts API returned 503 for hours, pushes stopped and every
card said alerts couldn't be checked. When the NWS doesn't answer, `nws.alerts_at` now returns the Iowa Environmental
Mesonet's copy of its warnings in the NWS's own shape, with the same VTEC codes (a warning keeps its push key whichever
service reported it), labelled as the copy; when both fail, the NWS's error as before. No network: faked answers."""

from datetime import timedelta

import httpx
import pytest
from better_weather.core import clock, http, report, watch
from better_weather.core.places import Place
from better_weather.core.sources import iem, nws
from fixturekit import replay_transport

CONFIG = {"name": "Des Moines, IA", "lat": 41.6, "lon": -93.61, "timezone": "America/Chicago",
          "level": "watches", "clock": "12h"}


def iso(hours: float) -> str:
    return (clock.now_utc() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(name, ph, sig, eid, wfo="DMX", ends_in=2.0, issued_ago=1.0, office=None, year=None):
    year = year or (clock.now_utc() - timedelta(hours=issued_ago)).year
    return {"url": f"/vtec/?year={year}&wfo={office or 'K' + wfo}&phenomena={ph}&significance={sig}&eventid={eid:04d}",
            "issue": iso(-issued_ago), "expire": iso(ends_in), "eventid": eid, "phenomena": ph,
            "significance": sig, "wfo": wfo, "name": name, "ugc": "IAZ060"}


def row(ph, sig, eid, wfo="DMX", status="CON", year=None, issued_ago=1.0, updated_ago=0.2):
    return {"wfo": wfo, "phenomena": ph, "significance": sig, "eventid": eid,
            "year": year or clock.now_utc().year, "status": status,
            "issue": iso(-issued_ago), "updated": iso(-updated_ago)}


ELSEWHERE = row("FG", "Y", 12, wfo="ABQ")   # something is always in effect somewhere


def nws_feature(ident, event, action="NEW", etn="0012", ph="TO", sig="W", vtec=True):
    params = {"VTEC": [f"/O.{action}.KDMX.{ph}.{sig}.{etn}.261115T2100Z-261115T2145Z/"]} if vtec else {}
    return {"properties": {"id": ident, "event": event, "status": "Actual", "messageType": "Alert",
                           "severity": "Extreme" if "Tornado" in event else "Moderate", "ends": iso(1),
                           "onset": iso(-0.5), "description": "...", "instruction": "", "parameters": params}}


def in_window(request: httpx.Request, items):
    """What the mesonet's by-point lookups really return: events that START on or after `sdate` and before `edate`
    (exclusive, UTC dates)."""
    sdate, edate = request.url.params.get("sdate"), request.url.params.get("edate")
    assert sdate and edate, "the mesonet's by-point lookups need a window"
    return [i for i in items if "issue" not in i or sdate <= i["issue"][:10] < edate]


def serve(nws_features=None, spot=(), rows=(ELSEWHERE,), mesonet_up=True, log=None, polygons=None):
    """api.weather.gov answers `nws_features` (None = 503); the mesonet answers `spot` and `rows`, and its polygon
    lookup `polygons` (None = it fails)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(f"{request.url.host}{request.url.path}?{request.url.query.decode()}")
        if request.url.host == "mesonet.agron.iastate.edu" and request.url.path.endswith("sbw_by_point.py"):
            if polygons is None or not mesonet_up:
                return httpx.Response(503)
            return httpx.Response(200, json={"sbws": in_window(request, polygons)})
        if request.url.host == "api.weather.gov":
            if nws_features is None:
                return httpx.Response(503, text="<HTML>Error</HTML>")
            return httpx.Response(200, json={"features": list(nws_features)})
        if request.url.host == "mesonet.agron.iastate.edu" and mesonet_up:
            if request.url.path.endswith("vtec_events_bypoint.py"):
                return httpx.Response(200, json={"events": in_window(request, spot)})
            return httpx.Response(200, json={"data": list(rows)})
        return httpx.Response(503)
    return httpx.MockTransport(handler)


def test_the_nws_answering_means_the_copy_is_never_asked():
    log: list[str] = []
    http.set_transport_for_tests(serve([], log=log))
    fetched = nws.alerts_at(41.6, -93.61)
    assert not iem.is_copy(fetched.data) and all(h.startswith("api.weather.gov") for h in log)


def test_with_the_nws_down_the_copy_answers_in_the_nws_shape():
    http.set_transport_for_tests(serve(None, spot=[
        ev("Tornado Warning", "TO", "W", 41, ends_in=0.5),
        ev("Winter Weather Advisory", "WW", "Y", 7), ev("Winter Weather Advisory", "WW", "Y", 7),
        ev("Wind Advisory", "WI", "Y", 9),                    # cancelled early: not in the live list
        ev("Flood Watch", "FA", "A", 3, ends_in=-1),          # expired
        ev("Blizzard Warning", "BZ", "W", 2),                 # its last action was a cancel
    ], rows=[ELSEWHERE, row("TO", "W", 41, status="NEW"), row("WW", "Y", 7), row("FA", "A", 3),
             row("BZ", "W", 2, status="CAN")]))
    fetched = nws.alerts_at(41.6, -93.61)
    assert iem.is_copy(fetched.data)
    alerts = nws.normalize_alerts(fetched.data)
    assert [a["event"] for a in alerts] == ["Tornado Warning", "Winter Weather Advisory"]
    assert alerts[0]["life_threatening"] is True and alerts[0]["issued_by"].startswith("NWS DMX")
    props = fetched.data["features"][0]["properties"]
    assert watch.event_key(alerts[0], props["parameters"]) == ("KDMX.TO.W.0041", "CON")   # (issued an hour ago)


def test_offices_outside_the_lower_48_keep_their_own_codes():
    assert [iem.office(w) for w in ("DMX", "AFC", "HFO", "GUM", "SJU", "PPG")] == \
        ["KDMX", "PAFC", "PHFO", "PGUM", "TJSJ", "NSTU"]


def test_with_both_down_it_is_the_nws_error_as_before():
    http.set_transport_for_tests(serve(None, mesonet_up=False))
    with pytest.raises(http.FetchError) as caught:
        nws.alerts_at(41.6, -93.61)
    assert "503" in str(caught.value)


def test_a_country_with_nothing_in_effect_is_never_taken_as_no_alerts():
    http.set_transport_for_tests(serve(None, rows=[]))
    with pytest.raises(http.FetchError):
        nws.alerts_at(41.6, -93.61)


def test_a_push_from_the_copy_says_so_and_keeps_its_key_when_the_nws_is_back():
    http.set_transport_for_tests(serve(None, spot=[ev("Tornado Warning", "TO", "W", 12)],
                                       rows=[ELSEWHERE, row("TO", "W", 12, status="NEW")]))
    message, state = watch.check(CONFIG, {})
    assert message.startswith("🚨 TORNADO WARNING — Des Moines, IA")
    assert message.endswith("(From Iowa State's copy of NWS warnings: the NWS didn't answer.)")
    assert list(state["sent"]) == ["KDMX.TO.W.0012"]
    http.set_transport_for_tests(serve([nws_feature("n1", "Tornado Warning", action="CON")]))
    message, state = watch.check(CONFIG, state)
    assert message == ""                                    # the same warning: not pushed twice


def test_a_warning_seen_first_from_the_nws_is_not_pushed_again_from_the_copy():
    http.set_transport_for_tests(serve([nws_feature("n1", "Tornado Warning")]))
    message, state = watch.check(CONFIG, {})
    assert "TORNADO WARNING" in message
    http.set_transport_for_tests(serve(None, spot=[ev("Tornado Warning", "TO", "W", 12, ends_in=1)],   # same end
                                       rows=[ELSEWHERE, row("TO", "W", 12)]))
    assert watch.check(CONFIG, state)[0] == ""


def test_the_copy_never_ends_what_it_cannot_list():
    """A special weather statement (no VTEC) pushed from the NWS: the copy can't list it, so its absence there
    doesn't end it (no false "has ended", no second push when the NWS is back)."""
    config = {**CONFIG, "level": "all"}
    http.set_transport_for_tests(serve([nws_feature("s1", "Special Weather Statement", vtec=False)]))
    message, state = watch.check(config, {})
    assert "SPECIAL WEATHER STATEMENT" in message
    http.set_transport_for_tests(serve(None))
    for _ in range(3):
        message, state = watch.check(config, state)
        assert message == ""
    assert len(state["sent"]) == 1
    http.set_transport_for_tests(serve([nws_feature("s1", "Special Weather Statement", vtec=False)]))
    assert watch.check(config, state)[0] == ""


def test_checks_working_again_through_the_copy_say_so():
    http.set_transport_for_tests(serve(None, mesonet_up=False))
    state: dict = {}
    for _ in range(watch.FAIL_NOTICE_AFTER):
        _, state = watch.check(CONFIG, state)
    http.set_transport_for_tests(serve(None))
    message, state = watch.check(CONFIG, state)
    assert message == ("✅ Alert checks are working again (from Iowa State's copy of NWS warnings while the NWS "
                       "is down).") and state["failures"] == 0


def test_the_alerts_tool_and_the_card_say_where_alerts_came_from():
    def overrides(request: httpx.Request) -> httpx.Response | None:
        if request.url.host == "api.weather.gov" and request.url.path == "/alerts/active":
            return httpx.Response(503)
        if request.url.host == "mesonet.agron.iastate.edu":
            if request.url.path.endswith("vtec_events_bypoint.py"):
                return httpx.Response(200, json={"events": [ev("Winter Weather Advisory", "WW", "Y", 7)]})
            return httpx.Response(200, json={"data": [ELSEWHERE, row("WW", "Y", 7)]})
        return None

    http.set_transport_for_tests(replay_transport(overrides))
    place = Place(name="Des Moines, IA", lat=41.6, lon=-93.61, country="US")
    out = report.alerts(place, None, "12h")
    assert out["source"] == iem.SOURCE and iem.LIMITS in out["notes"]
    assert [a["event"] for a in out["alerts"]] == ["Winter Weather Advisory"]



# ---------------------------------------------------------------- the review of 0.9.1
def test_an_event_with_one_zone_ended_and_one_still_going_is_kept():
    http.set_transport_for_tests(serve(None, spot=[ev("Winter Storm Warning", "WS", "W", 5)],
                                       rows=[ELSEWHERE, row("WS", "W", 5, status="CON"),
                                             row("WS", "W", 5, status="EXP")]))
    assert [a["event"] for a in nws.normalize_alerts(nws.alerts_at(41.6, -93.61).data)] == ["Winter Storm Warning"]


def test_the_office_comes_from_the_events_own_link():
    """San Juan is JSJ at the mesonet but TJSJ in the NWS's codes: a pushed warning keeps its key."""
    assert iem.office("JSJ", "/vtec/?year=2026&wfo=TJSJ&phenomena=FF") == "TJSJ"
    assert [iem.office(w) for w in ("JSJ", "AAQ", "HEB")] == ["TJSJ", "PAAQ", "PHEB"]
    http.set_transport_for_tests(serve(None, spot=[ev("Flash Flood Warning", "FF", "W", 12, wfo="JSJ", office="TJSJ")],
                                       rows=[ELSEWHERE, row("FF", "W", 12, wfo="JSJ")], polygons=[
                                           {"wfo": "JSJ", "phenomena": "FF", "significance": "W", "eventid": 12,
                                            "url": f"/vtec/?year={clock.now_utc().year}&wfo=TJSJ"}]))
    feature = nws.alerts_at(18.4, -66.1).data["features"][0]["properties"]
    assert watch.event_key({"event": "x", "id": "y"}, feature["parameters"])[0] == "TJSJ.FF.W.0012"


def test_the_year_comes_from_the_link_not_the_start_time():
    """A watch issued Dec 31 that starts Jan 1: the live list files it under the year it was issued."""
    year = clock.now_utc().year
    http.set_transport_for_tests(serve(None, spot=[ev("Winter Storm Watch", "WS", "A", 90, issued_ago=-10, year=year)],
                                       rows=[ELSEWHERE, row("WS", "A", 90, year=year)]))
    assert [a["event"] for a in nws.normalize_alerts(nws.alerts_at(41.6, -93.61).data)] == ["Winter Storm Watch"]


def test_the_lookback_reaches_the_oldest_event_in_effect():
    """A river flood warning issued 70 days ago and still going (45 days missed it)."""
    log: list[str] = []
    http.set_transport_for_tests(serve(None, rows=[ELSEWHERE, row("FL", "W", 3, issued_ago=70 * 24)], log=log))
    nws.alerts_at(41.6, -93.61)
    asked = next(line for line in log if "bypoint" in line)
    oldest = (clock.now_utc() - timedelta(days=71)).date().isoformat()
    assert f"sdate={oldest}" in asked


def test_a_storm_warning_needs_its_polygon_to_cover_the_spot():
    spot = [ev("Severe Thunderstorm Warning", "SV", "W", 55), ev("Flood Warning", "FL", "W", 3)]
    rows = [ELSEWHERE, row("SV", "W", 55), row("FL", "W", 3)]
    http.set_transport_for_tests(serve(None, spot=spot, rows=rows, polygons=[]))   # the polygon misses the spot
    assert [a["event"] for a in nws.normalize_alerts(nws.alerts_at(41.6, -93.61).data)] == ["Flood Warning"]
    hit = {"wfo": "DMX", "phenomena": "SV", "significance": "W", "eventid": 55,
           "url": f"/vtec/?year={clock.now_utc().year}&wfo=KDMX"}
    http.set_transport_for_tests(serve(None, spot=spot, rows=rows, polygons=[hit]))
    assert {a["event"] for a in nws.normalize_alerts(nws.alerts_at(41.7, -93.61).data)} == \
        {"Severe Thunderstorm Warning", "Flood Warning"}
    http.set_transport_for_tests(serve(None, spot=spot, rows=rows, polygons=None))  # lookup down: by county (safe)
    assert len(nws.normalize_alerts(nws.alerts_at(41.8, -93.61).data)) == 2


def test_a_stalled_copy_is_not_taken_as_no_alerts():
    http.set_transport_for_tests(serve(None, rows=[row("FG", "Y", 12, wfo="ABQ", updated_ago=5)]))
    with pytest.raises(http.FetchError, match="hasn't been updated"):
        iem.alerts_at(41.6, -93.61)
    with pytest.raises(http.FetchError):   # (the caller sees the NWS's own error, as before)
        nws.alerts_at(41.6, -93.61)


def test_a_surprise_in_the_copy_is_cant_say_not_a_crash(monkeypatch):
    def broken(lat, lon):
        raise TypeError("a bug")

    monkeypatch.setattr(iem, "_read", broken)
    with pytest.raises(http.FetchError, match="couldn't be read"):
        iem.alerts_at(41.6, -93.61)



def test_an_old_polygon_answer_never_hides_a_new_warning():
    """The polygon lookup failing after an earlier answer: its saved copy (from before a new tornado warning) must not
    filter that warning out, nor may a cached answer older than the event list it filters. County matching then."""
    import time

    window = {"lat": 41.6, "lon": -93.61, "sdate": "2026-11-01", "edate": "2026-11-18"}
    http.set_transport_for_tests(serve(None, polygons=[]))
    assert iem._polygon_hits(window, time.time() - 60) == set()     # a fresh answer, newer than the events: used
    http.set_transport_for_tests(serve(None, polygons=None))        # the lookup fails now
    assert iem._polygon_hits(window, time.time() + 1) is None        # older than the events, and asking again failed
    assert iem._polygon_hits({**window, "lat": 40.0}, time.time()) is None   # never answered: county matching


def test_the_polygon_lookup_covers_the_same_window_as_the_events():
    log: list[str] = []
    http.set_transport_for_tests(serve(None, rows=[ELSEWHERE, row("FF", "W", 4, issued_ago=80)], polygons=[], log=log))
    nws.alerts_at(41.6, -93.61)
    events = next(line for line in log if "bypoint" in line)
    polygons = next(line for line in log if "sbw_by_point" in line)
    sdate = next(p for p in events.split("?", 1)[1].split("&") if p.startswith("sdate="))
    assert sdate in polygons and sdate == f"sdate={(clock.now_utc() - timedelta(hours=80 + 24)).date().isoformat()}"


def test_one_odd_event_doesnt_cost_the_others():
    spot = [{**ev("Wind Advisory", "WI", "Y", 9), "eventid": "x"}, ev("Winter Storm Warning", "WS", "W", 5)]
    http.set_transport_for_tests(serve(None, spot=spot, rows=[ELSEWHERE, row("WI", "Y", "x"), row("WS", "W", 5)]))
    assert [a["event"] for a in nws.normalize_alerts(nws.alerts_at(41.6, -93.61).data)] == ["Winter Storm Warning"]


# ---------------------------------------------------------------- the audit of 0.9.2
def test_an_alert_starting_after_the_next_utc_midnight_is_in_the_copy():
    """Ojai, 2026-11-18: a Flood Watch and a Wind Advisory starting at 09Z and 11Z the next day were missing from the
    copy (its window ended at the next UTC midnight) — a false "has ended" push, then a second "new" one."""
    clock.freeze_for_tests(clock.now_utc().replace(hour=19, minute=30))      # 19:30Z: midnight is 4.5 hours away
    spot = [ev("Flood Watch", "FA", "A", 4, wfo="LOX", issued_ago=-13.5, ends_in=27.5),
            ev("Wind Advisory", "WI", "Y", 73, wfo="LOX", issued_ago=-15.5, ends_in=20.5)]
    rows = [ELSEWHERE, row("FA", "A", 4, wfo="LOX", issued_ago=-13.5), row("WI", "Y", 73, wfo="LOX", issued_ago=-15.5)]
    http.set_transport_for_tests(serve(None, spot=spot, rows=rows))
    alerts = nws.normalize_alerts(nws.alerts_at(34.448, -119.243).data)
    assert sorted(a["event"] for a in alerts) == ["Flood Watch", "Wind Advisory"]


def test_the_window_reaches_past_the_latest_start_in_the_live_list():
    log: list[str] = []
    http.set_transport_for_tests(serve(None, rows=[ELSEWHERE, row("HU", "A", 9, issued_ago=-14 * 24)], log=log))
    nws.alerts_at(41.6, -93.61)
    asked = next(line for line in log if "bypoint" in line)
    edate = asked.split("edate=")[1].split("&")[0]
    assert edate > (clock.now_utc() + timedelta(days=14)).date().isoformat()


def test_a_flood_advisory_needs_its_polygon_too():
    """Homestead FL, 2026-11-18: the copy matched 4 flood advisories by county; the NWS (by polygon) listed 1."""
    spot = [ev("Flood Advisory", "FA", "Y", n, wfo="MFL") for n in (206, 207, 208, 209)] + \
        [ev("Flood Watch", "FA", "A", 3, wfo="MFL")]
    rows = [ELSEWHERE, *(row("FA", "Y", n, wfo="MFL") for n in (206, 207, 208, 209)), row("FA", "A", 3, wfo="MFL")]
    hit = {"wfo": "MFL", "phenomena": "FA", "significance": "Y", "eventid": 207, "issue": iso(-1),
           "url": f"/vtec/?year={clock.now_utc().year}&wfo=KMFL&phenomena=FA&significance=Y&eventid=0207"}
    http.set_transport_for_tests(serve(None, spot=spot, rows=rows, polygons=[hit]))
    found = nws.normalize_alerts(nws.alerts_at(25.468, -80.477).data)
    assert sorted((a["event"], a["id"]) for a in found) == [
        ("Flood Advisory", f"iem:KMFL.FA.Y.0207.{clock.now_utc().year}"),
        ("Flood Watch", f"iem:KMFL.FA.A.0003.{clock.now_utc().year}")]


def serve_abroad(log, points=503, alerts=400, mesonet=True):
    """api.weather.gov as it answers for Berlin: its alerts say "out of bounds" (or 503: down), its grid 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "api.weather.gov" and request.url.path.startswith("/points/"):
            if points == 200:
                return httpx.Response(200, json={"properties": {"gridId": "XXX", "timeZone": "UTC"}})
            return httpx.Response(points, json={"title": "Data Unavailable For Requested Point", "status": points})
        if request.url.host == "api.weather.gov":
            return httpx.Response(alerts, json={"title": "Invalid Parameter", "status": alerts,
                                                "detail": "Parameter \"point\" is invalid: out of bounds"})
        if request.url.host == "mesonet.agron.iastate.edu" and mesonet:
            if request.url.path.endswith("vtec_events_bypoint.py"):
                return httpx.Response(200, json={"events": []})
            if request.url.path.endswith("sbw_by_point.py"):
                return httpx.Response(200, json={"sbws": []})
            return httpx.Response(200, json={"data": [ELSEWHERE]})
        return httpx.Response(503)
    return httpx.MockTransport(handler)


def test_out_of_bounds_is_not_covered_not_nws_down_and_never_the_copy():
    """Berlin, 2026-11-18: the NWS's 400 "out of bounds" fell through to the copy, whose empty answer read as "no
    alerts" (pushes for a home abroad silently never came)."""
    log: list[str] = []
    http.set_transport_for_tests(serve_abroad(log))
    with pytest.raises(nws.NotCovered):
        nws.alerts_at(52.52, 13.405)
    assert not any("mesonet" in line for line in log)
    assert issubclass(nws.NotCovered, http.FetchError)    # every caller already treats it as "couldn't check"


def test_with_the_nws_down_its_grid_lookup_still_says_abroad():
    log: list[str] = []
    http.set_transport_for_tests(serve_abroad(log, points=404, alerts=503))
    with pytest.raises(nws.NotCovered):
        nws.alerts_at(43.65, -79.38)   # Toronto: inside the copy's coarse area, outside the NWS's grid
    assert not any("mesonet" in line for line in log)


def test_with_the_nws_all_down_the_copy_refuses_a_spot_abroad():
    log: list[str] = []
    http.set_transport_for_tests(serve_abroad(log, points=503, alerts=503))
    with pytest.raises(nws.NotCovered):
        nws.alerts_at(52.52, 13.405)
    with pytest.raises(nws.NotCovered):
        iem.alerts_at(52.52, 13.405)
    assert not any("mesonet" in line for line in log)


def test_a_spot_the_nws_covers_is_never_called_abroad_by_the_copys_rough_area():
    log: list[str] = []
    http.set_transport_for_tests(serve_abroad(log, points=200, alerts=503))   # its grid answers; alerts are down
    with pytest.raises(http.FetchError) as caught:
        nws.alerts_at(0.0, 0.0)   # (outside the copy's area: pretend the NWS covered it)
    assert not isinstance(caught.value, nws.NotCovered) and "503" in str(caught.value)


def test_the_copys_area_takes_in_every_part_of_the_us():
    inside = [(41.6, -93.61), (64.838, -147.716), (71.3, -156.8), (52.9, 173.2), (21.3, -157.85), (18.4, -66.1),
              (17.7, -64.8), (13.47, 144.75), (-14.3, -170.7), (24.55, -81.78), (26.0, -90.0), (20.0, -87.0),
              (45.0, -130.0), (40.0, -66.0), (48.99, -97.0), (7.45, 151.85)]
    outside = [(52.52, 13.405), (19.43, -99.13), (35.68, 139.69), (51.5, -0.12), (-33.87, 151.21), (0.0, 0.0),
               (-13.83, -171.76)]
    assert all(iem.in_nws_area(lat, lon) for lat, lon in inside)
    assert not any(iem.in_nws_area(lat, lon) for lat, lon in outside)


def test_the_alerts_card_never_says_no_alerts_for_coordinates_abroad():
    log: list[str] = []
    http.set_transport_for_tests(serve_abroad(log, points=503, alerts=503))
    berlin = report.alerts(Place(name="52.520, 13.405", lat=52.52, lon=13.405, kind="coords"), None, "12h")
    assert "✅" not in berlin["card"] and "outside the US" in berlin["card"]
    toronto = report.alerts(Place(name="43.650, -79.380", lat=43.65, lon=-79.38, kind="coords"), None, "12h")
    assert "✅" not in toronto["card"] and "may be outside the US" in toronto["card"]
    assert "summary" not in toronto and "_exact" not in toronto


def test_the_alerts_card_says_when_it_is_the_copy():
    http.set_transport_for_tests(serve(None, spot=[ev("Winter Weather Advisory", "WW", "Y", 7)],
                                       rows=[ELSEWHERE, row("WW", "Y", 7)]))
    out = report.alerts(Place(name="Des Moines, IA", lat=41.6, lon=-93.61, country="US"), None, "12h")
    assert report.COPY_NOTE in out["card"].splitlines()
    http.set_transport_for_tests(serve(None))
    quiet = report.alerts(Place(name="Des Moines, IA", lat=41.7, lon=-93.61, country="US"), None, "12h")
    assert quiet["card"].splitlines() == ["✅ No active alerts for Des Moines, IA", report.COPY_NOTE]
