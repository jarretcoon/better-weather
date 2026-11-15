from better_weather.core import desktop_api


def test_chip_payload_for_home(home):
    desktop_api._cached = None
    p = desktop_api.now_payload(force=True)
    assert p["place"] == "Des Moines, IA" and p["temp"].endswith("°F")
    assert isinstance(p["alerts"], list) and p["summary"].startswith("Des Moines")


def test_chip_payload_without_home():
    desktop_api._cached = None
    p = desktop_api.now_payload(force=True)
    assert "No home location" in p["error"]
