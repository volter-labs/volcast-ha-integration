"""Tryb bezpośredni: zabezpieczenia jak w trybie encji — hamulec do trybu neutralnego, zejście
z rozładowania przy rezerwie w pauzie, degradacja akcji przy brakującej nastawie.

Na symulatorze modułu Wi-Fi GoodWe (`tests/sim`), profil przełączony na `verified` tylko w teście.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.volcast.control import executor as ex_mod
from custom_components.volcast.control.direct import target_fingerprint
from custom_components.volcast.control.store import ControlState, ControlStore
from custom_components.volcast.core.modbus import writer as writer_mod
from custom_components.volcast.core.slot import parse_schedule
from tests.control.test_executor import plan
from tests.control.test_executor_direct import (EXPORT_EN, EXPORT_W, GW_DRAFT, GW_V, MODE, POWER, SALT, SOC_MAX,
                                                SOC_MIN, Harness, _owned_sell, gw_raw_word, gw_target, issues,
                                                regs)  # noqa: F401 — `issues` to fikstura

LOGGER = "custom_components.volcast"
BRAKE_TEXT = "cannot be applied safely"
BRAKE_DONE_TEXT = "inverter set to its neutral mode"
BRAKE_FAILED_TEXT = "setting the neutral mode failed"


def _stale(h, age: float = 400.0) -> None:
    """Odczyt cyklu starszy niż `max_state_age_s` (I-9) — bez nowego odpytania."""
    r = h.conn.reading
    h.conn.reading = SimpleNamespace(**{**r.__dict__, "at_mono": h.clock() - age})


def _grid_charge(power=3000, sid="gc"):
    return plan(sid=sid, slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "mode": "charge",
                                 "charge_source": "grid", "power_w": power, "price_pln_kwh": 0.3}])


@pytest.fixture
def instant_settle(monkeypatch):
    async def no_wait(_s):
        return None
    monkeypatch.setattr(writer_mod, "_sleep", no_wait)


# ── hamulec: cykl, którego nie da się bezpiecznie wykonać, przy NASZYM trybie wymuszonym ──


@pytest.mark.asyncio
async def test_direct_stale_soc_during_our_sell_writes_neutral_mode_alone(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                          issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        n = len(goodwe_bank.writes)
        h.clock.advance(120.0)
        _stale(h)                                             # I-9: plan nie do wykonania
        await h.ex.async_tick()
        d = h.ex.last_decision
        assert regs(goodwe_bank)[n:] == [MODE] and gw_raw_word(goodwe_bank, MODE) == 1
        assert d.status == "blocked" and d.reason == "guard:I-9" and "neutral_brake" in d.notes
        assert h.ex.owned                                     # to nie powrót — sterowanie trwa
        assert h.ex._memory.last_written["mode"] == "auto"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_no_plan_during_our_grid_charge_writes_neutral_mode(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                         issues):
    goodwe_bank.poke(MODE, 1)                                 # właściciel: auto — tryb ładowania piszemy my
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(raw=_grid_charge())
    try:
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 11 and h.ex.owned and h.ex._memory.last_written["mode"] \
            == "charge_battery"
        h.ex.schedule = None                                  # plan zniknął (np. zły plan z chmury)
        await h.cycle(120.0)
        assert h.ex.last_decision.reason == "no_plan" and "neutral_brake" in h.ex.last_decision.notes
        assert gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_neutral_brake_respects_write_interval(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        h.clock.advance(10.0)
        _stale(h)
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 10           # I-6: 60 s od naszego zapisu trybu
        h.clock.advance(60.0)
        _stale(h)
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_no_brake_on_a_mode_we_did_not_write(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    goodwe_bank.poke(MODE, 10)                                # tryb właściciela, nic nie zapisaliśmy
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start()
    try:
        _stale(h)
        await h.ex.async_tick()
        assert goodwe_bank.writes == [] and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_no_brake_on_a_mode_taken_over_by_the_owner(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        h.ex._state.taken_over = ["mode"]
        n = len(goodwe_bank.writes)
        h.clock.advance(120.0)
        _stale(h)
        await h.ex.async_tick()
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_brake_on_a_stale_reading_keeps_a_mode_changed_since(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                          issues):
    # Odczyt cyklu stary (pokazuje nasz tryb), a właściciel przełączył tryb w międzyczasie: świeży odczyt
    # rejestru trybu przed zapisem rozstrzyga — nic nie piszemy na ślepo.
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        goodwe_bank.poke(MODE, 12)                            # discharge_battery — wybór właściciela
        n = len(goodwe_bank.writes)
        h.clock.advance(120.0)
        _stale(h)
        await h.ex.async_tick()
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 12
        assert "neutral_brake" not in h.ex.last_decision.notes
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_brake_after_restart_uses_persisted_ownership(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    target = gw_target(goodwe_udp_sim)
    goodwe_bank.poke(MODE, 11)                                # charge_battery z poprzedniego przebiegu
    store = ControlStore(make_hass(), "e1")
    await store.async_save(ControlState(
        consent=True, local_switch=True, owned=True, snapshot={"mode": "auto", "soc_min": 5.0},
        owner={"profile": "goodwe-et", "mode": "direct", "target": target_fingerprint(target, SALT),
               "device": target["device_fp"]},
        restore_keys=["mode", "power_w"]))
    h = await Harness(make_hass, GW_V, target, store=store).start()
    try:
        assert h.ex.owned and h.ex._memory.last_written.get("mode") is None
        _stale(h)
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 1 and "neutral_brake" in h.ex.last_decision.notes
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_no_brake_with_an_unverified_profile(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    target = gw_target(goodwe_udp_sim)
    goodwe_bank.poke(MODE, 10)
    store = ControlStore(make_hass(), "e1")
    await store.async_save(ControlState(
        consent=True, local_switch=True, owned=True, snapshot={"mode": "auto"},
        owner={"profile": "goodwe-et", "mode": "direct", "target": target_fingerprint(target, SALT),
               "device": target["device_fp"]},
        restore_keys=["mode"]))
    h = await Harness(make_hass, GW_DRAFT, target, store=store).start()
    try:
        _stale(h)
        await h.ex.async_tick()
        assert goodwe_bank.writes == []
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_no_brake_while_only_paused(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        h.ex._memory.paused_until = h.clock() + 3600.0
        new = plan(power=3000, sid="p2")
        await h.ex.async_on_plan(new, parse_schedule(new))
        n = len(goodwe_bank.writes)
        await h.cycle(120.0)
        assert h.ex.last_decision.reason == "paused" and len(goodwe_bank.writes) == n
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_exception_in_cycle_still_brakes_our_mode(make_hass, goodwe_udp_sim, goodwe_bank, issues,
                                                               monkeypatch):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        def boom(**_kw):
            raise RuntimeError("x")
        monkeypatch.setattr(ex_mod, "decide_cycle", boom)
        await h.cycle(120.0)
        assert h.ex.last_decision.reason == "exception:tick" and "neutral_brake" in h.ex.last_decision.notes
        assert gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_repeated_failed_brake_warns_once(make_hass, goodwe_udp_sim, goodwe_bank, issues, caplog,
                                                       instant_settle):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        goodwe_bank.ignore_writes.add(MODE)                   # echo jest, rejestr bez zmian (odmowa)
        for _ in range(3):
            h.clock.advance(120.0)
            _stale(h)
            await h.ex.async_tick()
        d = h.ex.last_decision
        assert gw_raw_word(goodwe_bank, MODE) == 10
        assert caplog.text.count(BRAKE_FAILED_TEXT) == 1 and BRAKE_DONE_TEXT not in caplog.text
        assert "neutral_brake_failed" in d.notes and "neutral_brake" not in d.notes
        goodwe_bank.ignore_writes.discard(MODE)               # falownik znów przyjmuje: ponowienie się udaje
        h.clock.advance(120.0)
        _stale(h)
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 1 and "neutral_brake" in h.ex.last_decision.notes
        assert caplog.text.count(BRAKE_DONE_TEXT) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_brake_without_a_pre_read_is_reported_as_failed(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                     sim_faults, issues, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, timeout_s=0.1)   # ramki gubione celowo
    try:
        h.clock.advance(120.0)
        _stale(h)
        sim_faults.drop_next = 10 ** 6                        # odczyt rejestru trybu przed zapisem nie wraca
        await h.ex.async_tick()
        d = h.ex.last_decision
        assert gw_raw_word(goodwe_bank, MODE) == 10
        assert "neutral_brake_failed" in d.notes and "neutral_brake" not in d.notes
        assert BRAKE_FAILED_TEXT in caplog.text and BRAKE_DONE_TEXT not in caplog.text
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_failed_reserve_neutral_is_not_reported_as_done(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                     issues, caplog, instant_settle):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    goodwe_bank.poke(SOC, 12)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await _owner_took_power(h, goodwe_bank)
        goodwe_bank.ignore_writes.add(MODE)
        goodwe_bank.poke(SOC, 9)
        await h.cycle(120.0)
        assert gw_raw_word(goodwe_bank, MODE) == 10
        assert "battery at the reserve — returning" not in caplog.text
        assert "reserve_neutral_failed" in h.ex.last_decision.notes
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_reserve_neutral_retries_after_a_failed_pre_read(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                     sim_faults, issues):
    # Odczyt rejestru trybu przed zapisem neutralnym nie wraca (wynik niepewny): ochrona rezerwy
    # w pauzie nie może zgasnąć — następny cykl ponawia zapis z odczytu, nie z pamięci zapisu.
    goodwe_bank.poke(SOC, 12)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, timeout_s=0.1)   # ramki gubione celowo
    try:
        await _owner_took_power(h, goodwe_bank)
        goodwe_bank.poke(SOC, 9)
        h.clock.advance(120.0)
        await h.conn.async_poll()
        sim_faults.drop_next = 10 ** 6                         # łącze leży tylko na czas zapisu
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 10
        assert "reserve_neutral_failed" in h.ex.last_decision.notes
        sim_faults.drop_next = 0                               # łącze wraca
        await h.cycle(120.0)
        assert gw_raw_word(goodwe_bank, MODE) == 1
        assert h.ex.paused and h.ex.last_decision.reason == "reserve_neutral"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_bus_conflict_with_a_changed_plan_brakes_our_mode(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                       issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        h.conn.stats.stray += 3
        await h.conn.async_poll()
        assert h.conn.conflict
        new = plan(power=3000, sid="p2")                      # nowa nastawa nie dojdzie (kolizja)
        await h.ex.async_on_plan(new, parse_schedule(new))
        h.clock.advance(120.0)
        await h.ex.async_tick()
        assert h.ex.last_decision.reason == "bus_conflict" and "neutral_brake" in h.ex.last_decision.notes
        assert gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_link_down_brakes_when_the_link_returns(make_hass, goodwe_udp_sim, goodwe_bank, sim_faults,
                                                             issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, timeout_s=0.1)   # ramki gubione celowo
    try:
        n = len(goodwe_bank.writes)
        sim_faults.drop_next = 10 ** 6                       # łącze leży
        for _ in range(3):
            await h.cycle(150.0)
        assert len(goodwe_bank.writes) == n and h.ex.owned   # bez łącza nic (i bez wyjątku)
        assert h.ex.last_decision.status == "blocked"
        sim_faults.drop_next = 0                             # łącze wraca przed kolejnym odpytaniem
        h.clock.advance(61.0)
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 1 and "neutral_brake" in h.ex.last_decision.notes
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_brake_without_a_fresh_read_retries_on_a_later_cycle(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                          sim_faults, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank, timeout_s=0.1)   # ramki gubione celowo
    try:
        n = len(goodwe_bank.writes)
        h.clock.advance(120.0)
        _stale(h)
        sim_faults.drop_next = 10 ** 6                       # tożsamość jeszcze potwierdzona, łącze już nie
        await h.ex.async_tick()
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 10
        sim_faults.drop_next = 0
        h.clock.advance(120.0)
        _stale(h)
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_direction_budget_exhausted_brakes_our_opposite_mode(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                          issues):
    goodwe_bank.poke(MODE, 1)
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(raw=_grid_charge())
    try:
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 11
        h.clock.advance(120.0)
        sell = plan(sid="sell2")
        await h.ex.async_on_plan(sell, parse_schedule(sell))
        now = h.clock()
        for i, direction in enumerate(["discharge", "charge", "discharge", "charge"]):
            h.ex._memory.limiter.record(direction, now - 100 + i)
        await h.cycle(1.0)
        assert "I-8" in h.ex.last_decision.notes and "neutral_brake" in h.ex.last_decision.notes
        assert gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


# ── chwilowa blokada: hamulec dopiero przy drugiej z rzędu (bez migania trybu) ──


def _no_temp(h) -> None:
    """Świeży odczyt bez temperatury baterii (jedno odpytanie bez tej wartości)."""
    r = h.conn.reading
    h.conn.reading = SimpleNamespace(**{**r.__dict__, "values": {**r.values, "battery_temp_c": None}})


async def _fresh_cycle(h, *, advance=61.0, tweak=None):
    h.clock.advance(advance)
    await h.conn.async_poll()
    if tweak is not None:
        tweak(h)
    await h.ex.async_tick()


@pytest.mark.asyncio
async def test_direct_single_cycle_without_temperature_does_not_flap(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        n = len(goodwe_bank.writes)
        await _fresh_cycle(h, tweak=_no_temp)
        d = h.ex.last_decision
        assert d.reason == "temperature_unknown" and "brake_deferred" in d.notes
        await _fresh_cycle(h)                                  # temperatura wróciła
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_second_cycle_without_temperature_brakes(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await _fresh_cycle(h, tweak=_no_temp)
        assert gw_raw_word(goodwe_bank, MODE) == 10
        await _fresh_cycle(h, tweak=_no_temp)
        assert gw_raw_word(goodwe_bank, MODE) == 1 and "neutral_brake" in h.ex.last_decision.notes
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_single_soc_jump_does_not_flap(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await _fresh_cycle(h)
        n = len(goodwe_bank.writes)
        goodwe_bank.poke(SOC, 40)                              # 83 → 40 w minutę: niewiarygodny skok
        await _fresh_cycle(h)
        d = h.ex.last_decision
        assert d.reason == "guard:I-9" and "brake_deferred" in d.notes
        await _fresh_cycle(h)
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_soc_spike_and_return_does_not_flap(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    """83 → 40 → 83: chwilowa próbka nie staje się odniesieniem — powrót to nie drugi skok."""
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await _fresh_cycle(h)
        n = len(goodwe_bank.writes)
        soc = gw_raw_word(goodwe_bank, SOC)
        goodwe_bank.poke(SOC, 40)
        await _fresh_cycle(h)
        assert "brake_deferred" in h.ex.last_decision.notes
        goodwe_bank.poke(SOC, soc)                             # odczyt wraca do poprzedniej wartości
        await _fresh_cycle(h)
        assert h.ex.last_decision.reason != "guard:I-9"
        await _fresh_cycle(h)
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 10
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_two_different_soc_jumps_in_a_row_brake(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    """83 → 40 → 10: żadna próbka nie potwierdza poprzedniej — drugi skok z rzędu hamuje."""
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await _fresh_cycle(h)
        goodwe_bank.poke(SOC, 40)
        await _fresh_cycle(h)
        assert gw_raw_word(goodwe_bank, MODE) == 10
        goodwe_bank.poke(SOC, 10)
        await _fresh_cycle(h)
        assert gw_raw_word(goodwe_bank, MODE) == 1 and "neutral_brake" in h.ex.last_decision.notes
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_single_invalid_pv_sample_in_a_sell_slot_does_not_flap(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                            issues):
    def no_pv(h):
        r = h.conn.reading
        h.conn.reading = SimpleNamespace(**{**r.__dict__, "values": {**r.values, "pv_power_w": None}})
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await _fresh_cycle(h)
        n = len(goodwe_bank.writes)
        await _fresh_cycle(h, tweak=no_pv)
        assert "sell_reading_held" in h.ex.last_decision.notes
        await _fresh_cycle(h)
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 10
        await _fresh_cycle(h, tweak=no_pv)
        await _fresh_cycle(h, tweak=no_pv)                     # druga z rzędu: tryb neutralny
        assert gw_raw_word(goodwe_bank, MODE) == 1
    finally:
        await h.close()


def test_brake_reason_classes_are_explicit():
    from custom_components.volcast.core.control.cycle import CycleDecision
    from custom_components.volcast.core.guards import GuardResult
    from custom_components.volcast.core.params import Params
    jump = GuardResult("degraded", False, "I-9", "", Params(), code="soc_jump")
    stale = GuardResult("degraded", False, "I-9", "", Params(), code="stale")
    transient = [CycleDecision("blocked", "temperature_unknown"), CycleDecision("blocked", "guard:I-9", guard=jump)]
    hard = [CycleDecision("blocked", "guard:I-9", guard=stale), CycleDecision("error", "exception:tick"),
            CycleDecision("blocked", "bus_conflict"), CycleDecision("blocked", "identity"),
            CycleDecision("idle", "no_plan"), CycleDecision("idle", "missing_entities"),
            CycleDecision("blocked", "guard:I-3")]
    assert all(ex_mod.transient_block(d) for d in transient)
    assert not any(ex_mod.transient_block(d) for d in hard)


# ── pauza (przejęcie przez właściciela) a rezerwa SoC ─────────────────────


SOC = 37007


async def _owner_took_power(h, bank):
    """Właściciel dwa razy zmienia moc w 30 min — przejęcie, pauza (moc zostaje jego)."""
    for _ in range(2):
        bank.poke(POWER, 1500)
        await h.cycle(120.0)
    assert h.ex.paused and "power_w" in h.ex._state.taken_over


@pytest.mark.asyncio
async def test_direct_reserve_forces_neutral_mode_even_during_pause(make_hass, goodwe_udp_sim, goodwe_bank, issues,
                                                                    caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    goodwe_bank.poke(SOC, 12)                                  # tuż nad rezerwą (skok SoC to I-9)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await _owner_took_power(h, goodwe_bank)
        assert gw_raw_word(goodwe_bank, MODE) == 10
        n = len(goodwe_bank.writes)
        goodwe_bank.poke(SOC, 9)                               # pod rezerwą planu (10 %)
        await h.cycle(120.0)
        assert regs(goodwe_bank)[n:] == [MODE] and gw_raw_word(goodwe_bank, MODE) == 1
        assert gw_raw_word(goodwe_bank, POWER) == 1500         # nastawa właściciela zostaje
        assert h.ex.paused and h.ex.last_decision.reason == "reserve_neutral"
        assert "battery at the reserve" in caplog.text
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_pause_above_reserve_writes_nothing(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    goodwe_bank.poke(SOC, 14)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await _owner_took_power(h, goodwe_bank)
        n = len(goodwe_bank.writes)
        goodwe_bank.poke(SOC, 12)
        await h.cycle(120.0)
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 10
        assert h.ex.last_decision.reason == "paused"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_pause_keeps_owner_mode_below_reserve(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    # Tryb przejęty przez właściciela zostaje jego — także pod rezerwą.
    goodwe_bank.poke(SOC, 12)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        for _ in range(2):
            goodwe_bank.poke(MODE, 12)                         # discharge_battery właściciela
            await h.cycle(120.0)
        assert h.ex.paused and "mode" in h.ex._state.taken_over
        n = len(goodwe_bank.writes)
        goodwe_bank.poke(SOC, 9)
        await h.cycle(120.0)
        assert h.ex.last_decision.guard.invariant == "I-1"    # zejście do rezerwy, nie inna blokada
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 12
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_reserve_in_pause_keeps_a_mode_changed_since_the_reading(make_hass, goodwe_udp_sim,
                                                                              goodwe_bank, issues):
    # Odczyt cyklu pokazał nasz tryb, ale tuż przed zapisem rejestr trybu ma już wybór właściciela.
    goodwe_bank.poke(SOC, 12)
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        await _owner_took_power(h, goodwe_bank)
        goodwe_bank.poke(SOC, 9)
        h.clock.advance(120.0)
        await h.conn.async_poll()
        goodwe_bank.poke(MODE, 2)                              # charge_pv — po odczycie, przed zapisem
        n = len(goodwe_bank.writes)
        await h.ex.async_tick()
        assert h.ex.last_decision.guard.invariant == "I-1"
        assert len(goodwe_bank.writes) == n and gw_raw_word(goodwe_bank, MODE) == 2
    finally:
        await h.close()


# ── degradacja akcji przy brakującej nastawie (sonda: brak rejestru albo niezweryfikowany) ──


ACTIONS = {
    "charge_grid": ({"mode": "charge", "charge_source": "grid", "power_w": 3000}, 11),
    "discharge_forced": ({"mode": "discharge", "power_w": 2500, "soc_target": 40}, 12),
    "sell": ({"mode": "discharge", "discharge_purpose": "sell", "power_w": 2500, "soc_target": 40}, 10),
    "standby": ({"mode": "idle"}, 8),
    "self_consume": ({"mode": "self_consume"}, 1),
}
# (akcja, brakująca nastawa) → tryb neutralny; reszta idzie bez tej nastawy (tabela z rozpoznania, sekcja d)
DEGRADES = {("charge_grid", "power_w"), ("discharge_forced", "power_w"), ("discharge_forced", "soc_min"),
            ("sell", "power_w"), ("sell", "soc_min"), ("standby", "power_w")}
REG = {"power_w": (POWER,), "soc_min": (SOC_MIN,), "export_limit_w": (EXPORT_W, EXPORT_EN), "soc_max": (SOC_MAX,)}
KEYS = {"export_limit_w": ("export_limit_w", "export_limit_enabled")}


def _slot(**kw):
    return {"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "price_pln_kwh": 0.8, **kw}


def _caps(missing, how):
    caps = {k: True for k in GW_V.modbus.probe_keys}
    for k in missing:
        if how == "unsupported":
            caps[k] = False                                   # sonda: brak rejestru (wyjątek 2)
        else:
            caps.pop(k)                                       # sonda go nie potwierdziła
    return caps


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["unsupported", "unverified"])
@pytest.mark.parametrize("key", ["power_w", "soc_min", "export_limit_w", "soc_max"])
@pytest.mark.parametrize("action", sorted(ACTIONS))
async def test_direct_action_per_missing_setting(make_hass, goodwe_udp_sim, goodwe_bank, issues, action, key, how):
    spec, mode_word = ACTIONS[action]
    target = gw_target(goodwe_udp_sim, capabilities=_caps(KEYS.get(key, (key,)), how))
    h = await Harness(make_hass, GW_V, target).start(raw=plan(slots=[_slot(**spec)]))
    try:
        await h.ex.async_tick()
        d = h.ex.last_decision
        assert d.status == "write", (d.status, d.reason)
        assert not set(REG[key]) & set(regs(goodwe_bank))      # brakującej nastawy nie piszemy nigdy
        if (action, key) in DEGRADES:
            assert gw_raw_word(goodwe_bank, MODE) == 1 and "degraded" in d.notes
            assert POWER not in regs(goodwe_bank)
        else:
            assert gw_raw_word(goodwe_bank, MODE) == mode_word and "degraded" not in d.notes
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_sell_with_export_ban_without_the_pair_goes_neutral(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                         issues):
    spec, _ = ACTIONS["sell"]
    target = gw_target(goodwe_udp_sim, capabilities=_caps(("export_limit_enabled",), "unsupported"))
    h = await Harness(make_hass, GW_V, target).start(raw=plan(slots=[_slot(**spec, export_allowed=False)]))
    try:
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 1 and "degraded" in h.ex.last_decision.notes
        assert not {EXPORT_W, EXPORT_EN, POWER} & set(regs(goodwe_bank))
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_floor_lost_in_session_degrades_sell(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    # Rejestr progu odpowiedział wyjątkiem 2 w trakcie sesji (pamięć `unsupported`): sprzedaż bez
    # gwarantowanej rezerwy nie idzie — tryb neutralny.
    spec, _ = ACTIONS["sell"]
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(raw=plan(slots=[_slot(**spec)]))
    try:
        h.ex._memory.unsupported.add("soc_min")
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 1 and "degraded" in h.ex.last_decision.notes
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_write_exception_2_becomes_unsupported_only_when_it_repeats(make_hass, goodwe_udp_sim,
                                                                                 goodwe_bank, issues):
    spec, _ = ACTIONS["sell"]
    goodwe_bank.readonly.add(SOC_MIN)                          # zapis progu → wyjątek 2 (odczyt działa)
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(raw=plan(slots=[_slot(**spec)]))
    try:
        await h.ex.async_tick()
        assert "soc_min" not in h.ex._memory.unsupported      # jeden wyjątek to nie werdykt na sesję
        assert gw_raw_word(goodwe_bank, MODE) != 10            # próg nie doszedł — sprzedaż wstrzymana
        await h.cycle(400.0)                                   # po wstrzymaniu odmowy: ten sam wyjątek drugi raz
        assert "soc_min" in h.ex._memory.unsupported
        await h.cycle(120.0)
        assert gw_raw_word(goodwe_bank, MODE) == 1 and "degraded" in h.ex.last_decision.notes
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_write_exception_2_repeat_is_confirmed_across_a_reconnect(make_hass, goodwe_udp_sim,
                                                                               goodwe_bank, issues):
    spec, _ = ACTIONS["sell"]
    goodwe_bank.readonly.add(SOC_MIN)
    h = await Harness(make_hass, GW_V, gw_target(goodwe_udp_sim)).start(raw=plan(slots=[_slot(**spec)]))
    try:
        await h.ex.async_tick()
        assert "soc_min" not in h.ex._memory.unsupported
        h.ex._direct._writer = None                            # nowy klient łącza = nowy pisarz
        await h.cycle(400.0)
        assert "soc_min" in h.ex._memory.unsupported           # pierwszy wyjątek nie zginął z pisarzem
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_missing_mode_register_means_no_control_and_an_alert(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                          issues, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    target = gw_target(goodwe_udp_sim, capabilities=_caps(("mode",), "unsupported"))
    h = await Harness(make_hass, GW_V, target).start()
    try:
        for _ in range(3):
            await h.cycle()
        d = h.ex.last_decision
        assert (d.status, d.reason) == ("idle", "missing_entities") and goodwe_bank.writes == []
        assert [(i, k) for i, k, _ in issues.created].count(("direct_mode_unsupported_e1", "control_error")) == 1
        assert caplog.text.count("inverter mode register is not available") == 1
        await h.ex.async_set_local_switch(False)
        await h.cycle()
        assert "direct_mode_unsupported_e1" in issues.deleted       # sterowanie wyłączone — bez alertu
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_direct_unverified_mode_register_brakes_our_forced_mode(make_hass, goodwe_udp_sim, goodwe_bank,
                                                                      issues):
    target = gw_target(goodwe_udp_sim, capabilities=_caps(("mode",), "unverified"))
    goodwe_bank.poke(MODE, 10)                                 # nasz tryb sprzedaży z poprzedniego przebiegu
    store = ControlStore(make_hass(), "e1")
    await store.async_save(ControlState(
        consent=True, local_switch=True, owned=True, snapshot={"mode": "auto", "soc_min": 5.0},
        owner={"profile": "goodwe-et", "mode": "direct", "target": target_fingerprint(target, SALT),
               "device": target["device_fp"]},
        restore_keys=["mode", "power_w"]))
    h = await Harness(make_hass, GW_V, target, store=store).start()
    try:
        await h.ex.async_tick()
        d = h.ex.last_decision
        assert d.reason == "missing_entities" and "neutral_brake" in d.notes
        assert regs(goodwe_bank) == [MODE] and gw_raw_word(goodwe_bank, MODE) == 1
        assert "direct_mode_unsupported_e1" in [i for i, _, _ in issues.created]
    finally:
        await h.close()
