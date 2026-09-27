"""Straż sieci w testach: żadnego gniazda poza pętlą zwrotną ani rozgłoszenia."""
import asyncio
import inspect
import socket

import pytest

from custom_components.volcast.core.discovery import network

GUARD = pytest.fail.Exception


@pytest.fixture(autouse=True)
def _expected_violations(_no_real_network):
    # Testy tego pliku celowo wywołują straż — oczekiwane naruszenia nie psują sprzątania.
    yield
    _no_real_network.violations.clear()


def test_guard_blocks_non_loopback_connect():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(GUARD):
            s.connect(("192.0.2.1", 502))
        with pytest.raises(GUARD):
            s.connect_ex(("192.0.2.1", 502))


def test_guard_blocks_broadcast_sendto():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(GUARD):
            s.sendto(b"x", ("255.255.255.255", 48899))
        with pytest.raises(GUARD):
            s.sendto(b"x", 0, ("10.0.0.1", 8899))


@pytest.mark.parametrize("host", ["inverter.local", "8.8.8.8", "2001:4860::1", ""])
def test_guard_blocks_hostnames_and_public(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(GUARD):
            s.sendto(b"x", (host, 9))


@pytest.mark.asyncio
async def test_guard_blocks_asyncio_entry_points():
    loop = asyncio.get_running_loop()
    with pytest.raises(GUARD):
        await asyncio.open_connection("10.0.0.1", 502)
    with pytest.raises(GUARD):
        await loop.create_connection(asyncio.Protocol, "192.168.1.50", 502)
    with pytest.raises(GUARD):
        await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=("255.255.255.255", 48899))


@pytest.mark.asyncio
async def test_guard_allows_loopback():
    loop = asyncio.get_running_loop()
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol,
                                                           remote_addr=("127.0.0.1", 9))
        transport.close()
    finally:
        server.close()
        await server.wait_closed()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(b"x", ("127.0.0.1", 9))


def test_probe_default_target_is_loopback_in_tests():
    assert inspect.signature(network.probe_udp_48899).parameters["target"].default == "127.0.0.1"


@pytest.mark.asyncio
async def test_probe_with_default_target_runs_under_guard():
    res = await network.probe_udp_48899(port=9, timeout=0.05)
    assert res.sent is True


@pytest.mark.asyncio
async def test_probe_explicit_broadcast_fails_the_guard(_no_real_network):
    # Transport asyncio połyka wyjątek z `sendto` — straż i tak zapamiętuje naruszenie,
    # a przy sprzątaniu testu kończy go błędem (tu czyścimy listę, bo to zamierzone).
    await network.probe_udp_48899(target="255.255.255.255", port=9, timeout=0.05)
    assert _no_real_network.violations
    _no_real_network.violations.clear()


def test_guard_records_violations_for_teardown(_no_real_network):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.sendto(b"x", ("192.0.2.7", 9))
        except BaseException:           # noqa: BLE001 — symulacja kodu, który połyka błąd
            pass
    assert len(_no_real_network.violations) == 1
    _no_real_network.violations.clear()
