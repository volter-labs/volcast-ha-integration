"""Decyzje właściciela z planu (blok `control` w odpowiedzi `get-schedule`).

Pola są stosowane OSOBNO — przestarzałe pole nie blokuje pozostałych:

* `box_active` → konflikt `box` (każdy przebieg).
* `path` / `controller` — stosowane, gdy `<pole>_at` jest nowsze niż ostatnio zastosowane (ponowny wybór tej
  samej wartości z nowszym znacznikiem też się liczy). Bez `<pole>_at` (starsza chmura): gdy wartość różni się
  od ostatnio rozstrzygniętej.
* `verification` — jednorazowe: gdy `verification_at` (bez niego `at`) jest nowsze niż ostatnie zastosowane.

Wynik pola: `applied` → zapamiętane i w `choice_ack`; `ignored` → zapamiętane jako rozstrzygnięte, bez ack;
niepowodzenie (`restore_failed`, wyjątek) → bez zapisu i ponowienie przy kolejnych pobraniach planu (najwyżej
jedna próba na `RETRY_GAP_S`, żeby pingi nie zużywały limitu), najwyżej `MAX_TRIES` prób na (pole, znacznik);
potem rozstrzygnięte i zgłoszenie dla właściciela. `choice_ack` zawiera wyłącznie zastosowane pola.

Reguła jednego zastosowania (reguła ścieżki sterowania): ten sam wybór ścieżki dociera dwoma kanałami — z kreatora
parowania (`choices.control_mode`) i tu, jako `path` z nowszym `path_at`. Kreator, który ścieżkę zastosował,
zapisuje w danych wpisu (`pairing.applied_choices`, razem ze zmianą opcji) znacznik: wartość, chwilę wg zegara HA
i `path_at` ostatnio rozstrzygniętej decyzji z planu. Pierwsza należna `path` z TĄ SAMĄ wartością, póki znacznik
żyje (najwyżej `WIZARD_MARK_S` = okno sesji na żywo), jest bez działania — bez ponownego zastosowania, wyszukiwania
i restartu drabiny; zapisujemy tylko `path_done`/`path_at` i `path` w `choice_ack`. Rozstrzygnięcie dowolnej `path`
(nowe `path_at` w stanie) zużywa znacznik, więc każda następna powtórka stosuje się zwykłą regułą.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..const import CONF_PAIRING

_LOGGER = logging.getLogger(__name__)

# Zapis wyborów zastosowanych przez kreator parowania (`entry.data[pairing]`) i znacznik w nim.
APPLIED_CHOICES = "applied_choices"
WIZARD_MARK = "mark"
WIZARD_MARK_S = 1800.0       # okno sesji parowania na żywo (30 min)

PATHS = frozenset({"entities", "direct", "plan_only"})
CONTROLLERS = frozenset({"volcast", "own_ems"})
VERIFICATIONS = frozenset({"abort", "retry"})
MAX_TRIES = 3
MAX_TRIES_KEYS = 8
RETRY_GAP_S = 240.0          # krócej niż okres pobierania planu (300 s), dłużej niż seria pingów


def parse_at(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _newer(raw, last_raw) -> bool:
    ts, last = parse_at(raw), parse_at(last_raw)
    return ts is not None and (last is None or ts > last)


def wizard_mark(meta: Mapping | None) -> dict:
    """Znacznik „zastosowano z kreatora” do zapisu przy wyborze: `path_at` ostatnio rozstrzygniętej decyzji."""
    return {"path_at": (meta or {}).get("path_at")}


def _wizard_marked(rt, meta: Mapping, value: str, now: datetime) -> bool:
    """Czy `value` zastosował już kreator parowania, a znacznik nie wygasł i nie został zużyty."""
    data = getattr(getattr(rt, "entry", None), "data", None)
    pairing = data.get(CONF_PAIRING) if isinstance(data, Mapping) else None
    record = pairing.get(APPLIED_CHOICES) if isinstance(pairing, Mapping) else None
    choices = record.get("choices") if isinstance(record, Mapping) else None
    item: Any = choices.get("control_mode") if isinstance(choices, Mapping) else None
    mark = item.get(WIZARD_MARK) if isinstance(item, Mapping) else None
    if not isinstance(mark, Mapping) or item.get("value") != value:
        return False
    at = parse_at(item.get("at"))
    if at is None or not timedelta(0) <= now - at <= timedelta(seconds=WIZARD_MARK_S):
        return False
    return mark.get("path_at") == meta.get("path_at")          # inna = decyzja rozstrzygnięta po kreatorze


async def _already_applied(_value) -> str:
    return "applied"


async def _try(call, *args) -> str:
    try:
        return await call(*args)
    except Exception as err:  # noqa: BLE001 — wyjątek to niepowodzenie pola, nie całego przebiegu
        _LOGGER.debug("Volcast control choice step failed (%s)", type(err).__name__)
        return "error"


async def apply(control, rt) -> None:
    """Zastosuj blok `control` na runtime sterowania (błędy `box_active` łapie fetcher)."""
    if not isinstance(control, dict):
        return
    box = control.get("box_active")
    if isinstance(box, bool):
        await rt.async_set_box_active(box)
    at_raw = control.get("at")
    if parse_at(at_raw) is None:
        return
    meta = rt.executor.control_meta
    tries = meta.setdefault("tries", {})
    last_try = rt.__dict__.setdefault("_choice_last_try", {})      # w pamięci: restart zaczyna bez przerwy
    clock = getattr(rt, "choice_clock", time.monotonic)
    ack = dict(meta.get("ack") or {})
    state = {"applied": False, "gave_up": False, "dirty": False}

    async def field(name: str, value, ts_raw, call) -> str | None:
        """Zastosuj pole; zwraca wynik albo None, gdy ponowienie jest jeszcze za wcześnie."""
        key = f"{name}:{ts_raw or value}"
        if key in tries and clock() - last_try.get(key, -RETRY_GAP_S) < RETRY_GAP_S:
            return None
        result = await _try(call, value)
        if result == "applied":
            ack[name] = value
            state["applied"] = True
        elif result == "ignored":
            _LOGGER.debug("Volcast control choice %s not applicable now — consumed", name)
        else:
            n = tries.get(key, 0) + 1
            last_try[key] = clock()
            state["dirty"] = True
            if n < MAX_TRIES:
                tries[key] = n
                return result                            # ponowienie przy kolejnym pobraniu
            _LOGGER.warning("Volcast control choice %s could not be applied — giving up", name)
            state["gave_up"] = True
        tries.pop(key, None)
        last_try.pop(key, None)
        meta[f"{name}_done"] = value
        if ts_raw:
            meta[f"{name}_at"] = ts_raw
        state["dirty"] = True
        return result

    path, controller, verification = (control.get(k) for k in ("path", "controller", "verification"))
    for name, value, allowed, call in (("path", path, PATHS, rt.async_apply_path_choice),
                                       ("controller", controller, CONTROLLERS, rt.async_apply_controller_choice)):
        if value not in allowed:
            continue
        ts_raw = control.get(f"{name}_at")
        if parse_at(ts_raw) is None:
            ts_raw = None
            due = value != meta.get(f"{name}_done")                 # starsza chmura: reguła zmiany wartości
        else:
            due = _newer(ts_raw, meta.get(f"{name}_at"))
        if due and name == "path" and ts_raw is not None \
                and _wizard_marked(rt, meta, value, (getattr(rt, "wall_clock", None) or _utcnow)()):
            _LOGGER.debug("Volcast control choice path %s already applied by the pairing wizard", value)
            call = _already_applied
        if due:
            await field(name, value, ts_raw, call)
    if verification in VERIFICATIONS:
        ver_raw = control.get("verification_at")
        if parse_at(ver_raw) is None:
            ver_raw = at_raw
        if _newer(ver_raw, meta.get("ver_at")):
            ver = getattr(rt, "verification", None)

            async def run_verification(_value):
                if ver is None:
                    return "ignored"
                await (ver.async_abort() if verification == "abort" else ver.async_retry())
                return "applied"

            key = f"verification:{ver_raw}"
            if key not in tries or clock() - last_try.get(key, -RETRY_GAP_S) >= RETRY_GAP_S:
                result = await _try(run_verification, ver_raw)
                if result in ("applied", "ignored"):
                    state["applied"] = state["applied"] or result == "applied"
                    tries.pop(key, None)
                    meta["ver_at"] = ver_raw
                else:
                    n = tries.get(key, 0) + 1
                    last_try[key] = clock()
                    if n < MAX_TRIES:
                        tries[key] = n
                    else:
                        tries.pop(key, None)
                        meta["ver_at"] = ver_raw
                        state["gave_up"] = True
                state["dirty"] = True
    if len(tries) > MAX_TRIES_KEYS:
        for old in list(tries)[:-MAX_TRIES_KEYS]:
            del tries[old]
    if state["applied"]:
        ack["at"] = at_raw
        meta["ack"] = ack                                # ack dopiero po sukcesie
    if not tries:
        meta.pop("tries", None)
    if state["dirty"]:
        await rt.executor.async_save_control_meta()
    if state["gave_up"] and hasattr(rt, "report_choice_error"):
        rt.report_choice_error()
    if state["applied"]:
        rt.notify_control_state()
