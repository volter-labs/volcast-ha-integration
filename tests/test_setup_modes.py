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

DISCOVERY_IDS = {"test_entry_id_discovery", "test_entry_id_run_discovery"}
MODES_DATA = [pytest.param(None, id="forecast"), pytest.param({"mode": "discovery_only"}, id="discovery_only")]


async def test_forecast_entry_unique_ids_identical_to_1_7_2(setup_forecast_entry):
    ids = await setup_forecast_entry(options={"pv_energy_entity": "sensor.pv_today"})
    assert IDS_172_WITH_ENERGY <= ids
    assert ids - IDS_172_WITH_ENERGY == DISCOVERY_IDS


async def test_forecast_entry_without_options_identical_to_1_7_2(setup_forecast_entry):
    ids = await setup_forecast_entry(options={})
    assert IDS_172_NO_OPTIONS <= ids
    assert ids - IDS_172_NO_OPTIONS == DISCOVERY_IDS


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
