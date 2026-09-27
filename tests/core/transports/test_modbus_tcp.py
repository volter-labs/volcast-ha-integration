"""Transport Modbus TCP (MBAP) na symulatorze z pętli zwrotnej."""
import pytest

from custom_components.volcast.core.transports.modbus_frames import FC_WRITE_SINGLE

from .tcp_common import *  # noqa: F401,F403 — wspólne przypadki transportów strumieniowych
from .tcp_common import link_factory


@pytest.fixture
def link(modbus_tcp_sim, goodwe_bank, sim_faults):
    return link_factory(modbus_tcp_sim, goodwe_bank, sim_faults, "modbus_tcp", 247, None,
                        a=(45356, 1, [5]), b=(47511, 1, [11]), write=(47511, 10),
                        function=FC_WRITE_SINGLE)


@pytest.mark.asyncio
async def test_golden_block_over_mbap(link):
    t = link.open()
    try:
        assert await t.read(47509, 4) == [0, 16000, 11, 8846]
    finally:
        await t.close()
