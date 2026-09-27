"""Złożenie sterowania dla wpisu sparowanego z kontem (część zależna od HA).

Zmiana opcji sterowania (sposób sterowania, profil, integracja falownika — od nich
zależy mapowanie encji) przeładowuje wpis. Nowy wykonawca nie może bezpiecznie
przywrócić migawki przez NOWE mapowanie, więc powrót do trybu bazowego robi STARY
wykonawca, zanim wpis się przeładuje: `async_restore_if_control_changed`. Woła go
przepływ opcji przed zapisem i słuchacz aktualizacji wpisu przed przeładowaniem
(względem kopii opcji z chwili złożenia — `ControlRuntime.options_at_setup`).

Kolejni wykonawcy tego samego wpisu dzielą jedną blokadę zapisu: po przeładowaniu nowy
czeka, aż stary skończy zapis w toku, także gdy `async_stop` starego się poddał.
Zaraz po złożeniu idzie jeden cykl (zmiana opcji = przeładowanie = cykl od razu), a po
każdym odświeżeniu planu — następny (zmiana zgody działa od razu).

Błąd złożenia za `executor.async_start()` zatrzymuje wykonawcę i telemetrię, zanim
wyjątek pójdzie dalej — nieudany setup ani przeładowanie nie zostawia żywego wykonawcy.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Mapping

import homeassistant.util.dt as dt_util
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from ..cloud.client import Backend, PairingClient, PairingSession, VolcastCloud
from ..cloud.fetcher import SCHEDULE_FETCH_INTERVAL_S, ScheduleFetcher
from ..const import (BETA_PAIRING_URL, CONF_BACKEND, CONF_PAIRING, CONF_PV_ENERGY_ENTITY, DOMAIN,
                     OPT_BATTERY_CAPACITY_KWH, OPT_CONTROL_MODE, OPT_GRID_NEGATE, OPT_INVERTER_DOMAIN,
                     OPT_LOAD_ENERGY, OPT_PROFILE_ID, OPT_RATED_POWER_W, OPT_TELEMETRY_MAP)
from ..core.control.limits import executor_limits, rated_power_from_model
from ..core.control.select import InverterHint, ProfileChoice, select_profile
from ..core.discovery.known import INVERTER_DOMAINS
from ..core.entity_map import EntityCandidate, resolve_entities
from ..core.profile import ProfileError, builtin_ids, load_builtin
from ..registry_compat import all_devices
from .executor import VolcastExecutor
from .ha_writer import EntityServiceWriter
from .history_import import async_import_history_once
from .store import ControlStore
from .telemetry import TelemetrySender

_LOGGER = logging.getLogger(__name__)
ONBOARDING_KEY = "volcast_onboarding"
# Blokady zapisu per wpis — wspólne dla kolejnych wykonawców (przeładowania).
_LOCKS_KEY = "volcast_control_locks"

# Opcje, od których zależą: czy sterujemy i przez które encje.
CONTROL_OPTION_KEYS = (OPT_CONTROL_MODE, OPT_PROFILE_ID, OPT_INVERTER_DOMAIN)
# Opcje, których zmiana nie wymaga przeładowania wpisu (wystarczy import historii).
RELOAD_FREE_KEYS = frozenset({OPT_LOAD_ENERGY})


@dataclass
class ControlRuntime:
    executor: object
    fetcher: object
    telemetry: object
    cloud: object
    choice: object | None
    mapped: dict[str, str]
    rated_power_w: float | None
    unsubs: list = field(default_factory=list)
    # kopia `entry.options` z chwili złożenia — słuchacz aktualizacji porównuje z nią
    options_at_setup: dict = field(default_factory=dict)
    # encje wpisu konfiguracji / urządzenia zmapowanego falownika (`inverter_entity_ids`)
    inverter_entities: frozenset = frozenset()


def control_options_changed(old: Mapping, new: Mapping) -> bool:
    return any(old.get(k) != new.get(k) for k in CONTROL_OPTION_KEYS)


def changed_option_keys(old: Mapping, new: Mapping) -> set[str]:
    return {k for k in {*old, *new} if old.get(k) != new.get(k)}


async def async_restore_if_control_changed(runtime, old: Mapping, new: Mapping) -> bool:
    """Stary wykonawca przywraca tryb bazowy, gdy zmiana opcji zmienia sterowanie.

    True = powrót wykonany (wykonawca nie jest już właścicielem). Nigdy nie rzuca.
    """
    executor = getattr(runtime, "executor", None)
    if executor is None or not control_options_changed(old, new) or not getattr(executor, "owned", False):
        return False
    try:
        await executor.async_restore_now()
    except Exception as err:  # noqa: BLE001 — zapis opcji nie może się przez to wywrócić
        _LOGGER.warning("Volcast control: return to the baseline before reload failed (%s)",
                        type(err).__name__)
        return False
    return not getattr(executor, "owned", False)


def _entry_lock(hass, entry_id: str) -> asyncio.Lock:
    locks = hass.data.setdefault(_LOCKS_KEY, {})
    lock = locks.get(entry_id)
    if lock is None:
        lock = locks[entry_id] = asyncio.Lock()
    return lock


def _load_profiles() -> list:
    out = []
    for pid in builtin_ids():
        try:
            out.append(load_builtin(pid))
        except ProfileError as err:
            _LOGGER.warning("Volcast profile %s rejected: %s", pid, err)
    return out


def inverter_hints(hass) -> list[InverterHint]:
    out: list[InverterHint] = []
    for dev in all_devices(dr.async_get(hass)):
        if getattr(dev, "disabled_by", None):
            continue
        for ce_id in sorted(getattr(dev, "config_entries", None) or ()):
            ce = hass.config_entries.async_get_entry(ce_id)
            if ce is not None and ce.domain in INVERTER_DOMAINS:
                out.append(InverterHint(ce.domain, dev.manufacturer, dev.model))
    return out


def _choice_for(hass, entry, profiles) -> ProfileChoice | None:
    hints = inverter_hints(hass)
    pid, domain = entry.options.get(OPT_PROFILE_ID), entry.options.get(OPT_INVERTER_DOMAIN)
    if pid and domain:
        prof = next((p for p in profiles if p.id == pid), None)
        if prof is not None:
            model = next((h.model for h in hints if h.domain == domain), None)
            return ProfileChoice(prof, domain, model)
    return select_profile(hints, profiles)


def map_entities(hass, choice: ProfileChoice | None) -> dict[str, str]:
    if choice is None or not choice.integration_domain:
        return {}
    cands = []
    for e in er.async_get(hass).entities.values():
        if e.platform != choice.integration_domain or getattr(e, "disabled_by", None):
            continue
        st = hass.states.get(e.entity_id)
        unit = (st.attributes.get("unit_of_measurement") if st else None) or getattr(e, "unit_of_measurement", None)
        cands.append(EntityCandidate(e.entity_id, e.platform, e.unique_id or "", unit))
    return dict(resolve_entities(choice.profile, choice.integration_domain, cands).mapped)


def inverter_entity_ids(hass, choice: ProfileChoice | None, mapped: Mapping[str, str]) -> frozenset[str]:
    """Encje z tego samego wpisu konfiguracji albo urządzenia co zmapowane encje falownika.

    Tylko platforma integracji falownika; nic zmapowanego = pusty zbiór. Z nich jedynych
    wolno automatycznie wybrać licznik zużycia domu.
    """
    domain = choice.integration_domain if choice else None
    if not domain or not mapped:
        return frozenset()
    entities = [e for e in er.async_get(hass).entities.values() if e.platform == domain]
    targets = set(mapped.values())
    entries = {getattr(e, "config_entry_id", None) for e in entities if e.entity_id in targets} - {None}
    devices = {getattr(e, "device_id", None) for e in entities if e.entity_id in targets} - {None}
    return frozenset(e.entity_id for e in entities
                     if e.entity_id in targets or getattr(e, "config_entry_id", None) in entries
                     or getattr(e, "device_id", None) in devices)


async def async_setup_control(hass, entry, *, report: Callable[[], dict | None]) -> ControlRuntime | None:
    backend = Backend.from_dict(entry.data.get(CONF_BACKEND))
    if backend is None:
        return None
    opts = entry.options
    cloud = VolcastCloud(async_get_clientsession(hass), entry.data[CONF_API_KEY], backend)
    profiles = await hass.async_add_executor_job(_load_profiles)
    choice = _choice_for(hass, entry, profiles)
    mapped = map_entities(hass, choice)
    manual_rated = opts.get(OPT_RATED_POWER_W)
    rated = float(manual_rated) if manual_rated else rated_power_from_model(choice.model if choice else None)
    executor = VolcastExecutor(hass, entry, choice=choice, mapped=mapped, rated_power_w=rated,
                               store=ControlStore(hass, entry.entry_id), writer=EntityServiceWriter(hass),
                               lock=_entry_lock(hass, entry.entry_id))
    await executor.async_start()
    telemetry = None
    rt = None
    try:
        fetcher = ScheduleFetcher(cloud, on_plan=executor.async_on_plan, on_consent=executor.async_set_consent,
                                  on_auth_failure=executor.async_on_auth_failure)
        limits = executor_limits(rated_power_w=rated, battery_capacity_kwh=opts.get(OPT_BATTERY_CAPACITY_KWH),
                                 source="user" if manual_rated else "entities")
        telemetry = TelemetrySender(hass, entry, cloud, executor, choice=choice, profile_map=mapped,
                                    manual_map=opts.get(OPT_TELEMETRY_MAP) or {},
                                    grid_negate=bool(opts.get(OPT_GRID_NEGATE)), limits=limits)
        await telemetry.async_start()
        rt = ControlRuntime(executor, fetcher, telemetry, cloud, choice, mapped, rated,
                            options_at_setup=dict(opts),
                            inverter_entities=inverter_entity_ids(hass, choice, mapped))

        async def _fetch(_now=None) -> None:
            # Odświeżenie planu (i zgody) — zaraz po nim cykl: cofnięta zgoda działa od razu.
            await fetcher.async_refresh()
            await executor.async_tick()

        rt.unsubs.append(async_track_time_interval(hass, _fetch, timedelta(seconds=SCHEDULE_FETCH_INTERVAL_S)))
        # Runtime w hass.data PRZED onboardingiem — ten czyta go od razu (start „na gorąco").
        hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["control"] = rt
        _maybe_start_onboarding(hass, entry, report)
    except BaseException:
        # Nieudane złożenie nie zostawia żywego wykonawcy (bez encji wyłącznika nikt by go
        # nie zatrzymał, a każde przeładowanie dokładałoby kolejnego).
        await _async_abort_setup(hass, entry, executor, telemetry, rt)
        raise
    # Pierwszy cykl i pierwsze pobranie — zadania HA, nie wpisu: unload nie anuluje ich
    # w połowie zapisu grupy (zatrzymany wykonawca i tak już nic nie zapisze).
    hass.async_create_background_task(executor.async_tick(), "volcast_first_tick")
    hass.async_create_background_task(_fetch(), "volcast_first_fetch")
    entry.async_create_background_task(
        hass, async_import_history_once(hass, cloud, executor, load_entity=opts.get(OPT_LOAD_ENERGY),
                                        pv_entity=opts.get(CONF_PV_ENERGY_ENTITY) or None,
                                        now_utc=dt_util.utcnow()),
        "volcast_history_import")
    return rt


async def _async_abort_setup(hass, entry, executor, telemetry, rt) -> None:
    """Sprzątanie po błędzie za `executor.async_start()`; samo nigdy nie rzuca."""
    for unsub in (rt.unsubs if rt is not None else ()):
        try:
            unsub()
        except Exception:  # noqa: BLE001
            pass
    if rt is not None:
        rt.unsubs.clear()
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(entry_data, dict) and rt is not None and entry_data.get("control") is rt:
        entry_data["control"] = None
    for stop in ((telemetry.async_stop,) if telemetry is not None else ()) + (executor.async_stop,):
        try:
            await stop()
        except Exception as err:  # noqa: BLE001 — pierwotny błąd jest ważniejszy
            _LOGGER.warning("Volcast control: cleanup after a failed setup failed (%s)", type(err).__name__)


def _maybe_start_onboarding(hass, entry, report) -> None:
    from ..onboarding import Onboarding   # późny import: onboarding nie jest potrzebny po oknie 30 min
    p = entry.data.get(CONF_PAIRING)
    if not isinstance(p, dict):
        return
    live_until = dt_util.parse_datetime(str(p.get("live_until") or ""))
    if live_until is None or live_until <= dt_util.utcnow():
        return
    running = hass.data.setdefault(ONBOARDING_KEY, {})
    task = running.get(entry.entry_id)
    if task is not None and not task.done():
        return      # przeładowanie wpisu (zmiana opcji) nie startuje drugiego onboardingu
    client = PairingClient(async_get_clientsession(hass), p.get("url") or BETA_PAIRING_URL)
    session = PairingSession(str(p.get("session_id")), str(p.get("poll_token")), "", "")

    def runtime():
        return (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get("control")

    async def import_history():
        rt, e = runtime(), hass.config_entries.async_get_entry(entry.entry_id)
        if rt is None or e is None:
            return None
        if rt.executor.history_imported_at:
            return {"accepted": 0, "already": True}
        return await async_import_history_once(hass, rt.cloud, rt.executor,
                                               load_entity=e.options.get(OPT_LOAD_ENERGY),
                                               pv_entity=e.options.get(CONF_PV_ENERGY_ENTITY) or None,
                                               now_utc=dt_util.utcnow())

    ob = Onboarding(hass, entry.entry_id, client=client, session=session, live_until=live_until,
                    runtime=runtime, report=report, import_history=import_history)
    running[entry.entry_id] = hass.async_create_background_task(ob.async_run(), "volcast_onboarding")


def freeze_control(rt: ControlRuntime) -> None:
    """Przed powrotem i przeładowaniem po zmianie opcji sterowania: koniec cykli i pobierania.

    Powrót do trybu bazowego (`async_restore_now`) nadal działa.
    """
    for unsub in rt.unsubs:
        unsub()
    rt.unsubs.clear()
    freeze = getattr(rt.executor, "freeze", None)
    if freeze is not None:
        freeze()


async def async_unload_control(hass, rt: ControlRuntime, *, restore: bool = False) -> None:
    """Zwykłe przeładowanie/restart nie oddaje falownika (`restore=False`, domyślnie).

    `restore=True` jest dla jawnej decyzji właściciela wyłączyć wpis (`entry.disabled_by`
    ustawione przy rozładunku) — wtedy oddajemy falownik w tryb bazowy, zanim wykonawca
    się zatrzyma. Błąd przywrócenia nigdy nie blokuje rozładunku.
    """
    for unsub in rt.unsubs:
        unsub()
    rt.unsubs.clear()
    if restore:
        try:
            await rt.executor.async_restore_now()
        except Exception as err:  # noqa: BLE001 — rozładunek wpisu nie może się przez to wywrócić
            _LOGGER.warning("Volcast control: restore before disabling the entry failed (%s)",
                            type(err).__name__)
    await rt.telemetry.async_stop()
    await rt.executor.async_stop()


async def async_remove_control(hass, entry) -> None:
    """Usunięcie wpisu: przywróć tryb bazowy, jeśli to my zmienialiśmy nastawy; skasuj stan."""
    if Backend.from_dict(entry.data.get(CONF_BACKEND)) is None:
        return
    task = hass.data.get(ONBOARDING_KEY, {}).pop(entry.entry_id, None)
    if task is not None:
        task.cancel()
    store = ControlStore(hass, entry.entry_id)
    try:
        state = await store.async_load()
    except Exception as err:  # noqa: BLE001 — zły format magazynu: nie ma czego przywrócić
        _LOGGER.warning("Volcast control: saved state unreadable on removal (%s)", type(err).__name__)
        state = None
    if state is not None and state.owned:
        profiles = await hass.async_add_executor_job(_load_profiles)
        choice = _choice_for(hass, entry, profiles)
        executor = VolcastExecutor(hass, entry, choice=choice, mapped=map_entities(hass, choice) if choice else {},
                                   rated_power_w=None, store=store, writer=EntityServiceWriter(hass),
                                   lock=_entry_lock(hass, entry.entry_id))
        await executor.async_start()
        await executor.async_restore_now()
        if executor.owned:
            # Still owned after the attempt: no profile/integration, a read-only profile,
            # or the write itself failed (already logged by the executor in that case) —
            # the store is wiped next regardless, so this is the last chance to say so.
            _LOGGER.warning("Volcast control: could not return the inverter to its settings from "
                            "before control — check its mode")
        await executor.async_stop()
    await store.async_remove()
    hass.data.get(_LOCKS_KEY, {}).pop(entry.entry_id, None)
