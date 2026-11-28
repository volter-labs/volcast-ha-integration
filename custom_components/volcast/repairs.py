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

Brak sieci / zużycia domu przy rozpoznanym falowniku (`telemetry_channels_missing_`): formularz tylko z pól
kanałów, których telemetria nie czyta (te same etykiety co krok „Installation details”). Zapis DOKŁADA czujniki do
mapy ręcznej (reszta mapy i opcji zostaje) — encja ręczna dotyczy tylko swojego klucza, więc nie zasłania profilu.
Bez podpowiedzi czujników: przy rozpoznanym falowniku propozycji nie pokazujemy (jak w kroku opcji).
"""
from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
import voluptuous as vol

from .const import DOMAIN, OPT_GRID_NEGATE, OPT_TELEMETRY_MAP
from .control.runtime import async_no_inverter_integration_now, async_resume_control

_FOREIGN_PREFIX = "foreign_control_"
_CONFLICT_PREFIX = "controller_conflict_"
_STOPPED_PREFIX = "verification_stopped_"
_ASSIGNMENT_PREFIX = "sensor_assignment_missing_"
_CHANNELS_PREFIX = "telemetry_channels_missing_"
_GRID = "grid_power_w"


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


def _loaded_entry(hass, entry_id: str):
    """Wpis, który jest wczytany — rozładowany albo usunięty nie ma czego mapować."""
    loaded = (getattr(hass, "data", None) or {}).get(DOMAIN, {})
    if entry_id not in loaded:
        return None
    return hass.config_entries.async_get_entry(entry_id)


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
        return _loaded_entry(self.hass, self._entry_id)

    async def async_step_init(self, user_input: dict[str, str] | None = None):
        return await self.async_step_assign()

    async def async_step_assign(self, user_input: dict | None = None):
        # Import na żądanie: formularz mapy jest wspólny z krokiem opcji, a pozostałe naprawy go nie potrzebują.
        from .config_flow import async_load_assignment, merge_options, sensor_map_fields, sensor_map_patch
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        shown_options = entry.options
        if user_input is not None:
            # Naprawa mogła zostać otwarta przed rozpoznaniem falownika (profil dostarczony po starcie albo
            # integracja falownika dodana po setupie) — ręczna mapa zasłoniłaby po przeładowaniu profil, więc przy
            # zatwierdzeniu sprawdzamy to samo co przy podpowiedzi, z wyborem profilu liczonym na żywo.
            control = ((getattr(self.hass, "data", None) or {}).get(DOMAIN, {}).get(self._entry_id) or {}).get("control")
            if not await async_no_inverter_integration_now(self.hass, entry, control):
                return self.async_abort(reason="inverter_recognised")
            patch = sensor_map_patch(user_input)
            options = merge_options(entry.options, patch)
            # Bez mocy PV zgłoszenie wróciłoby po przeładowaniu — formularz zostaje, nic nie jest zapisane.
            if patch[OPT_TELEMETRY_MAP].get("pv_power_w"):
                self.hass.config_entries.async_update_entry(entry, options=options)
                return self.async_create_entry(data={})
            errors["base"] = "pv_power_required"
            shown_options = options        # to, co użytkownik już wybrał, zostaje w polach
        assignment, placeholders = await async_load_assignment(self.hass, entry)
        return self.async_show_form(
            step_id="assign", data_schema=vol.Schema(sensor_map_fields(shown_options, assignment)),
            errors=errors,
            description_placeholders=placeholders or {"candidates": "", "sign_check": ""})


class TelemetryChannelsFlow(RepairsFlow):
    """Rozpoznany falownik bez sieci albo zużycia domu: wybór brakujących czujników, dopisanych do mapy ręcznej."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None):
        return await self.async_step_assign()

    async def async_step_assign(self, user_input: dict | None = None):
        from homeassistant.helpers import selector

        from .config_flow import _entity_field, merge_options
        entry = _loaded_entry(self.hass, self._entry_id)
        if entry is None:
            return self.async_abort(reason="not_loaded")
        telemetry = getattr(_runtime(self.hass, self._entry_id), "telemetry", None)
        missing = tuple(getattr(telemetry, "missing_balance_channels", ()) or ())
        if not missing:
            return self.async_abort(reason="channels_present")
        errors: dict[str, str] = {}
        tmap = dict(entry.options.get(OPT_TELEMETRY_MAP) or {})
        if user_input is not None:
            picked = {k: v for k in missing if (v := (user_input.get(k) or "").strip())}
            if picked:
                patch: dict = {OPT_TELEMETRY_MAP: {**tmap, **picked}}
                # Znak sieci dotyczy tylko czujnika sieci wskazanego ręcznie — bez niego zapisany znak zostaje.
                if _GRID in picked:
                    patch[OPT_GRID_NEGATE] = bool(user_input.get(OPT_GRID_NEGATE)) or None
                self.hass.config_entries.async_update_entry(entry, options=merge_options(entry.options, patch))
                return self.async_create_entry(data={})
            errors["base"] = "channel_required"
        fields: dict = {
            _entity_field(key, tmap.get(key)): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
            for key in missing
        }
        if _GRID in missing:
            fields[vol.Optional(OPT_GRID_NEGATE, default=bool(entry.options.get(OPT_GRID_NEGATE)))] = bool
        return self.async_show_form(step_id="assign", data_schema=vol.Schema(fields), errors=errors)


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
    if issue_id.startswith(_CHANNELS_PREFIX):
        return TelemetryChannelsFlow(str((data or {}).get("entry_id") or issue_id[len(_CHANNELS_PREFIX):]))
    return ConfirmRepairFlow()
