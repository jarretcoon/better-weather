from datetime import UTC, datetime

import httpx
import pytest
from better_weather.core import clock, http, places, travel, water

FAA_XML = b"""<AIRPORT_STATUS_INFORMATION><Update_Time>now</Update_Time>
<Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List><Ground_Delay><ARPT>ORD</ARPT>
<Reason>weather / thunderstorms</Reason><Avg>1 hour and 12 minutes</Avg><Max>2 hours</Max></Ground_Delay>
</Ground_Delay_List></Delay_type>
<Delay_type><Name>General Arrival/Departure Delay Info</Name><Arrival_Departure_Delay_List><Delay><ARPT>ORD</ARPT>
<Reason>WX:Thunderstorms</Reason><Arrival_Departure Type="Departure"><Min>31 minutes</Min><Max>45 minutes</Max>
<Trend>Increasing</Trend></Arrival_Departure></Delay></Arrival_Departure_Delay_List></Delay_type>
<Delay_type><Name>Airport Closures</Name><Airport_Closure_List><Airport><ARPT>SNA</ARPT><Reason>runway work</Reason>
</Airport></Airport_Closure_List></Delay_type></AIRPORT_STATUS_INFORMATION>"""


def test_airport_codes_and_flight_categories():
    assert travel.icao_for("dsm") == "KDSM" and travel.icao_for("PAFA") == "PAFA"
    with pytest.raises(ValueError):
        travel.icao_for("ZZ")
    assert travel._category("10+", None) == "VFR"
    assert travel._category("4", 2500) == "MVFR"
    assert travel._category("2", 5000) == "IFR"
    assert travel._category("10", 300) == "LIFR"
    assert travel._ceiling_ft([{"cover": "SCT", "base": 800}, {"cover": "BKN", "base": 2500}]) == 2500


def test_faa_status_parsing():
    travel._faa_cache.update(at=0.0, body=b"")
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, content=FAA_XML)))
    ord_status = travel.faa_status("ORD")
    kinds = {s["type"] for s in ord_status}
    assert kinds == {"Ground Delay Programs", "General Arrival/Departure Delay Info"}
    gdp = next(s for s in ord_status if s["type"] == "Ground Delay Programs")
    assert gdp["Avg"] == "1 hour and 12 minutes"
    dep = next(s for s in ord_status if s["type"].startswith("General"))
    assert dep["Max"] == "45 minutes" and dep["Trend"] == "Increasing"
    assert travel.faa_status("DSM") == []


def _gauge(lid, lat, lon, stage, cat, fstage=-999, fcat="fcst_not_current"):
    return {"lid": lid, "name": lid, "latitude": lat, "longitude": lon, "status": {
        "observed": {"primary": stage, "primaryUnit": "ft", "floodCategory": cat},
        "forecast": {"primary": fstage, "primaryUnit": "ft", "floodCategory": fcat}}}


def test_rivers_put_flooding_first_and_skip_dead_gauges():
    gauges = [_gauge("NEAR", 41.6, -93.6, 10, "no_flooding"),
              _gauge("FAR_FLOOD", 41.8, -93.4, 25, "minor", 27, "moderate"),
              _gauge("DEAD", 41.6, -93.6, -999, "out_of_service"),
              _gauge("NODEF", 41.6, -93.6, 900, "not_defined")]

    def handler(request):
        if request.url.path.endswith("/gauges"):
            return httpx.Response(200, json={"gauges": gauges})
        return httpx.Response(200, json={"flood": {"categories": {"minor": {"stage": 24}, "major": {"stage": 30},
                                                                  "action": {"stage": -9999}}}})

    http.set_transport_for_tests(httpx.MockTransport(handler))
    r = water.rivers(places.Place("X", 41.6, -93.6, country="US"))
    assert [g["lid"] for g in r["gauges"]] == ["FAR_FLOOD", "NEAR"]
    assert r["flooding_nearby"] and r["gauges"][0]["forecast"] == "27.0 ft, moderate flooding"
    assert r["gauges"][0]["flood_stages_ft"] == {"minor": 24, "major": 30}


def test_tides_pick_the_nearest_station_and_waves_skip_inland():
    clock.freeze_for_tests(datetime(2026, 11, 15, 12, 0, tzinfo=UTC))
    stations = {"stations": [{"id": "1", "name": "Far", "lat": 33.5, "lng": -79.0},
                             {"id": "2", "name": "Near", "lat": 32.78, "lng": -79.92}]}
    preds = {"predictions": [{"t": "2026-11-15 14:00", "v": "5.6", "type": "H"},  # GMT = 9 AM EST
                             {"t": "2026-11-15 20:10", "v": "0.2", "type": "L"}]}

    def handler(request):
        url = str(request.url)
        if "stations.json" in url:
            return httpx.Response(200, json=stations)
        if "datagetter" in url:
            assert "station=2" in url and "time_zone=gmt" in url
            return httpx.Response(200, json=preds)
        return httpx.Response(200, json={"current": {"wave_height": None}})

    http.set_transport_for_tests(httpx.MockTransport(handler))
    place = places.Place("Charleston, SC", 32.776, -79.933, country="US", timezone="America/New_York")
    t = water.tides(place, "12h")
    assert t["station"].startswith("Near") and t["next"][0] == {"time": "9 AM", "type": "high", "height_ft": 5.6}
    assert water.waves(place, "us") is None
