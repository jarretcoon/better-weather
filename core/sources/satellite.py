"""GOES satellite pictures from NOAA's STAR image server (public domain)."""

from __future__ import annotations

from .. import images

BASE = "https://cdn.star.nesdis.noaa.gov"

# (satellite, sector code, name, south, north, west, east) — only sectors that exist on the server.
SECTORS: tuple[tuple[str, str, str, float, float, float, float], ...] = (
    ("GOES18", "pnw", "Pacific Northwest", 40, 50, -127, -110),
    ("GOES18", "psw", "Pacific Southwest", 30, 42, -125, -110),
    ("GOES19", "nr", "Northern Rockies", 40, 50, -117, -100),
    ("GOES19", "sr", "Southern Rockies", 30, 42, -115, -100),
    ("GOES19", "umv", "Upper Mississippi Valley", 38, 50, -103, -87),
    ("GOES19", "sp", "Southern Plains", 26, 39, -108, -92),
    ("GOES19", "smv", "Southern Mississippi Valley", 28, 38, -96, -84),
    ("GOES19", "cgl", "Central Great Lakes", 37, 48, -92, -78),
    ("GOES19", "ne", "Northeast", 38, 48, -82, -66),
    ("GOES19", "se", "Southeast", 24, 37, -90, -75),
    ("GOES19", "pr", "Puerto Rico", 16, 20, -69, -63),
    ("GOES18", "hi", "Hawaii", 17, 24, -162, -153),
    ("GOES18", "cak", "Central Alaska", 58, 70, -165, -140),
    ("GOES18", "sea", "Southeast Alaska", 54, 60, -140, -130),
)
BANDS = {"geocolor": "GEOCOLOR", "infrared": "13", "water_vapor": "09"}
BAND_NAMES = {"geocolor": "true color by day, infrared at night", "infrared": "clean infrared (cloud-top cold)",
              "water_vapor": "mid-level water vapor"}
TTL_STILL = 10 * 60
TTL_LOOP = 15 * 60


def sector_for(lat: float, lon: float) -> tuple[str, str, str] | None:
    best: tuple[float, tuple[str, str, str]] | None = None
    for sat, code, name, south, north, west, east in SECTORS:
        if south <= lat <= north and west <= lon <= east:
            dist = (lat - (south + north) / 2) ** 2 + (lon - (west + east) / 2) ** 2
            if best is None or dist < best[0]:
                best = (dist, (sat, code, name))
    return best[1] if best else None


def in_conus(lat: float, lon: float) -> bool:
    return 24 <= lat <= 50 and -126 <= lon <= -66


def sector_image(sat: str, code: str, band: str, loop: bool) -> images.Image:
    if loop:
        url = f"{BASE}/{sat}/ABI/SECTOR/{code}/{band}/{sat}-{code.upper()}-{band}-600x600.gif"
        return images.fetch(url, f"sat-{sat}-{code}-{band}-loop.gif", ttl=TTL_LOOP)
    url = f"{BASE}/{sat}/ABI/SECTOR/{code}/{band}/1200x1200.jpg"
    return images.fetch(url, f"sat-{sat}-{code}-{band}.jpg", ttl=TTL_STILL)


def conus_image(band: str, loop: bool) -> images.Image:
    if loop:
        url = f"{BASE}/GOES19/ABI/CONUS/{band}/GOES19-CONUS-{band}-625x375.gif"
        return images.fetch(url, f"sat-GOES19-conus-{band}-loop.gif", ttl=TTL_LOOP)
    url = f"{BASE}/GOES19/ABI/CONUS/{band}/1250x750.jpg"
    return images.fetch(url, f"sat-GOES19-conus-{band}.jpg", ttl=TTL_STILL)
