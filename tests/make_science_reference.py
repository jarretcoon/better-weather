"""Regenerate tests/fixtures/science/reference.json with MetPy + the reference `ecape` package.

    .venv/bin/pip install "metpy>=1.7,<2" "ecape==0.1.1"
    .venv/bin/python -I tests/make_science_reference.py

Dev-only: Better Weather itself never imports MetPy or ecape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import metpy.calc as mpcalc  # noqa: E402
import numpy as np  # noqa: E402
from ecape.calc import calc_ecape  # noqa: E402
from metpy.units import units  # noqa: E402
from soundings import all_soundings  # noqa: E402


def reference(s: dict[str, list[float]]) -> dict[str, float | None]:
    p = np.array(s["p"]) * units.Pa
    t = np.array(s["t"]) * units.K
    td = np.array(s["td"]) * units.K
    z = np.array(s["z"]) * units.m
    u = np.array(s["u"]) * units("m/s")
    v = np.array(s["v"]) * units("m/s")
    agl = z - z[0]
    sb = mpcalc.surface_based_cape_cin(p, t, td)
    ml = mpcalc.mixed_layer_cape_cin(p, t, td, depth=100 * units.hPa)
    mu = mpcalc.most_unstable_cape_cin(p, t, td)
    lcl_p, _ = mpcalc.lcl(p[0], t[0], td[0])
    right, _, _ = mpcalc.bunkers_storm_motion(p, u, v, agl)
    srh1 = mpcalc.storm_relative_helicity(agl, u, v, 1000 * units.m, storm_u=right[0], storm_v=right[1])[2]
    srh3 = mpcalc.storm_relative_helicity(agl, u, v, 3000 * units.m, storm_u=right[0], storm_v=right[1])[2]
    su, sv = mpcalc.bulk_shear(p, u, v, height=agl, depth=6000 * units.m)
    pw = mpcalc.precipitable_water(p, td)
    q = mpcalc.specific_humidity_from_dewpoint(p, td)
    try:
        ec = calc_ecape(z, p, t, q, u, v, "most_unstable").to("J/kg").m if mu[0].m > 0 else 0.0
    except Exception as exc:  # the reference has edge cases (no EL); record that it gave nothing
        print("ecape reference failed:", exc)
        ec = None
    return {
        "sb_cape": sb[0].to("J/kg").m, "sb_cin": sb[1].to("J/kg").m,
        "ml_cape": ml[0].to("J/kg").m, "ml_cin": ml[1].to("J/kg").m,
        "mu_cape": mu[0].to("J/kg").m, "mu_cin": mu[1].to("J/kg").m,
        "lcl_p": lcl_p.to("Pa").m,
        "storm_u": right[0].to("m/s").m, "storm_v": right[1].to("m/s").m,
        "srh1": float(srh1.to("m^2/s^2").m), "srh3": float(srh3.to("m^2/s^2").m),
        "shear6": float(np.hypot(su.to("m/s").m, sv.to("m/s").m)),
        "pwat_mm": pw.to("mm").m,
        "ecape": ec,
    }


def main() -> None:
    out = {name: reference(s) for name, s in all_soundings().items()}
    path = HERE / "fixtures" / "science" / "reference.json"
    path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
