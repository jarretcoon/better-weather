"""Loads the plugin the way Hermes does (the repo root as a package) and isolates every test:
in-memory cache, in-memory settings, no network, a frozen clock."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent


def _load_plugin() -> None:
    if "better_weather" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "better_weather", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["better_weather"] = module
    spec.loader.exec_module(module)


_load_plugin()
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from better_weather.core import cache, clock, http, settings  # noqa: E402
from fixturekit import FIXTURES, replay_transport  # noqa: E402


def recorded_at() -> datetime:
    meta = json.loads((FIXTURES / "recorded_at.json").read_text(encoding="utf-8"))
    return datetime.fromisoformat(meta["utc"])


@pytest.fixture(autouse=True)
def isolated(request: pytest.FixtureRequest):
    live = request.node.get_closest_marker("live") is not None
    settings.reset_for_tests()
    cache.use_memory_cache_for_tests()
    if live:
        http.set_transport_for_tests(None, no_sleep=False)
        clock.freeze_for_tests(None)
    else:
        http.set_transport_for_tests(replay_transport())
        clock.freeze_for_tests(recorded_at())
    yield
    http.set_transport_for_tests(None, no_sleep=False)
    clock.freeze_for_tests(None)


@pytest.fixture
def home():
    s = settings.current()
    s.set("home", "Des Moines, IA")
    return s
