"""Urządzenie przycinające nastawę: wartość rzeczywista = osiągnięta dla tej zamówionej.

Bez tego każdy cykl widziałby „plan 2500 ≠ urządzenie 2000” i pisał znowu (do wyczerpania budżetu).
"""
import pytest

from custom_components.volcast.core.control.cycle import (
    ControlMemory, Gates, Limits, Telemetry, commit, decide_cycle)
from custom_components.volcast.core.control.group_writes import async_run_group_writes
from custom_components.volcast.core.control.target import RegisterTarget
from custom_components.volcast.core.modbus.client import RegisterClient
from custom_components.volcast.core.modbus.writer import RegisterWriter
from custom_components.volcast.core.transports.base import TransportConfig
from custom_components.volcast.core.transports.factory import make_transport
from tests.core.golden import T0

from .conftest import POWER_REG, one_slot

GATES = Gates(consent=True, local_switch=True, control_mode="direct", verified=True)


async def _cycle(profile, schedule, client, writer, memory, now_mono):
    reading = await client.read_state()
    d = decide_cycle(profile=profile, schedule=schedule, now_utc=T0, now_mono=now_mono,
                     tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0),
                     limits=Limits(rated_power_w=8000.0), gates=GATES, memory=memory,
                     target=RegisterTarget(reading))
    if d.status == "write":
        rep = await async_run_group_writes(d.writes, writer.async_write, restore=d.restore,
                                           ambiguous_safe=d.restore_ambiguous_safe)
        commit(d, rep, memory, now_mono)
    return d


@pytest.mark.asyncio
async def test_clamping_device_gets_one_write_per_plan_change(goodwe_profile, goodwe_udp_sim, goodwe_bank):
    goodwe_bank.clamp[POWER_REG] = (0, 2000)          # urządzenie zawsze przycina moc do 2 kW
    t = make_transport(TransportConfig(kind="goodwe_udp", host=goodwe_udp_sim.host, port=goodwe_udp_sim.port,
                                       unit=0xF7, timeout_s=0.2, gap_s=0.0), allow_loopback=True)
    client = RegisterClient(t, goodwe_profile, unreadable={"soc_max"})
    writer = RegisterWriter(client, goodwe_profile)
    memory = ControlMemory.for_profile(goodwe_profile)
    try:
        plan = one_slot(mode="discharge", discharge_purpose="sell", power_w=2500)
        for i in range(6):                            # co 10 min — dawno po interwale I-6
            await _cycle(goodwe_profile, plan, client, writer, memory, 1000.0 + 600 * i)
        power_writes = [w for w in goodwe_bank.writes if w[0] == POWER_REG]
        assert power_writes == [(POWER_REG, 2500)]
        assert memory.last_written["power_w"] == 2000.0

        plan = one_slot(mode="discharge", discharge_purpose="sell", power_w=3000)
        for i in range(6, 12):
            await _cycle(goodwe_profile, plan, client, writer, memory, 1000.0 + 600 * i)
        power_writes = [w for w in goodwe_bank.writes if w[0] == POWER_REG]
        # Odmowa (urządzenie stoi na 2 kW): wstrzymanie 5 min, podwajane przy każdej identycznej
        # odmowie (5 → 10 → 20 → 40 min) — cykle co 10 min: zapisy w 1., 2., 3. i 5. cyklu.
        assert power_writes == [(POWER_REG, 2500)] + [(POWER_REG, 3000)] * 4
        assert memory.denied["power_w"][3] == 2400.0
    finally:
        await t.close()


def test_adjusted_value_settles_only_for_same_request(goodwe_profile, sell_schedule):
    from .conftest import MODE_REG, goodwe_reading
    memory = ControlMemory.for_profile(goodwe_profile)
    memory.adjusted = {"power_w": (2500.0, 2000.0)}
    reading = goodwe_reading(goodwe_profile, **{str(MODE_REG): 10, str(POWER_REG): 2000, "37007": 80})

    def run(r):
        return decide_cycle(profile=goodwe_profile, schedule=sell_schedule, now_utc=T0, now_mono=1000.0,
                            tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0),
                            limits=Limits(rated_power_w=8000.0), gates=GATES, memory=memory,
                            target=RegisterTarget(r))
    assert run(reading).reason == "nothing_to_write"
    # Ktoś zmienił moc na urządzeniu — to już nie „nasza przycięta wartość”.
    moved = goodwe_reading(goodwe_profile, **{str(MODE_REG): 10, str(POWER_REG): 1500, "37007": 80})
    assert [w.key for w in run(moved).writes] == ["power_w"]


def _decide(profile, schedule, reading, memory, now_mono=1000.0):
    return decide_cycle(profile=profile, schedule=schedule, now_utc=T0, now_mono=now_mono,
                        tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0),
                        limits=Limits(rated_power_w=8000.0), gates=GATES, memory=memory,
                        target=RegisterTarget(reading))


def test_denied_request_not_repeated_but_keeps_holding_mode(goodwe_profile, charge_schedule):
    from .conftest import MODE_REG, goodwe_reading
    reading_auto = goodwe_reading(goodwe_profile, **{str(MODE_REG): 1, "37007": 80, "47760": 100})
    memory = ControlMemory.for_profile(goodwe_profile)
    d = _decide(goodwe_profile, charge_schedule, reading_auto, memory)
    assert "soc_max" in [w.key for w in d.writes]
    # soc_max odrzucony (rejestr bez zmian) → warunek niespełniony
    from custom_components.volcast.core.control.group_writes import GroupReport
    commit(d, GroupReport(failed=["soc_max"], mode_held=True, ambiguous=[]), memory, 1000.0)
    again = _decide(goodwe_profile, charge_schedule, reading_auto, memory, now_mono=1200.0)
    assert "soc_max" not in [w.key for w in again.writes] and "mode" not in [w.key for w in again.writes]
    assert "denied_hold" in again.notes and "mode_held" in again.notes
    later = _decide(goodwe_profile, charge_schedule, reading_auto, memory, now_mono=1000.0 + 301)
    assert "soc_max" in [w.key for w in later.writes]          # po max(I-6, 5 min) nowa próba


def test_entity_mode_keeps_retrying_denied(goodwe_profile, sell_schedule):
    from custom_components.volcast.core.control.cycle import EntityContext
    ents = EntityContext("goodwe", {"mode": "select.m", "power_w": "number.p", "soc_min": "number.d",
                                    "soc_max": "number.u", "export_limit_w": "number.e",
                                    "export_limit_enabled": "switch.e"},
                         {"power_w": "W", "soc_min": "%", "soc_max": "%", "export_limit_w": "W"},
                         {"number.p": {"min": 0, "max": 10000, "step": 1}},
                         {"mode": "sell_power", "power_w": 100.0, "export_limit_enabled": 0.0})
    memory = ControlMemory.for_profile(goodwe_profile)
    egates = Gates(consent=True, local_switch=True, control_mode="entities", verified=True)
    kw = dict(profile=goodwe_profile, schedule=sell_schedule, now_utc=T0,
              tele=Telemetry(soc=80.0, soc_age_s=5.0, battery_temp_c=25.0),
              limits=Limits(rated_power_w=8000.0), gates=egates, memory=memory, ents=ents)
    d = decide_cycle(now_mono=1000.0, **kw)
    from custom_components.volcast.core.control.group_writes import GroupReport
    commit(d, GroupReport(failed=["power_w"], ambiguous=[]), memory, 1000.0)
    assert memory.denied == {}
    assert "power_w" in [w.key for w in decide_cycle(now_mono=1700.0, **kw).writes]
