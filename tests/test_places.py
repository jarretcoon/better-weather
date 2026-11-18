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


def _city(name, admin1, cc, lat=40.0, lon=-90.0, feature=None, population=None):
    out = {"name": name, "admin1": admin1, "country_code": cc, "country": cc, "latitude": lat, "longitude": lon,
           "timezone": "America/Chicago"}
    if feature:
        out["feature_code"] = feature
    if population:
        out["population"] = population
    return out


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


def test_a_state_on_its_own_asks_which_place(monkeypatch):
    calls = []
    table = {"iowa": [_city("Iowa", "Louisiana", "US", feature="PPL", population=3219),
                      _city("Iowa City", "Iowa", "US", feature="PPLA2", population=74220)],
             "wyoming": [_city("Wyoming", "Michigan", "US", feature="PPL", population=75275)]}
    monkeypatch.setattr(places.openmeteo, "geocode", lambda name, count=10: calls.append(name) or table.get(
        name.lower(), []))
    for text in ("Iowa", "iowa", "IA"):
        with pytest.raises(places.PlaceError) as err:
            places.resolve(text)
        assert str(err.value) == "Which place in Iowa?" and "'Des Moines, IA'" in err.value.hint
    assert calls == ["Iowa", "iowa"], "a two-letter code isn't searched at all"
    with pytest.raises(places.PlaceError, match="Which place in Wyoming"):
        places.resolve("Wyoming")  # not the city in Michigan
    with pytest.raises(places.PlaceError, match="Which place in American Samoa"):
        places.resolve("American Samoa")  # the geocoder has nothing by that name
    assert places.resolve("Iowa, LA").region == "Louisiana", "the town called Iowa, with its state"


def test_a_state_name_that_is_also_a_big_place(monkeypatch):
    monkeypatch.setattr(places.openmeteo, "geocode", _geo({
        "new york": [_city("New York", "New York", "US", 40.71, -74.01, "PPL", 8804190),
                     _city("New York", "England", "GB", feature="PPL")],
        "washington": [_city("Washington D.C.", "District of Columbia", "US", 38.9, -77.04, "PPLC", 689545),
                       _city("Washington", "Pennsylvania", "US", feature="PPLA2", population=13497)],
        "georgia": [_city("Georgia", None, "GE", 42.0, 43.5, "PCLI", 3935766),
                    _city("Georgia", "Alabama", "US", feature="PPL")],
        "puerto rico": [_city("Puerto Rico", None, "PR", 18.25, -66.5, "PCLD", 3184835)]}))
    nyc = places.resolve("New York")
    assert nyc.region == "New York" and nyc.note is None, "New York City is in New York: nothing to say"
    dc = places.resolve("Washington")
    assert dc.region == "District of Columbia" and "Seattle, WA" in (dc.note or "")
    country = places.resolve("Georgia")
    assert country.country == "GE", "not blocked: the country, with a note saying so"
    assert country.note == ("Using Georgia, GE. For the US state of Georgia, name a place in it, "
                            "like 'Atlanta, GA'.")
    assert places.resolve("Puerto Rico").country == "PR" and places.resolve("Puerto Rico").note is None


def test_us_shorthands(monkeypatch):
    monkeypatch.setattr(places.nws, "station_info", lambda code: {"name": "Central Park", "lat": 40.78,
                                                                  "lon": -73.97, "timezone": "America/New_York"})
    monkeypatch.setattr(places.openmeteo, "geocode", _geo({
        "la": [_city("La", "Kampong Speu Province", "KH", 11.5, 104.5)],
        "los angeles": [_city("Los Angeles", "California", "US", 34.05, -118.24)],
        "new york": [_city("New York", "New York", "US", 40.71, -74.01)],
        "san francisco": [_city("San Francisco", "California", "US", 37.77, -122.42)],
        "washington": [_city("Washington D.C.", "District of Columbia", "US", 38.9, -77.04)],
        "philadelphia": [_city("Philadelphia", "Pennsylvania", "US", 39.95, -75.16)],
        "las vegas": [_city("Las Vegas", "Nevada", "US", 36.17, -115.14)]}))
    for text, region in (("LA", "California"), ("L.A.", "California"), ("NYC", "New York"),
                         ("nyc", "New York"), ("SF", "California"), ("DC", "District of Columbia"),
                         ("D.C.", "District of Columbia"), ("Philly", "Pennsylvania"), ("vegas", "Nevada")):
        place = places.resolve(text)
        assert place.region == region and place.kind == "search", text
    places.save_place("LA", places.Place("Lake Ahquabi", 41.29, -93.59))
    assert places.resolve("la").kind == "saved", "the user's own names come first"


def test_saint_fort_and_mount_spelled_either_way(monkeypatch):
    monkeypatch.setattr(places.openmeteo, "geocode", _geo({
        "st paul": [_city("St Paul's", "England", "GB", feature="PPL")],
        "st. paul": [_city("Saint Paul", "Alaska", "US", 57.12, -170.28, "PPL", 479),
                     _city("St. Paul", "Alberta", "CA", 53.98, -111.29, "PPL", 5728)],
        "saint paul": [_city("Saint Paul", "Minnesota", "US", 44.94, -93.09, "PPLA", 303176),
                       _city("Saint Paul", "Nebraska", "US", feature="PPLA2", population=2290)],
        "st. louis": [_city("St Louis", "Missouri", "US", 38.63, -90.2, "PPLA2", 279695)],
        "saint louis": [_city("Saint-Louis", "Saint-Louis", "SN", feature="PPLA", population=254171)],
        "st george": [_city("St George", "Wales", "GB", feature="PPL")],
        "st. george": [_city("St. George", "Utah", "US", 37.1, -113.58, "PPL", 72897)],
        "fort myers": [_city("Fort Myers", "Florida", "US", 26.62, -81.84, "PPLA2", 74013)],
        "st petersburg": [_city("St Petersburg", "St. Petersburg", "RU", 59.94, 30.31, "PPLA", 5351935),
                          _city("St Petersburg-Clearwater International Airport", "Florida", "US", feature="AIRP")],
        "st. petersburg": [_city("St. Petersburg", "Florida", "US", 27.77, -82.68, "PPL", 257083)],
        "mount vernon": [_city("Mt. Vernon", "Illinois", "US", 38.32, -88.9, "PPLA2", 15087)]}))
    assert places.resolve("St Paul, MN").region == "Minnesota"
    assert places.resolve("St. Paul, MN").region == "Minnesota"
    assert places.resolve("St Paul").region == "Minnesota", "alone: the biggest Saint Paul"
    assert places.resolve("St. Louis, MO").region == "Missouri"
    assert places.resolve("Saint Louis, MO").region == "Missouri"
    assert places.resolve("St George, UT").region == "Utah"
    assert places.resolve("Ft Myers, FL").name == "Fort Myers, FL"
    assert places.resolve("Ft. Myers").name == "Fort Myers, FL"
    assert places.resolve("Mt Vernon, IL").region == "Illinois"
    assert places.resolve("St Petersburg, FL").name == "St. Petersburg, FL", "the city, not its airport"
    assert places.resolve("St Petersburg").country == "RU", "found as typed: no second guessing"
    with pytest.raises(places.PlaceError, match="'St George' in Iowa"):
        places.resolve("St George, IA")


def test_a_name_that_lands_abroad_says_so(monkeypatch, home):
    monkeypatch.setattr(places.openmeteo, "geocode", _geo({
        "paris": [_city("Paris", "Île-de-France Region", "FR", 48.85, 2.35, "PPLC", 2138551),
                  _city("Paris", "Texas", "US", 33.66, -95.56, "PPLA2", 24782)],
        "tokyo": [_city("Tokyo", "Tokyo", "JP", 35.69, 139.69, "PPLC"), _city("Tokyo", "Madang", "PG")],
        "ames": [_city("Ames", "Iowa", "US", 42.03, -93.62), _city("Ames", "Hauts-de-France", "FR")]}))
    paris = places.resolve("Paris")
    assert paris.country == "FR"
    assert paris.note == "Using Paris, Île-de-France Region, FR. For Paris in the US, add the state, like 'Paris, TX'."
    assert places.resolve("Paris, TX").note is None
    assert places.resolve("Tokyo").note is None, "no US place by that name: nothing to mix up"
    assert places.resolve("Ames").note is None, "a US pick needs no note"
    home.set("home", "Paris")
    assert places.resolve(None).note is None, "home: the user saw the note when they set it"
