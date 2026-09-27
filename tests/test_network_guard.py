"""Straż sieci w testach: żadnego gniazda poza pętlą zwrotną ani rozgłoszenia.

Cele w tym pliku to WYŁĄCZNIE adresy dokumentacyjne (203.0.113.0/24, 198.51.100.0/24,
2001:db8::/32) i nazwy w domenie `.invalid` — nawet zepsuta straż nie wyśle nic do
prawdziwego urządzenia ani usługi.
"""
import asyncio
import asyncio.base_events
import asyncio.selector_events
import inspect
import socket

import pytest

from custom_components.volcast.core.discovery import network

GUARD = pytest.fail.Exception
DOC_V4 = "203.0.113.5"
DOC_V4_B = "198.51.100.7"
DOC_V4_BROADCAST = "203.0.113.255"
DOC_V6 = "2001:db8::1"
BAD_NAME = "inverter.invalid"


@pytest.fixture(autouse=True)
def _expected_violations(_no_real_network):
    # Testy tego pliku celowo wywołują straż — oczekiwane naruszenia nie psują sprzątania.
    yield
    _no_real_network.violations.clear()


def test_guard_blocks_non_loopback_connect():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(GUARD):
            s.connect((DOC_V4, 502))
        with pytest.raises(GUARD):
            s.connect_ex((DOC_V4, 502))


def test_guard_blocks_broadcast_sendto():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(GUARD):
            s.sendto(b"x", (DOC_V4_BROADCAST, 48899))
        with pytest.raises(GUARD):
            s.sendto(b"x", 0, (DOC_V4_B, 8899))


@pytest.mark.parametrize("host", [BAD_NAME, DOC_V4, DOC_V6, ""])
def test_guard_blocks_hostnames_and_public(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(GUARD):
            s.sendto(b"x", (host, 9))


@pytest.mark.asyncio
async def test_guard_blocks_asyncio_entry_points():
    loop = asyncio.get_running_loop()
    with pytest.raises(GUARD):
        await asyncio.open_connection(DOC_V4, 502)
    with pytest.raises(GUARD):
        await loop.create_connection(asyncio.Protocol, DOC_V4_B, 502)
    with pytest.raises(GUARD):
        await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=(DOC_V4_BROADCAST, 48899))


@pytest.mark.parametrize("host", [BAD_NAME, DOC_V4, DOC_V6, b"inverter.invalid", ""])
def test_guard_blocks_name_resolution(host):
    with pytest.raises(GUARD):
        socket.getaddrinfo(host, 502)


@pytest.mark.asyncio
async def test_guard_blocks_loop_name_resolution():
    loop = asyncio.get_running_loop()
    with pytest.raises(GUARD):
        await loop.getaddrinfo(BAD_NAME, 502)
    with pytest.raises(GUARD):
        await loop.getaddrinfo(DOC_V6, 502)


def test_guard_allows_loopback_name_resolution():
    assert socket.getaddrinfo("127.0.0.1", 9, type=socket.SOCK_DGRAM)
    assert socket.getaddrinfo("::1", 9, family=socket.AF_INET6, type=socket.SOCK_DGRAM)
    assert socket.getaddrinfo("localhost", 9, type=socket.SOCK_DGRAM)


@pytest.mark.asyncio
async def test_guard_blocks_loop_sock_connect_and_sendto():
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setblocking(False)
        with pytest.raises(GUARD):
            await loop.sock_connect(s, (DOC_V4, 502))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setblocking(False)
        with pytest.raises(GUARD):
            await loop.sock_sendto(s, b"x", (DOC_V4_B, 8899))


def test_every_entry_point_is_wrapped_explicitly():
    # Straż nie może polegać na tym, że pętla asyncio woła metody gniazda (uvloop, Proactor tak nie robią).
    entry_points = [
        socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto, socket.getaddrinfo,
        asyncio.open_connection,
        asyncio.base_events.BaseEventLoop.create_connection,
        asyncio.base_events.BaseEventLoop.create_datagram_endpoint,
        asyncio.base_events.BaseEventLoop.getaddrinfo,
        asyncio.selector_events.BaseSelectorEventLoop.sock_connect,
        asyncio.selector_events.BaseSelectorEventLoop.sock_sendto,
    ]
    assert all(getattr(fn, "_network_guard", False) for fn in entry_points)


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
    await network.probe_udp_48899(target=DOC_V4_BROADCAST, port=9, timeout=0.05)
    assert _no_real_network.violations
    _no_real_network.violations.clear()


def test_guard_records_violations_for_teardown(_no_real_network):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.sendto(b"x", (DOC_V4, 9))
        except BaseException:           # noqa: BLE001 — symulacja kodu, który połyka błąd
            pass
    assert len(_no_real_network.violations) == 1
    _no_real_network.violations.clear()
