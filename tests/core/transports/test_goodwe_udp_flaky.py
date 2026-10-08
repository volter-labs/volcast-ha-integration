"""Transport GoodWe UDP wobec zachowań modułu Wi-Fi zmierzonych na żywo: datagramy czekające w gnieździe
przed wysłaniem, obce ramki innych klientów, odpowiedź spóźniona ponad limit czasu, odstęp 300 ms."""
import asyncio
import time

import pytest
import pytest_asyncio

from custom_components.volcast.core.discovery.identify import _groups
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.transports.base import RequestTimeout, TransportConfig
from custom_components.volcast.core.transports.factory import make_transport
from tests.sim.device import RegisterBank
from tests.sim.fixtures import goodwe_words
from tests.sim.flaky import aa55, flaky_module, read_pdu

from .helpers import FakeClock


@pytest_asyncio.fixture
async def module():
    m = await flaky_module(RegisterBank(goodwe_words()))
    yield m
    await m.close()


def _udp(m, **kw):
    cfg = TransportConfig(kind="goodwe_udp", host=m.host, port=m.port, unit=0xF7,
                          timeout_s=kw.pop("timeout_s", 0.2), gap_s=kw.pop("gap_s", 0.0), **kw)
    return make_transport(cfg, allow_loopback=True)


def _in_socket_not_in_loop() -> None:
    """Blokujące czekanie: wstrzyknięte datagramy dochodzą do gniazda (jądro), a pętla — wstrzymana —
    ich nie odbiera. Następne żądanie startuje bez żadnego ustąpienia pętli (odstęp 0)."""
    time.sleep(0.03)


@pytest.mark.asyncio
async def test_datagram_waiting_in_socket_before_send_is_dropped_and_counted(module):
    t = _udp(module)
    try:
        assert await t.read(45356, 1) == [5]
        # Nieaktualna ramka tej samej długości trafia do gniazda TUŻ przed kolejnym żądaniem —
        # pętla jeszcze jej nie odebrała. Bez opróżnienia byłaby wzięta za odpowiedź.
        module.inject(aa55(0xF7, read_pdu([999])))
        _in_socket_not_in_loop()
        assert await t.read(47511, 1) == [11]
        assert t.stats.stray == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_last_read_count_is_the_last_read_request(module):
    t = _udp(module)
    try:
        assert t.last_read_count is None
        await t.read(47509, 4)
        assert t.last_read_count == 4
        await t.write(45356, [5], function=6)              # zapis nie zmienia (inna funkcja odpowiedzi)
        assert t.last_read_count == 4
        await t.read(45356, 1)
        assert t.last_read_count == 1
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_several_waiting_datagrams_all_dropped(module):
    t = _udp(module)
    try:
        await t.read(45356, 1)
        for v in (997, 998, 999):
            module.inject(aa55(0xF7, read_pdu([v])))
        _in_socket_not_in_loop()
        assert await t.read(47512, 1) == [8846]
        assert t.stats.stray == 3
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_foreign_frames_of_other_lengths_are_stray(module):
    module.foreign = {1: (45, 125, 24)}
    t = _udp(module)
    try:
        assert await t.read(47509, 4) == [0, 16000, 11, 8846]
        assert t.stats.stray == 3 and t.stats.timeouts == 0
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_reply_later_than_timeout_never_answers_next_request(module):
    module.plan = ["late"]
    module.late_s = 0.3
    t = _udp(module, timeout_s=0.15, read_tries=1)
    try:
        with pytest.raises(RequestTimeout):
            await t.read(45356, 1)
        await asyncio.sleep(0.25)                     # spóźniona odpowiedź dochodzi do starego gniazda
        assert await t.read(47511, 1) == [11]
    finally:
        await t.close()


# ── odstęp między żądaniami: 300 ms jak Box ───────────────────────────────


def test_goodwe_udp_gap_is_300_ms_in_profile_and_built_config():
    gw = load_builtin("goodwe-et")
    assert gw.modbus.transport_options["goodwe_udp"]["gap_ms"] == 300
    (key, profs), = [g for g in _groups("goodwe_udp", [gw])]
    port, unit, timeout_s, gap_s = key
    assert (port, unit, timeout_s, gap_s) == (8899, 247, 2.0, 0.3)


@pytest.mark.asyncio
async def test_gap_300_ms_waited_before_each_frame(module):
    clock = FakeClock()
    cfg = TransportConfig(kind="goodwe_udp", host=module.host, port=module.port, unit=0xF7,
                          timeout_s=0.3, gap_s=0.3)
    t = make_transport(cfg, clock=clock, sleep=clock.sleep, allow_loopback=True)
    try:
        await t.read(45356, 1)
        await t.read(47509, 4)
        await t.read(47512, 1)
        assert clock.sleeps == [pytest.approx(0.3), pytest.approx(0.3)]
    finally:
        await t.close()
