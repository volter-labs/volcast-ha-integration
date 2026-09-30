"""Wykrywanie instalacji — warstwa Home Assistant (tylko odczyt).

Zbiera migawki z rejestrów HA (urządzenia, encje, wpisy konfiguracji), stany
sklasyfikowanych encji i liczbę dni statystyk z rekordera, uruchamia czysty pakiet
`core.discovery` i publikuje raport sygnałem dispatchera. Niczego nie zapisuje
i niczym nie steruje. Każdy krok ma własny `try` — awaria kroku trafia do
`errors` raportu, nigdy nie przerywa przebiegu ani nie wychodzi poza `async_run`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DISCOVERY_TIMEOUT_S, SIGNAL_DISCOVERY_UPDATED
from .core.discovery import (Classification, ConfigEntrySnap, DeviceSnap,
                             EntitySnap, StateSnap, classify)
from .core.discovery.history import HISTORY_WINDOW_DAYS, days_with_statistics
from .core.discovery.known import HOST_KEYS
from .core.discovery.network import NetworkProbeResult, probe_udp_48899
from .registry_compat import all_devices
from .core.discovery.report import ERROR_TIMEOUT, REPORT_SCHEMA, build_report

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


def _err(step: str, err: BaseException) -> str:
    return f"{step}: {type(err).__name__}: {err}"


def _identifiers(raw: Any) -> tuple[tuple[str, str], ...]:
    """Identyfikatory urządzenia jako pary (domena, wartość).

    Stare integracje potrafią zapisać krotki dłuższe niż 2 — każdy element po domenie
    staje się osobną parą (domena, wartość). Serial bywa na dowolnej pozycji, więc każda
    wartość trafia do zbioru kandydatów maskowania (także w entity_id/unique_id), a raport
    (rozpakowujący pary) się nie wywraca.
    """
    out: set[tuple[str, str]] = set()
    for ident in raw or ():
        parts = [str(p) for p in ident] if isinstance(ident, (tuple, list)) else [str(ident)]
        out.update((parts[0], value) for value in parts[1:])
    return tuple(sorted(out))


def _config_entry_ids(device: Any) -> tuple[str, ...]:
    """Wpisy urządzenia w stałej kolejności; wpis główny (jeśli HA go zna) pierwszy —
    klasyfikacja bierze pierwszy wpis jako klucz znaleziska."""
    ids = sorted(str(x) for x in (getattr(device, "config_entries", None) or ()))
    primary = getattr(device, "primary_config_entry", None)
    if primary in ids:
        ids.remove(primary)
        ids.insert(0, primary)
    return tuple(ids)


def _host(data: Any) -> str | None:
    """Pierwsza tekstowa wartość z kluczy HOST_KEYS. Nic innego z `data` nie czytamy
    (hasła, klucze API, loginy zostają w HA)."""
    if not hasattr(data, "get"):
        return None
    for key in HOST_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _capabilities(entry: Any) -> dict[str, Any] | None:
    """Kopia capabilities wpisu rejestru encji (w HA to mapowanie tylko do odczytu)."""
    raw = getattr(entry, "capabilities", None)
    return dict(raw) if raw else None


class DiscoveryRunner:
    """Jeden przebieg wykrywania na żądanie; przechowuje ostatni raport."""

    def __init__(self, hass, entry_id: str, integration_version: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.integration_version = integration_version
        self.report: dict | None = None
        # Ostatnia klasyfikacja — tylko dla kroków opcji w tym procesie, nie trafia do raportu.
        self.classification: Classification | None = None
        self.running: bool = False
        self._inflight: asyncio.Future[dict] | None = None

    async def async_run(self) -> dict:
        """Uruchom wykrywanie. Nigdy nie rzuca; zawsze zwraca raport.

        Wywołanie w trakcie trwającego przebiegu nie startuje drugiego: zwraca
        bieżący (poprzedni) raport, a gdy go jeszcze nie ma — czeka na wynik
        trwającego przebiegu.
        """
        if self.running:
            if self.report is not None:
                return self.report
            if self._inflight is not None:
                return await asyncio.shield(self._inflight)
        self.running = True
        inflight: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._inflight = inflight
        try:
            report = await self._run_bounded()
            self.report = report
            inflight.set_result(report)
            try:
                async_dispatcher_send(
                    self.hass, SIGNAL_DISCOVERY_UPDATED.format(entry_id=self.entry_id))
            except Exception:  # noqa: BLE001 — publikacja nie może zepsuć przebiegu
                _LOGGER.exception("Volcast discovery: dispatcher signal failed")
            return report
        finally:
            if not inflight.done():
                inflight.cancel()
            self.running = False
            self._inflight = None

    async def _run_bounded(self) -> dict:
        # DISCOVERY_TIMEOUT_S czytany jako globalna modułu w chwili wywołania.
        try:
            return await asyncio.wait_for(self._collect(), DISCOVERY_TIMEOUT_S)
        except TimeoutError:
            _LOGGER.warning("Volcast discovery timed out after %ss", DISCOVERY_TIMEOUT_S)
            return self._empty_report([ERROR_TIMEOUT])
        except Exception as err:  # noqa: BLE001 — async_run nigdy nie rzuca
            _LOGGER.exception("Volcast discovery failed")
            return self._empty_report([_err("runner", err)])

    def _empty_report(self, errors: list[str]) -> dict:
        generated_at = dt_util.utcnow().isoformat()
        try:
            return build_report(
                classification=Classification([], [], []), states={}, history_days={},
                network=None, errors=errors,
                integration_version=self.integration_version,
                ha_version=HA_VERSION, generated_at=generated_at)
        except Exception:  # noqa: BLE001 — ostatnia deska: bez treści wyjątków
            _LOGGER.exception("Volcast discovery: fallback report failed")
            return {
                "schema": REPORT_SCHEMA, "generated_at": generated_at,
                "integration_version": self.integration_version,
                "ha_version": HA_VERSION, "inverters": [], "price_entities": [],
                "energy_sensors": [], "network": {"udp_48899": None},
                "errors": ["runner: report build failed"],
            }

    @staticmethod
    def _step(name: str, errors: list[str], fn: Callable[[], _T], default: _T) -> _T:
        try:
            return fn()
        except Exception as err:  # noqa: BLE001 — błąd kroku → errors
            errors.append(_err(name, err))
            return default

    async def _collect(self) -> dict:
        errors: list[str] = []
        devices, skipped_devices = self._step(
            "devices", errors, self._snap_devices, ([], frozenset()))
        entities = self._step(
            "entities", errors, lambda: self._snap_entities(skipped_devices), [])
        entries = self._step("config_entries", errors, self._snap_entries, [])
        classification = self._step(
            "classify", errors, lambda: classify(devices, entities, entries, {}),
            Classification([], [], []))
        self.classification = classification
        states = self._step(
            "states", errors, lambda: self._snap_states(classification), {})
        history_days = await self._history_days(classification, errors)
        network: NetworkProbeResult | None
        try:
            network = await probe_udp_48899()
        except Exception as err:  # noqa: BLE001 — sonda z założenia nie rzuca, ale na wszelki wypadek
            errors.append(_err("network", err))
            network = None
        return build_report(
            classification=classification, states=states, history_days=history_days,
            network=network, errors=errors,
            integration_version=self.integration_version, ha_version=HA_VERSION,
            generated_at=dt_util.utcnow().isoformat(), devices=devices)

    def _snap_devices(self) -> tuple[list[DeviceSnap], frozenset[str]]:
        """Migawki aktywnych urządzeń i identyfikatory pominiętych (wyłączonych).

        Wyłączone urządzenia (także te po wyłączonym wpisie, disabled_by=config_entry)
        nie są działającą instalacją — bez nich nie wracają ścieżką producenta. Ich
        identyfikatory zwracamy, bo encje takich urządzeń też trzeba pominąć.
        """
        devices = all_devices(dr.async_get(self.hass))
        active = [d for d in devices if not getattr(d, "disabled_by", None)]
        skipped = frozenset(d.id for d in devices if getattr(d, "disabled_by", None))
        return [
            DeviceSnap(
                id=d.id, manufacturer=d.manufacturer, model=d.model, name=d.name,
                sw_version=d.sw_version, hw_version=getattr(d, "hw_version", None),
                serial_number=getattr(d, "serial_number", None),
                identifiers=_identifiers(d.identifiers),
                config_entry_ids=_config_entry_ids(d),
            )
            for d in active
        ], skipped

    def _snap_entities(self, skipped_devices: frozenset[str]) -> list[EntitySnap]:
        # Encje pominiętego urządzenia pomijamy: jego serial nie trafia do zbioru
        # maskowanego, a encje dołączyłyby do znaleziska po config_entry_id i
        # wyniosły serial z entity_id/unique_id w jawnej postaci.
        return [
            EntitySnap(
                entity_id=e.entity_id, platform=e.platform, unique_id=str(e.unique_id),
                device_id=e.device_id, config_entry_id=e.config_entry_id,
                device_class=e.device_class or e.original_device_class,
                unit=e.unit_of_measurement,
                translation_key=getattr(e, "translation_key", None),
                original_name=e.original_name, disabled=e.disabled_by is not None,
                # options/min/max/step z rejestru — klasyfikacja ładowarek działa bez stanów;
                # raport do chmury ich nie serializuje (pola wybierane jawnie)
                capabilities=_capabilities(e),
            )
            for e in er.async_get(self.hass).entities.values()
            if e.device_id not in skipped_devices
        ]

    def _snap_entries(self) -> list[ConfigEntrySnap]:
        # async_entries() domyślnie zwraca też wpisy zignorowane (source "ignore",
        # przycisk „Ignoruj" przy wykrytym urządzeniu) i wyłączone — to nie są działające
        # integracje falownika, a ich tytuł (często nazwa urządzenia z numerem seryjnym)
        # trafiłby do raportu bez maskowania. Filtr po atrybutach działa w każdej wersji HA.
        return [
            ConfigEntrySnap(entry_id=c.entry_id, domain=c.domain, title=c.title,
                            host=_host(getattr(c, "data", None)))
            for c in self.hass.config_entries.async_entries()
            if getattr(c, "source", None) != "ignore"
            and not getattr(c, "disabled_by", None)
        ]

    def _snap_states(self, c: Classification) -> dict[str, StateSnap]:
        wanted: list[str] = []
        for inv in c.inverters:
            wanted.extend(e.entity_id for e in inv.entities)
        wanted.extend(e.entity_id for e in c.price_entities)
        wanted.extend(e.entity_id for e in c.energy_candidates)
        out: dict[str, StateSnap] = {}
        for eid in dict.fromkeys(wanted):
            s = self.hass.states.get(eid)
            if s is not None:
                out[eid] = StateSnap(eid, s.state, dict(s.attributes))
        return out

    async def _history_days(self, c: Classification, errors: list[str]) -> dict[str, int]:
        try:
            if "recorder" not in self.hass.config.components:
                errors.append("history: recorder not loaded")
                return {}
            ids = {e.entity_id for e in c.energy_candidates}
            if not ids:
                return {}
            start = dt_util.utcnow() - timedelta(days=HISTORY_WINDOW_DAYS)
            rows = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, None, set(ids),
                "day", None, {"sum", "state"})
            tz_name = self.hass.config.time_zone
        except Exception as err:  # noqa: BLE001
            errors.append(_err("history", err))
            return {}
        out: dict[str, int] = {}
        for eid in sorted(ids):
            # days_with_statistics rzuca przy złej strefie / NaN — błąd per encja
            try:
                out[eid] = days_with_statistics((rows or {}).get(eid) or [], tz_name)
            except Exception as err:  # noqa: BLE001
                errors.append(f"history: {eid}: {type(err).__name__}: {err}")
        return out
