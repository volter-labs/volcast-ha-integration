"""Decyzje właściciela z planu (blok `control` w odpowiedzi `get-schedule`).

* `box_active` → konflikt `box` (każdy przebieg, bez bramki `at`).
* `path` / `controller` / `verification` — stosowane RAZ na `at`: tylko gdy `at` jest nowsze niż ostatnio
  zastosowane (trwałe w magazynie sterowania). `verification` jest jednorazowe, więc idzie tą samą bramką.
* Po zastosowaniu `choice_ack` {path?, controller?, at} jedzie w następnej telemetrii (`driver.control`).
* Nieudany powrót do trybu bazowego (albo odmowa) nie zapisuje `at` — decyzja wraca przy następnym pobraniu.
"""
from __future__ import annotations

from datetime import datetime, timezone

PATHS = frozenset({"entities", "direct", "plan_only"})
CONTROLLERS = frozenset({"volcast", "own_ems"})
VERIFICATIONS = frozenset({"abort", "retry"})


def parse_at(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


async def apply(control, rt) -> None:
    """Zastosuj blok `control` na runtime sterowania (błędy łapie fetcher)."""
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
    last = parse_at(meta.get("at"))
    if last is not None and at <= last:
        return
    path, controller, verification = (control.get(k) for k in ("path", "controller", "verification"))
    path = path if path in PATHS else None
    controller = controller if controller in CONTROLLERS else None
    verification = verification if verification in VERIFICATIONS else None
    if path is None and controller is None and verification is None:
        return
    ack = {k: v for k, v in (("path", path), ("controller", controller)) if v}
    ack["at"] = at_raw
    previous = {k: meta.get(k) for k in ("at", "ack")}
    # Zapis PRZED zastosowaniem: zmiana ścieżki przeładowuje wpis, a ten sam `at` nie może wrócić.
    meta["at"], meta["ack"] = at_raw, ack
    await rt.executor.async_save_control_meta()
    failed = False
    if path is not None:
        failed = await rt.async_apply_path_choice(path) != "applied"
    if controller is not None and not failed:
        failed = await rt.async_apply_controller_choice(controller) != "applied"
    if failed:
        for key, value in previous.items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        await rt.executor.async_save_control_meta()
        return
    ver = getattr(rt, "verification", None)
    if verification is not None and ver is not None:
        await (ver.async_abort() if verification == "abort" else ver.async_retry())
    rt.notify_control_state()
