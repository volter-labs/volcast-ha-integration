"""Profil „tylko encje” jako wybrany profil wykonawcy: zero zapisów na falownik — także gdy wpis ma jeszcze
opcje po wcześniejszym profilu (tryb encji z mapą encji zapisu albo cel bezpośredni), zgoda konta i
przełącznik lokalny są włączone, a magazyn trzyma własność z migawką. Bez podmiany `control_verified`:
bariera ma działać sama (`writes_allowed`)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.volcast.control.executor import VolcastExecutor
from custom_components.volcast.control.ha_writer import EntityServiceWriter
from custom_components.volcast.control.store import ControlState, ControlStore
from custom_components.volcast.core.control.cycle import ERROR, WRITE
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import ORIGIN_PACKAGE, profile_from_dict
from tests.control.test_executor_direct import Harness, gw_target, issues  # noqa: F401 — `issues` to fixture
from tests.core.profile_fixtures import eo_profile

from . import test_executor as base
from . import test_executor_handback as hb

DOMAIN = "test_entities"
# Charge-grid slot w planie: gdyby bariera puściła, wykonawca pisałby tryb i moc.
CHARGE = [{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "mode": "charge", "power_w": 3000,
           "charge_source": "grid", "price_pln_kwh": 0.2}]


def _eo():
    """Najgorszy przypadek: profil i wpis integracji `verified`, pochodzenie z pakietu."""
    raw = eo_profile()
    raw["status"] = "verified"
    raw["ha"]["integrations"][0]["status"] = "verified"
    return profile_from_dict(raw, origin=ORIGIN_PACKAGE)


class SpyWriter:
    """Pisarz encji, który liczy każde wywołanie (i przekazuje dalej, gdyby jednak przyszło)."""

    def __init__(self, hass):
        self.inner, self.writes = EntityServiceWriter(hass), []

    async def async_write(self, w):
        self.writes.append(w)
        return await self.inner.async_write(w)


def _owned_store(h) -> ControlStore:
    store = ControlStore(h, "e1")
    asyncio.run(store.async_save(ControlState(
        consent=True, local_switch=True, owned=True,
        snapshot={"mode": "auto", "charge_power_w": 5000.0, "discharge_power_w": 6000.0, "enable": 0.0,
                  "soc_min": 15.0},
        owner={"profile": "test-eo", "domain": DOMAIN, "mode_entity": hb.ENT["mode"]},
        restore_keys=["soc_min", "charge_power_w", "discharge_power_w", "enable", "mode"])))
    return store


def test_stale_entity_mode_with_consent_and_owned_state_never_writes():
    h = hb.dhass(mode="charge_battery", enable="on")      # encje zapisu po poprzednim profilu nadal istnieją
    store = _owned_store(h)
    writer = SpyWriter(h)
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"})
    ex = VolcastExecutor(h, entry, choice=ProfileChoice(_eo(), DOMAIN, "X"), mapped=dict(hb.ENT),
                         rated_power_w=8000.0, store=store, writer=writer, clock=base.Clock(), utcnow=hb.Utc())

    async def go():
        await base.ready(ex, raw=base.plan(slots=CHARGE))
        await ex.async_tick()
        ex._clock.t += 120
        await ex.async_tick()
    asyncio.run(go())

    assert writer.writes == [] and h.services.calls == []
    assert ex.last_decision is None or ex.last_decision.status not in (WRITE, ERROR)
    assert ex.verification_can_write() is False
    assert not ex.owned and not store._store._data.get("owned")     # rekord własności porzucony
    assert not store._store._data.get("snapshot")


@pytest.mark.asyncio
async def test_stale_direct_target_with_consent_never_sends_a_write(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await Harness(make_hass, _eo(), gw_target(goodwe_udp_sim, profile_id="test-eo")).start()
    try:
        await h.ex.async_tick()
        await h.cycle()
        await h.cycle()
        assert goodwe_bank.writes == []
        assert h.conn.refused() == "bad_target" and h.conn.client is None    # bez łącza, bez ramek
        assert h.ex.last_decision is None or h.ex.last_decision.status != WRITE
        assert h.ex.verification_can_write() is False and not h.ex.owned
    finally:
        await h.close()
