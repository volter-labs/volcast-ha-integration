"""Sonda możliwości na GoodWe UDP wobec modułu Wi-Fi z poprzednią odpowiedzią zamiast bieżącej.

Kształt urządzenia z nagrania GW8KN-ET (`tests/golden/goodwe_et`, fikcyjny numer seryjny):
47760 (soc_max) odpowiada wyjątkiem 2, reszta kluczy sondy istnieje. Jeden nieaktualny wyjątek nie
może wyłączyć możliwości; potwierdzony wyjątek 2 wyłącza tylko górny próg SoC.
"""
from dataclasses import replace

import pytest
import pytest_asyncio

from custom_components.volcast.core.control.caps import direct_capabilities
from custom_components.volcast.core.discovery.identify import Candidate
from custom_components.volcast.core.discovery.probe import discover
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.transports.factory import make_transport
from tests.sim.device import RegisterBank
from tests.sim.fixtures import goodwe_words
from tests.sim.flaky import flaky_module

SALT = bytes(range(16))
GW = load_builtin("goodwe-et")
LEGAL = ("mode", "power_w", "soc_min", "export_limit_w", "export_limit_enabled")


@pytest_asyncio.fixture
async def module():
    m = await flaky_module(RegisterBank(goodwe_words(), unsupported=(47760,)))
    yield m
    await m.close()


async def _probe(m):
    def factory(cfg):
        return make_transport(replace(cfg, port=m.port, timeout_s=0.1, gap_s=0.0), allow_loopback=True)
    reports = await discover([Candidate(m.host, "manual", transports=("goodwe_udp",))], [GW],
                             transport_factory=factory, conflicts=lambda host: (), salt=SALT)
    assert len(reports) == 1
    return reports[0]


@pytest.mark.asyncio
async def test_recorded_inverter_shape_soc_ceiling_off_everything_else_on(module):
    rep = await _probe(module)
    assert rep.identity is not None and rep.identity.model == "GW8KN-ET"
    assert rep.capabilities == {**{k: True for k in LEGAL}, "soc_max": False}
    assert rep.echo_only == () and rep.direct_available is True
    caps = direct_capabilities(GW, rep.capabilities, rep.unreadable)
    assert caps["set_soc_ceiling"] is False
    for cap in ("set_soc_floor", "force_charge_from_grid", "sell_from_battery", "force_discharge",
                "standby", "limit_export", "set_power_w"):
        assert caps[cap] is True, cap


@pytest.mark.asyncio
async def test_exception_2_confirmed_after_a_separator_read(module):
    await _probe(module)
    reads = [(a, n) for fc, a, n in module.log if fc == 0x03]
    i = reads.index((47760, 1))
    # wyjątek 2 → blok innej długości z poprawną odpowiedzią → wyjątek 2 ponownie
    assert reads[i + 2] == (47760, 1) and reads[i + 1][1] != 1


@pytest.mark.asyncio
async def test_consecutive_probe_reads_never_have_equal_length(module):
    await _probe(module)
    counts = [n for fc, _, n in module.log if fc == 0x03]
    assert all(a != b for a, b in zip(counts, counts[1:]))


@pytest.mark.asyncio
@pytest.mark.parametrize("at", range(1, 12))
@pytest.mark.parametrize("replays", [1, 2, 3])
async def test_stale_replies_never_switch_a_legal_key_off(module, at, replays):
    module.plan = ["ok"] * at + ["replay"] * replays
    rep = await _probe(module)
    for key in LEGAL:
        # True albo bez werdyktu (próba przerwana) — nigdy wyłączony ani „nieczytelny”
        assert rep.capabilities.get(key) is not False and key not in rep.echo_only, key
    assert direct_capabilities(GW, rep.capabilities, rep.unreadable)["set_soc_ceiling"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("at", range(1, 7))                       # każdy odczyt sondy po identyfikacji
async def test_single_exception_2_anywhere_switches_nothing_off(at):
    m = await flaky_module(RegisterBank(goodwe_words()))           # wariant z czytelnym 47760
    m.plan = ["ok"] * at + ["exc2"]                                # jeden wyjątek 2 w odczycie nr at+1
    try:
        rep = await _probe(m)
    finally:
        await m.close()
    assert "exc2" in m.actions
    for key in (*LEGAL, "soc_max"):
        assert rep.capabilities.get(key) is not False, key
    assert rep.capabilities == {k: True for k in (*LEGAL, "soc_max")} or "unconfirmed" in rep.errors
