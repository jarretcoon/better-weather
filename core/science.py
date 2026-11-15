"""Storm science from a model sounding: CAPE/CIN for three parcels, LCL/LFC/EL, ECAPE, lapse rate,
shear, storm motion, helicity, precipitable water, freezing level and the tornado/supercell composites,
plus plain-language readings. Math in ``thermo`` (numpy, checked against MetPy and ``ecape``)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import http, thermo, units
from .clock import now_utc
from .places import Place

URL = "https://api.open-meteo.com/v1/forecast"
LEVELS = (1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400, 350, 300, 250, 200, 150, 100)
MODELS = {"gfs": ("gfs_seamless", "GFS"), "hrrr": ("ncep_hrrr_conus", "HRRR (US only)")}
MS_TO_KT = 1.943844


def _fetch(place: Place, model: str) -> dict[str, Any]:
    level_vars = [f"{v}_{lvl}hPa" for lvl in LEVELS
                  for v in ("temperature", "dew_point", "wind_speed", "wind_direction", "geopotential_height")]
    surface = "temperature_2m,dew_point_2m,surface_pressure,wind_speed_10m,wind_direction_10m,cape"
    hourly = surface + "," + ",".join(level_vars)
    return http.get_json(URL, {"latitude": round(place.lat, 2), "longitude": round(place.lon, 2), "hourly": hourly,
                               "models": MODELS[model][0], "forecast_days": 3, "timezone": "auto",
                               "wind_speed_unit": "ms"}, ttl=60 * 60, max_stale=6 * 3600).data or {}


def _uv(speed: float, direction: float) -> tuple[float, float]:
    rad = math.radians(direction)
    return -speed * math.sin(rad), -speed * math.cos(rad)


def build_profile(data: dict[str, Any], i: int) -> dict[str, np.ndarray] | None:
    h = data.get("hourly") or {}
    elev = float(data.get("elevation") or 0.0)

    def val(key: str) -> float | None:
        seq = h.get(key) or []
        return units.num(seq[i]) if i < len(seq) else None

    psfc, t2, d2 = val("surface_pressure"), val("temperature_2m"), val("dew_point_2m")
    ws, wd = val("wind_speed_10m"), val("wind_direction_10m")
    if None in (psfc, t2, d2, ws, wd):
        return None
    assert psfc is not None and t2 is not None and d2 is not None and ws is not None and wd is not None
    su, sv = _uv(ws, wd)
    rows = [(psfc * 100.0, t2 + 273.15, min(d2, t2) + 273.15, elev, su, sv)]
    for lvl in LEVELS:
        z, tt, dd = val(f"geopotential_height_{lvl}hPa"), val(f"temperature_{lvl}hPa"), val(f"dew_point_{lvl}hPa")
        s, d = val(f"wind_speed_{lvl}hPa"), val(f"wind_direction_{lvl}hPa")
        if None in (z, tt, dd, s, d):
            continue
        assert z is not None and tt is not None and dd is not None and s is not None and d is not None
        if lvl >= psfc or z <= elev:  # below ground (both in hPa)
            continue
        u, v = _uv(s, d)
        rows.append((lvl * 100.0, tt + 273.15, min(dd, tt) + 273.15, z, u, v))
    if len(rows) < 8:
        return None
    cols = [np.array(c, dtype=float) for c in zip(*rows, strict=True)]
    return dict(zip(("p", "t", "td", "z", "u", "v"), cols, strict=True))


def _height_at(p_target: float | None, prof: dict[str, np.ndarray]) -> float | None:
    if p_target is None:
        return None
    p, z = prof["p"], prof["z"]
    return float(np.interp(math.log(p_target), np.log(p[::-1]), z[::-1]) - z[0])


def _instability(cape: float) -> str:
    return ("none" if cape < 100 else "weak" if cape < 1000 else "moderate" if cape < 2500
            else "strong" if cape < 4000 else "extreme")


def compute(prof: dict[str, np.ndarray]) -> dict[str, Any]:
    p, t, td, z, u, v = prof["p"], prof["t"], prof["td"], prof["z"], prof["u"], prof["v"]
    agl = z - z[0]
    sb = thermo.surface_parcel(p, t, td)
    ml = thermo.mixed_layer_parcel(p, t, td)
    mu, _ = thermo.most_unstable_parcel(p, t, td)
    storm = thermo.bunkers_right(agl, p, u, v)
    shear6 = thermo.bulk_shear(agl, p, u, v, 6000.0)
    srh1 = thermo.srh(agl, p, u, v, 1000.0, storm)
    srh3 = thermo.srh(agl, p, u, v, 3000.0, storm)
    lcl_h = _height_at(sb.lcl_p, prof) or 0.0
    lcl_term = (2000.0 - min(max(lcl_h, 1000.0), 2000.0)) / 1000.0
    stp_shear = 0.0 if shear6 < 12.5 else min(shear6, 30.0) / 20.0
    stp = max(0.0, sb.cape / 1500.0 * lcl_term * srh1 / 150.0 * stp_shear)
    scp_shear = 0.0 if shear6 < 10.0 else min(shear6, 20.0) / 20.0
    scp = max(0.0, mu.cape / 1000.0 * srh3 / 50.0 * scp_shear)
    return {
        "sb": sb, "ml": ml, "mu": mu, "storm": storm, "shear6": shear6, "srh1": srh1, "srh3": srh3,
        "lcl_h": lcl_h, "stp": stp, "scp": scp,
        "ecape": thermo.ecape(z, p, t, td, u, v, mu.cape),
        "lapse_700_500": thermo.lapse_rate(p, t, z, 70000.0, 50000.0),
        "pwat_mm": thermo.precipitable_water(p, td),
        "freezing_h": (lambda f: None if f is None else f - z[0])(thermo.freezing_level(z, t)),
    }


def sounding(place: Place, units_: str, clock: str, *, model: str = "gfs", hours_ahead: int | None = None,
             at_peak: bool = False) -> dict[str, Any]:
    model = model if model in MODELS else "gfs"
    data = _fetch(place, model)
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        raise http.FetchError(URL, f"no {MODELS[model][1]} data here")
    tz = units.zone(data.get("timezone"), data.get("utc_offset_seconds"))
    now = now_utc().astimezone(tz)
    stamps = [units.parse_time(s) for s in times]
    future = [i for i, s in enumerate(stamps) if s is not None and s.replace(tzinfo=tz) >= now.replace(
        minute=0, second=0, microsecond=0)]
    if not future:
        raise http.FetchError(URL, "the model run has no future hours")
    idx = future[0]
    if at_peak:
        window = future[:24]
        capes = hourly.get("cape") or []
        idx = max(window, key=lambda i: units.num(capes[i] if i < len(capes) else 0) or 0)
    elif hours_ahead:
        idx = future[min(max(0, int(hours_ahead)), len(future) - 1)]
    prof = build_profile(data, idx)
    if prof is None:
        raise http.FetchError(URL, "incomplete sounding for that hour")
    r = compute(prof)
    us = units_ != "metric"

    def height(m: float | None) -> str | None:
        return None if m is None else (f"{round(m * 3.281 / 10) * 10:,} ft" if us else f"{round(m / 10) * 10:,} m")

    def parcel(pc: thermo.Parcel) -> dict[str, Any]:
        return {k: v for k, v in {"cape_jkg": round(pc.cape), "cin_jkg": round(pc.cin),
                                  "lcl": height(_height_at(pc.lcl_p, prof)), "lfc": height(_height_at(pc.lfc_p, prof)),
                                  "el": height(_height_at(pc.el_p, prof))}.items() if v is not None}

    shear_kt = r["shear6"] * MS_TO_KT
    su, sv = r["storm"]
    storm_from = (math.degrees(math.atan2(-su, -sv)) + 360) % 360
    ml_cape = r["ml"].cape
    read: list[str] = [f"Instability: {_instability(ml_cape)} (mixed-layer CAPE {round(ml_cape)} J/kg)."]
    if r["ecape"] is not None and r["mu"].cape > 100:
        read.append(f"After mixing with drier air, updrafts would get about {round(r['ecape'])} J/kg (ECAPE).")
    if r["sb"].cin < -100 and r["sb"].cape > 100:
        read.append(f"Strong cap (CIN {round(r['sb'].cin)} J/kg): storms need a strong trigger to form.")
    shear_words = ("supports supercells" if shear_kt >= 40 else "supports organized storms" if shear_kt >= 25
                   else "weak, storms would be short-lived")
    read.append(f"Shear: {shear_words} ({round(shear_kt)} kt, 0-6 km).")
    if r["srh1"] >= 100:
        read.append(f"Low-level spin is notable (0-1 km helicity {round(r['srh1'])} m²/s²).")
    if r["stp"] >= 1:
        read.append(f"Significant Tornado Parameter {r['stp']:.1f}: the environment favors strong tornadoes IF storms "
                    "form and stay surface-based.")
    if ml_cape < 100 and r["mu"].cape < 100:
        read = [read[0], "No meaningful thunderstorm energy at this hour."]
        if shear_kt >= 40:
            read.append(f"Wind shear is strong ({round(shear_kt)} kt), but there's no instability for storms to use.")

    out: dict[str, Any] = {
        "place": place.public(), "model": MODELS[model][1],
        "valid": units.day_clock(valid.replace(tzinfo=tz), clock, now) if (valid := stamps[idx]) else None,
        "reading": read,
        "parcels": {"surface_based": parcel(r["sb"]), "mixed_layer_100hPa": parcel(r["ml"]),
                    "most_unstable": parcel(r["mu"])},
        "ecape_jkg": None if r["ecape"] is None else round(r["ecape"]),
        "lapse_rate_700_500_c_per_km": None if r["lapse_700_500"] is None else round(r["lapse_700_500"], 1),
        "bulk_shear_0_6km": f"{round(shear_kt)} kt" if us else f"{r['shear6']:.0f} m/s",
        "storm_motion_right": (f"from {round(storm_from)}° at {round(math.hypot(su, sv) * MS_TO_KT)} kt" if us
                               else f"from {round(storm_from)}° at {math.hypot(su, sv):.0f} m/s"),
        "helicity_0_1km": round(r["srh1"]), "helicity_0_3km": round(r["srh3"]),
        "precipitable_water": f"{r['pwat_mm'] / 25.4:.2f} in" if us else f"{r['pwat_mm']:.0f} mm",
        "freezing_level": height(r["freezing_h"]) or "at the ground",
        "significant_tornado_parameter": round(r["stp"], 1),
        "supercell_composite_parameter": round(r["scp"], 1),
        "model_cape_jkg": units.num((hourly.get("cape") or [None] * (idx + 1))[idx]),
        "source": f"{MODELS[model][1]} model sounding via Open-Meteo; calculations checked against MetPy",
        "how_to_read": ("Model guidance, not observations. Composites use fixed layers (0-1 km, 0-3 km, 0-6 km). "
                        "Give the 'reading' lines in plain words; quote numbers only if asked."),
    }
    return {k: v for k, v in out.items() if v is not None}
