"""Pobieranie planu (pull co 5 min). Błąd sieci nie kasuje planu lokalnego; zły
plan jest odrzucany w całości; zgoda z tej samej odpowiedzi jest stosowana nawet
przy odrzuconym planie (cofnięcie zgody nie może czekać na poprawny plan).
Pole `discovery_request` (raport rozpoznania na żądanie) jest obsługiwane PO planie — wysyłka
nie opóźnia zgody ani planu, a jej błąd nie wywraca odświeżenia.
Deduplikacja po `schedule_id` ORAZ treści — id bywa to samo przy innej treści.

Odrzucony plan loguje ostrzeżenie raz na treść (chmura serwuje go co 5 min).
Wyjątek klienta albo callbacku kończy przebieg wynikiem "error" (sama nazwa klasy
w logu) — wołający po odświeżeniu zawsze robi swoje (np. cykl wykonawcy).
Plan, którego callback się nie powiódł, nie jest zapamiętany — następny przebieg
poda go jeszcze raz."""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from homeassistant.helpers import issue_registry as ir

from ..const import DOMAIN
from ..core.slot import InvalidSchedule, Schedule, parse_schedule
from ..version import async_integration_version
from .client import CloudAuthError
from .discovery_report import DiscoveryReportSender

try:
    from packaging.version import InvalidVersion, Version
except ImportError:  # HA dostarcza `packaging`; bez niego porównujemy tylko równość
    Version = None

_LOGGER = logging.getLogger(__name__)
SCHEDULE_FETCH_INTERVAL_S = 300
UPDATE_ISSUE_ID = "update_available"
# Waga zgłoszeń — z rejestru zgłoszeń (jak w `__init__`).
_ISSUE_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")
# Pola odpowiedzi poza treścią planu: ich zmiana nie wywołuje ponownej akceptacji planu.
_NOT_IN_SIGNATURE = ("control_enabled", "signals", "control", "latest_version", "plan_meta",
                     "discovery_request")


def _is_newer(latest: str, current: str) -> bool:
    if Version is not None:
        try:
            return Version(latest) > Version(current)
        except InvalidVersion:
            pass
    return latest != current


class ScheduleFetcher:
    def __init__(self, cloud, *, on_plan: Callable[[dict, Schedule], Awaitable[None]],
                 on_consent: Callable[[bool], Awaitable[None]],
                 on_auth_failure: Callable[[int], Awaitable[None]],
                 on_signals: Callable[[dict | None], Awaitable[None]] | None = None,
                 on_control: Callable[[dict | None], Awaitable[None]] | None = None,
                 on_consent_mirror: Callable[[bool], None] | None = None,
                 hass=None, discovery_report: Callable[[], dict | None] | None = None) -> None:
        self._hass = hass   # tylko do zgłoszenia „dostępna aktualizacja"
        self._cloud = cloud
        self._on_plan = on_plan
        self._on_consent = on_consent
        self._on_auth_failure = on_auth_failure
        self._on_signals = on_signals
        self._on_control = on_control
        self._on_consent_mirror = on_consent_mirror
        # `control.consent_mirror` z ostatniej odpowiedzi (lustro zgody konta); brak/nie-`true` = wył.
        self.consent_mirror = False
        self._auth_failures = 0
        self._last_signature: str | None = None
        self._last_rejected: str | None = None
        self._last_error: str | None = None
        # Ostatni raport wykrywania (bez nowego przebiegu) — wysyłany, gdy plan niesie `discovery_request`.
        self._discovery = (DiscoveryReportSender(hass, cloud, discovery_report)
                           if discovery_report is not None else None)

    async def async_refresh(self) -> str:
        try:
            result = await self._async_refresh()
        except Exception as err:  # noqa: BLE001 — odświeżenie planu nie może wywrócić wołającego
            name = type(err).__name__
            if name != self._last_error:
                _LOGGER.warning("Volcast plan refresh failed (%s)", name)
            else:
                _LOGGER.debug("Volcast plan refresh failed again (%s)", name)
            self._last_error = name
            return "error"
        self._last_error = None
        return result

    async def _async_refresh(self) -> str:
        try:
            raw = await self._cloud.async_get_schedule()
        except CloudAuthError:
            self._auth_failures += 1
            await self._on_auth_failure(self._auth_failures)
            return "auth"
        if raw is None:
            return "network"
        self._auth_failures = 0
        await self._async_signals(raw)
        await self._async_control(raw)
        self._apply_consent_mirror(raw)          # PRZED zgodą: zgoda z tej odpowiedzi idzie już pod lustrem
        await self._async_update_issue(raw.get("latest_version"))
        consent = raw.get("control_enabled")
        if isinstance(consent, bool):
            await self._on_consent(consent)
        result = await self._async_plan(raw)
        await self._async_discovery(raw)
        return result

    async def _async_plan(self, raw: dict) -> str:
        body = {k: v for k, v in raw.items() if k not in _NOT_IN_SIGNATURE}
        signature = json.dumps(body, sort_keys=True, default=str)
        if signature == self._last_signature:
            return "unchanged"
        try:
            schedule = parse_schedule(raw)
        except InvalidSchedule as err:
            if signature != self._last_rejected:
                _LOGGER.warning("Volcast plan rejected (%s) — keeping the previous plan", err.field)
            else:
                _LOGGER.debug("Volcast plan still rejected (%s)", err.field)
            self._last_rejected = signature
            return "rejected"
        # Bloki `signals` i `control` zostają przy swoich odbiorcach — nie trafiają do planu w magazynie.
        await self._on_plan({k: v for k, v in raw.items()
                             if k not in ("signals", "control", "discovery_request")}, schedule)
        self._last_signature = signature
        self._last_rejected = None
        return "accepted"

    async def _async_discovery(self, raw: dict) -> None:
        """Raport rozpoznania na żądanie chmury; jego błąd (także pamięci w Store) nie wywraca odświeżenia."""
        if self._discovery is None:
            return
        try:
            await self._discovery.async_handle(raw.get("discovery_request"))
        except Exception as err:  # noqa: BLE001 — wysyłka raportu nie może zatrzymać planu
            _LOGGER.debug("Volcast discovery report failed (%s)", type(err).__name__)

    def _apply_consent_mirror(self, raw: dict) -> None:
        """Lustro zgody tylko przy jawnym `control.consent_mirror: true`; błąd odbiorcy nie wywraca odświeżenia."""
        block = raw.get("control")
        self.consent_mirror = isinstance(block, dict) and block.get("consent_mirror") is True
        if self._on_consent_mirror is None:
            return
        try:
            self._on_consent_mirror(self.consent_mirror)
        except Exception as err:  # noqa: BLE001 — odbiorca flagi nie może zatrzymać planu
            _LOGGER.debug("Volcast consent mirror callback failed (%s)", type(err).__name__)

    async def _async_signals(self, raw: dict) -> None:
        """Blok `signals` do odbiorcy PRZED deduplikacją; jego błąd nie wywraca odświeżenia."""
        if self._on_signals is None:
            return
        block = raw.get("signals")
        try:
            await self._on_signals(block if isinstance(block, dict) else None)
        except Exception as err:  # noqa: BLE001 — callback sygnałów nie może zatrzymać planu
            _LOGGER.debug("Volcast signals callback failed (%s)", type(err).__name__)

    async def _async_control(self, raw: dict) -> None:
        """Blok `control` (decyzje właściciela, `box_active`) do odbiorcy PRZED deduplikacją; jego błąd
        nie wywraca odświeżenia."""
        if self._on_control is None:
            return
        block = raw.get("control")
        try:
            await self._on_control(block if isinstance(block, dict) else None)
        except Exception as err:  # noqa: BLE001 — callback decyzji nie może zatrzymać planu
            _LOGGER.debug("Volcast control callback failed (%s)", type(err).__name__)

    async def _async_update_issue(self, latest) -> None:
        """Zgłoszenie `update_available`, gdy chmura zna nowszą wersję integracji niż zainstalowana;
        znika, gdy wersje się zrównają albo pole zniknie. Jego błąd nie wywraca odświeżenia."""
        if self._hass is None:
            return
        try:
            current = await async_integration_version(self._hass)
            if isinstance(latest, str) and latest and current != "unknown" and _is_newer(latest, current):
                ir.async_create_issue(
                    self._hass, DOMAIN, UPDATE_ISSUE_ID, is_fixable=False, severity=_ISSUE_WARNING,
                    translation_key=UPDATE_ISSUE_ID,
                    translation_placeholders={"latest_version": latest, "current_version": current})
            else:
                ir.async_delete_issue(self._hass, DOMAIN, UPDATE_ISSUE_ID)
        except Exception as err:  # noqa: BLE001 — zgłoszenie jest informacyjne
            _LOGGER.debug("Volcast update issue failed (%s)", type(err).__name__)
