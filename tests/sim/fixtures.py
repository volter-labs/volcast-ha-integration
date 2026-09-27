"""Fixtures symulatorów — każdy serwer zamykany przy sprzątaniu testu."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from .device import Faults, RegisterBank
from .servers import goodwe_udp_server, modbus_tcp_server, rtu_tcp_server, solarman_v5_server

GOLDEN = Path(__file__).resolve().parents[1] / "golden"
V5_LOGGER_SERIAL = 1234567890           # fikcyjny numer loggera symulatora
GOODWE_UNREADABLE = (47760,)            # nagranie: rejestr nieczytelny na tym modelu


def _aa55_words(frame_hex: str, offset: int, count: int) -> dict[int, int]:
    raw = bytes.fromhex(frame_hex)[2 + 3:2 + 3 + 2 * count]
    return {offset + i: int.from_bytes(raw[2 * i:2 * i + 2], "big") for i in range(count)}


def goodwe_words() -> dict[int, int]:
    frames = json.loads((GOLDEN / "goodwe_et" / "frames.json").read_text())
    words: dict[int, int] = {}
    for f in frames.values():
        if f["valid"]:
            words.update(_aa55_words(f["response"], f["offset"], f["count"]))
    return words


def deye_words() -> dict[int, int]:
    doc = json.loads((GOLDEN / "deye_sg" / "registers.json").read_text())
    return {int(a): w for a, w in doc["registers"].items()}


@pytest.fixture
def goodwe_bank() -> RegisterBank:
    return RegisterBank(goodwe_words(), unreadable=GOODWE_UNREADABLE)


@pytest.fixture
def deye_bank() -> RegisterBank:
    return RegisterBank(deye_words())


@pytest.fixture
def sim_faults() -> Faults:
    return Faults()


@pytest_asyncio.fixture
async def goodwe_udp_sim(goodwe_bank, sim_faults):
    server = await goodwe_udp_server(goodwe_bank, sim_faults)
    yield server
    await server.close()


@pytest_asyncio.fixture
async def modbus_tcp_sim(goodwe_bank, sim_faults):
    server = await modbus_tcp_server(goodwe_bank, sim_faults)
    yield server
    await server.close()


@pytest_asyncio.fixture
async def rtu_tcp_sim(deye_bank, sim_faults):
    server = await rtu_tcp_server(deye_bank, sim_faults)
    yield server
    await server.close()


@pytest_asyncio.fixture
async def v5_sim(deye_bank, sim_faults):
    server = await solarman_v5_server(deye_bank, sim_faults, logger_serial=V5_LOGGER_SERIAL)
    yield server
    await server.close()
