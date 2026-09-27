import asyncio
import logging
from types import SimpleNamespace

from homeassistant.core import Context
from homeassistant.helpers import event as ha_event
from homeassistant.helpers import issue_registry as ir

from custom_components.volcast.control.store import ControlState, ControlStore

from .ha_fakes import GOODWE_ENTITIES as E, FakeState, goodwe_hass
from .test_executor import LOGGER, make, no_entity_ids_in, ready

ISSUE = "foreign_control_e1"


def event(eid, state, ctx, unit=None):
    attrs = {"unit_of_measurement": unit} if unit else {}
    return SimpleNamespace(data={"entity_id": eid, "new_state": FakeState(eid, state, attrs, context=ctx)},
                           context=ctx)


def _written(monkeypatch, h=None):
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    return h, ex


def _created(issue=ISSUE):
    return [c for c in ir.async_create_issue.call_args_list if c.args[2] == issue]


def test_user_change_pauses_and_raises_issue(monkeypatch):
    ir.async_create_issue.reset_mock()
    h, ex = _written(monkeypatch)
    asyncio.run(ex.async_on_state_event(event(E["mode"], "auto", Context(user_id="u1"))))
    assert ex.paused and ex.foreign_changes[-1]["key"] == "mode"
    assert "foreign_control_e1" in [c.args[2] for c in ir.async_create_issue.call_args_list]
    n = len(h.services.calls)
    asyncio.run(ex.async_tick())
    assert len(h.services.calls) == n                          # w pauzie zero zapisów


def test_automation_change_is_foreign(monkeypatch):
    _, ex = _written(monkeypatch)
    asyncio.run(ex.async_on_state_event(event(E["power_w"], "500", Context(parent_id="auto1"), "W")))
    assert ex.paused


def test_device_refresh_without_actor_is_not_foreign(monkeypatch):
    _, ex = _written(monkeypatch)
    asyncio.run(ex.async_on_state_event(event(E["power_w"], "500", Context(), "W")))
    assert not ex.paused


def test_own_write_echo_is_not_foreign(monkeypatch):
    h, ex = _written(monkeypatch)
    own_ctx = h.services.calls[-1][3]
    asyncio.run(ex.async_on_state_event(event(E["mode"], "auto", Context(user_id="u1", id=own_ctx.id))))
    assert not ex.paused


def test_pause_ends_and_issue_is_deleted(monkeypatch):
    ir.async_delete_issue.reset_mock()
    h, ex = _written(monkeypatch)
    h.states.set(E["mode"], "auto")                              # właściciel przełączył tryb
    asyncio.run(ex.async_on_state_event(event(E["mode"], "auto", Context(user_id="u1"))))
    ex._clock.t += 1801
    asyncio.run(ex.async_tick())
    assert not ex.paused
    assert "foreign_control_e1" in [c.args[2] for c in ir.async_delete_issue.call_args_list]
    assert h.states.get(E["mode"]).state == "sell_power"         # po pauzie plan wraca


# ── dopowiedzenia: surowa opcja, sygnał poziomu, logi, wyjątki ─────────────


def test_issue_deleted_only_when_pause_ends(monkeypatch):
    h, ex = _written(monkeypatch)
    ir.async_delete_issue.reset_mock()
    asyncio.run(ex.async_on_state_event(event(E["power_w"], "500", Context(user_id="u1"), "W")))
    asyncio.run(ex.async_tick())
    assert ISSUE not in [c.args[2] for c in ir.async_delete_issue.call_args_list]
    ex._clock.t += 1801
    asyncio.run(ex.async_tick())
    assert ISSUE in [c.args[2] for c in ir.async_delete_issue.call_args_list]


def test_user_switch_to_option_outside_profile_is_foreign(monkeypatch):
    _, ex = _written(monkeypatch)
    asyncio.run(ex.async_on_state_event(event(E["mode"], "export_ac", Context(user_id="u1"))))
    assert ex.paused and ex.foreign_changes[-1]["key"] == "mode"


def test_unavailable_mode_event_is_not_foreign(monkeypatch):
    _, ex = _written(monkeypatch)
    asyncio.run(ex.async_on_state_event(event(E["mode"], "unavailable", Context(user_id="u1"))))
    assert not ex.paused and ex.foreign_changes == []


def test_log_names_key_never_entity_id_and_issue_keeps_entity_placeholder(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    ir.async_create_issue.reset_mock()
    _, ex = _written(monkeypatch)
    asyncio.run(ex.async_on_state_event(event(E["power_w"], "500", Context(user_id="u1"), "W")))
    assert "power_w" in caplog.text and no_entity_ids_in(caplog.text)
    (call,) = _created()
    assert call.kwargs["translation_placeholders"] == {"entity_id": E["power_w"]}
    assert ex.foreign_changes[-1]["entity_id"] == E["power_w"]
    assert ex.exec_summary()["foreign_changes"] == 1             # telemetria: sam licznik


def test_repeated_foreign_changes_raise_issue_once_and_keep_last_20(monkeypatch):
    ir.async_create_issue.reset_mock()
    _, ex = _written(monkeypatch)
    for i in range(25):
        asyncio.run(ex.async_on_state_event(event(E["power_w"], str(500 + 10 * i), Context(user_id="u1"), "W")))
    assert len(_created()) == 1 and len(ex.foreign_changes) == 20


def test_foreign_mode_on_device_is_edge_triggered(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    ir.async_create_issue.reset_mock()
    h, ex = _written(monkeypatch)
    h.states.set(E["mode"], "export_ac")                          # zmiana bez aktora, spoza profilu
    for _ in range(3):
        asyncio.run(ex.async_tick())
        ex._clock.t += 61
    assert ex.last_decision.reason == "foreign_mode"
    assert len(_created()) == 1 and len(ex.foreign_changes) == 1 and ex.paused
    assert sum("outside the profile" in r.getMessage() for r in caplog.records) == 1
    assert no_entity_ids_in(caplog.text)


def test_foreign_mode_episode_ends_and_can_start_again(monkeypatch):
    ir.async_create_issue.reset_mock()
    h, ex = _written(monkeypatch)
    h.states.set(E["mode"], "export_ac")
    asyncio.run(ex.async_tick())
    h.states.set(E["mode"], "auto")
    ex._clock.t += 1801
    asyncio.run(ex.async_tick())
    assert not ex.paused
    h.states.set(E["mode"], "export_ac")
    asyncio.run(ex.async_tick())
    assert len(ex.foreign_changes) == 2 and len(_created()) == 2


def test_foreign_mode_unavailable_reading_keeps_episode(monkeypatch):
    h, ex = _written(monkeypatch)
    h.states.set(E["mode"], "export_ac")
    asyncio.run(ex.async_tick())
    h.states.set(E["mode"], "unavailable")
    asyncio.run(ex.async_tick())
    h.states.set(E["mode"], "export_ac")
    asyncio.run(ex.async_tick())
    assert len(ex.foreign_changes) == 1                          # brak odczytu nie kończy epizodu


def test_foreign_mode_suppressed_while_control_closed(monkeypatch):
    ir.async_create_issue.reset_mock()
    for kw in ({"verified": False}, {"consent": False}, {"local": False}, {"options": {}}):
        h = goodwe_hass(mode="export_ac")
        opts = kw.pop("options", None)
        h, ex = make(h, monkeypatch=monkeypatch, options=opts, verified=kw.pop("verified", True))

        async def go():
            await ready(ex, **kw)
            await ex.async_tick()
        asyncio.run(go())
        assert not ex.paused and ex.foreign_changes == []
    assert _created() == []


def test_foreign_mode_not_masked_by_earlier_block(monkeypatch):
    ir.async_create_issue.reset_mock()
    h = goodwe_hass(mode="export_ac", temp="unavailable")         # blokada temperatury przed trybem
    h, ex = make(h, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert ex.last_decision.reason == "temperature_unknown"
    assert ex.paused and len(_created()) == 1


def test_state_event_handler_survives_exceptions(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    _, ex = _written(monkeypatch)

    def boom(_ctx_id):
        raise RuntimeError("host 10.0.0.1")
    ex._writer.is_ours = boom
    asyncio.run(ex.async_on_state_event(event(E["power_w"], "500", Context(user_id="u1"), "W")))
    asyncio.run(ex.async_on_state_event(SimpleNamespace(data=None, context=None)))
    assert not ex.paused and "10.0.0.1" not in caplog.text


def test_events_ignored_for_unwatched_or_after_stop(monkeypatch):
    _, ex = _written(monkeypatch)
    asyncio.run(ex.async_on_state_event(event(E["soc"], "10", Context(user_id="u1"), "%")))
    assert not ex.paused
    asyncio.run(ex.async_stop())
    asyncio.run(ex.async_on_state_event(event(E["power_w"], "500", Context(user_id="u1"), "W")))
    assert not ex.paused


def test_start_watches_write_key_entities_only(monkeypatch):
    ha_event.async_track_state_change_event.reset_mock()
    _, ex = make(monkeypatch=monkeypatch)
    asyncio.run(ex.async_start())
    (call,) = ha_event.async_track_state_change_event.call_args_list
    assert sorted(call.args[1]) == sorted(E[k] for k in ("mode", "power_w", "soc_min", "soc_max",
                                                         "export_limit_w", "export_limit_enabled"))


def test_start_clears_issue_left_by_previous_run(monkeypatch):
    ir.async_delete_issue.reset_mock()
    _, ex = make(monkeypatch=monkeypatch)
    asyncio.run(ex.async_start())
    assert ISSUE in [c.args[2] for c in ir.async_delete_issue.call_args_list]


def test_pause_during_ownership_save_prevents_writes(monkeypatch):
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        real = ex._store.async_save

        async def pausing(state):
            await real(state)
            ex._memory.paused_until = ex._clock() + 1800        # obca zmiana w trakcie zapisu
        ex._store.async_save = pausing
        await ex.async_tick()
    asyncio.run(go())
    assert h.services.calls == [] and ex.last_decision.reason == "gates_changed"


def test_foreign_change_holds_baseline_restore(monkeypatch):
    h, ex = _written(monkeypatch)
    n = len(h.services.calls)
    h.states.set(E["mode"], "charge_pv")
    asyncio.run(ex.async_on_state_event(event(E["mode"], "charge_pv", Context(user_id="u1"))))
    asyncio.run(ex.async_set_consent(False))
    asyncio.run(ex.async_tick())
    assert len(h.services.calls) == n and h.states.get(E["mode"]).state == "charge_pv"


def test_unreadable_store_raises_control_error_issue(monkeypatch):
    ir.async_create_issue.reset_mock()
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def broken_load():
        raise ValueError("future version")
    store._store.async_load = broken_load
    asyncio.run(ex.async_start())
    assert _created("control_error_e1")


def test_restart_while_paused_resumes_control(monkeypatch):
    # Znane ograniczenie: pauza (zegar monotoniczny) nie przeżywa restartu.
    h = goodwe_hass()
    store = ControlStore(h, "e1")
    _, ex = make(h, store=store, monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        await ex.async_on_state_event(event(E["power_w"], "500", Context(user_id="u1"), "W"))
        _, ex2 = make(h, store=store, monkeypatch=monkeypatch)
        await ex2.async_start()
        return ex2
    ex2 = asyncio.run(go())
    assert ex.paused and not ex2.paused
    assert asyncio.run(store.async_load()) != ControlState()


def test_takeover_signal_blocks_writes_in_the_same_tick(monkeypatch):
    from custom_components.volcast.control import executor as ex_mod
    h = goodwe_hass(mode="export_ac")
    h, ex = make(h, monkeypatch=monkeypatch)
    real = ex_mod.decide_cycle

    def write_anyway(**kw):                    # cykl, który nie rozpoznał obcego trybu
        d = real(**kw)
        kw["ents"].readings.pop("mode", None)
        return real(**{**kw, "ents": kw["ents"]}) if d.reason == "foreign_mode" else d
    monkeypatch.setattr(ex_mod, "decide_cycle", write_anyway)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert ex.paused and h.services.calls == [] and ex.last_decision.reason == "paused"


def test_foreign_mode_lasting_past_the_pause_is_not_signalled_again(monkeypatch):
    ir.async_create_issue.reset_mock()
    h, ex = _written(monkeypatch)
    ir.async_delete_issue.reset_mock()                  # start sprząta zgłoszenie poprzedniego przebiegu
    h.states.set(E["mode"], "export_ac")
    asyncio.run(ex.async_tick())
    ex._clock.t += 1801
    asyncio.run(ex.async_tick())
    # epizod trwa: bez drugiej pauzy i wpisu, zgłoszenie zostaje otwarte, zapisów brak
    assert not ex.paused and len(ex.foreign_changes) == 1 and len(_created()) == 1
    assert ISSUE not in [c.args[2] for c in ir.async_delete_issue.call_args_list]
    assert ex.last_decision.reason == "foreign_mode"
