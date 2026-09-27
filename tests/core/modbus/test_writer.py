"""Pisarz rejestrów: echo + odczyt zwrotny (tabela wyników), budżet ramek, tryb próbny."""
import pytest

from custom_components.volcast.core.modbus.writer import NoWriteWriter, RegisterWriter
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.transports.base import LinkDown, TransportError
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, OK_ADJUSTED, UNSUPPORTED


# ── tabela: echo × odczyt zwrotny ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_echo_and_readback_equal_is_ok(goodwe_writer, goodwe_bank):
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == OK
    assert goodwe_bank.read(47511, 1) == [10]


@pytest.mark.asyncio
async def test_readback_unchanged_is_denied(goodwe_writer, goodwe_bank):
    goodwe_bank.ignore_writes.add(47512)             # echo, ale rejestr bez zmian
    assert await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 3000)) == DENIED


@pytest.mark.asyncio
async def test_clamped_value_is_ok_adjusted_with_actual(goodwe_writer, goodwe_bank, caplog):
    goodwe_bank.clamp[47512] = (0, 5000)             # zmieniony, ale nie na zamówioną wartość
    with caplog.at_level("WARNING"):
        out = await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 8000))
    assert out == OK_ADJUSTED and out.actual == 5000.0
    assert "power_w" in caplog.text and "8000" not in caplog.text


@pytest.mark.asyncio
async def test_mode_changed_to_other_value_is_error(goodwe_writer, goodwe_bank):
    goodwe_bank.clamp[47511] = (0, 2)                 # tryb inny niż zamówiony i inny niż przed
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == ERROR


@pytest.mark.asyncio
async def test_pre_read_failure_sends_nothing(goodwe_writer, goodwe_bank, goodwe_udp_sim):
    # Rejestr bez odczytu (47760 odpowiada ramką złej długości): nic nie jest wysyłane, a wynik
    # to ERROR — chwilowa awaria nie jest odmową urządzenia (odmowa blokowałaby ponowienie).
    assert await goodwe_writer.async_write(RegisterWrite("soc_max", 47760, 95)) == ERROR
    assert goodwe_bank.writes == [] and all(fc == 0x03 for fc, _, _ in goodwe_udp_sim.log)


@pytest.mark.asyncio
async def test_pre_read_exception_2_is_unsupported_without_write(goodwe_writer, goodwe_bank):
    goodwe_bank.unsupported.add(47510)
    assert await goodwe_writer.async_write(RegisterWrite("export_limit_w", 47510, 100)) == UNSUPPORTED
    assert goodwe_bank.writes == []


@pytest.mark.asyncio
async def test_echo_ok_but_readback_missing_is_error(goodwe_writer, sim_faults):
    sim_faults.drop_after_write = 10              # zapis przyjęty, odczyt zwrotny ginie
    assert await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 3000)) == ERROR


@pytest.mark.asyncio
async def test_exception_2_is_unsupported(goodwe_writer, goodwe_bank, goodwe_udp_sim):
    goodwe_bank.readonly.add(47510)
    assert await goodwe_writer.async_write(RegisterWrite("export_limit_w", 47510, 100)) == UNSUPPORTED
    assert goodwe_udp_sim.requests == 2           # odczyt przed zapisem + zapis, bez odczytu zwrotnego


@pytest.mark.asyncio
async def test_exception_4_readback_equal_is_ok_unknown_is_error(goodwe_writer, goodwe_bank, sim_faults):
    sim_faults.exception_on_write = {47511: 4}       # symulator: zapisz, ale odpowiedz wyjątkiem 4
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == OK
    sim_faults.exception_on_write = {47511: 4}
    goodwe_bank.ignore_writes.add(47511)
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 11)) == DENIED
    sim_faults.exception_on_write = {47511: 4}
    sim_faults.drop_after_write = 9                   # wyjątek 4 dochodzi, odczyt zwrotny ginie
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 12)) == ERROR


@pytest.mark.asyncio
async def test_exception_5_with_unchanged_register_is_error(goodwe_writer, goodwe_bank, sim_faults):
    # 5 = „przyjęte, w trakcie”: rejestr jeszcze stary to nie odmowa — zapis może dojść.
    goodwe_bank.ignore_writes.add(47511)
    sim_faults.exception_on_write = {47511: 5}
    assert await goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)) == ERROR


@pytest.mark.asyncio
async def test_exception_other_than_2_with_readback_missing_is_error(goodwe_writer, sim_faults):
    sim_faults.drop_after_write = 9
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
    sim_faults.mute_write_echo = 2
    sim_faults.drop_after_write = 20                  # ani echa, ani odczytu zwrotnego
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
    sim_faults.delay_writes_only = True
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
    assert await deye_writer.async_write(RegisterWrite("tou.2.grid_charge", 173, 0b11)) == ERROR


@pytest.mark.asyncio
async def test_fc16_used_for_deye(deye_writer, rtu_tcp_sim):
    assert await deye_writer.async_write(RegisterWrite("tou.1.start", 148, 130)) == OK
    writes = [e for e in rtu_tcp_sim.log if e[0] != 0x03]
    assert writes == [(0x10, 148, 1)]
    assert [e[0] for e in rtu_tcp_sim.log] == [0x03, 0x10, 0x03]      # odczyt przed, zapis, odczyt zwrotny


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
        self.writes = 0

    async def write(self, addr, values, *, function, on_send=None):
        self.writes += 1
        raise self.exc

    async def read(self, addr, count, *, tries=None):
        self.reads += 1
        if self.reads == 1:
            return [0] * count                        # odczyt przed zapisem działa
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
    assert boom.reads == 2 and boom.writes == 1       # odczyt zwrotny spróbowany (zapis mógł dojść)


@pytest.mark.asyncio
async def test_failing_on_send_callback_does_not_break_write(goodwe_client, goodwe_profile):
    def bad(_key):
        raise RuntimeError("counter broken")
    w = RegisterWriter(goodwe_client, goodwe_profile, on_send=bad)
    assert await w.async_write(RegisterWrite("mode", 47511, 10)) == OK


# ── klucze tylko z echem, licznik ramek, tryb próbny ──────────────────────


@pytest.mark.asyncio
async def test_unreadable_key_not_sent(goodwe_client, goodwe_profile, goodwe_bank, goodwe_udp_sim):
    w = RegisterWriter(goodwe_client, goodwe_profile, unreadable={"soc_max"})
    assert await w.async_write(RegisterWrite("soc_max", 47760, 95)) == UNSUPPORTED
    assert goodwe_bank.writes == [] and goodwe_udp_sim.requests == 0       # ani odczytu, ani zapisu


@pytest.mark.asyncio
async def test_unseeded_writer_never_writes_unreadable_register(goodwe_client, goodwe_profile, goodwe_bank):
    w = RegisterWriter(goodwe_client, goodwe_profile)
    assert w.echo_only == frozenset() and w.unreadable == frozenset()
    assert await w.async_write(RegisterWrite("soc_max", 47760, 95)) == ERROR
    assert goodwe_bank.writes == []


@pytest.mark.asyncio
async def test_profile_echo_only_key_written_on_echo_alone(goodwe_client, goodwe_bank, goodwe_udp_sim):
    from dataclasses import replace
    from custom_components.volcast.core.profile import load_builtin
    gw = load_builtin("goodwe-et")
    gw = replace(gw, modbus=replace(gw.modbus, echo_only=("soc_max",)))
    w = RegisterWriter(goodwe_client, gw)
    assert await w.async_write(RegisterWrite("soc_max", 47760, 95)) == OK
    assert goodwe_bank.writes == [(47760, 95)] and all(fc != 0x03 for fc, _, _ in goodwe_udp_sim.log)


@pytest.mark.asyncio
async def test_tou_keys_match_by_prefix(deye_client, deye_profile, deye_bank):
    w = RegisterWriter(deye_client, deye_profile, unreadable={"tou"})
    for key, addr in (("tou.1.start", 148), ("tou.6.grid_charge", 177), ("tou_enable", 146)):
        assert await w.async_write(RegisterWrite(key, addr, 1)) == UNSUPPORTED
    assert deye_bank.writes == []


@pytest.mark.asyncio
async def test_address_or_value_outside_profile_refused(goodwe_writer, goodwe_udp_sim):
    for w in (RegisterWrite("mode", 47512, 10), RegisterWrite("mode", 47511, 70000),
              RegisterWrite("mode", 47511, -1), RegisterWrite("nope", 1, 1), RegisterWrite("tou.1.start", 148, 1)):
        assert await goodwe_writer.async_write(w) == ERROR      # nic nie wysłano — to nie odmowa urządzenia
    assert goodwe_udp_sim.requests == 0


@pytest.mark.asyncio
async def test_concurrent_writes_do_not_interleave(goodwe_writer, goodwe_udp_sim):
    import asyncio
    a, b = await asyncio.gather(goodwe_writer.async_write(RegisterWrite("mode", 47511, 10)),
                                goodwe_writer.async_write(RegisterWrite("power_w", 47512, 3000)))
    assert (a, b) == (OK, OK)
    seq = [(fc, addr) for fc, addr, _ in goodwe_udp_sim.log]
    assert seq == [(3, 47511), (6, 47511), (3, 47511), (3, 47512), (6, 47512), (3, 47512)]


@pytest.mark.asyncio
async def test_on_send_counts_every_frame(goodwe_client, goodwe_profile, sim_faults):
    sent = []
    sim_faults.mute_write_echo = 1
    w = RegisterWriter(goodwe_client, goodwe_profile, on_send=sent.append)
    await w.async_write(RegisterWrite("mode", 47511, 10))
    assert sent == ["mode", "mode"]


@pytest.mark.asyncio
async def test_on_send_not_called_when_nothing_sent(goodwe_client, goodwe_profile):
    sent = []
    w = RegisterWriter(goodwe_client, goodwe_profile, on_send=sent.append, unreadable={"soc_max"})
    await w.async_write(RegisterWrite("soc_max", 47760, 95))
    await w.async_write(RegisterWrite("power_w", 47760, 95))     # zły adres
    assert sent == []


@pytest.mark.asyncio
async def test_no_write_writer_never_sends(goodwe_client, goodwe_bank, caplog):
    w = NoWriteWriter()
    with caplog.at_level("ERROR"):
        assert await w.async_write(RegisterWrite("mode", 47511, 10)) == DENIED
    assert goodwe_bank.writes == [] and w.blocked_attempts == 1
    assert "mode" in caplog.text and "47511" not in caplog.text and " 10" not in caplog.text


# ── odchylenie w stronę groźną, pola bitowe na świeżym słowie ─────────────


@pytest.mark.asyncio
async def test_power_raised_above_request_is_error(goodwe_writer, goodwe_bank):
    goodwe_bank.clamp[47512] = (3000, 65535)           # minimalna moc urządzenia ponad zamówioną
    assert await goodwe_writer.async_write(RegisterWrite("power_w", 47512, 1000)) == ERROR


@pytest.mark.asyncio
async def test_floor_raised_is_adjusted_lowered_is_error(goodwe_writer, goodwe_bank):
    goodwe_bank.clamp[45356] = (30, 100)
    out = await goodwe_writer.async_write(RegisterWrite("soc_min", 45356, 20))
    assert out == OK_ADJUSTED and out.actual == 30.0          # wyższy próg dolny = bezpieczniej
    goodwe_bank.clamp[45356] = (0, 10)
    assert await goodwe_writer.async_write(RegisterWrite("soc_min", 45356, 25)) == ERROR


@pytest.mark.asyncio
async def test_bit_field_uses_fresh_pre_write_word(deye_writer, deye_bank):
    # Wartość zakodowana ze starego odpytania (bit 1 nieznany); właściciel ustawił bit 2 w międzyczasie.
    deye_bank.poke(172, 0b100)
    assert await deye_writer.async_write(RegisterWrite("tou.1.grid_charge", 172, 0b01)) == OK
    assert deye_bank.read(172, 1) == [0b101]


@pytest.mark.asyncio
async def test_enable_bit_on_fresh_word_keeps_or_fills_days(deye_writer, deye_bank):
    deye_bank.poke(146, 0b0111110)                     # dni właściciela, włącznik OFF
    assert await deye_writer.async_write(RegisterWrite("tou_enable", 146, 0x01)) == OK
    assert deye_bank.read(146, 1) == [0b0111111]
    assert await deye_writer.async_write(RegisterWrite("tou_enable", 146, 0x00)) == OK
    assert deye_bank.read(146, 1) == [0b0111110]       # OFF nie rusza dni
    deye_bank.poke(146, 0)
    assert await deye_writer.async_write(RegisterWrite("tou_enable", 146, 0x01)) == OK
    assert deye_bank.read(146, 1) == [0xFF]            # bez dni → cały tydzień


# ── odmowa (DENIED) tylko po prawdziwej odmowie urządzenia ────────────────


class _PreReadFails:
    kind = "goodwe_udp"

    def __init__(self, exc):
        self.exc = exc
        self.writes = 0

    async def read(self, addr, count, *, tries=None):
        raise self.exc

    async def write(self, addr, values, *, function, on_send=None):
        self.writes += 1


@pytest.mark.asyncio
async def test_pre_read_transport_failure_is_error_not_denied(goodwe_profile):
    from custom_components.volcast.core.modbus.client import RegisterClient
    from custom_components.volcast.core.transports.base import InverterAsleep, ModbusException, RequestTimeout
    for exc in (LinkDown("down"), RequestTimeout(silent=True), InverterAsleep("zz"), ModbusException(6)):
        t = _PreReadFails(exc)
        w = RegisterWriter(RegisterClient(t, goodwe_profile), goodwe_profile)
        assert await w.async_write(RegisterWrite("mode", 47511, 1)) == ERROR, type(exc).__name__
        assert t.writes == 0
