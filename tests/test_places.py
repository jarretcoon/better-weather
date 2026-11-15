import pytest
from better_weather.core import places


def test_home_required_with_helpful_hint():
    with pytest.raises(places.PlaceError) as err:
        places.resolve("")
    assert "set_home" in err.value.hint


def test_home_resolves(home):
    place = places.resolve(None)
    assert place.kind == "home" and place.name == "Des Moines, IA" and place.country == "US"


def test_city_state_prefers_the_right_state():
    place = places.resolve("Des Moines, IA")
    assert place.region == "Iowa" and abs(place.lat - 41.6) < 0.1


def test_city_country():
    place = places.resolve("London, UK")
    assert place.country == "GB" and not place.maybe_us


def test_zip_code_is_us_only():
    place = places.resolve("50309")
    assert place.kind == "zip" and place.country == "US" and "50309" in place.name


def test_airport_code():
    place = places.resolve("KDSM")
    assert place.kind == "airport" and abs(place.lat - 41.53) < 0.05


def test_coordinates():
    place = places.resolve("41.6, -93.6")
    assert place.kind == "coords" and place.lat == 41.6
    with pytest.raises(places.PlaceError):
        places.resolve(None, 120, 0)


def test_saved_places_round_trip():
    stored = places.save_place("Work", places.resolve("41.6,-93.6"))
    assert stored["kind"] == "saved"
    assert places.resolve("work").kind == "saved"
    assert places.remove_place("WORK") is True
    assert places.remove_place("work") is False


def test_unknown_place_explains_itself(monkeypatch):
    monkeypatch.setattr(places.openmeteo, "geocode", lambda name, count=10: [])
    with pytest.raises(places.PlaceError) as err:
        places.resolve("Nowhereville Zzq")
    assert "ZIP" in err.value.hint


def test_saved_places_live_in_the_data_folder_and_carry_over(tmp_path):
    from better_weather.core import settings

    settings.current().state_set("places", {"work": {"name": "Ankeny, Iowa", "lat": 41.73, "lon": -93.6}})
    settings.set_data_dir(tmp_path)
    try:
        assert places.saved_places()["work"]["name"] == "Ankeny, Iowa"  # copied from 0.8.x state
        assert (tmp_path / "places.json").exists()
        places.save_place("Mom", places.Place("Ames, Iowa", 42.03, -93.62))
        assert set(places.saved_places()) == {"work", "mom"} and places.remove_place("work")
        assert set(places.saved_places()) == {"mom"}
    finally:
        settings.set_data_dir(None)
