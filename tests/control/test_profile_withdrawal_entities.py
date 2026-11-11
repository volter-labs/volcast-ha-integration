"""Restart na ścieżce encji z przełącznikiem zdalnego sterowania: wykonawca przejął falownik profilem z feedu
z prawem zapisu, integracja się zrestartowała, a feed nie daje już tej treści. Zapisana kopia profilu jest
sprawdzana od nowa przy starcie; wykonawca tylko oddaje falownik (tryb, nastawy, przełącznik wyłączony)."""
from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace

from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.executor import VolcastExecutor
from custom_components.volcast.control.ha_writer import EntityServiceWriter
from custom_components.volcast.control.store import ControlStore
from custom_components.volcast.core import endorsement as en
from custom_components.volcast.core.profile import ORIGIN_FEED_ENDORSED, profile_from_dict

from . import test_executor as base
from . import test_executor_handback as hb
from .test_executor_direct import _thaw
from .test_profile_withdrawal import BASELINE, VECTORS, _sign, state

PROFILE_ID = "acme-entities"


def _signed_raw(monkeypatch) -> dict:
    pair = VECTORS["test_keys"][0]
    monkeypatch.setattr(en, "PROFILE_ENDORSEMENT_PUBLIC_KEYS", MappingProxyType({pair["key_id"]: pair["public_hex"]}))
    raw = _thaw(hb.profile().raw)
    raw["id"] = PROFILE_ID
    return _sign(raw)


def _restart(h, store, monkeypatch):
    """Złożenie wykonawcy po restarcie tak jak w runtime: kopia z rekordu własności, feed bez profili."""
    monkeypatch.setattr(rt_mod, "mapped_with_overrides", lambda *_: dict(hb.ENT))
    monkeypatch.setattr(rt_mod, "mode_unique_id", lambda *_: None)
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"})
    composed = asyncio.run(rt_mod._async_compose(h, entry, [], store))
    return entry, composed


def test_restart_on_the_entity_path_hands_the_inverter_back_with_the_saved_copy(monkeypatch):
    raw = _signed_raw(monkeypatch)
    prof = profile_from_dict(raw, origin=ORIGIN_FEED_ENDORSED)
    h = hb.dhass()
    store = ControlStore(h, "e1")
    h, ex = hb.make(h, store=store, prof=prof, monkeypatch=monkeypatch)
    hb.take_over(h, ex)
    assert ex.owned and state(h)[:2] == ("sell_power", "on")
    saved = store._store._data
    assert saved["owned"] and saved["trusted_profile"]["id"] == PROFILE_ID
    # awaria bez oddania: nowy wykonawca z tym samym magazynem i stanem encji

    entry, composed = _restart(h, store, monkeypatch)
    assert composed.withdrawn and composed.returning
    assert composed.choice.profile.origin == ORIGIN_FEED_ENDORSED and composed.choice.profile.id == PROFILE_ID
    assert composed.io is None and composed.mapped == hb.ENT           # ścieżka encji, nie bezpośrednia
    reloads: list = []
    ex2 = VolcastExecutor(h, entry, choice=composed.choice, mapped=composed.mapped, rated_power_w=8000.0,
                          store=store, writer=EntityServiceWriter(h), clock=base.Clock(), utcnow=hb.Utc(),
                          on_released=lambda: reloads.append(1), withdrawn=composed.withdrawn)
    n = len(h.services.calls)
    asyncio.run(ex2.async_start())
    asyncio.run(ex2.async_tick())
    seq = hb.calls(h, n)
    assert state(h) == BASELINE and not ex2.owned and ex2.withdrawn
    assert ("turn_off", hb.ENT["enable"], None) in seq
    # tylko oddanie: żadnego zapisu planu ani ponownego włączenia przełącznika
    assert not any(c[0] == "turn_on" for c in seq)
    assert not any(c[0] == "select_option" and c[2] == "sell_power" for c in seq)
    n = len(h.services.calls)
    ex2._clock.t += 120
    asyncio.run(ex2.async_tick())
    assert hb.calls(h, n) == [] and not ex2.owned and "trusted_profile" not in store._store._data


def test_restart_with_a_tampered_saved_copy_is_not_used_for_the_return(monkeypatch):
    raw = _signed_raw(monkeypatch)
    prof = profile_from_dict(raw, origin=ORIGIN_FEED_ENDORSED)
    h = hb.dhass()
    store = ControlStore(h, "e1")
    h, ex = hb.make(h, store=store, prof=prof, monkeypatch=monkeypatch)
    hb.take_over(h, ex)
    assert ex.owned
    store._store._data["trusted_profile"]["write_policy"]["min_interval_s"] += 1      # podpis już nie pasuje
    record = {"profile": PROFILE_ID, "domain": "goodwe"}
    assert rt_mod._hand_back_profile(record, store._store._data["trusted_profile"], []) is None
