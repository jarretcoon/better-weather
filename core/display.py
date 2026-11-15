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


def _alert_lines(alerts: list[dict[str, Any]]) -> list[str]:
    lines = []
    for a in alerts[:4]:
        mark = "🚨" if a.get("life_threatening") else "⚠️"
        until = a.get("until") or a.get("ends")
        lines.append(f"{mark} **{a.get('event')}**" + (f" until {until}" if until else ""))
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
        lines.append(f"{icon(c.get('sky'), not is_night)} **{temp}** {sky}{feels_txt}".rstrip())
        bits = []
        if c.get("wind"):
            bits.append(f"💨 {c['wind']}")
        if c.get("humidity_pct") is not None:
            bits.append(f"💧 {c['humidity_pct']}%")
        if bits:
            lines.append(" · ".join(bits))
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
    lines.append("")
    lines.extend(_alert_lines(alerts) if alerts else ["✅ No alerts"])
    for note in r.get("notes") or []:
        if note.startswith("⚠️"):
            lines.append(note)
    sun = r.get("sun") or {}
    if sun.get("sunrise") and sun.get("sunset"):
        lines.append(f"🌅 {sun['sunrise']} · 🌇 {sun['sunset']}")
    return "\n".join(lines).strip()


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
        short = name if name in {"Today", "This Afternoon"} else name[:3]
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
    if not alerts:
        return f"✅ No active alerts for {r.get('where') or r.get('place', {}).get('name', 'this place')}"
    lines = [f"**Alerts · {r.get('where')}**"]
    for a in alerts[:5]:
        mark = "🚨" if a.get("life_threatening") else "⚠️"
        lines.append(f"{mark} **{a['event']}**" + (f" until {a['ends']}" if a.get("ends") else ""))
        if a.get("headline"):
            lines.append(f"   {a['headline']}")
        if a.get("life_threatening") and a.get("instruction"):
            lines.append(f"   👉 {str(a['instruction'])[:280]}")
    return "\n".join(lines)
