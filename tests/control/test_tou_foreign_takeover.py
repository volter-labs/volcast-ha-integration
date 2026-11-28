"""Okna czasowe (Deye): pierwsza obca zmiana pola programu = przejęcie tego pola; powrót do migawki przywraca
rejestr tylko, gdy ma on jeszcze wartość, którą ostatnio zapisaliśmy — także po restarcie. Oba transporty
trybu bezpośredniego (Modbus RTU po TCP i hub `modbus` HA) zachowują się tak samo. GoodWe (tryb + nastawa)
zostaje przy dwóch rozjazdach."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from tests.control.test_executor_direct import (  # noqa: F401 — fixture `issues`
    POWER, TOU_RAW, _deye, _owned_sell, issues)
from tests.control.test_ha_modbus_control import TOU, HubHarness

START2 = 149                                     # start programu 2 (Deye: starty 148..153)
KEY2 = "tou.2.start"


class _Rig:
    """Wspólny widok banku rejestrów obu transportów: słowa TOU, zmiana z zewnątrz, liczba zapisów rejestru."""

    def __init__(self, kind: str, h, bank=None) -> None:
        self.kind, self.h, self._bank = kind, h, bank
        self.events: list[tuple[str, dict]] = []
        bus = getattr(h.hass, "bus", None)
        if bus is None:
            h.hass.bus = bus = SimpleNamespace()
        bus.async_fire = lambda event_type, data=None, *a, **k: self.events.append((event_type, dict(data or {})))

    def words(self) -> list[int]:
        return list(self._bank.read(146, 32)) if self.kind == "direct" else self.h.hub.words(TOU)

    def poke(self, addr: int, value: int) -> None:
        """Właściciel zmienia rejestr poza HA (aplikacja producenta)."""
        if self.kind == "direct":
            self._bank.poke(addr, value)
        else:
            self.h.hub.bank[addr] = value

    def writes_to(self, addr: int) -> int:
        if self.kind == "direct":
            return sum(1 for a, _ in self._bank.writes if a == addr)
        return sum(1 for c in self.h.service_writes()
                   if c["address"] <= addr < c["address"] + len(c["value"]))

    async def restart(self) -> None:
        await self.h.restart()
        bus = self.h.hass.bus
        bus.async_fire = lambda event_type, data=None, *a, **k: self.events.append((event_type, dict(data or {})))


@pytest.fixture(params=["direct", "hub"])
def rig(request, make_hass, issues, rtu_tcp_sim, deye_bank):
    async def make() -> _Rig:
        if request.param == "direct":
            h = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
            return _Rig("direct", h, deye_bank)
        h = await HubHarness(make_hass).start(raw=TOU_RAW)
        return _Rig("hub", h)
    return make


async def _owned(r: _Rig) -> list[int]:
    await r.h.ex.async_tick()
    assert r.h.ex.owned and r.h.ex._state.tou_snapshot is not None
    return r.words()


def _minutes(hhmm: int) -> float:
    """Start programu Deye: słowo HHMM → minuty doby (widok urządzenia)."""
    return float(hhmm // 100 * 60 + hhmm % 100)


def _owner_start(ours: list[int]) -> int:
    return ours[START2 - 146] + 5


@pytest.mark.asyncio
async def test_first_foreign_change_of_a_window_is_a_takeover(rig):
    r = await rig()
    try:
        ours = await _owned(r)
        owner = _owner_start(ours)
        r.poke(START2, owner)
        n = r.writes_to(START2)
        await r.h.cycle(400.0)
        ex = r.h.ex
        assert KEY2 in ex._state.taken_over and ex.paused             # pierwsza zmiana — przejęcie
        assert ex.foreign_changes and ex.foreign_changes[-1]["key"] == KEY2
        assert any(t == "volcast_control_takeover" and d.get("key") == KEY2 for t, d in r.events)
        for _ in range(3):
            await r.h.cycle(400.0)
        assert r.writes_to(START2) == n and r.words()[START2 - 146] == owner   # okno 2 nie jest pisane
    finally:
        await r.h.close()


@pytest.mark.asyncio
async def test_disabling_control_keeps_the_owner_window_and_restores_the_rest(rig):
    r = await rig()
    try:
        before = r.words()
        ours = await _owned(r)
        assert ours != before
        owner = _owner_start(ours)
        r.poke(START2, owner)
        await r.h.cycle(400.0)
        assert KEY2 in r.h.ex._state.taken_over
        await r.h.ex.async_set_consent(False)
        await r.h.cycle()
        after = r.words()
        assert not r.h.ex.owned
        assert after[START2 - 146] == owner                            # okno 2 — wartość właściciela
        expect = list(before)
        expect[START2 - 146] = owner
        assert after == expect                                         # reszta — z migawki
    finally:
        await r.h.close()


@pytest.mark.asyncio
async def test_change_not_yet_noticed_is_left_on_restore_and_reported(rig):
    """Zmiana właściciela tuż przed wyłączeniem sterowania (bez cyklu, który by ją zauważył): powrót
    porównuje rejestr z naszym ostatnim zapisem i zostawia wartość właściciela."""
    r = await rig()
    try:
        before = r.words()
        ours = await _owned(r)
        owner = _owner_start(ours)
        r.poke(START2, owner)
        await r.h.ex.async_set_consent(False)
        await r.h.cycle()
        after = r.words()
        assert not r.h.ex.owned
        expect = list(before)
        expect[START2 - 146] = owner
        assert after == expect
        skipped = r.h.ex.restore_skipped
        assert [s["key"] for s in skipped] == [KEY2]
        assert skipped[0]["current"] == _minutes(owner) and skipped[0]["snapshot"] == _minutes(before[START2 - 146])
        assert any(t == "volcast_control_restore_skipped" and d.get("key") == KEY2 for t, d in r.events)
    finally:
        await r.h.close()


@pytest.mark.asyncio
async def test_restore_after_restart_honours_the_owner_change(rig):
    r = await rig()
    try:
        before = r.words()
        ours = await _owned(r)
        await r.restart()
        assert r.h.ex.owned
        owner = _owner_start(ours)
        r.poke(START2, owner)                                          # zmiana w czasie przerwy
        await r.h.ex.async_set_consent(False)
        await r.h.cycle()
        after = r.words()
        assert not r.h.ex.owned
        expect = list(before)
        expect[START2 - 146] = owner
        assert after == expect
    finally:
        await r.h.close()


@pytest.mark.asyncio
async def test_without_a_foreign_change_restore_is_complete(rig):
    """Regresja: bez obcej zmiany powrót przywraca całą migawkę — od razu i po restarcie."""
    r = await rig()
    try:
        before = r.words()
        await _owned(r)
        await r.h.ex.async_set_consent(False)
        await r.h.cycle()
        assert r.words() == before and not r.h.ex.owned and r.h.ex.restore_skipped == []
    finally:
        await r.h.close()


@pytest.mark.asyncio
async def test_without_a_foreign_change_restore_after_restart_is_complete(rig):
    r = await rig()
    try:
        before = r.words()
        await _owned(r)
        await r.restart()
        await r.h.ex.async_set_consent(False)
        await r.h.cycle()
        assert r.words() == before and not r.h.ex.owned
    finally:
        await r.h.close()


@pytest.mark.asyncio
async def test_record_without_our_written_values_restores_as_before(rig):
    """Własność zapisana przez wersję bez wartości odniesienia: po restarcie powrót jak dotąd (cała migawka
    poza kluczami przejętymi) — nigdy pozostawienie naszych programów na falowniku."""
    r = await rig()
    try:
        before = r.words()
        ours = await _owned(r)
        r.h.ex._state.tou_written = None
        assert await r.h.ex._async_save("test")
        await r.restart()
        assert r.h.ex.owned and r.h.ex._state.tou_written is None
        r.poke(START2, _owner_start(ours))
        await r.h.ex.async_set_consent(False)
        await r.h.cycle()
        assert r.words() == before and not r.h.ex.owned
    finally:
        await r.h.close()


@pytest.mark.asyncio
async def test_our_uncertain_write_that_landed_is_not_a_takeover(rig):
    """Nasz zapis o nieznanym wyniku, który doszedł: odczyt równy planowi to nie zmiana właściciela."""
    r = await rig()
    try:
        await _owned(r)
        mem = r.h.ex._memory
        mine = mem.last_written[KEY2]
        mem.last_written[KEY2] = mine - 30.0                           # poprzedni zapis; ostatni bez echa
        await r.h.cycle(400.0)
        assert KEY2 not in r.h.ex._state.taken_over and not r.h.ex.paused
        assert mem.last_written[KEY2] == mine and r.h.ex._state.tou_written[KEY2] == mine
    finally:
        await r.h.close()


# ── tryb + nastawa (GoodWe): próg dwóch rozjazdów bez zmian ───────────────


@pytest.mark.asyncio
async def test_mode_setpoint_profile_keeps_the_two_drift_rule(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    h = await _owned_sell(make_hass, goodwe_udp_sim, goodwe_bank)
    try:
        goodwe_bank.poke(POWER, 4000)
        await h.cycle()
        assert not h.ex.paused and not h.ex._state.taken_over         # pierwszy rozjazd — uzgodnienie
        goodwe_bank.poke(POWER, 4000)
        await h.cycle()
        assert h.ex.paused                                             # drugi — przejęcie
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_foreign_hub_service_write_to_a_window_is_a_takeover_and_survives_restore(make_hass, issues):
    """Hub `modbus` HA: drugi sterownik pisze usługą `modbus.write_register` — kolizja na łączu i przejęcie pola
    od pierwszej zmiany; powrót zostawia jego wartość."""
    h = await HubHarness(make_hass).start(raw=TOU_RAW)
    r = _Rig("hub", h)
    try:
        before = r.words()
        ours = await _owned(r)
        owner = _owner_start(ours)
        await h.hub.foreign_write(START2, owner)
        await h.cycle(400.0)
        assert KEY2 in h.ex._state.taken_over and h.ex.paused
        await h.ex.async_set_consent(False)
        await h.cycle()
        expect = list(before)
        expect[START2 - 146] = owner
        assert r.words() == expect and not h.ex.owned
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_restart_in_the_middle_of_a_rewrite_never_keeps_our_programs(make_hass, issues):
    """HA pada w trakcie sekwencji zapisów (przed zapisem stanu po niej): pola w toku nie mają odniesienia, więc
    powrót po restarcie przywraca całą migawkę — nasz nowy program nie jest brany za zmianę właściciela."""
    h = HubHarness(make_hass)
    r = _Rig("hub", h)
    before = r.words()
    stored = []
    h.hub.on_write = lambda data: stored.append(copy.deepcopy(h.store._store._data))
    await h.start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert h.ex.owned and len(stored) > 2
        h.store._store._data = stored[-1]                  # stan magazynu z chwili ostatniej ramki sekwencji
        h.hub.on_write = None
        await r.restart()
        assert h.ex.owned
        await h.ex.async_set_consent(False)
        await h.cycle()
        assert r.words() == before and not h.ex.owned
    finally:
        await h.close()


def _stronger_charge_plan() -> dict:
    from tests.control.test_executor_direct import tou_raw
    raw = {**tou_raw(), "schedule_id": "tou2"}
    for s in raw["slots"]:
        if s.get("mode") == "charge":
            s["power_w"] = 5000
    return raw


def _frames(r: _Rig) -> int:
    return len(r._bank.writes) if r.kind == "direct" else len(r.h.service_writes())


@pytest.mark.asyncio
async def test_store_failure_before_a_rewrite_sends_no_frame(rig):
    """Odniesienia nie da się zapisać przed sekwencją → żadnej ramki (jak przy migawce). Po restarcie: bez
    fałszywego przejęcia i pełny powrót."""
    from custom_components.volcast.core.slot import parse_schedule
    r = await rig()
    try:
        before = r.words()
        await _owned(r)
        raw2 = _stronger_charge_plan()
        await r.h.ex.async_on_plan(raw2, parse_schedule(raw2))
        real_save = r.h.store.async_save

        async def broken(_state):
            raise OSError("disk full")
        r.h.store.async_save = broken
        n = _frames(r)
        await r.h.cycle(4000.0)
        assert _frames(r) == n                                          # nic nie poszło do falownika
        assert r.h.ex.last_decision.reason == "store_failed"
        r.h.store.async_save = real_save
        await r.restart()
        await r.h.cycle(400.0)
        assert not r.h.ex.paused and not r.h.ex._state.taken_over
        await r.h.ex.async_set_consent(False)
        await r.h.cycle()
        assert r.words() == before and not r.h.ex.owned
    finally:
        await r.h.close()


@pytest.mark.asyncio
async def test_unknown_outcome_write_has_no_reference_and_is_restored_after_restart(rig):
    """Zapis pola z wynikiem ERROR, który jednak doszedł; powrót od razu niemożliwy (brak świeżego odczytu),
    potem restart (pamięć niepewności ginie): pole bez odniesienia wraca z migawki, nie zostaje jako „właściciela”."""
    from custom_components.volcast.core.write_sequence import ERROR as W_ERROR
    r = await rig()
    try:
        before = r.words()
        ex = r.h.ex
        failed = []
        real = ex._writer.async_write

        async def write(w):
            out = await real(w)
            if not failed and w.key.startswith("tou."):
                failed.append(w.key)
                return W_ERROR                                          # ramka doszła, wynik nieznany
            return out
        ex._writer.async_write = write
        r.h.conn.fresh_wait_s = 0.2
        await r.h.conn._poll_lock.acquire()
        try:
            await ex.async_tick()
        finally:
            r.h.conn._poll_lock.release()
        assert failed and ex.owned and ex._tou_restore_pending
        assert failed[0] not in ex._memory.last_written and failed[0] not in ex._state.tou_written
        ex._writer.async_write = real
        await r.restart()
        await r.h.ex.async_set_consent(False)
        await r.h.cycle()
        assert r.words() == before and not r.h.ex.owned
    finally:
        await r.h.close()


@pytest.mark.asyncio
async def test_new_ownership_clears_the_previous_restore_report(rig):
    r = await rig()
    try:
        ours = await _owned(r)
        r.poke(START2, _owner_start(ours))
        await r.h.ex.async_set_consent(False)
        await r.h.cycle()
        assert r.h.ex.restore_skipped and not r.h.ex.owned
        await r.h.ex.async_set_consent(True)
        await r.h.cycle(4000.0)
        assert r.h.ex.owned and r.h.ex.restore_skipped == []
    finally:
        await r.h.close()
