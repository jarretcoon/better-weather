"""Re-record the replay fixtures from the live services.

    python -I tests/record_fixtures.py

Run it when a source changes shape; then fix whatever tests break. Large responses are trimmed.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import conftest  # noqa: E402,F401  (loads the plugin package)
from better_weather.core import cache, clock, http, settings, tools  # noqa: E402
from fixturekit import FIXTURES, RecordingTransport  # noqa: E402

PLACES = ["Des Moines, IA", "London, UK", "Tokyo", "50309", "KDSM", "Fairbanks, AK"]


def shrink(key: str, data: object) -> object:
    if not isinstance(data, dict):
        return data
    props = data.get("properties")
    if isinstance(props, dict) and isinstance(props.get("periods"), list):
        props["periods"] = props["periods"][:80]
    if "alerts/active" in key and isinstance(data.get("features"), list):
        data["features"] = data["features"][:12]
    return data


def main() -> None:
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    FIXTURES.mkdir(parents=True)
    settings.reset_for_tests()
    cache.use_memory_cache_for_tests()
    clock.freeze_for_tests(None)
    http.set_transport_for_tests(RecordingTransport(shrink), no_sleep=False)
    settings.current().set("home", "Des Moines, IA")
    (FIXTURES / "recorded_at.json").write_text(
        json.dumps({"utc": datetime.now(UTC).isoformat()}) + "\n", encoding="utf-8")
    calls = [("weather", {"location": p}) for p in PLACES]
    calls += [("weather_forecast", {"days": 9}), ("weather_forecast", {"hourly": True, "hours": 24}),
              ("weather_forecast", {"location": "London, UK", "days": 5}),
              ("weather_alerts", {}), ("weather_alerts", {"state": "IA"}),
              ("weather_alerts", {"location": "Fairbanks, AK"}),
              ("weather_severe", {}), ("weather_severe", {"location": "Tokyo"})]
    for name, args in calls:
        result = json.loads(dict(tools.TOOLS)[name](args))
        print(name, args, "ERROR " + result["error"] if "error" in result else "ok")


if __name__ == "__main__":
    main()
