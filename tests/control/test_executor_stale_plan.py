"""Limit wieku planu baterii bez kontaktu z chmurą: po `STALE_PLAN_S` od ostatniego udanego pobrania
sloty aktywne (ładowanie z sieci, sprzedaż, wymuszone rozładowanie) wykonuje slot zapasowy z chmury."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

from custom_components.volcast.control import executor as ex_mod
from custom_components.volcast.control.runtime import refresh_and_tick
from custom_components.volcast.control.store import ControlState, ControlStore
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.slot import STALE_PLAN_S, parse_schedule

from .ha_fakes import GOODWE_ENTITIES as E, NOW, goodwe_hass
from .test_executor import charge_plan, make, plan, ready


class WallClock:
    """Zegar ścienny testu (UTC), przestawiany między pobraniem planu a cyklem."""

    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def _charge_hours_ago(hours, monkeypatch, *, h=None, store=None):
    """Plan z ładowaniem z sieci 10:00–11:00 pobrany `hours` godzin przed cyklem o 10:00:30."""
    wall = WallClock(NOW + timedelta(seconds=30) - timedelta(hours=hours))
    h, ex = make(h, store=store, monkeypatch=monkeypatch, utc=wall)

    async def go():
        await ready(ex, raw=charge_plan())
        wall.at = NOW + timedelta(seconds=30)
        await ex.async_tick()
    asyncio.run(go())
    return h, ex, wall


def test_grid_charge_fetched_7h_ago_runs_the_fallback_slot(monkeypatch):
    h, ex, _ = _charge_hours_ago(7, monkeypatch)
    d = ex.last_decision
    assert d.intent == "self_consume" and d.fallback is True
    assert h.states.get(E["mode"]).state != "charge_battery"
    assert ex.plan_stale(NOW) is True


def test_grid_charge_fetched_5h_ago_runs_the_plan_slot(monkeypatch):
    h, ex, _ = _charge_hours_ago(5, monkeypatch)
    d = ex.last_decision
    assert d.intent == "charge_grid" and d.fallback is False
    assert h.states.get(E["mode"]).state == "charge_battery"
    assert ex.plan_stale(NOW) is False


def test_sell_slot_fetched_7h_ago_does_not_sell(monkeypatch):
    wall = WallClock(NOW - timedelta(hours=7))
    h, ex = make(monkeypatch=monkeypatch, utc=wall)

    async def go():
        await ready(ex, raw=plan())
        wall.at = NOW + timedelta(seconds=30)
        await ex.async_tick()
    asyncio.run(go())
    assert ex.last_decision.intent == "self_consume" and ex.last_decision.fallback is True
    assert h.states.get(E["mode"]).state != "sell_power"


def test_passive_slot_is_unchanged_when_stale(monkeypatch):
    idle = plan(slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "mode": "idle"}])
    wall = WallClock(NOW - timedelta(hours=7))
    _, ex = make(monkeypatch=monkeypatch, utc=wall)

    async def go():
        await ready(ex, raw=idle)
        wall.at = NOW + timedelta(seconds=30)
        await ex.async_tick()
    asyncio.run(go())
    assert ex.last_decision.intent == "standby" and ex.last_decision.fallback is False


def test_restart_without_network_keeps_the_plan_age(monkeypatch):
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    _charge_hours_ago(7, monkeypatch, h=h, store=store)
    # Restart HA bez sieci: nowy wykonawca z tego samego magazynu, bez pobrania planu.
    _, ex2 = make(h, store=store, monkeypatch=monkeypatch, utc=lambda: NOW + timedelta(seconds=30))
    asyncio.run(_start_and_tick(ex2))
    assert ex2.raw_plan is not None and ex2.plan_fetched_at == NOW + timedelta(seconds=30) - timedelta(hours=7)
    assert ex2.last_decision.intent == "self_consume" and ex2.last_decision.fallback is True
    assert h.states.get(E["mode"]).state != "charge_battery"


def test_restart_with_a_fresh_fetch_time_runs_the_plan(monkeypatch):
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    fetched = NOW - timedelta(hours=5)
    asyncio.run(store.async_save(ControlState(
        plan_raw=charge_plan(), consent=True, local_switch=True, plan_fetched_at=fetched.isoformat())))
    _, ex = make(h, store=store, monkeypatch=monkeypatch)
    asyncio.run(_start_and_tick(ex))
    assert ex.plan_fetched_at == fetched
    assert ex.last_decision.intent == "charge_grid"
    assert h.states.get(E["mode"]).state == "charge_battery"


def test_stored_plan_without_fetch_time_is_stale(monkeypatch):
    # Magazyn z poprzedniej wersji (bez pola): wiek nieznany = przeterminowany do najbliższego pobrania.
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    asyncio.run(store.async_save(ControlState(plan_raw=charge_plan(), consent=True, local_switch=True)))
    _, ex = make(h, store=store, monkeypatch=monkeypatch)
    asyncio.run(_start_and_tick(ex))
    assert ex.plan_fetched_at is None and ex.plan_stale(NOW) is True
    assert ex.last_decision.intent == "self_consume"
    assert h.states.get(E["mode"]).state != "charge_battery"


async def _start_and_tick(ex):
    await ex.async_start()
    await ex.async_tick()


class _Fetcher:
    def __init__(self, result):
        self.result = result

    async def async_refresh(self):
        return self.result


def test_network_back_returns_the_plan_at_the_next_fetch(monkeypatch):
    h, ex, wall = _charge_hours_ago(7, monkeypatch)
    assert ex.last_decision.intent == "self_consume"

    asyncio.run(refresh_and_tick(_Fetcher("network"), ex, None))    # nadal bez sieci
    assert ex.last_decision.intent == "self_consume" and ex.plan_stale(wall()) is True

    ex._clock.t += 3600                                              # poza progiem zmiany trybu
    asyncio.run(refresh_and_tick(_Fetcher("unchanged"), ex, None))  # sieć wróciła, ten sam plan
    assert ex.plan_fetched_at == wall() and ex.plan_stale(wall()) is False
    assert ex.last_decision.intent == "charge_grid" and ex.last_decision.fallback is False
    assert h.states.get(E["mode"]).state == "charge_battery"


def test_rejected_or_auth_failed_fetch_does_not_refresh_the_age(monkeypatch):
    _, ex, wall = _charge_hours_ago(7, monkeypatch)
    before = ex.plan_fetched_at
    for result in ("rejected", "auth", "error"):
        asyncio.run(refresh_and_tick(_Fetcher(result), ex, None))
        assert ex.plan_fetched_at == before and ex.last_decision.intent == "self_consume"


def test_accepted_plan_sets_the_fetch_time_and_persists_it(monkeypatch):
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    _, ex = make(h, store=store, monkeypatch=monkeypatch)
    asyncio.run(ready(ex, raw=charge_plan()))
    assert ex.plan_fetched_at == NOW + timedelta(seconds=30)
    assert asyncio.run(store.async_load()).plan_fetched_at == (NOW + timedelta(seconds=30)).isoformat()


def test_unchanged_fetches_persist_the_age_at_most_every_15_min(monkeypatch):
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    wall = WallClock(NOW)
    _, ex = make(h, store=store, monkeypatch=monkeypatch, utc=wall)
    asyncio.run(ready(ex, raw=charge_plan()))
    saves = []
    real = store.async_save

    async def spy(state):
        saves.append(state.plan_fetched_at)
        await real(state)
    monkeypatch.setattr(store, "async_save", spy)

    for minutes in (5, 10):
        wall.at = NOW + timedelta(minutes=minutes)
        asyncio.run(ex.async_note_plan_confirmed())
    assert saves == [] and ex.plan_fetched_at == NOW + timedelta(minutes=10)   # w pamięci zawsze świeży
    wall.at = NOW + timedelta(minutes=15)
    asyncio.run(ex.async_note_plan_confirmed())
    assert saves == [(NOW + timedelta(minutes=15)).isoformat()]


def test_stale_plan_is_saved_at_once_when_confirmed(monkeypatch):
    # Po przerwie dłuższej niż limit pierwsze pobranie zapisuje się od razu (restart nie wróci do „stary”).
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    _, ex, wall = _charge_hours_ago(7, monkeypatch, h=h, store=store)
    asyncio.run(ex.async_note_plan_confirmed())
    assert asyncio.run(store.async_load()).plan_fetched_at == wall().isoformat()


def test_time_window_preview_uses_the_stale_masked_plan():
    deye = ProfileChoice(load_builtin("deye-sg"), None, "SUN-10K-SG04LP3-EU")
    wall = WallClock(NOW - timedelta(hours=7))
    _, ex = make(choice=deye, options={}, utc=wall)

    async def go():
        await ready(ex, raw=charge_plan())
        wall.at = NOW + timedelta(seconds=30)
        await ex.async_tick()
    asyncio.run(go())
    assert not any(p["grid_charge"] for p in ex.tou_preview["programs"])

    fresh = WallClock(NOW + timedelta(seconds=30))
    _, ex2 = make(choice=deye, options={}, utc=fresh)

    async def go2():
        await ready(ex2, raw=charge_plan())
        await ex2.async_tick()
    asyncio.run(go2())
    assert any(p["grid_charge"] for p in ex2.tou_preview["programs"])


def test_time_window_cycle_gets_the_stale_masked_plan(monkeypatch):
    seen = []

    def spy(**kw):
        seen.append(kw["schedule"])
        raise RuntimeError("stop")
    monkeypatch.setattr(ex_mod, "decide_tou_cycle", spy)
    deye = ProfileChoice(load_builtin("deye-sg"), None, "SUN-10K-SG04LP3-EU")
    wall = WallClock(NOW - timedelta(hours=7))
    _, ex = make(choice=deye, options={}, utc=wall)
    asyncio.run(ready(ex, raw=charge_plan()))
    wall.at = NOW + timedelta(seconds=30)
    rd = SimpleNamespace(source=object())
    try:
        asyncio.run(ex._tou_tick(rd, SimpleNamespace(), SimpleNamespace(soc=50), 0.0, wall()))
    except RuntimeError:
        pass
    assert seen and seen[0].slots[0].action.value == "self_consume" and seen[0].slots[0].from_fallback


def test_constant_lives_in_one_place():
    assert STALE_PLAN_S == 6 * 3600
    assert not hasattr(ex_mod, "STALE_PLAN_S") or ex_mod.STALE_PLAN_S is STALE_PLAN_S
    assert parse_schedule(charge_plan()).slots[0].from_fallback is False
