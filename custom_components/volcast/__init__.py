"""The Volcast Solar Forecast integration."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change

from .cloud.client import Backend
from .const import (
    ATTR_DATE,
    CONF_API_URL,
    CONF_BACKEND,
    CONF_BATTERY_CHARGE_POWER_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_MODE,
    CONF_PV_ENERGY_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_API_URL,
    DEFAULT_SUBMIT_URL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MODE_DISCOVERY_ONLY,
    OPT_LOAD_ENERGY,
    SERVICE_SYNC_PRODUCTION,
)
from .control.history_import import async_import_history_once
from .control.runtime import (RELOAD_FREE_KEYS, async_remove_control, async_restore_if_control_changed,
                              async_setup_control, async_unload_control, changed_option_keys,
                              control_options_changed, freeze_control)
from .coordinator import VolcastCoordinator
from .discovery_runner import DiscoveryRunner
from .frontend import async_register_card, async_register_panel, async_remove_panel
from .key_format import account_unique_id, is_legacy_unique_id
from .production import VolcastProductionTracker
from .reconciler import DailyReconciler
from .version import async_integration_version

_LOGGER = logging.getLogger(__name__)

# Waga zgłoszeń naprawy — z rejestru zgłoszeń (komponent `repairs` jej nie re-eksportuje).
IssueSeverity = getattr(ir, "IssueSeverity", None)
_ISSUE_WARNING = getattr(IssueSeverity, "WARNING", "warning")

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

# Wpis bez konta (tylko wykrywanie) — bez koordynatora/trackera/reconcilera,
# więc tylko encje, które czytają raport wykrywania.
DISCOVERY_ONLY_PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

# Wpis sparowany z kontem, gdy sterowanie się złożyło — dochodzi lokalny wyłącznik.
PAIRED_PLATFORMS: list[Platform] = [*PLATFORMS, Platform.SWITCH]

# Klucz w hass.data[DOMAIN][entry_id]: platformy faktycznie przekazane przy setupie.
# Unload zdejmuje dokładnie je — dane wpisu mogą się zmienić przed przeładowaniem
# (parowanie zamienia wpis „tylko rozpoznanie" w wpis konta).
DATA_PLATFORMS = "platforms"


async def _async_forward_platforms(hass: HomeAssistant, entry: ConfigEntry, platforms: list) -> None:
    """Przekaż platformy i zapamiętaj ich listę dla `async_unload_entry`."""
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    hass.data[DOMAIN][entry.entry_id][DATA_PLATFORMS] = list(platforms)


def _loaded_platforms(hass: HomeAssistant, entry: ConfigEntry) -> list:
    """Platformy do zdjęcia: zapamiętane przy setupie, awaryjnie według trybu wpisu."""
    recorded = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get(DATA_PLATFORMS)
    if recorded is not None:
        return list(recorded)
    if entry.data.get(CONF_MODE) == MODE_DISCOVERY_ONLY:
        return DISCOVERY_ONLY_PLATFORMS
    return PLATFORMS


def _migrate_unique_id(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wpis sprzed skrótu miał jawny klucz jako unique_id — zamiana na skrót.

    Wołane przed rejestracją listenera aktualizacji (zmiana nie przeładowuje wpisu).
    Błąd nie blokuje prognozy; bez klucza/unique_id w logu.
    """
    api_key = entry.data.get(CONF_API_KEY)
    if not is_legacy_unique_id(getattr(entry, "unique_id", None)) or not isinstance(api_key, str):
        return
    try:
        hass.config_entries.async_update_entry(entry, unique_id=account_unique_id(api_key))
    except Exception as err:  # noqa: BLE001 — porządek w rejestrze, nie warunek działania
        _LOGGER.warning("Volcast: could not update the entry identifier (%s)", type(err).__name__)


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
    if entry.data.get(CONF_MODE) == MODE_DISCOVERY_ONLY:
        return await _async_setup_discovery_only_entry(hass, entry)

    _migrate_unique_id(hass, entry)

    api_key = entry.data[CONF_API_KEY]
    api_url = entry.data.get(CONF_API_URL, DEFAULT_API_URL)
    update_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

    coordinator = VolcastCoordinator(
        hass, api_key, api_url, update_interval, entry_id=entry.entry_id
    )
    # Load retained past-day forecast history before the first poll so the Energy
    # Dashboard keeps showing previous days even if that first refresh fails.
    await coordinator.async_load_forecast_history()
    backend = Backend.from_dict(entry.data.get(CONF_BACKEND))
    if backend is None:
        await coordinator.async_config_entry_first_refresh()
    else:
        # Wpis sparowany: sterowanie nie może czekać na prognozę. Bez chmury (brak sieci,
        # 401, 503…) wykonawca i tak startuje z planu z magazynu i potrafi oddać falownik;
        # encje prognozy są niedostępne, a koordynator ponawia sam co `update_interval`.
        await coordinator.async_refresh()

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
        if backend is not None:
            # Wpis sparowany zawsze woła backend swojego konta.
            submit_url = backend.submit_production

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
        ir.async_create_issue(
            hass,
            DOMAIN,
            "production_tracking_available",
            is_fixable=False,
            severity=_ISSUE_WARNING,
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

    # Wykrywanie instalacji (tylko odczyt) — runner musi istnieć przed platformami,
    # bo dają mu encje; sam przebieg startuje dopiero po ich załadowaniu.
    runner = DiscoveryRunner(hass, entry.entry_id, await async_integration_version(hass))
    hass.data[DOMAIN][entry.entry_id]["discovery"] = runner

    _async_register_services(hass)

    control = None
    if entry.data.get(CONF_BACKEND):
        try:
            # Nieudane złożenie samo zatrzymuje wykonawcę (`async_setup_control`).
            control = await async_setup_control(hass, entry, report=lambda: runner.report)
        except Exception as err:  # noqa: BLE001 — sterowanie nigdy nie psuje prognozy
            _LOGGER.error("Volcast control could not be set up (%s) — forecast continues",
                          type(err).__name__)
            control = None
    hass.data[DOMAIN][entry.entry_id]["control"] = control

    try:
        await _async_forward_platforms(hass, entry, PAIRED_PLATFORMS if control else PLATFORMS)
    except BaseException:
        # HA nie woła `async_unload_entry` po nieudanym setupie — wykonawca nie może zostać.
        if control is not None:
            hass.data[DOMAIN][entry.entry_id]["control"] = None
            try:
                await async_unload_control(hass, control)
            except Exception as err:  # noqa: BLE001 — pierwotny błąd jest ważniejszy
                _LOGGER.warning("Volcast control: cleanup after a failed setup failed (%s)",
                                type(err).__name__)
        raise

    if control is not None:
        version = await async_integration_version(hass)
        if await async_register_card(hass, version) is not None:
            plan_entity = er.async_get(hass).async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_control_plan")
            # Bez encji w rejestrze nie ma czym skonfigurować panelu (pusta konfiguracja
            # nadpisywałaby domyślną encję karty wartością `null`) — pomijamy rejestrację
            # zamiast wystawiać panel bez treści.
            if plan_entity:
                hass.data[DOMAIN][entry.entry_id]["panel"] = await async_register_panel(
                    hass, plan_entity, version)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _schedule_discovery(hass, entry, runner)

    return True


async def _async_setup_discovery_only_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an account-less entry — only the read-only installation discovery.

    Bez klucza API nie ma czego pytać o prognozę, więc pomijamy koordynator,
    tracker produkcji, reconciler i repair issue prognozy — tylko `DiscoveryRunner`
    i platformy, które wystawiają jego raport (sensor + przycisk ręcznego uruchomienia).
    """
    runner = DiscoveryRunner(hass, entry.entry_id, await async_integration_version(hass))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"discovery": runner}

    await _async_forward_platforms(hass, entry, DISCOVERY_ONLY_PLATFORMS)

    _schedule_discovery(hass, entry, runner)

    return True


DISCOVERY_TASK_NAME = "volcast_discovery"


async def _run_discovery_safely(runner: DiscoveryRunner) -> None:
    # async_run z założenia nie rzuca; ta osłona to druga linia obrony,
    # żeby wykrywanie nigdy nie zostawiło nieobsłużonego wyjątku w zadaniu.
    try:
        await runner.async_run()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Volcast discovery failed")


def _schedule_discovery(
    hass: HomeAssistant, entry: ConfigEntry, runner: DiscoveryRunner
) -> None:
    """Uruchom wykrywanie w tle: od razu (HA działa) albo po starcie HA.

    Wołane PO `async_forward_entry_setups` i nigdy nie awaitowane w setupie —
    błąd wykrywania nie może wpłynąć na wpis prognozy. Zadanie w tle wpisu:
    start HA na nie nie czeka, a unload/reload wpisu je anuluje (stary i nowy
    przebieg nie nakładają się). Argumenty pozycyjne, bez `eager_start` —
    zgodność z HA 2024.1.
    """

    def _start() -> None:
        entry.async_create_background_task(
            hass, _run_discovery_safely(runner), DISCOVERY_TASK_NAME)

    try:
        if hass.is_running:
            _start()
            return

        # Ta sama osłona flagą co w _setup_reconciler: listener async_listen_once
        # sam się wyrejestrowuje, więc remove tylko gdy unload przed startem HA.
        listener_fired = False

        async def _on_started(_event=None) -> None:
            nonlocal listener_fired
            listener_fired = True
            try:
                _start()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Volcast discovery could not be scheduled")

        remove_listener = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _on_started
        )

        def _safe_remove() -> None:
            if not listener_fired:
                remove_listener()

        entry.async_on_unload(_safe_remove)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Volcast discovery could not be scheduled")


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
    platforms = _loaded_platforms(hass, entry)
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, platforms):
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        tracker = entry_data.get("tracker")
        if tracker is not None:
            await tracker.async_stop()
        control = entry_data.get("control")
        if control is not None:
            try:
                # Zwykłe przeładowanie/restart nie oddaje falownika (nowy wykonawca i tak
                # czeka na blokadę wpisu). Jawne wyłączenie wpisu przez właściciela
                # (`entry.disabled_by`) to co innego — to decyzja "wyłącz Volcast", więc
                # oddajemy sterowanie, zanim wykonawca się zatrzyma.
                await async_unload_control(hass, control, restore=bool(entry.disabled_by))
            finally:
                # Usuwamy panel tylko wtedy, gdy naprawdę wystawiliśmy go przy setupie —
                # inaczej HA loguje ostrzeżenie o nieznanym panelu przy każdym przeładowaniu.
                if entry_data.get("panel"):
                    async_remove_panel(hass)
        if not hass.data[DOMAIN] and hass.services.has_service(
            DOMAIN, SERVICE_SYNC_PRODUCTION
        ):
            # Wpisy tylko-rozpoznanie nigdy nie rejestrują tego serwisu — bez
            # strażnika HA loguje ostrzeżenie o usuwaniu nieznanego serwisu.
            hass.services.async_remove(DOMAIN, SERVICE_SYNC_PRODUCTION)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration.

    Zmiana sterowania (sposób, profil, integracja falownika) najpierw oddaje falownik
    przez OBECNEGO wykonawcę. Zmiana samego czujnika zużycia domu nie przeładowuje
    wpisu — uruchamia tylko jednorazowy import historii.
    """
    control = (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get("control")
    if control is not None:
        old, new = control.options_at_setup, dict(entry.options)
        changed = changed_option_keys(old, new)
        if changed and changed <= RELOAD_FREE_KEYS:
            control.options_at_setup = new
            if new.get(OPT_LOAD_ENERGY):
                entry.async_create_background_task(
                    hass, async_import_history_once(
                        hass, control.cloud, control.executor, load_entity=new[OPT_LOAD_ENERGY],
                        pv_entity=new.get(CONF_PV_ENERGY_ENTITY) or None, now_utc=dt_util.utcnow()),
                    "volcast_history_import")
            return
        if control_options_changed(old, new):
            # Najpierw zamrożenie: cykl startujący między powrotem a zatrzymaniem
            # (licznik, pobranie planu, wyłącznik) pisałby jeszcze przez STARE mapowanie.
            freeze_control(control)
        await async_restore_if_control_changed(control, old, new)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Usunięcie wpisu: sterowanie oddaje falownik w tryb bazowy (jeśli je przejęło)."""
    try:
        await async_remove_control(hass, entry)
    except Exception as err:  # noqa: BLE001 — usunięcie wpisu nie może się wywrócić
        _LOGGER.error("Volcast: control cleanup on removal failed (%s)", type(err).__name__)
