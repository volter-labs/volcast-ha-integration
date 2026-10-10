import asyncio
from types import SimpleNamespace

import pytest

from custom_components.volcast.cloud.client import TelemetryResult
from custom_components.volcast.control import runtime as rt_mod

from .setup_harness import API_KEY, run_setup, unique_ids

BASE = "https://s.example.test"
BACKEND = {"base_url": BASE, **{k: f"{BASE}/functions/v1/{k}" for k in (
    "forecast", "submit_production", "telemetry", "schedule", "history_import", "pairing")}}
PAIRED = {"api_key": API_KEY, "api_url": BACKEND["forecast"], "backend": BACKEND, "user_id": "u1"}
FORECAST_IDS = {"test_entry_id_energy_today", "test_entry_id_energy_tomorrow", "test_entry_id_power_now",
                "test_entry_id_api_status", "test_entry_id_peak_production",
                *(f"test_entry_id_energy_day_{d}" for d in range(3, 8))}


@pytest.fixture(autouse=True)
def _no_network_probe(monkeypatch):
    # Wykrywanie w teście nie wysyła pakietów w sieć (jak `setup_forecast_entry`).
    from unittest.mock import AsyncMock

    from custom_components.volcast import discovery_runner
    monkeypatch.setattr(discovery_runner, "probe_udp_48899", AsyncMock(return_value=None))


class FakeExec:
    raw_plan, schedule, consent, local_switch, paused = None, None, None, False, False
    last_decision = tou_preview = None


def fake_runtime():
    return rt_mod.ControlRuntime(executor=FakeExec(), fetcher=None, telemetry=None, cloud=None, choice=None,
                                 mapped={}, rated_power_w=None)


@pytest.mark.asyncio
async def test_paired_entry_adds_control_platforms_and_prefixed_ids(monkeypatch):
    import custom_components.volcast as integ

    async def setup_control(hass, entry, *, report):
        return fake_runtime()
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    ids = unique_ids(hass.entities)
    assert ok and "switch" in hass.config_entries.forwarded
    new = ids - FORECAST_IDS
    assert {"test_entry_id_control_plan", "test_entry_id_control_status", "test_entry_id_control_switch"} <= new
    assert all(i.startswith("test_entry_id_control_") or i in {
        "test_entry_id_discovery", "test_entry_id_run_discovery", "test_entry_id_sync_now",
        "test_entry_id_submit_queue_depth", "test_entry_id_last_reconciliation",
        "test_entry_id_integration_healthy"} for i in new)


@pytest.mark.asyncio
async def test_unpaired_entry_never_builds_control(monkeypatch):
    import custom_components.volcast as integ

    async def boom(*_a, **_k):
        raise AssertionError("control must not be built for an unpaired entry")
    monkeypatch.setattr(integ, "async_setup_control", boom)
    hass, entry, ok = await run_setup(monkeypatch.setattr)
    assert ok and "switch" not in hass.config_entries.forwarded


@pytest.mark.asyncio
async def test_control_failure_does_not_break_forecast(monkeypatch):
    import custom_components.volcast as integ

    async def boom(*_a, **_k):
        raise RuntimeError("x")
    monkeypatch.setattr(integ, "async_setup_control", boom)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and FORECAST_IDS <= unique_ids(hass.entities)
    assert not any("_control_" in (i or "") for i in unique_ids(hass.entities))


@pytest.mark.asyncio
async def test_paired_submit_url_from_backend(monkeypatch):
    import custom_components.volcast as integ
    seen = {}

    class Tracker:
        def __init__(self, **kw):
            seen.update(kw)

        async def async_start(self):
            return None

        async def async_stop(self):
            return None

    async def setup_control(hass, entry, *, report):
        return None
    monkeypatch.setattr(integ, "async_setup_control", setup_control)

    def setattr_(obj, name, value):
        # uprząż podstawia FakeTracker — tu chcemy własny, który zapamięta argumenty
        monkeypatch.setattr(obj, name, Tracker if name == "VolcastProductionTracker" else value)
    await run_setup(setattr_, data=PAIRED, options={"pv_energy_entity": "sensor.pv"})
    assert seen["submit_url"] == BACKEND["submit_production"]


# ── karta planu i panel boczny ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forecast_only_entry_never_registers_card_or_panel(monkeypatch):
    import custom_components.volcast as integ

    async def boom(*_a, **_k):
        raise AssertionError("the card must not be registered for a forecast-only entry")
    monkeypatch.setattr(integ, "async_register_card", boom)
    monkeypatch.setattr(integ, "async_register_panel", boom)
    hass, entry, ok = await run_setup(monkeypatch.setattr)
    assert ok


@pytest.mark.asyncio
async def test_card_or_panel_failure_never_fails_setup(monkeypatch):
    """Platformy i wykonawca już stoją — błąd dodatku (karta/panel) nie może wywrócić setupu,
    bo HA nie rozładowałby wtedy tego, co już wstało."""
    import custom_components.volcast as integ

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    async def boom(*_a, **_k):
        raise KeyError("frontend_extra_module_url")
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_register_card", boom)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and hass.data["volcast"][entry.entry_id]["control"] is not None
    assert "panel" not in hass.data["volcast"][entry.entry_id]


async def _ret(value):
    return value


@pytest.mark.asyncio
async def test_no_control_entry_never_registers_card_or_panel(monkeypatch):
    """Sparowany wpis, którego złożenie sterowania i tak zwróciło `None`."""
    import custom_components.volcast as integ

    async def boom(*_a, **_k):
        raise AssertionError("the card must not be registered without control")
    monkeypatch.setattr(integ, "async_setup_control", lambda hass, entry, *, report: _ret(None))
    monkeypatch.setattr(integ, "async_register_card", boom)
    monkeypatch.setattr(integ, "async_register_panel", boom)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok


@pytest.mark.asyncio
async def test_paired_entry_registers_card_and_panel_with_the_plan_sensor_entity_id(monkeypatch):
    import custom_components.volcast as integ
    from .setup_harness import FakeEntityRegistry

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    seen = {}

    async def register_card(hass, version):
        seen["card_version"] = version
        return "card-url"

    async def register_panel(hass, entity_id, version):
        seen["panel_entity"] = entity_id
        seen["panel_version"] = version
        return True

    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_register_card", register_card)
    monkeypatch.setattr(integ, "async_register_panel", register_panel)

    reg = FakeEntityRegistry()
    reg.entities["sensor.x"] = SimpleNamespace(entity_id="sensor.plan", unique_id="test_entry_id_control_plan")
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, entity_registry=reg)
    assert ok
    assert seen["panel_entity"] == "sensor.plan"
    assert seen["card_version"] == seen["panel_version"]
    assert hass.data["volcast"][entry.entry_id]["panel"] is True


@pytest.mark.asyncio
async def test_panel_not_registered_when_card_registration_fails(monkeypatch):
    import custom_components.volcast as integ
    from .setup_harness import FakeEntityRegistry

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    async def register_card(hass, version):
        return None

    async def boom_panel(*_a, **_k):
        raise AssertionError("the panel must not be registered when the card failed")
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_register_card", register_card)
    monkeypatch.setattr(integ, "async_register_panel", boom_panel)
    reg = FakeEntityRegistry()
    reg.entities["sensor.x"] = SimpleNamespace(entity_id="sensor.plan", unique_id="test_entry_id_control_plan")
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, entity_registry=reg)
    assert ok and hass.data["volcast"][entry.entry_id].get("panel") is None


@pytest.mark.asyncio
async def test_panel_not_registered_when_plan_sensor_is_not_in_the_registry(monkeypatch):
    import custom_components.volcast as integ

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    async def register_card(hass, version):
        return "card-url"

    async def boom_panel(*_a, **_k):
        raise AssertionError("no entity_id to configure the panel with")
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_register_card", register_card)
    monkeypatch.setattr(integ, "async_register_panel", boom_panel)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and hass.data["volcast"][entry.entry_id].get("panel") is None


@pytest.mark.asyncio
async def test_unload_removes_the_panel_only_when_it_was_registered(monkeypatch):
    import custom_components.volcast as integ
    from .setup_harness import FakeEntityRegistry

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    async def unload_control(hass, rt, **_kw):
        return None
    removed = []
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_unload_control", unload_control)
    monkeypatch.setattr(integ, "async_register_card", lambda hass, version: _ret("url"))
    monkeypatch.setattr(integ, "async_register_panel", lambda hass, entity_id, version: _ret(True))
    monkeypatch.setattr(integ, "async_remove_panel", lambda hass: removed.append(True))
    reg = FakeEntityRegistry()
    reg.entities["sensor.x"] = SimpleNamespace(entity_id="sensor.plan", unique_id="test_entry_id_control_plan")
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, entity_registry=reg)
    assert ok
    assert await integ.async_unload_entry(hass, entry)
    assert removed == [True]


@pytest.mark.asyncio
async def test_unload_skips_panel_removal_when_it_was_never_registered(monkeypatch):
    import custom_components.volcast as integ

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    async def unload_control(hass, rt, **_kw):
        return None
    removed = []
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_unload_control", unload_control)
    monkeypatch.setattr(integ, "async_register_card", lambda hass, version: _ret(None))
    monkeypatch.setattr(integ, "async_remove_panel", lambda hass: removed.append(True))
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok
    assert await integ.async_unload_entry(hass, entry)
    assert removed == []


# ── rozładunek i słuchacz aktualizacji ─────────────────────────────────────


@pytest.mark.asyncio
async def test_unload_removes_exactly_the_forwarded_platforms_and_stops_control(monkeypatch):
    import custom_components.volcast as integ
    stopped = []

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    async def unload_control(hass, rt, **_kw):
        stopped.append(rt)
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_unload_control", unload_control)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert await integ.async_unload_entry(hass, entry)
    assert hass.config_entries.unloaded == hass.config_entries.forwarded and "switch" in hass.config_entries.unloaded
    assert len(stopped) == 1


@pytest.mark.asyncio
async def test_unload_restores_when_the_owner_disables_the_entry(monkeypatch):
    import custom_components.volcast as integ
    seen = []

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    async def unload_control(hass, rt, *, restore=False):
        seen.append(restore)
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_unload_control", unload_control)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    entry.disabled_by = "user"                      # jak HA ustawia go przed rozładunkiem wyłączanego wpisu
    assert await integ.async_unload_entry(hass, entry)
    assert seen == [True]


@pytest.mark.asyncio
async def test_unload_does_not_restore_on_plain_reload_or_restart(monkeypatch):
    import custom_components.volcast as integ
    seen = []

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    async def unload_control(hass, rt, *, restore=False):
        seen.append(restore)
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_unload_control", unload_control)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert entry.disabled_by is None                # reload/restart: HA nie ustawia go
    assert await integ.async_unload_entry(hass, entry)
    assert seen == [False]


@pytest.mark.asyncio
async def test_remove_entry_restores_regardless_of_disabled_by(monkeypatch):
    """`async_remove_entry` idzie przez `async_remove_control`, który już zawsze
    przywraca, gdy wpis był właścicielem (patrz `test_remove_entry_restores_only_when_owned`
    w `tests/control/test_runtime.py`) — usunięcie wpisu to usunięcie, niezależnie od tego,
    czy ktoś go wcześniej też wyłączył."""
    import custom_components.volcast as integ
    seen = []

    async def remove_control(hass, entry):
        seen.append(entry.disabled_by)
    monkeypatch.setattr(integ, "async_remove_control", remove_control)
    entry = SimpleNamespace(entry_id="e1", disabled_by="user")
    await integ.async_remove_entry(SimpleNamespace(), entry)
    assert seen == ["user"]


def _listener_hass(rt, options_now):
    from .setup_harness import SetupHass, FakeEntry
    hass = SetupHass()
    entry = FakeEntry(data=PAIRED, options=options_now)
    hass.data["volcast"] = {entry.entry_id: {"control": rt}}
    order = []

    async def reload(entry_id):
        order.append("reload")
    hass.config_entries.async_reload = reload
    return hass, entry, order


class OwnedExec:
    def __init__(self, order):
        self.order, self.owned = order, True

    async def async_restore_now(self):
        self.order.append("restore")
        self.owned = False


@pytest.mark.asyncio
async def test_update_listener_restores_before_reload_on_control_change():
    import custom_components.volcast as integ
    rt = fake_runtime()
    rt.options_at_setup = {"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe"}
    hass, entry, order = _listener_hass(rt, {"profile_id": "goodwe-et", "inverter_domain": "goodwe"})
    rt.executor = OwnedExec(order)
    await integ._async_update_listener(hass, entry)
    assert order == ["restore", "reload"]


@pytest.mark.asyncio
async def test_update_listener_unrelated_change_reloads_without_restore():
    import custom_components.volcast as integ
    rt = fake_runtime()
    rt.options_at_setup = {"control_mode": "entities", "update_interval": 60}
    hass, entry, order = _listener_hass(rt, {"control_mode": "entities", "update_interval": 30})
    rt.executor = OwnedExec(order)
    await integ._async_update_listener(hass, entry)
    assert order == ["reload"]


@pytest.mark.asyncio
async def test_update_listener_load_sensor_only_imports_history_without_reload(monkeypatch):
    import custom_components.volcast as integ
    rt = fake_runtime()
    rt.options_at_setup = {"control_mode": "entities"}
    hass, entry, order = _listener_hass(rt, {"control_mode": "entities", "load_energy_entity": "sensor.house"})
    seen = {}

    async def import_once(h, cloud, executor, *, load_entity, pv_entity=None, now_utc):
        seen["load"] = load_entity
    monkeypatch.setattr(integ, "async_import_history_once", import_once)
    await integ._async_update_listener(hass, entry)
    await asyncio.gather(*hass.tasks)
    assert order == [] and seen == {"load": "sensor.house"}
    assert rt.options_at_setup == dict(entry.options)            # następna zmiana liczona od tego stanu


@pytest.mark.asyncio
async def test_update_listener_without_control_just_reloads():
    import custom_components.volcast as integ
    hass, entry, order = _listener_hass(None, {"update_interval": 30})
    await integ._async_update_listener(hass, entry)
    assert order == ["reload"]


@pytest.mark.asyncio
async def test_remove_entry_never_raises(monkeypatch):
    import custom_components.volcast as integ

    async def boom(hass, entry):
        raise RuntimeError("x")
    monkeypatch.setattr(integ, "async_remove_control", boom)
    await integ.async_remove_entry(SimpleNamespace(), SimpleNamespace(entry_id="e1"))


# ── sterowanie niezależne od pierwszego odświeżenia prognozy ───────────────


class NotReady(Exception):
    """Jak `ConfigEntryNotReady` z `async_config_entry_first_refresh`."""


def _failing_coordinator(error: BaseException, calls: list):
    from .setup_harness import FakeCoordinator

    class Coordinator(FakeCoordinator):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.data = None

        async def _async_update_data(self):
            raise error

        async def async_config_entry_first_refresh(self):
            calls.append("first_refresh")
            try:
                await self._async_update_data()
            except Exception as err:
                raise NotReady from err

        async def async_refresh(self):
            # jak DataUpdateCoordinator.async_refresh: błąd tylko w stanie, nigdy wyjątek
            calls.append("refresh")
            try:
                self.data = await self._async_update_data()
                self.last_update_success = True
            except Exception:
                self.last_update_success = False
    return Coordinator


FORECAST_FAILURES = {
    "offline": (OSError("unreachable"), None),
    "unauthorized": (Exception("Invalid API key"), "auth"),
    "unavailable": (Exception("Forecast not yet available — cache being populated"), None),
}


def _control_hass():
    from .control.ha_fakes import goodwe_hass
    from .setup_harness import SetupHass

    class Hass(SetupHass):
        def __init__(self):
            super().__init__()
            fake = goodwe_hass()
            self.states = fake.states
            services = fake.services
            registered = self.services.registered
            services.registered = registered
            services.has_service = lambda d, s: (d, s) in registered or d != "volcast"
            services.async_register = lambda d, s, h, schema=None: registered.__setitem__((d, s), h)
            services.async_remove = lambda d, s: registered.pop((d, s), None)
            self.services = services
    return Hass()


def _patch_control(monkeypatch, store, schedule):
    from custom_components.volcast.cloud.client import CloudAuthError
    from custom_components.volcast.control import executor as ex_mod
    from custom_components.volcast.core.control.select import ProfileChoice
    from custom_components.volcast.core.profile import load_builtin

    from .control.ha_fakes import GOODWE_ENTITIES

    class Cloud:
        def __init__(self, session, key, backend):
            self.backend = backend

        async def async_get_schedule(self):
            if schedule == "auth":
                raise CloudAuthError
            return None                                              # chmura nieosiągalna

        async def async_post_telemetry(self, reading):
            return TelemetryResult(500, None)

        async def async_import_history(self, hours):
            return None
    gw = ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET")
    monkeypatch.setattr(rt_mod, "VolcastCloud", Cloud)
    monkeypatch.setattr(rt_mod, "async_get_clientsession", lambda hass: None)
    monkeypatch.setattr(rt_mod, "_choice_for", lambda hass, entry, profiles: gw)
    monkeypatch.setattr(rt_mod, "map_entities", lambda hass, choice: dict(GOODWE_ENTITIES))
    monkeypatch.setattr(rt_mod, "ControlStore", lambda hass, entry_id: store)
    monkeypatch.setattr(ex_mod, "control_verified", lambda *_: True)
    # Drabina weryfikacji w próbie bez zapisu (szczebel 1→3): testy setupu liczą zapisy samego planu.
    monkeypatch.setattr(rt_mod, "start_rung_for", lambda *_, **__: 1)


async def _setup(monkeypatch, hass, coordinator, *, data=PAIRED, options=None):
    import custom_components.volcast as integ

    from .setup_harness import FakeEntry, FakeReconciler, FakeTracker, drain
    monkeypatch.setattr(integ, "VolcastCoordinator", coordinator)
    monkeypatch.setattr(integ, "VolcastProductionTracker", FakeTracker)
    monkeypatch.setattr(integ, "DailyReconciler", FakeReconciler)
    entry = FakeEntry(data=data, options={"control_mode": "entities"} if options is None else options)
    ok = await integ.async_setup_entry(hass, entry)
    await drain(hass)
    return entry, ok


def _stored_plan():
    from custom_components.volcast.control.store import ControlState, ControlStore

    from .control.test_executor import plan
    store = ControlStore(object(), "test_entry_id")
    asyncio.run(store.async_save(ControlState(plan_raw=plan(), consent=True, local_switch=True)))
    return store


@pytest.mark.parametrize("case", sorted(FORECAST_FAILURES))
def test_forecast_failure_at_setup_never_blocks_control(monkeypatch, case):
    from .control.ha_fakes import GOODWE_ENTITIES as E
    error, schedule = FORECAST_FAILURES[case]
    store, calls = _stored_plan(), []
    _patch_control(monkeypatch, store, schedule)
    hass = _control_hass()

    async def go():
        entry, ok = await _setup(monkeypatch, hass, _failing_coordinator(error, calls))
        rt = hass.data["volcast"][entry.entry_id]["control"]
        after_setup = hass.states.get(E["mode"]).state
        if schedule == "auth":
            # drugie 401 z get-schedule = cofnięta zgoda → powrót do trybu bazowego w cyklu
            await rt.fetcher.async_refresh()
            await rt.executor.async_tick()
        else:
            await rt.executor.async_restore_now()
        return entry, ok, rt, after_setup
    entry, ok, rt, after_setup = asyncio.run(go())
    assert ok and calls == ["refresh"]                     # bez first_refresh → bez ConfigEntryNotReady
    assert "switch" in hass.config_entries.forwarded
    assert FORECAST_IDS <= unique_ids(hass.entities)
    assert after_setup == "sell_power"                     # cykl z planu z magazynu, bez chmury
    assert hass.states.get(E["mode"]).state == "auto" and not rt.executor.owned
    asyncio.run(rt_mod.async_unload_control(hass, rt))


def test_forecast_only_entry_still_fails_setup_on_first_refresh(monkeypatch):
    # regresja: wpis bez konta (tylko prognoza) zachowuje się jak dotąd — ConfigEntryNotReady
    calls = []
    hass = _control_hass()
    with pytest.raises(NotReady):
        asyncio.run(_setup(monkeypatch, hass, _failing_coordinator(OSError("x"), calls),
                           data={"api_key": API_KEY}, options={}))
    assert calls == ["first_refresh"] and hass.config_entries.forwarded == []


def test_paired_entry_with_working_forecast_uses_non_raising_refresh(monkeypatch):
    from .setup_harness import FakeCoordinator
    calls = []

    class Coordinator(FakeCoordinator):
        async def async_config_entry_first_refresh(self):
            calls.append("first_refresh")

        async def async_refresh(self):
            calls.append("refresh")
    _patch_control(monkeypatch, _stored_plan(), None)
    hass = _control_hass()
    entry, ok = asyncio.run(_setup(monkeypatch, hass, Coordinator))
    assert ok and calls == ["refresh"]
    asyncio.run(rt_mod.async_unload_control(hass, hass.data["volcast"][entry.entry_id]["control"]))


# ── nieudany setup i przeładowania nie mnożą wykonawców ────────────────────


def _recording_executors(monkeypatch):
    made = []

    class Rec(rt_mod.VolcastExecutor):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)
    monkeypatch.setattr(rt_mod, "VolcastExecutor", Rec)
    return made


def _live(made):
    return [ex for ex in made if ex._started and not ex._stopped]


def test_failed_control_setup_and_reloads_never_accumulate_executors(monkeypatch):
    import custom_components.volcast as integ

    from .setup_harness import FakeCoordinator
    _patch_control(monkeypatch, _stored_plan(), None)
    made = _recording_executors(monkeypatch)
    fail = {"on": True}

    class Telemetry(rt_mod.TelemetrySender):
        async def async_start(self):
            if fail["on"]:
                raise RuntimeError("x")
            await super().async_start()
    monkeypatch.setattr(rt_mod, "TelemetrySender", Telemetry)
    hass = _control_hass()

    async def go():
        for _ in range(3):                                       # setup z błędem + przeładowanie
            entry, ok = await _setup(monkeypatch, hass, FakeCoordinator)
            assert ok and hass.data["volcast"][entry.entry_id]["control"] is None
            assert "switch" not in hass.config_entries.forwarded
            assert _live(made) == []
            assert await integ.async_unload_entry(hass, entry)
        fail["on"] = False
        entry, ok = await _setup(monkeypatch, hass, FakeCoordinator)
        assert len(_live(made)) == 1
        assert await integ.async_unload_entry(hass, entry)
        assert _live(made) == []
    asyncio.run(go())
    assert len(made) == 4


def test_platform_forward_failure_after_control_stops_the_executor(monkeypatch):
    from .setup_harness import FakeCoordinator
    _patch_control(monkeypatch, _stored_plan(), None)
    made = _recording_executors(monkeypatch)
    hass = _control_hass()
    real_forward = hass.config_entries.async_forward_entry_setups
    fail = {"on": True}

    async def forward(entry, platforms):
        if fail["on"]:
            raise RuntimeError("platform setup failed")
        await real_forward(entry, platforms)
    hass.config_entries.async_forward_entry_setups = forward

    async def go():
        with pytest.raises(RuntimeError):
            await _setup(monkeypatch, hass, FakeCoordinator)
        assert _live(made) == []
        assert hass.data["volcast"]["test_entry_id"]["control"] is None
        fail["on"] = False
        entry, ok = await _setup(monkeypatch, hass, FakeCoordinator)   # HA ponawia setup
        assert ok and len(_live(made)) == 1
        await rt_mod.async_unload_control(hass, hass.data["volcast"][entry.entry_id]["control"])
    asyncio.run(go())


# ── zamrożenie starego wykonawcy przed powrotem i przeładowaniem ───────────


def test_update_listener_freezes_old_executor_before_restore(monkeypatch):
    # Cykl, który wystartuje między powrotem a zatrzymaniem (stare mapowanie), nic nie zapisze.
    import custom_components.volcast as integ

    from .control.ha_fakes import GOODWE_ENTITIES as E
    from .setup_harness import FakeCoordinator
    _patch_control(monkeypatch, _stored_plan(), None)
    hass = _control_hass()
    order = []

    async def go():
        entry, ok = await _setup(monkeypatch, hass, FakeCoordinator)
        rt = hass.data["volcast"][entry.entry_id]["control"]
        assert hass.states.get(E["mode"]).state == "sell_power" and rt.executor.owned
        unsubbed = []
        rt.unsubs.append(lambda: unsubbed.append(True))

        async def reload(entry_id):
            n = len(hass.services.calls)
            await rt.executor.async_tick()                        # np. licznik albo wyłącznik
            order.append(("reload", len(hass.services.calls) - n))
        hass.config_entries.async_reload = reload
        entry.options = {**entry.options, "profile_id": "goodwe-et", "inverter_domain": "goodwe_other"}
        await integ._async_update_listener(hass, entry)
        return rt, unsubbed
    rt, unsubbed = asyncio.run(go())
    assert hass.states.get(E["mode"]).state == "auto" and not rt.executor.owned   # powrót zrobiony
    assert order == [("reload", 0)]                                                # po powrocie: zero zapisów
    assert unsubbed == [True] and rt.unsubs == []
