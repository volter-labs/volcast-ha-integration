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


async def _async_statistics(hass, ids: set[str], start: datetime, end: datetime) -> dict:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, ids, "hour", None, {"change"})


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
    extra = {"pv_kwh": (rows.get(pv_entity) or [], units.get(pv_entity))} if pv_entity else None
    hours = hours_from_statistics(rows.get(load_entity) or [], load_unit=units.get(load_entity),
                                  now_utc=now_utc, extra=extra)
    if not hours:
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
    await executor.async_mark_history_imported(now_utc.isoformat())
    merged = _merge(results, [len(p) for p in parts])
    _LOGGER.info("Volcast history import done: %s hours sent, %s inserted",
                 len(hours), merged.get("inserted"))
    return merged


async def async_import_history_once(hass, cloud, executor, *, load_entity: str | None,
                                    pv_entity: str | None = None, now_utc: datetime) -> dict | None:
    """Wyślij historię raz na wpis; wynik chmury (zsumowany z partii) albo None."""
    if not load_entity:
        return None
    lock = _LOCKS.get(executor)
    if lock is None:
        lock = _LOCKS[executor] = asyncio.Lock()
    waited = lock.locked()
    async with lock:
        if executor.history_imported_at:
            # Czekający na przebieg w toku dostaje jego wynik; każdy późniejszy — nic.
            return _LAST_RESULT.get(executor) if waited else None
        result = await _async_import(hass, cloud, executor, load_entity, pv_entity, now_utc)
        if result is not None:
            _LAST_RESULT[executor] = result
        return result
