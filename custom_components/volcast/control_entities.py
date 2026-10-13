"""Encje sterowania (tylko wpis sparowany): plan dla karty, stan wykonawcy, lokalny wyłącznik.

Bez lustra zgody przełącznik zmienia wyłącznie lokalne wł./wył. sterowania — zgoda konta przychodzi
tylko z chmury (aplikacja Volcast) i tu nie da się jej nadać.

Lustro zgody konta (`control.consent_mirror` z planu): wyłączenie = lokalnie od razu (powrót do
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


_SUMMARY_KEYS = ("now", "then", "until", "no_plan", "charge_grid", "charge_pv", "charge", "discharge",
                 "self_consume", "idle", "hold")
# Teksty opisu planu dla człowieka (13 języków); wyłącznie słowa i godziny, bez liczb mocy.
_SUMMARY_TEXTS = {
    "pl": dict(zip(_SUMMARY_KEYS, (
        "Teraz", "potem", "do", "Brak planu", "ładowanie z sieci", "ładowanie z PV", "ładowanie", "rozładowanie", "autokonsumpcja", "bateria w spoczynku", "bateria wstrzymana"))),
    "en": dict(zip(_SUMMARY_KEYS, (
        "Now", "then", "until", "No plan", "charging from the grid", "charging from PV", "charging", "discharging", "self-consumption", "battery idle", "battery on hold"))),
    "de": dict(zip(_SUMMARY_KEYS, (
        "Jetzt", "danach", "bis", "Kein Plan", "Laden aus dem Netz", "Laden aus PV", "Laden", "Entladen", "Eigenverbrauch", "Batterie im Leerlauf", "Batterie angehalten"))),
    "nl": dict(zip(_SUMMARY_KEYS, (
        "Nu", "daarna", "tot", "Geen plan", "laden vanaf het net", "laden met PV", "laden", "ontladen", "eigen verbruik", "batterij in rust", "batterij vastgehouden"))),
    "es": dict(zip(_SUMMARY_KEYS, (
        "Ahora", "después", "hasta", "Sin plan", "carga desde la red", "carga con FV", "carga", "descarga", "autoconsumo", "batería en reposo", "batería retenida"))),
    "fr": dict(zip(_SUMMARY_KEYS, (
        "Maintenant", "ensuite", "jusqu'à", "Aucun plan", "charge depuis le réseau", "charge depuis le PV", "charge", "décharge", "autoconsommation", "batterie au repos", "batterie en attente"))),
    "uk": dict(zip(_SUMMARY_KEYS, (
        "Зараз", "потім", "до", "Немає плану", "заряджання з мережі", "заряджання від PV", "заряджання", "розряджання", "власне споживання", "батарея у спокої", "батарея утримується"))),
    "pt": dict(zip(_SUMMARY_KEYS, (
        "Agora", "depois", "até", "Sem plano", "carregamento da rede", "carregamento com FV", "carregamento", "descarga", "autoconsumo", "bateria em repouso", "bateria retida"))),
    "pt-BR": dict(zip(_SUMMARY_KEYS, (
        "Agora", "depois", "até", "Sem plano", "carregamento da rede", "carregamento com FV", "carregamento", "descarga", "autoconsumo", "bateria em repouso", "bateria retida"))),
    "ro": dict(zip(_SUMMARY_KEYS, (
        "Acum", "apoi", "până la", "Fără plan", "încărcare din rețea", "încărcare din PV", "încărcare", "descărcare", "autoconsum", "baterie în repaus", "baterie reținută"))),
    "cs": dict(zip(_SUMMARY_KEYS, (
        "Nyní", "potom", "do", "Žádný plán", "nabíjení ze sítě", "nabíjení z FV", "nabíjení", "vybíjení", "vlastní spotřeba", "baterie v klidu", "baterie podržena"))),
    "sk": dict(zip(_SUMMARY_KEYS, (
        "Teraz", "potom", "do", "Žiadny plán", "nabíjanie zo siete", "nabíjanie z FV", "nabíjanie", "vybíjanie", "vlastná spotreba", "batéria v pokoji", "batéria podržaná"))),
    "it": dict(zip(_SUMMARY_KEYS, (
        "Ora", "poi", "fino alle", "Nessun piano", "carica dalla rete", "carica da FV", "carica", "scarica", "autoconsumo", "batteria a riposo", "batteria in attesa"))),
}


def _summary_label(texts: dict, slot: dict) -> str:
    action = slot.get("action")
    if action == "charge":
        return texts["charge_" + slot["charge_source"]] if slot.get("charge_source") in ("grid", "pv") \
            else texts["charge"]
    return texts.get(action) or texts["idle"]


def plan_summary_text(slots: list[dict], language: str | None, to_local) -> str:
    """Krótki opis planu: bieżący odcinek (akcja + do której godziny), potem następny. Bez liczb mocy.

    `slots` — wynik `plan_slots_for_card`; `to_local` zamienia chwilę UTC na czas lokalny użytkownika.
    """
    lang = language if language in _SUMMARY_TEXTS else (language or "").split("-")[0]
    texts = _SUMMARY_TEXTS.get(lang, _SUMMARY_TEXTS["en"])
    start = next((i for i, s in enumerate(slots) if s.get("now")), None)
    if start is None:
        return texts["no_plan"]

    def run_end(i: int) -> int:
        key = (slots[i].get("action"), slots[i].get("charge_source"))
        while i + 1 < len(slots) and (slots[i + 1].get("action"), slots[i + 1].get("charge_source")) == key:
            i += 1
        return i

    end = run_end(start)
    until = to_local(dt_util.parse_datetime(slots[end]["to"])).strftime("%H:%M")
    text = f"{texts['now']}: {_summary_label(texts, slots[start])} {texts['until']} {until}"
    if end + 1 < len(slots):
        text += f" · {texts['then']}: {_summary_label(texts, slots[end + 1])}"
    return text


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


class VolcastPlanSummarySensor(_ControlEntity, SensorEntity):
    """Plan po ludzku: jeden krótki tekst z akcji i godzin slotów, bez liczb mocy."""

    _attr_icon = "mdi:text-box-outline"

    def __init__(self, entry, rt) -> None:
        super().__init__(entry, rt, "plan_summary")

    @property
    def native_value(self) -> str:
        now = dt_util.utcnow()
        slots = plan_slots_for_card(self._rt.executor.raw_plan, now)
        language = getattr(getattr(self.hass, "config", None), "language", None)
        return plan_summary_text(slots, language, dt_util.as_local)[:255]


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
        self._off_calling = False        # nadawca wyłączenia czeka właśnie na odpowiedź chmury
        # Jedno wywołanie `set-controller` naraz: chmura dostaje prośby w kolejności akcji właściciela
        # (inaczej „off” wysłane w trakcie „on” mogłoby dojść pierwsze i zostać nadpisane nadaniem).
        self._call_lock = asyncio.Lock()
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
        """Lustro zgody przy fladze z planu (albo dopóki wyłączenie właściciela czeka na doręczenie) i kliencie
        chmury; inaczej przełącznik jak dotąd."""
        ex = self._rt.executor
        return ((getattr(ex, "consent_mirror", False) is True or getattr(ex, "mirror_off_pending", False) is True)
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
        async with self._call_lock:
            if gen != self._gen:
                return                               # w międzyczasie nowsza akcja właściciela
            result = await self._rt.cloud.async_set_controller("ha")
        if gen != self._gen:
            # Nowsza akcja (wyłączenie) w trakcie: nadanie mogło dojść do chmury — wyłączenie musi pójść za nim.
            if result.ok:
                self._ensure_off_sender()
            return
        if result.ok:
            await ex.async_mirror_switch_on()
        else:
            key = ex.note_consent_refused(result.outcome)
            _LOGGER.warning("Volcast control switch stays off: the cloud refused to turn control on (%s)", key)
            self._ensure_off_sender()                # wcześniejsze wyłączenie nadal niedoręczone
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
        self._ensure_off_sender()

    def _ensure_off_sender(self) -> None:
        """Nadawca wyłączenia dla bieżącej akcji, o ile wyłączenie czeka, a żywego nadawcy nie ma."""
        if not self._rt.executor.mirror_off_pending:
            return
        if self._off_task is not None and not self._off_task.done():
            return
        self._off_task = self._spawn(self._async_send_off(self._gen), "volcast_consent_off")

    def _cancel_off(self) -> None:
        """Koniec nadawcy wyłączenia. W trakcie wywołania nie przerywamy (prośba mogła już dojść) — nadawca
        sam kończy po odpowiedzi, bo jego akcja nie jest już bieżąca."""
        task, self._off_task = self._off_task, None
        if task is not None and not task.done() and not self._off_calling:
            task.cancel()

    def _spawn(self, coro, name: str):
        create = getattr(self._entry, "async_create_background_task", None)
        if create is not None:
            return create(self.hass, coro, name)
        create = getattr(self.hass, "async_create_background_task", None)
        return create(coro, name) if create is not None else self.hass.async_create_task(coro)

    async def _async_send_off(self, gen: int) -> None:
        """Wyłączenie z HA do chmury, best-effort aż do 200. Koniec także, gdy chmura sama zgłosi zgodę off
        (`mirror_off_pending` gaśnie), gdy odmówi na stałe (każda odmowa spoza błędów sieci, 5xx, 429 i 401, w tym 409 — np. lustro wyłączone na serwerze) albo
        gdy właściciel wykonał nowszą akcję."""
        ex, cloud = self._rt.executor, self._rt.cloud
        delays = iter(_OFF_RETRY_DELAYS_S)
        warned = False
        while True:
            delay = next(delays, _OFF_RETRY_EVERY_S)
            if delay:
                await self._sleep(delay)
            async with self._call_lock:
                if gen != self._gen or not ex.mirror_off_pending:
                    return
                self._off_calling = True
                try:
                    result = await cloud.async_set_controller("none")
                finally:
                    self._off_calling = False
            if result.ok:
                if gen == self._gen:
                    ex.mirror_off_delivered()
                return
            if not (result.retriable or result.outcome == "unauthorized"):
                _LOGGER.warning("Volcast control switch off was not accepted by the cloud (%s)", result.outcome)
                if gen == self._gen:
                    ex.mirror_off_dropped()
                return
            if not warned:
                warned = True
                _LOGGER.warning("Volcast control switch off not delivered to the cloud yet (%s) — retrying",
                                result.outcome)
