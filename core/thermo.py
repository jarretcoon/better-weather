"""Sounding thermodynamics and kinematics in plain numpy (SI units: Pa, K, m, m/s).

The formulas follow MetPy 1.7 (Romps 2017 exact LCL, Ambaum 2020 saturation vapor pressure,
virtual-temperature CAPE/CIN between the lowest LFC and the highest EL, Bunkers storm motion,
storm-relative helicity) and the reference ECAPE implementation (Peters et al. 2023, as coded in the
MIT-licensed ``ecape`` package by Robert Capella). Tests compare against values produced by those
packages, so they are never needed at runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# MetPy constants
RD = 287.04749097718457
RV = 461.52311572606084
CP_D = 1004.6662184201462
CP_L = 4219.400000000001
CP_V = 1860.078011865639
EPS = 0.6219569100577033
LV = 2500840.0
G = 9.80665
T0 = 273.16
ES0 = 611.2
KAPPA = RD / CP_D
RHO_WATER = 999.97495

Array = np.ndarray


# --- moisture ----------------------------------------------------------------------------------

def _latent_heat(t: Array | float) -> Array | float:
    return LV - (CP_L - CP_V) * (np.asarray(t) - T0)


def sat_vapor_pressure(t: Array | float) -> Array:
    """Ambaum (2020) over liquid water, as MetPy 1.7."""
    t = np.asarray(t, dtype=float)
    power = (CP_L - CP_V) / RV
    return ES0 * (T0 / t) ** power * np.exp((LV / T0 - _latent_heat(t) / t) / RV)


def sat_mixing_ratio(p: Array | float, t: Array | float) -> Array:
    e = sat_vapor_pressure(t)
    return EPS * e / (np.asarray(p, dtype=float) - e)


def dewpoint_from_vapor_pressure(e: Array | float) -> Array:
    """Invert the saturation curve (Newton steps from Bolton's inverse as the first guess)."""
    e = np.asarray(e, dtype=float)
    ln = np.log(e / 611.2)
    t = 273.15 + 243.5 * ln / (17.67 - ln)
    for _ in range(6):
        es = sat_vapor_pressure(t)
        # d(es)/dT = es * L / (Rv T^2) (Clausius-Clapeyron, matching the formula's slope closely enough)
        t = t - (es - e) / (es * _latent_heat(t) / (RV * t * t))
    return t


def vapor_pressure(p: Array | float, w: Array | float) -> Array:
    w = np.asarray(w, dtype=float)
    return np.asarray(p, dtype=float) * w / (EPS + w)


def virtual_temperature(t: Array | float, w: Array | float) -> Array:
    w = np.asarray(w, dtype=float)
    return np.asarray(t, dtype=float) * (w + EPS) / (EPS * (1 + w))


def theta(p: Array | float, t: Array | float) -> Array:
    return np.asarray(t, dtype=float) * (100000.0 / np.asarray(p, dtype=float)) ** KAPPA


def theta_e(p: Array, t: Array, td: Array) -> Array:
    """Bolton (1980) eq. 39 (used only to pick the most unstable parcel)."""
    p_hpa = np.asarray(p) / 100.0
    e = sat_vapor_pressure(td) / 100.0
    r = EPS * e / (p_hpa - e)
    t_l = 56 + 1.0 / (1.0 / (td - 56) + np.log(t / td) / 800.0)
    th_l = t * (1000.0 / (p_hpa - e)) ** KAPPA * (t / t_l) ** (0.28 * r)
    return th_l * np.exp((3036.0 / t_l - 1.78) * r * (1 + 0.448 * r))


# --- LCL (Romps 2017 via Lambert W, branch -1) -------------------------------------------------

def _lambertw_m1(x: float) -> float:
    if not (-1 / math.e <= x < 0):
        return float("nan")
    if x > -0.25:
        lx = math.log(-x)
        w = lx - math.log(-lx)
    else:
        q = -math.sqrt(2 * (math.e * x + 1))
        w = -1 + q - q * q / 3 + 11 / 72 * q**3
    for _ in range(100):
        ew = math.exp(w)
        f = w * ew - x
        wp1 = w + 1
        if wp1 == 0:
            break
        step = f / (ew * wp1 - (w + 2) * f / (2 * wp1))
        w -= step
        if abs(step) < 1e-14 * max(1.0, abs(w)):
            break
    return w


def lcl(p: float, t: float, td: float) -> tuple[float, float]:
    w = float(sat_mixing_ratio(p, td))
    q = w / (1 + w)
    cp_m = CP_D + q * (CP_V - CP_D)
    r_m = RD + q * (RV - RD)
    ratio = cp_m / r_m
    rh = float(sat_vapor_pressure(td) / sat_vapor_pressure(t))
    a = ratio + (CP_L - CP_V) / RV
    b = -(LV + (CP_L - CP_V) * T0) / (RV * t)
    c = b / a
    w_m1 = _lambertw_m1(rh ** (1 / a) * c * math.exp(c))
    t_lcl = c / w_m1 * t
    return p * (t_lcl / t) ** ratio, t_lcl


# --- parcels ----------------------------------------------------------------------------------

def _moist_dt_dp(p: float, t: float) -> float:
    rs = float(sat_mixing_ratio(p, t))
    return ((RD * t + LV * rs) / (CP_D + LV * LV * rs * EPS / (RD * t * t))) / p


def moist_lapse(pressures: Array, t_start: float, p_start: float) -> Array:
    """Pseudo-adiabat (MetPy's ODE) integrated with RK4 in small pressure steps."""
    out = np.empty(len(pressures))
    p_cur, t_cur = p_start, t_start
    for i, p_target in enumerate(pressures):
        span = p_target - p_cur
        n = max(1, int(abs(span) / 500.0))  # <= 5 hPa steps
        h = span / n
        for _ in range(n):
            k1 = _moist_dt_dp(p_cur, t_cur)
            k2 = _moist_dt_dp(p_cur + h / 2, t_cur + h * k1 / 2)
            k3 = _moist_dt_dp(p_cur + h / 2, t_cur + h * k2 / 2)
            k4 = _moist_dt_dp(p_cur + h, t_cur + h * k3)
            t_cur += h * (k1 + 2 * k2 + 2 * k3 + k4) / 6
            p_cur += h
        out[i] = t_cur
    return out


def parcel_profile(p: Array, t0: float, td0: float) -> Array:
    p_lcl, t_lcl = lcl(float(p[0]), t0, td0)
    out = np.empty(len(p))
    below = p >= p_lcl
    out[below] = t0 * (p[below] / p[0]) ** KAPPA
    above = ~below
    if above.any():
        out[above] = moist_lapse(p[above], t_lcl, p_lcl)
    return out


def _interp_logp(p_new: float, p: Array, values: Array) -> float:
    lp = np.log(p[::-1])
    return float(np.interp(math.log(p_new), lp, values[::-1]))


def with_lcl(p: Array, t: Array, td: Array) -> tuple[Array, Array, Array, Array]:
    """MetPy's parcel_profile_with_lcl: insert the LCL level, return env + parcel profiles."""
    p_lcl, _ = lcl(float(p[0]), float(t[0]), float(td[0]))
    if p_lcl < p[-1] or np.any(np.isclose(p, p_lcl)):
        prof = parcel_profile(p, float(t[0]), float(td[0]))
        return p, t, td, prof
    idx = int(np.searchsorted(-p, -p_lcl))
    p2 = np.insert(p, idx, p_lcl)
    t2 = np.insert(t, idx, _interp_logp(p_lcl, p, t))
    td2 = np.insert(td, idx, _interp_logp(p_lcl, p, td))
    return p2, t2, td2, parcel_profile(p2, float(t[0]), float(td[0]))


def _crossings(p: Array, y: Array) -> tuple[Array, Array]:
    """Append zero crossings of y (interpolated in log p), as MetPy does."""
    xs, ys = [p[0]], [y[0]]
    for i in range(1, len(p)):
        if (y[i - 1] < 0 < y[i]) or (y[i - 1] > 0 > y[i]):
            lp = math.log(p[i - 1]) + (math.log(p[i]) - math.log(p[i - 1])) * (0 - y[i - 1]) / (y[i] - y[i - 1])
            xs.append(math.exp(lp))
            ys.append(0.0)
        xs.append(p[i])
        ys.append(y[i])
    return np.array(xs), np.array(ys)


def _lfc_el(p: Array, b: Array, p_lcl: float) -> tuple[float | None, float | None]:
    """Lowest LFC and highest EL from buoyancy b on p (bottom-up)."""
    x, y = _crossings(p, b)
    ups = [x[i] for i in range(1, len(x)) if y[i] == 0 and i + 1 < len(x) and y[i + 1] > 0 and y[i - 1] <= 0]
    downs = [x[i] for i in range(1, len(x)) if y[i] == 0 and i + 1 < len(x) and y[i + 1] < 0 and y[i - 1] >= 0]
    ups = [u for u in ups if u <= p_lcl + 1e-6]
    if ups:
        lfc_p: float | None = float(ups[0])
    else:
        above = (x <= p_lcl) & (y > 0)
        lfc_p = float(p_lcl) if above.any() and b[np.argmin(np.abs(p - p_lcl))] >= 0 else None
        if lfc_p is None and above.any():
            lfc_p = float(x[above][0])
    if lfc_p is None:
        return None, None
    els = [d for d in downs if d < lfc_p]
    return lfc_p, (float(els[-1]) if els else None)


@dataclass
class Parcel:
    cape: float
    cin: float
    lcl_p: float
    lfc_p: float | None
    el_p: float | None


def cape_cin(p: Array, t: Array, td: Array, prof: Array) -> Parcel:
    p_lcl, _ = lcl(float(p[0]), float(t[0]), float(td[0]))
    below = p > p_lcl
    w_parcel = np.where(below, sat_mixing_ratio(p[0], td[0]), sat_mixing_ratio(p, prof))
    tv_env = virtual_temperature(t, sat_mixing_ratio(p, td))
    tv_par = virtual_temperature(prof, w_parcel)
    b = tv_par - tv_env
    lfc_p, el_p = _lfc_el(p, b, p_lcl)
    if lfc_p is None:
        return Parcel(0.0, 0.0, p_lcl, None, None)
    top = el_p if el_p is not None else float(p[-1])
    x, y = _crossings(p, b)
    m = (x <= lfc_p * (1 + 1e-9)) & (x >= top * (1 - 1e-9))
    cape = RD * float(np.trapezoid(y[m], np.log(x[m]))) if m.sum() > 1 else 0.0
    m = x >= lfc_p * (1 - 1e-9)
    cin = RD * float(np.trapezoid(y[m], np.log(x[m]))) if m.sum() > 1 else 0.0
    # trapezoid over decreasing p gives the sign flipped
    cape, cin = -cape, min(0.0, -cin)
    return Parcel(max(0.0, cape), cin, p_lcl, lfc_p, el_p)


def surface_parcel(p: Array, t: Array, td: Array) -> Parcel:
    return cape_cin(*with_lcl(p, t, td))


def most_unstable_index(p: Array, t: Array, td: Array, depth: float = 30000.0) -> int:
    layer = p >= p[0] - depth
    return int(np.argmax(theta_e(p[layer], t[layer], td[layer])))


def most_unstable_parcel(p: Array, t: Array, td: Array) -> tuple[Parcel, int]:
    i = most_unstable_index(p, t, td)
    return cape_cin(*with_lcl(p[i:], t[i:], td[i:])), i


def _layer_mean(p: Array, values: Array, depth: float) -> float:
    """Pressure-weighted mean over the lowest `depth` Pa (bottom bound interpolated in log p)."""
    top = p[0] - depth
    inside = p >= top
    pp, vv = list(p[inside]), list(values[inside])
    if top > p[-1] and not np.isclose(pp[-1], top):
        pp.append(top)
        vv.append(_interp_logp(top, p, values))
    pa, va = np.array(pp), np.array(vv)
    return float(np.trapezoid(va, pa) / (pa[-1] - pa[0]))


def mixed_layer_parcel(p: Array, t: Array, td: Array, depth: float = 10000.0) -> Parcel:
    th = theta(p, t)
    w = sat_mixing_ratio(p, td)
    th_m, w_m = _layer_mean(p, th, depth), _layer_mean(p, w, depth)
    t0 = th_m * (p[0] / 100000.0) ** KAPPA
    td0 = float(dewpoint_from_vapor_pressure(vapor_pressure(p[0], w_m)))
    keep = p < p[0] - depth
    pp = np.concatenate([[p[0]], p[keep]])
    tt = np.concatenate([[t0], t[keep]])
    dd = np.concatenate([[td0], td[keep]])
    return cape_cin(*with_lcl(pp, tt, dd))


# --- kinematics ---------------------------------------------------------------------------------

def _at_height(z: Array, values: Array, h: float) -> float:
    return float(np.interp(h, z, values))


def _layer_by_height(z: Array, p: Array, u: Array, v: Array, bottom: float, top: float):
    inside = (z > bottom) & (z < top)
    zz = np.concatenate([[bottom], z[inside], [top]])
    pp = np.exp(np.interp(zz, z, np.log(p)))
    uu = np.concatenate([[_at_height(z, u, bottom)], u[inside], [_at_height(z, u, top)]])
    vv = np.concatenate([[_at_height(z, v, bottom)], v[inside], [_at_height(z, v, top)]])
    return zz, pp, uu, vv


def _pressure_weighted_mean(z, p, u, v, bottom, top) -> tuple[float, float]:
    _, pp, uu, vv = _layer_by_height(z, p, u, v, bottom, top)
    span = pp[-1] - pp[0]
    return float(np.trapezoid(uu, pp) / span), float(np.trapezoid(vv, pp) / span)


def bunkers_right(z_agl: Array, p: Array, u: Array, v: Array) -> tuple[float, float]:
    mean_u, mean_v = _pressure_weighted_mean(z_agl, p, u, v, 0.0, 6000.0)
    lo_u, lo_v = _pressure_weighted_mean(z_agl, p, u, v, 0.0, 500.0)
    hi_u, hi_v = _pressure_weighted_mean(z_agl, p, u, v, 5500.0, 6000.0)
    su, sv = hi_u - lo_u, hi_v - lo_v
    mag = math.hypot(su, sv) or 1e-9
    return mean_u + 7.5 * sv / mag, mean_v - 7.5 * su / mag


def bulk_shear(z_agl: Array, p: Array, u: Array, v: Array, depth: float) -> float:
    _, _, uu, vv = _layer_by_height(z_agl, p, u, v, 0.0, depth)
    return math.hypot(uu[-1] - uu[0], vv[-1] - vv[0])


def srh(z_agl: Array, p: Array, u: Array, v: Array, depth: float, storm: tuple[float, float]) -> float:
    _, _, uu, vv = _layer_by_height(z_agl, p, u, v, 0.0, depth)
    sru, srv = uu - storm[0], vv - storm[1]
    layers = sru[1:] * srv[:-1] - sru[:-1] * srv[1:]
    return float(layers[layers > 0].sum() + layers[layers < 0].sum())


def precipitable_water(p: Array, td: Array) -> float:
    """Millimetres of water in the column (MetPy's definition)."""
    w = sat_mixing_ratio(p, td)
    return float(-np.trapezoid(w, p) / (G * RHO_WATER) * 1000.0)


def lapse_rate(p: Array, t: Array, z: Array, p_bottom: float, p_top: float) -> float | None:
    if p[0] < p_bottom or p[-1] > p_top:
        return None
    t_b, t_t = _interp_logp(p_bottom, p, t), _interp_logp(p_top, p, t)
    z_b, z_t = _interp_logp(p_bottom, p, z), _interp_logp(p_top, p, z)
    return (t_b - t_t) / (z_t - z_b) * 1000.0


def freezing_level(z: Array, t: Array) -> float | None:
    for i in range(1, len(t)):
        if t[i - 1] > 273.15 >= t[i]:
            return float(z[i - 1] + (z[i] - z[i - 1]) * (t[i - 1] - 273.15) / (t[i - 1] - t[i]))
    return None


# --- ECAPE (Peters et al. 2023, as in the reference implementation) ---------------------------------

def ecape(z_msl: Array, p: Array, t: Array, td: Array, u: Array, v: Array, mu_cape: float) -> float | None:
    """Entraining CAPE for the most-unstable parcel. Mirrors ``ecape.calc.calc_ecape`` step by step."""
    if mu_cape <= 0:
        return 0.0
    i = most_unstable_index(p, t, td)
    t_mu, td_mu = t[i], td[i]
    # The reference lifts the MU parcel's T/Td from the first pressure level and finds LFC/EL with
    # plain (not virtual) temperatures, taking the topmost LFC and EL.
    prof = parcel_profile(p, float(t_mu), float(td_mu))
    b = prof - t
    x, y = _crossings(p, b)
    ups = [x[k] for k in range(1, len(x) - 1) if y[k] == 0 and y[k + 1] > 0 and y[k - 1] <= 0]
    downs = [x[k] for k in range(1, len(x) - 1) if y[k] == 0 and y[k + 1] < 0 and y[k - 1] >= 0]
    p_lcl, _ = lcl(float(p[0]), float(t_mu), float(td[0]))  # MetPy lfc(): dewpoint_start = dewpoint[0]
    ups = [q for q in ups if q <= p_lcl + 1e-6] or ([p_lcl] if (b[p <= p_lcl] > 0).any() else [])
    if not ups:
        return 0.0
    lfc_p = ups[-1]
    els = [q for q in downs if q < lfc_p]
    el_p = els[-1] if els else float(p[-1])
    lfc_idx = int(np.nonzero(p - lfc_p > 0)[0][-1])
    el_idx = int(np.nonzero(p - el_p > 0)[0][-1])
    el_z = float(z_msl[el_idx])

    q = sat_mixing_ratio(p, td)
    q = q / (1 + q)  # specific humidity from dewpoint
    mse = CP_D * t + G * z_msl + LV * q
    mse_bar = np.cumsum(mse) / np.arange(1, len(mse) + 1)
    mse_star = CP_D * t + G * z_msl + LV * sat_mixing_ratio(p, t)
    arg = -(G / (CP_D * t)) * (mse_bar - mse_star)
    ncape = float(np.sum((0.5 * arg[lfc_idx:el_idx] + 0.5 * arg[lfc_idx + 1:el_idx + 1])
                         * (z_msl[lfc_idx + 1:el_idx + 1] - z_msl[lfc_idx:el_idx])))

    z_agl = z_msl - z_msl[0]
    storm = bunkers_right(z_agl, p, u, v)
    low = (z_agl >= 0) & (z_agl <= 1000)
    v_sr = float(np.mean(np.hypot(u[low] - storm[0], v[low] - storm[1])))
    if v_sr <= 0:
        return None

    sigma, alpha, l_mix, pr, ksq = 1.6, 0.8, 120.0, 1.0 / 3.0, 0.18
    psi = (ksq * alpha**2 * math.pi**2 * l_mix) / (4.0 * pr * sigma**2 * el_z)
    a = v_sr**2 / 2.0
    k = 4 * psi / v_sr**2
    b_term = (-1 - psi - (2 * psi / v_sr**2) * ncape) / k
    c_term = math.sqrt(max(0.0, (1 + psi + (2 * psi / v_sr**2) * ncape) ** 2
                           + 8 * (psi / v_sr**2) * (mu_cape - psi * ncape))) / k
    value = a + b_term + c_term
    return max(0.0, value)
