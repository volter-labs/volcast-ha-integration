"""Pisarz rejestrów: echo + odczyt zwrotny (tabela wyników), budżet ramek, tryb próbny."""
import pytest

from custom_components.volcast.core.modbus.writer import NoWriteWriter, RegisterWriter
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.transports.base import LinkDown, TransportError
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, UNSUPPORTED


# ── tabela: echo × odczyt zwrotny ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_echo_and_readback_equal_is_ok(goodwe_writer, goodwe_bank):
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == OK
    assert goodwe_bank.read(47511, 1) == [10]


@pytest.mark.asyncio
async def test_readback_differs_is_denied(goodwe_writer, goodwe_bank):
    goodwe_bank.clamp[47512] = (0, 5000)
    assert await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 8000)) == DENIED


@pytest.mark.asyncio
async def test_echo_ok_but_readback_missing_is_error(goodwe_writer, goodwe_bank):
    goodwe_bank.unreadable.add(47512)             # zapis przyjęty, rejestru nie da się odczytać
    assert await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 3000)) == ERROR


@pytest.mark.asyncio
async def test_exception_2_is_unsupported(goodwe_writer, goodwe_bank, goodwe_udp_sim):
    goodwe_bank.unsupported.add(47760)
    assert await goodwe_writer.async_write(RegisterWrite("soc_max", 47760, 95)) == UNSUPPORTED
    assert goodwe_udp_sim.requests == 1           # bez odczytu zwrotnego


@pytest.mark.asyncio
async def test_exception_4_readback_equal_is_ok_unknown_is_error(goodwe_writer, goodwe_bank, sim_faults):
    sim_faults.exception_on_write = {47511: 4}       # symulator: zapisz, ale odpowiedz wyjątkiem 4
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == OK
    sim_faults.exception_on_write = {47511: 4}
    goodwe_bank.ignore_writes.add(47511)
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 11)) == DENIED
    sim_faults.exception_on_write = {47511: 4}
    sim_faults.drop_next = 9                          # odczyt zwrotny niemożliwy
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 12)) == ERROR


@pytest.mark.asyncio
async def test_exception_other_than_2_with_readback_missing_is_error(goodwe_writer, goodwe_bank, sim_faults):
    goodwe_bank.unreadable.add(47512)
    sim_faults.exception_on_write = {47512: 6}        # urządzenie zajęte — i tak mogło zapisać
    assert await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 3000)) == ERROR


@pytest.mark.asyncio
async def test_lost_echo_but_readback_equal_is_ok(goodwe_writer, sim_faults):
    sim_faults.mute_write_echo = 2
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == OK


@pytest.mark.asyncio
async def test_lost_echo_and_readback_old_is_error(goodwe_writer, goodwe_bank, sim_faults):
    goodwe_bank.ignore_writes.add(47511)
    sim_faults.mute_write_echo = 2
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == ERROR


@pytest.mark.asyncio
async def test_lost_echo_and_no_readback_is_error(goodwe_writer, goodwe_udp_sim, sim_faults):
    sim_faults.drop_next = 9                          # ani echa, ani odczytu zwrotnego
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == ERROR
    assert [e[0] for e in goodwe_udp_sim.log].count(0x06) == 2


@pytest.mark.asyncio
async def test_wrong_echo_value_then_readback_decides(goodwe_writer, goodwe_bank, sim_faults):
    # Echo z inną wartością nigdy nie jest potwierdzeniem; rozstrzyga odczyt zwrotny.
    sim_faults.wrong_echo_value = True
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == OK
    goodwe_bank.ignore_writes.add(47511)
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 12)) == ERROR


@pytest.mark.asyncio
async def test_readback_after_timeout_uses_fresh_channel(goodwe_writer, goodwe_bank, sim_faults):
    # Echo obu wysyłek spóźnia się ponad limit czasu; odczyt zwrotny idzie nowym gniazdem,
    # więc spóźnione echa go nie dosięgną (żadnej obcej ramki na nowym kanale).
    sim_faults.delay_s = 0.25
    sim_faults.delay_only_next = 2
    t = goodwe_writer.client.transport
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == OK
    assert t.stats.channel_resets >= 2 and t.stats.stray == 0
    assert goodwe_bank.read(47511, 1) == [10]


@pytest.mark.asyncio
async def test_readback_compares_whole_word_for_bit_fields(deye_writer, deye_bank):
    deye_bank.poke(172, 0b10)                         # bit 1 ustawiony przez właściciela
    assert await deye_writer.async_write(RegisterWrite("tou.1.grid_charge", 172, 0b11)) == OK
    assert deye_bank.read(172, 1) == [0b11]
    deye_bank.poke(173, 0b10)
    deye_bank.clamp[173] = (0, 1)                      # urządzenie gubi bit właściciela
    assert await deye_writer.async_write(RegisterWrite("tou.2.grid_charge", 173, 0b11)) == DENIED


@pytest.mark.asyncio
async def test_fc16_used_for_deye(deye_writer, rtu_tcp_sim):
    assert await deye_writer.async_write(RegisterWrite("tou.1.start", 148, 130)) == OK
    writes = [e for e in rtu_tcp_sim.log if e[0] != 0x03]
    assert writes == [(0x10, 148, 1)]


@pytest.mark.asyncio
async def test_fc6_used_for_goodwe(goodwe_writer, goodwe_udp_sim):
    assert await goodwe_writer.async_write(RegisterWrite("soc_min", 45356, 20)) == OK
    writes = [e for e in goodwe_udp_sim.log if e[0] != 0x03]
    assert writes == [(0x06, 45356, 20)]


# ── błędy transportu nigdy nie wychodzą z pisarza ─────────────────────────


class _Boom:
    kind = "modbus_tcp"

    def __init__(self, exc):
        self.exc = exc
        self.reads = 0

    async def write(self, addr, values, *, function, on_send=None):
        raise self.exc

    async def read(self, addr, count):
        self.reads += 1
        raise self.exc


@pytest.mark.asyncio
async def test_transport_exception_is_error_not_raised(goodwe_profile):
    from custom_components.volcast.core.modbus.client import RegisterClient
    for exc in (RuntimeError("x"), OSError("y"), TransportError("z")):
        w = RegisterWriter(RegisterClient(_Boom(exc), goodwe_profile), goodwe_profile)
        assert await w.async_write(RegisterWrite("mode", 47511, 10)) == ERROR


@pytest.mark.asyncio
async def test_link_down_is_error(goodwe_profile):
    from custom_components.volcast.core.modbus.client import RegisterClient
    boom = _Boom(LinkDown("down"))
    w = RegisterWriter(RegisterClient(boom, goodwe_profile), goodwe_profile)
    assert await w.async_write(RegisterWrite("mode", 47511, 10)) == ERROR
    assert boom.reads == 1                            # odczyt zwrotny spróbowany (zapis mógł dojść)


@pytest.mark.asyncio
async def test_failing_on_send_callback_does_not_break_write(goodwe_client, goodwe_profile):
    def bad(_key):
        raise RuntimeError("counter broken")
    w = RegisterWriter(goodwe_client, goodwe_profile, on_send=bad)
    assert await w.async_write(RegisterWrite("mode", 47511, 10)) == OK


# ── klucze tylko z echem, licznik ramek, tryb próbny ──────────────────────


@pytest.mark.asyncio
async def test_echo_only_key_not_sent(goodwe_client, goodwe_profile, goodwe_bank):
    w = RegisterWriter(goodwe_client, goodwe_profile)
    w.echo_only = frozenset({"soc_max"})
    assert await w.async_write(RegisterWrite("soc_max", 47760, 95)) == UNSUPPORTED
    assert goodwe_bank.writes == []


@pytest.mark.asyncio
async def test_on_send_counts_every_frame(goodwe_client, goodwe_profile, sim_faults):
    sent = []
    sim_faults.mute_write_echo = 1
    w = RegisterWriter(goodwe_client, goodwe_profile, on_send=sent.append)
    await w.async_write(RegisterWrite("mode", 47511, 10))
    assert sent == ["mode", "mode"]


@pytest.mark.asyncio
async def test_on_send_not_called_for_echo_only(goodwe_client, goodwe_profile):
    sent = []
    w = RegisterWriter(goodwe_client, goodwe_profile, on_send=sent.append)
    w.echo_only = frozenset({"soc_max"})
    await w.async_write(RegisterWrite("soc_max", 47760, 95))
    assert sent == []


@pytest.mark.asyncio
async def test_no_write_writer_never_sends(goodwe_client, goodwe_bank, caplog):
    w = NoWriteWriter()
    with caplog.at_level("ERROR"):
        assert await w.async_write(RegisterWrite("mode", 47511, 10)) == DENIED
    assert goodwe_bank.writes == [] and w.blocked_attempts == 1
    assert "mode" in caplog.text and "47511" not in caplog.text and " 10" not in caplog.text
