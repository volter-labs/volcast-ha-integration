import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.store import ControlState, ControlStore
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin

BASE = "https://s.example.test"
BACKEND = {"base_url": BASE, **{k: f"{BASE}/functions/v1/{k}" for k in (
    "forecast", "submit_production", "telemetry", "schedule", "history_import", "pairing")}}


def test_unpaired_returns_none_without_network():
    entry = SimpleNamespace(entry_id="e1", data={"api_key": "vk_x"}, options={})
    assert asyncio.run(rt_mod.async_setup_control(object(), entry, report=lambda: None)) is None


def test_map_entities_by_unique_id_and_units():
    reg = SimpleNamespace(entities={
        "a": SimpleNamespace(entity_id="select.gw_mode", platform="goodwe", unique_id="goodwe-ems_mode-SN1",
                             disabled_by=None, unit_of_measurement=None),
        "b": SimpleNamespace(entity_id="number.gw_power", platform="goodwe",
                             unique_id="goodwe-ems_power_limit-SN1", disabled_by=None, unit_of_measurement="W"),
        "c": SimpleNamespace(entity_id="number.other", platform="other", unique_id="goodwe-ems_mode-X",
                             disabled_by=None, unit_of_measurement=None)})
    hass = SimpleNamespace(data={"entity_registry": reg}, states=SimpleNamespace(get=lambda e: None))
    choice = ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET")
    assert rt_mod.map_entities(hass, choice) == {"mode": "select.gw_mode", "power_w": "number.gw_power"}
    assert rt_mod.map_entities(hass, ProfileChoice(load_builtin("deye-sg"), None, None)) == {}


def test_unload_control_restores_only_when_requested():
    calls = []

    class Exec:
        async def async_restore_now(self):
            calls.append("restore")

        async def async_stop(self):
            calls.append("stop")

    class Telemetry:
        async def async_stop(self):
            calls.append("telemetry_stop")

    rt = rt_mod.ControlRuntime(executor=Exec(), fetcher=None, telemetry=Telemetry(), cloud=None,
                               choice=None, mapped={}, rated_power_w=None)
    asyncio.run(rt_mod.async_unload_control(object(), rt))
    assert calls == ["telemetry_stop", "stop"]                     # domyślnie: bez przywracania

    calls.clear()
    asyncio.run(rt_mod.async_unload_control(object(), rt, restore=True))
    assert calls == ["restore", "telemetry_stop", "stop"]           # przywrócenie PRZED zatrzymaniem


def test_unload_control_restore_failure_never_blocks_the_unload():
    calls = []

    class Exec:
        async def async_restore_now(self):
            raise RuntimeError("boom")

        async def async_stop(self):
            calls.append("stop")

    class Telemetry:
        async def async_stop(self):
            calls.append("telemetry_stop")

    rt = rt_mod.ControlRuntime(executor=Exec(), fetcher=None, telemetry=Telemetry(), cloud=None,
                               choice=None, mapped={}, rated_power_w=None)
    asyncio.run(rt_mod.async_unload_control(object(), rt, restore=True))
    assert calls == ["telemetry_stop", "stop"]


def test_remove_entry_restores_only_when_owned(monkeypatch):
    restored = []

    class Exec:
        def __init__(self, *a, **k):
            pass

        async def async_start(self):
            return None

        async def async_restore_now(self):
            restored.append(True)

        async def async_stop(self):
            return None

    monkeypatch.setattr(rt_mod, "VolcastExecutor", Exec)
    monkeypatch.setattr(rt_mod, "_choice_for", lambda hass, entry, profiles: None)
    monkeypatch.setattr(rt_mod, "_load_profiles", lambda: [])
    hass = SimpleNamespace(data={}, async_add_executor_job=lambda f, *a: _ret(f(*a)))
    entry = SimpleNamespace(entry_id="e1", options={}, data={"api_key": "vk_x", "backend": BACKEND})
    store = ControlStore(hass, "e1")
    monkeypatch.setattr(rt_mod, "ControlStore", lambda h, eid: store)
    asyncio.run(rt_mod.async_remove_control(hass, entry))
    assert restored == []
    asyncio.run(store.async_save(ControlState(owned=True)))
    asyncio.run(rt_mod.async_remove_control(hass, entry))
    assert restored == [True] and asyncio.run(store.async_load()) == ControlState()


def test_remove_entry_warns_when_no_profile_can_restore(monkeypatch, caplog):
    caplog.set_level("WARNING", logger="custom_components.volcast.control.runtime")

    class Exec:
        def __init__(self, *a, **k):
            pass

        async def async_start(self):
            return None

        async def async_restore_now(self):
            return None          # profil nieznany, więc wykonawca i tak nic nie robi

        async def async_stop(self):
            return None

    monkeypatch.setattr(rt_mod, "VolcastExecutor", Exec)
    monkeypatch.setattr(rt_mod, "_choice_for", lambda hass, entry, profiles: None)
    monkeypatch.setattr(rt_mod, "_load_profiles", lambda: [])
    hass = SimpleNamespace(data={}, async_add_executor_job=lambda f, *a: _ret(f(*a)))
    entry = SimpleNamespace(entry_id="e1", options={}, data={"api_key": "vk_x", "backend": BACKEND})
    store = ControlStore(hass, "e1")
    monkeypatch.setattr(rt_mod, "ControlStore", lambda h, eid: store)
    asyncio.run(store.async_save(ControlState(owned=True)))
    asyncio.run(rt_mod.async_remove_control(hass, entry))
    assert "could not return the inverter" in caplog.text
    assert asyncio.run(store.async_load()) == ControlState()


def test_remove_entry_tolerates_unreadable_store(monkeypatch):
    removed = []

    class BadStore:
        async def async_load(self):
            raise ValueError("future version")

        async def async_remove(self):
            removed.append(True)
    monkeypatch.setattr(rt_mod, "ControlStore", lambda h, eid: BadStore())
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(entry_id="e1", options={}, data={"api_key": "vk_x", "backend": BACKEND})
    asyncio.run(rt_mod.async_remove_control(hass, entry))
    assert removed == [True]


async def _ret(v):
    return v


# ── pełny setup sterowania na atrapach ────────────────────────────────────


class FakeCloud:
    def __init__(self, session, key, backend):
        self.backend = backend

    async def async_get_schedule(self):
        return None

    async def async_post_telemetry(self, reading):
        return True

    async def async_import_history(self, hours):
        return None


def _setup_hass():
    from tests.setup_harness import SetupHass

    class Hass(SetupHass):
        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

        def async_create_background_task(self, coro, name, **_kw):
            self.events.append(f"hass_background:{name}")
            task = asyncio.get_running_loop().create_task(coro)
            self.tasks.append(task)
            return task
    h = Hass()
    h.config_entries.async_get_entry = lambda eid: None
    return h


def _entry(**data):
    from tests.setup_harness import FakeEntry
    return FakeEntry(data={"api_key": "vk_" + "a" * 64, "backend": BACKEND, **data},
                     options={"control_mode": "entities", "update_interval": 30})


def _patch(monkeypatch):
    monkeypatch.setattr(rt_mod, "VolcastCloud", FakeCloud)
    monkeypatch.setattr(rt_mod, "async_get_clientsession", lambda hass: None)
    monkeypatch.setattr(rt_mod, "_choice_for", lambda hass, entry, profiles: None)


@pytest.mark.asyncio
async def test_setup_keeps_options_copy_and_starts_first_tick_and_fetch(monkeypatch):
    from tests.setup_harness import drain
    _patch(monkeypatch)
    hass, entry = _setup_hass(), _entry()
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    await drain(hass)
    assert rt.options_at_setup == entry.options and rt.options_at_setup is not entry.options
    # pierwszy cykl i pierwsze pobranie nie są zadaniami wpisu — unload nie przerywa ich w pół zapisu
    assert "hass_background:volcast_first_tick" in hass.events
    assert "hass_background:volcast_first_fetch" in hass.events
    assert rt.executor.last_decision is not None                 # tik przebiegł zaraz po starcie
    assert hass.data["volcast"]["test_entry_id"]["control"] is rt
    await rt_mod.async_unload_control(hass, rt)


@pytest.mark.asyncio
async def test_old_and_new_executor_share_the_entry_lock(monkeypatch):
    _patch(monkeypatch)
    hass, entry = _setup_hass(), _entry()
    rt1 = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    await rt_mod.async_unload_control(hass, rt1)
    rt2 = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    assert rt1.executor._lock is rt2.executor._lock
    other = await rt_mod.async_setup_control(hass, _entry_other(), report=lambda: None)
    assert other.executor._lock is not rt2.executor._lock
    await rt_mod.async_unload_control(hass, rt2)
    await rt_mod.async_unload_control(hass, other)


def _entry_other():
    from tests.setup_harness import FakeEntry
    return FakeEntry(data={"api_key": "vk_" + "a" * 64, "backend": BACKEND}, entry_id="other")


@pytest.mark.asyncio
async def test_runtime_is_stored_before_onboarding_reads_it(monkeypatch):
    import custom_components.volcast.onboarding as ob_mod
    import homeassistant.util.dt as dt_util
    _patch(monkeypatch)
    seen = {}

    class FakeOnboarding:
        def __init__(self, hass, entry_id, *, runtime, **_kw):
            self._runtime = runtime

        async def async_run(self):
            return None

    monkeypatch.setattr(ob_mod, "Onboarding", FakeOnboarding)
    hass = _setup_hass()
    real_create = hass.async_create_background_task

    def create(coro, name, **kw):
        # start „na gorąco" (eager_start): onboarding czyta runtime od razu
        if name == "volcast_onboarding":
            frame_self = coro.cr_frame.f_locals.get("self")
            seen["rt"] = frame_self._runtime() if frame_self is not None else "no-self"
        return real_create(coro, name, **kw)
    hass.async_create_background_task = create
    live = (dt_util.utcnow() + timedelta(minutes=20)).isoformat()
    entry = _entry(pairing={"session_id": "s1", "poll_token": "p", "live_until": live,
                            "url": f"{BASE}/functions/v1/pairing-session"})
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    assert seen["rt"] is rt
    await rt_mod.async_unload_control(hass, rt)
    for t in hass.tasks:
        t.cancel()


# ── zakres encji falownika (auto-wybór licznika zużycia domu) ─────────────


def _reg_entry(eid, platform, ce, dev):
    return SimpleNamespace(entity_id=eid, platform=platform, config_entry_id=ce, device_id=dev,
                           unique_id=eid, disabled_by=None, unit_of_measurement=None)


def test_inverter_entity_ids_only_from_the_mapped_inverters_entry_or_device():
    reg = SimpleNamespace(entities={e.entity_id: e for e in (
        _reg_entry("select.gw_mode", "goodwe", "ce1", "d1"),
        _reg_entry("sensor.gw_total_load", "goodwe", "ce1", "d1"),
        _reg_entry("sensor.gw_house_consumption", "goodwe", "ce1", "d2"),       # ten sam wpis, inne urządzenie
        _reg_entry("sensor.gw2_total_load", "goodwe", "ce2", "d9"),             # drugi falownik tej marki
        _reg_entry("sensor.heat_pump_consumption", "shelly", "ce3", "d1"),      # obca platforma na urządzeniu
    )})
    hass = SimpleNamespace(data={"entity_registry": reg})
    choice = ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET")
    got = rt_mod.inverter_entity_ids(hass, choice, {"mode": "select.gw_mode"})
    assert got == frozenset({"select.gw_mode", "sensor.gw_total_load", "sensor.gw_house_consumption"})
    assert rt_mod.inverter_entity_ids(hass, choice, {}) == frozenset()             # nic zmapowanego
    assert rt_mod.inverter_entity_ids(hass, None, {"mode": "select.gw_mode"}) == frozenset()


@pytest.mark.asyncio
async def test_onboarding_history_import_sends_pv_series_like_the_setup_import(monkeypatch):
    import custom_components.volcast.onboarding as ob_mod
    import homeassistant.util.dt as dt_util
    _patch(monkeypatch)
    captured, seen = {}, {}

    class FakeOnboarding:
        def __init__(self, hass, entry_id, *, import_history, **_kw):
            captured["import"] = import_history

        async def async_run(self):
            return None

    async def import_once(h, cloud, executor, *, load_entity, pv_entity=None, now_utc):
        seen.update(load=load_entity, pv=pv_entity)
        return {"accepted": 1}
    monkeypatch.setattr(ob_mod, "Onboarding", FakeOnboarding)
    monkeypatch.setattr(rt_mod, "async_import_history_once", import_once)
    hass = _setup_hass()
    live = (dt_util.utcnow() + timedelta(minutes=20)).isoformat()
    entry = _entry(pairing={"session_id": "s1", "poll_token": "p", "live_until": live,
                            "url": f"{BASE}/functions/v1/pairing-session"})
    entry.options = {**entry.options, "load_energy_entity": "sensor.load", "pv_energy_entity": "sensor.pv"}
    hass.config_entries.async_get_entry = lambda eid: entry
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    seen.clear()
    rt.executor._state.history_imported_at = None
    await captured["import"]()
    assert seen == {"load": "sensor.load", "pv": "sensor.pv"}
    await rt_mod.async_unload_control(hass, rt)
    for t in hass.tasks:
        t.cancel()


# ── sprzątanie po nieudanym złożeniu ──────────────────────────────────────


def _recording_executor(monkeypatch):
    made = []

    class Rec(rt_mod.VolcastExecutor):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)
    monkeypatch.setattr(rt_mod, "VolcastExecutor", Rec)
    return made


def _live(made):
    return [ex for ex in made if ex._started and not ex._stopped]


@pytest.mark.parametrize("where", ["telemetry", "timer", "onboarding"])
@pytest.mark.asyncio
async def test_failure_after_executor_start_stops_everything(monkeypatch, where):
    from tests.setup_harness import drain
    _patch(monkeypatch)
    made = _recording_executor(monkeypatch)
    stopped_telemetry, unsubbed = [], []

    class Telemetry(rt_mod.TelemetrySender):
        async def async_start(self):
            if where == "telemetry":
                raise RuntimeError("x")
            await super().async_start()

        async def async_stop(self):
            stopped_telemetry.append(True)
            await super().async_stop()
    monkeypatch.setattr(rt_mod, "TelemetrySender", Telemetry)

    def track(hass, action, interval):
        if where == "timer":
            raise RuntimeError("x")
        return lambda: unsubbed.append(True)
    monkeypatch.setattr(rt_mod, "async_track_time_interval", track)

    def onboarding(*_a):
        raise RuntimeError("x")
    if where == "onboarding":
        monkeypatch.setattr(rt_mod, "_maybe_start_onboarding", onboarding)
    hass, entry = _setup_hass(), _entry()
    with pytest.raises(RuntimeError):
        await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    await drain(hass)
    assert len(made) == 1 and made[0]._stopped and made[0]._unsub == []
    assert _live(made) == []
    assert (hass.data.get("volcast", {}).get(entry.entry_id) or {}).get("control") is None
    if where == "onboarding":
        assert stopped_telemetry and unsubbed                              # licznik pobierania zdjęty
    if where == "timer":
        assert stopped_telemetry
    assert made[0].last_decision is None                                   # zatrzymany nie zrobił cyklu


@pytest.mark.asyncio
async def test_setup_computes_the_inverter_entity_scope(monkeypatch):
    _patch(monkeypatch)
    choice = ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET")
    monkeypatch.setattr(rt_mod, "_choice_for", lambda hass, entry, profiles: choice)
    monkeypatch.setattr(rt_mod, "map_entities", lambda hass, c: {"mode": "select.gw_mode"})
    hass, entry = _setup_hass(), _entry()
    hass.data["entity_registry"] = SimpleNamespace(entities={e.entity_id: e for e in (
        _reg_entry("select.gw_mode", "goodwe", "ce1", "d1"),
        _reg_entry("sensor.gw_total_load", "goodwe", "ce1", "d1"))})
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    assert rt.inverter_entities == frozenset({"select.gw_mode", "sensor.gw_total_load"})
    await rt_mod.async_unload_control(hass, rt)
