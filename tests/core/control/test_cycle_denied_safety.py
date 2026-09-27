"""Pamięć odmowy (tryb bezpośredni) nigdy nie wstrzymuje ruchu w stronę bezpieczną.

Chwilowa awaria (np. nieudany odczyt przed zapisem) nie jest odmową urządzenia, a nawet
prawdziwa odmowa nie może na długo zatrzymać powrotu do trybu bazowego ani zmniejszenia mocy.
"""
import pytest

from custom_components.volcast.core.control.cycle import (
    ControlMemory, Gates, Limits, Telemetry, commit, decide_cycle)
from custom_components.volcast.core.control.group_writes import GroupReport, async_run_group_writes
from custom_components.volcast.core.control.target import RegisterTarget
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.transports.base import TransportConfig
from custom_components.volcast.core.transports.factory import make_transport
from tests.core.golden import T0

from .conftest import MODE_REG, POWER_REG, SOC_REG, goodwe_reading, one_slot

GATES = Gates(consent=True, local_switch=True, control_mode="direct", verified=True)
SELF_CONSUME = one_slot(mode="self_consume")


def _decide(profile, schedule, reading, memory, now_mono):
    return decide_cycle(profile=profile, schedule=schedule, now_utc=T0, now_mono=now_mono,
                        tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0),
                        limits=Limits(rated_power_w=8000.0), gates=GATES, memory=memory,
                        target=RegisterTarget(reading))


def _selling(profile, power):
    return goodwe_reading(profile, **{str(MODE_REG): 10, str(POWER_REG): power, str(SOC_REG): 80})


def _keys(d):
    return [w.key for w in d.writes]


def test_refused_return_to_baseline_is_retried_next_cycle(goodwe_profile):
    reading = _selling(goodwe_profile, 5000)
    memory = ControlMemory.for_profile(goodwe_profile)
    d = _decide(goodwe_profile, SELF_CONSUME, reading, memory, 1000.0)
    assert _keys(d) == ["mode"]
    commit(d, GroupReport(failed=["mode"], ambiguous=[]), memory, 1000.0)     # nawet odmowa
    again = _decide(goodwe_profile, SELF_CONSUME, reading, memory, 1000.0 + 900)
    assert _keys(again) == ["mode"] and "denied_hold" not in again.notes


def test_refused_power_reduction_is_retried_next_cycle(goodwe_profile):
    reading = _selling(goodwe_profile, 5000)
    plan = one_slot(mode="discharge", discharge_purpose="sell", power_w=1000)
    memory = ControlMemory.for_profile(goodwe_profile)
    d = _decide(goodwe_profile, plan, reading, memory, 1000.0)
    assert _keys(d) == ["power_w"]
    commit(d, GroupReport(failed=["power_w"], ambiguous=[]), memory, 1000.0)
    again = _decide(goodwe_profile, plan, reading, memory, 1000.0 + 61)
    assert _keys(again) == ["power_w"] and "denied_hold" not in again.notes


def test_refused_condition_does_not_hold_return_to_baseline(goodwe_profile):
    # Powrót do auto z warunkiem (przełącznik limitu eksportu), którego zapis urządzenie odrzuciło.
    reading = goodwe_reading(goodwe_profile, **{str(MODE_REG): 10, str(POWER_REG): 5000, str(SOC_REG): 80,
                                                "47509": 1})
    memory = ControlMemory.for_profile(goodwe_profile)
    d = _decide(goodwe_profile, SELF_CONSUME, reading, memory, 1000.0)
    assert "export_limit_enabled" in _keys(d)
    commit(d, GroupReport(failed=["export_limit_enabled"], mode_held=True, ambiguous=[]), memory, 1000.0)
    again = _decide(goodwe_profile, SELF_CONSUME, reading, memory, 1000.0 + 61)
    assert "export_limit_enabled" in _keys(again) and "denied_hold" not in again.notes


def test_refusal_toward_unsafe_direction_still_held_but_capped(goodwe_profile):
    reading = _selling(goodwe_profile, 1000)
    plan = one_slot(mode="discharge", discharge_purpose="sell", power_w=5000)
    memory = ControlMemory.for_profile(goodwe_profile)
    d = _decide(goodwe_profile, plan, reading, memory, 1000.0)
    assert _keys(d) == ["power_w"]
    commit(d, GroupReport(failed=["power_w"], ambiguous=[]), memory, 1000.0)
    held = _decide(goodwe_profile, plan, reading, memory, 1000.0 + 61)
    assert _keys(held) == [] and "denied_hold" in held.notes
    # Odmowa pamiętana najwyżej max(I-6, 5 min), nie godzinę.
    later = _decide(goodwe_profile, plan, reading, memory, 1000.0 + 301)
    assert _keys(later) == ["power_w"]


@pytest.mark.asyncio
async def test_transient_pre_read_failure_does_not_hold_return_to_self_consume(
        goodwe_profile, goodwe_udp_sim, goodwe_bank, sim_faults):
    goodwe_bank.poke(MODE_REG, 10)
    goodwe_bank.poke(POWER_REG, 5000)
    t = make_transport(TransportConfig(kind="goodwe_udp", host=goodwe_udp_sim.host, port=goodwe_udp_sim.port,
                                       unit=0xF7, timeout_s=0.1, gap_s=0.0, read_tries=2), allow_loopback=True)
    client = RegisterClient(t, goodwe_profile, unreadable={"soc_max"})
    writer = RegisterWriter(client, goodwe_profile)
    memory = ControlMemory.for_profile(goodwe_profile)

    async def cycle(now_mono, *, fail_pre_read=False):
        reading = await client.read_state()
        d = _decide(goodwe_profile, SELF_CONSUME, reading, memory, now_mono)
        if d.status == "write":
            if fail_pre_read:
                sim_faults.drop_next = 2               # odczyt przed zapisem ginie (obie próby)
            rep = await async_run_group_writes(d.writes, writer.async_write, restore=d.restore,
                                               ambiguous_safe=d.restore_ambiguous_safe)
            commit(d, rep, memory, now_mono)
        return d

    try:
        await cycle(1000.0, fail_pre_read=True)
        assert goodwe_bank.writes == [] and memory.denied == {}
        await cycle(1000.0 + 61)                        # następny cykl po I-6
        assert goodwe_bank.read(MODE_REG, 1) == [1]
    finally:
        await t.close()
