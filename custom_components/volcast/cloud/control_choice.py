"""Decyzje właściciela z planu (blok `control` w odpowiedzi `get-schedule`).

Blok niesie lepkie wartości (`path`, `controller` zostają, gdy zmienia się tylko `at`), więc pola są stosowane
OSOBNO, jako różnica względem ostatniej rozstrzygniętej wartości — przestarzałe pole nie blokuje pozostałych:

* `box_active` → konflikt `box` (każdy przebieg).
* `path` / `controller` — stosowane, gdy wartość różni się od ostatnio rozstrzygniętej.
* `verification` — jednorazowe: stosowane, gdy `at` jest nowsze niż ostatnie `at` polecenia.

Wynik pola: `applied` → zapamiętane i w `choice_ack`; `ignored` → zapamiętane jako rozstrzygnięte, bez ack;
niepowodzenie (`restore_failed`, wyjątek) → bez zapisu i ponowienie przy kolejnych pobraniach, najwyżej
`MAX_TRIES` razy na (pole, wartość); potem rozstrzygnięte i zgłoszenie błędu sterowania dla właściciela.
`choice_ack` zawiera wyłącznie zastosowane pola.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

PATHS = frozenset({"entities", "direct", "plan_only"})
CONTROLLERS = frozenset({"volcast", "own_ems"})
VERIFICATIONS = frozenset({"abort", "retry"})
MAX_TRIES = 3
MAX_TRIES_KEYS = 8


def parse_at(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


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
    at = parse_at(at_raw)
    if at is None:
        return
    meta = rt.executor.control_meta
    tries = meta.setdefault("tries", {})
    ack = dict(meta.get("ack") or {})
    applied_any = failed_out = dirty = False

    def settle(key: str) -> None:
        tries.pop(key, None)

    async def field(name: str, value, call) -> None:
        nonlocal applied_any, failed_out, dirty
        key = f"{name}:{value}"
        result = await _try(call, value)
        if result == "applied":
            ack[name] = value
            applied_any = True
        elif result == "ignored":
            _LOGGER.debug("Volcast control choice %s not applicable now — consumed", name)
        else:
            n = tries.get(key, 0) + 1
            if n < MAX_TRIES:
                tries[key] = n
                dirty = True
                return                                  # ponowienie przy kolejnym pobraniu
            _LOGGER.warning("Volcast control choice %s could not be applied — giving up", name)
            failed_out = True
        settle(key)
        meta[f"{name}_done"] = value
        dirty = True

    path, controller, verification = (control.get(k) for k in ("path", "controller", "verification"))
    if path in PATHS and path != meta.get("path_done"):
        await field("path", path, rt.async_apply_path_choice)
    if controller in CONTROLLERS and controller != meta.get("controller_done"):
        await field("controller", controller, rt.async_apply_controller_choice)
    if verification in VERIFICATIONS:
        last = parse_at(meta.get("ver_at"))
        if last is None or at > last:
            ver = getattr(rt, "verification", None)
            call = None if ver is None else (ver.async_abort if verification == "abort" else ver.async_retry)

            async def run_verification(_value):
                if call is None:
                    return "ignored"
                await call()
                return "applied"

            key = f"verification:{at_raw}"
            result = await _try(run_verification, at_raw)
            if result in ("applied", "ignored"):
                settle(key)
                meta["ver_at"] = at_raw
                dirty = True
                applied_any = applied_any or result == "applied"
                if result == "applied":
                    ack["at"] = at_raw
            else:
                n = tries.get(key, 0) + 1
                dirty = True
                if n < MAX_TRIES:
                    tries[key] = n
                else:
                    settle(key)
                    meta["ver_at"] = at_raw
                    failed_out = True
    if len(tries) > MAX_TRIES_KEYS:
        for old in list(tries)[:-MAX_TRIES_KEYS]:
            del tries[old]
    if applied_any:
        ack["at"] = at_raw
        meta["ack"] = ack                                # ack dopiero po sukcesie
    if not tries:
        meta.pop("tries", None)
    if dirty:
        await rt.executor.async_save_control_meta()
    if failed_out and hasattr(rt, "report_choice_error"):
        rt.report_choice_error()
    if applied_any:
        rt.notify_control_state()
