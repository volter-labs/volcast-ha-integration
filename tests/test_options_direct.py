"""Opcje: „Bezpośrednio”, wyszukiwanie falownika, cel ręczny, połączenie próbne."""
import asyncio
import sys
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

import tests.test_options_flow as base  # noqa: F401 — atrapy flow (menu, abort, selektory)
from custom_components.volcast import config_flow as cf_mod
from custom_components.volcast.config_flow import VolcastOptionsFlow
from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control import direct_search as ds_mod
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.core.discovery.identify import Candidate, Identity
from custom_components.volcast.core.discovery.probe import ProbeReport
from custom_components.volcast.core.profile import load_builtin, profile_from_dict

_ce = sys.modules["homeassistant.config_entries"]
for _name, _fn in {
    "async_show_progress": lambda self, *, step_id, progress_action, progress_task=None, **_: {
        "type": "progress", "step_id": step_id, "progress_action": progress_action, "task": progress_task},
    "async_show_progress_done": lambda self, *, next_step_id: {"type": "progress_done", "next_step_id": next_step_id},
}.items():
    if not hasattr(_ce.OptionsFlowWithConfigEntry, _name):
        setattr(_ce.OptionsFlowWithConfigEntry, _name, _fn)

HOST = "192.168.77.9"
FP = "0123456789abcdef"


def _thaw(v):
    if isinstance(v, Mapping):
        return {k: _thaw(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_thaw(x) for x in v]
    return v


def verified(pid):
    raw = _thaw(load_builtin(pid).raw)
    raw["status"] = "verified"
    raw["modbus"]["status"] = "verified"
    return profile_from_dict(raw)


GW_V = verified("goodwe-et")
GW = load_builtin("goodwe-et")
DEYE = load_builtin("deye-sg")


def report(*, profile_id="goodwe-et", available=True, host=HOST, errors=(), identity=True, transport="goodwe_udp",
           logger_serial=None, rated=10000.0):
    ident = Identity(profile_id, transport, 8899, 247, "GW10K-ET", rated, FP) if identity else None
    caps = {"mode": True, "power_w": True, "soc_min": True, "soc_max": True, "export_limit_w": True,
            "export_limit_enabled": False}
    return ProbeReport(ident, caps if identity else {}, ("soc_max",) if identity else (), available,
                       None, "verified", 12, tuple(errors), Candidate(host, "udp_48899", logger_serial))


class Entries:
    def __init__(self, entries):
        self._e = list(entries)

    def async_entries(self, domain=None):
        return [e for e in self._e if domain is None or e.domain == domain]


def flow(*, options=None, runtime=None, entries=(), profiles=(GW_V, DEYE), monkeypatch=None):
    entry = SimpleNamespace(entry_id="e1", domain=DOMAIN, data={"api_key": "vk_x", "backend": base.BACKEND},
                            options=dict(options or {}), disabled_by=None)
    f = VolcastOptionsFlow(entry)

    async def job(fn, *a):
        return fn(*a)
    f.hass = SimpleNamespace(data={DOMAIN: {"e1": {"control": runtime}}}, states=base._States({}),
                             config=SimpleNamespace(time_zone="Europe/Warsaw"),
                             config_entries=Entries([entry, *entries]), async_add_executor_job=job,
                             async_create_task=lambda coro, *a, **k: asyncio.get_event_loop().create_task(coro))
    if monkeypatch is not None:
        monkeypatch.setattr(ds_mod, "load_profiles", lambda: list(profiles))
    return f


def rt(*, reports=(), owned=False):
    return SimpleNamespace(choice=None, mapped={}, last_probe=list(reports),
                           executor=SimpleNamespace(owned=owned, async_restore_now=_noop))


async def _noop():
    return None


# ── „Bezpośrednio” w menu sterowania ──────────────────────────────────────


def test_control_menu_has_three_items_no_default():
    r = asyncio.run(flow().async_step_control())
    assert r["menu_options"] == ["control_entities", "control_direct", "control_off"]
    assert "default" not in r


def test_control_direct_unverified_aborts(monkeypatch):
    f = flow(runtime=rt(reports=[report()]), profiles=(GW, DEYE), monkeypatch=monkeypatch)   # shipped: draft
    assert asyncio.run(f.async_step_control_direct()) == {"type": "abort", "reason": "direct_unverified"}
    f = flow(runtime=rt(reports=[report(available=False)]), monkeypatch=monkeypatch)
    assert asyncio.run(f.async_step_control_direct())["reason"] == "direct_unverified"


def test_control_direct_not_found_aborts(monkeypatch):
    for reports in ([], [report(identity=False)]):
        f = flow(runtime=rt(reports=reports), monkeypatch=monkeypatch)
        assert asyncio.run(f.async_step_control_direct())["reason"] == "direct_not_found"


def test_control_direct_conflict_aborts_with_integration_name(monkeypatch):
    goodwe = SimpleNamespace(domain="goodwe", entry_id="g", data={"host": HOST}, options={}, disabled_by=None)
    f = flow(runtime=rt(reports=[report()]), entries=[goodwe], monkeypatch=monkeypatch)
    captured = {}
    monkeypatch.setattr(VolcastOptionsFlow, "async_abort",
                        lambda self, *, reason, description_placeholders=None: captured.update(
                            reason=reason, placeholders=description_placeholders) or {"type": "abort"}, raising=False)
    asyncio.run(f.async_step_control_direct())
    assert captured == {"reason": "direct_conflict", "placeholders": {"integration": "goodwe"}}


def test_control_direct_in_use_by_other_entry(monkeypatch):
    f = flow(runtime=rt(reports=[report()]), monkeypatch=monkeypatch)
    other = SimpleNamespace(_entry=SimpleNamespace(entry_id="other"))
    f.hass.data[DOMAIN]["direct_hosts"] = {HOST: other}
    assert asyncio.run(f.async_step_control_direct())["reason"] == "direct_in_use"


def test_control_direct_verified_sets_mode_and_target(monkeypatch):
    f = flow(options={"direct_trial": True}, runtime=rt(reports=[report()]), monkeypatch=monkeypatch)
    r = asyncio.run(f.async_step_control_direct())
    assert r["type"] == "create_entry"
    opts = r["data"]
    assert opts["control_mode"] == "direct" and "direct_trial" not in opts
    t = opts["direct_target"]
    assert t == {"profile_id": "goodwe-et", "transport": "goodwe_udp", "host": HOST, "port": 8899, "unit_id": 247,
                 "device_fp": FP, "unreadable": ["soc_max"], "rated_power_w": 10000.0,
                 "capabilities": report().capabilities}


def test_switch_to_direct_restores_entities_first(monkeypatch):
    calls = []

    async def restore():
        calls.append("restore")
        runtime.executor.owned = False                          # powrót doszedł
    runtime = rt(reports=[report()])
    runtime.executor = SimpleNamespace(owned=True, async_restore_now=restore)
    f = flow(options={"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe"},
             runtime=runtime, monkeypatch=monkeypatch)
    r = asyncio.run(f.async_step_control_direct())
    assert calls == ["restore"] and r["data"]["control_mode"] == "direct"


# ── wyszukiwanie i cel ręczny ─────────────────────────────────────────────


def test_direct_search_lists_candidates_without_ip_in_label(monkeypatch):
    runtime = rt()
    f = flow(runtime=runtime, monkeypatch=monkeypatch)
    found = [report(), report(profile_id="deye-sg", host="192.168.77.10", transport="solarman_v5",
                              logger_serial=1234567890)]

    async def search(hass, entry, *, manual=None, **kw):
        return found
    monkeypatch.setattr(cf_mod, "async_direct_search", search)

    async def go():
        r = await f.async_step_details({"direct_search": True})
        assert r["type"] == "progress" and r["progress_action"] == "direct_search"
        await r["task"]
        done = await f.async_step_direct_search()
        assert done == {"type": "progress_done", "next_step_id": "direct_pick"}
        return await f.async_step_direct_pick()
    form = asyncio.run(go())
    assert form["type"] == "form" and form["step_id"] == "direct_pick"
    labels = f._pick_labels()
    assert len(labels) == 3 and "manual" in labels
    text = " ".join(labels.values())
    assert HOST not in text and "192.168.77.10" not in text and "1234567890" not in text
    assert "test only" in labels["1"] and "verified" in labels["0"]
    r = asyncio.run(f.async_step_direct_pick({"candidate": "1"}))
    t = r["data"]["direct_target"]
    assert t["profile_id"] == "deye-sg" and t["logger_serial"] == 1234567890 and t["host"] == "192.168.77.10"
    assert "control_mode" not in r["data"]                      # sam cel nie zmienia sposobu sterowania


@pytest.mark.parametrize("host", ["inverter.local", "8.8.8.8", "192.0.2.1", "255.255.255.255", "127.0.0.1"])
def test_direct_manual_rejects_hostname_public_ip(monkeypatch, host):
    f = flow(runtime=rt(), monkeypatch=monkeypatch)
    called = []

    async def search(*a, **k):
        called.append(1)
        return []
    monkeypatch.setattr(cf_mod, "async_direct_search", search)
    r = asyncio.run(f.async_step_direct_manual({"host": host, "transport": "goodwe_udp", "port": 8899,
                                                 "unit_id": 247}))
    assert r["type"] == "form" and r["errors"] == {"host": "invalid_host"} and called == []


def test_solarman_requires_logger_serial(monkeypatch):
    f = flow(runtime=rt(), monkeypatch=monkeypatch)
    for serial in (None, "", "12345678901", "12ab"):
        data = {"host": HOST, "transport": "solarman_v5", "port": 8899, "unit_id": 1}
        if serial is not None:
            data["logger_serial"] = serial
        r = asyncio.run(f.async_step_direct_manual(data))
        assert r["errors"] == {"logger_serial": "logger_serial_required"}


def test_manual_target_gets_device_fp_from_probe(monkeypatch):
    f = flow(runtime=rt(), monkeypatch=monkeypatch)
    seen = []

    async def search(hass, entry, *, manual=None, port=None, unit_id=None, **kw):
        seen.append((manual, port, unit_id))
        return [report()] if manual.transports == ("modbus_tcp",) else []
    monkeypatch.setattr(cf_mod, "async_direct_search", search)
    r = asyncio.run(f.async_step_direct_manual({"host": HOST, "transport": "modbus_tcp", "port": 1502,
                                                 "unit_id": 3}))
    t = r["data"]["direct_target"]
    assert t["device_fp"] == FP and t["port"] == 1502 and t["unit_id"] == 3 and t["host"] == HOST
    assert seen[0][0].host == HOST and seen[0][1:] == (1502, 3)
    r = asyncio.run(f.async_step_direct_manual({"host": HOST, "transport": "goodwe_udp", "port": 8899,
                                                 "unit_id": 247}))
    assert r["type"] == "form" and r["errors"] == {"base": "direct_not_found"}


def _capture_forms(monkeypatch):
    shown = []

    def show(self, *, step_id, data_schema=None, errors=None, description_placeholders=None, **_):
        shown.append({"step_id": step_id, "errors": errors or {}, "placeholders": description_placeholders})
        return {"type": "form", "step_id": step_id, "errors": errors or {}}
    monkeypatch.setattr(VolcastOptionsFlow, "async_show_form", show, raising=False)
    return shown


def _goodwe_entry(host=HOST):
    return SimpleNamespace(domain="goodwe", entry_id="g", data={"host": host}, options={}, disabled_by=None)


def test_manual_address_used_by_other_integration_names_it(monkeypatch):
    f = flow(runtime=rt(), entries=[_goodwe_entry()], monkeypatch=monkeypatch)
    shown = _capture_forms(monkeypatch)

    async def search(hass, entry, *, manual=None, **kw):
        return [report(identity=False, errors=("conflict",), host=manual.host)]
    monkeypatch.setattr(cf_mod, "async_direct_search", search)
    asyncio.run(f.async_step_direct_manual({"host": HOST, "transport": "goodwe_udp", "port": 8899,
                                             "unit_id": 247}))
    assert shown[-1] == {"step_id": "direct_manual", "errors": {"base": "direct_conflict"},
                         "placeholders": {"integration": "goodwe"}}


def test_manual_without_conflict_keeps_not_found(monkeypatch):
    f = flow(runtime=rt(), monkeypatch=monkeypatch)
    shown = _capture_forms(monkeypatch)

    async def search(hass, entry, *, manual=None, **kw):
        return [report(identity=False, host=manual.host)]
    monkeypatch.setattr(cf_mod, "async_direct_search", search)
    asyncio.run(f.async_step_direct_manual({"host": HOST, "transport": "goodwe_udp", "port": 8899,
                                             "unit_id": 247}))
    assert shown[-1]["errors"] == {"base": "direct_not_found"} and not shown[-1]["placeholders"]


def test_pick_with_only_conflicting_candidate_names_the_integration(monkeypatch):
    f = flow(runtime=rt(), entries=[_goodwe_entry()], monkeypatch=monkeypatch)
    shown = _capture_forms(monkeypatch)
    f._reports = [report(identity=False, errors=("conflict",))]
    asyncio.run(f.async_step_direct_pick())
    assert shown[-1] == {"step_id": "direct_pick", "errors": {"base": "direct_conflict"},
                         "placeholders": {"integration": "goodwe"}}
    f._reports = []
    asyncio.run(f.async_step_direct_pick())
    assert shown[-1]["errors"] == {"base": "direct_not_found"}


@pytest.mark.parametrize("step", ["manual", "pick"])
def test_address_of_other_volcast_entry_is_in_use_not_conflict(monkeypatch, step):
    # Inny wpis Volcast z żywym połączeniem na tym adresie: „już podłączony”, nie „wyłącz integrację”.
    f = flow(runtime=rt(), monkeypatch=monkeypatch)
    f.hass.data[DOMAIN]["direct_hosts"] = {HOST: SimpleNamespace(_entry=SimpleNamespace(entry_id="other"))}
    shown = _capture_forms(monkeypatch)
    conflict = [report(identity=False, errors=("conflict",))]
    if step == "manual":
        async def search(hass, entry, *, manual=None, **kw):
            return conflict
        monkeypatch.setattr(cf_mod, "async_direct_search", search)
        asyncio.run(f.async_step_direct_manual({"host": HOST, "transport": "goodwe_udp", "port": 8899,
                                                 "unit_id": 247}))
    else:
        f._reports = conflict
        asyncio.run(f.async_step_direct_pick())
    assert shown[-1]["errors"] == {"base": "direct_in_use"} and not shown[-1]["placeholders"]


def test_clash_label():
    assert ds_mod.clash_label(("goodwe",)) == "goodwe"
    assert ds_mod.clash_label(("goodwe", "solarman")) == "goodwe, solarman"
    assert ds_mod.clash_label(("goodwe", "volcast")) == "goodwe"          # Volcast nie jest „inną integracją”
    assert ds_mod.clash_label(("unknown",)) == ds_mod.clash_label(()) == "unknown"


# ── połączenie próbne ─────────────────────────────────────────────────────


def _draft_target():
    return {"profile_id": "deye-sg", "transport": "modbus_rtu", "host": HOST, "port": 8899, "unit_id": 1,
            "device_fp": FP}


def test_trial_toggle_only_for_draft_profiles(monkeypatch):
    keys = lambda f: {getattr(k, "schema", k) for k in f._details_schema().schema}  # noqa: E731
    assert "direct_trial" not in keys(flow(monkeypatch=monkeypatch))
    assert "direct_trial" in keys(flow(options={"direct_target": _draft_target()}, profiles=(GW, DEYE),
                                       monkeypatch=monkeypatch))
    assert "direct_trial" not in keys(flow(options={"direct_target": {**_draft_target(), "profile_id": "goodwe-et"}},
                                           profiles=(GW_V,), monkeypatch=monkeypatch))
    assert "direct_search" in keys(flow(monkeypatch=monkeypatch))


def test_trial_saved_and_poll_interval(monkeypatch):
    f = flow(options={"direct_target": _draft_target()}, runtime=rt(), monkeypatch=monkeypatch)
    r = asyncio.run(f.async_step_details({"direct_trial": True, "direct_poll_s": 15}))
    assert r["data"]["direct_trial"] is True and r["data"]["direct_poll_s"] == 15
    r = asyncio.run(f.async_step_details({"direct_trial": False}))
    assert "direct_trial" not in r["data"]


def test_trial_with_entities_rejected(monkeypatch):
    f = flow(options={"control_mode": "entities", "direct_target": _draft_target()}, runtime=rt(),
             monkeypatch=monkeypatch)
    r = asyncio.run(f.async_step_details({"direct_trial": True}))
    assert r["type"] == "form" and r["errors"] == {"direct_trial": "trial_with_entities"}


def test_trial_while_owned_rejected(monkeypatch):
    f = flow(options={"direct_target": _draft_target()}, runtime=rt(owned=True), monkeypatch=monkeypatch)
    r = asyncio.run(f.async_step_details({"direct_trial": True}))
    assert r["type"] == "form" and r["errors"] == {"direct_trial": "trial_while_owned"}


def test_direct_option_keys_are_control_changes():
    assert rt_mod.control_options_changed({}, {"direct_target": _draft_target()})
    assert rt_mod.control_options_changed({}, {"direct_trial": True})


def test_strings_have_direct_steps_errors_and_aborts():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "custom_components" / "volcast"
    texts = [json.loads((root / n).read_text(encoding="utf-8")) for n in ("strings.json", "translations/en.json")]
    assert texts[0] == texts[1]
    opts = texts[0]["options"]
    assert {"direct_pick", "direct_manual"} <= set(opts["step"])
    assert "direct_search" in opts["progress"]
    assert {"direct_unverified", "direct_conflict", "direct_not_found", "direct_in_use"} <= set(opts["abort"])
    assert "{integration}" in opts["abort"]["direct_conflict"]
    assert {"trial_with_entities", "trial_while_owned", "invalid_host", "logger_serial_required",
            "direct_not_found", "direct_conflict", "direct_in_use"} <= set(opts["error"])
    assert "{integration}" in opts["error"]["direct_conflict"]
    assert {"direct_search", "direct_trial", "direct_poll_s"} <= set(opts["step"]["details"]["data"])


def test_refreshed_probe_details_are_not_a_control_change():
    t = _draft_target()
    assert not rt_mod.control_options_changed({"direct_target": {**t, "capabilities": {"tou": True}}},
                                              {"direct_target": {**t, "capabilities": {"tou": False},
                                                                 "unreadable": ["tou"], "rated_power_w": 8000.0}})
    assert rt_mod.control_options_changed({"direct_target": t}, {"direct_target": {**t, "host": "192.168.77.8"}})
    assert rt_mod.control_options_changed({"direct_target": t}, {"direct_target": {**t, "device_fp": "f" * 16}})


def test_unpaired_entry_options_unchanged():
    f = base.flow(paired=False)
    r = asyncio.run(f.async_step_init())
    assert (r["type"], r["step_id"]) == ("form", "init")
    assert "direct_search" not in {getattr(k, "schema", k) for k in f._forecast_schema().schema}
