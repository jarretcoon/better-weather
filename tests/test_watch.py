import httpx
from better_weather.core import http, watch

CONFIG = {"name": "Des Moines, IA", "lat": 41.6, "lon": -93.61, "timezone": "America/Chicago",
          "level": "watches", "clock": "12h"}


def _feature(ident, event, vtec_action="NEW", etn="0012", phen="TO", sig="W", ends="2026-11-15T15:45:00-06:00",
             msg="Alert", refs=(), instruction="TAKE COVER NOW!"):
    return {"properties": {
        "id": ident, "event": event, "severity": "Extreme" if "Tornado Warning" in event else "Moderate",
        "status": "Actual", "messageType": msg, "references": [{"identifier": r} for r in refs],
        "headline": f"{event} issued", "description": "...", "instruction": instruction, "ends": ends,
        "onset": "2026-11-15T15:00:00-06:00",
        "parameters": {"VTEC": [f"/O.{vtec_action}.KDMX.{phen}.{sig}.{etn}.261115T2100Z-261115T2145Z/"],
                       "tornadoDetection": ["RADAR INDICATED"]},
    }}


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
    assert str(tmp_path / "data") in script and "--no-agent" in out["cron_command"]
    assert watch._load(tmp_path / "data" / watch.CONFIG_NAME, {})["level"] == "warnings"
