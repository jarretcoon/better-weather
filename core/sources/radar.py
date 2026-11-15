"""Radar pictures: the NWS's own radar images first (clean, official, warning boxes drawn in),
Iowa State's IEM map service as the backup (any zoom, still images only)."""

from __future__ import annotations

from typing import Any

from .. import images

RIDGE = "https://radar.weather.gov/ridge/standard"
IEM_RADMAP = "https://mesonet.agron.iastate.edu/GIS/radmap.php"

# NWS regional radar mosaics: (name, south, north, west, east).
REGIONS: tuple[tuple[str, float, float, float, float], ...] = (
    ("PACNORTHWEST", 40, 50, -126, -110),
    ("PACSOUTHWEST", 31, 42, -125, -112),
    ("NORTHROCKIES", 40, 50, -117, -100),
    ("SOUTHROCKIES", 31, 42, -115, -100),
    ("UPPERMISSVLY", 39, 50, -104, -88),
    ("SOUTHPLAINS", 25, 40, -107, -92),
    ("SOUTHMISSVLY", 28, 38, -97, -84),
    ("CENTGRLAKES", 37, 48, -93, -79),
    ("NORTHEAST", 38, 48, -82, -66),
    ("SOUTHEAST", 24, 37, -91, -75),
    ("ALASKA", 51, 72, -180, -129),
    ("HAWAII", 18, 23, -161, -154),
    ("CARIB", 16, 20, -68, -64),
    ("GUAM", 12, 16, 143, 147),
)
REGION_NAMES = {
    "PACNORTHWEST": "Pacific Northwest", "PACSOUTHWEST": "Pacific Southwest",
    "NORTHROCKIES": "Northern Rockies", "SOUTHROCKIES": "Southern Rockies",
    "UPPERMISSVLY": "Upper Mississippi Valley", "SOUTHPLAINS": "Southern Plains",
    "SOUTHMISSVLY": "Southern Mississippi Valley", "CENTGRLAKES": "Central Great Lakes",
    "NORTHEAST": "Northeast", "SOUTHEAST": "Southeast", "ALASKA": "Alaska", "HAWAII": "Hawaii",
    "CARIB": "Puerto Rico and the Virgin Islands", "GUAM": "Guam", "CONUS": "Lower 48 states",
}
TTL_STILL = 4 * 60
TTL_LOOP = 5 * 60


def region_for(lat: float, lon: float) -> str | None:
    best: tuple[float, str] | None = None
    for name, south, north, west, east in REGIONS:
        if south <= lat <= north and west <= lon <= east:
            c_lat, c_lon = (south + north) / 2, (west + east) / 2
            dist = (lat - c_lat) ** 2 + (lon - c_lon) ** 2
            if best is None or dist < best[0]:
                best = (dist, name)
    return best[1] if best else None


def in_conus(lat: float, lon: float) -> bool:
    return 24 <= lat <= 50 and -126 <= lon <= -66


def ridge(product: str, loop: bool) -> images.Image:
    suffix = "loop" if loop else "0"
    return images.fetch(f"{RIDGE}/{product}_{suffix}.gif", f"radar-{product}-{suffix}.gif",
                        ttl=TTL_LOOP if loop else TTL_STILL)


def iem_map(lat: float, lon: float, half_height_deg: float) -> images.Image:
    """Radar + counties + cities + warning boxes + watches, centred on the point (still image)."""
    half_w = half_height_deg * 4 / 3
    bbox = f"{lon - half_w:.2f},{lat - half_height_deg:.2f},{lon + half_w:.2f},{lat + half_height_deg:.2f}"
    layers = "&".join(f"layers[]={layer}" for layer in ("nexrad", "uscounties", "cities", "sbw", "watches"))
    url = f"{IEM_RADMAP}?{layers}&width=800&height=600&bbox={bbox}"
    return images.fetch(url, f"radar-iem-{lat:.1f}_{lon:.1f}_{half_height_deg:g}.png", ttl=TTL_STILL)


def describe(product: str) -> str:
    if product.startswith("K") or product.startswith("P") or product.startswith("T"):
        return f"NWS radar {product}"
    return f"NWS radar mosaic, {REGION_NAMES.get(product, product)}"


def plan(view: str, lat: float, lon: float, station: str | None) -> list[tuple[str, Any]]:
    """Ordered list of (label, fetch-callable) to try for a view."""
    region = region_for(lat, lon)
    tries: list[tuple[str, Any]] = []
    if view == "local" and station:
        tries.append((describe(station), lambda loop: ridge(station, loop)))
    if view in {"local", "regional"} and region:
        tries.append((describe(region), lambda loop: ridge(region, loop)))
    if view == "national" or (in_conus(lat, lon) and not tries):
        tries.append((describe("CONUS"), lambda loop: ridge("CONUS", loop)))
    if in_conus(lat, lon) or region:
        size = {"local": 1.6, "regional": 5.0}.get(view, 12.0)
        tries.append(("Iowa Environmental Mesonet radar map",
                      lambda _loop: iem_map(lat, lon, size)))
    return tries
