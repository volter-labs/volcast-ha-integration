"""Guardy ze stanem — port testów I-6/I-8 i uzgadniania referencyjnego wykonawcy."""
import pytest

from custom_components.volcast.core.guard_state import DirectionLimiter, WriteThrottle

P = {"mode": "sell_power", "power_w": 2500.0, "soc_min": 20.0, "export_limit_enabled": 1.0}


# ── I-6: throttling zapisów ─────────────────────────────────────────────────

def test_first_write_passes():
    assert WriteThrottle(60).filter(P, 0.0) == set(P)


def test_unchanged_value_never_rewritten_even_after_interval():
    t = WriteThrottle(60)
    t.record(P, P.keys(), 0.0)
    assert t.filter(P, 10_000.0) == set()


def test_changed_value_waits_for_interval():
    t = WriteThrottle(60)
    t.record(P, P.keys(), 0.0)
    changed = {**P, "power_w": 3000.0}
    assert t.filter(changed, 30.0) == set()
    assert t.filter(changed, 60.0) == {"power_w"}


def test_failed_write_does_not_move_memory():
    t = WriteThrottle(60)
    t.record(P, ["mode"], 0.0)                 # tylko tryb się zapisał
    assert t.filter(P, 1.0) == {"power_w", "soc_min", "export_limit_enabled"}


def test_unsupported_register_is_not_a_write():
    # Rejestr nieobsługiwany (wyjątek Modbus 2) nie trafia do listy zapisanych —
    # pamięć stoi, więc nie udajemy, że falownik ma tę nastawę.
    t = WriteThrottle(60)
    flat = {**P, "soc_max": 90.0}
    t.record(flat, ["mode", "power_w", "soc_min", "export_limit_enabled"], 0.0)
    assert t.filter(flat, 1.0) == {"soc_max"}
    assert t.reconcile({"soc_max": 50.0}) == 0     # brak pamięci = nic do uzgodnienia


def test_record_ignores_written_keys_absent_from_params():
    t = WriteThrottle(60)
    t.record(P, ["mode", "soc_max"], 0.0)
    assert "soc_max" in t.filter({"soc_max": 90.0}, 1.0)


def test_retry_after_failure_is_not_throttled_by_earlier_success():
    t = WriteThrottle(60)
    t.record(P, P.keys(), 0.0)
    changed = {**P, "power_w": 3000.0}
    assert t.filter(changed, 60.0) == {"power_w"}
    t.record(changed, [], 60.0)                # zapis padł
    assert t.filter(changed, 61.0) == {"power_w"}


def test_reconcile_forgets_external_change_but_ignores_register_quantum():
    t = WriteThrottle(60)
    flat = {**P, "export_limit_w": 625.6}
    t.record(flat, flat.keys(), 0.0)
    assert t.reconcile({"export_limit_w": 626.0, "mode": "sell_power"}) == 0
    assert t.reconcile({"mode": "discharge_battery", "power_w": 2000.0}) == 2
    assert t.filter(flat, 1.0) == {"mode", "power_w"}


def test_reconcile_catches_difference_of_exactly_one_quantum():
    t = WriteThrottle(60)
    t.record(P, P.keys(), 0.0)
    assert t.reconcile({"power_w": 2499.0}) == 1


def test_reconcile_on_agreement_changes_nothing():
    t = WriteThrottle(60)
    t.record(P, P.keys(), 0.0)
    assert t.reconcile(P) == 0
    assert t.filter(P, 5000.0) == set()


def test_reconcile_mode_as_number_is_a_caller_error():
    # Odczyt musi mieć postać `Params.flatten()` (nazwa trybu). Surowa wartość
    # rejestru kasowałaby pamięć w każdym cyklu i przepisywała tryb do NVM.
    t = WriteThrottle(60)
    t.record(P, P.keys(), 0.0)
    with pytest.raises(TypeError):
        t.reconcile({"mode": 3.0})
    with pytest.raises(TypeError):
        t.reconcile({"power_w": "2500"})


def test_reconcile_accepts_bool_as_number():
    t = WriteThrottle(60)
    t.record(P, P.keys(), 0.0)
    assert t.reconcile({"export_limit_enabled": True}) == 0
    assert t.reconcile({"export_limit_enabled": False}) == 1


def test_clock_step_backwards_does_not_silence_changed_value():
    # Zegar cofnięty (np. korekta NTP) nie może wyciszyć zmiany na godzinę —
    # błąd idzie w stronę ponownej synchronizacji, nie ciszy.
    t = WriteThrottle(60)
    t.record(P, P.keys(), 10_000.0)
    changed = {**P, "power_w": 3000.0}
    assert t.filter(changed, 6_400.0) == {"power_w"}
    assert t.filter(P, 6_400.0) == set()       # niezmienione dalej nie jedzie


def test_reconcile_ignores_unknown_keys():
    t = WriteThrottle(60)
    t.record(P, P.keys(), 0.0)
    assert t.reconcile({"soc_max": 90.0}) == 0


def test_tou_keys_are_throttled_independently():
    t = WriteThrottle(60)
    flat = {"tou.1.start": 120.0, "tou.1.soc": 80.0}
    t.record(flat, flat.keys(), 0.0)
    assert t.filter({**flat, "tou.1.soc": 90.0}, 61.0) == {"tou.1.soc"}


# ── I-8: anty-oscylacja ─────────────────────────────────────────────────────

def test_direction_first_setting_is_free_and_same_direction_is_not_change():
    d = DirectionLimiter(1)
    assert d.allows("charge", 0.0)
    d.record("charge", 0.0)
    assert d.allows("charge", 1.0)
    assert d.allows("neutral", 1.0) and d.allows("idle", 1.0)


def test_direction_budget_exhausts_and_renews_after_hour():
    d = DirectionLimiter(2)
    d.record("charge", 0.0)
    d.record("discharge", 10.0)
    d.record("charge", 20.0)
    assert not d.allows("discharge", 30.0)
    assert d.allows("discharge", 3611.0)


def test_fifth_change_in_hour_blocked_with_budget_four():
    d = DirectionLimiter(4)
    d.record("charge", 0.0)
    direction, t = "discharge", 10.0
    for _ in range(4):
        assert d.allows(direction, t)
        d.record(direction, t)
        direction = "charge" if direction == "discharge" else "discharge"
        t += 10.0
    assert not d.allows(direction, t)
    assert d.allows(direction, t + 3600.0)


def test_neutral_does_not_erase_last_direction():
    d = DirectionLimiter(1)
    d.record("charge", 0.0)
    d.record("neutral", 5.0)
    d.record("discharge", 10.0)                # zmiana charge → discharge zużywa budżet
    assert not d.allows("charge", 20.0)


def test_neutral_and_idle_never_consume_budget():
    d = DirectionLimiter(1)
    d.record("charge", 0.0)
    for i, direction in enumerate(("neutral", "idle", "neutral", "idle")):
        d.record(direction, 10.0 + i)
    assert d.allows("discharge", 20.0)


def test_budget_larger_than_history_is_still_enforced():
    # Budżet pochodzi z profilu (do 60/h) i może przekraczać domyślną historię (16).
    d = DirectionLimiter(20)
    d.record("charge", 0.0)
    direction, t = "discharge", 1.0
    for _ in range(20):
        assert d.allows(direction, t)
        d.record(direction, t)
        direction = "charge" if direction == "discharge" else "discharge"
        t += 1.0
    assert not d.allows(direction, t), "21. zmiana w godzinie musi zostać zablokowana"


def test_window_edge_is_inclusive():
    d = DirectionLimiter(1)
    d.record("charge", 0.0)
    d.record("discharge", 100.0)
    assert not d.allows("charge", 3700.0)      # wpis sprzed dokładnie okna jeszcze się liczy
    assert d.allows("charge", 3700.001)


def test_old_changes_are_pruned_on_record():
    # Pamięć ograniczona oknem: po godzinie stare zmiany znikają przy zapisie.
    d = DirectionLimiter(20)
    d.record("charge", 0.0)
    direction = "discharge"
    for t in range(1, 21):
        d.record(direction, float(t))
        direction = "charge" if direction == "discharge" else "discharge"
    assert len(d._changes) == 20
    d.record(direction, 4000.0)
    assert len(d._changes) == 1


def test_clock_step_backwards_does_not_block_for_an_hour():
    d = DirectionLimiter(1)
    d.record("charge", 10_000.0)
    d.record("discharge", 10_010.0)
    assert not d.allows("charge", 10_020.0)
    # Zegar cofnął się o godzinę: wpisy „z przyszłości" są niewiarygodne i wypadają.
    assert d.allows("charge", 6_420.0)


@pytest.mark.parametrize("bad", ["charging", "", "auto", None])
def test_unknown_direction_is_rejected(bad):
    d = DirectionLimiter(4)
    with pytest.raises(ValueError):
        d.allows(bad, 0.0)
    with pytest.raises(ValueError):
        d.record(bad, 0.0)
