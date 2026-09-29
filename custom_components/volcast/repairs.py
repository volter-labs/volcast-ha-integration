"""Naprawy: „Wznów sterowanie teraz" w zgłoszeniu pauzy przejęcia (`foreign_control`).

Potwierdzenie kończy pauzę bez przeładowania wpisu (przeładowanie przywracałoby nastawy
i pisało plan od nowa — zbędne zapisy NVM); najbliższy cykl od razu wykonuje plan.
Wpis już rozładowany nie ma czego wznawiać — naprawa tylko zamyka zgłoszenie. Falownik
wciąż w trybie spoza profilu: zapisy i tak stoją, więc naprawa się przerywa (zgłoszenie
zostaje) i mówi to właścicielowi.
"""
from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow

from .control.runtime import async_resume_control

_FOREIGN_PREFIX = "foreign_control_"


class ResumeControlFlow(ConfirmRepairFlow):
    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is not None:
            results = await async_resume_control(self.hass, self._entry_id) or []
            if "foreign_mode" in results:
                return self.async_abort(reason="foreign_mode")
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(hass, issue_id: str, data: dict | None) -> RepairsFlow:
    if issue_id.startswith(_FOREIGN_PREFIX):
        entry_id = (data or {}).get("entry_id") or issue_id[len(_FOREIGN_PREFIX):]
        return ResumeControlFlow(str(entry_id))
    return ConfirmRepairFlow()
