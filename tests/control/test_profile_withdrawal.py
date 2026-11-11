"""Profil z feedu z prawem zapisu traci je przy działającym wykonawcy (zniknął z feedu, nie ma już ważnego
certyfikatu albo ma inną treść): od razu koniec zapisów planu i drabiny, powrót do stanu wyjściowego
właściciela (tryb, nastawy, przełącznik zdalnego sterowania) mapą zaufaną przy przejęciu, dopiero potem
przeładowanie wpisu. Po restarcie bez profilu migawka nie przepada bez powrotu: oddaje ją zapisana kopia
zaufanej mapy. Profil z pakietu — bez zmian."""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.device_io import DirectIO
from custom_components.volcast.control.store import ControlStore
from custom_components.volcast.core import endorsement as en
from custom_components.volcast.core import profile_feed as pf
from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.profile import ORIGIN_FEED_ENDORSED, load_builtin
from custom_components.volcast.core.registers import RegisterImage
from tests.sim.fixtures import goodwe_words

from . import test_executor as base
from . import test_executor_handback as hb
from .test_executor import plan
from .test_executor_direct import (  # noqa: F401 — `issues` to fixture
    EXPORT_EN, GW_V, MODE, SALT, Harness, _thaw, gw_raw_word, gw_target, issues)

ENT = hb.ENT
BASELINE = ("auto", "off", "5.0", "6.0")          # tryb, przełącznik, nastawa ładowania, nastawa rozładowania


# ── wykonawca: profil z przełącznikiem zdalnego sterowania (encje) ────────


def endorsed():
    return dataclasses.replace(hb.profile(), origin=ORIGIN_FEED_ENDORSED)


def state(h):
    return tuple(h.states.get(ENT[k]).state for k in ("mode", "enable", "charge_power_w", "discharge_power_w"))


def owned(monkeypatch, h=None):
    h, ex = hb.make(h, prof=endorsed(), monkeypatch=monkeypatch)
    hb.take_over(h, ex)
    assert ex.owned and state(h)[:2] == ("sell_power", "on")
    return h, ex


def test_withdrawal_hands_the_inverter_back_at_once_then_reloads(monkeypatch):
    h, ex = owned(monkeypatch)
    reloads: list = []
    n = len(h.services.calls)
    asyncio.run(ex.async_withdraw(lambda: reloads.append(1)))
    seq = hb.calls(h, n)
    mode, set_point, switch = hb.order(seq, ("select_option", ENT["mode"], "auto"),
                                       ("set_value", ENT["discharge_power_w"], 6.0), ("turn_off", ENT["enable"]))
    assert mode < set_point < switch
    assert state(h) == BASELINE and not ex.owned and ex.withdrawn and reloads == [1]
    # plan, zgoda i przełącznik w HA zostają — a mimo to żadnego zapisu więcej
    n = len(h.services.calls)
    ex._clock.t += 120
    asyncio.run(ex.async_tick())
    assert hb.calls(h, n) == [] and not ex.owned and ex.last_decision.reason == "profile_withdrawn"
    assert ex.verification_can_write() is False
    assert asyncio.run(ex.async_control_write()) is None and hb.calls(h, n) == []
    asyncio.run(ex.async_withdraw(lambda: reloads.append(2)))          # drugie wycofanie: nic
    assert reloads == [1]


def test_withdrawal_during_a_running_cycle_abandons_its_plan_writes_then_restores(monkeypatch):
    h, ex = hb.make(prof=endorsed(), monkeypatch=monkeypatch)
    reloads: list = []
    real = h.services.async_call

    async def go():
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated(domain, service, data, *a, **kw):
            if service == "turn_on" and not release.is_set():
                entered.set()                        # przejęcie: nastawa poszła, przełącznik w toku
                await release.wait()
            return await real(domain, service, data, *a, **kw)
        h.services.async_call = gated
        await base.ready(ex, raw=plan(2500))
        cycle = asyncio.create_task(ex.async_tick())
        await entered.wait()
        withdraw = asyncio.create_task(ex.async_withdraw(lambda: reloads.append(1)))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not withdraw.done() and ex.withdrawn        # powrót czeka na blokadę cyklu
        release.set()
        await cycle
        await withdraw
    asyncio.run(go())
    seq = hb.calls(h)
    assert ("select_option", ENT["mode"], "sell_power") not in seq     # reszta zapisów cyklu porzucona
    assert seq.index(("turn_off", ENT["enable"], None)) > seq.index(("turn_on", ENT["enable"], None))
    assert state(h) == BASELINE and not ex.owned and reloads == [1]


def test_failed_return_is_retried_every_cycle_and_the_reload_waits_for_it(monkeypatch):
    h, ex = owned(monkeypatch)
    reloads: list = []
    h.services.fail[ENT["enable"]] = RuntimeError("unavailable")
    asyncio.run(ex.async_withdraw(lambda: reloads.append(1)))
    assert ex.owned and reloads == [] and state(h)[0] == "auto"
    for _ in range(2):                                   # nadal bez przełącznika: tylko ponowny powrót
        n = len(h.services.calls)
        ex._clock.t += 120
        asyncio.run(ex.async_tick())
        assert {c[1] for c in hb.calls(h, n)} <= {ENT["enable"]} and ex.owned and reloads == []
    h.services.fail.clear()
    ex._clock.t += 120
    asyncio.run(ex.async_tick())
    assert state(h) == BASELINE and not ex.owned and reloads == [1]


def test_withdrawal_without_ownership_writes_nothing_and_reloads(monkeypatch):
    h, ex = hb.make(prof=endorsed(), monkeypatch=monkeypatch)
    reloads: list = []

    async def go():
        await base.ready(ex, raw=plan(2500))
        await ex.async_withdraw(lambda: reloads.append(1))
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == [] and reloads == [1] and not ex.owned


def test_endorsed_take_over_stores_the_trusted_map_with_the_snapshot_until_the_return(monkeypatch):
    h, ex = owned(monkeypatch)
    saved = ex._store._store._data
    assert saved["owned"] and saved["trusted_profile"] == json.loads(json.dumps(dict(ex.profile.raw)))
    asyncio.run(ex.async_set_consent(False))
    ex._clock.t += 120
    asyncio.run(ex.async_tick())
    assert not ex.owned and "trusted_profile" not in ex._store._store._data


def test_packaged_take_over_stores_the_same_record_as_before(monkeypatch):
    h, ex = hb.make(monkeypatch=monkeypatch)
    hb.take_over(h, ex)
    assert ex.owned and "trusted_profile" not in ex._store._store._data
    assert ex.withdrawn is False


# ── certyfikowana kopia profilu GoodWe (tryb bezpośredni, symulator) ──────


VECTORS = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "profile_feed"
                      / "endorsement-vectors.json").read_text("utf-8"))


def _sign(raw: dict) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pair = VECTORS["test_keys"][0]
    raw = {k: v for k, v in raw.items() if k != "endorsement"}
    digest = en.endorsement_digest(raw)
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(pair["seed_hex"]))
    sig = base64.b64encode(priv.sign(en.endorsement_message(raw["id"], digest, pair["key_id"]))).decode()
    return {**raw, "endorsement": {"key_id": pair["key_id"], "digest": digest, "signature": sig}}


@pytest.fixture
def acme(monkeypatch):
    """Treść `GW_V` pod id spoza pakietu, z certyfikatem klucza testowego (klucz wydania podmieniony)."""
    pair = VECTORS["test_keys"][0]
    monkeypatch.setattr(en, "PROFILE_ENDORSEMENT_PUBLIC_KEYS",
                        MappingProxyType({pair["key_id"]: pair["public_hex"]}))
    raw = _thaw(GW_V.raw)
    raw["id"] = "acme-gw"
    raw = _sign(raw)
    pf.set_feed_profiles({raw["id"]: raw})
    yield raw
    pf.set_feed_profiles({})


def _acme_target(sim, profile):
    return gw_target(sim, profile_id=profile.id,
                     device_fp=device_fingerprint(SALT, profile, RegisterImage(goodwe_words())))


async def _owned_acme(make_hass, sim, bank, *, store=None):
    profile = load_builtin("acme-gw")
    assert profile.origin == ORIGIN_FEED_ENDORSED
    bank.poke(EXPORT_EN, 1)                                  # ogranicznik eksportu właściciela (plan go nie rusza)
    h = await Harness(make_hass, profile, _acme_target(sim, profile), store=store).start()
    await h.ex.async_tick()
    assert h.ex.owned and gw_raw_word(bank, MODE) == 10
    return h


def _withdrawn(raw):
    return {}


def _demoted(raw):
    changed = _thaw(raw)
    changed["write_policy"]["min_interval_s"] = float(changed["write_policy"]["min_interval_s"]) + 1
    return {raw["id"]: pf._as_draft(changed)}              # jak po przejściu przez feed: certyfikat nie pasuje


def _re_signed(raw):
    changed = _thaw(raw)
    changed["write_policy"]["min_interval_s"] = float(changed["write_policy"]["min_interval_s"]) + 1
    return {raw["id"]: _sign(changed)}                     # nadal z prawem zapisu, ale inna treść


def _cosmetic(raw):
    changed = _thaw(raw)
    changed["label"] = "Other label"
    return {raw["id"]: changed}                            # ten sam digest: certyfikat dalej ważny


REVOKED = [_withdrawn, _demoted, _re_signed]


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", REVOKED, ids=["gone", "no_certificate", "other_content"])
async def test_feed_update_revoking_the_profile_restores_the_owner_state_then_reloads(
        make_hass, goodwe_udp_sim, goodwe_bank, issues, monkeypatch, acme, revoke):
    reloads: list = []
    monkeypatch.setattr(rt_mod, "_schedule_reload", lambda hass, eid: reloads.append(eid))
    h = await _owned_acme(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        pf.set_feed_profiles(revoke(acme))
        assert await rt_mod.async_withdraw_if_revoked(h.hass, h.entry, SimpleNamespace(executor=h.ex)) is True
        assert not h.ex.owned and gw_raw_word(goodwe_bank, MODE) == 1 and gw_raw_word(goodwe_bank, EXPORT_EN) == 1
        assert reloads == ["e1"]
        before = len(goodwe_bank.writes)
        await h.cycle()
        assert len(goodwe_bank.writes) == before and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_feed_update_keeping_the_certified_content_changes_nothing(
        make_hass, goodwe_udp_sim, goodwe_bank, issues, monkeypatch, acme):
    reloads: list = []
    monkeypatch.setattr(rt_mod, "_schedule_reload", lambda hass, eid: reloads.append(eid))
    h = await _owned_acme(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        before = len(goodwe_bank.writes)
        for registry in ({acme["id"]: acme}, _cosmetic(acme)):
            pf.set_feed_profiles(registry)
            assert await rt_mod.async_withdraw_if_revoked(h.hass, h.entry, SimpleNamespace(executor=h.ex)) is False
        assert h.ex.owned and not h.ex.withdrawn and reloads == [] and len(goodwe_bank.writes) == before
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_packaged_profile_is_never_withdrawn_by_a_feed_update(make_hass, goodwe_udp_sim, goodwe_bank, issues,
                                                                    monkeypatch):
    reloads: list = []
    monkeypatch.setattr(rt_mod, "_schedule_reload", lambda hass, eid: reloads.append(eid))
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        await h.ex.async_tick()
        assert h.ex.owned
        pf.set_feed_profiles({})
        assert await rt_mod.async_withdraw_if_revoked(h.hass, h.entry, SimpleNamespace(executor=h.ex)) is False
        assert h.ex.owned and not h.ex.withdrawn and reloads == []
    finally:
        await h.close()


# ── setup: nasłuch feedu i restart bez profilu ────────────────────────────


class FakeFeed:
    """Aktualizator feedu dla runtime: tylko nasłuch aktualizacji (sprawdzenia w sieci nigdy nienależne)."""

    def __init__(self):
        self.listeners: list = []

    def add_update_listener(self, cb):
        self.listeners.append(cb)

        def unsub():
            if cb in self.listeners:
                self.listeners.remove(cb)
        return unsub

    def due(self):
        return False

    def hint(self, _etag):
        return False

    def fire(self):
        for cb in list(self.listeners):
            cb()


def _setup(monkeypatch, store, options, feed):
    from tests.control.test_mode_switch_restore import _setup as setup

    async def profile_feed(_hass):
        return feed
    monkeypatch.setattr(rt_mod, "_async_profile_feed", profile_feed)
    return setup(monkeypatch, store, options)


@pytest.mark.asyncio
async def test_running_endorsed_executor_listens_to_feed_updates_and_is_handed_back(
        make_hass, goodwe_udp_sim, goodwe_bank, issues, monkeypatch, acme):
    from tests.setup_harness import drain
    store = ControlStore(make_hass(), "e1")
    h = await _owned_acme(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    feed = FakeFeed()
    hass, entry, reloads = _setup(monkeypatch, store, dict(h.options), feed)
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    try:
        await drain(hass)
        assert rt.executor.profile.origin == ORIGIN_FEED_ENDORSED and len(feed.listeners) == 1
        feed.fire()                                        # feed bez tej pozycji
        pf.set_feed_profiles({})
        feed.fire()
        await drain(hass)
        assert rt.executor.withdrawn and not rt.executor.owned and gw_raw_word(goodwe_bank, MODE) == 1
        assert reloads == ["e1"]
    finally:
        await rt_mod.async_unload_control(hass, rt)
    assert feed.listeners == []                            # rozładunek zdejmuje nasłuch


@pytest.mark.asyncio
async def test_packaged_executor_does_not_listen_to_feed_updates(make_hass, goodwe_udp_sim, goodwe_bank, issues,
                                                                 monkeypatch):
    from tests.control.test_executor_direct import _owned_sell
    from tests.setup_harness import drain
    store = ControlStore(make_hass(), "e1")
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    feed = FakeFeed()
    hass, entry, reloads = _setup(monkeypatch, store, dict(h.options), feed)
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    try:
        await drain(hass)
        assert feed.listeners == [] and not rt.executor.withdrawn and reloads == []
    finally:
        await rt_mod.async_unload_control(hass, rt)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", REVOKED, ids=["gone", "no_certificate", "other_content"])
async def test_restart_without_the_profile_returns_the_saved_state_with_the_trusted_map(
        make_hass, goodwe_udp_sim, goodwe_bank, issues, monkeypatch, acme, revoke):
    # Awaria w trakcie sterowania: brak oddania; po restarcie feed nie daje już tej treści z prawem zapisu.
    from tests.setup_harness import drain
    store = ControlStore(make_hass(), "e1")
    h = await _owned_acme(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    pf.set_feed_profiles(revoke(acme))
    hass, entry, reloads = _setup(monkeypatch, store, dict(h.options), FakeFeed())
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    try:
        await drain(hass)
        assert isinstance(rt.executor.io, DirectIO) and rt.executor.withdrawn and rt.verification is None
        await rt.executor.async_tick()
        assert not rt.executor.owned and gw_raw_word(goodwe_bank, MODE) == 1
        assert gw_raw_word(goodwe_bank, EXPORT_EN) == 1
        assert "control_record_dropped" not in issues.keys() and reloads == ["e1"]
    finally:
        await rt_mod.async_unload_control(hass, rt)


@pytest.mark.asyncio
async def test_restart_with_a_tampered_trusted_copy_drops_the_record_with_a_repair(
        make_hass, goodwe_udp_sim, goodwe_bank, issues, monkeypatch, acme):
    from tests.setup_harness import drain
    store = ControlStore(make_hass(), "e1")
    h = await _owned_acme(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    store._store._data["trusted_profile"]["write_policy"]["min_interval_s"] += 1     # podpis już nie pasuje
    pf.set_feed_profiles({})
    hass, entry, reloads = _setup(monkeypatch, store, dict(h.options), FakeFeed())
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    try:
        await drain(hass)
        assert not rt.executor.owned and not rt.executor.withdrawn
        assert "control_record_dropped" in issues.keys() and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await rt_mod.async_unload_control(hass, rt)


@pytest.mark.asyncio
async def test_restart_with_the_same_certified_profile_resumes_control(
        make_hass, goodwe_udp_sim, goodwe_bank, issues, monkeypatch, acme):
    from tests.setup_harness import drain
    store = ControlStore(make_hass(), "e1")
    h = await _owned_acme(make_hass, goodwe_udp_sim, goodwe_bank, store=store)
    await h.close()
    hass, entry, reloads = _setup(monkeypatch, store, dict(h.options), FakeFeed())
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    try:
        await drain(hass)
        assert rt.executor.owned and not rt.executor.withdrawn and reloads == []
        assert "control_record_dropped" not in issues.keys()
    finally:
        await rt_mod.async_unload_control(hass, rt)
