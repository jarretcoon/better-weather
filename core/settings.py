"""User settings and plugin state.

Inside Hermes these come from the plugin context: settings live under
``plugins.entries.better-weather.settings`` in config.yaml (and show up as a form in the
Desktop app), state lives in the plugin's own data directory. Outside Hermes (tests, a bare
import) an in-memory stand-in is used so nothing ever touches the user's home directory.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_NAME = "better-weather"

DEFAULTS: dict[str, Any] = {
    "home": "",
    "units": "us",
    "clock": "12h",
}


class Settings:
    """Bridge to the Hermes plugin context, with an in-memory fallback."""

    def __init__(self, ctx: Any = None) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        self._mem_config: dict[str, Any] = {}
        self._mem_state: dict[str, Any] = {}

    # -- config (user-visible) -------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        fallback = DEFAULTS.get(key, default) if default is None else default
        if self._ctx is not None:
            try:
                value = self._ctx.get_config(key, fallback)
            except Exception:  # never let a settings read break a tool call
                logger.debug("better-weather: get_config(%s) failed", key, exc_info=True)
                return fallback
            return fallback if value is None else value
        with self._lock:
            return self._mem_config.get(key, fallback)

    def set(self, key: str, value: Any) -> None:
        if self._ctx is not None:
            self._ctx.set_config(key, value)
            return
        with self._lock:
            self._mem_config[key] = value

    # -- state (plugin-owned) --------------------------------------------------
    def state_get(self, key: str, default: Any = None) -> Any:
        if self._ctx is not None:
            try:
                return self._ctx.state.get(key, default)
            except Exception:
                logger.debug("better-weather: state.get(%s) failed", key, exc_info=True)
                return default
        with self._lock:
            return self._mem_state.get(key, default)

    def state_set(self, key: str, value: Any) -> None:
        if self._ctx is not None:
            self._ctx.state.set(key, value)
            return
        with self._lock:
            self._mem_state[key] = value

    # -- convenience -----------------------------------------------------------
    @property
    def home(self) -> str:
        return str(self.get("home") or "").strip()

    @property
    def units(self) -> str:
        value = str(self.get("units") or "us").strip().lower()
        return "metric" if value in {"metric", "si", "c", "celsius"} else "us"

    @property
    def clock(self) -> str:
        return "24h" if str(self.get("clock") or "12h").strip().lower() in {"24h", "24"} else "12h"


_current = Settings()
_data_dir: Path | None = None


def bind(ctx: Any) -> None:
    """Called once from ``register(ctx)``."""
    global _current
    _current = Settings(ctx)


def current() -> Settings:
    return _current


def reset_for_tests() -> Settings:
    global _current, _data_dir
    _current = Settings()
    _data_dir = None
    return _current


def data_dir() -> Path | None:
    """The plugin's durable data directory inside Hermes, or None outside Hermes."""
    if _data_dir is not None:
        return _data_dir
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return plugin_data_dir(PLUGIN_NAME)
    except Exception:
        logger.warning("better-weather: plugin data dir unavailable; caching in memory only")
        return None


def set_data_dir_for_tests(path: Path | None) -> None:
    global _data_dir
    _data_dir = path
