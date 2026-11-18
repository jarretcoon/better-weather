import argparse
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from better_weather.core import cache, clock, commands, http, places, settings, watch
from better_weather.core.places import Place

CONFIG = {"name": "Des Moines, IA", "lat": 41.6, "lon": -93.61, "timezone": "America/Chicago",
          "level": "watches", "clock": "12h"}


def _feature(ident, event, vtec_action="NEW", etn="0012", phen="TO", sig="W", ends="2026-11-15T15:45:00-06:00",
             msg="Alert", refs=(), instruction="TAKE COVER NOW!", vtec=True, onset="2026-11-15T02:30:00-06:00"):
    feature = {"properties": {
        "id": ident, "event": event, "severity": "Extreme" if "Tornado Warning" in event else "Moderate",
        "status": "Actual", "messageType": msg, "references": [{"identifier": r} for r in refs],
        "headline": f"{event} issued", "description": "...", "instruction": instruction, "ends": ends,
        "onset": onset,
        "parameters": {"VTEC": [f"/O.{vtec_action}.KDMX.{phen}.{sig}.{etn}.261115T2100Z-261115T2145Z/"],
                       "tornadoDetection": ["RADAR INDICATED"]},
    }}
    if not vtec:
        del feature["properties"]["parameters"]["VTEC"]
    return feature


def _serve(features):
    return httpx.MockTransport(lambda r: httpx.Response(200, json={"features": features}))


def test_vtec_key_is_stable_across_updates():
    alert = {"event": "Tornado Warning", "id": "x"}
    key, action = watch.event_key(alert, {"VTEC": ["/O.CON.KDMX.TO.W.0012.000000T0000Z-261115T2145Z/"]})
    assert key == "KDMX.TO.W.0012" and action == "CON"


def test_new_warning_pushes_once_then_stays_quiet_then_says_ended():
    http.set_transport_for_tests(_serve([_feature("a1", "Tornado Warning")]))
    message, state = watch.check(CONFIG, {})
    assert message.startswith("🚨 TORNADO WARNING — Des Moines, IA, until 3:45 PM")
    assert "TAKE COVER NOW!" in message and "Tornado: radar indicated" in message

    http.set_transport_for_tests(_serve([_feature("a2", "Tornado Warning", vtec_action="CON", msg="Update",
                                                  refs=["a1"])]))
    message, state = watch.check(CONFIG, state)
    assert message == ""

    http.set_transport_for_tests(_serve([]))
    message, state = watch.check(CONFIG, state)
    assert message == ""  # one missing response could be a glitch: wait for a second miss
    message, state = watch.check(CONFIG, state)
    assert message == "✅ Tornado Warning for Des Moines, IA has ended or expired."
    assert state["sent"] == {}


def test_extension_is_pushed():
    http.set_transport_for_tests(_serve([_feature("w1", "Winter Storm Warning", phen="WS", etn="0004")]))
    _, state = watch.check(CONFIG, {})
    http.set_transport_for_tests(_serve([_feature("w2", "Winter Storm Warning", phen="WS", etn="0004",
                                                  vtec_action="EXT", msg="Update", refs=["w1"],
                                                  ends="2026-11-15T21:00:00-06:00")]))
    message, _ = watch.check(CONFIG, state)
    assert message.startswith("⏱ Winter Storm Warning for Des Moines, IA extended until 9 PM")


def test_level_filters_advisories():
    advisory = _feature("f1", "Frost Advisory", phen="FR", sig="Y", etn="0001", instruction="")
    http.set_transport_for_tests(_serve([advisory]))
    assert watch.check(CONFIG, {})[0] == ""
    assert "FROST ADVISORY" in watch.check({**CONFIG, "level": "all"}, {})[0]
    watch_alert = _feature("t1", "Tornado Watch", sig="A", etn="0101", instruction="")
    http.set_transport_for_tests(_serve([watch_alert]))
    assert watch.check({**CONFIG, "level": "warnings"}, {})[0] == ""
    assert "TORNADO WATCH" in watch.check(CONFIG, {})[0]


def test_outage_is_reported_once_and_recovery_too():
    http.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(503)))
    state: dict = {}
    said = []
    for _ in range(watch.FAIL_NOTICE_AFTER + 3):
        message, state = watch.check(CONFIG, state)
        if message:
            said.append(message)
    assert len(said) == 1 and "can't reach the NWS alert service" in said[0]
    http.set_transport_for_tests(_serve([]))
    message, state = watch.check(CONFIG, state)
    assert message == "✅ Alert checks are working again." and state["failures"] == 0


def test_run_reports_a_crash_once(tmp_path, monkeypatch):
    watch._save(tmp_path / watch.CONFIG_NAME, CONFIG)

    def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(watch, "check", boom)
    first = watch.run(tmp_path)
    second = watch.run(tmp_path)
    assert "kaboom" in first and second == ""


def test_setup_writes_a_valid_script_and_config(tmp_path):
    out = watch.write_setup(hermes_home=tmp_path / "home", data_dir=tmp_path / "data",
                            core_dir=tmp_path / "plugin" / "core", place={"name": "X", "lat": 1, "lon": 2},
                            level="warnings", freeze=True, rain=False, units_="us", clock="12h")
    script = (tmp_path / "home" / "scripts" / watch.SCRIPT_NAME).read_text(encoding="utf-8")
    compile(script, "script", "exec")
    assert repr(str(tmp_path / "data")) in script and "--no-agent" in out["cron_command"]  # escaped on Windows
    assert watch._load(tmp_path / "data" / watch.CONFIG_NAME, {})["level"] == "warnings"


# --- regressions from the independent review -----------------------------------------------------

def test_broken_extras_never_block_a_warning(monkeypatch):
    def boom(*_a, **_k):
        raise TypeError("bad freeze setting")

    monkeypatch.setattr(watch, "_extras", boom)
    http.set_transport_for_tests(_serve([_feature("a1", "Tornado Warning")]))
    message, state = watch.check({**CONFIG, "freeze": True}, {})
    assert "TORNADO WARNING" in message and state["sent"]


def test_cancel_or_upgrade_notices_are_not_news_and_the_old_alert_ends_after_two_misses():
    """As in the live feed: CAN and UPG notices are messageType "Cancel" (dropped, and /alerts/active doesn't list
    them), so a cancelled alert just vanishes and ends after two misses like any other."""
    upgraded = _feature("u1", "Gale Watch", vtec_action="UPG", phen="GL", sig="A", etn="0006", msg="Cancel",
                        instruction="")
    http.set_transport_for_tests(_serve([upgraded]))
    assert watch.check({**CONFIG, "level": "all"}, {})[0] == ""  # a replaced watch is not news

    http.set_transport_for_tests(_serve([_feature("w1", "Tornado Watch", sig="A", etn="0101", instruction="")]))
    _, state = watch.check(CONFIG, {})
    http.set_transport_for_tests(_serve([_feature("w2", "Tornado Watch", sig="A", etn="0101", vtec_action="CAN",
                                                  msg="Cancel", refs=["w1"], instruction="")]))
    message, state = watch.check(CONFIG, state)
    assert message == ""
    message, state = watch.check(CONFIG, state)
    assert message == "✅ Tornado Watch for Des Moines, IA has ended or expired." and state["sent"] == {}


def test_an_expiry_notice_ends_the_alert_right_away():
    """EXP notices are "Update"/"Alert" messages in the live feed, so they do come through."""
    http.set_transport_for_tests(_serve([_feature("w1", "Winter Storm Warning", phen="WS", etn="0004")]))
    _, state = watch.check(CONFIG, {})
    http.set_transport_for_tests(_serve([_feature("w2", "Winter Storm Warning", phen="WS", etn="0004",
                                                  vtec_action="EXP", msg="Update", refs=["w1"])]))
    message, state = watch.check(CONFIG, state)
    assert message == "✅ Winter Storm Warning for Des Moines, IA has expired." and state["sent"] == {}


def test_a_watch_upgraded_to_a_warning_is_not_an_all_clear():
    """KMQT, 2026-11-18: a Gale Watch upgraded to a Gale Warning. The UPG notice is a "Cancel" (dropped) and the new
    warning doesn't reference the watch, so the watch just vanished: "✅ … has ended or expired" 4 min later."""
    config = {**CONFIG, "name": "Marquette, MI", "timezone": "America/Detroit"}
    http.set_transport_for_tests(_serve([_feature("g1", "Gale Watch", phen="GL", sig="A", etn="0031",
                                                  instruction="")]))
    _, state = watch.check(config, {})
    upgrade = [_feature("g2", "Gale Warning", phen="GL", sig="W", etn="0045", instruction=""),
               _feature("g3", "Gale Watch", vtec_action="UPG", phen="GL", sig="A", etn="0031", msg="Cancel",
                        instruction="")]
    http.set_transport_for_tests(_serve(upgrade))
    message, state = watch.check(config, state)
    assert message.startswith("⚠️ GALE WARNING — Marquette, MI") and "✅" not in message
    message, state = watch.check(config, state)
    assert message == "⬆️ Gale Watch for Marquette, MI was upgraded to Gale Warning."
    assert list(state["sent"]) == ["KDMX.GL.W.0045"]


def test_an_upgrade_up_a_ladder_and_not_an_old_warning():
    http.set_transport_for_tests(_serve([_feature("t1", "Tropical Storm Warning", phen="TR", etn="1001",
                                                  instruction=""),
                                         _feature("s1", "Winter Storm Watch", phen="WS", sig="A", etn="0009",
                                                  instruction=""),
                                         _feature("s2", "Winter Storm Warning", phen="WS", etn="0008",
                                                  instruction="")]))
    _, state = watch.check(CONFIG, {})
    state["sent"]["KDMX.WS.W.0008"]["at"] -= 6 * 3600   # a warning for the storm before, up for hours
    http.set_transport_for_tests(_serve([_feature("h1", "Hurricane Warning", phen="HU", etn="1001", instruction=""),
                                         _feature("s2", "Winter Storm Warning", phen="WS", etn="0008",
                                                  instruction="")]))
    _, state = watch.check(CONFIG, state)
    message, state = watch.check(CONFIG, state)
    assert message.split("\n\n") == ["⬆️ Tropical Storm Warning for Des Moines, IA was upgraded to Hurricane Warning.",
                                      "✅ Winter Storm Watch for Des Moines, IA has ended or expired."]


def test_alert_without_vtec_keeps_its_key_through_updates():
    first = _feature("c1", "Civil Emergency Message", vtec=False, instruction="")
    http.set_transport_for_tests(_serve([first]))
    message, state = watch.check(CONFIG, {})
    assert "CIVIL EMERGENCY MESSAGE" in message
    update = _feature("c2", "Civil Emergency Message", vtec=False, msg="Update", refs=["c1"], instruction="")
    update["properties"]["onset"] = "2026-11-15T16:00:00-06:00"
    http.set_transport_for_tests(_serve([update]))
    message, state = watch.check(CONFIG, state)
    assert message == "" and len(state["sent"]) == 1


def test_until_further_notice_has_no_made_up_end_time():
    flood = _feature("f1", "Flood Warning", phen="FL", etn="0007", ends=None, instruction="Turn around, don't drown.")
    http.set_transport_for_tests(_serve([flood]))
    message, _ = watch.check(CONFIG, {})
    assert message.splitlines()[0] == "⚠️ FLOOD WARNING — Des Moines, IA"


def test_an_alert_that_hasnt_started_says_when_it_starts():
    """Ojai, 2026-11-18: a Flood Watch starting overnight was pushed as "FLOOD WATCH — Ojai, CA, until …", which read
    as in effect now. (The clock: 2:38 AM in Des Moines.)"""
    watch_ = _feature("f1", "Flood Watch", phen="FA", sig="A", etn="0004", onset="2026-11-16T03:00:00-06:00",
                      ends="2026-11-16T21:00:00-06:00", instruction="")
    later = _feature("f2", "Winter Storm Warning", phen="WS", etn="0002", onset="2026-11-15T22:00:00-06:00",
                     ends=None, instruction="")
    now = _feature("f3", "Wind Advisory", phen="WI", sig="Y", etn="0007", onset="2026-11-15T02:41:00-06:00",
                   ends="2026-11-15T09:00:00-06:00", instruction="")
    http.set_transport_for_tests(_serve([watch_, later, now]))
    firsts = [m.splitlines()[0] for m in watch.check({**CONFIG, "level": "all"}, {})[0].split("\n\n")]
    assert sorted(firsts) == ["⚠️ FLOOD WATCH — Des Moines, IA, from tomorrow 3 AM to 9 PM",
                              "⚠️ WIND ADVISORY — Des Moines, IA, until 9 AM",   # starts in 3 minutes: now
                              "⚠️ WINTER STORM WARNING — Des Moines, IA, from 10 PM"]


def test_push_text_is_not_hard_wrapped():
    """Ojai, 2026-11-18: a push read "…possible Flood\\nWarnings…" (the NWS wraps its text at ~70 columns)."""
    text = ("Monitor later forecasts and be alert for possible Flood\nWarnings. Those living in areas prone to\n"
            "flooding should be prepared to take action should flooding\ndevelop. ") * 6
    flood = _feature("f1", "Flood Watch", phen="FA", sig="A", etn="0004", instruction=text)
    params = flood["properties"]["parameters"]
    del params["tornadoDetection"]
    params["NWSheadline"] = ["FLOOD WATCH IN EFFECT FROM LATE WEDNESDAY NIGHT\nTHROUGH THURSDAY AFTERNOON"]
    http.set_transport_for_tests(_serve([flood]))
    lines = watch.check(CONFIG, {})[0].splitlines()
    assert lines[1] == "FLOOD WATCH IN EFFECT FROM LATE WEDNESDAY NIGHT THROUGH THURSDAY AFTERNOON"
    assert lines[2].startswith("Monitor later forecasts and be alert for possible Flood Warnings. Those living")
    assert lines[2].endswith("…") and len(lines[2]) <= watch.INSTRUCTION_CHARS + 1 and not lines[2].endswith(" …")
    assert lines[3] == "— National Weather Service"


def test_missing_settings_are_reported_once_a_day(tmp_path):
    first = watch.run(tmp_path)
    second = watch.run(tmp_path)
    assert "no place to watch" in first and second == ""


def test_paused_pushes_stay_silent(tmp_path):
    watch._save(tmp_path / watch.CONFIG_NAME, {**CONFIG, "paused": True})
    http.set_transport_for_tests(_serve([_feature("a1", "Tornado Warning")]))
    assert watch.run(tmp_path) == ""


# --- freeze and rain/snow heads-ups ---------------------------------------------------------------

def _model(start_local, temps, *, chance=0, precip=0.0, snow=0.0, current_precip=0.0, current_code=0):
    """Open-Meteo-shaped forecast: hourly rows from start_local (naive local ISO), one per temp."""
    base = datetime.fromisoformat(start_local)
    times = [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(len(temps))]
    n = len(temps)
    return {"current": {"precipitation": current_precip, "weather_code": current_code},
            "hourly": {"time": times, "temperature_2m": temps, "precipitation_probability": [chance] * n,
                       "precipitation": [precip] * n, "snowfall": [snow] * n},
            "daily": {"time": []}}


def _at(local_iso):
    """Freeze the clock at a Des Moines local time (CST, UTC-6)."""
    clock.freeze_for_tests(datetime.fromisoformat(local_iso).replace(tzinfo=timezone(timedelta(hours=-6))))


def _serve_model(payload):
    cache.use_memory_cache_for_tests()  # each scenario is a different forecast; skip the 15-min cache
    def handler(request):
        if "open-meteo" in str(request.url):
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"features": []})
    return httpx.MockTransport(handler)


FREEZE = {**CONFIG, "freeze": True}
RAIN = {**CONFIG, "rain": True}


def test_first_freeze_after_a_mild_stretch_only():
    _at("2026-10-20T15:05")
    http.set_transport_for_tests(_serve_model(_model("2026-10-20T15:00", [5.0] * 10 + [-2.0] * 8 + [3.0] * 3)))
    message, state = watch.check(FREEZE, {})
    assert message.startswith("🥶 First freeze in a while tonight in Des Moines, IA: low near 28°F around 1 AM")
    assert watch.check(FREEZE, state)[0] == ""  # once a day
    _at("2026-10-21T15:05")  # the next night freezes too: same cold spell, stay quiet
    http.set_transport_for_tests(_serve_model(_model("2026-10-21T15:00", [-1.0] * 20)))
    message, state = watch.check(FREEZE, state)
    assert message == ""
    _at("2026-10-26T15:05")  # five nights later, after a mild stretch: worth saying again
    http.set_transport_for_tests(_serve_model(_model("2026-10-26T15:00", [-3.0] * 20)))
    assert watch.check(FREEZE, state)[0].startswith("🥶")


def test_no_freeze_check_before_mid_afternoon():
    _at("2026-10-20T09:00")
    http.set_transport_for_tests(_serve_model(_model("2026-10-20T09:00", [-5.0] * 24)))
    message, state = watch.check(FREEZE, {})
    assert message == "" and "freeze_day" not in state


def test_rain_soon_only_when_dry_now_and_awake_hours():
    _at("2026-10-20T13:20")
    http.set_transport_for_tests(_serve_model(_model("2026-10-20T13:00", [12.0] * 4, chance=80, precip=1.5)))
    message, state = watch.check(RAIN, {})
    assert message == "☔ Rain likely to start in Des Moines, IA within the hour."
    assert watch.check(RAIN, state)[0] == ""  # not again within 3 hours
    # already raining: nothing "starting"
    http.set_transport_for_tests(_serve_model(_model("2026-10-20T13:00", [12.0] * 4, chance=80, precip=1.5,
                                                     current_precip=0.8, current_code=63)))
    assert watch.check(RAIN, {})[0] == ""
    # 2 AM: quiet hours
    _at("2026-10-21T02:20")
    http.set_transport_for_tests(_serve_model(_model("2026-10-21T02:00", [12.0] * 4, chance=80, precip=1.5)))
    assert watch.check(RAIN, {})[0] == ""


def test_snow_is_called_snow():
    _at("2026-12-20T13:20")
    http.set_transport_for_tests(_serve_model(_model("2026-12-20T13:00", [-4.0] * 4, chance=90, precip=0.6,
                                                     snow=0.5)))
    assert watch.check(RAIN, {})[0] == "🌨️ Snow likely to start in Des Moines, IA within the hour."


def test_zero_degrees_counts_as_freezing():
    """0.0 °C read as "no reading": last night's 0.0 °C wasn't a freeze (a false "first freeze"), and 0.0 °C rain
    wasn't snow."""
    _at("2026-11-15T15:05")
    temps = [2.0] * 60 + [0.0] * 6 + [6.0] * 6 + [1.0] * 9 + [-2.0] * 10 + [3.0] * 10
    http.set_transport_for_tests(_serve_model(_model("2026-11-12T00:00", temps)))
    assert watch.check(FREEZE, {})[0] == ""
    _at("2026-12-20T13:20")
    http.set_transport_for_tests(_serve_model(_model("2026-12-20T13:00", [0.0] * 4, chance=90, precip=0.6)))
    assert watch.check(RAIN, {})[0] == "🌨️ Snow likely to start in Des Moines, IA within the hour."


def test_no_first_freeze_claim_when_it_froze_last_night_even_without_history():
    _at("2026-11-15T15:05")
    # model rows start 3 days back: last night dropped to -4°C, tonight -2°C
    temps = [2.0] * 60 + [-4.0] * 6 + [6.0] * 6 + [1.0] * 9 + [-2.0] * 10 + [3.0] * 10
    http.set_transport_for_tests(_serve_model(_model("2026-11-12T00:00", temps)))
    message, state = watch.check(FREEZE, {})
    assert message == "" and state["last_freeze_night"] == "2026-11-15"


# --- pushes are the NWS's alerts: US only (the audit of 0.9.2) --------------------------------------

def _setup(tmp_path, monkeypatch, place, points_status=200, points=None):
    settings.set_data_dir(tmp_path / "data")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(places, "resolve", lambda _q: place)

    def handler(request):
        if request.url.path.startswith("/points/"):
            return httpx.Response(points_status, json=points or {"status": points_status})
        return httpx.Response(503)
    http.set_transport_for_tests(httpx.MockTransport(handler))
    args = argparse.Namespace(push_command="setup", place=["x"], level="watches", freeze=False, rain=False)
    return commands._push_cli(args)


def test_push_setup_refuses_a_place_abroad(tmp_path, monkeypatch, capsys):
    """Berlin, 2026-11-18: setup took it, and every check after read the NWS's "out of bounds" as no alerts."""
    berlin = Place("Berlin, Berlin, DE", 52.52, 13.405, "DE", timezone="Europe/Berlin")
    assert _setup(tmp_path, monkeypatch, berlin) == 1
    assert capsys.readouterr().out.startswith("Warning pushes are US-only")
    assert not (tmp_path / "data" / watch.CONFIG_NAME).exists()
    coords = Place("52.520, 13.405", 52.52, 13.405, kind="coords")   # no country: the NWS's grid decides
    assert _setup(tmp_path, monkeypatch, coords, points_status=404) == 1
    assert "US-only" in capsys.readouterr().out
    nws_down = Place("43.650, -79.380", 43.65, -79.38, kind="coords")
    assert _setup(tmp_path, monkeypatch, nws_down, points_status=503) == 1
    assert "Try again in a few minutes" in capsys.readouterr().out
    assert not (tmp_path / "data" / watch.CONFIG_NAME).exists()


def test_push_setup_saves_the_places_own_zone(tmp_path, monkeypatch, capsys):
    coords = Place("41.600, -93.610", 41.6, -93.61, kind="coords")
    point = {"properties": {"timeZone": "America/Chicago", "gridId": "DMX"}}
    assert _setup(tmp_path, monkeypatch, coords, points=point) == 0
    assert capsys.readouterr().out.startswith("Watching 41.600, -93.610 (watches).")
    assert watch._load(tmp_path / "data" / watch.CONFIG_NAME, {})["timezone"] == "America/Chicago"


def test_a_home_abroad_says_pushes_are_us_only_once_a_day():
    def handler(request):
        return httpx.Response(400, json={"status": 400, "detail": "Parameter \"point\" is invalid: out of bounds"})
    http.set_transport_for_tests(httpx.MockTransport(handler))
    berlin = {**CONFIG, "name": "Berlin", "lat": 52.52, "lon": 13.405, "timezone": "Europe/Berlin", "level": "all"}
    message, state = watch.check(berlin, {})
    assert message.startswith("⚠️ Better Weather warning pushes are US-only, and Berlin is outside")
    assert watch.check(berlin, state)[0] == "" and not state.get("failures")


# --- one run at a time, each within a time budget (the audit of 0.9.2) -------------------------------

def test_a_run_while_another_holds_the_lock_stays_quiet(tmp_path, monkeypatch):
    watch._save(tmp_path / watch.CONFIG_NAME, CONFIG)
    calls = []
    monkeypatch.setattr(watch, "check", lambda *a, **k: calls.append(1) or ("🚨 x", {}))
    (tmp_path / watch.LOCK_NAME).write_text("1234 0", encoding="utf-8")
    assert watch.run(tmp_path) == "" and calls == []
    assert not (tmp_path / watch.STATE_NAME).exists()          # and it never touched the state
    assert (tmp_path / watch.LOCK_NAME).read_text(encoding="utf-8") == "1234 0"


def test_a_stale_lock_is_taken_over_and_released(tmp_path, monkeypatch):
    watch._save(tmp_path / watch.CONFIG_NAME, CONFIG)
    monkeypatch.setattr(watch, "check", lambda config, state, **k: ("🚨 x", {**state, "sent": {}}))
    lock = tmp_path / watch.LOCK_NAME
    lock.write_text("1234 0", encoding="utf-8")
    old = time.time() - watch.LOCK_STALE_S - 60
    os.utime(lock, (old, old))
    assert watch.run(tmp_path) == "🚨 x"
    assert not lock.exists() and not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))


def test_the_lock_is_released_after_a_crash(tmp_path, monkeypatch):
    watch._save(tmp_path / watch.CONFIG_NAME, CONFIG)

    def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(watch, "check", boom)
    assert "kaboom" in watch.run(tmp_path)
    assert not (tmp_path / watch.LOCK_NAME).exists()


def test_a_run_that_runs_out_of_time_gives_up_as_a_failed_check(tmp_path, monkeypatch):
    watch._save(tmp_path / watch.CONFIG_NAME, CONFIG)
    watch._save(tmp_path / watch.STATE_NAME, {"failures": watch.FAIL_NOTICE_AFTER - 2, "sent": {}})
    release = threading.Event()
    monkeypatch.setattr(watch, "RUN_BUDGET_S", 0.3)
    monkeypatch.setattr(watch, "check", lambda *a, **k: release.wait(5) and ("never", {}))
    try:
        assert watch.run(tmp_path) == ""
        assert watch.run(tmp_path).startswith("⚠️ Better Weather can't reach the NWS alert service.")
    finally:
        release.set()
    assert watch._load(tmp_path / watch.STATE_NAME, {})["failures"] == watch.FAIL_NOTICE_AFTER
    assert not (tmp_path / watch.LOCK_NAME).exists()


def test_slow_extras_never_cost_a_warning(monkeypatch):
    release = threading.Event()

    def slow(config, state, *_a):
        release.wait(5)
        state["rain_at"] = 1.0
        return ["☔ late"]

    monkeypatch.setattr(watch, "_extras", slow)
    monkeypatch.setattr(watch, "EXTRAS_MIN_S", 0.1)
    http.set_transport_for_tests(_serve([_feature("a1", "Tornado Warning")]))
    try:
        message, state = watch.check({**CONFIG, "rain": True}, {}, deadline=time.monotonic() + 0.4)
    finally:
        release.set()
    assert message.startswith("🚨 TORNADO WARNING") and "☔" not in message
    assert state["sent"] and "rain_at" not in state


# --- a saved place without a time zone (the audit of 0.9.2) -----------------------------------------

def _no_zone_world(points_zone=None, model=None):
    cache.use_memory_cache_for_tests()

    def handler(request):
        if "open-meteo" in str(request.url):
            return httpx.Response(200, json=model) if model else httpx.Response(503)
        if request.url.path.startswith("/points/"):
            if points_zone:
                return httpx.Response(200, json={"properties": {"timeZone": points_zone}})
            return httpx.Response(503)
        return httpx.Response(200, json={"features": [_feature("a1", "Tornado Warning")]})
    return httpx.MockTransport(handler)


def test_no_saved_zone_uses_the_places_own_never_utc():
    """`timezone: None` in watch.json made every time UTC ("until 9:45 PM" for a 3:45 PM CST end)."""
    config = {**CONFIG, "timezone": None}
    http.set_transport_for_tests(_no_zone_world(points_zone="America/Chicago"))
    assert watch.check(config, {})[0].startswith("🚨 TORNADO WARNING — Des Moines, IA, until 3:45 PM")
    http.set_transport_for_tests(_no_zone_world())     # no lookup answers: the alert's own (local) offset
    assert watch.check(config, {})[0].startswith("🚨 TORNADO WARNING — Des Moines, IA, until 3:45 PM")


def test_no_saved_zone_keeps_the_rain_hours_local():
    """5:30 PM in Des Moines is 23:30 UTC: past the 10 PM cutoff if the zone were UTC."""
    _at("2026-10-20T17:30")
    model = {**_model("2026-10-20T18:00", [12.0] * 4, chance=80, precip=1.5), "timezone": "America/Chicago",
             "utc_offset_seconds": -5 * 3600}
    http.set_transport_for_tests(_no_zone_world(model=model))
    message = watch.check({**RAIN, "timezone": None}, {})[0]
    assert message.endswith("☔ Rain likely to start in Des Moines, IA within the hour.")
