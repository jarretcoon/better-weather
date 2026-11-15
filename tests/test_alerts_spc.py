import json

import httpx
from better_weather.core import http
from better_weather.core.sources import nws, spc


def _alert(ident, event, severity="Moderate", message="Alert", refs=(), **params):
    return {"properties": {
        "id": ident, "event": event, "severity": severity, "status": "Actual", "messageType": message,
        "references": [{"identifier": r} for r in refs], "headline": f"{event} headline",
        "description": "desc", "instruction": "Take shelter now." if "Tornado" in event else "",
        "ends": "2026-11-15T15:00:00-06:00", "parameters": {k: [v] for k, v in params.items()},
    }}


def test_updates_are_kept_and_replace_what_they_reference():
    data = {"features": [
        _alert("a1", "Wind Advisory"),
        _alert("a2", "Wind Advisory", message="Update", refs=["a1"]),
        _alert("w1", "Winter Storm Watch"),
    ]}
    out = nws.normalize_alerts(data)
    assert {a["id"] for a in out} == {"a2", "w1"}  # the update survives, the original it replaced does not


def test_life_threatening_warnings_sort_first():
    data = {"features": [
        _alert("1", "Frost Advisory", "Minor"),
        _alert("2", "Severe Thunderstorm Warning", "Severe", maxHailSize="1.00"),
        _alert("3", "Tornado Warning", "Extreme", tornadoDetection="RADAR INDICATED"),
        _alert("4", "Tornado Watch", "Severe"),
    ]}
    out = nws.normalize_alerts(data)
    assert out[0]["event"] == "Tornado Warning" and out[0]["life_threatening"]
    assert out[0]["details"]["tornadoDetection"] == "RADAR INDICATED"
    assert out[0]["instruction"] == "Take shelter now."
    assert [a["event"] for a in out][1:3] == ["Severe Thunderstorm Warning", "Tornado Watch"]
    assert out[-1]["event"] == "Frost Advisory"


def test_cancellations_and_tests_are_dropped():
    cancel = _alert("c", "Flood Warning", message="Cancel")
    test_msg = _alert("t", "Tornado Warning")
    test_msg["properties"]["status"] = "Test"
    assert nws.normalize_alerts({"features": [cancel, test_msg]}) == []


def _square(cx, cy, r):
    return [[[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r], [cx - r, cy - r]]]


def _outlook(*features):
    return {"features": [{"type": "Feature", "properties": {
        "LABEL": label, "LABEL2": label2, "VALID_ISO": "2026-11-15T12:00:00+00:00",
        "EXPIRE_ISO": "2026-11-16T12:00:00+00:00"},
        "geometry": {"type": "Polygon", "coordinates": geom}} for label, label2, geom in features]}


def test_spc_risk_is_checked_at_the_exact_point():
    lon, lat = -93.6, 41.6
    outlooks = {
        "day1otlk_cat": _outlook(("TSTM", "General", _square(lon, lat, 5)),
                                 ("MRGL", "Marginal", _square(lon, lat, 3)),
                                 ("SLGT", "Slight", _square(lon, lat, 1)),
                                 ("ENH", "Enhanced", _square(lon + 4, lat, 0.5))),
        "day1otlk_torn": _outlook(("0.02", "2%", _square(lon, lat, 3)), ("0.05", "5%", _square(lon, lat, 1)),
                                  ("SIGN", "Significant", _square(lon, lat, 1))),
        "day1otlk_wind": _outlook(("0.15", "15%", _square(lon, lat, 2))),
        "day1otlk_hail": _outlook(),
        "day2otlk_cat": _outlook(("MRGL", "Marginal", _square(lon + 10, lat, 1))),
        "day3otlk_cat": _outlook(),
    }

    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1].replace(".nolyr.geojson", "")
        return httpx.Response(200, content=json.dumps(outlooks[name]).encode())

    http.set_transport_for_tests(httpx.MockTransport(handler))
    risk = spc.risk_at(lat, lon)
    day1, day2, day3 = risk["days"]
    assert day1["category"] == "SLGT" and day1["level"] == 3  # not ENH: that polygon is elsewhere
    assert day1["hazards"]["tornado"]["chance_within_25_miles_pct"] == 5
    assert day1["hazards"]["tornado"]["intensity"] == ["Significant"]
    assert day1["hazards"]["wind"]["chance_within_25_miles_pct"] == 15
    assert day1["hazards"]["hail"]["chance_within_25_miles_pct"] == 0
    assert day2["category"] == "NONE" and day3["category"] == "NONE"
