"""Backend for the Better Weather Desktop chip: GET /api/plugins/better-weather/now.

Hermes loads this file on its own, outside the plugin package, so relative imports don't work. It
loads the Better Weather package from the folder above under a private module name, and reads the
plugin's settings from config.yaml on each request, as the plugin guide describes. The data
directory (cache, places) is Hermes' plugin data dir, shared with the tools."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()
_ROOT = Path(__file__).resolve().parent.parent
_NAME = "better_weather_dashboard"
_lock = threading.Lock()


def _package() -> Any:
    with _lock:
        module = sys.modules.get(_NAME)
        if module is not None:
            return module
        spec = importlib.util.spec_from_file_location(_NAME, _ROOT / "__init__.py",
                                                      submodule_search_locations=[str(_ROOT)])
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load Better Weather")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_NAME, None)
            raise
        return module


def _apply_config_settings(package: Any) -> None:
    """Copy home/units/clock from config.yaml (plugins.entries.better-weather.settings)."""
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore[import-not-found]

        cfg = load_config_readonly() or {}
    except Exception:
        return
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("better-weather") or {}
    saved = entry.get("settings") or {}
    current = package.core.settings.current()
    for key, default in package.core.settings.DEFAULTS.items():
        value = saved.get(key)
        current.set(key, default if value in (None, "") else value)


@router.get("/now")
def now(refresh: bool = False) -> dict[str, Any]:
    package = _package()
    _apply_config_settings(package)
    desktop_api = importlib.import_module(f"{_NAME}.core.desktop_api")
    return desktop_api.now_payload(force=refresh)
