"""Transport RTU przez przezroczystą bramkę TCP na symulatorze z pętli zwrotnej."""
import asyncio

import pytest

from custom_components.volcast.core.transports.modbus_frames import FC_WRITE_MULTIPLE

from .tcp_common import *  # noqa: F401,F403 — wspólne przypadki transportów strumieniowych
from .tcp_common import link_factory


@pytest.fixture
def link(rtu_tcp_sim, deye_bank, sim_faults):
    return link_factory(rtu_tcp_sim, deye_bank, sim_faults, "modbus_rtu", 1, None,
                        a=(154, 1, [3000]), b=(166, 1, [80]), write=(148, 130),
                        function=FC_WRITE_MULTIPLE)


@pytest.mark.asyncio
async def test_undelimitable_function_drops_connection(link, rtu_tcp_sim):
    # Kod funkcji, którego nie da się odciąć w strumieniu → strumień rozsynchronizowany:
    # połączenie zamknięte przed wysłaniem, żądanie idzie już nowym połączeniem.
    t = link.open(read_tries=1)
    try:
        assert await t.read(154, 1) == [3000]
        rtu_tcp_sim.push(bytes([1, 0x2B, 0x0E, 0x01]))
        await asyncio.sleep(0.05)
        assert await t.read(166, 1) == [80]
        assert t.stats.stray == 1 and t.stats.channel_resets == 1
        assert rtu_tcp_sim.clients == 2
    finally:
        await t.close()
