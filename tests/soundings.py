"""Test soundings in SI units (Pa, K, m MSL, m/s), bottom-up."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LEVELS = (1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400, 350, 300, 250, 200, 150, 100)


def weisman_klemp(qv0: float = 0.014) -> dict[str, list[float]]:
    """Weisman & Klemp (1982) analytic supercell sounding with a veering, strengthening wind profile."""
    g, cp, rd = 9.80665, 1004.6662184201462, 287.04749097718457
    theta0, theta_tr, z_tr, t_tr = 300.0, 343.0, 12000.0, 213.0
    z = np.arange(0.0, 16001.0, 250.0)
    th = np.where(z <= z_tr, theta0 + (theta_tr - theta0) * (z / z_tr) ** 1.25,
                  theta_tr * np.exp(g * (z - z_tr) / (cp * t_tr)))
    rh = np.where(z <= z_tr, 1 - 0.75 * (z / z_tr) ** 1.25, 0.25)
    # hydrostatic pressure from the surface (100000 Pa), Exner integration
    pi = np.empty_like(z)
    pi[0] = 1.0
    for i in range(1, len(z)):
        th_mean = 0.5 * (th[i] + th[i - 1])
        pi[i] = pi[i - 1] - g * (z[i] - z[i - 1]) / (cp * th_mean)
    p = 100000.0 * pi ** (cp / rd)
    t = th * pi
    es = 611.2 * np.exp(17.67 * (t - 273.15) / (t - 29.65))
    w = np.minimum(0.6219569 * rh * es / (p - rh * es), qv0)
    e = p * w / (0.6219569 + w)
    ln = np.log(e / 611.2)
    td = 273.15 + 243.5 * ln / (17.67 - ln)
    u = 25.0 * np.tanh(z / 3000.0) - 5.0
    v = 12.0 * np.sin(np.minimum(z, 6000.0) / 6000.0 * math.pi / 2) - 2.0
    return {"p": p.tolist(), "t": t.tolist(), "td": td.tolist(), "z": z.tolist(), "u": u.tolist(), "v": v.tolist()}


def from_open_meteo(data: dict, hour: int) -> dict[str, list[float]]:
    """Build a bottom-up sounding from an Open-Meteo pressure-level response (wind in m/s)."""
    h = data["hourly"]
    elev = float(data.get("elevation") or 0.0)

    def uv(speed: float, direction: float) -> tuple[float, float]:
        rad = math.radians(direction)
        return -speed * math.sin(rad), -speed * math.cos(rad)

    psfc = h["surface_pressure"][hour] * 100.0
    su, sv = uv(h["wind_speed_10m"][hour], h["wind_direction_10m"][hour])
    rows = [(psfc, h["temperature_2m"][hour] + 273.15, h["dew_point_2m"][hour] + 273.15, elev, su, sv)]
    for lvl in LEVELS:
        z = h.get(f"geopotential_height_{lvl}hPa", [None])[hour]
        tt = h.get(f"temperature_{lvl}hPa", [None])[hour]
        dd = h.get(f"dew_point_{lvl}hPa", [None])[hour]
        ws = h.get(f"wind_speed_{lvl}hPa", [None])[hour]
        wd = h.get(f"wind_direction_{lvl}hPa", [None])[hour]
        if None in (z, tt, dd, ws, wd) or lvl * 100.0 >= psfc or z <= elev:
            continue
        u, v = uv(ws, wd)
        rows.append((lvl * 100.0, tt + 273.15, min(dd, tt) + 273.15, float(z), u, v))
    cols = list(zip(*rows, strict=True))
    return {"p": list(cols[0]), "t": list(cols[1]), "td": list(cols[2]), "z": list(cols[3]),
            "u": list(cols[4]), "v": list(cols[5])}


def all_soundings() -> dict[str, dict[str, list[float]]]:
    out = {"weisman_klemp": weisman_klemp()}
    for name, hour in (("houston", 15), ("desmoines", 9)):
        data = json.loads((HERE / "fixtures" / "science" / f"gfs_{name}.json").read_text(encoding="utf-8"))
        out[name] = from_open_meteo(data, hour)
    return out
