"""Rekomendacja ścieżki w runtime wpisu: po rozpoznaniu i wyszukiwaniu, w stanie wpisu, z sygnałem."""
import asyncio
from types import SimpleNamespace

import pytest

from custom_components.volcast.const import SIGNAL_CONTROL_STATE_UPDATED, SIGNAL_DISCOVERY_UPDATED
from custom_components.volcast.control import direct_search as ds
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.core.control.recommend import DIRECT, ENTITIES, UNSUPPORTED, Recommendation
from custom_components.volcast.core.discovery.identify import Candidate, Identity
from custom_components.volcast.core.discovery.probe import ProbeReport
from custom_components.volcast.core.profile import load_builtin
from tests.control.test_runtime import _entry, _patch, _setup_hass

GOODWE = load_builtin("goodwe-et")


def _probe():
    ident = Identity("goodwe-et", "goodwe_udp", 8899, 247, "GW8KN-ET", 8000.0, device_fp="fp")
    return ProbeReport(ident, {"mode": True}, (), True, None, "verified", 3, (),
                       candidate=Candidate(host="inverter.lan", source="udp_48899"))


def _rt(report=None, probe=()):
    rt = rt_mod.ControlRuntime(executor=None, fetcher=None, telemetry=None, cloud=None, choice=None, mapped={},
                               rated_power_w=None, report=(lambda: report))
    rt.last_probe = list(probe)
    return rt


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(rt_mod, "async_dispatcher_send", lambda hass, signal, *a: out.append(signal))
    monkeypatch.setattr(rt_mod, "_choice_for", lambda hass, entry, profiles: None)
    monkeypatch.setattr(rt_mod, "map_entities", lambda hass, choice: {})

    async def no_clash(hass, entry_id, host, **_kw):
        return ()
    monkeypatch.setattr(ds, "async_clash", no_clash)
    return out


def test_recommendation_from_last_probe_is_kept_and_signalled(sent):
    hass, entry = SimpleNamespace(data={}), SimpleNamespace(entry_id="e1", options={})
    rt = _rt({"inverters": []}, [_probe()])
    rec = asyncio.run(rt_mod.async_update_recommendation(hass, entry, rt, [GOODWE]))
    assert rt.recommendation is rec and rec.path == DIRECT
    assert sent == [SIGNAL_CONTROL_STATE_UPDATED.format(entry_id="e1")]
    # to samo jeszcze raz = bez sygnału (zmiana stanu, nie każde przeliczenie)
    asyncio.run(rt_mod.async_update_recommendation(hass, entry, rt, [GOODWE]))
    assert len(sent) == 1


def test_entities_recommendation_uses_the_runtime_choice_and_map(sent, monkeypatch):
    from custom_components.volcast.core.control.select import ProfileChoice
    choice = ProfileChoice(GOODWE, "goodwe", "GW8KN-ET")
    monkeypatch.setattr(rt_mod, "_choice_for", lambda hass, entry, profiles: choice)
    monkeypatch.setattr(rt_mod, "map_entities", lambda hass, choice: {"mode": "select.gw_mode"})
    rt = _rt(None)
    rec = asyncio.run(rt_mod.async_update_recommendation(SimpleNamespace(data={}),
                                                         SimpleNamespace(entry_id="e1", options={}), rt, [GOODWE]))
    assert rec.path == ENTITIES and rec.entity_map == (("mode", "select.gw_mode"),)


def test_failure_keeps_the_previous_recommendation_and_never_raises(sent, monkeypatch):
    def boom(hass, entry, profiles):
        raise RuntimeError("registry")
    monkeypatch.setattr(rt_mod, "_choice_for", boom)
    rt = _rt(None)
    old = Recommendation(UNSUPPORTED, "no_profile", 1)
    rt.recommendation = old
    assert asyncio.run(rt_mod.async_update_recommendation(SimpleNamespace(data={}),
                                                          SimpleNamespace(entry_id="e1", options={}), rt, [])) is None
    assert rt.recommendation is old and sent == []


def test_direct_search_recomputes_the_recommendation(sent, monkeypatch):
    scheduled = []
    rt = _rt({"inverters": []})

    async def search(hass, entry, profiles, **kw):
        return [_probe()]
    monkeypatch.setattr(ds, "async_search", search)
    monkeypatch.setattr(ds, "load_profiles", lambda: [GOODWE])

    class Hass:
        data = {"volcast": {"e1": {"control": rt}}}

        async def async_add_executor_job(self, fn, *a):
            return fn(*a)

        def async_create_background_task(self, coro, name, **_kw):
            scheduled.append(name)
            return asyncio.get_running_loop().create_task(coro)

    async def run():
        reports = await rt_mod.async_direct_search(Hass(), SimpleNamespace(entry_id="e1", options={}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return reports

    reports = asyncio.run(run())
    assert rt.last_probe == reports and scheduled == ["volcast_recommendation"]
    assert rt.recommendation is not None and rt.recommendation.path == DIRECT


@pytest.mark.asyncio
async def test_setup_recomputes_after_discovery_updates(monkeypatch):
    from tests.setup_harness import drain
    connected = {}

    def connect(hass, signal, target):
        connected[signal] = target
        return lambda: connected.pop(signal, None)
    monkeypatch.setattr(rt_mod, "async_dispatcher_connect", connect)
    _patch(monkeypatch)
    hass, entry = _setup_hass(), _entry()
    report = {"inverters": []}
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: report)
    await drain(hass)
    signal = SIGNAL_DISCOVERY_UPDATED.format(entry_id=entry.entry_id)
    assert rt.report() is report and signal in connected
    connected[signal]()
    await drain(hass)
    assert "hass_background:volcast_recommendation" in hass.events
    assert rt.recommendation is not None and rt.recommendation.path == UNSUPPORTED
    await rt_mod.async_unload_control(hass, rt)
    assert signal not in connected


def test_recommendation_text_for_the_control_mode_step():
    rec = Recommendation(ENTITIES, "integration_writes", 1, integration={"domain": "huawei_solar"})
    assert ds.recommendation_text(rec) == "recommended: entities (huawei_solar)"
    assert ds.recommendation_text(Recommendation(UNSUPPORTED, "no_profile", 1)) == "recommended: unsupported"
    long = Recommendation(DIRECT, "identified", 1, device={"manufacturer": "X", "model": "M" * 300})
    assert len(ds.recommendation_text(long)) <= 200
