"""Uprząż testowa: pełny `async_setup_entry` integracji na atrapach HA.

Przepuszcza wpis przez prawdziwy `async_setup_entry` (koordynator, tracker
i reconciler podmienione na atrapy bez sieci), prawdziwe `async_forward_entry_setups`
do platform `sensor`/`binary_sensor`/`button` i zbiera dodane encje. Zadania
tworzone przez integrację (`hass.async_create_task`) naprawdę się wykonują —
`run_setup` czeka na nie i odkłada ich wyjątki w `hass.task_errors`.

Plik nie zależy od kodu wykrywania, więc ten sam przebieg da się puścić na
starszej gałęzi (np. `main` 1.7.2) i porównać listę `unique_id`.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from typing import Any, Callable

ENTRY_ID = "test_entry_id"
API_KEY = "vk_" + "a" * 64


class FakeEntry:
    """Atrapa ConfigEntry z rejestrem callbacków unload."""

    def __init__(self, *, data: dict | None = None, options: dict | None = None,
                 entry_id: str = ENTRY_ID) -> None:
        self.entry_id = entry_id
        self.data = dict(data if data is not None else {"api_key": API_KEY})
        self.options = dict(options or {})
        self.version = 1
        self.title = "Volcast"
        self.unload_callbacks: list[Callable[[], Any]] = []
        self.update_listeners: list[Any] = []

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)
        return lambda: None

    def async_on_unload(self, func) -> None:
        self.unload_callbacks.append(func)


class _Bus:
    def __init__(self, hass: "SetupHass") -> None:
        self._hass = hass
        self.listeners: list[tuple[str, Any]] = []
        self.removed: list[Any] = []

    def async_listen_once(self, event_type, listener):
        self._hass.events.append(f"listen:{event_type}")
        self.listeners.append((event_type, listener))
        return lambda: self.removed.append(listener)


class _Services:
    def __init__(self) -> None:
        self.registered: dict[tuple[str, str], Any] = {}

    def has_service(self, domain, service) -> bool:
        return (domain, service) in self.registered

    def async_register(self, domain, service, handler, schema=None) -> None:
        self.registered[(domain, service)] = handler

    def async_remove(self, domain, service) -> None:
        self.registered.pop((domain, service), None)


def _platform_name(platform: Any) -> str:
    """Nazwa modułu platformy — działa i dla prawdziwego `Platform`, i dla atrapy."""
    if isinstance(platform, str):
        return str(platform)
    stub = sys.modules["homeassistant.const"].Platform
    names = {
        id(stub.SENSOR): "sensor",
        id(stub.BINARY_SENSOR): "binary_sensor",
        id(stub.BUTTON): "button",
    }
    return names[id(platform)]


class _ConfigEntries:
    def __init__(self, hass: "SetupHass") -> None:
        self._hass = hass
        self.forwarded: list[str] = []

    def async_entries(self, domain: str | None = None):
        return []

    async def async_forward_entry_setups(self, entry, platforms) -> None:
        self._hass.events.append("forward")
        for platform in platforms:
            name = _platform_name(platform)
            self.forwarded.append(name)
            module = importlib.import_module(f"custom_components.volcast.{name}")

            def _add(entities, *_args, **_kwargs):
                self._hass.entities.extend(entities)

            await module.async_setup_entry(self._hass, entry, _add)

    async def async_unload_platforms(self, entry, platforms) -> bool:
        return True

    async def async_reload(self, entry_id) -> None:
        return None


class SetupHass:
    """Atrapa hass wystarczająca dla `async_setup_entry` i przebiegu wykrywania."""

    def __init__(self, *, is_running: bool = True) -> None:
        self.data: dict = {
            "device_registry": types.SimpleNamespace(devices={}),
            "entity_registry": types.SimpleNamespace(entities={}),
        }
        self.config = types.SimpleNamespace(time_zone="Europe/Warsaw", components=set())
        self.states = types.SimpleNamespace(get=lambda _eid: None)
        self.is_running = is_running
        self.events: list[str] = []
        self.entities: list[Any] = []
        self.tasks: list[asyncio.Task] = []
        self.task_errors: list[BaseException] = []
        self.bus = _Bus(self)
        self.services = _Services()
        self.config_entries = _ConfigEntries(self)

    def async_create_task(self, coro, *_args, **_kwargs):
        self.events.append("task")
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task


class FakeCoordinator:
    def __init__(self, hass, api_key, api_url, update_interval, entry_id=None, **_kw):
        self.hass = hass
        self.entry_id = entry_id
        self.data = types.SimpleNamespace(submit_url="", system_capacity_kwp=6.5)
        self.last_update_success = True

    async def async_load_forecast_history(self) -> None:
        return None

    async def async_config_entry_first_refresh(self) -> None:
        return None


class FakeTracker:
    def __init__(self, **_kw) -> None:
        self._queue: list = []

    async def async_start(self) -> None:
        return None

    async def async_stop(self) -> None:
        return None


class FakeReconciler:
    def __init__(self, **_kw) -> None:
        from zoneinfo import ZoneInfo

        self._tz = ZoneInfo("Europe/Warsaw")
        self._last_result = None

    async def reconcile_recent(self):
        return []

    async def reconcile_day(self, _day):
        return None


def unique_ids(entities) -> set[str]:
    return {getattr(e, "_attr_unique_id", None) for e in entities}


async def run_setup(setattr_: Callable[[Any, str, Any], None], *,
                    options: dict | None = None, data: dict | None = None,
                    is_running: bool = True):
    """Uruchom `async_setup_entry`, dokończ utworzone zadania; zwróć (hass, entry, ok)."""
    integ = importlib.import_module("custom_components.volcast")
    setattr_(integ, "VolcastCoordinator", FakeCoordinator)
    setattr_(integ, "VolcastProductionTracker", FakeTracker)
    setattr_(integ, "DailyReconciler", FakeReconciler)

    hass = SetupHass(is_running=is_running)
    entry = FakeEntry(data=data, options=options)
    ok = await integ.async_setup_entry(hass, entry)
    await drain(hass)
    return hass, entry, ok


async def drain(hass: SetupHass) -> None:
    """Poczekaj na wszystkie zadania utworzone przez integrację (także zagnieżdżone)."""
    done: set[int] = set()
    while True:
        pending = [t for t in hass.tasks if id(t) not in done]
        if not pending:
            return
        results = await asyncio.gather(*pending, return_exceptions=True)
        for task, result in zip(pending, results):
            done.add(id(task))
            if isinstance(result, BaseException):
                hass.task_errors.append(result)
