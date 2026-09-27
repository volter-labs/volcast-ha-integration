import asyncio

import pytest

from custom_components.volcast.core.discovery.network import (
    PROBE_MESSAGE,
    parse_reply,
    probe_udp_48899,
)


def test_parse_reply_three_fields():
    r = parse_reply(b"192.168.1.50,AABBCCDDEEFF,2712345678")
    assert (r.ip, r.mac, r.name) == ("192.168.1.50", "AABBCCDDEEFF", "2712345678")


def test_parse_reply_garbage_keeps_raw():
    r = parse_reply(b"\xff\xfegarbage")
    assert r.ip is None and r.raw


@pytest.mark.asyncio
async def test_probe_collects_reply_from_fake_logger():
    loop = asyncio.get_running_loop()

    class Fake(asyncio.DatagramProtocol):
        def connection_made(self, t):
            self.t = t

        def datagram_received(self, data, addr):
            if data == PROBE_MESSAGE:
                self.t.sendto(b"127.0.0.1,AABBCCDDEEFF,SN123", addr)

    transport, _ = await loop.create_datagram_endpoint(Fake, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    try:
        res = await probe_udp_48899(target="127.0.0.1", port=port, timeout=0.5)
    finally:
        transport.close()
    assert res.sent and res.error is None
    assert [r.mac for r in res.replies] == ["AABBCCDDEEFF"]


@pytest.mark.asyncio
async def test_probe_no_replies_is_not_error():
    res = await probe_udp_48899(target="127.0.0.1", port=9, timeout=0.2)
    assert res.replies == [] and res.error is None


@pytest.mark.asyncio
async def test_probe_socket_failure_reported_not_raised(monkeypatch):
    loop = asyncio.get_running_loop()

    async def boom(*a, **k):
        raise PermissionError("broadcast not permitted")

    monkeypatch.setattr(loop, "create_datagram_endpoint", boom)
    res = await probe_udp_48899(timeout=0.1)
    assert res.sent is False and "broadcast not permitted" in res.error


@pytest.mark.asyncio
async def test_probe_send_error_surfaced_via_error_received(monkeypatch):
    """CPython's datagram transport catches OSError from sendto and reports it
    through protocol.error_received instead of raising — the probe must surface
    that instead of returning sent=True, error=None."""
    loop = asyncio.get_running_loop()
    real_create = loop.create_datagram_endpoint

    async def create_and_fail(protocol_factory, *a, **k):
        transport, protocol = await real_create(protocol_factory, *a, **k)
        loop.call_soon(protocol.error_received, OSError(101, "Network is unreachable"))
        return transport, protocol

    monkeypatch.setattr(loop, "create_datagram_endpoint", create_and_fail)
    res = await probe_udp_48899(target="127.0.0.1", port=9, timeout=0.2)
    assert res.sent is True
    assert res.error is not None and "Network is unreachable" in res.error
    assert res.replies == []


@pytest.mark.asyncio
async def test_probe_ignores_own_echo_and_dedups_replies():
    loop = asyncio.get_running_loop()

    class Fake(asyncio.DatagramProtocol):
        def connection_made(self, t):
            self.t = t

        def datagram_received(self, data, addr):
            if data == PROBE_MESSAGE:
                # echo naszej sondy z powrotem — musi zostać zignorowane
                self.t.sendto(PROBE_MESSAGE, addr)
                # ten sam wpis wysłany dwukrotnie — musi zostać zdeduplikowany
                self.t.sendto(b"127.0.0.1,AABBCCDDEEFF,SN123", addr)
                self.t.sendto(b"127.0.0.1,AABBCCDDEEFF,SN123", addr)

    transport, _ = await loop.create_datagram_endpoint(Fake, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    try:
        res = await probe_udp_48899(target="127.0.0.1", port=port, timeout=0.5)
    finally:
        transport.close()
    assert res.sent and res.error is None
    assert [r.mac for r in res.replies] == ["AABBCCDDEEFF"]


def test_logger_serial_parsed_from_ten_digit_field():
    r = parse_reply(b"192.168.1.50,AABBCCDDEEFF,2712345678")
    assert r.logger_serial == 2712345678
    for raw in (b"192.168.1.50,AABBCCDDEEFF,Solar-WiFi123", b"192.168.1.50,AABBCCDDEEFF,123456789",
                b"192.168.1.50,AABBCCDDEEFF,9999999999", b"192.168.1.50,AABBCCDDEEFF"):
        assert parse_reply(raw).logger_serial is None          # nie 10 cyfr albo ponad u32
