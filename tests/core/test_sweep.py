"""Wyszukiwanie kandydatów w lokalnych podsieciach — czyste reguły podsieci i skan z wstrzykniętym I/O."""
from __future__ import annotations

import asyncio
import ipaddress
import logging

import pytest

from custom_components.volcast.core.discovery.network import LoggerReply, NetworkProbeResult
from custom_components.volcast.core.discovery.sweep import local_subnets, sweep


def _adapter(*addrs: tuple[str, int], enabled: bool = True) -> dict:
    # kształt z homeassistant.components.network.async_get_adapters
    return {
        "name": "eth0", "index": 1, "enabled": enabled, "auto": True, "default": True,
        "ipv6": [],
        "ipv4": [{"address": a, "network_prefix": p} for a, p in addrs],
    }


def _nets(result) -> list[str]:
    return [str(n) for n in result]


def test_private_24_plus_old_host_24():
    r = local_subnets([_adapter(("192.168.1.10", 24))], "192.168.5.20")
    assert _nets(r) == ["192.168.1.0/24", "192.168.5.0/24"]


def test_old_host_in_same_subnet_deduplicated():
    r = local_subnets([_adapter(("10.0.0.5", 24))], "10.0.0.99")
    assert _nets(r) == ["10.0.0.0/24"]


def test_larger_than_24_cut_to_ha_24_and_old_host_24():
    r = local_subnets([_adapter(("10.1.2.3", 16))], "10.1.9.9")
    assert _nets(r) == ["10.1.2.0/24", "10.1.9.0/24"]


def test_smaller_than_24_kept():
    r = local_subnets([_adapter(("192.168.1.10", 28))], "192.168.1.11")
    assert "192.168.1.0/28" in _nets(r)
    assert "192.168.1.0/24" in _nets(r)  # /24 starego hosta


@pytest.mark.parametrize("addr", ["8.8.8.8", "100.64.0.5", "169.254.1.1", "127.0.0.1", "192.0.2.5", "172.32.0.1"])
def test_non_rfc1918_never_scanned(addr):
    assert local_subnets([_adapter((addr, 24))], addr) == []


def test_rfc1918_172_block_boundaries():
    assert _nets(local_subnets([_adapter(("172.16.0.5", 24))], "8.8.8.8")) == ["172.16.0.0/24"]
    assert _nets(local_subnets([_adapter(("172.31.255.5", 24))], "8.8.8.8")) == ["172.31.255.0/24"]


def test_disabled_adapter_and_garbage_tolerated():
    adapters = [
        _adapter(("192.168.1.10", 24), enabled=False),
        {"enabled": True, "ipv4": [{"address": "x", "network_prefix": 24}]},
        {"enabled": True},
    ]
    assert local_subnets(adapters, "not-an-ip") == []


def test_old_host_public_not_added():
    assert _nets(local_subnets([_adapter(("192.168.1.10", 24))], "8.8.8.8")) == ["192.168.1.0/24"]


def _udp_reply(has_logger: bool) -> NetworkProbeResult:
    replies = [LoggerReply(raw="r", ip="x", mac="m", name=None)] if has_logger else []
    return NetworkProbeResult(sent=True, replies=replies)


def _net(s: str):
    return ipaddress.ip_network(s)


@pytest.mark.asyncio
async def test_tcp_sweep_private_hosts():
    async def connect(host, port, timeout):
        return host in ("10.9.9.5", "10.9.9.7") and port == 502

    out = await sweep([_net("10.9.9.0/24")], transport="modbus_tcp", port=502,
                      connect=connect, udp_probe=None)
    assert sorted(c.host for c in out) == ["10.9.9.5", "10.9.9.7"]
    assert all(c.source == "sweep" and c.transports == ("modbus_tcp",) for c in out)


@pytest.mark.asyncio
async def test_udp_sweep_uses_unicast_probe_and_replies():
    seen = []

    async def udp_probe(target, port=48899, timeout=2.0):
        seen.append((target, port))
        return _udp_reply(target == "10.9.9.20")

    out = await sweep([_net("10.9.9.16/28")], transport="goodwe_udp", port=48899,
                      connect=None, udp_probe=udp_probe)
    assert [c.host for c in out] == ["10.9.9.20"]
    assert out[0].transports == ("goodwe_udp",)
    assert ("10.9.9.20", 48899) in seen and len(seen) == 14


@pytest.mark.asyncio
async def test_solarman_v5_uses_udp_and_rtu_tcp_uses_connect():
    async def udp_probe(target, port=48899, timeout=2.0):
        return _udp_reply(True)

    async def connect(host, port, timeout):
        return True

    u = await sweep([_net("10.9.9.0/30")], transport="solarman_v5", port=8899, connect=None, udp_probe=udp_probe)
    assert len(u) == 2 and u[0].transports == ("solarman_v5",)
    t = await sweep([_net("10.9.9.0/30")], transport="rtu_tcp", port=8899, connect=connect, udp_probe=None)
    assert len(t) == 2


@pytest.mark.asyncio
async def test_probe_error_or_exception_means_no_candidate():
    async def udp_probe(target, port=48899, timeout=2.0):
        if target.endswith(".1"):
            raise OSError("boom")
        return NetworkProbeResult(sent=False, error="x")

    out = await sweep([_net("10.9.9.0/30")], transport="goodwe_udp", port=48899, connect=None, udp_probe=udp_probe)
    assert out == []


@pytest.mark.asyncio
async def test_skip_own_address_and_public_addresses():
    probed = []

    async def connect(host, port, timeout):
        probed.append(host)
        return True

    out = await sweep([_net("10.9.9.0/30"), _net("8.8.8.0/30"), _net("192.0.2.0/30")],
                      transport="modbus_tcp", port=502,
                      connect=connect, udp_probe=None, skip=("10.9.9.1",))
    assert [c.host for c in out] == ["10.9.9.2"]
    assert all(h.startswith("10.9.9.") for h in probed)


@pytest.mark.asyncio
async def test_dedup_overlapping_subnets():
    async def connect(host, port, timeout):
        return True

    out = await sweep([_net("10.9.9.0/30"), _net("10.9.9.0/29")], transport="modbus_tcp", port=502,
                      connect=connect, udp_probe=None)
    hosts = [c.host for c in out]
    assert len(hosts) == len(set(hosts)) == 6


async def _peak_of(**kw) -> int:
    active = peak = 0

    async def connect(host, port, timeout):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return False

    await sweep([_net("10.9.9.0/24")], transport="modbus_tcp", port=502,
                connect=connect, udp_probe=None, **kw)
    return peak


@pytest.mark.asyncio
async def test_concurrency_limit_never_exceeded():
    assert await _peak_of(concurrency=8) == 8


@pytest.mark.asyncio
async def test_default_concurrency_is_32():
    assert await _peak_of() == 32


@pytest.mark.asyncio
async def test_budget_timeout_returns_partial_and_cancels():
    cancelled = 0

    async def connect(host, port, timeout):
        nonlocal cancelled
        if host == "10.9.9.1":
            return True
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled += 1
            raise
        return True

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    out = await sweep([_net("10.9.9.0/28")], transport="modbus_tcp", port=502,
                      connect=connect, udp_probe=None, budget_s=0.2)
    assert loop.time() - t0 < 2
    assert [c.host for c in out] == ["10.9.9.1"]
    assert cancelled > 0


@pytest.mark.asyncio
async def test_per_try_timeout_passed_to_io():
    got = []

    async def connect(host, port, timeout):
        got.append(timeout)
        return False

    await sweep([_net("10.9.9.0/30")], transport="modbus_tcp", port=502, connect=connect,
                udp_probe=None, per_try_s=0.25)
    assert got and set(got) == {0.25}


@pytest.mark.asyncio
async def test_no_addresses_in_logs(caplog):
    async def connect(host, port, timeout):
        return True

    async def bad(host, port, timeout):
        raise OSError("10.9.9.2 unreachable")

    with caplog.at_level(logging.DEBUG):
        await sweep([_net("10.9.9.0/30")], transport="modbus_tcp", port=502, connect=connect, udp_probe=None)
        await sweep([_net("10.9.9.0/30")], transport="modbus_tcp", port=502, connect=bad, udp_probe=None)
    assert "10.9.9" not in caplog.text
