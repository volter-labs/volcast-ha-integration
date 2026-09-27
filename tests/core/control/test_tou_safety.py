"""Bezpieczeństwo i oszczędność przepisywania programów TOU (tryb bezpośredni).

* włącznik OFF zawsze przed zapisem programów (stan włącznika sprawdza świeży odczyt pisarza);
* pole wstrzymane (I-6, budżet, pamięć odmowy) = żadnej sekwencji, harmonogram nie jest
  wyłączany na darmo;
* wartość przycięta przez urządzenie i prawdziwa odmowa nie są przepisywane co cykl;
* małe zmiany planu w tolerancji profilu nie przepisują programów, przepisanie najwyżej raz
  na godzinę (poza zmianą w stronę bezpieczną);
* włącznik w pamięci sterowania (rozjazd, interwał, niepewność).
"""
from collections import Counter
from datetime import timedelta

from custom_components.volcast.core.control.conflict import DriftTracker, drifted_keys
from custom_components.volcast.core.control.cycle import ControlMemory
from custom_components.volcast.core.control.tou_cycle import _toward_safety, commit_tou
from custom_components.volcast.core.control.tou_writes import (
    ENABLE, TouReport, run_tou_writes, tou_restore_writes, tou_snapshot)
from custom_components.volcast.core.write_sequence import DENIED, OK, AdjustedOutcome
from tests.sim.fixtures import deye_words

from .tou_helpers import DEYE, NOW, SELF, apply, daily_plan, decide, reading

EN_ADDR = 146


def _pattern(soc=90, power=3000, charge=(2, 5)):
    return {h: ({"mode": "charge", "charge_source": "grid", "power_w": power, "soc_target": soc,
                 "price_pln_kwh": 0.2} if charge[0] <= h < charge[1] else SELF) for h in range(24)}


class Device:
    """Falownik jak z pisarzem rejestrów: OFF na słowie ŚWIEŻYM (bez ramki, gdy już wyłączony),
    ON = bit włącznika + wszystkie dni tygodnia, surowe słowo przy powrocie."""

    def __init__(self, words=None, *, clamp_power=None, deny_power_above=None):
        self.words = dict(words or deye_words())
        self.frames = Counter()
        self.clamp_power = clamp_power
        self.deny_power_above = deny_power_above
        self.live_program_writes = 0

    @property
    def enabled(self):
        return bool(self.words[EN_ADDR] & 1)

    def write(self, w):
        if w.key == ENABLE:
            before = self.words[EN_ADDR]
            new = (before & ~1) if not w.value & 1 else (before | 1 | 0xFE)
            if new == before:
                return OK                                   # nic do zmiany — bez ramki
            self.frames[w.key] += 1
            self.words[EN_ADDR] = new
            return OK
        self.frames[w.key] += 1
        if w.key.startswith("tou.") and self.enabled:
            self.live_program_writes += 1
        if w.key.endswith(".power_w"):
            if self.deny_power_above is not None and w.value > self.deny_power_above:
                return DENIED
            if self.clamp_power is not None and w.value > self.clamp_power:
                self.words[w.addr] = self.clamp_power
                return AdjustedOutcome(float(self.clamp_power))
        self.words[w.addr] = w.value
        return OK


def _cycle(dev, plan, memory, now_mono, *, now=NOW, rd=None, snapshot=None):
    d, _ = decide(plan, rd or reading(dev.words), memory=memory, now=now, now_mono=now_mono)
    rep = None
    if d.status == "write":
        rep = run_tou_writes(d.writes, dev.write, pre_held=d.pre_held)
        commit_tou(d, rep, memory, now_mono)
        if rep.restore_needed:
            rw = tou_restore_writes(DEYE, snapshot, reading(dev.words), soc_reserve=10.0, rated_power_w=10000.0)
            run_tou_writes(rw, dev.write)
    return d, rep


# ── nieświeży odczyt ──────────────────────────────────────────────────────


def test_stale_reading_after_own_on_never_writes_programs_while_enabled():
    words = deye_words()
    words[EN_ADDR] = 0xFE                                  # właściciel: harmonogram wyłączony
    dev = Device(words)
    stale = reading(dict(dev.words), at_mono=500.0)
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, daily_plan(), memory, 1000.0, rd=stale)
    assert dev.enabled
    # Odczyt sprzed naszego zapisu nie jest podstawą decyzji (ani pamięci interwału).
    d, _ = _cycle(dev, daily_plan(_pattern(soc=60, charge=(3, 6))), memory, 5000.0, rd=stale)
    assert (d.status, d.reason, d.writes) == ("idle", "stale_reading", [])
    assert memory.throttle.known("tou.1.soc") is not None
    # Odczyt świeży, ale z włącznikiem pokazanym jako OFF: OFF i tak pierwszy.
    wrong = dict(dev.words)
    wrong[EN_ADDR] = 0xFE
    fresh_but_wrong = reading(wrong)
    d, _ = _cycle(dev, daily_plan(_pattern(soc=60, charge=(3, 6))), memory, 5000.0, rd=fresh_but_wrong)
    assert d.status == "write" and d.writes[0].key == ENABLE and not d.writes[0].value & 1
    assert dev.live_program_writes == 0 and dev.enabled


def test_rewrite_always_starts_with_off_even_if_reading_says_off():
    plan = daily_plan()
    words = apply(deye_words(), decide(plan, reading())[0].writes)
    words[166], words[EN_ADDR] = 55, 0xFE
    d, _ = decide(plan, reading(words))
    assert [w.key for w in d.writes] == [ENABLE, "tou.1.soc", ENABLE]
    assert not d.writes[0].value & 1 and d.writes[-1].value & 1


# ── pole wstrzymane → nic ─────────────────────────────────────────────────


def test_first_program_field_held_writes_nothing():
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, daily_plan(), memory, 1000.0)
    frames = sum(dev.frames.values())
    d, _ = _cycle(dev, daily_plan(_pattern(power=4000)), memory, 1100.0)  # I-6 wstrzymuje pola
    assert (d.status, d.writes) == ("idle", []) and "I-6" in d.notes
    assert sum(dev.frames.values()) == frames and dev.enabled


def test_budget_held_field_writes_nothing():
    memory = ControlMemory.for_profile(DEYE)
    d0, _ = decide(daily_plan(), reading())
    last_field = d0.writes[-2].key
    for _ in range(memory.budget.per_key):
        memory.budget.note(last_field, NOW.timestamp() - 60)
    d, _ = decide(daily_plan(), reading(**{"146": 0xFE}), memory=memory)       # harmonogram wyłączony
    assert (d.status, d.reason, d.writes) == ("idle", "held", []) and "nvm_budget" in d.notes


# ── przycięcie i odmowa ───────────────────────────────────────────────────


def _day(dev, plan, memory, snapshot=None):
    enable_offs = 0
    for i in range(24 * 12):
        _cycle(dev, plan, memory, 1000.0 + 300.0 * i, now=NOW + timedelta(minutes=5 * i), snapshot=snapshot)
        enable_offs += not dev.enabled
    return enable_offs


def test_clamped_program_power_written_once_and_schedule_stays_on():
    dev = Device(clamp_power=5000)
    memory = ControlMemory.for_profile(DEYE)
    _day(dev, daily_plan(_pattern(power=8000)), memory)
    assert dev.frames[ENABLE] == 2 and dev.enabled                 # jedna sekwencja: OFF i ON
    assert all(n == 1 for k, n in dev.frames.items() if k != ENABLE)


def test_refused_program_field_is_not_rewritten_every_cycle():
    words = deye_words()
    words[EN_ADDR] |= 1
    dev = Device(words, deny_power_above=5000)
    snapshot = tou_snapshot(reading(dev.words), DEYE)
    memory = ControlMemory.for_profile(DEYE)
    plan = daily_plan(_pattern(power=8000))
    _day(dev, plan, memory, snapshot=snapshot)
    assert dev.frames[ENABLE] <= 36, dev.frames                    # odwrót + limit wyłączeń, nie co 5 min
    assert memory.budget.hit is False
    # Na koniec nic nie ładuje z sieci wbrew planowi: albo harmonogram właściciela zgodny, albo OFF.
    programs = decide(plan, reading(dev.words))[0].programs
    assert not (dev.enabled and _toward_safety(reading(dev.words).programs, programs, DEYE))


# ── tolerancja i rzadkie przepisywanie ────────────────────────────────────


def test_small_plan_changes_within_profile_tolerance_are_settled():
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, daily_plan(), memory, 1000.0)
    d, _ = _cycle(dev, daily_plan(_pattern(soc=88, power=3200)), memory, 5000.0)
    assert (d.status, d.reason) == ("idle", "nothing_to_write")


def test_rewrite_at_most_hourly_unless_toward_safety():
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, daily_plan(), memory, 1000.0)
    d, _ = _cycle(dev, daily_plan(_pattern(power=4000)), memory, 1000.0 + 600)
    assert (d.status, d.writes) == ("idle", []) and "tou_rewrite_interval" in d.notes
    d, _ = _cycle(dev, daily_plan(_pattern(power=4000)), memory, 1000.0 + 3601)
    assert d.status == "write"
    # Zdjęcie ładowania z sieci (strona bezpieczna) idzie od razu.
    d, _ = _cycle(dev, daily_plan({h: SELF for h in range(24)}), memory, 1000.0 + 3601 + 600)
    assert d.status == "write" and any(w.key.endswith(".grid_charge") for w in d.writes)


# ── włącznik w pamięci ────────────────────────────────────────────────────


def test_enable_tracked_for_drift_and_interval():
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, daily_plan(), memory, 1000.0)
    assert memory.last_written["tou_enabled"] == 1.0
    dev.words[EN_ADDR] &= ~1                                       # właściciel wyłącza w aplikacji
    tracker = DriftTracker()
    rd = reading(dev.words)
    assert drifted_keys(memory.last_written, rd.device) == ("tou_enabled",)
    assert tracker.note_drift("tou_enabled", 1060.0) is False
    d, _ = _cycle(dev, daily_plan(), memory, 1060.0)
    assert (d.status, d.writes) == ("idle", []) and not dev.enabled    # bez ponownego włączenia w I-6
    assert tracker.note_drift("tou_enabled", 1120.0) is True           # drugi rozjazd = przejęcie
    d, _ = _cycle(dev, daily_plan(), memory, 1000.0 + 301)
    assert [w.key for w in d.writes] == [ENABLE] and dev.enabled


def test_ambiguous_enable_is_cleared_by_reading():
    plan = daily_plan()
    d, memory = decide(plan, reading())
    rep = TouReport(written=[k.key for k in d.writes[:-1]], failed=[ENABLE], ambiguous=[ENABLE], frames=[ENABLE])
    commit_tou(d, rep, memory, 1000.0)
    assert "tou_enabled" in memory.uncertain
    decide(plan, reading(apply(deye_words(), d.writes)), memory=memory, now_mono=1060.0)
    assert "tou_enabled" not in memory.uncertain


# ── dni tygodnia ──────────────────────────────────────────────────────────


def test_enable_on_sets_every_weekday_while_we_own():
    plan = daily_plan()
    words = apply(deye_words(), decide(plan, reading())[0].writes)
    words[EN_ADDR] = 0b0111110                             # właściciel: tylko dni robocze, wyłączony
    d, _ = decide(plan, reading(words), owner_word=0b0111110)
    assert d.writes[-1].key == ENABLE and d.writes[-1].value == 0xFF


def test_restore_ends_with_the_owner_raw_word():
    snap = tou_snapshot(reading(), DEYE)
    for owner in (0x0001, 0x0000, 0b0111111):
        snap = {**snap, "tou_word": owner}
        words = deye_words()
        words[EN_ADDR] = 0xFF
        rw = tou_restore_writes(DEYE, snap, reading(words), soc_reserve=10.0, rated_power_w=10000.0)
        assert rw[-1].key == "tou_word" and rw[-1].value == owner


# ── stary program ładujący z sieci nie może zostać wbrew planowi ─────────


def _hours(charge, idle=range(17, 19)):
    ch = {"mode": "charge", "charge_source": "grid", "power_w": 3000, "soc_target": 90, "price_pln_kwh": 0.2}
    return daily_plan({h: ch if h in charge else {"mode": "idle", "price_pln_kwh": 1.1} if h in idle else SELF
                       for h in range(24)})


def _grid_charging_live(dev, plan_programs=None):
    rd = reading(dev.words)
    return dev.enabled and any(p.grid_charge for p in rd.programs)


def test_budget_exhausted_safety_rewrite_switches_schedule_off():
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, _hours(range(2, 5)), memory, 1000.0)
    for _ in range(60):
        memory.budget.note(ENABLE, NOW.timestamp() - 60)
    d, _ = _cycle(dev, _hours([]), memory, 5000.0)                   # plan: bez ładowania z sieci
    assert d.status == "write" and [w.key for w in d.writes] == [ENABLE] and not d.writes[0].value & 1
    assert "tou_safety_off" in d.notes and not dev.enabled
    assert memory.last_written["tou_enabled"] == 0.0
    d, _ = _cycle(dev, _hours([]), memory, 5300.0)                   # dalej wstrzymane: zostaje OFF
    assert d.writes == [] and not dev.enabled


def test_refusal_of_other_field_never_holds_a_safety_change():
    from custom_components.volcast.core.control.cycle import note_denied
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, _hours(range(2, 5)), memory, 1000.0)
    safe = _hours([], idle=range(16, 19))
    d0, _ = decide(safe, reading(dev.words), memory=memory, now_mono=9000.0)
    other = [w.key for w in d0.writes if w.key.startswith("tou.") and not w.key.endswith("grid_charge")][-1]
    note_denied(memory, other, d0.flat[other], d0.device[other], 8990.0, max_hold_s=6 * 3600)
    d, _ = _cycle(dev, safe, memory, 9000.0)
    assert [w.key for w in d.writes] == [ENABLE] and not _grid_charging_live(dev)


def test_shortened_charge_window_is_written_within_the_rewrite_interval():
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, _hours(range(2, 5)), memory, 1000.0)
    d, _ = _cycle(dev, _hours(range(2, 3)), memory, 1000.0 + 600)   # ładowanie tylko 02–03
    assert d.status == "write" and dev.enabled
    starts = {p.start_min: p.grid_charge for p in reading(dev.words).programs}
    assert all(not gc for s, gc in starts.items() if 180 <= s < 300)


def test_extended_charge_window_waits_for_the_rewrite_interval():
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, _hours(range(2, 5)), memory, 1000.0)
    d, _ = _cycle(dev, _hours(range(1, 5)), memory, 1000.0 + 600)   # dłuższe ładowanie — nie „bezpieczne”
    assert (d.status, d.writes) == ("idle", []) and "tou_rewrite_interval" in d.notes


def test_safety_off_writes_are_capped_per_day():
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, _hours(range(2, 5)), memory, 1000.0)
    for _ in range(60):
        memory.budget.note(ENABLE, NOW.timestamp() - 60)
    offs = 0
    for i in range(40):
        dev.words[EN_ADDR] |= 1                                        # ktoś ciągle włącza z powrotem
        d, _ = _cycle(dev, _hours([]), memory, 5000.0 + 300.0 * i)
        offs += d.status == "write"
    assert offs <= 24


def test_unsupported_schedule_with_live_stale_charging_switches_off():
    dev = Device()
    memory = ControlMemory.for_profile(DEYE)
    _cycle(dev, _hours(range(2, 5)), memory, 1000.0)
    memory.unsupported.add("tou")
    d, _ = _cycle(dev, _hours([]), memory, 5000.0)
    assert [w.key for w in d.writes] == [ENABLE] and not dev.enabled
    d, _ = _cycle(dev, _hours(range(2, 5)), memory, 5300.0)
    assert (d.status, d.reason) == ("blocked", "tou_unsupported")
