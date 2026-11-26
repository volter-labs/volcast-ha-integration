"""Kreator huba `modbus`: raport weryfikacji przed zgodą (identyfikacja + porównanie z encjami użytkownika)."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_options_hub import hub_flow, run  # noqa: I001 — najpierw atrapy HA
import homeassistant.util.dt as dt_util
from custom_components.volcast import config_flow as cf_mod
from custom_components.volcast import sensor_assignment as sa_mod

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.verification_entities import VolcastVerificationSensor
from tests.control.test_ha_modbus_control import HUB, UNIT

USER = {"hub": HUB, "unit_id": UNIT}


def setup(monkeypatch, *, soc=None, power=None, bank=None, options=None, negate=False):
    f, hub = hub_flow(monkeypatch)
    for a, v in (bank or {}).items():
        hub.bank[a] = v & 0xFFFF
        hub.refresh(a)
    tmap = {}
    for key, eid, val in (("soc", "sensor.user_soc", soc), ("battery_power_w", "sensor.user_bat", power)):
        if val is not None:
            hub.hass.states.set(eid, str(val), dt_util.utcnow())
            tmap[key] = eid
    f.config_entry.options.update({"telemetry_map": tmap, "battery_power_negate": negate, **(options or {})})
    saved = []

    def update(entry, *, options):
        entry.options = dict(options)
        saved.append(options)
    f.hass.config_entries.async_update_entry = update
    f.issues = issues = {}
    monkeypatch.setattr(cf_mod, "ir", SimpleNamespace(
        async_create_issue=lambda hass, domain, iid, **kw: issues.__setitem__(iid, kw),
        async_delete_issue=lambda hass, domain, iid: issues.pop(iid, None),
        IssueSeverity=SimpleNamespace(WARNING="warning")))
    return f, hub, saved


def test_matching_values_enable_control_and_carry_the_report(monkeypatch):
    f, _, _ = setup(monkeypatch, soc=54, power=-1150, bank={588: 55, 590: -1200})
    r = run(f.async_step_control_hub(USER))
    assert r["type"] == "create_entry"
    rep = r["data"]["hub_check"]
    assert rep["verdict"] == "ok" and rep["identity_ok"] is True
    assert "sensor." not in repr(rep) and HUB not in repr(rep)


def test_soc_mismatch_gives_a_report_and_no_control(monkeypatch):
    f, _, saved = setup(monkeypatch, soc=80, power=0, bank={588: 55, 590: 0})
    r = run(f.async_step_control_hub(USER))
    assert r["type"] == "form" and r["errors"] == {"base": "hub_check_failed"}
    assert f.config_entry.options.get("control_mode") is None and "direct_target" not in f.config_entry.options
    assert f.config_entry.options["hub_check"]["verdict"] == "mismatch"
    assert f.config_entry.options["hub_check"]["quantities"]["soc"]["deviation"] == 25.0
    assert r["description_placeholders"]["problems"] and "55" in r["description_placeholders"]["problems"]


def test_wrong_device_type_is_reported_not_called_unreadable(monkeypatch):
    f, _, _ = setup(monkeypatch, soc=55, bank={0: 9999, 588: 55})
    r = run(f.async_step_control_hub(USER))
    assert r["errors"] == {"base": "hub_check_failed"}
    rep = f.config_entry.options["hub_check"]
    assert rep["verdict"] == "identity_mismatch" and rep["device_type"] == 9999
    assert f.config_entry.options.get("control_mode") is None


def test_missing_user_entities_report_cannot_compare_and_do_not_block(monkeypatch):
    f, _, _ = setup(monkeypatch, bank={588: 55})
    r = run(f.async_step_control_hub(USER))
    assert r["type"] == "create_entry" and r["data"]["hub_check"]["verdict"] == "cannot_compare"


def test_battery_negate_flag_is_applied_to_the_user_entity(monkeypatch):
    # encja użytkownika ma przeciwny znak, ale flaga to mówi — zgodne
    f, _, _ = setup(monkeypatch, soc=55, power=1200, bank={588: 55, 590: -1200}, negate=True)
    assert run(f.async_step_control_hub(USER))["data"]["hub_check"]["verdict"] == "ok"
    f2, _, _ = setup(monkeypatch, soc=55, power=1200, bank={588: 55, 590: -1200})
    r = run(f2.async_step_control_hub(USER))
    assert f2.config_entry.options["hub_check"]["quantities"]["battery_power_w"]["sign_inverted"] is True
    assert r["errors"] == {"base": "hub_check_failed"}


def test_unavailable_user_entity_cannot_compare(monkeypatch):
    f, hub, _ = setup(monkeypatch, soc=55, bank={588: 55})
    hub.hass.states.set("sensor.user_soc", "unavailable", dt_util.utcnow())
    r = run(f.async_step_control_hub(USER))
    assert r["data"]["hub_check"]["quantities"]["soc"]["verdict"] == "cannot_compare"


def test_verification_entity_shows_the_report_without_control():
    report = {"verdict": "mismatch", "identity_ok": True, "device_type": 5, "checked_at": "t", "quantities": {}}
    entry = SimpleNamespace(entry_id="e1", options={"hub_check": report})
    ent = VolcastVerificationSensor(entry, None)
    assert ent.native_value == "hub_check_failed"
    assert ent.extra_state_attributes["hub_check"] == report
    ok = VolcastVerificationSensor(SimpleNamespace(entry_id="e1", options={"hub_check": {**report, "verdict": "ok"}}),
                                   None)
    assert ok.native_value == "idle" and ok.extra_state_attributes["hub_check"]["verdict"] == "ok"
    none = VolcastVerificationSensor(SimpleNamespace(entry_id="e1", options={}), None)
    assert none.extra_state_attributes == {}


def test_idle_battery_is_reported_inconclusive_and_does_not_block(monkeypatch):
    f, _, _ = setup(monkeypatch, soc=55, power=250, bank={588: 55, 590: 25})
    r = run(f.async_step_control_hub(USER))
    assert r["type"] == "create_entry" and r["data"]["hub_check"]["verdict"] == "partial"
    assert f"hub_check_not_compared_{f.config_entry.entry_id}" in f.issues


def test_unmapped_battery_candidate_is_never_compared(monkeypatch):
    # kandydat z propozycji (znak niezweryfikowany) nie blokuje ani nie przepuszcza — porównujemy samo SoC
    f, hub, _ = setup(monkeypatch, soc=55, bank={588: 55, 590: -2000})
    hub.hass.states.set("sensor.cand_bat", "2000", dt_util.utcnow())

    async def sources(hass, entry, *, exclude=frozenset()):
        return {"soc": "sensor.user_soc", "battery_power_w": "sensor.cand_bat"}
    monkeypatch.setattr(cf_mod, "async_user_sources", sources)
    r = run(f.async_step_control_hub(USER))
    rep = r["data"]["hub_check"]
    assert r["type"] == "create_entry" and rep["quantities"]["battery_power_w"]["verdict"] == "cannot_compare"
    assert rep["quantities"]["battery_power_w"]["user"] is None and rep["quantities"]["soc"]["verdict"] == "ok"
    assert rep["verdict"] == "partial"


def test_stale_user_entity_cannot_compare(monkeypatch):
    f, hub, _ = setup(monkeypatch, soc=55, bank={588: 55})
    hub.hass.states.set("sensor.user_soc", "55", dt_util.utcnow() - timedelta(minutes=30))
    r = run(f.async_step_control_hub(USER))
    assert r["data"]["hub_check"]["quantities"]["soc"]["verdict"] == "cannot_compare"


def test_failed_recheck_turns_running_control_off_through_the_restore_gate(monkeypatch):
    target = {"transport": "ha_modbus", "hub": HUB, "unit_id": UNIT, "profile_id": "deye-sg", "device_fp": "x"}
    f, _, saved = setup(monkeypatch, soc=80, power=0, bank={588: 55, 590: 0},
                        options={"control_mode": "direct", "direct_target": target})
    gate = []

    async def allowed(rt, old, new):
        gate.append((old.get("control_mode"), new.get("control_mode")))
        return True
    monkeypatch.setattr(cf_mod, "async_control_change_allowed", allowed)
    r = run(f.async_step_control_hub(USER))
    assert r["type"] == "abort" and r["reason"] == "hub_check_control_off"
    assert gate == [("direct", None)]
    assert f.config_entry.options.get("control_mode") is None
    assert f.config_entry.options["hub_check"]["verdict"] == "mismatch"
    assert f"hub_check_failed_{f.config_entry.entry_id}" in f.issues


def test_failed_recheck_keeps_control_when_the_restore_fails(monkeypatch):
    f, _, saved = setup(monkeypatch, soc=80, bank={588: 55}, options={"control_mode": "direct"})

    async def denied(rt, old, new):
        return False
    monkeypatch.setattr(cf_mod, "async_control_change_allowed", denied)
    r = run(f.async_step_control_hub(USER))
    assert r["type"] == "form" and r["errors"] == {"base": "restore_failed"} and saved == []


def test_passing_check_clears_the_issues(monkeypatch):
    f, _, _ = setup(monkeypatch, soc=55, power=-1200, bank={588: 55, 590: -1200})
    f.issues["hub_check_failed_e1"] = {}
    run(f.async_step_control_hub(USER))
    assert f.issues == {}


def test_async_user_sources_never_returns_excluded_sensors():
    entry = SimpleNamespace(options={"telemetry_map": {"soc": "sensor.volcast_reg_588", "battery_power_w": "sensor.mine"}})
    out = asyncio.run(sa_mod.async_user_sources(SimpleNamespace(), entry,
                                                exclude=frozenset({"sensor.volcast_reg_588"})))
    assert out == {"battery_power_w": "sensor.mine"}


@pytest.mark.parametrize("options,expect", [
    ({"direct_target": {"transport": "ha_modbus"}}, True), ({"direct_target": {"transport": "tcp"}}, False),
    ({}, False)])
def test_stale_report_is_hidden_when_control_runs_another_way(options, expect):
    report = {"verdict": "mismatch", "identity_ok": True, "device_type": 5, "checked_at": "t", "quantities": {}}
    entry = SimpleNamespace(entry_id="e1", options={"hub_check": report, **options})
    ent = VolcastVerificationSensor(entry, SimpleNamespace(verification=None))
    assert ("hub_check" in ent.extra_state_attributes) is expect
    assert (ent.native_value == "hub_check_failed") is expect
