"""Naprawy: „Wznów sterowanie teraz" w zgłoszeniu pauzy przejęcia (`foreign_control`).

Potwierdzenie kończy pauzę bez przeładowania wpisu (przeładowanie przywracałoby nastawy
i pisało plan od nowa — zbędne zapisy NVM); najbliższy cykl od razu wykonuje plan.
Wpis już rozładowany nie ma czego wznawiać — naprawa tylko zamyka zgłoszenie. Falownik
wciąż w trybie spoza profilu: zapisy i tak stoją, więc naprawa się przerywa (zgłoszenie
zostaje) i mówi to właścicielowi.

Mapa czujników (`sensor_assignment_missing_`): formularz z propozycją tych samych pól co krok opcji
„Installation details” (kod wspólny w `config_flow`). Do zatwierdzenia nic się nie zapisuje; zapis
zmienia tylko opcje mapy (reszta zostaje), a przeładowanie wpisu robi istniejący słuchacz opcji.
Naprawa nie dotyka sterowania.
"""
from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
import voluptuous as vol

from .const import DOMAIN
from .control.runtime import async_resume_control

_FOREIGN_PREFIX = "foreign_control_"
_CONFLICT_PREFIX = "controller_conflict_"
_STOPPED_PREFIX = "verification_stopped_"
_ASSIGNMENT_PREFIX = "sensor_assignment_missing_"


class ResumeControlFlow(ConfirmRepairFlow):
    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is not None:
            results = await async_resume_control(self.hass, self._entry_id) or []
            if "foreign_mode" in results:
                return self.async_abort(reason="foreign_mode")
        return await super().async_step_confirm(user_input)


def _runtime(hass, entry_id: str):
    return ((getattr(hass, "data", None) or {}).get(DOMAIN, {}).get(entry_id) or {}).get("control")


class RetryVerificationFlow(ConfirmRepairFlow):
    """Zatrzymana weryfikacja urządzenia: potwierdzenie wznawia drabinę (to samo co w aplikacji)."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is not None:
            rt = _runtime(self.hass, self._entry_id)
            if rt is not None and rt.verification is not None:
                await rt.verification.async_retry()
        return await super().async_step_confirm(user_input)


class ControllerConflictFlow(RepairsFlow):
    """Konflikt sterowników: wybór, kto steruje falownikiem (to samo co wybór sterownika w aplikacji)."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None):
        return self.async_show_menu(step_id="init", menu_options=["volcast", "own_ems"])

    async def _choose(self, controller: str):
        rt = _runtime(self.hass, self._entry_id)
        if rt is None:
            return self.async_abort(reason="not_loaded")
        if await rt.async_apply_controller_choice(controller) == "restore_failed":
            return self.async_abort(reason="restore_failed")
        return self.async_create_entry(data={})

    async def async_step_volcast(self, user_input: dict[str, str] | None = None):
        return await self._choose("volcast")

    async def async_step_own_ems(self, user_input: dict[str, str] | None = None):
        return await self._choose("own_ems")


class SensorAssignmentFlow(RepairsFlow):
    """Brak rozpoznanego falownika: formularz mapy czujników z propozycją do sprawdzenia i zatwierdzenia."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    def _entry(self):
        """Wpis, który jest wczytany — rozładowany albo usunięty nie ma czego mapować."""
        loaded = (getattr(self.hass, "data", None) or {}).get(DOMAIN, {})
        if self._entry_id not in loaded:
            return None
        return self.hass.config_entries.async_get_entry(self._entry_id)

    async def async_step_init(self, user_input: dict[str, str] | None = None):
        return await self.async_step_assign()

    async def async_step_assign(self, user_input: dict | None = None):
        # Import na żądanie: formularz mapy jest wspólny z krokiem opcji, a pozostałe naprawy go nie potrzebują.
        from .config_flow import async_load_assignment, merge_options, sensor_map_fields, sensor_map_patch
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            options = merge_options(entry.options, sensor_map_patch(user_input))
            self.hass.config_entries.async_update_entry(entry, options=options)
            return self.async_create_entry(data={})
        assignment, placeholders = await async_load_assignment(self.hass, entry)
        return self.async_show_form(
            step_id="assign", data_schema=vol.Schema(sensor_map_fields(entry.options, assignment)),
            description_placeholders=placeholders or {"candidates": "", "sign_check": ""})


async def async_create_fix_flow(hass, issue_id: str, data: dict | None) -> RepairsFlow:
    if issue_id.startswith(_FOREIGN_PREFIX):
        entry_id = (data or {}).get("entry_id") or issue_id[len(_FOREIGN_PREFIX):]
        return ResumeControlFlow(str(entry_id))
    if issue_id.startswith(_CONFLICT_PREFIX):
        return ControllerConflictFlow(str((data or {}).get("entry_id") or issue_id[len(_CONFLICT_PREFIX):]))
    if issue_id.startswith(_STOPPED_PREFIX):
        return RetryVerificationFlow(str((data or {}).get("entry_id") or issue_id[len(_STOPPED_PREFIX):]))
    if issue_id.startswith(_ASSIGNMENT_PREFIX):
        return SensorAssignmentFlow(str((data or {}).get("entry_id") or issue_id[len(_ASSIGNMENT_PREFIX):]))
    return ConfirmRepairFlow()
