"""Kolizje klientów tego samego falownika: odmowa statyczna (fail-closed) i wykrywanie w pracy."""
from custom_components.volcast.core.control.conflict import (
    CLEAR_AFTER_S, DRIFT_WINDOW_S, STRAY_WINDOW_S, ContentionMonitor, DriftTracker, EntrySnap, drifted_keys,
    static_conflicts)
from custom_components.volcast.core.transports.base import TransportStats


# ── statycznie ────────────────────────────────────────────────────────────


def test_enabled_inverter_integration_on_same_host_conflicts():
    entries = [EntrySnap("goodwe", ("192.168.1.50",), False), EntrySnap("solarman", ("192.168.1.60",), False)]
    assert static_conflicts("192.168.1.50", entries) == ("goodwe",)


def test_disabled_entry_does_not_conflict():
    assert static_conflicts("192.168.1.50", [EntrySnap("goodwe", None, True)]) == ()


def test_second_volcast_entry_conflicts_but_not_self():
    entries = [EntrySnap("volcast", ("192.168.1.50",), False, is_self=True),
               EntrySnap("volcast", ("192.168.1.50",), False)]
    assert static_conflicts("192.168.1.50", entries) == ("volcast",)


def test_volcast_entry_without_direct_target_does_not_conflict():
    assert static_conflicts("192.168.1.50", [EntrySnap("volcast", (), False)]) == ()


def test_unknown_host_of_enabled_inverter_entry_conflicts():
    assert static_conflicts("192.168.1.50", [EntrySnap("goodwe", None, False)]) == ("goodwe",)
    assert static_conflicts("192.168.1.50", [EntrySnap("solarman", (), False)]) == ("solarman",)
    assert static_conflicts("192.168.1.50", [EntrySnap("modbus", ("inverter.local",), False)]) == ("modbus",)


def test_entry_resolving_to_several_addresses_conflicts_on_any():
    entries = [EntrySnap("goodwe", ("192.168.1.9", "192.168.1.50"), False)]
    assert static_conflicts("192.168.1.50", entries) == ("goodwe",)


def test_unrelated_domains_ignored_even_without_host():
    entries = [EntrySnap("met", None, False), EntrySnap("mqtt", ("192.168.1.50",), False)]
    assert static_conflicts("192.168.1.50", entries) == ()


def test_yaml_modbus_without_entry_not_static_conflict():
    # YAML-owa integracja `modbus` nie ma wpisu konfiguracji — statycznie jej nie widać (zostaje
    # wykrywanie w pracy); wpis konfiguracji domeny `modbus` na tym hoście — kolizja.
    assert static_conflicts("192.168.1.50", []) == ()
    assert static_conflicts("192.168.1.50", [EntrySnap("modbus", ("192.168.1.50",), False)]) == ("modbus",)


def test_ipv6_and_ipv4_normalized():
    assert static_conflicts("::ffff:192.168.1.50", [EntrySnap("goodwe", ("192.168.1.50",), False)]) == ("goodwe",)
    assert static_conflicts("192.168.1.50", [EntrySnap("goodwe", ("::ffff:c0a8:132",), False)]) == ("goodwe",)
    assert static_conflicts("fd00::1", [EntrySnap("solarman", ("FD00:0000::0001",), False)]) == ("solarman",)
    assert static_conflicts("fe80::1%eth0", [EntrySnap("solarman", ("fe80::1",), False)]) == ("solarman",)
    assert static_conflicts("fd00::1", [EntrySnap("solarman", ("fd00::2",), False)]) == ()


def test_many_conflicts_listed_once_in_order():
    entries = [EntrySnap("solarman", None, False), EntrySnap("goodwe", ("192.168.1.50",), False),
               EntrySnap("goodwe", ("192.168.1.50",), False)]
    assert static_conflicts("192.168.1.50", entries) == ("solarman", "goodwe")


def test_unparsable_target_is_a_conflict():
    assert static_conflicts("inverter.local", []) == ("invalid_target",)


# ── w pracy ───────────────────────────────────────────────────────────────


def test_stray_frames_raise_conflict_and_clear_later():
    m = ContentionMonitor()
    s = TransportStats()
    for t in (0.0, 100.0, 200.0):
        s.stray += 1
        m.note_stats(s, t)
    assert (m.state, m.reason) == ("conflict", "stray_frames")
    m.tick(200.0 + 1801.0)
    assert m.state == "ok" and m.reason is None


def test_conflict_holds_while_signals_continue():
    m = ContentionMonitor()
    s = TransportStats(stray=3)
    m.note_stats(s, 0.0)
    assert m.state == "conflict"
    s.stray += 1
    m.note_stats(s, CLEAR_AFTER_S - 10.0)
    m.tick(CLEAR_AFTER_S + 10.0)
    assert m.state == "conflict"                       # ostatni sygnał 20 s temu
    m.tick(2 * CLEAR_AFTER_S)
    assert m.state == "ok"


def test_peer_resets_raise_conflict():
    m = ContentionMonitor()
    s = TransportStats()
    for t in (0.0, 60.0, 120.0):
        s.peer_resets += 1
        s.last_ok_mono = t - 5.0                        # odczyty działają
        m.note_stats(s, t)
    assert (m.state, m.reason) == ("conflict", "peer_resets")


def test_peer_resets_without_working_reads_are_not_contention():
    m = ContentionMonitor()
    s = TransportStats()
    for t in (0.0, 60.0, 120.0):
        s.peer_resets += 1                              # falownik wyłączony / poza siecią
        m.note_stats(s, t)
    assert m.state == "ok"


def test_signals_outside_window_forgotten():
    m = ContentionMonitor()
    s = TransportStats()
    for t in (0.0, STRAY_WINDOW_S + 1.0, 2 * STRAY_WINDOW_S + 2.0):
        s.stray += 1
        m.note_stats(s, t)
    assert m.state == "ok"
    s.stray += 2
    m.note_stats(s, 2 * STRAY_WINDOW_S + 3.0)
    assert m.state == "conflict"


def test_unsolicited_frames_never_raise_conflict():
    m = ContentionMonitor()
    s = TransportStats()
    for t in range(0, 600, 30):
        s.unsolicited += 1
        m.note_stats(s, float(t))
    assert m.state == "ok"


def test_counter_reset_of_new_transport_counts_from_zero():
    m = ContentionMonitor()
    m.note_stats(TransportStats(stray=2), 0.0)
    m.note_stats(TransportStats(stray=1), 10.0)        # nowy transport: liczniki od zera
    assert m.state == "conflict"


def test_other_counters_do_not_raise_conflict():
    m = ContentionMonitor()
    s = TransportStats()
    for t in range(0, 600, 30):
        s.timeouts += 1
        s.reconnects += 1
        s.channel_resets += 1
        s.requests += 3
        m.note_stats(s, float(t))
    assert m.state == "ok"


# ── rozjazd nastaw: przejęcie, nie kolizja ────────────────────────────────


def test_single_drift_is_reconcile_not_takeover():
    d = DriftTracker()
    assert d.note_drift("mode", 0.0) is False
    assert d.note_drift("mode", 60.0) is True


def test_drift_of_other_key_or_outside_window_is_not_takeover():
    d = DriftTracker()
    assert d.note_drift("mode", 0.0) is False
    assert d.note_drift("power_w", 10.0) is False
    assert d.note_drift("mode", DRIFT_WINDOW_S + 1.0) is False


def test_own_write_does_not_clear_drift_history():
    # Właściciel zmienia klucz, my uzgadniamy (zapis), właściciel zmienia znowu w 30 min — przejęcie.
    d = DriftTracker()
    assert d.note_drift("mode", 0.0) is False
    d.note_own_write("mode", 10.0)
    assert d.note_drift("mode", 60.0) is True


def test_forget_clears_drift_history_when_the_plan_changes():
    d = DriftTracker()
    d.note_drift("mode", 0.0)
    d.forget("mode")                                    # nowa wartość planu — nowa historia
    assert d.note_drift("mode", 60.0) is False


def test_reading_started_before_write_end_is_not_drift_evidence():
    d = DriftTracker()
    assert d.usable(0.0) is True                        # bez naszych zapisów każdy odczyt się liczy
    d.note_own_write("mode", 100.0)
    assert d.usable(99.0) is False and d.usable(100.5) is True
    d.note_own_write("power_w", 50.0)                   # ostatni zapis to nadal koniec o 100 s
    assert d.usable(99.0) is False


def test_drifted_keys_uses_register_quantum():
    last = {"power_w": 625.6, "mode": "sell_power", "soc_min": 20.0, "soc_max": 90.0}
    device = {"power_w": 626.0, "mode": "auto", "soc_min": 22.0}
    assert drifted_keys(last, device) == ("mode", "soc_min")
    assert drifted_keys(last, {**device, "soc_min": None}) == ("mode",)
