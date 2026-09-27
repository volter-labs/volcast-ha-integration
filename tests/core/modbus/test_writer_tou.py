"""Pisarz rejestrów + symulator Deye: włącznik harmonogramu i powrót do słowa właściciela."""
import pytest

from custom_components.volcast.core.control.tou_writes import (
    ENABLE, async_run_tou_writes, tou_restore_writes, tou_snapshot)
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.write_sequence import OK

EN = 146


@pytest.mark.asyncio
async def test_enable_off_sends_no_frame_when_already_off(deye_client, deye_profile, deye_bank, rtu_tcp_sim):
    from custom_components.volcast.core.modbus.writer import RegisterWriter
    sent = []
    w = RegisterWriter(deye_client, deye_profile, on_send=sent.append)
    deye_bank.poke(EN, 0xFE)
    assert await w.async_write(RegisterWrite(ENABLE, EN, 0xFE)) == OK
    assert sent == [] and deye_bank.writes == [] and all(fc == 3 for fc, _, _ in rtu_tcp_sim.log)


@pytest.mark.asyncio
async def test_enable_off_from_stale_word_switches_live_schedule_off(deye_writer, deye_bank):
    deye_bank.poke(EN, 0xFF)                        # odczyt w cyklu mówił OFF (0xFE), falownik ma ON
    assert await deye_writer.async_write(RegisterWrite(ENABLE, EN, 0xFE)) == OK
    assert deye_bank.read(EN, 1) == [0xFE]


@pytest.mark.asyncio
async def test_enable_on_sets_every_weekday(deye_writer, deye_bank):
    deye_bank.poke(EN, 0b0111110)                   # dni robocze właściciela
    assert await deye_writer.async_write(RegisterWrite(ENABLE, EN, 0xFF)) == OK
    assert deye_bank.read(EN, 1) == [0xFF]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [0x0001, 0x0000, 0b0111111])
async def test_restore_writes_owner_word_exactly(deye_client, deye_writer, deye_profile, deye_bank, owner):
    snap = tou_snapshot(await deye_client.read_state(), deye_profile)
    snap = {**snap, "tou_word": owner}
    deye_bank.poke(EN, 0xFF)                        # nasz harmonogram włączony
    deye_bank.poke(166, 55)                         # i nasz SoC programu 1
    rw = tou_restore_writes(deye_profile, snap, await deye_client.read_state(), soc_reserve=10.0,
                            rated_power_w=10000.0)
    rep = await async_run_tou_writes(rw, deye_writer.async_write)
    assert rep.failed == [] and deye_bank.read(EN, 1) == [owner]
    assert deye_bank.read(166, 1) == [snap["programs"][0][2]]
