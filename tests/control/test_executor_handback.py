"""Przejęcie i oddanie na profilu z przełącznikiem zdalnego sterowania i mocą wg kierunku (encje).

Kolejność przejęcia: nastawy → przełącznik → tryb; przy zapamiętanym trybie wymuszonym najpierw obie
nastawy = 0, potem przełącznik, tryb i nastawy docelowe. Oddanie: tryb neutralny → nastawy z migawki →
przełącznik off. Profil bez tych konstrukcji — bez zmian (testy `test_executor*.py`)."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

from homeassistant.core import Context
from homeassistant.helpers import issue_registry as ir

from custom_components.volcast.control.executor import VolcastExecutor
from custom_components.volcast.control.ha_writer import EntityServiceWriter
from custom_components.volcast.control.store import ControlState, ControlStore
from custom_components.volcast.core.control.select import ProfileChoice
from tests.core.modbus.engine_profile import directional_profile

from . import test_executor as base
from .ha_fakes import NOW, FakeHass, FakeState
from .test_executor import Clock, plan

ENT = {"mode": "select.x_ems", "enable": "switch.x_remote", "charge_power_w": "number.x_charge",
       "discharge_power_w": "number.x_discharge", "soc_min": "number.x_dod", "soc": "sensor.x_soc"}
OPTIONS = ["auto", "charge_battery", "sell_power", "discharge_battery", "battery_standby"]


def profile(**policy):
    def mutate(raw):
        raw["ha"]["integrations"][0]["entities"]["mode"] = {"domain": "select", "unique_id_regex": "^x-ems-"}
        raw["write_policy"].update(policy)
    return directional_profile(mutate)


def dhass(*, mode="auto", enable="off", charge="5.0", discharge="6.0", dod="85"):
    h = FakeHass()
    s = h.states
    s.set(ENT["mode"], mode, {"options": OPTIONS})
    s.set(ENT["enable"], enable, {})
    for key, value in (("charge_power_w", charge), ("discharge_power_w", discharge)):
        s.set(ENT[key], value, {"min": 0, "max": 10, "step": 0.01, "unit_of_measurement": "kW"})
    s.set(ENT["soc_min"], dod, {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"})
    s.set(ENT["soc"], "60", {"unit_of_measurement": "%"})
    return h


class Utc:
    def __init__(self):
        self.t = NOW + timedelta(seconds=30)

    def __call__(self):
        return self.t


def make(h=None, *, store=None, prof=None, utc=None, monkeypatch, stop_timeout_s=None):
    h = h or dhass()
    monkeypatch.setattr(base.ex_mod, "control_verified", lambda *_: True)
    prof = prof or profile()
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"})
    kw = {} if stop_timeout_s is None else {"stop_timeout_s": stop_timeout_s}
    ex = VolcastExecutor(h, entry, choice=ProfileChoice(prof, "goodwe", "X"), mapped=ENT, rated_power_w=8000.0,
                         store=store or ControlStore(h, "e1"), writer=EntityServiceWriter(h), clock=Clock(),
                         utcnow=utc or Utc(), **kw)
    return h, ex


def calls(h, since=0):
    """(usługa, encja, wartość) każdego wywołania od `since`."""
    out = []
    for domain, service, data, _ctx in h.services.calls[since:]:
        value = data.get("option", data.get("value"))
        out.append((service, data["entity_id"], value))
    return out


def order(seq, *items):
    """Indeksy pierwszego wystąpienia każdej pozycji (usługa, encja[, wartość])."""
    idx = []
    for item in items:
        idx.append(next(i for i, c in enumerate(seq) if c[:len(item)] == item))
    return idx


def take_over(h, ex, raw=None):
    async def go():
        await base.ready(ex, raw=raw or plan(2500))
        await ex.async_tick()
    asyncio.run(go())


# ── przejęcie ─────────────────────────────────────────────────────────────


def test_takeover_from_neutral_writes_set_point_then_switch_then_mode(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    take_over(h, ex)
    seq = calls(h)
    a, b, c = order(seq, ("set_value", ENT["discharge_power_w"], 2.5), ("turn_on", ENT["enable"]),
                    ("select_option", ENT["mode"], "sell_power"))
    assert a < b < c
    assert not any(e == ENT["charge_power_w"] for _, e, _ in seq)
    assert ex.last_decision.status == "write"


def test_takeover_from_stored_forced_mode_zeroes_set_points_before_the_switch(monkeypatch):
    h, ex = make(dhass(mode="charge_battery"), monkeypatch=monkeypatch)
    take_over(h, ex)
    seq = calls(h)
    z1, z2, on, mode, target = order(
        seq, ("set_value", ENT["charge_power_w"], 0.0), ("set_value", ENT["discharge_power_w"], 0.0),
        ("turn_on", ENT["enable"]), ("select_option", ENT["mode"], "sell_power"),
        ("set_value", ENT["discharge_power_w"], 2.5))
    assert max(z1, z2) < on < mode < target
    assert h.states.get(ENT["discharge_power_w"]).state == "2.5"
    assert h.states.get(ENT["enable"]).state == "on"
    # migawka sprzed pierwszego zapisu: nastawy i przełącznik właściciela
    assert ex._state.snapshot["charge_power_w"] == 5000.0 and ex._state.snapshot["enable"] == 0.0
    assert {"charge_power_w", "discharge_power_w", "enable", "mode"} <= set(ex._state.restore_keys)


def test_takeover_holds_switch_and_mode_when_zeroing_fails(monkeypatch):
    h, ex = make(dhass(mode="charge_battery"), monkeypatch=monkeypatch)
    h.services.fail[ENT["charge_power_w"]] = RuntimeError("x")
    take_over(h, ex)
    seq = calls(h)
    assert not any(s == "turn_on" for s, _, _ in seq)
    assert not any(s == "select_option" for s, _, _ in seq)
    assert h.states.get(ENT["enable"]).state == "off"


def test_switch_hold_persists_while_the_set_point_waits(monkeypatch):
    # Nastawa odrzucona: przełącznik i tryb wstrzymane — także w następnym cyklu (interwał I-6).
    h, ex = make(monkeypatch=monkeypatch)
    h.services.fail[ENT["discharge_power_w"]] = RuntimeError("x")
    take_over(h, ex)
    h.services.fail.clear()
    n = len(h.services.calls)
    ex._clock.t += 5
    asyncio.run(ex.async_tick())
    assert not any(s == "turn_on" for s, _, _ in calls(h, n))
    assert h.states.get(ENT["enable"]).state == "off"


def test_snapshot_with_set_points_and_switch_survives_restart(monkeypatch):
    h = dhass()
    store = ControlStore(h, "e1")
    h, ex = make(h, store=store, monkeypatch=monkeypatch)
    take_over(h, ex)
    h2, ex2 = make(h, store=store, monkeypatch=monkeypatch)
    asyncio.run(ex2.async_start())
    snap = ex2._state.snapshot
    assert ex2._state.owned and snap["discharge_power_w"] == 6000.0 and snap["charge_power_w"] == 5000.0
    assert snap["enable"] == 0.0


# ── oddanie ───────────────────────────────────────────────────────────────


def _assert_handback(seq, *, charge=True):
    mode, dis, off = order(seq, ("select_option", ENT["mode"], "auto"),
                           ("set_value", ENT["discharge_power_w"], 6.0), ("turn_off", ENT["enable"]))
    assert mode < dis < off
    if charge:
        assert order(seq, ("set_value", ENT["charge_power_w"], 5.0))[0] < off


def test_consent_withdrawal_hands_back_mode_set_points_then_switch(monkeypatch):
    h, ex = make(dhass(mode="charge_battery"), monkeypatch=monkeypatch)
    take_over(h, ex)
    n = len(h.services.calls)

    async def go():
        await ex.async_set_consent(False)
        await ex.async_tick()
    asyncio.run(go())
    _assert_handback(calls(h, n))
    assert h.states.get(ENT["enable"]).state == "off" and not ex._state.owned


def test_switch_kept_on_when_the_owner_had_it_on(monkeypatch):
    h, ex = make(dhass(mode="charge_battery", enable="on"), monkeypatch=monkeypatch)
    take_over(h, ex)
    n = len(h.services.calls)

    async def go():
        await ex.async_set_consent(False)
        await ex.async_tick()
    asyncio.run(go())
    assert not any(s == "turn_off" for s, _, _ in calls(h, n))
    assert not ex._state.owned


def test_baseline_enable_false_switches_off_even_if_the_owner_had_it_on(monkeypatch):
    def mutate(raw):
        raw["ha"]["integrations"][0]["entities"]["mode"] = {"domain": "select", "unique_id_regex": "^x-ems-"}
        raw["baseline"]["enable"] = False
    h, ex = make(dhass(mode="charge_battery", enable="on"), prof=directional_profile(mutate),
                 monkeypatch=monkeypatch)
    take_over(h, ex)
    n = len(h.services.calls)

    async def go():
        await ex.async_set_consent(False)
        await ex.async_tick()
    asyncio.run(go())
    assert ("turn_off", ENT["enable"], None) in calls(h, n)


def test_auth_failures_hand_back_in_order(monkeypatch):
    h, ex = make(dhass(mode="charge_battery"), monkeypatch=monkeypatch)
    take_over(h, ex)
    n = len(h.services.calls)

    async def go():
        await ex.async_on_auth_failure(2)
        await ex.async_tick()
    asyncio.run(go())
    _assert_handback(calls(h, n))


def test_restart_with_consent_revoked_hands_back_from_the_persisted_snapshot(monkeypatch):
    h = dhass(mode="sell_power", enable="on", charge="0", discharge="2.5", dod="90")
    store = ControlStore(h, "e1")
    asyncio.run(store.async_save(ControlState(
        consent=False, local_switch=True, owned=True,
        snapshot={"mode": "charge_battery", "charge_power_w": 5000.0, "discharge_power_w": 6000.0,
                  "enable": 0.0, "soc_min": 15.0},
        owner={"profile": "test-ms", "domain": "goodwe", "mode_entity": ENT["mode"]},
        restore_keys=["soc_min", "charge_power_w", "discharge_power_w", "enable", "mode"])))
    h, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ex.async_start()
        await ex.async_tick()
    asyncio.run(go())
    _assert_handback(calls(h))
    assert not ex._state.owned


def test_stop_hands_back_and_keeps_ownership(monkeypatch):
    h, ex = make(dhass(mode="charge_battery"), monkeypatch=monkeypatch)
    take_over(h, ex)
    n = len(h.services.calls)
    assert asyncio.run(ex.async_neutral_at_stop()) is True
    _assert_handback(calls(h, n))
    assert ex._state.owned                      # restart przejmuje od nowa z tej samej migawki


def test_stop_with_neutral_mode_still_hands_back_the_switch(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    take_over(h, ex, raw=plan(slots=[{"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z",
                                      "mode": "self_consume", "price_pln_kwh": 0.5}]))
    assert h.states.get(ENT["enable"]).state == "on"
    n = len(h.services.calls)
    asyncio.run(ex.async_neutral_at_stop())
    assert ("turn_off", ENT["enable"], None) in calls(h, n)


def test_slow_integration_at_stop_still_gets_the_switch_off(monkeypatch):
    h, ex = make(dhass(mode="charge_battery"), monkeypatch=monkeypatch, stop_timeout_s=1.0)
    take_over(h, ex)
    h.services.delay_s = 0.3
    n = len(h.services.calls)
    asyncio.run(ex.async_neutral_at_stop())
    assert ("turn_off", ENT["enable"], None) in calls(h, n)
    assert h.states.get(ENT["enable"]).state == "off"


# ── brak planu ────────────────────────────────────────────────────────────


def test_no_plan_beyond_threshold_hands_back_and_stays_released(monkeypatch):
    utc = Utc()
    h, ex = make(dhass(mode="charge_battery"), prof=profile(release_without_plan_s=600), utc=utc,
                 monkeypatch=monkeypatch)
    take_over(h, ex)
    assert ex._state.owned
    utc.t = NOW + timedelta(hours=1, minutes=5)          # slot skończył się 5 min temu: poniżej progu
    ex._clock.t += 3900
    asyncio.run(ex.async_tick())
    assert ex._state.owned
    utc.t = NOW + timedelta(hours=1, minutes=11)
    ex._clock.t += 360
    n = len(h.services.calls)
    asyncio.run(ex.async_tick())
    seq = calls(h, n)                        # tryb już neutralny (slot zapasowy przed progiem)
    dis, off = order(seq, ("set_value", ENT["discharge_power_w"], 6.0), ("turn_off", ENT["enable"]))
    assert dis < off and h.states.get(ENT["mode"]).state == "auto"
    assert not ex._state.owned
    n = len(h.services.calls)
    ex._clock.t += 600
    asyncio.run(ex.async_tick())
    assert calls(h, n) == [] and ex.last_decision.reason == "no_plan"


def test_saved_stale_plan_after_restart_hands_back(monkeypatch):
    h = dhass(mode="sell_power", enable="on", charge="0", discharge="2.5", dod="90")
    raw = plan(2500)
    store = ControlStore(h, "e1")
    asyncio.run(store.async_save(ControlState(
        consent=True, local_switch=True, owned=True, plan_raw=raw,
        snapshot={"mode": "auto", "charge_power_w": 5000.0, "discharge_power_w": 6000.0, "enable": 0.0},
        owner={"profile": "test-ms", "domain": "goodwe", "mode_entity": ENT["mode"]},
        restore_keys=["charge_power_w", "discharge_power_w", "enable", "mode"])))
    utc = Utc()
    utc.t = NOW + timedelta(hours=3)
    h, ex = make(h, store=store, prof=profile(release_without_plan_s=600), utc=utc, monkeypatch=monkeypatch)

    async def go():
        await ex.async_start()
        await ex.async_tick()
    asyncio.run(go())
    _assert_handback(calls(h))
    assert not ex._state.owned


def test_without_the_threshold_the_fallback_slot_keeps_control(monkeypatch):
    utc = Utc()
    h, ex = make(dhass(mode="charge_battery"), utc=utc, monkeypatch=monkeypatch)
    take_over(h, ex)
    utc.t = NOW + timedelta(hours=5)
    ex._clock.t += 18000
    asyncio.run(ex.async_tick())
    assert ex._state.owned and not any(s == "turn_off" for s, _, _ in calls(h))


# ── obce sterowanie ───────────────────────────────────────────────────────


def _switch_event(state, ctx):
    return SimpleNamespace(data={"entity_id": ENT["enable"], "new_state": FakeState(ENT["enable"], state, {},
                                                                                   context=ctx)},
                           context=ctx)


def test_foreign_switch_off_pauses_hands_back_and_raises_issue(monkeypatch):
    ir.async_create_issue.reset_mock()
    h, ex = make(dhass(mode="charge_battery"), monkeypatch=monkeypatch)
    take_over(h, ex)
    h.states.set(ENT["enable"], "off")
    n = len(h.services.calls)
    asyncio.run(ex.async_on_state_event(_switch_event("off", Context())))
    assert ex.paused
    assert [c for c in ir.async_create_issue.call_args_list if c.args[2] == "foreign_control_e1"]
    seq = calls(h, n)
    assert ("select_option", ENT["mode"], "auto") in seq
    assert not any(s == "turn_on" for s, _, _ in seq)
    assert not ex._state.owned
