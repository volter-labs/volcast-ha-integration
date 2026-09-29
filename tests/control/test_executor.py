import asyncio
import logging
import weakref
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir

from custom_components.volcast.control import executor as ex_mod
from custom_components.volcast.control.executor import VolcastExecutor
from custom_components.volcast.control.ha_writer import EntityServiceWriter
from custom_components.volcast.control.store import ControlState, ControlStore
from custom_components.volcast.core.control import group_writes
from custom_components.volcast.core.control.cycle import ERROR, CycleDecision
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.slot import parse_schedule

from .ha_fakes import GOODWE_ENTITIES as E, NOW, goodwe_hass

GW = ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET")
LOGGER = "custom_components.volcast.control"


def plan(power=2000, sid="p1", control=True, slots=None):
    slots = slots if slots is not None else [{
        "from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "mode": "discharge",
        "discharge_purpose": "sell", "power_w": power, "price_pln_kwh": 0.8}]
    return {"schedule_id": sid, "slots": slots, "fallback": {"mode": "self_consume", "soc_reserve": 10},
            "control_enabled": control}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(h=None, *, options=None, store=None, verified=True, choice=GW, monkeypatch=None,
         utc=lambda: NOW + timedelta(seconds=30), clock=None, writer=None):
    h = h or goodwe_hass()
    entry = SimpleNamespace(entry_id="e1", options=options if options is not None else {"control_mode": "entities"})
    if monkeypatch is not None:
        monkeypatch.setattr(ex_mod, "control_verified", lambda *_: verified)
    ex = VolcastExecutor(h, entry, choice=choice, mapped=E if choice and choice.integration_domain else {},
                         rated_power_w=8000.0, store=store or ControlStore(h, "e1"),
                         writer=writer or EntityServiceWriter(h), clock=clock or Clock(), utcnow=utc)
    return h, ex


async def ready(ex, *, consent=True, local=True, raw=None):
    await ex.async_start()
    raw = raw or plan()
    await ex.async_on_plan(raw, parse_schedule(raw))
    await ex.async_set_consent(consent)
    await ex.async_set_local_switch(local)


def no_entity_ids_in(text):
    return not any(eid in text for eid in E.values())


def test_tick_writes_when_all_gates_open(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["mode"]).state == "sell_power"
    assert h.states.get(E["power_w"]).state == "2000.0"
    assert ex.last_decision.status == "write"


def test_tick_passes_units_from_state(monkeypatch):
    h = goodwe_hass(temp="82.4")                     # °F → 28 °C: guard temperatury przepuszcza
    h.states.set(E["power_w"], "0", {"min": 0, "max": 10, "step": 0.1, "unit_of_measurement": "kW"})
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["power_w"]).state == "2.0"


def test_dry_run_when_unverified(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch, verified=False)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == [] and ex.last_decision.status == "dry_run"


def test_no_mode_chosen_never_writes(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch, options={})

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == [] and ex.last_decision.reason == "no_mode_chosen"


def test_snapshot_before_first_write_and_restore_when_consent_revoked(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        n = len(h.services.calls)
        await ex.async_set_consent(False)
        await ex.async_tick()
        restored = h.services.calls[n:]
        await ex.async_tick()
        return n, restored
    n, restored = asyncio.run(go())
    # próg SoC i limit eksportu są już jak w migawce — jadą tylko tryb (najpierw) i przełącznik
    assert [c[:2] for c in restored] == [("select", "select_option"), ("switch", "turn_on")]
    assert h.states.get(E["mode"]).state == "auto"
    assert h.states.get(E["export_limit_w"]).state == "4000"
    assert h.states.get(E["export_limit_enabled"]).state == "on"
    assert h.states.get(E["soc_min"]).state == "85"
    assert len(h.services.calls) == n + 2                     # trzeci tik: nic


def test_restore_when_control_enabled_turns_false_with_empty_plan(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        empty = plan(slots=[], sid="", control=False)
        await ex.async_on_plan(empty, parse_schedule(empty))
        await ex.async_set_consent(False)
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["mode"]).state == "auto"


def test_restore_on_local_switch_off(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        await ex.async_set_local_switch(False)
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["mode"]).state == "auto"


def test_no_restore_and_no_rewrite_after_reload(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    store = ControlStore(h, "e1")
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        n = len(h.services.calls)
        _, ex2 = make(h, store=store, monkeypatch=monkeypatch)
        await ex2.async_start()
        await ex2.async_tick()
        return n, ex2
    n, ex2 = asyncio.run(go())
    assert len(h.services.calls) == n                        # stan urządzenia = plan → nic
    assert ex2.last_decision.reason == "nothing_to_write"


def test_plan_from_store_after_restart_without_cloud(monkeypatch):
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    asyncio.run(store.async_save(ControlState(plan_raw=plan(), consent=True, local_switch=True)))
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ex.async_start()
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["mode"]).state == "sell_power"


def test_invalid_stored_plan_is_dropped_fail_closed(monkeypatch):
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    asyncio.run(store.async_save(ControlState(plan_raw={"slots": "nope"}, consent=True, local_switch=True)))
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ex.async_start()
        await ex.async_tick()
    asyncio.run(go())
    assert ex.raw_plan is None and ex.schedule is None
    assert h.services.calls == [] and ex.last_decision.reason == "no_plan"


def test_wall_clock_jump_does_not_unthrottle(monkeypatch):
    clock = Clock()
    times = iter([NOW + timedelta(minutes=3), NOW + timedelta(minutes=1), NOW + timedelta(minutes=1)])
    h, ex = make(monkeypatch=monkeypatch, clock=clock, utc=lambda: next(times))

    async def go():
        await ready(ex)
        await ex.async_tick()                                   # 2000 W
        p = plan(power=3000, sid="p2")
        await ex.async_on_plan(p, parse_schedule(p))
        clock.t += 30                                           # 30 s monotonicznie, ściana −2 min
        await ex.async_tick()
        first = h.states.get(E["power_w"]).state
        clock.t += 31
        await ex.async_tick()
        return first
    first = asyncio.run(go())
    assert first == "2000.0" and h.states.get(E["power_w"]).state == "3000.0"


def test_repeated_errors_raise_repair_issue(monkeypatch):
    ir.async_create_issue.reset_mock()
    h, ex = make(monkeypatch=monkeypatch)
    monkeypatch.setattr(ex_mod, "decide_cycle", lambda **_: CycleDecision(ERROR, "exception:X"))

    async def go():
        await ready(ex)
        for _ in range(10):
            await ex.async_tick()
    asyncio.run(go())
    ids = [c.args[2] for c in ir.async_create_issue.call_args_list]
    assert "control_error_e1" in ids


def test_repair_issue_deleted_after_recovery(monkeypatch):
    ir.async_delete_issue.reset_mock()
    h, ex = make(monkeypatch=monkeypatch)
    real = ex_mod.decide_cycle
    monkeypatch.setattr(ex_mod, "decide_cycle", lambda **_: CycleDecision(ERROR, "exception:X"))

    async def go():
        await ready(ex)
        for _ in range(10):
            await ex.async_tick()
        monkeypatch.setattr(ex_mod, "decide_cycle", real)
        await ex.async_tick()
    asyncio.run(go())
    assert "control_error_e1" in [c.args[2] for c in ir.async_delete_issue.call_args_list]


def test_auth_failures_revoke_consent_after_two(monkeypatch):
    _, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_on_auth_failure(1)
        first = ex.consent
        await ex.async_on_auth_failure(2)
        return first
    assert asyncio.run(go()) is True and ex.consent is False


def test_restore_runs_while_paused(monkeypatch):
    # Hamulec właściciela działa od razu — pauza nie wstrzymuje powrotu do trybu bazowego.
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        ex._memory.paused_until = ex._clock() + 1800
        await ex.async_set_consent(False)
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["mode"]).state == "auto" and ex._state.owned is False


def test_tou_preview_for_time_window_profile():
    deye = ProfileChoice(load_builtin("deye-sg"), None, "SUN-10K-SG04LP3-EU")
    h, ex = make(choice=deye, options={})

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == []
    assert len(ex.tou_preview["programs"]) == 6 and "lost_value_pln" in ex.tou_preview


def test_tou_preview_survives_engine_error(monkeypatch):
    deye = ProfileChoice(load_builtin("deye-sg"), None, None)
    _, ex = make(choice=deye, options={})

    def boom(*_a, **_k):
        raise RuntimeError("dst")
    monkeypatch.setattr(ex_mod, "compress", boom)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert ex.tou_preview == {"error": "RuntimeError"}


# ── zapis grupowy (tryb + moc) ────────────────────────────────────────────


def _spy_group_runner(monkeypatch):
    seen = []
    real = group_writes.async_run_group_writes

    async def spy(writes, write, **kw):
        seen.append(({w.key for w in writes}, kw))
        return await real(writes, write, **kw)
    monkeypatch.setattr(ex_mod, "async_run_group_writes", spy)
    return seen


def test_cycle_writes_go_through_group_runner_with_restore_and_ambiguous_safe(monkeypatch):
    seen = _spy_group_runner(monkeypatch)
    captured = {}
    real_decide = ex_mod.decide_cycle

    def decide(**kw):
        captured["d"] = real_decide(**kw)
        return captured["d"]
    monkeypatch.setattr(ex_mod, "decide_cycle", decide)
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    d = captured["d"]
    assert d.status == "write" and d.restore                 # auto → sell: jest do czego wrócić
    assert len(seen) == 1
    keys, kw = seen[0]
    assert {"mode", "power_w"} <= keys
    assert kw["restore"] == d.restore and kw["ambiguous_safe"] == d.restore_ambiguous_safe
    assert callable(kw["on_exception"])


def test_second_group_member_denied_returns_mode_and_logs(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    h, ex = make(monkeypatch=monkeypatch)
    h.services.fail[E["power_w"]] = ServiceValidationError("rejected")

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    # tryb poszedł pierwszy (wzrost mocy), moc odrzucona na pewno → tryb wraca do „auto"
    assert h.states.get(E["mode"]).state == "auto"
    assert [c[2].get("option") for c in h.services.calls if c[0] == "select"] == ["sell_power", "auto"]
    assert "returned to its previous value" in caplog.text
    assert no_entity_ids_in(caplog.text)


def test_ambiguous_second_member_holds_restore_and_logs(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    h = goodwe_hass(mode="charge_battery", power="1000")
    h.services.fail[E["power_w"]] = HomeAssistantError("timeout")
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    # powrót do ładowania przy nieznanym wyniku mocy byłby groźny — tryb zostaje
    assert h.states.get(E["mode"]).state == "sell_power"
    assert "may have been applied" in caplog.text
    assert "power_w" in caplog.text and no_entity_ids_in(caplog.text)


def test_no_planned_restore_logged_distinctly(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    # postój z mocą > 0 nie jest stanem, do którego wolno wrócić
    h = goodwe_hass(mode="battery_standby", power="500")
    h.services.fail[E["power_w"]] = ServiceValidationError("rejected")
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["mode"]).state == "sell_power"
    assert "no return to the previous value was planned" in caplog.text
    assert "restore failed" not in caplog.text.lower()


def test_writer_exception_logs_key_and_class_only(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    h, ex = make(monkeypatch=monkeypatch)
    real = ex._writer.async_write

    async def raising(w):
        if w.key == "export_limit_enabled":
            raise RuntimeError("tcp://192.168.1.50 id XYZ-PLACEHOLDER")
        return await real(w)
    ex._writer.async_write = raising

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert "export_limit_enabled" in caplog.text and "RuntimeError" in caplog.text
    assert "192.168" not in caplog.text and "XYZ-PLACEHOLDER" not in caplog.text
    assert no_entity_ids_in(caplog.text)
    # warunek trybu nie doszedł → grupa czeka
    assert h.states.get(E["mode"]).state == "auto"


# ── odczyt trybu: surowa opcja i lista opcji ──────────────────────────────


def test_foreign_mode_option_blocks_writes(monkeypatch):
    h = goodwe_hass(mode="export_ac")                     # czytelna opcja spoza profilu
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == []
    assert ex.last_decision.reason == "foreign_mode" and ex.last_decision.takeover


def test_mode_missing_from_select_options_blocks_before_any_write(monkeypatch):
    h = goodwe_hass()
    h.states.set(E["mode"], "auto", {"options": ["auto", "battery_standby"]})
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == [] and ex.last_decision.reason == "mode_unsupported"


# ── migawka trybu bazowego ────────────────────────────────────────────────


def test_unavailable_setting_does_not_hold_control(monkeypatch):
    # Nastawa z niedostępną encją jest nieobsługiwana: migawka bez niej, sterowanie idzie dalej.
    h = goodwe_hass()
    h.states.set(E["export_limit_w"], "unavailable")
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert ex.last_decision.status == "write", ex.last_decision.reason
    assert ex._state.owned is True and "export_limit_w" not in ex._state.snapshot
    assert E["export_limit_w"] not in [c[2]["entity_id"] for c in h.services.calls]
    assert h.states.get(E["mode"]).state == "sell_power"


def test_setting_back_after_ownership_joins_the_snapshot(monkeypatch):
    # Nastawa wraca po naszym pierwszym zapisie — jej wartość sprzed zapisu trafia do migawki,
    # zanim ją zapiszemy (inaczej powrót do trybu bazowego nie miałby jej wartości).
    h = goodwe_hass()
    h.states.set(E["export_limit_w"], "unavailable")
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        h.states.set(E["export_limit_w"], "4000")
        ex._clock.t += 120
        await ex.async_tick()
    asyncio.run(go())
    assert ex._state.snapshot["export_limit_w"] == 4000.0


def test_unavailable_mode_entity_holds_every_write(monkeypatch):
    h = goodwe_hass()
    h.states.set(E["mode"], "unavailable")
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == [] and ex.last_decision.reason == "missing_entities"
    assert ex.last_decision.unmapped == ("mode",)


def _unsupported_issues():
    return [c for c in ir.async_create_issue.call_args_list if c.args[2] == "unsupported_setting_e1"]


def test_lasting_unavailable_setting_raises_issue_and_clears_when_back(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    ir.async_create_issue.reset_mock()
    ir.async_delete_issue.reset_mock()
    h = goodwe_hass()
    h.states.set(E["soc_max"], "unavailable")
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        first = (list(_unsupported_issues()), ex.unsupported_settings)   # chwilowy brak — bez zgłoszenia
        ex._clock.t += 601
        await ex.async_tick()
        lasting = (list(_unsupported_issues()), ex.unsupported_settings)
        h.states.set(E["soc_max"], "100")
        ex._clock.t += 60
        await ex.async_tick()
        return first, lasting
    first, lasting = asyncio.run(go())
    assert first == ([], ())
    (call,), keys = lasting
    assert keys == ("soc_max",)
    assert call.kwargs["translation_key"] == "unsupported_setting"
    assert call.kwargs["translation_placeholders"] == {"entities": E["soc_max"]}
    assert ex.unsupported_settings == ()
    assert "unsupported_setting_e1" in [c.args[2] for c in ir.async_delete_issue.call_args_list]
    assert no_entity_ids_in(caplog.text)


def test_unmapped_setting_is_unsupported_at_once(monkeypatch):
    ir.async_create_issue.reset_mock()
    h = goodwe_hass()
    mapped = {k: v for k, v in E.items() if k != "soc_max"}
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"})
    monkeypatch.setattr(ex_mod, "control_verified", lambda *_: True)
    ex = VolcastExecutor(h, entry, choice=GW, mapped=mapped, rated_power_w=8000.0,
                         store=ControlStore(h, "e1"), writer=EntityServiceWriter(h), clock=Clock(),
                         utcnow=lambda: NOW + timedelta(seconds=30))

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert ex.unsupported_settings == ("soc_max",) and ex.last_decision.status == "write"
    (call,) = _unsupported_issues()
    assert call.kwargs["translation_placeholders"] == {"entities": "soc_max"}


def test_snapshot_save_failure_holds_writes(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)

        async def broken(_state):
            raise OSError("disk")
        ex._store.async_save = broken
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == [] and ex._state.owned is False
    assert ex.last_decision.status == "error"


# ── powrót do trybu bazowego ─────────────────────────────────────────────


def test_restore_goes_through_group_runner(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        seen = _spy_group_runner(monkeypatch)
        await ex.async_set_consent(False)
        await ex.async_tick()
        return seen
    seen = asyncio.run(go())
    # tryb bazowy osobnym krokiem, przed pozostałymi nastawami
    assert [keys for keys, _ in seen] == [{"mode"}, {"export_limit_enabled"}]
    assert ex.last_decision.status == "restore"


def test_restore_does_not_overwrite_foreign_mode(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        n = len(h.services.calls)
        h.states.set(E["mode"], "export_ac")               # właściciel wybrał tryb spoza profilu
        await ex.async_set_consent(False)
        await ex.async_tick()
        return h.services.calls[n:]
    restored = asyncio.run(go())
    assert [c[:2] for c in restored] == [("switch", "turn_on")]
    assert h.states.get(E["mode"]).state == "export_ac"
    assert ex._state.owned is False
    assert ex.last_decision.takeover
    assert no_entity_ids_in(caplog.text)


def test_failed_restore_keeps_ownership_and_retries(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        h.services.fail[E["mode"]] = HomeAssistantError("down")
        await ex.async_set_consent(False)
        await ex.async_tick()
        first = (ex._state.owned, ex.last_decision.status)
        del h.services.fail[E["mode"]]
        await ex.async_tick()
        return first
    first = asyncio.run(go())
    assert first == (True, "error")
    assert ex._state.owned is False and h.states.get(E["mode"]).state == "auto"


def test_restore_now_swallows_non_ha_exception(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()

        async def bad_write(_w):
            raise RuntimeError("secret host 10.0.0.1")
        ex._writer.async_write = bad_write
        await ex.async_restore_now()
    asyncio.run(go())
    assert ex._state.owned is True                        # nie udało się — własność zostaje
    assert "10.0.0.1" not in caplog.text


def test_restore_now_restores_when_owned(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        await ex.async_restore_now()
    asyncio.run(go())
    assert h.states.get(E["mode"]).state == "auto" and ex._state.owned is False


# ── jeden zapis naraz, zatrzymanie ───────────────────────────────────────


def test_ticks_are_single_flight_and_coalesced(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    inflight = {"now": 0, "max": 0}
    real = ex._writer.async_write

    async def tracked(w):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        try:
            await asyncio.sleep(0.01)
            return await real(w)
        finally:
            inflight["now"] -= 1
    ex._writer.async_write = tracked

    async def go():
        await ready(ex)
        await asyncio.gather(ex.async_tick(), ex.async_tick(), ex.async_tick())
    asyncio.run(go())
    assert inflight["max"] == 1
    # zaległy tik przebiegł po pierwszym: stan = plan, więc drugi raz nic nie poszło
    assert [c[0] for c in h.services.calls].count("select") == 1
    assert ex.last_decision.reason == "nothing_to_write"


def test_stop_ends_ticks_and_does_not_restore(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        n = len(h.services.calls)
        await ex.async_stop()
        await ex.async_set_consent(False)
        await ex.async_tick()
        return n
    n = asyncio.run(go())
    assert len(h.services.calls) == n and h.states.get(E["mode"]).state == "sell_power"


def test_stop_waits_at_most_timeout_for_write_in_progress(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    ex._stop_timeout_s = 0.05
    h.services.delay_s = 0.5

    async def go():
        await ready(ex)
        task = asyncio.ensure_future(ex.async_tick())
        await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await ex.async_stop()
        waited = loop.time() - t0
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return waited
    assert asyncio.run(go()) < 0.4


# ── wejścia odporne na błąd magazynu ─────────────────────────────────────


def test_inputs_survive_store_failure(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ex.async_start()

        async def broken(_state):
            raise OSError("disk")
        ex._store.async_save = broken
        raw = plan()
        await ex.async_on_plan(raw, parse_schedule(raw))
        await ex.async_set_consent(True)
        await ex.async_set_local_switch(True)
        await ex.async_on_auth_failure(1)
        await ex.async_on_auth_failure(2)
    asyncio.run(go())
    assert ex.raw_plan is not None and ex.local_switch is True
    assert ex.consent is False                            # cofnięcie działa w pamięci mimo błędu zapisu


def test_consent_change_saved_once(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)
    saves = []

    async def go():
        await ex.async_start()
        real = ex._store.async_save

        async def counting(state):
            saves.append(state.consent)
            await real(state)
        ex._store.async_save = counting
        for _ in range(3):
            await ex.async_set_consent(True)
    asyncio.run(go())
    assert saves == [True]


# ── import historii, słabe odwołanie, podsumowanie ───────────────────────


def test_history_imported_marker_persists_in_same_store(monkeypatch):
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        assert ex.history_imported_at is None
        await ex.async_mark_history_imported("2026-09-23T10:00:00+00:00")
        return await store.async_load()
    state = asyncio.run(go())
    assert ex.history_imported_at == "2026-09-23T10:00:00+00:00"
    assert state.history_imported_at == "2026-09-23T10:00:00+00:00"
    assert state.consent is True and state.plan_raw is not None   # reszta stanu nietknięta


def test_executor_is_weak_referenceable(monkeypatch):
    _, ex = make(monkeypatch=monkeypatch)
    assert weakref.ref(ex)() is ex


def test_exec_summary_is_small_and_has_no_entity_ids(monkeypatch):
    import json
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    s = ex.exec_summary()
    text = json.dumps(s)
    assert len(text) < 1024 and no_entity_ids_in(text)
    assert s["decision"]["status"] == "write" and s["consent"] is True and s["profile"] == "goodwe-et"


def test_notifies_listeners_after_tick(monkeypatch):
    from homeassistant.helpers import dispatcher
    dispatcher.async_dispatcher_send.reset_mock()
    _, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    signals = [c.args[1] for c in dispatcher.async_dispatcher_send.call_args_list]
    assert "volcast_control_updated_e1" in signals


@pytest.mark.parametrize("name,value", [
    ("CONF_BACKEND", "backend"), ("CONF_USER_ID", "user_id"), ("CONF_PAIRED_AT", "paired_at"),
    ("CONF_PAIRING", "pairing"), ("OPT_CONTROL_MODE", "control_mode"),
    ("CONTROL_MODE_ENTITIES", "entities"), ("OPT_PROFILE_ID", "profile_id"),
    ("OPT_INVERTER_DOMAIN", "inverter_domain"), ("OPT_TELEMETRY_MAP", "telemetry_map"),
    ("OPT_GRID_NEGATE", "grid_power_negate"), ("OPT_RATED_POWER_W", "rated_power_w"),
    ("OPT_BATTERY_CAPACITY_KWH", "battery_capacity_kwh"), ("OPT_LOAD_ENERGY", "load_energy_entity"),
    ("OPT_PRICE_BUY", "entity_price_buy"), ("OPT_PRICE_SELL", "entity_price_sell"),
    ("OPT_PRICE_CURRENCY", "price_currency"),
    ("SIGNAL_CONTROL_UPDATED", "volcast_control_updated_{entry_id}"),
    ("EXECUTOR_INTERVAL_S", 60), ("STOP_WRITE_TIMEOUT_S", 10.0), ("ERROR_ISSUE_AFTER", 10),
    ("BETA_PAIRING_URL", "https://staging.volcast.app/functions/v1/pairing-session"),
])
def test_control_constants(name, value):
    from custom_components.volcast import const
    assert getattr(const, name) == value


# ── powrót do trybu bazowego niezależny od pozostałych nastaw ─────────────


def charge_plan(sid="c1"):
    return plan(sid=sid, slots=[{
        "from": "2026-09-23T10:00:00Z", "to": "2026-09-23T11:00:00Z", "mode": "charge",
        "charge_source": "grid", "power_w": 3000, "soc_target": 90, "price_pln_kwh": 0.3}])


async def _charging(ex, h):
    await ready(ex, raw=charge_plan())
    await ex.async_tick()
    assert h.states.get(E["mode"]).state == "charge_battery"
    assert h.states.get(E["soc_max"]).state == "90.0"


def test_restore_mode_first_when_soc_max_unavailable(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await _charging(ex, h)
        await ex.async_set_local_switch(False)
        h.states.set(E["soc_max"], "unavailable")
        await ex.async_tick()
        first = (h.states.get(E["mode"]).state, ex._state.owned, ex.last_decision.reason)
        h.states.set(E["soc_max"], "90")
        await ex.async_tick()
        return first
    first = asyncio.run(go())
    # hamulec właściciela nie zależy od niedostępnej encji progu ładowania
    assert first == ("auto", True, "restore_failed")
    assert h.states.get(E["soc_max"]).state == "100.0"
    assert ex._state.owned is False and ex.last_decision.reason == "baseline"


def test_restore_mode_despite_denied_condition_keys(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await _charging(ex, h)
        h.services.fail[E["soc_max"]] = ServiceValidationError("no")
        h.services.fail[E["export_limit_enabled"]] = ServiceValidationError("no")
        await ex.async_set_consent(False)
        await ex.async_tick()
        first = (h.states.get(E["mode"]).state, ex._state.owned)
        await ex.async_tick()
        second = ex._state.owned
        h.services.fail.clear()
        await ex.async_tick()
        return first, second
    first, second = asyncio.run(go())
    assert first == ("auto", True) and second is True           # każdy tik ponawia resztę
    assert h.states.get(E["soc_max"]).state == "100.0"
    assert ex._state.owned is False


def test_failed_mode_restore_still_restores_other_keys(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await _charging(ex, h)
        h.services.fail[E["mode"]] = HomeAssistantError("down")
        await ex.async_set_consent(False)
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["soc_max"]).state == "100.0"
    assert ex._state.owned is True and ex.last_decision.reason == "restore_failed"


def test_restore_marks_direction_unknown(monkeypatch):
    from custom_components.volcast.core.guard_state import _UNKNOWN_DIRECTION
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        assert ex._memory.limiter._current == "discharge"
        await ex.async_set_consent(False)
        await ex.async_tick()
    asyncio.run(go())
    assert ex._memory.limiter._current == _UNKNOWN_DIRECTION


def test_release_with_incomplete_snapshot_is_logged(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    h = goodwe_hass(mode="sell_power", export="0")
    store = ControlStore(h, "e1")
    asyncio.run(store.async_save(ControlState(consent=False, local_switch=True, owned=True, snapshot={})))
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ex.async_start()
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["mode"]).state == "auto" and ex._state.owned is False
    msg = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "could not" in r.getMessage()]
    assert msg and "export_limit_w" in msg[0] and "soc_min" in msg[0]
    assert no_entity_ids_in(caplog.text)


# ── zatrzymanie, bramki, zgoda, magazyn, powiązanie własności ────────────


def test_stop_during_ownership_save_prevents_writes(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        real = ex._store.async_save

        async def stopping(state):
            await real(state)
            ex._stopped = True                       # zatrzymanie w trakcie zapisu własności
        ex._store.async_save = stopping
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == []


def test_stopped_executor_does_not_overwrite_store(monkeypatch):
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_stop()
        await ex.async_set_consent(False)
        await ex.async_set_local_switch(False)
        await ex.async_mark_history_imported("2026-09-23T10:00:00+00:00")
        return await store.async_load()
    state = asyncio.run(go())
    assert state.consent is True and state.local_switch is True and state.history_imported_at is None


def test_consent_withdrawn_during_ownership_save_prevents_writes(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        real = ex._store.async_save

        async def withdrawing(state):
            await real(state)
            ex._state.consent = False                # zgoda cofnięta w trakcie zapisu
        ex._store.async_save = withdrawing
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == [] and ex.last_decision.reason == "gates_changed"


@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_consent_accepts_only_bool(monkeypatch, value):
    _, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_set_consent(value)
    asyncio.run(go())
    assert ex.consent is True


def test_unreadable_store_disables_executor(monkeypatch):
    from homeassistant.helpers import event
    event.async_track_time_interval.reset_mock()
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    asyncio.run(store.async_save(ControlState(consent=True, local_switch=True, owned=True,
                                              snapshot={"soc_min": 15.0})))
    _, ex = make(h, store=store, monkeypatch=monkeypatch)
    saved = store._store._data

    async def broken_load():
        raise ValueError("future version")

    async def go():
        real_load = store._store.async_load
        store._store.async_load = broken_load
        await ex.async_start()
        store._store.async_load = real_load
        raw = plan()
        await ex.async_on_plan(raw, parse_schedule(raw))
        await ex.async_set_consent(False)
        await ex.async_tick()
        await ex.async_restore_now()
    asyncio.run(go())
    assert h.services.calls == [] and store._store._data is saved     # stan z dysku nietknięty
    assert ex.last_decision.reason == "store_unreadable"
    assert event.async_track_time_interval.call_count == 0


def test_start_twice_registers_one_timer(monkeypatch):
    from homeassistant.helpers import event
    event.async_track_time_interval.reset_mock()
    _, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ex.async_start()
        await ex.async_start()
    asyncio.run(go())
    assert event.async_track_time_interval.call_count == 1


def test_ownership_bound_to_profile_and_mode_entity(monkeypatch):
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        return await store.async_load()
    state = asyncio.run(go())
    assert state.owner == {"profile": "goodwe-et", "domain": "goodwe", "mode_entity": E["mode"]}


def test_mismatched_ownership_snapshot_is_not_reused(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    h = goodwe_hass(mode="sell_power")
    store = ControlStore(h, "e1")
    asyncio.run(store.async_save(ControlState(
        consent=False, local_switch=True, owned=True, snapshot={"soc_min": 15.0, "export_limit_w": 0.0},
        owner={"profile": "goodwe-et", "domain": "goodwe", "mode_entity": "select.other_inverter_mode"})))
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ex.async_start()
        await ex.async_tick()
        return await store.async_load()
    state = asyncio.run(go())
    assert h.services.calls == []                    # cudzej migawki nie wpisujemy w nowe encje
    assert state.owned is False and state.snapshot == {} and state.owner == {}
    assert any("different" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "select.other_inverter_mode" not in caplog.text


def test_incomplete_restore_warning_not_repeated_every_tick(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await _charging(ex, h)
        h.states.set(E["soc_max"], "unavailable")
        await ex.async_set_consent(False)
        for _ in range(3):
            await ex.async_tick()
    asyncio.run(go())
    assert sum("incomplete" in r.getMessage() for r in caplog.records) == 1


def test_executors_sharing_a_lock_never_write_at_the_same_time(monkeypatch):
    # Przeładowanie wpisu: stary wykonawca kończy zapis w toku, nowy czeka na niego.
    h = goodwe_hass()
    lock = asyncio.Lock()
    inflight = {"now": 0, "max": 0}
    real_call = h.services.async_call

    async def tracked(*a, **k):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        try:
            await asyncio.sleep(0.02)
            return await real_call(*a, **k)
        finally:
            inflight["now"] -= 1
    h.services.async_call = tracked
    _, old = make(h, monkeypatch=monkeypatch)
    _, new = make(h, monkeypatch=monkeypatch)
    old._lock = new._lock = lock
    old._stop_timeout_s = 0.01

    async def go():
        await ready(old)
        await ready(new)
        running = asyncio.ensure_future(old.async_tick())
        await asyncio.sleep(0.005)
        await old.async_stop()                                    # oddaje po limicie czasu
        await new.async_tick()                                    # czeka na zapis starego
        await running
    asyncio.run(go())
    assert inflight["max"] == 1
    assert new.last_decision is not None                          # tik nowego nie przepadł, tylko poczekał


def _hold_lock_with_slow_write(h, a, b):
    # A trzyma blokadę (wolny zapis), cykl B czeka na nią w kolejce
    real_call = h.services.async_call

    async def slow(*args, **kw):
        await asyncio.sleep(0.02)
        return await real_call(*args, **kw)
    h.services.async_call = slow
    a._lock = b._lock = asyncio.Lock()


@pytest.mark.parametrize("how", ["stop", "freeze"])
def test_queued_tick_rechecks_stop_and_freeze_inside_the_lock(monkeypatch, how):
    h = goodwe_hass()
    _, a = make(h, monkeypatch=monkeypatch)
    _, b = make(h, monkeypatch=monkeypatch)
    _hold_lock_with_slow_write(h, a, b)

    async def go():
        await ready(a)
        await ready(b, raw=plan(power=3000, sid="p2"))           # B zapisałby inną moc
        running = asyncio.ensure_future(a.async_tick())
        await asyncio.sleep(0.005)
        queued = asyncio.ensure_future(b.async_tick())           # minął sprawdzenie przed blokadą
        await asyncio.sleep(0)
        if how == "stop":
            await b.async_stop()                                 # czeka na blokadę (FIFO: najpierw cykl B)
        else:
            b.freeze()
        await running
        await queued
    asyncio.run(go())
    assert h.states.get(E["power_w"]).state == "2000.0"         # tylko zapis A
    assert b.last_decision is None


def test_frozen_executor_never_ticks_but_still_restores(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        assert h.states.get(E["mode"]).state == "sell_power" and ex.owned
        ex.freeze()
        n = len(h.services.calls)
        await ex.async_tick()
        ticked = len(h.services.calls) - n
        await ex.async_restore_now()
        return ticked
    assert asyncio.run(go()) == 0
    assert h.states.get(E["mode"]).state == "auto" and not ex.owned
    assert ex._unsub == []
