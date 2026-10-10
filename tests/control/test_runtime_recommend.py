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


def _probe(fp="fp", model="GW8KN-ET"):
    ident = Identity("goodwe-et", "goodwe_udp", 8899, 247, model, 8000.0, device_fp=fp)
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


def test_direct_search_recomputes_the_recommendation_in_an_entry_task(sent, monkeypatch):
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

    class Entry:
        entry_id, options = "e1", {}

        def async_create_background_task(self, hass, coro, name, /):
            # zadanie WPISU (unload je anuluje), nie zadanie hass
            scheduled.append(name)
            return asyncio.get_running_loop().create_task(coro)

    async def run():
        reports = await rt_mod.async_direct_search(Hass(), Entry())
        for _ in range(3):
            await asyncio.sleep(0)
        return reports

    reports = asyncio.run(run())
    assert rt.last_probe == reports and scheduled == ["volcast_recommendation"]
    assert rt.recommendation is not None and rt.recommendation.path == DIRECT


def test_origin_comes_from_the_ha_loader(sent, monkeypatch):
    from custom_components.volcast.core.control.select import ProfileChoice
    choice = ProfileChoice(GOODWE, "goodwe", "GW8KN-ET")
    monkeypatch.setattr(rt_mod, "_choice_for", lambda hass, entry, profiles: choice)
    monkeypatch.setattr(rt_mod, "map_entities", lambda hass, choice: {"mode": "select.gw_mode"})
    built_in = {"goodwe": True}

    async def get_integration(hass, domain):
        if domain not in built_in:
            raise LookupError(domain)
        return SimpleNamespace(is_built_in=built_in[domain])
    monkeypatch.setattr(rt_mod, "async_get_integration", get_integration)
    entry = SimpleNamespace(entry_id="e1", options={})
    rec = asyncio.run(rt_mod.async_update_recommendation(SimpleNamespace(data={}), entry, _rt(None), [GOODWE]))
    assert rec.to_payload()["integration"]["origin"] == "core"
    built_in["goodwe"] = False
    rec = asyncio.run(rt_mod.async_update_recommendation(SimpleNamespace(data={}), entry, _rt(None), [GOODWE]))
    assert rec.to_payload()["integration"]["origin"] == "custom"
    built_in.clear()                     # loader nie zna domeny → bez pochodzenia, rekomendacja zostaje
    rec = asyncio.run(rt_mod.async_update_recommendation(SimpleNamespace(data={}), entry, _rt(None), [GOODWE]))
    assert "origin" not in rec.to_payload()["integration"] and rec.path == ENTITIES


def test_probe_of_the_configured_target_is_preferred(sent):
    other, mine = _probe(fp="aaaa"), _probe(fp="bbbb", model="GW10K-ET")
    entry = SimpleNamespace(entry_id="e1", options={"direct_target": {"device_fp": "bbbb"}})
    rec = asyncio.run(rt_mod.async_update_recommendation(SimpleNamespace(data={}), entry,
                                                         _rt({"inverters": []}, [other, mine]), [GOODWE]))
    assert rec.device["model"] == "GW10K-ET"


def test_older_overlapping_recompute_does_not_overwrite_a_newer_one(sent, monkeypatch):
    async def run():
        release = asyncio.Event()
        calls = []

        async def slow_clash(hass, entry_id, host, **_kw):
            calls.append(host)
            if len(calls) == 1:
                await release.wait()          # pierwsze przeliczenie utknęło na sprawdzeniu kolizji
            return ()
        monkeypatch.setattr(ds, "async_clash", slow_clash)
        rt = _rt({"inverters": []}, [_probe()])
        entry = SimpleNamespace(entry_id="e1", options={})
        first = asyncio.ensure_future(rt_mod.async_update_recommendation(SimpleNamespace(data={}), entry, rt,
                                                                         [GOODWE]))
        await asyncio.sleep(0)
        rt.last_probe = []                    # nowsze dane: brak falownika
        newer = await rt_mod.async_update_recommendation(SimpleNamespace(data={}), entry, rt, [GOODWE])
        release.set()
        assert await first is None            # starszy wynik odrzucony
        return rt, newer

    rt, newer = asyncio.run(run())
    assert rt.recommendation is newer and newer.path == UNSUPPORTED


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
    assert "background:volcast_recommendation" in hass.events
    assert rt.recommendation is not None and rt.recommendation.path == UNSUPPORTED
    await rt_mod.async_unload_control(hass, rt)
    assert signal not in connected


def test_recommendation_text_for_the_control_mode_step():
    rec = Recommendation(ENTITIES, "integration_writes", 1, integration={"domain": "huawei_solar"})
    assert ds.recommendation_text(rec) == "recommended: entities (huawei_solar)"
    assert ds.recommendation_text(Recommendation(UNSUPPORTED, "no_profile", 1)) == "recommended: unsupported"
    long = Recommendation(DIRECT, "identified", 1, device={"manufacturer": "X", "model": "M" * 300})
    assert len(ds.recommendation_text(long)) <= 200


# ── wpis już w trybie bezpośrednim: tożsamość z celu w opcjach, bez sondy ──

DIRECT_OPTIONS = {"control_mode": "direct",
                  "direct_target": {"profile_id": "goodwe-et", "transport": "goodwe_udp", "host": "inverter.lan",
                                    "port": 8899, "unit_id": 247, "device_fp": "abcd"}}
BOX_REPORT = {"inverters": [{"domain": "goodwe", "host": "box.lan", "matched_by": "domain",
                             "devices": [{"manufacturer": "GoodWe", "model": "GW-HUB"}]}]}


def test_configured_direct_entry_recommends_direct_without_a_probe(sent, monkeypatch):
    hosts = []

    async def clash(hass, entry_id, host, **_kw):
        hosts.append(host)
        return ()
    monkeypatch.setattr(ds, "async_clash", clash)
    entry = SimpleNamespace(entry_id="e1", options=DIRECT_OPTIONS)
    rt = _rt(BOX_REPORT)                     # po starcie: `last_probe` pusty, cel znany z opcji
    rec = asyncio.run(rt_mod.async_update_recommendation(SimpleNamespace(data={}), entry, rt, [GOODWE]))
    payload = rec.to_payload()
    assert (payload["path"], payload["reason"], payload["ladder_start"]) == (DIRECT, "no_integration_identify_ok", 4)
    assert "integration" not in payload and hosts == ["inverter.lan"]
    assert "inverter.lan" not in str(payload)


def test_identity_mismatch_of_the_running_connection_is_not_a_known_inverter(sent):
    entry = SimpleNamespace(entry_id="e1", options=DIRECT_OPTIONS)
    rt = _rt({"inverters": []})
    rt.direct = SimpleNamespace(identity="mismatch")
    rec = asyncio.run(rt_mod.async_update_recommendation(SimpleNamespace(data={}), entry, rt, [GOODWE]))
    assert rec.path == UNSUPPORTED


def test_direct_start_triggers_a_recompute():
    calls = []

    class Conn:
        allow_conflicted_restore = False

        async def async_start(self):
            calls.append("start")

        def refused(self):
            return None

        async def async_poll(self):
            calls.append("poll")

    class Exec:
        owned = False

        async def async_tick(self):
            calls.append("tick")

    asyncio.run(rt_mod._async_start_direct(Conn(), Exec(), on_started=lambda: calls.append("recommend")))
    assert calls == ["start", "poll", "tick", "recommend"]
