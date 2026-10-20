"""Setup wpisu z kluczem API: prognoza bez zmian względem 1.7.2 + wykrywanie obok.

Listy `unique_id` poniżej są ZAMROŻONE — wygenerowane przebiegiem tej samej
uprzęży (`tests/setup_harness.py`) na kodzie `origin/main` (Release 1.7.2,
354b625). Zmiana którejkolwiek z nich = regresja dla płacących użytkowników.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.volcast.const import DOMAIN
from tests.setup_harness import drain

pytestmark = pytest.mark.asyncio

# Wpis z czujnikiem energii (tracker + reconciler) — przebieg na 1.7.2.
IDS_172_WITH_ENERGY = {f"test_entry_id_{k}" for k in (
    "energy_today", "energy_tomorrow", "power_now", "api_status",
    "energy_day_3", "energy_day_4", "energy_day_5", "energy_day_6", "energy_day_7",
    "submit_queue_depth", "last_reconciliation", "peak_production",
    "integration_healthy", "sync_now")}

# Wpis bez opcji (sama prognoza) — przebieg na 1.7.2.
IDS_172_NO_OPTIONS = {f"test_entry_id_{k}" for k in (
    "energy_today", "energy_tomorrow", "power_now", "api_status",
    "energy_day_3", "energy_day_4", "energy_day_5", "energy_day_6", "energy_day_7",
    "peak_production", "integration_healthy")}

# Diagnostyczny czujnik dodany w 2.0; zbiory IDS_172_* zostają zamrożone (przebieg na 1.7.2).
DIAGNOSTIC_IDS_2_0 = {"test_entry_id_forecast_age"}

DISCOVERY_IDS = {"test_entry_id_discovery", "test_entry_id_run_discovery"}
MODES_DATA = [pytest.param(None, id="forecast"), pytest.param({"mode": "discovery_only"}, id="discovery_only")]


async def test_forecast_entry_unique_ids_identical_to_1_7_2(setup_forecast_entry):
    ids = await setup_forecast_entry(options={"pv_energy_entity": "sensor.pv_today"})
    assert IDS_172_WITH_ENERGY <= ids
    assert ids - IDS_172_WITH_ENERGY == DISCOVERY_IDS | DIAGNOSTIC_IDS_2_0


async def test_forecast_entry_without_options_identical_to_1_7_2(setup_forecast_entry):
    ids = await setup_forecast_entry(options={})
    assert IDS_172_NO_OPTIONS <= ids
    assert ids - IDS_172_NO_OPTIONS == DISCOVERY_IDS | DIAGNOSTIC_IDS_2_0


async def test_forecast_entry_data_unchanged_plus_runner(setup_forecast_entry):
    from custom_components.volcast.discovery_runner import DiscoveryRunner

    await setup_forecast_entry(options={"pv_energy_entity": "sensor.pv_today"})
    entry_data = setup_forecast_entry.hass.data[DOMAIN]["test_entry_id"]
    assert {"coordinator", "tracker", "reconciler"} <= set(entry_data)
    assert isinstance(entry_data["discovery"], DiscoveryRunner)
    assert entry_data["discovery"].entry_id == "test_entry_id"


async def test_discovery_runs_after_setup_and_stores_report(setup_forecast_entry):
    await setup_forecast_entry(options={})
    hass = setup_forecast_entry.hass
    runner = hass.data[DOMAIN]["test_entry_id"]["discovery"]
    assert runner.report is not None and runner.report["schema"] == 1
    assert hass.task_errors == []


async def test_discovery_exception_does_not_break_forecast_setup(setup_forecast_entry, monkeypatch):
    from custom_components.volcast import discovery_runner

    async def boom(self):
        raise RuntimeError("x")

    monkeypatch.setattr(discovery_runner.DiscoveryRunner, "async_run", boom)
    ids = await setup_forecast_entry(options={})
    assert "test_entry_id_energy_today" in ids
    # Wyjątek wykrywania nie wycieka nawet z zadania w tle.
    assert setup_forecast_entry.hass.task_errors == []


async def test_discovery_exception_after_start_event_is_swallowed(setup_forecast_entry, monkeypatch):
    from custom_components.volcast import discovery_runner

    async def boom(self):
        raise RuntimeError("x")

    monkeypatch.setattr(discovery_runner.DiscoveryRunner, "async_run", boom)
    await setup_forecast_entry(options={}, is_running=False)
    hass = setup_forecast_entry.hass
    (event_type, listener), = [l for l in hass.bus.listeners]
    assert event_type == "homeassistant_started"
    await listener(None)  # nie rzuca
    await drain(hass)
    assert hass.task_errors == []


async def test_discovery_is_scheduled_after_platforms_and_never_awaited(
        setup_forecast_entry, monkeypatch):
    from custom_components.volcast import discovery_runner

    release = asyncio.Event()
    calls: list[str] = []

    async def slow_run(self):
        calls.append("run")
        await release.wait()
        return {}

    monkeypatch.setattr(discovery_runner.DiscoveryRunner, "async_run", slow_run)

    from custom_components.volcast import async_setup_entry
    from tests.setup_harness import FakeCoordinator, FakeEntry, SetupHass
    import custom_components.volcast as integ

    monkeypatch.setattr(integ, "VolcastCoordinator", FakeCoordinator)
    hass = SetupHass(is_running=True)
    # Setup kończy się, choć wykrywanie wisi (nie jest awaitowane w setupie).
    ok = await asyncio.wait_for(async_setup_entry(hass, FakeEntry(options={})), 1)
    assert ok is True
    assert hass.events.index("forward") < len(hass.events) - 1
    # zadanie wykrywania powstało PO platformach
    assert hass.events[-1] == "background:volcast_discovery"
    await asyncio.sleep(0)
    assert calls == ["run"]
    release.set()
    await asyncio.gather(*hass.tasks)


async def test_before_start_waits_for_started_event(setup_forecast_entry, monkeypatch):
    from custom_components.volcast import discovery_runner

    run = AsyncMock(return_value={})
    monkeypatch.setattr(discovery_runner.DiscoveryRunner, "async_run", run)
    await setup_forecast_entry(options={}, is_running=False)
    hass = setup_forecast_entry.hass
    assert hass.events == ["forward", "listen:homeassistant_started"]
    run.assert_not_awaited()
    (_, listener), = hass.bus.listeners
    await listener(None)
    await drain(hass)
    run.assert_awaited_once()


async def test_unload_after_start_event_does_not_remove_fired_listener(setup_forecast_entry):
    await setup_forecast_entry(options={}, is_running=False)
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    (_, listener), = hass.bus.listeners
    await listener(None)
    for cb in entry.unload_callbacks:
        cb()
    assert hass.bus.removed == []


async def test_unload_before_start_event_removes_listener(setup_forecast_entry):
    await setup_forecast_entry(options={}, is_running=False)
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    (_, listener), = hass.bus.listeners
    for cb in entry.unload_callbacks:
        cb()
    assert hass.bus.removed == [listener]


async def test_integration_version_from_loader(setup_forecast_entry, monkeypatch):
    import custom_components.volcast.version as integ

    monkeypatch.setattr(integ, "async_get_integration",
                        AsyncMock(return_value=SimpleNamespace(version="1.7.2")), raising=False)
    await setup_forecast_entry(options={})
    runner = setup_forecast_entry.hass.data[DOMAIN]["test_entry_id"]["discovery"]
    assert runner.integration_version == "1.7.2"


async def test_integration_version_unknown_when_loader_fails(setup_forecast_entry, monkeypatch):
    import custom_components.volcast.version as integ

    monkeypatch.setattr(integ, "async_get_integration",
                        AsyncMock(side_effect=RuntimeError("no loader")), raising=False)
    await setup_forecast_entry(options={})
    runner = setup_forecast_entry.hass.data[DOMAIN]["test_entry_id"]["discovery"]
    assert runner.integration_version == "unknown"


async def test_unload_still_cleans_up(setup_forecast_entry):
    from custom_components.volcast import async_unload_entry

    await setup_forecast_entry(options={"pv_energy_entity": "sensor.pv_today"})
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    assert await async_unload_entry(hass, entry) is True
    assert "test_entry_id" not in hass.data[DOMAIN]


# --- platformy tolerują wpis bez koordynatora (tryb tylko-rozpoznanie) ---

class _Entry:
    entry_id = "e1"
    options: dict = {}


def _hass(entry_data):
    return SimpleNamespace(data={DOMAIN: {"e1": entry_data}})


async def _setup_platform(module_name, entry_data):
    import importlib

    module = importlib.import_module(f"custom_components.volcast.{module_name}")
    added: list = []
    await module.async_setup_entry(_hass(entry_data), _Entry(), lambda ents, *a, **k: added.extend(ents))
    return {e._attr_unique_id for e in added}


async def test_platforms_without_coordinator_add_only_discovery_entities():
    runner = MagicMock(report=None)
    entry_data = {"discovery": runner}
    assert await _setup_platform("sensor", entry_data) == {"e1_discovery"}
    assert await _setup_platform("button", entry_data) == {"e1_run_discovery"}
    assert await _setup_platform("binary_sensor", entry_data) == set()


# --- wpis tylko-rozpoznanie (mode=discovery_only) — bez konta, bez prognozy ---

async def test_discovery_only_forwards_only_sensor_and_button(setup_forecast_entry):
    await setup_forecast_entry(data={"mode": "discovery_only"})
    hass = setup_forecast_entry.hass
    assert hass.config_entries.forwarded == ["sensor", "button"]


async def test_discovery_only_entry_data_has_only_discovery_runner(setup_forecast_entry):
    from custom_components.volcast.discovery_runner import DiscoveryRunner

    await setup_forecast_entry(data={"mode": "discovery_only"})
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    entry_data = hass.data[DOMAIN][entry.entry_id]
    # Żadnego koordynatora/trackera/reconcilera — tylko wykrywanie (+ lista platform).
    assert set(entry_data) == {"discovery", "platforms"}
    assert isinstance(entry_data["discovery"], DiscoveryRunner)


async def test_discovery_only_never_creates_coordinator(monkeypatch):
    """Dowód „brak wywołania chmury Volcast": VolcastCoordinator w ogóle nie powstaje."""
    import custom_components.volcast as integ
    from custom_components.volcast import discovery_runner
    from tests.setup_harness import FakeEntry, SetupHass

    monkeypatch.setattr(discovery_runner, "probe_udp_48899", AsyncMock(return_value=None))

    class _BoomCoordinator:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "VolcastCoordinator nie powinien powstać dla wpisu tylko-rozpoznanie"
            )

    monkeypatch.setattr(integ, "VolcastCoordinator", _BoomCoordinator)

    hass = SetupHass(is_running=True)
    entry = FakeEntry(data={"mode": "discovery_only"})
    assert await integ.async_setup_entry(hass, entry) is True


async def test_discovery_only_creates_no_production_repair_issue(setup_forecast_entry, monkeypatch):
    import custom_components.volcast as integ

    spy = MagicMock()
    monkeypatch.setattr(integ.ir, "async_create_issue", spy)
    await setup_forecast_entry(data={"mode": "discovery_only"})
    spy.assert_not_called()


async def test_discovery_only_unload_uses_discovery_only_platforms(setup_forecast_entry, monkeypatch):
    from custom_components.volcast import DISCOVERY_ONLY_PLATFORMS, async_unload_entry

    await setup_forecast_entry(data={"mode": "discovery_only"})
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry

    captured: dict = {}
    orig_unload = hass.config_entries.async_unload_platforms

    async def _spy(entry_, platforms):
        captured["platforms"] = platforms
        return await orig_unload(entry_, platforms)

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", _spy)

    assert await async_unload_entry(hass, entry) is True
    assert captured["platforms"] == DISCOVERY_ONLY_PLATFORMS
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_discovery_entry_upgraded_in_place_unloads_only_loaded_platforms(setup_forecast_entry):
    """Parowanie zamienia wpis „tylko rozpoznanie" w wpis konta PRZED przeładowaniem —
    unload musi zdjąć platformy faktycznie załadowane, nie wyliczone z nowych danych
    (HA: „Config entry was never loaded!" → wpis martwy do restartu)."""
    from custom_components.volcast import async_unload_entry

    await setup_forecast_entry(data={"mode": "discovery_only"})
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    forwarded = list(hass.config_entries.forwarded)
    entry.data = {"api_key": "vk_" + "b" * 64, "api_url": "https://x.example/f"}

    assert await async_unload_entry(hass, entry) is True
    assert hass.config_entries.unloaded == forwarded == ["sensor", "button"]


@pytest.mark.parametrize("data", MODES_DATA)
async def test_unload_uses_platforms_recorded_at_setup(setup_forecast_entry, data):
    from custom_components.volcast import async_unload_entry

    await setup_forecast_entry(data=data)
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    assert await async_unload_entry(hass, entry) is True
    assert hass.config_entries.unloaded == hass.config_entries.forwarded


async def test_unload_without_record_falls_back_to_entry_mode(setup_forecast_entry):
    from custom_components.volcast import async_unload_entry

    await setup_forecast_entry(data={"mode": "discovery_only"})
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    del hass.data[DOMAIN][entry.entry_id]["platforms"]
    assert await async_unload_entry(hass, entry) is True
    assert hass.config_entries.unloaded == ["sensor", "button"]


# --- unique_id wpisu konta: skrót klucza, nigdy jawny klucz ---

async def test_legacy_raw_key_unique_id_migrated_to_hash(setup_forecast_entry):
    from custom_components.volcast.key_format import account_unique_id
    from tests.setup_harness import API_KEY

    await setup_forecast_entry(options={}, unique_id=API_KEY)
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    assert hass.config_entries.updates == [{"unique_id": account_unique_id(API_KEY)}]
    assert entry.unique_id == account_unique_id(API_KEY)
    # Migracja przed rejestracją listenera — zmiana unique_id nie przeładowuje wpisu.
    assert hass.config_entries.updates and len(entry.update_listeners) == 1


@pytest.mark.parametrize("data,unique_id", [
    ({}, None), ({}, "account_" + "0" * 64), ({"mode": "discovery_only"}, "discovery_only")])
async def test_unique_id_left_alone_when_not_a_raw_key(setup_forecast_entry, data, unique_id):
    await setup_forecast_entry(data=data or None, options={}, unique_id=unique_id)
    assert setup_forecast_entry.hass.config_entries.updates == []


async def test_unique_id_migration_failure_does_not_block_forecast(setup_forecast_entry, monkeypatch):
    from tests.setup_harness import API_KEY, _ConfigEntries

    def boom(self, entry, **changes):
        raise ValueError("collision")
    monkeypatch.setattr(_ConfigEntries, "async_update_entry", boom)
    ids = await setup_forecast_entry(options={}, unique_id=API_KEY)
    assert IDS_172_NO_OPTIONS <= ids


async def test_discovery_only_unload_does_not_touch_unregistered_service(setup_forecast_entry):
    """Serwis sync_production nigdy nie jest rejestrowany dla wpisu tylko-rozpoznanie —
    unload ostatniego takiego wpisu nie wolno próbować go usunąć."""
    await setup_forecast_entry(data={"mode": "discovery_only"})
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry

    from custom_components.volcast import async_unload_entry
    from custom_components.volcast.const import SERVICE_SYNC_PRODUCTION

    assert not hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)
    assert await async_unload_entry(hass, entry) is True
    assert not hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)


# --- wykrywanie jako zadanie w tle wpisu (anulowane przy unload/reload) ---

MODES = [pytest.param({}, id="forecast"), pytest.param({"mode": "discovery_only"}, id="discovery_only")]


def _setup_kwargs(data):
    return {"options": {}} if not data else {"data": data}


@pytest.mark.parametrize("data", MODES)
async def test_discovery_scheduled_as_entry_background_task_when_running(
        setup_forecast_entry, data):
    await setup_forecast_entry(**_setup_kwargs(data))
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    assert hass.events.count("background:volcast_discovery") == 1
    assert "task" not in hass.events
    assert hass.data[DOMAIN][entry.entry_id]["discovery"].report is not None
    assert hass.task_errors == []


@pytest.mark.parametrize("data", MODES)
async def test_discovery_scheduled_as_entry_background_task_after_start(
        setup_forecast_entry, data):
    await setup_forecast_entry(**_setup_kwargs(data), is_running=False)
    hass, entry = setup_forecast_entry.hass, setup_forecast_entry.entry
    assert "background:volcast_discovery" not in hass.events
    (event_type, listener), = hass.bus.listeners
    assert event_type == "homeassistant_started"
    await listener(None)
    assert hass.events.count("background:volcast_discovery") == 1
    assert "task" not in hass.events
    await drain(hass)
    assert hass.data[DOMAIN][entry.entry_id]["discovery"].report is not None
    assert hass.task_errors == []


# --- źródło produkcji: jedno na konto — telemetria sterowania albo zmapowane czujniki ---

_BASE = "https://s.example.test"
_BACKEND = {"base_url": _BASE, **{k: f"{_BASE}/functions/v1/{k}" for k in (
    "forecast", "submit_production", "telemetry", "schedule", "history_import", "pairing")}}
_PAIRED = {"api_key": "vk_" + "a" * 64, "api_url": _BACKEND["forecast"], "backend": _BACKEND,
           "user_id": "u1"}
MAPPING = {"pv_energy_entity": "sensor.ext_energy", "pv_power_entity": "sensor.ext_power"}
DIRECT = {"control_mode": "direct"}
DISABLED_LOG = "Production tracking disabled: inverter control reports production through telemetry"


class _RecTracker:
    instances: list = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self._queue: list = []
        _RecTracker.instances.append(self)

    async def async_start(self) -> None:
        return None

    async def async_stop(self) -> None:
        return None


def _direct_conn():
    """Połączenie bezpośrednie w minimalnym kształcie, którego potrzebuje platforma czujników."""
    return SimpleNamespace(profile=SimpleNamespace(raw={"read": {}}, modes=()),
                           stats=SimpleNamespace(timeouts=0, requests=0),
                           add_listener=lambda _cb: (lambda: None))


def _gw(domain="goodwe"):
    from custom_components.volcast.core.control.select import ProfileChoice
    from custom_components.volcast.core.profile import load_builtin
    return ProfileChoice(load_builtin("goodwe-et"), domain, "GW8KN-ET")


def _runtime(*, choice=None, direct=None, profile_map=None, manual=None):
    """Runtime sterowania z prawdziwym nadajnikiem telemetrii — to on mówi, czy niesie produkcję PV."""
    from custom_components.volcast.control import runtime as rt_mod
    from custom_components.volcast.control.telemetry import TelemetrySender

    class _Exec:
        raw_plan, schedule, consent, local_switch, paused = None, None, None, False, False
        last_decision = tou_preview = None

    telemetry = TelemetrySender(SimpleNamespace(), SimpleNamespace(entry_id="e1", options={}), None, None,
                                choice=choice, profile_map=profile_map or {}, manual_map=manual or {},
                                grid_negate=False, limits=None, direct=direct)
    return rt_mod.ControlRuntime(executor=_Exec(), fetcher=None, telemetry=telemetry, cloud=None,
                                 choice=choice, mapped=dict(profile_map or {}), rated_power_w=None,
                                 direct=direct)


async def _run_production(monkeypatch, *, options, data=None, control=None):
    """run_setup z nagrywającym trackerem; `control` — wynik (albo wyjątek) `async_setup_control`."""
    import custom_components.volcast as integ
    from tests import setup_harness as sh

    _RecTracker.instances = []
    reconciler_energy: list[str] = []

    class _Rec(sh.FakeReconciler):
        def __init__(self, **kw):
            super().__init__(**kw)
            reconciler_energy.append(kw["energy_entity"])

    async def setup_control(hass, entry, *, report):
        if isinstance(control, BaseException):
            raise control
        return control
    monkeypatch.setattr(integ, "async_setup_control", setup_control)

    def setattr_(obj, name, value):
        if name == "VolcastProductionTracker":
            value = _RecTracker
        elif name == "DailyReconciler":
            value = _Rec
        monkeypatch.setattr(obj, name, value)

    issues: list[str] = []
    monkeypatch.setattr(integ.ir, "async_create_issue", lambda *a, **k: issues.append(a[2]))
    hass, entry, ok = await sh.run_setup(setattr_, options=options, data=data)
    assert ok is True
    tracker = _RecTracker.instances[0] if _RecTracker.instances else None
    assert hass.data[DOMAIN][entry.entry_id]["tracker"] is tracker
    return hass, entry, tracker, reconciler_energy, issues


def _disabled_logs(caplog) -> list:
    import logging
    return [r for r in caplog.records if r.levelno == logging.INFO and r.getMessage() == DISABLED_LOG]


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch):
    from custom_components.volcast import discovery_runner
    monkeypatch.setattr(discovery_runner, "probe_udp_48899", AsyncMock(return_value=None))


async def test_mapping_without_control_keeps_tracker_and_reconciler(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    hass, entry, tracker, rec, issues = await _run_production(monkeypatch, options=MAPPING)
    assert tracker.kw["energy_entity"] == "sensor.ext_energy"
    assert tracker.kw["power_entity"] == "sensor.ext_power"
    assert rec == ["sensor.ext_energy"]
    assert hass.data[DOMAIN][entry.entry_id]["reconciler"] is not None
    assert issues == [] and _disabled_logs(caplog) == []


async def test_entry_without_mapping_or_control_keeps_repair_issue(monkeypatch):
    hass, entry, tracker, rec, issues = await _run_production(monkeypatch, options={})
    assert issues == ["production_tracking_available"]
    assert tracker is None and rec == []
    assert hass.data[DOMAIN][entry.entry_id].get("reconciler") is None


async def test_control_with_inverter_telemetry_drops_the_mapped_tracker(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options=MAPPING, data=_PAIRED,
        control=_runtime(choice=_gw(), profile_map={"pv_power_w": "sensor.gw_pv_power"}))
    assert tracker is None and rec == []
    assert hass.data[DOMAIN][entry.entry_id].get("reconciler") is None
    assert issues == ["production_mapping_unused_test_entry_id"]
    assert len(_disabled_logs(caplog)) == 1


async def test_direct_connection_drops_the_mapped_tracker(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options={**DIRECT, **MAPPING}, data=_PAIRED, control=_runtime(choice=_gw(), direct=_direct_conn()))
    assert tracker is None and rec == [] and issues == ["production_mapping_unused_test_entry_id"]
    assert len(_disabled_logs(caplog)) == 1


async def test_direct_connection_without_mapping_needs_no_tracker_nor_repair(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options=DIRECT, data=_PAIRED, control=_runtime(choice=_gw(), direct=_direct_conn()))
    assert tracker is None and rec == [] and issues == []
    # Nic nie zmapowano — nie ma czego "wyłączać", więc bez komunikatu.
    assert _disabled_logs(caplog) == []


async def test_failed_control_setup_falls_back_to_the_mapped_tracker(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options=MAPPING, data=_PAIRED, control=RuntimeError("x"))
    assert tracker is not None and tracker.kw["energy_entity"] == "sensor.ext_energy"
    assert rec == ["sensor.ext_energy"] and issues == []
    assert _disabled_logs(caplog) == []


async def test_direct_mode_without_runtime_or_mapping_raises_repair_like_forecast_only(monkeypatch):
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options=DIRECT, data=_PAIRED, control=None)
    assert issues == ["production_tracking_available"]
    assert tracker is None and rec == []


async def test_control_without_inverter_keeps_the_mapped_tracker(monkeypatch, caplog):
    """Plan bez wykrytego falownika (choice i direct puste): telemetria nie niesie produkcji."""
    import logging
    caplog.set_level(logging.INFO)
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options=MAPPING, data=_PAIRED, control=_runtime())
    assert tracker is not None and tracker.kw["energy_entity"] == "sensor.ext_energy"
    assert rec == ["sensor.ext_energy"]
    assert hass.data[DOMAIN][entry.entry_id]["reconciler"] is not None
    assert issues == [] and _disabled_logs(caplog) == []


async def test_control_without_inverter_or_mapping_keeps_repair_issue(monkeypatch):
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options={}, data=_PAIRED, control=_runtime())
    assert issues == ["production_tracking_available"]
    assert tracker is None and rec == []


async def test_read_only_profile_keeps_the_mapped_tracker(monkeypatch, caplog):
    """Falownik rozpoznany tylko do odczytu (bez integracji): telemetria nie niesie produkcji PV."""
    import logging
    caplog.set_level(logging.INFO)
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options=MAPPING, data=_PAIRED, control=_runtime(choice=_gw(domain=None)))
    assert tracker is not None and rec == ["sensor.ext_energy"]
    assert issues == [] and _disabled_logs(caplog) == []


async def test_read_only_profile_without_mapping_raises_repair(monkeypatch):
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options={}, data=_PAIRED, control=_runtime(choice=_gw(domain=None)))
    assert issues == ["production_tracking_available"]
    assert tracker is None and rec == []


async def test_entity_mode_without_mapped_pv_entities_keeps_the_mapped_tracker(monkeypatch):
    """Profil opisuje encje PV, ale żadnej nie znaleziono w HA — liczy się to, co faktycznie zmapowane."""
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options=MAPPING, data=_PAIRED,
        control=_runtime(choice=_gw(), profile_map={"soc": "sensor.gw_soc"}))
    assert tracker is not None and issues == []


async def test_manual_telemetry_pv_power_drops_the_mapped_tracker(monkeypatch, caplog):
    """Bez falownika, ale ręczna mapa telemetrii ma moc PV — produkcja płynie telemetrią."""
    import logging
    caplog.set_level(logging.INFO)
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options=MAPPING, data=_PAIRED,
        control=_runtime(manual={"pv_power_w": "sensor.pv_power"}))
    assert tracker is None and rec == []
    assert issues == ["production_mapping_unused_test_entry_id"]
    assert len(_disabled_logs(caplog)) == 1


async def test_pv_energy_counter_alone_keeps_the_mapped_tracker(monkeypatch, caplog):
    """Sam licznik energii PV w telemetrii (bez mocy PV) — serwer nie wyliczy z niego produkcji."""
    import logging
    caplog.set_level(logging.INFO)
    hass, entry, tracker, rec, issues = await _run_production(
        monkeypatch, options=MAPPING, data=_PAIRED,
        control=_runtime(manual={"pv_energy_total_kwh": "sensor.pv_total"}))
    assert tracker is not None and rec == ["sensor.ext_energy"]
    assert issues == [] and _disabled_logs(caplog) == []


# --- nieudany setup po złożeniu sterowania: nic nie zostaje uruchomione ---

class _StopTracker(_RecTracker):
    start_error: BaseException | None = None
    stop_error: BaseException | None = None

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.stopped = 0

    async def async_start(self) -> None:
        if _StopTracker.start_error is not None:
            raise _StopTracker.start_error

    async def async_stop(self) -> None:
        self.stopped += 1
        if _StopTracker.stop_error is not None:
            raise _StopTracker.stop_error


async def _run_failing_setup(monkeypatch, *, tracker_error=None, reconciler_error=None,
                             forward_error=None, stop_error=None, control=None):
    """Setup sparowanego wpisu z mapowaniem, w którym zawodzi tracker, reconciler albo forward.

    `hass.open_issues` — zgłoszenia naprawy, które zostały po nieudanym setupie.
    """
    import custom_components.volcast as integ
    from tests import setup_harness as sh

    _RecTracker.instances = []
    _StopTracker.start_error = tracker_error
    _StopTracker.stop_error = stop_error
    if control is None:
        control = _runtime()        # bez falownika — tracker z mapowania powstaje
    unloaded: list = []
    open_issues: set[str] = set()
    monkeypatch.setattr(integ.ir, "async_create_issue", lambda *a, **k: open_issues.add(a[2]))
    monkeypatch.setattr(integ.ir, "async_delete_issue", lambda *a, **k: open_issues.discard(a[2]))

    async def setup_control(hass, entry, *, report):
        return control

    async def unload_control(hass, rt, **_kw):
        unloaded.append(rt)

    class _Rec(sh.FakeReconciler):
        def __init__(self, **kw):
            if reconciler_error is not None:
                raise reconciler_error
            super().__init__(**kw)

    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_unload_control", unload_control)
    monkeypatch.setattr(integ, "VolcastCoordinator", sh.FakeCoordinator)
    monkeypatch.setattr(integ, "VolcastProductionTracker", _StopTracker)
    monkeypatch.setattr(integ, "DailyReconciler", _Rec)
    hass = sh.SetupHass()
    if forward_error is not None:
        async def forward(entry, platforms):
            raise forward_error
        monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    hass.open_issues = open_issues
    entry = sh.FakeEntry(data=_PAIRED, options=MAPPING)
    with pytest.raises(RuntimeError):
        await integ.async_setup_entry(hass, entry)
    tracker = _RecTracker.instances[0] if _RecTracker.instances else None
    return hass, entry, tracker, control, unloaded


async def test_tracker_start_failure_unloads_control_and_stops_the_tracker(monkeypatch):
    hass, entry, tracker, control, unloaded = await _run_failing_setup(
        monkeypatch, tracker_error=RuntimeError("tracker"))
    assert unloaded == [control]
    assert tracker is not None and tracker.stopped == 1
    entry_data = hass.data[DOMAIN][entry.entry_id]
    assert entry_data["control"] is None and entry_data["tracker"] is None


async def test_reconciler_start_failure_unloads_control_and_stops_the_tracker(monkeypatch):
    hass, entry, tracker, control, unloaded = await _run_failing_setup(
        monkeypatch, reconciler_error=RuntimeError("reconciler"))
    assert unloaded == [control]
    assert tracker.stopped == 1
    entry_data = hass.data[DOMAIN][entry.entry_id]
    assert entry_data["tracker"] is None and entry_data.get("reconciler") is None


async def test_platform_forward_failure_stops_the_started_tracker(monkeypatch):
    hass, entry, tracker, control, unloaded = await _run_failing_setup(
        monkeypatch, forward_error=RuntimeError("platforms"))
    assert unloaded == [control]
    assert tracker.stopped == 1
    entry_data = hass.data[DOMAIN][entry.entry_id]
    assert entry_data["tracker"] is None and entry_data.get("reconciler") is None


async def test_failed_setup_leaves_no_mapping_unused_issue(monkeypatch):
    """Zgłoszenie „czujniki nieużywane” powstaje przed platformami — nieudany setup je zdejmuje."""
    control = _runtime(manual={"pv_power_w": "sensor.pv_power"})      # telemetria niesie moc PV
    hass, entry, tracker, control, unloaded = await _run_failing_setup(
        monkeypatch, forward_error=RuntimeError("platforms"), control=control)
    assert unloaded == [control]
    assert not any(i.startswith("production_mapping_unused_") for i in hass.open_issues)


async def test_cancelled_tracker_stop_still_unloads_control_and_keeps_the_original_error(monkeypatch):
    import asyncio
    hass, entry, tracker, control, unloaded = await _run_failing_setup(
        monkeypatch, forward_error=RuntimeError("platforms"), stop_error=asyncio.CancelledError())
    assert tracker.stopped == 1
    assert unloaded == [control]


async def test_remove_entry_deletes_the_mapping_unused_issue(monkeypatch):
    import custom_components.volcast as integ
    deleted: list[str] = []
    monkeypatch.setattr(integ, "async_remove_control", AsyncMock())
    monkeypatch.setattr(integ.ir, "async_delete_issue", lambda *a, **k: deleted.append(a[2]))
    await integ.async_remove_entry(SimpleNamespace(), SimpleNamespace(entry_id="e1"))
    assert "production_mapping_unused_e1" in deleted


# --- stare zgłoszenie naprawy znika, gdy pojawia się źródło produkcji ---

async def _deleted_issues(monkeypatch, **kw) -> list[str]:
    import custom_components.volcast as integ
    deleted: list[str] = []
    monkeypatch.setattr(integ.ir, "async_delete_issue", lambda *a, **k: deleted.append(a[2]))
    await _run_production(monkeypatch, **kw)
    return deleted


async def test_stale_repair_deleted_when_sensors_are_mapped(monkeypatch):
    assert "production_tracking_available" in await _deleted_issues(monkeypatch, options=MAPPING)


async def test_stale_repair_deleted_when_telemetry_carries_production(monkeypatch):
    deleted = await _deleted_issues(monkeypatch, options=DIRECT, data=_PAIRED,
                                    control=_runtime(choice=_gw(), direct=_direct_conn()))
    assert "production_tracking_available" in deleted


async def test_repair_not_deleted_without_any_source(monkeypatch):
    assert "production_tracking_available" not in await _deleted_issues(monkeypatch, options={})
