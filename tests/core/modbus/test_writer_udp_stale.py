"""Pisarz na GoodWe UDP wobec modułu Wi-Fi z zachowaniami zmierzonymi na żywo: poprzednia odpowiedź
zamiast bieżącej, obce ramki innych klientów, odpowiedź po limicie czasu, nastawa widoczna z opóźnieniem.

Wymagania: nieaktualna odpowiedź nigdy nie daje OK zapisu, który nie doszedł, nigdy UNSUPPORTED
rejestru, który istnieje, nigdy DENIED zapisu stosowanego z opóźnieniem; ramka zapisu najwyżej raz.
"""
import pytest
import pytest_asyncio

from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.transports.base import TransportConfig
from custom_components.volcast.core.transports.factory import make_transport
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, UNSUPPORTED
from tests.sim.device import RegisterBank
from tests.sim.fixtures import goodwe_words
from tests.sim.flaky import flaky_module

GOLDEN_EXPORT_LIMIT = 16000                 # 47510 w nagraniu


async def _module(**kw):
    # GW8KN-ET: 47760 (soc_max) odpowiada wyjątkiem 2
    return await flaky_module(RegisterBank(goodwe_words(), unsupported=kw.pop("unsupported", (47760,))), **kw)


@pytest_asyncio.fixture
async def module():
    m = await _module()
    yield m
    await m.close()


def _writer_for(m, profile):
    t = make_transport(TransportConfig(kind="goodwe_udp", host=m.host, port=m.port, unit=0xF7,
                                       timeout_s=0.1, gap_s=0.0, read_tries=2), allow_loopback=True)
    return RegisterWriter(RegisterClient(t, profile), profile)


@pytest_asyncio.fixture
async def writer(module, goodwe_profile):
    w = _writer_for(module, goodwe_profile)
    yield w
    await w.client.transport.close()


def _writes(m, addr=None):
    return [e for e in m.log if e[0] == 0x06 and (addr is None or e[1] == addr)]


# ── (1) poprzednia odpowiedź zamiast bieżącej ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("skip", range(6))
@pytest.mark.parametrize("replays", [1, 2, 3])
async def test_stale_replies_never_report_ok_for_a_write_that_did_not_happen(module, writer, skip, replays):
    # Poprzedni odczyt niósł 3000 (odczyt zwrotny mocy) — nieaktualna kopia tej ramki nie może
    # udawać ani wartości sprzed zapisu, ani odczytu zwrotnego limitu eksportu.
    assert await writer.async_write(RegisterWrite("power_w", 47512, 3000)) == OK
    module.bank.ignore_writes.add(47510)
    module.plan = ["ok"] * skip + ["replay"] * replays
    out = await writer.async_write(RegisterWrite("export_limit_w", 47510, 3000))
    assert out in (DENIED, ERROR)
    assert module.bank.read(47510, 1) == [GOLDEN_EXPORT_LIMIT] and len(_writes(module, 47510)) <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replays", [1, 2, 3])
async def test_stale_pre_read_never_skips_a_needed_restore(module, writer, replays):
    assert await writer.async_write(RegisterWrite("power_w", 47512, 3000)) == OK
    module.bank.ignore_writes.add(47510)
    module.plan = ["replay"] * replays
    assert await writer.async_write_restore(RegisterWrite("export_limit_w", 47510, 3000)) != OK


@pytest.mark.asyncio
@pytest.mark.parametrize("replays", [1, 2, 3])
async def test_stale_exception_never_makes_a_legal_register_unsupported(module, writer, replays):
    assert await writer.async_write(RegisterWrite("soc_max", 47760, 95)) == UNSUPPORTED    # prawdziwy wyjątek 2
    module.plan = ["replay"] * replays                    # potem jego nieaktualne kopie
    out = await writer.async_write(RegisterWrite("export_limit_w", 47510, 3000))
    assert out != UNSUPPORTED
    if replays == 1:
        assert out == OK                                  # kopia trafiła w blok — rejestr czytany osobno
    assert _writes(module, 47760) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lag", [1, 2])
async def test_delayed_apply_is_never_denied(module, writer, lag):
    module.apply_lag = lag                                # pierwsze odczyty po zapisie: wartość sprzed
    assert await writer.async_write(RegisterWrite("soc_min", 45356, 20)) == OK
    assert len(_writes(module)) == 1 and module.bank.read(45356, 1) == [20]


@pytest.mark.asyncio
async def test_delayed_apply_with_stale_readback_is_ok(module, writer):
    module.apply_lag = 1
    module.plan = ["ok", "ok", "replay", "replay"]        # odczyty zwrotne dostają kopię odczytu przed
    assert await writer.async_write(RegisterWrite("soc_min", 45356, 20)) == OK
    assert len(_writes(module)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("late_at", [0, 1, 2])
async def test_reply_later_than_timeout_is_retried_not_misread(module, writer, late_at):
    module.late_s = 0.15
    module.plan = ["ok"] * late_at + ["late"]
    assert await writer.async_write(RegisterWrite("mode", 47511, 10)) == OK
    assert len(_writes(module)) == 1 and module.bank.read(47511, 1) == [10]


@pytest.mark.asyncio
async def test_foreign_frames_of_other_lengths_do_not_disturb(module, writer):
    module.foreign = {i: (45, 125, 24) for i in range(1, 12)}
    assert await writer.async_write(RegisterWrite("power_w", 47512, 1500)) == OK
    assert len(_writes(module)) == 1


@pytest.mark.asyncio
async def test_foreign_block_of_the_same_length_makes_pre_reads_disagree(module, writer):
    # Box czyta blok DOD 45353×4 — jego odpowiedź ma długość bloku EMS; obca ramka przyjęta za
    # pierwszy odczyt przed zapisem nie zgadza się z drugim (innej długości) → nic nie wysłano.
    module.foreign = {1: (4,)}
    assert await writer.async_write(RegisterWrite("power_w", 47512, 1500)) == ERROR
    assert _writes(module) == []


# ── (2) wyjątek 2 tylko potwierdzony ──────────────────────────────────────


@pytest.mark.asyncio
async def test_single_exception_2_then_value_is_not_unsupported(goodwe_profile):
    m = await _module(unsupported=())
    w = _writer_for(m, goodwe_profile)
    try:
        m.plan = ["exc2"]
        assert await w.async_write(RegisterWrite("soc_max", 47760, 95)) == ERROR
        assert _writes(m) == []
        assert await w.async_write(RegisterWrite("soc_max", 47760, 95)) == OK       # następny cykl
    finally:
        await w.client.transport.close()
        await m.close()


@pytest.mark.asyncio
async def test_two_exceptions_2_on_a_block_key_are_not_unsupported(module, writer):
    module.plan = ["exc2", "exc2"]                        # blok EMS, potem sam rejestr
    assert await writer.async_write(RegisterWrite("export_limit_w", 47510, 3000)) == ERROR
    assert _writes(module) == []


@pytest.mark.asyncio
async def test_confirmed_exception_2_is_unsupported_and_nothing_written(module, writer):
    assert await writer.async_write(RegisterWrite("soc_max", 47760, 95)) == UNSUPPORTED
    assert _writes(module) == []
    reads = [(a, n) for fc, a, n in module.log if fc == 0x03]
    # dwa odczyty rejestru rozdzielone blokiem innej długości z poprawną odpowiedzią
    assert reads[0] == (47760, 1) and reads[-1] == (47760, 1)
    assert len(reads) == 3 and reads[1][1] != 1


@pytest.mark.asyncio
async def test_consecutive_writer_reads_never_have_equal_length(module, writer):
    for key, addr, value in (("soc_min", 45356, 20), ("soc_max", 47760, 95), ("power_w", 47512, 3000),
                             ("export_limit_w", 47510, 3000), ("export_limit_enabled", 47509, 1),
                             ("mode", 47511, 10)):
        await writer.async_write(RegisterWrite(key, addr, value))
    counts = [n for fc, _, n in module.log if fc == 0x03]
    assert all(a != b for a, b in zip(counts, counts[1:]))
    assert len(_writes(module)) == 5                      # bez soc_max (wyjątek 2)
