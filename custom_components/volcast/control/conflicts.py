"""Drugi sterownik falownika — dowody z HA i lista `conflicts` bloku `driver.control` (część zależna od HA).

Nasłuch zdarzeń `automation_triggered` (przebiegi automatyzacji, bufor 24 h) i `call_service`
(wywołania usług): wywołanie z celem w encjach zapisu wykonawcy (`executor.write_entity_ids`) i z
kontekstem przebiegu automatyzacji (albo jego dzieckiem) to zapis automatyzacji. Zapis bez kontekstu
automatyzacji (użytkownik, nasz zapis) pomijamy — obcą zmianę prowadzi wykonawca (pauza). Liczenie
i kształt listy: `core/control/conflict.py` (`AutomationWriteTracker`, `controllers_from_evidence`);
pozostałe dowody podaje runtime: kolizje adresu (`clashes`), klient na łączu (`lan_client`), Box konta
(`async_set_box_active`, z planu).

Przeliczenie po każdym zapisie automatyzacji, zmianie `box_active` i po każdym cyklu wykonawcy (wygasanie
dowodów, kolizje trybu bezpośredniego). Skutki zmiany:
* nowa para (`kind`, `label`), której właściciel nie potwierdził wyborem `volcast` (`executor.conflict_ack`),
  zatrzymuje trwającą drabinę (`stopped controller_conflict`); zweryfikowane sterowanie chroni pauza
  wykonawcy przy obcej zmianie;
* zgłoszenie `controller_conflict_<wpis>` (parametry: rodzaj i etykieta — automatyzacja do wyłączenia)
  trwa, dopóki jest dowód; w trybie „tylko plan” (`own_ems`) żadnego zgłoszenia;
* sygnał `SIGNAL_CONTROL_STATE_UPDATED` od razu przy zmianie par, przy samym liczniku zapisów najwyżej
  raz na godzinę.

Bufor żyje w pamięci: po restarcie automatyzacja wraca na listę przy następnym zapisie.
W logach tylko rodzaje konfliktów — nigdy encje ani automatyzacje.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send

from ..const import DOMAIN, SIGNAL_CONTROL_STATE_UPDATED, SIGNAL_CONTROL_UPDATED
from ..core.control.conflict import AUTOMATION, AutomationWriteTracker, controllers_from_evidence

_LOGGER = logging.getLogger(__name__)
_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")

EVENT_AUTOMATION_TRIGGERED = "automation_triggered"
EVENT_CALL_SERVICE = "call_service"
ISSUE_CONTROLLER_CONFLICT = "controller_conflict"
SIGNAL_EVERY_S = 3600.0


class ConflictMonitor:
    def __init__(self, hass, entry, executor, *, verification=None,
                 clashes: Callable[[], Iterable[str]] = tuple,
                 lan_client: Callable[[], str | None] = lambda: None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._hass = hass
        self._entry = entry
        self._ex = executor
        self._verification = verification
        self._clashes = clashes
        self._lan_client = lan_client
        self._clock = clock
        self._tracker = AutomationWriteTracker()
        self._box_active = False
        self._pairs: tuple[tuple[str, str], ...] = ()
        self._payload: list[dict] = []
        self._signalled_at: float | None = None
        self._issue_open = False
        self._unsubs: list[Callable[[], None]] = []
        self._stopped = False

    @property
    def _issue_id(self) -> str:
        return f"{ISSUE_CONTROLLER_CONFLICT}_{self._entry.entry_id}"

    @property
    def box_active(self) -> bool:
        return self._box_active

    def conflicts(self) -> list[dict]:
        """Blok `driver.control.conflicts` (kopia)."""
        return [dict(c) for c in self._payload]

    # ── cykl życia ──

    async def async_start(self) -> None:
        # Dowody z poprzedniego przebiegu (bufor w pamięci) przepadły — zgłoszenie wróci z nowym dowodem.
        ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
        bus = self._hass.bus
        self._unsubs.append(bus.async_listen(EVENT_AUTOMATION_TRIGGERED, self._on_trigger))
        self._unsubs.append(bus.async_listen(EVENT_CALL_SERVICE, self._on_call))
        self._unsubs.append(async_dispatcher_connect(
            self._hass, SIGNAL_CONTROL_UPDATED.format(entry_id=self._entry.entry_id), self._on_cycle))
        await self.async_refresh()

    def stop(self) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ── zdarzenia HA ──

    @callback
    def _on_trigger(self, event) -> None:
        try:
            ctx = getattr(event, "context", None)
            self._tracker.note_trigger(getattr(ctx, "id", None), (event.data or {}).get("entity_id"),
                                       self._clock())
        except Exception as err:  # noqa: BLE001 — obserwator nie może wywrócić pętli zdarzeń HA
            _LOGGER.warning("Volcast control: automation run check failed (%s)", type(err).__name__)

    @callback
    def _on_call(self, event) -> None:
        try:
            service_data = (event.data or {}).get("service_data")
            if not isinstance(service_data, dict) or "entity_id" not in service_data:
                return
            ctx = getattr(event, "context", None)
            found = self._tracker.note_call(getattr(ctx, "id", None), getattr(ctx, "parent_id", None),
                                            service_data["entity_id"], self._ex.write_entity_ids, self._clock())
        except Exception as err:  # noqa: BLE001 — obserwator nie może wywrócić pętli zdarzeń HA
            _LOGGER.warning("Volcast control: service call check failed (%s)", type(err).__name__)
            return
        if found is not None:
            self._schedule_refresh()

    @callback
    def _on_cycle(self) -> None:
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if not self._stopped:
            self._hass.async_create_task(self.async_refresh())

    # ── dowody spoza HA i wybór właściciela ──

    async def async_set_box_active(self, active: bool) -> None:
        """`box_active` z planu (konto ma Box ze sterowaniem)."""
        active = active is True
        if active != self._box_active:
            self._box_active = active
            await self.async_refresh()

    async def async_acknowledge(self) -> None:
        """Wybór `volcast`: obecne konflikty nie zatrzymują już drabiny (trwale, także po restarcie)."""
        acked = [[k, label] for k, label in self._pairs]
        acked += [p for p in (getattr(self._ex, "conflict_ack", None) or ()) if list(p) not in acked]
        await self._ex.async_save_conflict_ack(acked)

    # ── przeliczenie ──

    async def async_refresh(self) -> None:
        if self._stopped:
            return
        now = self._clock()
        try:
            payload = controllers_from_evidence(self._tracker.counts(now), tuple(self._clashes() or ()),
                                                self._lan_client(), self._box_active)
        except Exception as err:  # noqa: BLE001 — konflikty nie psują sterowania
            _LOGGER.warning("Volcast control: controller conflict check failed (%s)", type(err).__name__)
            return
        pairs = tuple((c["kind"], c["label"]) for c in payload)
        prev_pairs, prev_payload = self._pairs, self._payload
        self._pairs, self._payload = pairs, payload
        plan_only = getattr(self._ex, "plan_only", False) is True
        self._update_issue(payload, plan_only, changed=pairs != prev_pairs)
        if pairs != prev_pairs or (payload != prev_payload and (
                self._signalled_at is None or now - self._signalled_at >= SIGNAL_EVERY_S)):
            self._signalled_at = now
            async_dispatcher_send(self._hass, SIGNAL_CONTROL_STATE_UPDATED.format(entry_id=self._entry.entry_id))
        acked = {tuple(p) for p in (getattr(self._ex, "conflict_ack", None) or ())}
        new = [p for p in pairs if p not in prev_pairs and p not in acked]
        if new and not plan_only and self._verification is not None:
            _LOGGER.warning("Volcast control: another controller detected (%s)",
                            ", ".join(sorted({kind for kind, _ in new})))
            await self._verification.async_conflict()

    def _update_issue(self, payload: list[dict], plan_only: bool, *, changed: bool) -> None:
        if payload and not plan_only:
            if changed or not self._issue_open:
                first = next((c for c in payload if c["kind"] == AUTOMATION), payload[0])
                # Parametr tekstu Napraw zostaje lokalnie w UI — to nie jest log.
                ir.async_create_issue(self._hass, DOMAIN, self._issue_id, is_fixable=True, severity=_WARNING,
                                      translation_key=ISSUE_CONTROLLER_CONFLICT,
                                      translation_placeholders={"kind": first["kind"], "label": first["label"]},
                                      data={"entry_id": self._entry.entry_id})
                self._issue_open = True
        elif self._issue_open:
            self._issue_open = False
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
