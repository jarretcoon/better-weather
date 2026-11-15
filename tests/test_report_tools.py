"""End-to-end through the tool handlers, replaying real recorded responses."""

import json
import re
from pathlib import Path

import better_weather
import httpx
import pytest
from better_weather.core import commands, http, schemas, tools

ROOT = Path(__file__).resolve().parents[1]


def call(tool, **args):
    out = dict(tools.TOOLS)[tool](args)
    assert isinstance(out, str)
    return json.loads(out)


def test_weather_now_us_uses_station_report(home):
    r = call("weather")
    assert r["place"]["name"] == "Des Moines, IA"
    assert r["current"]["source"] == "NWS station report" and r["current"]["temp"].endswith("°F")
    assert r["forecast"] and "⏱️" in r["card"]
    assert "NWS alerts" in r["sources"]
    assert r["card"].startswith("**Des Moines, IA**") and list(r)[:2] == ["card", "how_to_reply"]


def test_weather_now_abroad_uses_open_meteo_and_says_no_alerts(home):
    r = call("weather", location="Tokyo")
    assert r["current"]["source"] == "Open-Meteo model estimate"
    assert any("US, Canada and 38 European countries" in n for n in r["notes"])
    assert "✅" not in r["card"] and "No official alert feed" in r["card"]


def test_history_slash_command_date_is_optional():
    from better_weather.core import commands

    assert commands._split_day("Des Moines") == ("yesterday", "Des Moines")
    assert commands._split_day("2026-11-01 Des Moines") == ("2026-11-01", "Des Moines")
    assert commands._split_day("3 days ago Ames, IA") == ("3 days ago", "Ames, IA")


def test_metric_override(home):
    r = call("weather", units="metric")
    assert r["current"]["temp"].endswith("°C")


def test_nws_outage_falls_back_and_warns_about_alerts(home):
    def nws_down(request):
        if request.url.host == "api.weather.gov" and "/points/" not in request.url.path:
            return httpx.Response(503, json={"detail": "down"})
        return None

    from fixturekit import replay_transport

    http.set_transport_for_tests(replay_transport(nws_down))
    r = call("weather")
    assert r["current"]["source"] == "Open-Meteo model estimate"
    assert r["forecast"]
    assert any("Alerts could not be checked" in n for n in r["notes"])
    assert "⚠️ Alerts could not be checked" in r["card"] and "✅" not in r["card"]


def test_forecast_daily_extends_past_day_seven(home):
    r = call("weather_forecast", days=9)
    assert len(r["periods"]) == 14 and len(r["extended_days"]) == 2
    assert "Open-Meteo (days 8+)" in r["sources"]


def test_forecast_hourly(home):
    r = call("weather_forecast", hourly=True, hours=24)
    assert len(r["hours"]) == 24 and r["hours"][0]["time"]


def test_forecast_abroad(home):
    r = call("weather_forecast", location="London, UK", days=5)
    assert len(r["days"]) == 5 and r["sources"] == ["Open-Meteo"]


def test_alerts_by_point_and_state(home):
    r = call("weather_alerts")
    assert r["count"] == len(r["alerts"])
    r = call("weather_alerts", state="IA")
    assert r["where"].startswith("IA")
    bad = call("weather_alerts", state="Iowa")
    assert "error" in bad


def test_severe(home):
    r = call("weather_severe")
    assert [d["day"] for d in r["spc_outlook"]] == [1, 2, 3]
    assert "hazards" in r["spc_outlook"][0]
    assert r["summary"]


def test_places_tool(home):
    assert call("weather_places", action="list")["home"] == "Des Moines, IA"
    saved = call("weather_places", action="save", name="airport", location="KDSM")
    assert saved["ok"] and saved["place"]["kind"] == "saved"
    assert call("weather", location="airport")["place"]["kind"] == "saved"
    assert call("weather_places", action="remove", name="airport")["ok"]
    assert "error" in call("weather_places", action="save", name="home", location="KDSM")
    assert call("weather_places", action="set_home", location="50309")["ok"]


def test_handlers_never_raise(home, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tools.report, "now", boom)
    r = call("weather")
    assert "kaboom" in r["error"]
    assert "error" in call("weather", location="Nowhereville Zzq")


def test_slash_command_works_without_ai(home):
    assert commands.slash_command("").startswith("**Des Moines, IA**")
    assert "forecast" in commands.slash_command("forecast")
    assert commands.slash_command("help") == commands.USAGE


class FakeCtx:
    def __init__(self):
        self.tools, self.commands, self.cli, self.skills, self.blueprints = {}, {}, {}, {}, {}
        self.config, self.state_store = {}, {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = path

    def register_automation_blueprint(self, key, *, title, description, schedule_template, prompt_template,
                                      category="general", slots=(), deliver_default="origin", skills=(), tags=()):
        assert ":" not in key and "{place}" in prompt_template
        self.blueprints[key] = schedule_template

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value


def test_register_wires_everything_and_matches_manifest():
    ctx = FakeCtx()
    better_weather.register(ctx)
    assert set(ctx.tools) == {s["name"] for s in schemas.ALL}
    assert "weather" in ctx.commands and "better-weather" in ctx.cli and "weather" in ctx.skills
    assert len(ctx.tools) == 16 and set(ctx.blueprints) == {"morning-brief", "week-ahead", "weekend-plan"}
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    block = manifest.split("provides_tools:")[1].split("provides_hooks:")[0]
    declared = set(re.findall(r"-\s+(\w+)", block))
    assert declared == set(ctx.tools)
    from better_weather.core import settings

    ctx.config["home"] = "KDSM"
    assert settings.current().home == "KDSM"
    settings.reset_for_tests()


@pytest.mark.parametrize("schema", schemas.ALL, ids=lambda s: s["name"])
def test_schemas_are_well_formed(schema):
    assert schema["parameters"]["type"] == "object"
    assert set(schema["parameters"].get("required", [])) <= set(schema["parameters"]["properties"])
    # Tool Search lists each deferred tool by its first sentence, cut at 60 characters
    assert len(schema["description"].split(". ")[0]) <= 60, schema["description"].split(". ")[0]


def test_numpy_is_not_loaded_just_by_loading_the_plugin():
    import subprocess
    import sys

    code = (
        "import importlib.util, sys; root = sys.argv[1]; "
        "spec = importlib.util.spec_from_file_location('bw', root + '/__init__.py', "
        "submodule_search_locations=[root]); "
        "m = importlib.util.module_from_spec(spec); sys.modules['bw'] = m; spec.loader.exec_module(m); "
        "print('numpy' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"



def test_chat_cards_are_compact_and_table_free(home):
    now = call("weather")
    card = now["card"]
    assert card.startswith("**Des Moines, IA** · ") and "|" not in card
    assert ("✅ No alerts" in card) or ("⚠️" in card) or ("🚨" in card)
    assert not any(k.startswith("_") for k in now) and "next_hours" not in now
    daily = call("weather_forecast", days=9)["card"]
    assert daily.splitlines()[0] == "**Des Moines, IA** · forecast" and len(daily.splitlines()) <= 12
    hourly = call("weather_forecast", hourly=True, hours=24)["card"]
    assert len(hourly.splitlines()) == 13  # header + every 2nd hour of 24


def test_icons():
    from better_weather.core import display

    assert display.icon("Chance Showers And Thunderstorms") == "⛈️"
    assert display.icon("Mostly Clear", is_day=False) == "🌙"
    assert display.icon("Patchy Fog") == "🌫️"
    assert display.icon("Light Snow Likely") == "🌨️"
    assert display.icon("Sunny") == "☀️"
