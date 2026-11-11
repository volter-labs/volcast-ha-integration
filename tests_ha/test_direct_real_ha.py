"""Połączenie bezpośrednie na prawdziwym rdzeniu: zegar odpytywania biegnie na pętli zdarzeń.

Transport i klient rejestrów są atrapami (żadnego gniazda); sprawdzamy, że prawdziwe
`async_track_time_interval` naprawdę wywołuje odczyty i że nic nie trafia do wątku roboczego.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.volcast.control import direct as direct_mod
from custom_components.volcast.control.direct import DirectConnection
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.transports.base import TransportStats

from .conftest import make_entry

FP = "0123456789abcdef"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Transport:
    def __init__(self) -> None:
        self.stats = TransportStats()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Client:
    instances: list["_Client"] = []

    def __init__(self, transport, profile, **kw) -> None:
        self.transport = transport
        self.reads = 0
        self.identity_reads = 0
        _Client.instances.append(self)

    async def read_identity(self) -> str:
        self.identity_reads += 1
        return FP

    async def read_state(self):
        self.reads += 1
        return SimpleNamespace(values={"soc": 50.0}, at_mono=0.0)


async def test_timer_polls_on_the_event_loop(hass: HomeAssistant, monkeypatch, caplog):
    monkeypatch.setattr(direct_mod, "RegisterClient", _Client)
    _Client.instances.clear()
    entry = make_entry(hass)
    clock = _Clock()
    target = {"profile_id": "goodwe-et", "transport": "goodwe_udp", "host": "127.0.0.1", "port": 8899,
              "unit_id": 247, "device_fp": FP}
    conn = DirectConnection(hass, entry, load_builtin("goodwe-et"), target, trial=False, salt=bytes(16),
                            poll_s=10.0, transport_factory=lambda cfg, **kw: _Transport(), clock=clock,
                            allow_loopback=True, unreadable={"soc_max"})
    polled = []
    conn.add_listener(lambda: polled.append(1))
    with caplog.at_level(logging.WARNING):
        await conn.async_start()
        assert conn.refused() is None and conn.identity == "confirmed"
        client = _Client.instances[-1]
        for step in (11, 22):
            clock.t += 11.0
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=step))
            await hass.async_block_till_done()
        assert client.reads >= 2 and polled
        await conn.async_stop()
    assert "thread other than the event loop" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    reads = client.reads
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()
    assert client.reads == reads and client.transport.closed


# ── przełącznik zdalnego sterowania i nastawy u32 na drugim adresie urządzenia (symulator Modbus TCP) ──


async def test_remote_switch_takeover_and_stop_hand_back_on_real_core(hass: HomeAssistant, socket_enabled,
                                                                      monkeypatch):
    from custom_components.volcast.control import executor as ex_mod
    from custom_components.volcast.control.device_io import DirectIO
    from custom_components.volcast.control.executor import VolcastExecutor
    from custom_components.volcast.control.store import ControlStore
    from custom_components.volcast.core.control.select import ProfileChoice
    from custom_components.volcast.core.slot import parse_schedule
    from tests.control.test_direct_connection import _factory
    from tests.control.test_executor_direct_handback import (
        AUTO, CHARGE, DISCHARGE, ENABLE, LINK, MODE, SELL, TwoUnitServer, plant_bank, profile, target)
    from tests.core.modbus.engine_profile import PLANT
    from tests.sim.device import Faults, RegisterBank
    from tests.sim.fixtures import goodwe_words

    monkeypatch.setattr(ex_mod.ir, "async_create_issue", lambda *a, **k: None)
    prof = profile()
    plant = plant_bank()                                  # zapamiętany tryb wymuszony, przełącznik wyłączony
    sim = await TwoUnitServer({LINK: RegisterBank({**goodwe_words(), 47509: 1}), PLANT: plant}, Faults()).start()
    tgt = target(sim, prof)
    entry = make_entry(hass, options={"control_mode": "direct", "direct_target": tgt})
    salt = bytes(range(16))
    conn = DirectConnection(hass, entry, prof, tgt, trial=False, salt=salt, transport_factory=_factory(1.0),
                            allow_loopback=True, unreadable=())
    io = DirectIO(conn, prof, trial=False, unreadable=(), capabilities=tgt["capabilities"], salt=salt)
    ex = VolcastExecutor(hass, entry, choice=ProfileChoice(prof, None, None), mapped={}, rated_power_w=8000.0,
                         store=ControlStore(hass, entry.entry_id), io=io)
    now = dt_util.utcnow().replace(microsecond=0)
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    plan = {"schedule_id": "p1", "control_enabled": True,
            "slots": [{"from": iso(now - timedelta(minutes=5)), "to": iso(now + timedelta(minutes=55)),
                       "mode": "discharge", "discharge_purpose": "sell", "power_w": 2000, "price_pln_kwh": 0.8}],
            "fallback": {"mode": "self_consume", "soc_reserve": 10}}
    try:
        await conn.async_start()
        await ex.async_start()
        await ex.async_on_plan(plan, parse_schedule(plan))
        await ex.async_set_consent(True)
        await ex.async_set_local_switch(True)
        await conn.async_poll()
        await ex.async_tick()
        w = plant.writes
        idx = lambda addr, value, after=-1: next(  # noqa: E731
            i for i, (a, v) in enumerate(w) if i > after and a == addr and v == value)
        mode = idx(MODE, SELL)
        assert max(idx(CHARGE + 1, 0), idx(DISCHARGE, 0)) < idx(ENABLE, 1) < mode < idx(DISCHARGE, 200, mode)
        assert ex.owned
        await conn.async_poll()
        n = len(w)
        assert await ex.async_neutral_at_stop() is True
        back = w[n:]
        first = lambda addr, value: next(i for i, (a, v) in enumerate(back) if a == addr and v == value)  # noqa: E731
        assert first(MODE, AUTO) < first(DISCHARGE, 600) < first(ENABLE, 0)
        assert plant.read(ENABLE, 1) == [0] and plant.read(CHARGE, 2) == [0, 5000]
    finally:
        await ex.async_stop()
        await conn.async_stop()
        await sim.close()
