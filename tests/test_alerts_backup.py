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


def ev(name, ph, sig, eid, wfo="DMX", ends_in=2.0, issued_ago=1.0):
    year = (clock.now_utc() - timedelta(hours=issued_ago)).year
    return {"url": f"/vtec/?year={year}&wfo=K{wfo}&phenomena={ph}&significance={sig}&eventid={eid:04d}",
            "issue": iso(-issued_ago), "expire": iso(ends_in), "eventid": eid, "phenomena": ph,
            "significance": sig, "wfo": wfo, "name": name, "ugc": "IAZ060"}


def row(ph, sig, eid, wfo="DMX", status="CON"):
    return {"wfo": wfo, "phenomena": ph, "significance": sig, "eventid": eid,
            "year": clock.now_utc().year, "status": status}


ELSEWHERE = row("FG", "Y", 12, wfo="ABQ")   # something is always in effect somewhere


def nws_feature(ident, event, action="NEW", etn="0012", ph="TO", sig="W", vtec=True):
    params = {"VTEC": [f"/O.{action}.KDMX.{ph}.{sig}.{etn}.261115T2100Z-261115T2145Z/"]} if vtec else {}
    return {"properties": {"id": ident, "event": event, "status": "Actual", "messageType": "Alert",
                           "severity": "Extreme" if "Tornado" in event else "Moderate", "ends": iso(1),
                           "onset": iso(-0.5), "description": "...", "instruction": "", "parameters": params}}


def serve(nws_features=None, spot=(), rows=(ELSEWHERE,), mesonet_up=True, log=None):
    """api.weather.gov answers `nws_features` (None = 503); the mesonet answers `spot` and `rows`."""
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "api.weather.gov":
            if nws_features is None:
                return httpx.Response(503, text="<HTML>Error</HTML>")
            return httpx.Response(200, json={"features": list(nws_features)})
        if request.url.host == "mesonet.agron.iastate.edu" and mesonet_up:
            if request.url.path.endswith("vtec_events_bypoint.py"):
                return httpx.Response(200, json={"events": list(spot)})
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
    assert watch.event_key(alerts[0], props["parameters"]) == ("KDMX.TO.W.0041", "NEW")


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
