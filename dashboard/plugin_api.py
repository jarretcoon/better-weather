"""Backend for the Better Weather Desktop chip: GET /api/plugins/better-weather/now.

Runs inside Hermes (the gateway or its plugin host). It reuses the Better Weather package Hermes
already loaded when it can (same settings, same cache), found by file path rather than by Hermes'
internal module naming. If this process hasn't loaded it, it loads the package itself and reads the
plugin's settings from Hermes' config."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()
_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_INIT = (_ROOT / "__init__.py").resolve()
_FALLBACK_NAME = "better_weather_dashboard"
_lock = threading.Lock()


def _loaded_package() -> Any:
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        try:
            if Path(path).resolve() == _PACKAGE_INIT:
                return module
        except OSError:
            continue
    return None


def _own_package() -> Any:
    with _lock:
        module = sys.modules.get(_FALLBACK_NAME)
        if module is not None:
            return module
        spec = importlib.util.spec_from_file_location(_FALLBACK_NAME, _PACKAGE_INIT,
                                                      submodule_search_locations=[str(_ROOT)])
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load Better Weather")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_FALLBACK_NAME] = module
        spec.loader.exec_module(module)
    return module


def _apply_config_settings(package: Any) -> None:
    """Without a plugin context, copy home/units/clock from config.yaml into the in-memory settings."""
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore[import-not-found]

        cfg = load_config_readonly() or {}
    except Exception:
        return
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("better-weather") or {}
    current = package.core.settings.current()
    for key, value in (entry.get("settings") or {}).items():
        if key in ("home", "units", "clock") and value not in (None, ""):
            current.set(key, value)


@router.get("/now")
def now(refresh: bool = False) -> dict[str, Any]:
    package = _loaded_package()
    if package is None:
        package = _own_package()
        _apply_config_settings(package)
    desktop_api = sys.modules.get(f"{package.__name__}.core.desktop_api")
    if desktop_api is None:
        from importlib import import_module

        desktop_api = import_module(f"{package.__name__}.core.desktop_api")
    return desktop_api.now_payload(force=refresh)
