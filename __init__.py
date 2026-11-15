"""Better Weather — a reliable, cross-platform weather plugin for Hermes Agent.

Pure Python, no compiled dependencies, free keyless data sources (NWS, Open-Meteo, SPC, NOAA GOES,
Iowa Environmental Mesonet).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .core import commands, schemas, settings, tools

logger = logging.getLogger(__name__)

TOOLSET = "better_weather"
_SKILLS_DIR = Path(__file__).resolve().parent / "skills"

_PLACE_SLOT = {"name": "place", "type": "text", "label": "Where?", "default": "home"}
_DELIVER_SLOT = {"name": "deliver", "type": "enum", "label": "Where to send it?", "default": "origin",
                 "options": ["origin", "local"], "strict": False}
BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "key": "morning-brief",
        "title": "Morning weather brief",
        "description": "Every morning: now, today's high and low, rain or snow timing, and any alerts.",
        "schedule_template": "{minute} {hour} * * *",
        "prompt_template": (
            "Morning weather brief for {place}. Use the weather tool (and weather_severe only if storms "
            "are possible today). Give: now, today's high and low, when rain or snow starts and stops, "
            "and every active alert. At most 5 short lines."),
        "slots": [_PLACE_SLOT, {"name": "time", "type": "time", "label": "What time?", "default": "07:00"},
                  _DELIVER_SLOT],
        "tags": ["weather", "daily"],
    },
    {
        "key": "week-ahead",
        "title": "Week-ahead weather",
        "description": "Sunday evening: the coming week's weather, wet days, big swings and storm risk.",
        "schedule_template": "{minute} {hour} * * 0",
        "prompt_template": (
            "Week-ahead weather for {place}. Use weather_forecast with days 7, and weather_severe if any "
            "day has thunderstorms. Call out wet or snowy days, big temperature swings and severe risk. "
            "At most 8 short lines."),
        "slots": [_PLACE_SLOT, {"name": "time", "type": "time", "label": "What time?", "default": "18:00"},
                  _DELIVER_SLOT],
        "tags": ["weather", "weekly"],
    },
    {
        "key": "weekend-plan",
        "title": "Weekend weather",
        "description": "Friday afternoon: Saturday and Sunday, and the best outdoor hours.",
        "schedule_template": "{minute} {hour} * * 5",
        "prompt_template": (
            "Weekend weather for {place}. Use weather_forecast (hourly for Saturday and Sunday daytime). "
            "Say what each day looks like and name the best few hours for being outside each day. "
            "At most 6 short lines."),
        "slots": [_PLACE_SLOT, {"name": "time", "type": "time", "label": "What time?", "default": "16:00"},
                  _DELIVER_SLOT],
        "tags": ["weather", "weekly"],
    },
)


def register(ctx: Any) -> None:
    """Called once by Hermes when the plugin loads."""
    settings.bind(ctx)

    schema_by_name = {s["name"]: s for s in schemas.ALL}
    for name, handler in tools.TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema_by_name[name],
                          handler=handler, emoji="🌦️")

    if _SKILLS_DIR.is_dir():
        for child in sorted(_SKILLS_DIR.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.is_file():
                try:
                    ctx.register_skill(child.name, skill_md)
                except Exception:  # an old Hermes without plugin skills still gets the tools
                    logger.debug("better-weather: could not register skill %s", child.name, exc_info=True)

    ctx.register_command("weather", handler=commands.slash_command,
                         description="Weather now, no AI needed (/weather help)", args_hint="[place]")
    ctx.register_cli_command(name="better-weather", help="Better Weather: weather in the terminal",
                             setup_fn=commands.setup_cli, handler_fn=commands.handle_cli)

    add_blueprint = getattr(ctx, "register_automation_blueprint", None)
    if callable(add_blueprint):
        for bp in BLUEPRINTS:
            spec = dict(bp)
            try:
                add_blueprint(spec.pop("key"), category="weather", **spec)
            except Exception:
                logger.debug("better-weather: blueprint %s not registered", bp["key"], exc_info=True)
    logger.info("Better Weather loaded: %d tools", len(tools.TOOLS))
