"""Fixtures klienta rejestrów i pisarza na symulatorach z pętli zwrotnej."""
import pytest
import pytest_asyncio

from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.transports.base import TransportConfig
from custom_components.volcast.core.transports.factory import make_transport

SALT = bytes(range(16))


def sim_transport(sim, kind: str, unit: int, **kw):
    cfg = TransportConfig(kind=kind, host=sim.host, port=sim.port, unit=unit,
                          timeout_s=kw.pop("timeout_s", 0.15), gap_s=0.0, read_tries=kw.pop("read_tries", 2), **kw)
    return make_transport(cfg, allow_loopback=True)


@pytest.fixture
def goodwe_profile():
    return load_builtin("goodwe-et")


@pytest.fixture
def deye_profile():
    return load_builtin("deye-sg")


@pytest_asyncio.fixture
async def goodwe_client(goodwe_udp_sim, goodwe_profile):
    client = RegisterClient(sim_transport(goodwe_udp_sim, "goodwe_udp", 0xF7), goodwe_profile, salt=SALT)
    yield client
    await client.transport.close()


@pytest.fixture
def goodwe_writer(goodwe_client, goodwe_profile):
    return RegisterWriter(goodwe_client, goodwe_profile)


@pytest_asyncio.fixture
async def deye_client(rtu_tcp_sim, deye_profile):
    client = RegisterClient(sim_transport(rtu_tcp_sim, "modbus_rtu", 1), deye_profile, salt=SALT)
    yield client
    await client.transport.close()


@pytest.fixture
def deye_writer(deye_client, deye_profile):
    return RegisterWriter(deye_client, deye_profile)


@pytest.fixture(autouse=True)
def writer_sleeps(monkeypatch):
    """Przerwy pisarza między ponowionymi odczytami — rejestrowane, bez realnego czekania."""
    from custom_components.volcast.core.modbus import writer as writer_mod
    delays: list[float] = []

    async def _no_wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(writer_mod, "_sleep", _no_wait, raising=False)
    return delays
