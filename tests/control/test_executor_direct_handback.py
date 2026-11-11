"""Wykonawca w trybie bezpośrednim na profilu z przełącznikiem zdalnego sterowania i mocą wg kierunku.

Rejestry sterowania (tryb, przełącznik, nastawy u32 — rozładowanie ze skalą i kolejnością lo_hi, próg SoC
ze skalą) leżą pod adresem urządzenia innym niż adres połączenia; symulator Modbus TCP odpowiada na oba
adresy, każdy ze swoim bankiem rejestrów. Kolejność ramek: przejęcie (nastawy = 0 → przełącznik → tryb →
nastawa docelowa), oddanie (tryb neutralny → nastawy z migawki → przełącznik), obce wyłączenie przełącznika."""
from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.registers import RegisterImage
from tests.control.test_executor import plan
from tests.control.test_executor_direct import GW_NOW, SALT, Harness, issues  # noqa: F401 — fixture
from tests.core.modbus.engine_profile import PLANT, directional_profile
from tests.sim.device import Faults, RegisterBank
from tests.sim.fixtures import goodwe_words
from tests.sim.servers import ModbusTcpServer

LINK = 1
ENABLE, MODE, CHARGE, DISCHARGE, SOC_MIN = 40029, 40031, 40032, 40034, 40046
AUTO, SELL, FORCED_CHARGE = 1, 10, 11


class TwoUnitServer(ModbusTcpServer):
    """Modbus TCP z dwoma urządzeniami za jednym adresem sieciowym (np. bramka i falownik)."""

    def __init__(self, banks: dict[int, RegisterBank], faults: Faults) -> None:
        super().__init__(banks[LINK], faults, LINK)
        self.banks = banks

    def _on_frame(self, frame: bytes) -> list[bytes]:
        unit = frame[6] if len(frame) > 6 else None
        if unit not in self.banks:
            return []
        self.unit, self.bank = unit, self.banks[unit]
        return super()._on_frame(frame)


def profile(**policy):
    def mutate(raw):
        raw["status"] = "verified"
        raw["transports"] = ["modbus_tcp"]
        raw["modbus"]["status"] = "verified"
        raw["modbus"]["transport_options"] = {"modbus_tcp": {"port": 502, "timeout_ms": 2000, "gap_ms": 50}}
        raw["unit_id"] = LINK
        raw["read"]["export_limit_enabled"] = {"addr": 47509, "type": "u16"}
        raw["write_policy"].update(policy)
    return directional_profile(mutate)


def plant_bank(*, mode=FORCED_CHARGE, enable=0, charge=(0, 5000), discharge=(600, 0), soc_min=150):
    """Urządzenie: ładowanie 5 kW, rozładowanie 6 kW (600 × 10, lo_hi), próg SoC 15 %."""
    return RegisterBank({ENABLE: enable, MODE: mode, CHARGE: charge[0], CHARGE + 1: charge[1],
                         DISCHARGE: discharge[0], DISCHARGE + 1: discharge[1], SOC_MIN: soc_min})


def target(sim, prof):
    fp = device_fingerprint(SALT, prof, RegisterImage(goodwe_words()))
    return {"profile_id": prof.id, "transport": "modbus_tcp", "host": sim.host, "port": sim.port, "unit_id": LINK,
            "device_fp": fp, "unreadable": [], "capabilities": {k: True for k in prof.modbus.probe_keys}}


@pytest_asyncio.fixture
async def rig(make_hass):
    made = []

    async def build(bank=None, prof=None, utc=None):
        prof = prof or profile()
        plant = bank or plant_bank()
        link = {**goodwe_words(), 47509: 1}         # przełącznik ogranicznika eksportu: czytelne 0/1
        sim = await TwoUnitServer({LINK: RegisterBank(link), PLANT: plant}, Faults()).start()
        h = Harness(make_hass, prof, target(sim, prof), utc=utc)
        made.append((h, sim))
        return h, plant
    yield build
    for h, sim in made:
        await h.close()
        await sim.close()


def first(writes, addr, value=None, after=-1):
    """Indeks pierwszego zapisu słowa `addr` (o wartości `value`) po indeksie `after`."""
    return next(i for i, (a, v) in enumerate(writes)
                if i > after and a == addr and (value is None or v == value))


def u32(bank, addr):
    return bank.read(addr, 2)


@pytest.mark.asyncio
async def test_takeover_from_stored_forced_mode_zeroes_then_switch_mode_target(rig, issues):
    h, plant = await rig()
    await h.start(raw=plan(2000))
    await h.cycle()
    w = plant.writes
    zc, zd = first(w, CHARGE + 1, 0), first(w, DISCHARGE, 0)
    on = first(w, ENABLE, 1)
    mode = first(w, MODE, SELL)
    tgt = first(w, DISCHARGE, 200, after=mode)
    assert max(zc, zd) < on < mode < tgt
    assert u32(plant, CHARGE) == [0, 0] and u32(plant, DISCHARGE) == [200, 0]
    assert h.ex.owned and h.ex.last_decision.status == "write"
    snap = h.ex._state.snapshot
    assert snap["charge_power_w"] == 5000.0 and snap["discharge_power_w"] == 6000.0 and snap["enable"] == 0.0


@pytest.mark.asyncio
async def test_neutral_plan_after_zeroing_returns_owner_set_points(rig, issues):
    h, plant = await rig()
    await h.start(raw=plan(slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z",
                                   "mode": "self_consume", "price_pln_kwh": 0.5}]))
    await h.cycle()
    w = plant.writes
    on, mode = first(w, ENABLE, 1), first(w, MODE, AUTO)
    assert max(first(w, CHARGE + 1, 0), first(w, DISCHARGE, 0)) < on < mode
    assert first(w, CHARGE + 1, 5000, after=mode) > mode and first(w, DISCHARGE, 600, after=mode) > mode
    assert u32(plant, CHARGE) == [0, 5000] and u32(plant, DISCHARGE) == [600, 0]
    n = len(w)
    await h.cycle()
    assert plant.writes[n:] == []


@pytest.mark.asyncio
async def test_neutral_plan_keeps_no_limit_owner_set_points_exact(rig, issues):
    no_limit = (0xFFFF, 0xFFFF)
    h, plant = await rig(plant_bank(charge=no_limit, discharge=no_limit))
    await h.start(raw=plan(slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z",
                                   "mode": "self_consume", "price_pln_kwh": 0.5}]))
    await h.cycle()
    assert u32(plant, CHARGE) == [0xFFFF, 0xFFFF] and u32(plant, DISCHARGE) == [0xFFFF, 0xFFFF]
    assert plant.read(MODE, 1) == [AUTO] and plant.read(ENABLE, 1) == [1]


class Utc:
    def __init__(self):
        self.t = GW_NOW + timedelta(seconds=30)

    def __call__(self):
        return self.t


SELL_THEN_NEUTRAL = [{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T10:30:00Z", "mode": "discharge",
                      "discharge_purpose": "sell", "power_w": 2000, "price_pln_kwh": 0.8},
                     {"from": "2026-09-23T10:30:00Z", "to": "2026-09-23T11:00:00Z", "mode": "self_consume",
                      "price_pln_kwh": 0.5}]


async def _sell_then_neutral(rig):
    utc = Utc()
    h, plant = await rig(plant_bank(mode=AUTO), utc=utc)
    await h.start(raw=plan(slots=SELL_THEN_NEUTRAL))
    await h.cycle()
    assert plant.read(MODE, 1) == [SELL] and u32(plant, DISCHARGE) == [200, 0]
    utc.t = GW_NOW + timedelta(minutes=31)
    return h, plant


def _set_point_writes(writes):
    return [(a, v) for a, v in writes if a in (CHARGE, CHARGE + 1, DISCHARGE, DISCHARGE + 1)]


@pytest.mark.asyncio
async def test_neutral_slot_writes_the_mode_then_owner_set_points_next_cycle(rig, issues):
    h, plant = await _sell_then_neutral(rig)
    n = len(plant.writes)
    await h.cycle()
    assert plant.read(MODE, 1) == [AUTO] and _set_point_writes(plant.writes[n:]) == []
    await h.cycle()
    assert u32(plant, DISCHARGE) == [600, 0] and u32(plant, CHARGE) == [0, 5000]
    assert plant.read(MODE, 1) == [AUTO]


@pytest.mark.asyncio
async def test_refused_owner_set_point_never_keeps_the_forced_mode(rig, issues):
    h, plant = await _sell_then_neutral(rig)
    plant.readonly.add(DISCHARGE)               # urządzenie odmawia zapisu nastawy właściciela
    n = len(plant.writes)
    for _ in range(3):
        await h.cycle()
        assert plant.read(MODE, 1) == [AUTO], h.ex.last_decision.notes
    assert not any(a == MODE and v == SELL for a, v in plant.writes[n:])
    assert u32(plant, CHARGE) == [0, 5000]      # nastawa ładowania właściciela nietknięta


@pytest.mark.asyncio
async def test_refused_neutral_mode_leaves_owner_set_points_unwritten(rig, issues):
    h, plant = await _sell_then_neutral(rig)
    plant.readonly.add(MODE)                    # tryb neutralny nie przechodzi
    n = len(plant.writes)
    for _ in range(3):
        await h.cycle()
    assert _set_point_writes(plant.writes[n:]) == []
    assert u32(plant, DISCHARGE) == [200, 0] and u32(plant, CHARGE) == [0, 5000]


@pytest.mark.asyncio
async def test_zero_first_takeover_with_a_refused_mode_keeps_the_zeros(rig, issues):
    bank = plant_bank()
    bank.readonly.add(MODE)
    h, plant = await rig(bank)
    await h.start(raw=plan(slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z",
                                   "mode": "self_consume", "price_pln_kwh": 0.5}]))
    await h.cycle()
    assert u32(plant, CHARGE) == [0, 0] and u32(plant, DISCHARGE) == [0, 0]


@pytest.mark.asyncio
async def test_takeover_from_neutral_writes_set_point_switch_mode_in_one_sequence(rig, issues):
    h, plant = await rig(plant_bank(mode=AUTO))
    await h.start(raw=plan(2000))
    await h.cycle()
    w = plant.writes
    assert first(w, DISCHARGE, 200) < first(w, ENABLE, 1) < first(w, MODE, SELL)
    assert not any(a in (CHARGE, CHARGE + 1) for a, _ in w)


@pytest.mark.asyncio
async def test_foreign_switch_off_pauses_hands_back_and_raises_issue(rig, issues):
    h, plant = await rig()
    await h.start(raw=plan(2000))
    await h.cycle()
    assert plant.read(ENABLE, 1) == [1]
    n = len(plant.writes)
    plant.poke(ENABLE, 0)                       # inny sterownik kończy zdalne sterowanie
    await h.cycle()
    assert h.ex.paused and not h.ex.owned
    assert "foreign_control_direct" in issues.keys()
    back = plant.writes[n:]
    assert first(back, MODE, AUTO) < first(back, DISCHARGE, 600)
    assert not any(a == ENABLE for a, _ in back)          # przełącznik zostaje taki, jak go zostawiono
    assert u32(plant, CHARGE) == [0, 5000] and u32(plant, DISCHARGE) == [600, 0]


@pytest.mark.asyncio
async def test_stop_hands_back_mode_set_points_then_switch(rig, issues):
    h, plant = await rig()
    await h.start(raw=plan(2000))
    await h.cycle()
    await h.conn.async_poll()
    n = len(plant.writes)
    assert await h.ex.async_neutral_at_stop() is True
    back = plant.writes[n:]
    mode, dis, off = first(back, MODE, AUTO), first(back, DISCHARGE, 600), first(back, ENABLE, 0)
    assert mode < dis < off and first(back, CHARGE + 1, 5000) < off
    assert h.ex._state.owned                           # restart przejmuje od nowa z tej samej migawki


@pytest.mark.asyncio
async def test_consent_withdrawal_restores_no_limit_set_points_exactly(rig, issues):
    no_limit = (0xFFFF, 0xFFFF)
    h, plant = await rig(plant_bank(mode=FORCED_CHARGE, charge=no_limit, discharge=no_limit))
    await h.start(raw=plan(2000))
    await h.cycle()
    assert u32(plant, DISCHARGE) == [200, 0] and u32(plant, CHARGE) == [0, 0]
    await h.ex.async_set_consent(False)
    await h.cycle()
    assert u32(plant, CHARGE) == [0xFFFF, 0xFFFF] and u32(plant, DISCHARGE) == [0xFFFF, 0xFFFF]
    assert plant.read(ENABLE, 1) == [0] and plant.read(MODE, 1) == [AUTO] and not h.ex.owned


@pytest.mark.asyncio
async def test_rejected_switch_register_holds_the_mode_in_later_cycles(rig, issues):
    bank = plant_bank(mode=AUTO)
    bank.readonly.add(ENABLE)                   # rejestr czytelny, zapis → wyjątek 2 (odmowa)
    h, plant = await rig(bank)
    await h.start(raw=plan(2000))
    await h.cycle()
    await h.cycle()
    assert plant.read(MODE, 1) == [AUTO] and not any(a == MODE for a, _ in plant.writes)
    assert "mode_held" in h.ex.last_decision.notes
