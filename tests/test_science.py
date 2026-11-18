"""The numpy port against MetPy 1.7 and the reference `ecape` package (values in reference.json,
regenerated with tests/make_science_reference.py)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest
from better_weather.core import clock, http, places, science, thermo
from soundings import all_soundings

REF = json.loads((Path(__file__).parent / "fixtures" / "science" / "reference.json").read_text(encoding="utf-8"))
SOUNDINGS = all_soundings()


def close(mine, ref, rel, absolute):
    assert mine == pytest.approx(ref, rel=rel, abs=absolute), (mine, ref)


@pytest.mark.parametrize("name", sorted(SOUNDINGS))
def test_matches_metpy_and_reference_ecape(name):
    s = {k: np.array(v, dtype=float) for k, v in SOUNDINGS[name].items()}
    p, t, td, z, u, v = s["p"], s["t"], s["td"], s["z"], s["u"], s["v"]
    agl = z - z[0]
    ref = REF[name]
    sb, ml = thermo.surface_parcel(p, t, td), thermo.mixed_layer_parcel(p, t, td)
    mu, _ = thermo.most_unstable_parcel(p, t, td)
    for mine, key in ((sb.cape, "sb_cape"), (ml.cape, "ml_cape"), (mu.cape, "mu_cape")):
        close(mine, ref[key], 0.03, 5)
    for mine, key in ((sb.cin, "sb_cin"), (ml.cin, "ml_cin"), (mu.cin, "mu_cin")):
        close(mine, ref[key], 0.10, 3)
    close(thermo.lcl(p[0], t[0], td[0])[0], ref["lcl_p"], 0.0005, 20)
    storm = thermo.bunkers_right(agl, p, u, v)
    close(storm[0], ref["storm_u"], 0.02, 0.1)
    close(storm[1], ref["storm_v"], 0.02, 0.1)
    close(thermo.srh(agl, p, u, v, 1000, storm), ref["srh1"], 0.02, 1)
    close(thermo.srh(agl, p, u, v, 3000, storm), ref["srh3"], 0.02, 1)
    close(thermo.bulk_shear(agl, p, u, v, 6000), ref["shear6"], 0.01, 0.1)
    close(thermo.precipitable_water(p, td), ref["pwat_mm"], 0.005, 0.1)
    if ref["ecape"] is not None:
        close(thermo.ecape(z, p, t, td, u, v, mu.cape), ref["ecape"], 0.03, 5)


def test_lambert_w_branch():
    for x in (-0.36, -0.2, -1e-3, -1e-8):
        w = thermo._lambertw_m1(x)
        assert w <= -1 and abs(w * np.exp(w) - x) < 1e-12


def test_sounding_tool_reads_a_real_gfs_profile():
    data = json.loads((Path(__file__).parent / "fixtures" / "science" / "gfs_houston.json").read_text(encoding="utf-8"))
    clock.freeze_for_tests(datetime(2026, 11, 15, 20, 0, tzinfo=UTC))  # 2 PM CST in Houston
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
    place = places.Place("Houston, TX", 29.76, -95.37, country="US", timezone="America/Chicago")
    r = science.sounding(place, "us", "12h")
    assert r["model"] == "GFS" and r["valid"] == "2 PM"
    assert set(r["parcels"]) == {"surface_based", "mixed_layer_100hPa", "most_unstable"}
    assert r["bulk_shear_0_6km"].endswith(" kt") and r["precipitable_water"].endswith(" in")
    assert r["reading"][0].startswith("Instability:")


def test_weisman_klemp_reads_as_a_supercell_day():
    s = {k: np.array(v, dtype=float) for k, v in SOUNDINGS["weisman_klemp"].items()}
    r = science.compute(s)
    assert r["ml"].cape > 1500 and r["shear6"] * science.MS_TO_KT > 40 and r["scp"] > 1


def _models(gfs, hrrr):
    """Open-Meteo by model: a dict answers, None is its 400 outside the HRRR grid."""
    def handler(request):
        if request.url.params.get("models") == "ncep_hrrr_conus":
            if hrrr is None:
                return httpx.Response(400, json={"error": True, "reason": "No data is available for this location"})
            return httpx.Response(200, json=hrrr)
        return httpx.Response(200, json=gfs)
    return httpx.MockTransport(handler)


def test_hrrr_outside_its_area_falls_back_to_gfs():
    data = json.loads((Path(__file__).parent / "fixtures" / "science" / "gfs_houston.json").read_text(encoding="utf-8"))
    clock.freeze_for_tests(datetime(2026, 11, 15, 20, 0, tzinfo=UTC))
    place = places.Place("London, England, GB", 51.5, -0.13, country="GB", timezone="Europe/London")
    http.set_transport_for_tests(_models(data, None))
    r = science.sounding(place, "us", "12h", model="hrrr")
    assert r["model"] == "GFS" and r["notes"] == [science.HRRR_AREA]
    assert "continental US" in science.HRRR_AREA


def test_hrrr_where_it_has_data_and_beyond_its_last_hour():
    data = json.loads((Path(__file__).parent / "fixtures" / "science" / "gfs_houston.json").read_text(encoding="utf-8"))
    clock.freeze_for_tests(datetime(2026, 11, 15, 20, 0, tzinfo=UTC))  # 2 PM CST: hour 14 of the run
    place = places.Place("Houston, TX", 29.76, -95.37, country="US", timezone="America/Chicago")
    hrrr = json.loads(json.dumps(data))
    pressure = hrrr["hourly"]["surface_pressure"]
    hrrr["hourly"]["surface_pressure"] = [v if i < 18 else None for i, v in enumerate(pressure)]  # ends at 6 PM
    http.set_transport_for_tests(_models(data, hrrr))
    r = science.sounding(place, "us", "12h", model="hrrr", hours_ahead=2)
    assert r["model"] == "HRRR (US only)" and r["valid"] == "4 PM" and "notes" not in r
    r = science.sounding(place, "us", "12h", model="hrrr", hours_ahead=6)
    assert r["model"] == "GFS" and r["notes"] == [science.HRRR_RANGE] and r["valid"] == "8 PM"
