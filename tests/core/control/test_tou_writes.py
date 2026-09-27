"""Sekwencja zapisów okien czasowych: włącznik OFF, programy, włącznik ON; migawka i powrót."""
import asyncio

from custom_components.volcast.core.control.tou_writes import (
    async_run_tou_writes, run_tou_writes, tou_restore_writes, tou_snapshot)
from custom_components.volcast.core.engines.time_window import baseline_programs
from custom_components.volcast.core.modbus.reading import build_reading
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage, RegisterWrite, encode_tou_enable
from custom_components.volcast.core.write_sequence import DENIED, ERROR, OK, UNSUPPORTED, AdjustedOutcome
from tests.core.golden import T0
from tests.sim.fixtures import deye_words

DEYE = load_builtin("deye-sg")


def _w(key, addr, value=1):
    return RegisterWrite(key, addr, value)


WRITES = [_w("tou_enable", 146, 0xFE), _w("tou.1.soc", 166), _w("tou.1.power_w", 154),
          _w("tou.2.soc", 167), _w("tou.2.power_w", 155), _w("tou.3.soc", 168), _w("tou_enable", 146, 0xFF)]


def _reading(**over):
    words = deye_words()
    words.update({int(a): v for a, v in over.items()})
    return build_reading(DEYE, RegisterImage(words), at_mono=0.0, at_utc=T0)


# ── sekwencja ─────────────────────────────────────────────────────────────


def test_failed_field_holds_rest_of_program_and_later_programs():
    rep = run_tou_writes(WRITES, lambda w: {"tou.2.soc": ERROR}.get(w.key, OK))
    assert rep.written == ["tou_enable", "tou.1.soc", "tou.1.power_w"]
    assert rep.failed == ["tou.2.soc"]
    assert rep.held == ["tou.2.power_w", "tou.3.soc", "tou_enable"]
    assert rep.restore_needed is True
    assert rep.ambiguous == ["tou.2.soc"] and rep.enable_written is False


def test_unsupported_mid_sequence_holds_enable_and_restores():
    rep = run_tou_writes(WRITES, lambda w: {"tou.1.power_w": UNSUPPORTED}.get(w.key, OK))
    assert rep.unsupported == ["tou.1.power_w"]
    assert "tou_enable" in rep.held and rep.restore_needed is True


def test_denied_field_holds_and_restores():
    rep = run_tou_writes(WRITES, lambda w: {"tou.3.soc": DENIED}.get(w.key, OK))
    assert rep.held == ["tou_enable"] and rep.restore_needed is True and rep.ambiguous == []


def test_held_field_by_throttle_holds_rest_without_restore():
    rep = run_tou_writes(WRITES, lambda w: OK, pre_held={"tou.2.soc"})
    assert rep.held == ["tou.2.soc", "tou.2.power_w", "tou.3.soc", "tou_enable"]
    assert rep.restore_needed is False           # włącznik OFF = samokonsumpcja; następny cykl dokończy


def test_failed_disable_stops_everything():
    calls = []
    rep = run_tou_writes(WRITES, lambda w: calls.append(w.key) or (ERROR if w.key == "tou_enable" else OK))
    assert rep.written == [] and rep.failed == ["tou_enable"] and calls == ["tou_enable"]
    assert rep.restore_needed is False


def test_full_sequence_enables_last():
    seen = []
    rep = run_tou_writes(WRITES, lambda w: seen.append(w.key) or OK)
    assert seen == [w.key for w in WRITES] and rep.held == [] and rep.enable_written is True


def test_adjusted_counts_as_written_and_writer_exception_is_error():
    def write(w):
        if w.key == "tou.1.power_w":
            return AdjustedOutcome(2500.0)
        if w.key == "tou.2.soc":
            raise RuntimeError("boom")
        return OK
    rep = run_tou_writes(WRITES, write)
    assert "tou.1.power_w" in rep.written and rep.actual == {"tou.1.power_w": 2500.0}
    assert rep.failed == ["tou.2.soc"] and rep.errors == {"tou.2.soc": "RuntimeError"}


def test_without_disable_nothing_to_restore():
    writes = WRITES[1:]                               # włącznik był już OFF
    rep = run_tou_writes(writes, lambda w: {"tou.2.soc": ERROR}.get(w.key, OK))
    assert rep.restore_needed is False and "tou_enable" in rep.held


def test_async_twin_matches_sync():
    outcome = {"tou.2.soc": ERROR}

    async def aw(w):
        return outcome.get(w.key, OK)
    for pre in ((), ("tou.3.soc",)):
        assert asyncio.run(async_run_tou_writes(WRITES, aw, pre_held=pre)) == \
            run_tou_writes(WRITES, lambda w: outcome.get(w.key, OK), pre_held=pre)


# ── włącznik (słowo 146) ──────────────────────────────────────────────────


def test_tou_enable_rmw_preserves_or_sets_day_bits():
    assert encode_tou_enable(True, 0b0111110, DEYE, None) == RegisterWrite("tou_enable", 146, 0b0111111)
    assert encode_tou_enable(True, 0, DEYE, None).value == 0xFF
    assert encode_tou_enable(True, 0, DEYE, 0b1010).value == 0b1011          # dni właściciela z migawki
    assert encode_tou_enable(False, 0b0111111, DEYE, None).value == 0b0111110
    assert encode_tou_enable(False, 0xFF, DEYE, 0b10).value == 0xFE


# ── migawka i powrót ──────────────────────────────────────────────────────


def test_snapshot_keeps_raw_owner_words():
    snap = tou_snapshot(_reading(**{"172": 0b110, "146": 0b0111111}), DEYE)
    assert snap["tou_word"] == 0b0111111
    assert snap["programs"][0] == [0, 3000, 80, 0b110]
    assert len(snap["programs"]) == 6


def test_snapshot_none_when_unreadable():
    words = deye_words()
    del words[150]
    assert tou_snapshot(build_reading(DEYE, RegisterImage(words), at_mono=0.0, at_utc=T0), DEYE) is None


def test_tou_restore_order_off_programs_owner_word():
    owner = _reading()
    snap = tou_snapshot(owner, DEYE)
    # nasz stan: włącznik ON (inne dni), program 2 zmieniony
    ours = _reading(**{"146": 0xFF, "167": 55, "155": 2000})
    writes = tou_restore_writes(DEYE, snap, ours, soc_reserve=10.0, rated_power_w=10000.0)
    assert writes[0].key == "tou_enable" and writes[0].value & 1 == 0
    assert [w.key for w in writes[1:-1]] == ["tou.2.soc", "tou.2.power_w"]
    assert [w.value for w in writes[1:-1]] == [20, 5000]
    assert writes[-1] == RegisterWrite("tou_enable", 146, snap["tou_word"])


def test_tou_restore_nothing_when_already_owner_state():
    owner = _reading()
    assert tou_restore_writes(DEYE, tou_snapshot(owner, DEYE), owner, soc_reserve=10.0, rated_power_w=10000.0) == []


def test_tou_restore_without_snapshot_uses_self_consume_baseline_enable_off():
    ours = _reading(**{"146": 0xFF})
    writes = tou_restore_writes(DEYE, None, ours, soc_reserve=10.0, rated_power_w=10000.0)
    assert writes[0].key == "tou_enable" and writes[0].value == 0xFE
    assert all(w.key != "tou_enable" for w in writes[1:])             # włącznik zostaje OFF
    base = baseline_programs(DEYE, 10.0, 10000.0)
    starts = {w.key: w.value for w in writes if w.key.endswith(".start")}
    assert starts.get("tou.2.start") == (base[1].start_min // 60) * 100 + base[1].start_min % 60


def test_snapshot_passes_the_store_validator():
    from custom_components.volcast.core.control.tou_writes import validate_tou_snapshot
    snap = tou_snapshot(_reading(), DEYE)
    assert validate_tou_snapshot(snap) == snap
