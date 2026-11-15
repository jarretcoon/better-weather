import os
import time

import httpx
import pytest
from better_weather.core import http, images, places, settings, visuals
from better_weather.core.sources import radar, satellite

GIF = b"GIF89a" + b"\x00" * 64
JPG = b"\xff\xd8\xff" + b"\x00" * 64


@pytest.fixture
def img_dir(tmp_path):
    settings.set_data_dir(tmp_path)
    yield tmp_path / "images"
    settings.set_data_dir(None)


def _serve(routes, log=None):
    """routes: list of (substring, response or callable); first match wins; else 404."""

    def handler(request):
        url = str(request.url)
        if log is not None:
            log.append(url)
        for needle, resp in routes:
            if needle in url:
                return resp(request) if callable(resp) else resp
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _place(lat=41.6, lon=-93.6, country="US"):
    return places.Place("Test, IA", lat, lon, country=country, timezone="America/Chicago")


def _point_json():
    return {"properties": {"radarStation": "KDMX", "timeZone": "America/Chicago"}}


def test_region_and_sector_picking():
    assert radar.region_for(41.6, -93.6) == "UPPERMISSVLY"
    assert radar.region_for(47.6, -122.3) == "PACNORTHWEST"
    assert radar.region_for(51.5, -0.1) is None
    assert satellite.sector_for(41.6, -93.6)[1] == "umv"
    assert satellite.sector_for(21.3, -157.8)[1] == "hi"
    assert satellite.sector_for(51.5, -0.1) is None


def test_radar_uses_nws_station_image(img_dir):
    log = []
    http.set_transport_for_tests(_serve([
        ("/points/", httpx.Response(200, json=_point_json())),
        ("KDMX_loop.gif", httpx.Response(200, content=GIF, headers={"last-modified": "Sun, 15 Nov 2026 09:00:00 GMT"})),
    ], log))
    r = visuals.radar(_place(), view="local", loop=True)
    assert r["source"] == "NWS radar KDMX" and r["animated"]
    assert r["media"].startswith("MEDIA:") and os.path.exists(r["path"])
    assert r["image_time"] == "3 AM"
    # second call within the TTL reuses the file
    visuals.radar(_place(), view="local", loop=True)
    assert sum("KDMX_loop.gif" in u for u in log) == 1


def test_radar_falls_back_to_iem_map(img_dir):
    http.set_transport_for_tests(_serve([
        ("/points/", httpx.Response(200, json=_point_json())),
        ("radar.weather.gov", httpx.Response(503)),
        ("radmap.php", httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)),
    ]))
    r = visuals.radar(_place(), view="local")
    assert r["source"].startswith("Iowa Environmental Mesonet")
    assert any("backup" in n for n in r["notes"])


def test_radar_outside_us_says_so(img_dir):
    r = visuals.radar(_place(51.5, -0.1, "GB"))
    assert "only available for the US" in r["error"]


def test_html_error_page_is_not_treated_as_an_image(img_dir):
    http.set_transport_for_tests(_serve([
        ("/points/", httpx.Response(200, json=_point_json())),
        ("radar.weather.gov", httpx.Response(200, content=b"<html>maintenance</html>")),
        ("radmap.php", httpx.Response(500)),
    ]))
    with pytest.raises(http.FetchError):
        visuals.radar(_place(), view="local")


def test_stale_picture_is_flagged(img_dir):
    img_dir.mkdir(parents=True, exist_ok=True)
    old = img_dir / "radar-KDMX-0.gif"
    old.write_bytes(GIF)
    then = time.time() - 30 * 60
    os.utime(old, (then, then))
    http.set_transport_for_tests(_serve([
        ("/points/", httpx.Response(200, json=_point_json())),
        ("radar.weather.gov", httpx.Response(503)),
        ("radmap.php", httpx.Response(503)),
    ]))
    r = visuals.radar(_place(), view="local")
    assert r["path"] == str(old) and "30 min old" in r["note"]


def test_satellite_loop_falls_back_to_still_when_no_animation(img_dir):
    http.set_transport_for_tests(_serve([
        ("-600x600.gif", httpx.Response(404)),
        ("/umv/09/1200x1200.jpg", httpx.Response(200, content=JPG)),
    ]))
    r = visuals.satellite(_place(), kind="water_vapor", loop=True)
    assert r["path"].endswith("sat-GOES19-umv-09.jpg") and not r["animated"]
    assert any("No animation" in n for n in r["notes"])


def test_images_dir_prunes_old_files(img_dir):
    img_dir.mkdir(parents=True, exist_ok=True)
    old = img_dir / "old.gif"
    old.write_bytes(GIF)
    then = time.time() - 2 * images.KEEP_SECONDS
    os.utime(old, (then, then))
    images._last_prune = 0
    http.set_transport_for_tests(_serve([("x.test", httpx.Response(200, content=GIF))]))
    images.fetch("https://x.test/a.gif", "a.gif", ttl=60)
    assert not old.exists()
