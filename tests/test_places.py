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


def _geo(table):
    return lambda name, count=10: table.get(name.lower(), [])


def _city(name, admin1, cc, lat=40.0, lon=-90.0):
    return {"name": name, "admin1": admin1, "country_code": cc, "country": cc, "latitude": lat, "longitude": lon,
            "timezone": "America/Chicago"}


def test_three_letter_airport_codes(monkeypatch):
    stations = {"KORD": {"name": "Chicago O'Hare", "lat": 41.98, "lon": -87.9, "timezone": "America/Chicago"},
                "PHNL": {"name": "Honolulu", "lat": 21.32, "lon": -157.92, "timezone": "Pacific/Honolulu"},
                "PRIO": {"name": "Prineville 4NW, OR", "lat": 44.3, "lon": -120.9, "timezone": "America/Los_Angeles"}}
    monkeypatch.setattr(places.nws, "station_info", lambda code: stations.get(code))
    monkeypatch.setattr(places.openmeteo, "geocode", _geo({
        "ord": [_city("Ordu", "Ordu", "TR", 40.98, 37.88)],
        "rio": [_city("Rio de Janeiro", "Rio de Janeiro", "BR", -22.9, -43.2)]}))
    assert places.resolve("ORD").kind == "airport" and "KORD" in places.resolve("ORD").name
    assert places.resolve("ord").kind == "airport", "lower case, and the search went abroad: the US airport"
    assert "PHNL" in places.resolve("HNL").name, "Hawaii's stations start with P"
    assert places.resolve("RIO").country == "BR", "a P code in Oregon isn't an airport for RIO"
    assert places.resolve("Rio").country == "BR", "a capitalised word stays a place name"


def test_a_state_after_the_name_without_a_comma(monkeypatch):
    monkeypatch.setattr(places.openmeteo, "geocode", _geo({
        "clarion": [_city("Clarion", "Pennsylvania", "US"), _city("Clarion", "Iowa", "US", 42.73, -93.73)],
        "clarion iowa": [], "buffalo": [_city("Buffalo", "New York", "US", 42.89, -78.88)]}))
    assert places.resolve("clarion iowa").region == "Iowa"
    assert places.resolve("clarion ia").region == "Iowa"
    assert places.resolve("buffalo new york").region == "New York"
    assert places._with_state_comma("west virginia") is None, "a state's own name isn't split"
    assert places._with_state_comma("kansas city") is None


def test_a_us_state_never_lands_in_another_country(monkeypatch):
    monkeypatch.setattr(places.openmeteo, "geocode", _geo({
        "barrow": [_city("Barrow in Furness", "England", "GB", 54.1, -3.2)],
        "utqiagvik": [_city("Utqiagvik", "Alaska", "US", 71.29, -156.79)],
        "smallville": [_city("Smallville", "England", "GB", 52.0, -1.0)]}))
    assert places.resolve("Barrow, AK").region == "Alaska", "renamed in 2016"
    assert places.resolve("Barrow, Alaska").region == "Alaska"
    with pytest.raises(places.PlaceError) as err:
        places.resolve("Smallville, KS")
    assert "Kansas" in str(err.value)


def test_us_territories_match_their_own_country_code(monkeypatch):
    monkeypatch.setattr(places.openmeteo, "geocode", _geo({
        "san juan": [_city("San Juan", "San Juan", "AR", -31.5, -68.5),
                     _city("San Juan", "San Juan", "PR", 18.47, -66.11)]}))
    place = places.resolve("San Juan, PR")
    assert place.country == "PR" and place.note is None
