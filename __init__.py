"""Better Weather — a reliable, cross-platform weather plugin for Hermes Agent.

Pure Python, no compiled dependencies, free keyless data sources (NWS, Open-Meteo, SPC).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .core import commands, schemas, settings, tools

logger = logging.getLogger(__name__)

TOOLSET = "better_weather"
_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


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
    logger.info("Better Weather loaded: %d tools", len(tools.TOOLS))
