"""Jednorazowy import historii zużycia z rekordera HA do chmury (zimny start planera).

Odczyt statystyk idzie przez wykonawcę rekordera (`async_add_executor_job`) — nigdy
w pętli zdarzeń. Wysyłka partiami (limity jednego żądania w `core.control.history`).
Chmura wstawia tylko brakujące godziny, więc powtórka po awarii jest bezpieczna;
znacznik „zaimportowano" w magazynie wykonawcy oszczędza powtórek po restarcie.

Jeden przebieg naraz na wykonawcę: tło przy starcie i onboarding potrafią ruszyć
w tej samej sekundzie — drugi czeka na pierwszy i dostaje jego wynik.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from weakref import WeakKeyDictionary

from ..core.control.history import HISTORY_DAYS, batches, hours_from_statistics

_LOGGER = logging.getLogger(__name__)
_REJECTED_MAX = 20
_COUNTS = ("accepted", "inserted", "skipped_existing")

_LOCKS: WeakKeyDictionary = WeakKeyDictionary()
_LAST_RESULT: WeakKeyDictionary = WeakKeyDictionary()

#: Jednostki metadanych statystyki, które są KLASĄ energii. Same NIE są współczynnikiem
#: przeliczenia — o to jawnie prosimy rekorder (`units={"energy": "kWh"}` niżej), więc
#: wiersze przychodzą już w kWh. Tu tylko potwierdzamy, że to w ogóle energia, a nie np.
#: moc — inaczej byłoby to zgadywanie jednostki z metadanych.
_ENERGY_UNITS = frozenset({
    "Wh", "kWh", "MWh", "GWh", "mWh", "TWh",
    "GJ", "MJ", "kJ", "J", "cal", "kcal", "Mcal", "Gcal",
})


async def _async_statistics(hass, ids: set[str], start: datetime, end: datetime) -> dict:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    # Jawnie żądana jednostka: rekorder przelicza KAŻDĄ statystykę klasy energii do kWh
    # sam, niezależnie od jednostki zapisanej w metadanych czy aktualnej jednostki stanu
    # encji — bez tego `units=None` konwertuje do jednostki STANU, która bywa inna niż
    # metadana, co dawało przeliczenie 1000x za duże/za małe.
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, ids, "hour", {"energy": "kWh"}, {"change"})


async def _async_units(hass, ids: set[str]) -> dict[str, str | None]:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import get_metadata
    meta = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids=ids))
    out: dict[str, str | None] = {}
    for sid, m in (meta or {}).items():
        info = m[1] if isinstance(m, tuple) and len(m) > 1 else None
        unit = info.get("unit_of_measurement") if isinstance(info, dict) else None
        out[sid] = unit if isinstance(unit, str) else None
    return out


def _energy_kwh_unit(raw_unit: str | None) -> str | None:
    """"kWh" gdy metadana potwierdza klasę energii, inaczej None (seria pominięta).

    Wołający już poprosił rekorder o `units={"energy": "kWh"}`, więc wartości SĄ w kWh,
    gdy metadana w ogóle jest energią — ta funkcja tylko waliduje klasę, nie przelicza.
    """
    return "kWh" if raw_unit in _ENERGY_UNITS else None


def _count(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _merge(results: list[dict], sizes: list[int]) -> dict:
    """Suma odpowiedzi partii; indeksy odrzuconych przesunięte na całą listę godzin."""
    if len(results) == 1:
        return results[0]
    total: dict = {}
    rejected: list[dict] = []
    offset = 0
    for res, size in zip(results, sizes):
        for key in _COUNTS:
            n = _count(res.get(key))
            if n is not None:
                total[key] = total.get(key, 0) + n
        for r in res.get("rejected") or ():
            if isinstance(r, dict) and _count(r.get("index")) is not None and isinstance(r.get("reason"), str):
                rejected.append({"index": r["index"] + offset, "reason": r["reason"]})
        offset += size
    total["rejected"] = rejected[:_REJECTED_MAX]
    return total


async def _async_import(hass, cloud, executor, load_entity: str, pv_entity: str | None,
                        now_utc: datetime) -> dict | None:
    ids = {e for e in (load_entity, pv_entity) if e}
    try:
        rows = await _async_statistics(hass, ids, now_utc - timedelta(days=HISTORY_DAYS), now_utc)
        units = await _async_units(hass, ids)
    except Exception as err:  # noqa: BLE001 — rekorder bywa niezaładowany; spróbujemy przy następnym starcie
        _LOGGER.info("Volcast history import skipped: recorder unavailable (%s)", type(err).__name__)
        return None
    rows = rows if isinstance(rows, dict) else {}
    load_unit = _energy_kwh_unit(units.get(load_entity))
    if load_unit is None:
        _LOGGER.debug("Volcast history import: load sensor statistic is not energy-class, skipping")
    extra = {"pv_kwh": (rows.get(pv_entity) or [], _energy_kwh_unit(units.get(pv_entity)))} if pv_entity else None
    hours = hours_from_statistics(rows.get(load_entity) or [], load_unit=load_unit,
                                  now_utc=now_utc, extra=extra)
    if not hours:
        _LOGGER.debug("Volcast history import: no complete hours available, nothing to send")
        return None
    results: list[dict] = []
    parts = batches(hours)
    for part in parts:
        result = await cloud.async_import_history(part)
        if not isinstance(result, dict) or _count(result.get("accepted")) is None:
            # Nie oznaczamy — następny start wyśle całość jeszcze raz (chmura pominie istniejące).
            _LOGGER.info("Volcast history import incomplete: batch %s of %s failed",
                         len(results) + 1, len(parts))
            return None
        results.append(result)
    merged = _merge(results, [len(p) for p in parts])
    processed = (_count(merged.get("accepted")) or 0) + (_count(merged.get("skipped_existing")) or 0)
    if processed > 0:
        try:
            await executor.async_mark_history_imported(now_utc.isoformat())
        except Exception as err:  # noqa: BLE001 — znacznik nie może wywrócić importu, który się już udał
            _LOGGER.warning("Volcast history import: could not save the imported marker (%s)",
                            type(err).__name__)
        if processed < len(hours):
            # Częściowa akceptacja (np. zły czujnik dla części encji) jest inaczej NIEWIDOCZNA:
            # „done" wyglądałoby tak samo jak pełny sukces. Sam licznik i pierwszy powód, bez id encji.
            odrzucone = merged.get("rejected") or []
            pierwszy_powod = odrzucone[0].get("reason") if odrzucone and isinstance(odrzucone[0], dict) else None
            _LOGGER.info("Volcast history import partial: %s of %s hours rejected (first reason: %s)",
                         len(hours) - processed, len(hours), pierwszy_powod)
        _LOGGER.info("Volcast history import done: %s hours sent, %s inserted",
                     len(hours), merged.get("inserted"))
    else:
        # 0 przyjętych i 0 pominiętych jako istniejące = wszystko odrzucone (zły czujnik/
        # jednostka) — nie oznaczamy, żeby naprawiona konfiguracja mogła spróbować ponownie.
        _LOGGER.warning("Volcast history import: cloud accepted 0 of %s hours sent, not marking done",
                        len(hours))
    return merged


async def async_import_history_once(hass, cloud, executor, *, load_entity: str | None,
                                    pv_entity: str | None = None, now_utc: datetime) -> dict | None:
    """Wyślij historię raz na wpis; wynik chmury (zsumowany z partii) albo None."""
    if not load_entity:
        _LOGGER.debug("Volcast history import skipped: no load entity configured")
        return None
    lock = _LOCKS.get(executor)
    if lock is None:
        lock = _LOCKS[executor] = asyncio.Lock()
    waited = lock.locked()
    async with lock:
        if waited:
            # Ten wołający czekał na przebieg w toku — dostaje JEGO wynik, sukces czy
            # porażka, i NIGDY nie odpala własnego. Powtórka po awarii należy do
            # kolejnego, niezależnego wywołania, nie do tego, które tylko czekało
            # na cudzy przebieg.
            return _LAST_RESULT.get(executor)
        # Zerujemy PRZED każdą ścieżką tego przebiegu (no-op, błąd, sukces) — czekający
        # nie może dostać wyniku SPRZED tego wywołania (np. wczorajszej odpowiedzi chmury).
        _LAST_RESULT[executor] = None
        if executor.history_imported_at:
            _LOGGER.debug("Volcast history import skipped: already imported at %s",
                         executor.history_imported_at)
            return None
        result = None
        try:
            result = await _async_import(hass, cloud, executor, load_entity, pv_entity, now_utc)
        finally:
            # `finally`, nie po `try`: wyjątek z `_async_import` ma trafić do TEGO wołającego
            # (leadera), a czekający ma i tak zobaczyć spójny stan (None), nie sprzed przebiegu.
            _LAST_RESULT[executor] = result
        return result
