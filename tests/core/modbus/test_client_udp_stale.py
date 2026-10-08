"""Odpytywanie (read_state) na GoodWe UDP wobec modułu Wi-Fi z poprzednią odpowiedzią zamiast bieżącej:
kolejność bloków, wyjątek 2 bloku nie z jednej ramki, podział bloku bez przesuniętych wartości."""
import asyncio

import pytest
import pytest_asyncio

from custom_components.volcast.core.modbus.blocks import read_plan
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.views import poll_order
from custom_components.volcast.core.transports.base import TransportConfig
from custom_components.volcast.core.transports.factory import make_transport
from tests.sim.device import RegisterBank
from tests.sim.fixtures import goodwe_words
from tests.sim.flaky import flaky_module

GOLDEN = {"power_w": 8846.0, "export_limit_w": 16000.0, "export_limit_enabled": 0.0}
EXPECTED = {**GOLDEN, "mode": "charge_battery"}           # 47511 = 11 w nagraniu


async def _setup(unsupported=(47760,), **kw):
    m = await flaky_module(RegisterBank(goodwe_words(), unsupported=unsupported))
    t = make_transport(TransportConfig(kind="goodwe_udp", host=m.host, port=m.port, unit=0xF7,
                                       timeout_s=0.1, gap_s=0.0, read_tries=2), allow_loopback=True)
    return m, t


@pytest_asyncio.fixture
async def link():
    m, t = await _setup()
    yield m, t
    await t.close()
    await m.close()


def _reads(m, start=0):
    return [(a, n) for fc, a, n in m.log[start:] if fc == 0x03]


# ── (b) kolejność bloków ──────────────────────────────────────────────────


def test_poll_order_no_equal_neighbours_and_unproven_single_last(goodwe_profile):
    plan = read_plan(goodwe_profile)
    out = poll_order(goodwe_profile, plan)
    assert sorted(out) == sorted(plan)
    assert out[-1] == (47760, 1)                  # rejestr zapisu bez znanego bloku — na końcu cyklu
    counts = [n for _, n in out]
    assert all(a != b for a, b in zip(counts, counts[1:]))
    assert counts[0] != 1                         # po ostatnim (pojedynczym) odczycie poprzedniego cyklu


def test_poll_order_without_unproven_singles(goodwe_profile):
    plan = read_plan(goodwe_profile, exclude={"soc_max"})
    counts = [n for _, n in poll_order(goodwe_profile, plan)]
    assert all(a != b for a, b in zip(counts, counts[1:]))


@pytest.mark.asyncio
async def test_read_state_uses_the_order_on_udp(link, goodwe_profile):
    m, t = link
    client = RegisterClient(t, goodwe_profile)
    await client.read_state()
    reads = _reads(m)
    order = poll_order(goodwe_profile, read_plan(goodwe_profile))
    assert reads[:len(order)] == order
    assert all(a[1] != b[1] for a, b in zip(reads, reads[1:]))


@pytest.mark.asyncio
@pytest.mark.parametrize("poll_block,writer_block", [((35140, 1), (47512, 1)), ((45356, 1), (47512, 1)),
                                                     ((47509, 4), (45353, 4))])
async def test_writer_read_between_poll_length_check_and_read_is_never_taken(link, goodwe_profile,
                                                                             poll_block, writer_block):
    # Sekwencja pisarza (sesja na wyłączność, odczyt tej samej długości) wchodzi między decyzję
    # odpytywania o długości a jego odczyt; moduł raz powtarza poprzednią odpowiedź.
    m, t = link
    client = RegisterClient(t, goodwe_profile)
    orig = t.read
    holding = asyncio.Event()

    async def writer():
        async with t.exclusive():
            holding.set()
            await orig(*writer_block)
            m.plan = ["replay"]                       # następny odczyt dostaje odpowiedź pisarza

    async def read(addr, count, **kw):
        if (addr, count) == poll_block and not holding.is_set():
            asyncio.ensure_future(writer())
            try:
                await asyncio.wait_for(holding.wait(), 0.05)
            except TimeoutError:
                pass                                  # łącze trzyma odpytywanie — pisarz poczeka
        return await orig(addr, count, **kw)
    t.read = read
    r = await client.read_state()
    await asyncio.sleep(0.3)
    assert holding.is_set()
    assert r.values["active_power_w"] == -2270 and r.values["soc_min"] == 5
    for key, v in EXPECTED.items():
        assert r.device[key] == v


# ── (a) klucze nieobsługiwane nie są odpytywane — test w test_direct_connection ──


# ── (c) wyjątek 2 bloku: z jednej ramki nie ma werdyktu ───────────────────


@pytest.mark.asyncio
async def test_stale_exception_at_cycle_start_is_retried_not_split(link, goodwe_profile):
    m, t = link
    client = RegisterClient(t, goodwe_profile)
    await client.read_state()                     # cykl kończy wyjątek 2 rejestru 47760
    start = len(m.log)
    m.plan = ["replay"]                           # pierwszy blok następnego cyklu dostaje ten wyjątek
    r = await client.read_state()
    for key, v in GOLDEN.items():
        assert r.device[key] == v
    assert r.values["pv_power_w"] == 828 and r.values["soc"] == 83
    # bez podziału na pojedyncze rejestry (blok ponowiony po bloku rozdzielającym)
    assert not [r for r in _reads(m, start) if r[1] == 1 and 47509 <= r[0] <= 47512]


@pytest.mark.asyncio
async def test_exception_on_ems_block_once_keeps_ems_values(link, goodwe_profile):
    m, t = link
    client = RegisterClient(t, goodwe_profile)
    order = poll_order(goodwe_profile, read_plan(goodwe_profile))
    m.plan = ["ok"] * order.index((47509, 4)) + ["exc2"]
    r = await client.read_state()
    for key, v in GOLDEN.items():
        assert r.device[key] == v
    assert (47511, 1) not in _reads(m)


async def _split_cycle(goodwe_profile, foreign_at=None, *, warm=True):
    """Cykl 1 dobry (`warm`); w cyklu 2 (albo pierwszym) blok EMS dwa razy odpowiada wyjątkiem 2
    (podział na rejestry)."""
    m, t = await _setup()
    client = RegisterClient(t, goodwe_profile)
    try:
        if warm:
            await client.read_state()
        start = len(m.log)
        order = poll_order(goodwe_profile, read_plan(goodwe_profile))
        i = order.index((47509, 4))
        m.plan = ["ok"] * i + ["exc2", "ok", "exc2"]     # blok, blok rozdzielający, blok ponowiony
        if foreign_at is not None:
            m.foreign = {start + foreign_at + 1: (1,)}   # obca ramka 1 słowa przed odpowiedzią
        m.foreign_word = 1234
        r = await client.read_state()
        return r, m.log[start:]
    finally:
        await t.close()
        await m.close()


@pytest.mark.asyncio
async def test_split_reads_never_neighbour_with_equal_length(goodwe_profile):
    r, log = await _split_cycle(goodwe_profile)
    counts = [n for fc, _, n in log if fc == 0x03]
    assert all(a != b for a, b in zip(counts, counts[1:]))
    for key, v in GOLDEN.items():
        assert r.device[key] == v                         # wartości z podziału, bez przesunięć


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [True, False])         # False: pierwszy cykl, bez wartości odniesienia
async def test_split_value_disagreeing_with_previous_needs_confirmation(goodwe_profile, warm):
    _, dry = await _split_cycle(goodwe_profile, warm=warm)
    first_split = next(i for i, (fc, a, n) in enumerate(dry) if fc == 0x03 and n == 1 and 47509 <= a <= 47512)
    r, log = await _split_cycle(goodwe_profile, foreign_at=first_split, warm=warm)
    key = {47509: "export_limit_enabled", 47510: "export_limit_w", 47511: "mode", 47512: "power_w"}[dry[first_split][1]]
    # obca wartość (1234) inna niż poprzednia — drugi odczyt jej nie potwierdza: brak wartości, nie 1234
    assert r.device.get(key) in (None, EXPECTED[key])
    assert sum(1 for fc, a, n in log if (fc, a, n) == dry[first_split]) == 2
