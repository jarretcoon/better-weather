"""Chat-ready weather cards: short lines, emoji, **bold** labels, no tables.

Built for Telegram's standard formatting (bold, italics and emoji survive; tables get flattened) and
reads just as well in the Desktop app and terminal. The agent is told to send these as-is."""

from __future__ import annotations

from typing import Any

_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    # (words in the description, day icon, night icon) — first match wins, most severe first
    (("tornado",), "🌪️", "🌪️"),
    (("thunder", "t-storm", "tstorm"), "⛈️", "⛈️"),
    (("freezing", "sleet", "ice", "wintry"), "🧊", "🧊"),
    (("snow", "flurr", "blizzard"), "🌨️", "🌨️"),
    (("rain", "shower", "drizzle"), "🌧️", "🌧️"),
    (("fog", "haze", "smoke", "mist", "dust"), "🌫️", "🌫️"),
    (("overcast", "mostly cloudy", "cloudy"), "☁️", "☁️"),
    (("partly",), "⛅", "☁️"),
    (("mostly sunny", "mostly clear", "few clouds", "fair"), "🌤️", "🌙"),
    (("sunny", "clear"), "☀️", "🌙"),
    (("wind", "breez", "blustery"), "💨", "💨"),
)


def icon(text: Any, is_day: bool = True) -> str:
    lowered = str(text or "").lower()
    for words, day, night in _RULES:
        if any(w in lowered for w in words):
            return day if is_day else night
    return "🌡️"


def _deg(value: Any) -> str:
    return "–" if value is None else f"{value}°"


def _num_temp(text: Any) -> str:
    """'41°F' -> '41°' for compact lines."""
    s = str(text or "")
    return s[:-1] if s.endswith(("°F", "°C")) else s


def _chance(pct: Any) -> str:
    try:
        value = round(float(pct))
    except (TypeError, ValueError):
        return ""
    return f" · 💧{value}%" if value >= 20 else ""


def _day_and_clock(text: str) -> tuple[str, str]:
    """'tomorrow 3 AM' -> ('tomorrow', '3 AM'); '15:00' -> ('', '15:00')."""
    parts = text.split()
    n = 2 if parts and parts[-1] in ("AM", "PM") else 1
    return " ".join(parts[:-n]), " ".join(parts[-n:])


def _when(a: dict[str, Any]) -> str:
    """' until 8 AM', or for an alert that hasn't started yet ' from tomorrow 3 AM to 8 AM'."""
    end = a.get("until") or a.get("ends")
    start = a.get("starts") if a.get("upcoming") else None
    if not start:
        return f" until {end}" if end else ""
    if not end:
        return f" from {start}"
    start_day = _day_and_clock(str(start))[0]
    end_day, end_clock = _day_and_clock(str(end))
    return f" from {start} to {end_clock if start_day and start_day == end_day else end}"


def _alert_lines(alerts: list[dict[str, Any]]) -> list[str]:
    lines = []
    for a in alerts[:4]:
        mark = "🚨" if a.get("life_threatening") else "⚠️"
        lines.append(f"{mark} **{a.get('event')}**{_when(a)}")
    if len(alerts) > 4:
        lines.append(f"…and {len(alerts) - 4} more")
    return lines


def now_card(r: dict[str, Any]) -> str:
    c = r.get("current") or {}
    is_night = r.get("_daylight") is False
    head = f"**{r['place']['name']}** · {r.get('as_of', '')}"
    lines = [head]
    if c:
        temp = c.get("temp") or "–"
        feels = c.get("feels_like")
        feels_txt = f" · feels {_num_temp(feels)}" if feels and feels != temp else ""
        sky = str(c.get("sky") or "").lower()
        source = r.get("_reading_from")  # "KDSM 12 min ago" / "model estimate"; nothing for a station next door
        lines.append(f"{icon(c.get('sky'), not is_night)} **{temp}**" + (f" {sky}" if sky else "") + feels_txt
                     + (f" · {source}" if source else ""))
        bits = []
        if c.get("wind"):
            bits.append(f"💨 {c['wind']}")
        if c.get("humidity_pct") is not None:
            bits.append(f"💧 {c['humidity_pct']}%")
        if bits:
            lines.append(" · ".join(bits))
    hours = (r.get("_next_hours") or [])[1::2][:4]
    if hours:
        lines.append("⏱️ " + " · ".join(
            f"{h.get('time')} {icon(h.get('sky'), h.get('_day', True))} {_deg(h.get('temp'))}"
            + (f" 💧{round(h['precip_chance_pct'])}%" if (h.get("precip_chance_pct") or 0) >= 20 else "")
            for h in hours))
    periods = (r.get("forecast") or [])[:2]
    if periods:
        lines.append("")
        for p in periods:
            if "name" in p:
                day = p.get("high_or_low") == "high"
                lines.append(f"**{p['name']}** {icon(p.get('sky'), day)} {_deg(p.get('temp'))} "
                             f"{'high' if day else 'low'} · {p.get('sky')}{_chance(p.get('precip_chance_pct'))}")
            else:
                lines.append(f"**{p.get('day')}** {icon(p.get('sky'))} {_deg(p.get('high'))} / {_deg(p.get('low'))} · "
                             f"{p.get('sky')}{_chance(p.get('precip_chance_pct'))}")
    alerts = r.get("alerts") or []
    warnings = [n for n in r.get("notes") or [] if n.startswith("⚠️")]
    lines.append("")
    if alerts:
        lines.extend(_alert_lines(alerts))
    elif r.get("_alerts_checked", True):
        lines.append("✅ No alerts")
    elif not warnings:
        warnings = ["⚠️ Alerts weren't checked here"]
    lines.extend(warnings)
    sky = r.get("_sky")
    if sky:
        lines.extend(sky_lines(sky))
    else:
        sun = r.get("_sun") or r.get("sun") or {}
        if sun.get("sunrise") and sun.get("sunset"):
            lines.append(f"🌅 Sunrise {sun['sunrise']} · 🌇 Sunset {sun['sunset']}")
    return "\n".join(lines).strip()


def _moon_label(moon: dict[str, Any]) -> str:
    name = str(moon.get("phase") or "Moon")
    name = name if name.lower().endswith("moon") else f"{name} moon"
    lit = moon.get("lit_pct")
    # a new moon can't be seen: no "0% lit", no rise or set
    return f"{moon.get('emoji', '🌙')} {name}" + (f" · {lit}% lit" if isinstance(lit, int) and lit >= 2 else "")


def sky_lines(sky: dict[str, Any]) -> list[str]:
    """Sun by day, moon by night, each emoji with its words: people don't know what a tiny 🌇 means."""
    moon = sky.get("moon") or {}
    visible = (moon.get("lit_pct") or 0) >= 2
    if moon.get("up"):
        status = f"up now, sets {moon['sets']}" if moon.get("sets") else "up all day"
    else:
        status = f"rises {moon['rises']}" if moon.get("rises") else "down all day"
    moon_line = _moon_label(moon) + (f" · {status}" if visible else "")
    if sky.get("mode") == "sun":
        return ["☀️ Sun up all day" if sky.get("sun_all_day")
                else f"🌅 Sunrise {sky.get('sunrise')} · 🌇 Sunset {sky.get('sunset')}", moon_line]
    lines = [moon_line]
    tail = []
    if sky.get("next_sunrise"):
        tail.append(f"🌅 Sunrise {sky['next_sunrise']}")
    nxt = moon.get("next") or {}
    if nxt.get("date") and nxt.get("phase") != moon.get("phase"):
        tail.append(f"{nxt.get('emoji')} {nxt.get('phase')} {nxt['date']}")
    if tail:
        lines.append(" · ".join(tail))
    return lines


def _daily_from_periods(periods: list[dict[str, Any]]) -> list[str]:
    """Pair NWS day/night periods into one line per day: 'Mon 🌤️ 54° / 35° · Mostly Sunny'."""
    lines: list[str] = []
    i = 0
    while i < len(periods):
        p = periods[i]
        is_day = p.get("high_or_low") == "high"
        if not is_day:  # a leading night ("Tonight", "Overnight")
            lines.append(f"**{p['name']}** {icon(p.get('sky'), False)} low {_deg(p.get('temp'))} · {p.get('sky')}"
                         f"{_chance(p.get('precip_chance_pct'))}")
            i += 1
            continue
        night = periods[i + 1] if i + 1 < len(periods) and periods[i + 1].get("high_or_low") == "low" else None
        name = str(p.get("name") or "")
        short = name if name in {"Today", "This Afternoon"} else (p.get("_dow") or name[:3])
        pop = max((q.get("precip_chance_pct") or 0) for q in (p, night) if q)
        low = _deg(night.get("temp") if night else None)
        lines.append(f"**{short}** {icon(p.get('sky'))} {_deg(p.get('temp'))} / {low} · {p.get('sky')}{_chance(pop)}")
        i += 2 if night else 1
    return lines


def forecast_card(r: dict[str, Any]) -> str:
    name = r["place"]["name"]
    if r.get("hours"):
        rows = r["hours"]
        stride = 1 if len(rows) <= 12 else 2 if len(rows) <= 24 else 3
        lines = [f"**{name}** · next {len(rows)} hours"]
        for h in rows[::stride][:16]:
            day = h["_day"] if isinstance(h.get("_day"), bool) else _daytime(h.get("time"))
            lines.append(f"{h.get('time')} {icon(h.get('sky'), day)} {_deg(h.get('temp'))}"
                         f"{_chance(h.get('precip_chance_pct'))}")
        return "\n".join(lines)
    lines = [f"**{name}** · forecast"]
    if r.get("periods"):
        lines.extend(_daily_from_periods(r["periods"]))
    for d in (r.get("days") or []) + (r.get("extended_days") or []):
        snow = f" · 🌨️ {d['snow']}" if d.get("snow") else ""
        lines.append(f"**{str(d.get('day'))[:3]} {str(d.get('day'))[-2:].strip()}** {icon(d.get('sky'))} "
                     f"{_deg(d.get('high'))} / {_deg(d.get('low'))} · {d.get('sky')}"
                     f"{_chance(d.get('precip_chance_pct'))}{snow}")
    lines.extend(n for n in r.get("notes") or [] if n.startswith("⚠️"))
    return "\n".join(lines)


def _daytime(label: Any) -> bool:
    text = str(label or "")
    try:
        hour = int(text.split()[-2].split(":")[0]) if text.split()[-1] in {"AM", "PM"} else int(text.split()[-1][:2])
    except (ValueError, IndexError):
        return True
    if text.endswith("AM"):
        return 7 <= (hour % 12) <= 11
    if text.endswith("PM"):
        return (hour % 12) <= 5
    return 7 <= hour <= 17


def strip_private(value: Any) -> Any:
    """Drop the card-only helper keys (leading underscore) before the result goes to the agent."""
    if isinstance(value, dict):
        return {k: strip_private(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [strip_private(v) for v in value]
    return value


def alerts_card(r: dict[str, Any]) -> str:
    alerts = r.get("alerts") or []
    where = r.get("where") or (r.get("place") or {}).get("name") or "this place"
    warnings = [n for n in r.get("notes") or [] if n.startswith("⚠️")]
    # a country that lists warnings by area name: its note says how many orange/red are elsewhere; name them
    elsewhere = [] if r.get("_exact", True) else _elsewhere(r.get("orange_and_red_in_country") or [])
    if not alerts:
        if r.get("_exact", True):
            return "\n".join([f"✅ No active alerts for {where}", *warnings])
        return "\n".join([f"**Alerts · {where}**", *(warnings or ["⚠️ Alerts weren't checked here"]), *elsewhere])
    lines = [f"**Alerts · {where}**"]
    for a in alerts[:5]:
        mark = "🚨" if a.get("life_threatening") else "⚠️"
        lines.append(f"{mark} **{a['event']}**{_when(a)}")
        if a.get("headline") and str(a["headline"]).strip().lower() != str(a.get("event")).strip().lower():
            lines.append(f"   {a['headline']}")
        if a.get("life_threatening") and a.get("instruction"):
            lines.append(f"   👉 {_flat(a['instruction'], 280)}")
    if len(alerts) > 5:
        lines.append(f"…and {len(alerts) - 5} more")
    lines.extend(warnings)
    lines.extend(elsewhere)
    return "\n".join(lines)


def _elsewhere(warnings: list[dict[str, Any]], groups: int = 5) -> list[str]:
    """Orange and red warnings elsewhere in the country, a line per kind: '🔴 Heavy rain · Kreis Calw, Enzkreis +6'."""
    kinds: dict[tuple[str, str], tuple[list[str], int]] = {}
    for w in warnings:
        names, more = kinds.get((str(w.get("level")), str(w.get("event"))), ([], 0))
        names.extend(n for n in w.get("regions") or [] if n and n not in names)
        kinds[(str(w.get("level")), str(w.get("event")))] = (names, more + (w.get("more_regions") or 0))
    lines = []
    for (level, event), (names, more) in list(kinds.items())[:groups]:
        rest = len(names) - 2 + more
        where = ", ".join(names[:2]) + (f" +{rest}" if rest > 0 else "")
        lines.append(f"{'🔴' if level == 'red' else '🟠'} {event}" + (f" · {where}" if where else ""))
    if len(kinds) > groups:
        lines.append(f"…and {len(kinds) - groups} more")
    return lines


def _flat(text: Any, limit: int) -> str:
    """NWS text comes hard-wrapped at ~70 columns; join it and cut at a word."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + "…"


REPLY_HINT = ("Send `card` to the user exactly as written; it is already formatted for chat. "
              "Add at most one short line of your own.")


def card_first(result: dict[str, Any], card: str) -> dict[str, Any]:
    """Put the card (and how to use it) first, where the model reads it before the raw data."""
    rest = {k: v for k, v in result.items() if k not in ("card", "how_to_reply")}
    return {"card": card, "how_to_reply": REPLY_HINT, **rest}
