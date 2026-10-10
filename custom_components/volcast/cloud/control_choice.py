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
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

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


def _newer(raw, last_raw) -> bool:
    ts, last = parse_at(raw), parse_at(last_raw)
    return ts is not None and (last is None or ts > last)


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
