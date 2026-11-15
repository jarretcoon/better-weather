"""Point-in-polygon for GeoJSON geometries (lon/lat order), holes respected."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

Ring = Sequence[Sequence[float]]


def _in_ring(lon: float, lat: float, ring: Ring) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _in_polygon(lon: float, lat: float, rings: Sequence[Ring]) -> bool:
    if not rings or not _in_ring(lon, lat, rings[0]):
        return False
    return not any(_in_ring(lon, lat, hole) for hole in rings[1:])


def contains(geometry: Any, lon: float, lat: float) -> bool:
    if not isinstance(geometry, dict):
        return False
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return _in_polygon(lon, lat, coords)
    if kind == "MultiPolygon":
        return any(_in_polygon(lon, lat, poly) for poly in coords)
    if kind == "GeometryCollection":
        return any(contains(g, lon, lat) for g in geometry.get("geometries") or [])
    return False
