"""Rozbieżności odczytu zwrotnego: wpis + zdarzenie przy ERROR/DENIED, kasowanie po udanym zapisie, limit 5."""
import pytest

from custom_components.volcast.core.control.readback import MAX_ISSUES, ReadbackLog
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.registers import RegisterWrite
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK


def _log():
    events = []
    return ReadbackLog(events.append), events


@pytest.mark.asyncio
async def test_denied_adds_item_and_fires_event(goodwe_client, goodwe_profile, goodwe_bank):
    log, events = _log()
    w = RegisterWriter(goodwe_client, goodwe_profile, readback=log)
    goodwe_bank.ignore_writes.add(47512)
    assert await w.async_write(RegisterWrite("power_w", 47512, 3000)) == DENIED
    [item] = log.items
    assert item["key"] == "power_w" and item["expected"] == 3000 and item["result"] == DENIED
    assert item["read"] is not None and item["at"]
    assert events == [item]
    assert set(item) == {"key", "expected", "read", "result", "at"}


@pytest.mark.asyncio
async def test_error_adds_item_and_fires_event(goodwe_client, goodwe_profile, goodwe_bank):
    log, events = _log()
    w = RegisterWriter(goodwe_client, goodwe_profile, readback=log)
    goodwe_bank.clamp[47511] = (0, 2)
    assert await w.async_write(RegisterWrite("mode", 47511, 10)) == ERROR
    assert log.items[0]["result"] == ERROR and log.items[0]["read"] == 2 and len(events) == 1


@pytest.mark.asyncio
async def test_success_clears_only_that_key(goodwe_client, goodwe_profile, goodwe_bank):
    log, _ = _log()
    w = RegisterWriter(goodwe_client, goodwe_profile, readback=log)
    goodwe_bank.ignore_writes.add(47512)
    await w.async_write(RegisterWrite("power_w", 47512, 3000))
    goodwe_bank.clamp[47511] = (0, 2)
    await w.async_write(RegisterWrite("mode", 47511, 10))
    goodwe_bank.clamp.pop(47511)
    assert await w.async_write(RegisterWrite("mode", 47511, 10)) == OK
    assert [i["key"] for i in log.items] == ["power_w"]


def test_cap_keeps_last_five():
    log = ReadbackLog()
    for n in range(MAX_ISSUES + 3):
        log.issue(f"k{n}", n, 0, ERROR)
    assert [i["key"] for i in log.items] == [f"k{n}" for n in range(3, MAX_ISSUES + 3)]


def test_event_failure_does_not_break_log():
    def boom(_):
        raise RuntimeError
    log = ReadbackLog(boom)
    log.issue("k", 1, 0, ERROR)
    assert len(log.items) == 1
