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
