"""The Volcast Solar Forecast integration."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging

try:
    from homeassistant.components.repairs import IssueSeverity
except ImportError:
    IssueSeverity = None
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change

from .const import (
    ATTR_DATE,
    CONF_API_URL,
    CONF_BATTERY_CHARGE_POWER_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_API_URL,
    DEFAULT_SUBMIT_URL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    SERVICE_SYNC_PRODUCTION,
)
from .coordinator import VolcastCoordinator
from .production import VolcastProductionTracker
from .reconciler import DailyReconciler

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]


def _async_register_services(hass: HomeAssistant) -> None:
    """Zarejestruj domain-level serwis volcast.sync_production (idempotentnie).

    Bez `date` → reconcile_recent() (wczoraj + dziś) na wszystkich entries.
    Z `date` (YYYY-MM-DD lub datetime.date z selectora) → reconcile_day(date);
    daty poza oknem odbija istniejący gate `out_of_window` w reconcile_day.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION):
        return

    async def _handle_sync_production(call: ServiceCall) -> None:
        raw_date = call.data.get(ATTR_DATE)
        target: date | None = None
        if raw_date is not None:
            if isinstance(raw_date, datetime):
                # datetime dziedziczy po date — sprowadź do czystej daty,
                # inaczej date - datetime rzuci TypeError w reconcile_day
                # (połknięty w success=False = cichy no-op zamiast błędu).
                target = raw_date.date()
            elif isinstance(raw_date, date):
                target = raw_date
            else:
                try:
                    target = date.fromisoformat(str(raw_date))
                except ValueError as err:
                    raise ServiceValidationError(
                        f"Invalid date {raw_date!r} — expected YYYY-MM-DD"
                    ) from err

        reconcilers = [
            entry_data["reconciler"]
            for entry_data in hass.data.get(DOMAIN, {}).values()
            if entry_data.get("reconciler") is not None
        ]
        if not reconcilers:
            raise ServiceValidationError(
                "No Volcast entry has production tracking configured "
                "(an energy sensor is required for sync)"
            )
        for reconciler in reconcilers:
            if target is not None:
                await reconciler.reconcile_day(target)
            else:
                await reconciler.reconcile_recent()

    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_PRODUCTION, _handle_sync_production
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Volcast from a config entry."""
    api_key = entry.data[CONF_API_KEY]
    api_url = entry.data.get(CONF_API_URL, DEFAULT_API_URL)
    update_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

    coordinator = VolcastCoordinator(
        hass, api_key, api_url, update_interval, entry_id=entry.entry_id
    )
    # Load retained past-day forecast history before the first poll so the Energy
    # Dashboard keeps showing previous days even if that first refresh fails.
    await coordinator.async_load_forecast_history()
    await coordinator.async_config_entry_first_refresh()

    # Production tracker — opcjonalny (wymaga skonfigurowanych sensorów)
    energy_entity = entry.options.get(CONF_PV_ENERGY_ENTITY, "")
    power_entity = entry.options.get(CONF_PV_POWER_ENTITY, "")
    battery_soc_entity = entry.options.get(CONF_BATTERY_SOC_ENTITY, "")
    battery_charge_power_entity = entry.options.get(CONF_BATTERY_CHARGE_POWER_ENTITY, "")

    tracker: VolcastProductionTracker | None = None
    if energy_entity or power_entity:
        # submit_url z odpowiedzi API (jeśli dostępny) lub domyślny
        submit_url = DEFAULT_SUBMIT_URL
        if coordinator.data and coordinator.data.submit_url:
            submit_url = coordinator.data.submit_url

        tracker = VolcastProductionTracker(
            hass=hass,
            api_key=api_key,
            submit_url=submit_url,
            energy_entity=energy_entity,
            power_entity=power_entity,
            battery_soc_entity=battery_soc_entity,
            battery_charge_power_entity=battery_charge_power_entity,
            system_capacity_kwp=(
                coordinator.data.system_capacity_kwp if coordinator.data else None
            ),
        )
        await tracker.async_start()

        # Wyczyść ewentualny repair issue (użytkownik już skonfigurował)
        ir.async_delete_issue(hass, DOMAIN, "production_tracking_available")
    else:
        if IssueSeverity is not None:
            ir.async_create_issue(
                hass,
                DOMAIN,
                "production_tracking_available",
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="production_tracking_available",
            )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "tracker": tracker,
    }

    # Daily reconciler — tylko jeśli mamy energy_entity (recorder potrzebny).
    reconciler: DailyReconciler | None = None
    if energy_entity and tracker is not None:
        # submit_url już policzone wyżej (linia ~57) — używamy tej samej wartości
        # zamiast sięgać do tracker._submit_url (private attribute).
        reconciler = _setup_reconciler(
            hass=hass,
            entry=entry,
            tracker=tracker,
            energy_entity=energy_entity,
            api_key=api_key,
            submit_url=submit_url,
        )
        hass.data[DOMAIN][entry.entry_id]["reconciler"] = reconciler
    else:
        _LOGGER.info(
            "Reconciler not started — energy_entity not configured (or tracker missing)"
        )

    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


def _setup_reconciler(
    *,
    hass: HomeAssistant,
    entry: ConfigEntry,
    tracker: VolcastProductionTracker,
    energy_entity: str,
    api_key: str,
    submit_url: str,
) -> DailyReconciler:
    """Stwórz DailyReconciler i podłącz dwa triggery: 00:30 codziennie + na startup.

    Wyodrębnione z async_setup_entry żeby można je było pokryć testem bez
    konieczności stubowania całego setupu integracji (config_entries
    forward, coordinator first refresh, tracker.async_start, etc.).
    """
    reconciler = DailyReconciler(
        hass=hass,
        tracker=tracker,
        energy_entity=energy_entity,
        api_key=api_key,
        submit_url=submit_url,
    )

    # Codzienny przebieg — 00:30 lokalnego czasu (po północy → wczorajszy dzień
    # już zamknięty w recorder, backend jeszcze przyjmuje wpisy z dnia D-1
    # (36h window)).
    async def _scheduled_reconcile(_now):
        target = (datetime.now(reconciler._tz) - timedelta(days=1)).date()
        await reconciler.reconcile_day(target)

    entry.async_on_unload(
        async_track_time_change(
            hass, _scheduled_reconcile, hour=0, minute=30, second=0,
        )
    )

    # Na startupie HA — uzgodnij wczoraj + dziś. Restart HA to dokładnie
    # moment, w którym powstają luki (update systemu = restart). Idempotentne:
    # godziny już dostarczone i bieżąca godzina (własność live trackera) są
    # pomijane wewnątrz reconcile_recent/reconcile_day.
    async def _on_started(_event=None):
        await reconciler.reconcile_recent()

    if hass.is_running:
        hass.async_create_task(_on_started())
    else:
        # async_listen_once samodzielnie wyrejestrowuje listener po fire'owaniu.
        # Naiwne `async_on_unload(remove)` powoduje przy unloadzie (np. HACS upgrade)
        # próbę usunięcia już-usuniętego listenera → "Unable to remove unknown job
        # listener". Trzymamy flagę żeby wywołać remove tylko gdy unload nastąpi
        # przed startem HA.
        listener_fired = False

        async def _on_started_tracked(event=None):
            nonlocal listener_fired
            listener_fired = True
            await _on_started(event)

        remove_listener = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _on_started_tracked
        )

        def _safe_remove() -> None:
            if not listener_fired:
                remove_listener()

        entry.async_on_unload(_safe_remove)

    return reconciler


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        tracker = entry_data.get("tracker")
        if tracker is not None:
            await tracker.async_stop()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SYNC_PRODUCTION)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)
