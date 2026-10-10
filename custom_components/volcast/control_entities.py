"""Encje sterowania (tylko wpis sparowany): plan dla karty, stan wykonawcy, lokalny wyłącznik.

Bez lustra zgody przełącznik zmienia wyłącznie lokalne wł./wył. sterowania — zgoda konta przychodzi
tylko z chmury (aplikacja Volcast) i tu nie da się jej nadać.

Lustro zgody (ADR-051, `control.consent_mirror` z planu): wyłączenie = lokalnie od razu (powrót do
trybu bazowego), potem w tle `set-controller {kind:'none'}` z ponawianiem aż do 200; włączenie =
NAJPIERW `set-controller {kind:'ha'}` (serwer przywraca tylko zgodę zgaszoną wcześniej przełącznikiem
HA), lokalnie dopiero po 200; odmowa = przełącznik zostaje wyłączony + zgłoszenie w Naprawach.
`set-controller` woła wyłącznie akcja `turn_on`/`turn_off` — nigdy start ani odtworzenie stanu.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (DOMAIN, OPT_BATTERY_CAPACITY_KWH, OPT_CONTROL_MODE, OPT_TELEMETRY_MAP,
                    SIGNAL_CONTROL_UPDATED)
from .core.slot import InvalidSchedule, effective_action, parse_schedule

_LOGGER = logging.getLogger(__name__)
# Ponawianie `set-controller {kind:'none'}` po wyłączeniu w HA: jak `http_retry` (0/5/15/45 s), potem co 5 min.
_OFF_RETRY_DELAYS_S = (0, 5, 15, 45)
_OFF_RETRY_EVERY_S = 300

_CARD_ENTITIES = {"soc": "soc", "pv": "pv_power_w", "grid": "grid_power_w", "battery": "battery_power_w",
                  "load": "load_power_w"}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _str(v: Any) -> str | None:
    return v[:40] if isinstance(v, str) else None


def _utc(moment: datetime) -> str:
    """Chwila w UTC z „Z" — krócej niż `+00:00` (48 slotów mieści się w limicie atrybutów)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_slots_for_card(raw: dict | None, now: datetime) -> list[dict]:
    """Sloty planu dla karty: kontrakt urządzenia + pola wyświetlania z planera, chronologicznie."""
    if not isinstance(raw, dict):
        return []
    try:
        schedule = parse_schedule(raw)
    except InvalidSchedule:
        return []
    raws = sorted((s for s in raw.get("slots") or [] if isinstance(s, dict)),
                  key=lambda s: dt_util.parse_datetime(str(s.get("from"))) or now)
    out = []
    for s, r in zip(schedule.slots, raws):
        out.append({
            "from": _utc(s.start), "to": _utc(s.end), "action": effective_action(s).value,
            "charge_source": s.charge_source, "discharge_purpose": s.discharge_purpose,
            "power_w": s.power_w, "soc_target": s.soc_target, "price": s.price_pln_kwh,
            "export_allowed": s.export_allowed, "export_limit_w": s.export_limit_w,
            "plan_mode": _str(r.get("plan_mode")), "import_kwh": _num(r.get("grid_import_kwh")),
            "export_kwh": _num(r.get("grid_export_kwh")), "display_kind": _str(r.get("display_kind")),
            "now": s.covers(now)})
    return out


class _ControlEntity:
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, rt, key: str) -> None:
        self._entry, self._rt = entry, rt
        self._attr_unique_id = f"{entry.entry_id}_control_{key}"
        self._attr_translation_key = f"control_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, f"{entry.entry_id}_control")},
                                  "name": "Volcast control", "manufacturer": "Volcast", "model": "EMS"}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_CONTROL_UPDATED.format(entry_id=self._entry.entry_id), self.async_write_ha_state))


class VolcastPlanSensor(_ControlEntity, SensorEntity):
    _attr_icon = "mdi:calendar-clock"
    _unrecorded_attributes = frozenset({"slots", "entities", "tou_preview"})

    def __init__(self, entry, rt) -> None:
        super().__init__(entry, rt, "plan")

    @property
    def native_value(self) -> str:
        ex = self._rt.executor
        if ex.schedule is None:
            return "no_plan"
        slot, _ = ex.schedule.effective_slot(dt_util.utcnow())
        return effective_action(slot).value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ex, now = self._rt.executor, dt_util.utcnow()
        slots = plan_slots_for_card(ex.raw_plan, now)
        manual = self._entry.options.get(OPT_TELEMETRY_MAP) or {}
        mapped = self._rt.mapped or {}
        d = ex.last_decision
        fallback = None
        if ex.schedule is not None:
            _, fallback = ex.schedule.effective_slot(now)
        return {
            "slots": slots, "schedule_id": (ex.raw_plan or {}).get("schedule_id"),
            "valid_until": slots[-1]["to"] if slots else None,
            "control_active": bool(d and (d.status == "write" or d.reason == "nothing_to_write")),
            "account_consent": ex.consent, "local_switch": ex.local_switch,
            "reason": d.reason if d else None, "paused": ex.paused, "fallback": fallback,
            "entities": {k: manual.get(key) or mapped.get(key) for k, key in _CARD_ENTITIES.items()},
            "battery_capacity_kwh": self._entry.options.get(OPT_BATTERY_CAPACITY_KWH),
            "tou_preview": ex.tou_preview,
        }


class VolcastControlStatusSensor(_ControlEntity, SensorEntity):
    _attr_icon = "mdi:content-save-check-outline"
    # notatki decyzji zmieniają się co cykl (nastawa sprzedaży) — widoczne na żywo, bez historii
    _unrecorded_attributes = frozenset({"notes"})

    def __init__(self, entry, rt) -> None:
        super().__init__(entry, rt, "status")

    @property
    def native_value(self) -> str:
        d = self._rt.executor.last_decision
        return d.status if d else "starting"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ex = self._rt.executor
        d = ex.last_decision
        return {**(d.summary() if d else {}), "foreign_changes": list(getattr(ex, "foreign_changes", []))}


class VolcastControlSwitch(_ControlEntity, SwitchEntity):
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, entry, rt) -> None:
        super().__init__(entry, rt, "switch")
        self._gen = 0                    # numer ostatniej akcji właściciela (późny wynik starszej odrzucany)
        self._off_task = None
        self._sleep = asyncio.sleep

    @property
    def is_on(self) -> bool:
        return bool(self._rt.executor.local_switch)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._rt.executor.last_decision
        return {"account_consent": self._rt.executor.consent,
                "control_mode": self._entry.options.get(OPT_CONTROL_MODE), "reason": d.reason if d else None}

    def _mirror(self) -> bool:
        """Lustro zgody tylko przy fladze z planu i kliencie chmury; inaczej przełącznik jak dotąd."""
        return (getattr(self._rt.executor, "consent_mirror", False) is True
                and getattr(self._rt, "cloud", None) is not None)

    async def async_turn_on(self, **_kw) -> None:
        ex = self._rt.executor
        if not self._mirror():
            await ex.async_set_local_switch(True)
            await ex.async_tick()
            return
        self._gen += 1
        gen = self._gen
        self._cancel_off()
        if ex.local_switch:
            await ex.async_tick()                    # już włączony — nic do przywrócenia
            return
        result = await self._rt.cloud.async_set_controller("ha")
        if gen != self._gen:
            return                                   # w międzyczasie inna akcja właściciela (np. wyłączenie)
        if result.ok:
            await ex.async_mirror_switch_on()
        else:
            key = ex.note_consent_refused(result.outcome)
            _LOGGER.warning("Volcast control switch stays off: the cloud refused to turn control on (%s)", key)
        await ex.async_tick()

    async def async_turn_off(self, **_kw) -> None:
        ex = self._rt.executor
        if not self._mirror():
            await ex.async_set_local_switch(False)
            await ex.async_tick()
            return
        self._gen += 1
        self._cancel_off()
        await ex.async_mirror_switch_off()           # lokalnie od razu, przed chmurą
        await ex.async_tick()                        # istniejący powrót do trybu bazowego
        self._off_task = self._spawn(self._async_send_off(), "volcast_consent_off")

    def _cancel_off(self) -> None:
        task, self._off_task = self._off_task, None
        if task is not None and not task.done():
            task.cancel()

    def _spawn(self, coro, name: str):
        create = getattr(self._entry, "async_create_background_task", None)
        if create is not None:
            return create(self.hass, coro, name)
        create = getattr(self.hass, "async_create_background_task", None)
        return create(coro, name) if create is not None else self.hass.async_create_task(coro)

    async def _async_send_off(self) -> None:
        """Wyłączenie z HA do chmury, best-effort aż do 200; koniec także, gdy chmura sama zgłosi zgodę off
        (`mirror_off_pending` gaśnie) albo odmówi na stałe (400/403 — np. lustro wyłączone na serwerze)."""
        ex, cloud = self._rt.executor, self._rt.cloud
        delays = iter(_OFF_RETRY_DELAYS_S)
        warned = False
        while ex.mirror_off_pending:
            delay = next(delays, _OFF_RETRY_EVERY_S)
            if delay:
                await self._sleep(delay)
                if not ex.mirror_off_pending:
                    return
            result = await cloud.async_set_controller("none")
            if result.ok:
                ex.mirror_off_delivered()
                return
            if not (result.retriable or result.outcome == "unauthorized"):
                _LOGGER.warning("Volcast control switch off was not accepted by the cloud (%s)", result.outcome)
                return
            if not warned:
                warned = True
                _LOGGER.warning("Volcast control switch off not delivered to the cloud yet (%s) — retrying",
                                result.outcome)
