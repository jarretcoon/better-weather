"""Radar and satellite answers: fetch the right picture, fall back cleanly, explain the result."""

from __future__ import annotations

from typing import Any

from . import http, images, units
from .places import Place
from .sources import nws
from .sources import radar as radar_src
from .sources import satellite as sat_src

SHOW_HINT = ("To show the picture, put the `media` line on its own line in your reply exactly as given "
             "(MEDIA:/path, not in a code block). The chat then displays it as an image.")


def _picture(img: images.Image, label: str, tz_name: str | None, clock: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "media": img.media,
        "path": str(img.path),
        "source": label,
        "animated": img.path.suffix == ".gif" and "loop" in img.path.name,
    }
    if img.image_time:
        dt = units.parse_time(img.image_time)
        if dt is not None:
            local = dt.astimezone(units.zone(tz_name))
            out["image_time"] = units.clock(local, clock)
            out["image_age_min"] = units.minutes_ago(dt)
    if img.stale:
        out["note"] = f"Couldn't refresh the picture; this copy is {img.age_minutes} min old."
    return out


def radar(place: Place, *, view: str = "local", loop: bool = False, clock: str = "12h") -> dict[str, Any]:
    view = view if view in {"local", "regional", "national"} else "local"
    notes: list[str] = [place.note] if place.note else []
    point = None
    if place.maybe_us:
        try:
            point = nws.point(place.lat, place.lon)
        except http.FetchError as exc:
            notes.append(f"NWS lookup failed ({exc})")
    station = (point or {}).get("radarStation")
    tz_name = (point or {}).get("timeZone") or place.timezone
    attempts = radar_src.plan(view, place.lat, place.lon, station)
    if not attempts:
        return {"place": place.public(), "error": "Radar pictures are only available for the US so far.",
                "hint": "Offer the forecast or satellite instead."}
    errors = []
    for label, fetcher in attempts:
        try:
            img = fetcher(loop)
        except http.FetchError as exc:
            errors.append(f"{label}: {exc}")
            continue
        out = {"place": place.public(), "view": view, "loop": loop, **_picture(img, label, tz_name, clock)}
        if errors:
            notes.append("Primary radar picture unavailable; showing a backup" + (
                " (a still image, no loop)" if loop and not out["animated"] else ""))
        if notes:
            out["notes"] = notes
        out["how_to_show"] = SHOW_HINT
        return out
    raise http.FetchError(attempts[0][0], "; ".join(errors) or "no radar picture available")


def satellite(place: Place, *, kind: str = "geocolor", loop: bool = False, clock: str = "12h") -> dict[str, Any]:
    kind = kind if kind in sat_src.BANDS else "geocolor"
    band = sat_src.BANDS[kind]
    notes: list[str] = [place.note] if place.note else []
    sector = sat_src.sector_for(place.lat, place.lon)
    attempts: list[tuple[str, Any]] = []
    if sector:
        sat, code, name = sector
        attempts.append((f"{sat.replace('GOES', 'GOES-')} satellite, {name}",
                         lambda want_loop: sat_src.sector_image(sat, code, band, want_loop)))
    if sat_src.in_conus(place.lat, place.lon) or sector:
        attempts.append(("GOES-19 satellite, continental US",
                         lambda want_loop: sat_src.conus_image(band, want_loop)))
    if not attempts:
        return {"place": place.public(), "error": "Satellite pictures cover the US and nearby areas so far.",
                "hint": "Offer the forecast instead."}
    errors = []
    for label, fetcher in attempts:
        for want_loop in ([True, False] if loop else [False]):
            try:
                img = fetcher(want_loop)
            except http.FetchError as exc:
                errors.append(f"{label}{' loop' if want_loop else ''}: {exc}")
                continue
            if loop and not want_loop:
                notes.append("No animation exists for this picture type; showing the latest still")
            out = {"place": place.public(), "kind": kind, "shows": sat_src.BAND_NAMES[kind], "loop": loop,
                   **_picture(img, label, place.timezone, clock)}
            if errors and not (loop and not want_loop and len(errors) == 1):
                notes.append("First choice unavailable; showing a backup picture")
            if notes:
                out["notes"] = notes
            out["how_to_show"] = SHOW_HINT
            return out
    raise http.FetchError(attempts[0][0], "; ".join(errors) or "no satellite picture available")
