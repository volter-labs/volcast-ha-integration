import asyncio
from types import SimpleNamespace

import pytest

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


# ── rozładunek i słuchacz aktualizacji ─────────────────────────────────────


@pytest.mark.asyncio
async def test_unload_removes_exactly_the_forwarded_platforms_and_stops_control(monkeypatch):
    import custom_components.volcast as integ
    stopped = []

    async def setup_control(hass, entry, *, report):
        return fake_runtime()

    async def unload_control(hass, rt):
        stopped.append(rt)
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    monkeypatch.setattr(integ, "async_unload_control", unload_control)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert await integ.async_unload_entry(hass, entry)
    assert hass.config_entries.unloaded == hass.config_entries.forwarded and "switch" in hass.config_entries.unloaded
    assert len(stopped) == 1


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
