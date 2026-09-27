"""Transport Solarman V5 (logger TCP 8899) na symulatorze z pętli zwrotnej."""
import asyncio

import pytest

from custom_components.volcast.core.transports.base import InverterAsleep
from custom_components.volcast.core.transports.modbus_frames import FC_WRITE_MULTIPLE
from custom_components.volcast.core.transports import v5_frames as v5
from tests.sim.fixtures import V5_LOGGER_SERIAL

from .tcp_common import *  # noqa: F401,F403 — wspólne przypadki transportów strumieniowych
from .tcp_common import link_factory


@pytest.fixture
def link(v5_sim, deye_bank, sim_faults):
    return link_factory(v5_sim, deye_bank, sim_faults, "solarman_v5", 1, V5_LOGGER_SERIAL,
                        a=(154, 1, [3000]), b=(166, 1, [80]), write=(148, 130),
                        function=FC_WRITE_MULTIPLE)


@pytest.mark.asyncio
async def test_v5_heartbeat_counts_unsolicited_not_stray(link, v5_sim):
    link.faults.heartbeat_next = 1
    t = link.open()
    try:
        assert await t.read(154, 1) == [3000]
        assert t.stats.unsolicited == 1 and t.stats.stray == 0
        await asyncio.sleep(0.05)
        acks = [f for f in v5_sim.protocol_frames if v5.control_code(f) == 0x1710]
        assert len(acks) == 1                      # potwierdzone jak w źródle protokołu
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_v5_idle_heartbeat_drained_as_unsolicited(link, v5_sim):
    t = link.open()
    try:
        assert await t.read(154, 1) == [3000]
        v5_sim.push(v5_sim.heartbeat())
        await asyncio.sleep(0.05)
        assert await t.read(166, 1) == [80]
        assert t.stats.unsolicited == 1 and t.stats.stray == 0
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_v5_foreign_logger_frame_is_stray(link, v5_sim):
    t = link.open()
    try:
        assert await t.read(154, 1) == [3000]
        v5_sim.push(v5_sim.heartbeat(logger_serial=V5_LOGGER_SERIAL + 1))
        await asyncio.sleep(0.05)
        assert await t.read(166, 1) == [80]
        assert t.stats.stray == 1 and t.stats.unsolicited == 0
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_v5_asleep_inverter_is_own_error(link):
    link.faults.asleep_next = 1
    t = link.open()
    try:
        with pytest.raises(InverterAsleep):
            await t.read(154, 1)
        assert t.stats.stray == 0 and t.stats.timeouts == 0
        assert await t.read(154, 1) == [3000]
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_v5_sequence_advances_per_request(link, v5_sim):
    t = link.open()
    try:
        for _ in range(3):
            assert await t.read(154, 1) == [3000]
        assert t.stats.stray == 0 and v5_sim.requests == 3
    finally:
        await t.close()
