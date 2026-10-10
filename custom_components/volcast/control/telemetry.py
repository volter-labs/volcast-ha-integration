"""Telemetria co 60 s do `device-telemetry` (kontrakt: `readings[]` z polami
monitoringu, blokiem `driver`, sekcją `prices` i `extra`).

Jednostki kanoniczne; znak sieci + = pobór. Błąd wysyłki nigdy nie wychodzi poza
`async_flush`, a w logu jest tylko nazwa klasy wyjątku (treść bywa z adresem hosta
albo identyfikatorem encji). `extra.volcast` to podsumowanie wykonawcy — liczniki
i klucze parametrów, nigdy `entity_id` (obce zmiany: sam licznik).

Ceny (`prices`) idą tylko przy zmianie treści albo co 6 h i są uznane za wysłane
dopiero po przyjęciu odczytu przez chmurę. Błąd bloku cen nie zabiera odczytu.
Jedna wysyłka naraz: tik zegara w trakcie trwającej wysyłki jest pomijany.

Tryb bezpośredni: wartości z odczytu rejestrów (`DirectReading.values` + tryb jako nazwa), tylko
świeże (młodsze niż 3× okres odpytywania); ręczne mapowanie z opcji dalej wygrywa. Blok `driver`
ma `access: "direct"`, możliwości z sondy (poza próbą) i limity (`registers` albo `user`);
`extra.volcast.direct` niesie nazwę transportu, stan łącza i liczniki. Nigdy adres, port, numer
seryjny, odcisk urządzenia ani sól.

Sygnały: blok `driver` deklaruje cechy (`features`), `extra.volcast.signal_connected` mówi, czy
kanał sygnałów jest dołączony, a blok `signals` z przyjętej odpowiedzi idzie do `on_signals`.
`build_live_reading` to lekki odczyt na żywo (same wartości + `live: true`, bez cen, `driver`,
`extra` i ładowarek) — wysyła go `LiveSender`, nie ten nadawca.

Liczniki energii (kontrakt sygnałów §6.3): `extra.counters` w odczycie niesie tylko klucze
`COUNTER_KEYS` z ważną wartością (skończona liczba ≥ 0, kWh, poniżej `COUNTER_MAX_KWH`); brak ważnego
licznika = brak bloku (chmura liczy wtedy ze średniej mocy). `*_total_kwh` rosną przez całe życie
falownika, `load_energy_day_kwh` zeruje się o północy lokalnej falownika. Liczniki PV i sieci zostają
w swoich kolumnach odczytu. Odczyt na żywo liczników nie niesie.

Limity w trybie bezpośrednim: bazowe (`direct_limits`) + moc z prądu BMS wskazanego w `$.limits` profilu
(`core/control/limits.py`, szczyt z doby), przeliczane przy każdej telemetrii.

Źródła odczytów: blok `driver` w KAŻDYM odczycie (oba tryby) niesie pełną mapę
`meta.readings_source` (klucz → `entity|register|manual|none`, `core/control/readings.py`) — chmura
czyści brakującą mapę w trybie bezpośrednim, a w trybie encji trzymałaby nieaktualną. Inne pola
`meta` nie idą (sufit chmury 1500 B).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util
from homeassistant.helpers.event import async_track_time_interval

from ..const import (CONTROL_MODE_ENTITIES, OPT_CONTROL_MODE, OPT_PRICE_BUY, OPT_PRICE_CURRENCY,
                     OPT_PRICE_SELL)
from ..core.control.caps import capabilities_for
from ..core.control.limits import BMS_POWER_FIELDS, PeakLimit, register_limits
from ..core.control.readings import RawState, manual_reading, normalize_readings_report, readings_sources
from ..core.control.select import ProfileChoice, control_verified
from ..core.prices import currency_from_attributes, fingerprint, intervals_from_attributes
from .loads_reader import LoadsReader
from .direct_sensors import STALE_FACTOR as _STALE_FACTOR   # jedna reguła świeżości z sensorami

_LOGGER = logging.getLogger(__name__)
TELEMETRY_INTERVAL_S = 60
PRICES_RESEND_S = 6 * 3600
_UNAVAILABLE = ("unavailable", "unknown")
# Rynek, gdy konfiguracja HA nie podaje kraju (instalacje sprzed tego pola).
_DEFAULT_MARKET = "PL"
TELEMETRY_FIELDS = {
    "soc": "battery_soc", "pv_power_w": "pv_power_w", "battery_power_w": "battery_power_w",
    "grid_power_w": "grid_power_w", "load_power_w": "load_power_w",
    "pv_energy_total_kwh": "pv_energy_total_kwh", "grid_import_total_kwh": "grid_import_total_kwh",
    "grid_export_total_kwh": "grid_export_total_kwh",
}
# Liczniki w `extra.counters` (zamknięty zbiór kontraktu §6.3; podzbiór `EXTRA_KEYS` mapy źródeł).
COUNTER_KEYS = ("load_energy_total_kwh", "load_energy_day_kwh", "battery_charge_total_kwh",
                "battery_discharge_total_kwh")
# Sanity HA: 10 GWh to wiele żyć domowego falownika; powyżej = śmieci (np. 0xFFFFFFFF × 0,1 kWh).
COUNTER_MAX_KWH = 1e7
# Cechy integracji deklarowane chmurze w bloku `driver` (zamknięty słownik kontraktu sygnałów;
# chmura przyjmuje dowolny jego podzbiór). `signals` w kontrakcie znaczy także obsługę wznowienia
# (`resume_requested_at`) — deklarujemy ją dopiero razem z tą obsługą, inaczej aplikacja pokazałaby
# „Wznów”, którego HA nigdy nie wykona.
DRIVER_FEATURES = ("live",)


def _with_sources(block: dict, readings_source: Mapping[str, str] | None) -> dict:
    if readings_source:
        block["meta"] = {"readings_source": dict(readings_source)}
    return block


def driver_block(*, choice: ProfileChoice | None, control_mode: str | None, mapped_keys: Iterable[str],
                 local_switch: bool, limits: dict | None,
                 readings_source: Mapping[str, str] | None = None) -> dict | None:
    """Deklaracja wykonawcy; możliwości tylko w trybie encji z integracją i modelem `mode_setpoint`."""
    if choice is None:
        return None
    block: dict = {"id": choice.profile.id, "model": choice.profile.control_model,
                   "local_switch_enabled": bool(local_switch)}
    if (choice.integration_domain and control_mode == CONTROL_MODE_ENTITIES
            and choice.profile.control_model == "mode_setpoint"):
        caps = capabilities_for(choice.profile, mapped_keys)
        if not control_verified(choice.profile, choice.integration_domain):
            caps = dict.fromkeys(caps, False)     # draft: HA nie wykona żadnej z nich
        block["capabilities"] = caps
    if limits is not None:
        block["limits"] = limits
    return _with_sources(block, readings_source)


def direct_driver_block(*, profile, access: str, capabilities: Mapping[str, bool] | None, local_switch: bool,
                        limits: dict | None, readings_source: Mapping[str, str] | None = None) -> dict:
    """Deklaracja wykonawcy w trybie bezpośrednim — bez seriala i adresu."""
    block: dict = {"id": profile.id, "model": profile.control_model, "local_switch_enabled": bool(local_switch),
                   "access": access}
    if capabilities is not None:
        block["capabilities"] = dict(capabilities)
    if limits is not None:
        block["limits"] = limits
    return _with_sources(block, readings_source)


def _declared_keys(choice: ProfileChoice | None, manual_keys: Iterable[str], *, direct: bool) -> frozenset[str]:
    """Klucze, które ścieżka odczytu w ogóle opisuje: rejestry profilu (tryb bezpośredni) albo encje
    wybranej integracji (tryb encji), plus mapa ręczna. Licznik z mapy rejestrów nie jest źródłem
    w trybie encji — nie deklarujemy go tam jako `none`."""
    if choice is None:
        return frozenset(manual_keys)
    raw = getattr(choice.profile, "raw", None) or {}
    keys: set[str] = set()
    if direct:
        keys.update(raw.get("read") or ())
    else:
        for integ in (raw.get("ha") or {}).get("integrations") or ():
            if integ.get("domain") == choice.integration_domain:
                keys.update(integ.get("entities") or ())
    return frozenset(keys | set(manual_keys))




def control_block(payload: Mapping, ack: Mapping | None, meta: dict, now_s: float) -> dict:
    """Blok `driver.control` z `seq` wg kontraktu: `max(now, poprzedni + 1)` TYLKO przy zmianie treści
    (poza samym `seq`), inaczej ten sam `seq`. `meta` (`seq`, `fp`) zmieniane w miejscu — trwały zapis
    robi wołający, żeby restart nie cofnął licznika."""
    body = dict(payload)
    if ack:
        body["choice_ack"] = dict(ack)
    fp = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    prev = meta.get("seq")
    prev = prev if isinstance(prev, int) and not isinstance(prev, bool) and prev >= 0 else -1
    if meta.get("fp") != fp:
        meta["seq"] = max(int(now_s), prev + 1)
        meta["fp"] = fp
    elif prev < 0:
        meta["seq"] = max(int(now_s), 0)
    return {"seq": meta["seq"], **body}


def _number(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return round(float(v), 3)


def _values(profile_readings: Mapping[str, float | str], manual: Mapping[str, float | None]) -> dict:
    """Wartości monitoringu; encja wskazana ręcznie ma pierwszeństwo (także gdy nieczytelna)."""
    values: dict = {}
    for key, name in TELEMETRY_FIELDS.items():
        v = _number(manual.get(key) if key in manual else profile_readings.get(key))
        if v is not None:
            values[name] = v
    mode = profile_readings.get("mode")
    if isinstance(mode, str):
        values["ems_mode"] = mode
    return values


def counters_block(profile_readings: Mapping[str, float | str], manual: Mapping[str, float | None]
                   ) -> dict | None:
    """`extra.counters`: tylko liczniki z ważną wartością; None = żadnego (bloku nie wysyłamy).
    Encja wskazana ręcznie ma pierwszeństwo (także gdy nieczytelna) — jak w `_values`."""
    out: dict = {}
    for key in COUNTER_KEYS:
        v = _number(manual.get(key) if key in manual else profile_readings.get(key))
        if v is not None and 0 <= v < COUNTER_MAX_KWH:
            out[key] = v
    return out or None


def build_live_reading(*, now_utc: datetime, profile_readings: Mapping[str, float | str],
                       manual: Mapping[str, float | None]) -> dict | None:
    """Odczyt na żywo: same wartości z `live: true`; None, gdy nie ma czego wysłać."""
    values = _values(profile_readings, manual)
    if not values:
        return None
    return {"timestamp": now_utc.isoformat(), "live": True, **values}


def build_reading(*, now_utc: datetime, profile_readings: Mapping[str, float | str],
                  manual: Mapping[str, float | None], driver: dict | None, extra: dict,
                  prices: dict | None, loads: list | None = None) -> dict | None:
    """Odczyt kontraktu; encja wskazana ręcznie ma pierwszeństwo (także gdy nieczytelna).

    Bez wartości monitoringu odczyt i tak idzie, gdy niesie ceny — chmura nie ma dla cen
    z HA żadnego zapasu, a blok `driver` jedzie razem z nimi. None tylko wtedy, gdy nie
    ma ani wartości, ani cen.
    """
    values = _values(profile_readings, manual)
    if not values and prices is None:
        return None
    reading = {"timestamp": now_utc.isoformat(), **values, "extra": {"volcast": extra}}
    counters = counters_block(profile_readings, manual)
    if counters is not None:
        reading["extra"]["counters"] = counters
    if driver is not None:
        reading["driver"] = driver
    if loads:
        reading["loads"] = loads
    if prices is not None:
        reading["prices"] = prices
    return reading


class TelemetrySender:
    def __init__(self, hass, entry, cloud, executor, *, choice: ProfileChoice | None,
                 profile_map: Mapping[str, str], manual_map: Mapping[str, str], grid_negate: bool,
                 limits: dict | None, utcnow=dt_util.utcnow, direct=None,
                 direct_capabilities: Mapping[str, bool] | None = None,
                 signal_connected: Callable[[], bool] | None = None,
                 on_signals: Callable[[dict | None], Awaitable[None]] | None = None) -> None:
        self._hass, self._entry, self._cloud, self._executor = hass, entry, cloud, executor
        self._choice = choice
        self._profile_map = dict(profile_map) if choice and choice.integration_domain else {}
        self._manual_map = dict(manual_map)
        self._negate = grid_negate
        self._limits = limits
        # Szczyt mocy z prądu BMS (doba) — tylko tryb bezpośredni.
        self._peaks = {key: PeakLimit() for key in BMS_POWER_FIELDS}
        # Źródło każdego odczytu i klucze z encją bez odczytu — z ostatniego `_current()`.
        self._declared = _declared_keys(choice, self._manual_map, direct=direct is not None)
        self._sources: dict[str, str] = {}
        self._dropped: tuple[str, ...] = ()
        self._utcnow = utcnow
        self._direct = direct
        self._direct_caps = dict(direct_capabilities) if direct_capabilities is not None else None
        self._signal_connected = signal_connected
        self._on_signals = on_signals
        self._prices_fp: str | None = None
        self._prices_at: datetime | None = None
        self._unsub = None
        self._busy = False
        self._loads = LoadsReader(hass, entry)
        self._loads_warned = False
        # Runtime sterowania (`control_block()` + `async_persist_control_meta()`) — ustawiany po jego złożeniu.
        self.control_runtime = None

    async def async_start(self) -> None:
        if self._unsub is None:
            self._unsub = async_track_time_interval(self._hass, self._timer,
                                                    timedelta(seconds=TELEMETRY_INTERVAL_S))

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _timer(self, _now=None) -> None:
        await self.async_flush()

    def _raw(self, eid: str) -> RawState | None:
        st = self._hass.states.get(eid)
        if st is None:
            return None
        state = st.state if isinstance(st.state, str) else None
        return RawState(state, st.attributes.get("unit_of_measurement"))

    def _manual(self, key: str, eid: str) -> float | None:
        """Jedna encja wskazana ręcznie; jej błąd kasuje tylko to pole, nie cały odczyt."""
        r = self._raw(eid)
        if r is None or not (r.unit is None or isinstance(r.unit, str)):
            return None               # nieznana postać jednostki ≠ brak jednostki
        try:
            return manual_reading(key, r, negate=(key == "grid_power_w" and self._negate))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Volcast telemetry: manual reading of %s skipped (%s)", key, type(err).__name__)
            return None

    def _market(self) -> str:
        country = getattr(self._hass.config, "country", None)
        country = country.strip().upper() if isinstance(country, str) else ""
        return country if len(country) == 2 and country.isascii() and country.isalpha() else _DEFAULT_MARKET

    def _prices(self, now: datetime) -> tuple[dict, str] | None:
        """Blok cen (albo None: brak encji, danych, zmiany) i jego odcisk."""
        opts = self._entry.options
        buy = self._hass.states.get((opts.get(OPT_PRICE_BUY) or "").strip())
        if buy is None or buy.state in _UNAVAILABLE:
            return None
        sell_id = (opts.get(OPT_PRICE_SELL) or "").strip()
        sell = self._hass.states.get(sell_id) if sell_id else None
        currency = currency_from_attributes(buy.attributes,
                                            (opts.get(OPT_PRICE_CURRENCY) or "").strip().upper() or None)
        if currency is None:
            return None
        intervals = intervals_from_attributes(buy.attributes, sell.attributes if sell else None, currency,
                                              ZoneInfo(self._hass.config.time_zone), now)
        if not intervals:
            return None
        market = self._market()
        fp = fingerprint([{"market": market}, *intervals])
        stale = self._prices_at is None or (now - self._prices_at).total_seconds() >= PRICES_RESEND_S
        if fp == self._prices_fp and not stale:
            return None
        return {"market": market, "currency": currency, "intervals": intervals}, fp

    def _unsupported(self) -> tuple[str, ...]:
        """Nastawy, które wykonawca uznał za nieobsługiwane (brak encji, długo niedostępna)."""
        return tuple(getattr(self._executor, "unsupported_settings", None) or ())

    def _extra(self) -> dict:
        try:
            summary = self._executor.exec_summary()
        except Exception as err:  # noqa: BLE001 — podsumowanie nie zabiera odczytu
            _LOGGER.debug("Volcast telemetry: executor summary skipped (%s)", type(err).__name__)
            summary = {}
        summary = dict(summary) if isinstance(summary, dict) else {}
        if self._direct is not None:
            stats = self._direct.stats
            summary["direct"] = {"transport": str(self._direct.target.get("transport") or ""),
                                 "status": self._direct_status(), "stray": int(stats.stray),
                                 "timeouts": int(stats.timeouts),
                                 "nvm_budget_hit": bool(getattr(self._executor, "nvm_budget_hit", False))}
        if self._signal_connected is not None:
            try:
                summary["signal_connected"] = bool(self._signal_connected())
            except Exception as err:  # noqa: BLE001 — stan kanału nie zabiera odczytu
                _LOGGER.debug("Volcast telemetry: signal state skipped (%s)", type(err).__name__)
        return summary

    def _direct_fresh(self):
        conn = self._direct
        r = conn.reading
        if r is None or not conn.age_s() < _STALE_FACTOR * float(conn.poll_s):
            return None
        return r

    def _direct_status(self) -> str:
        conn = self._direct
        if conn.trial:
            return "trial"
        if conn.conflict:
            return "conflict"
        return "ok" if self._direct_fresh() is not None else "link_down"

    def _direct_readings(self, r) -> dict:
        if r is None:
            return {}
        out = {k: v for k, v in r.values.items() if k in TELEMETRY_FIELDS or k in COUNTER_KEYS}
        mode = r.device.get("mode")
        if isinstance(mode, str) and mode in self._choice.profile.modes:
            out["mode"] = mode
        return out

    def _direct_limits(self) -> dict | None:
        """Limity bazowe + moc z prądu BMS ze świeżego odczytu rejestrów (szczyt z doby; bez świeżego
        odczytu — dotychczasowy szczyt). Błąd nigdy nie zabiera telemetrii — wtedy same bazowe."""
        try:
            fresh = self._direct_fresh()
            spec = self._choice.profile.raw.get("limits") or {}
            return register_limits(self._limits, spec, fresh.values if fresh is not None else {},
                                   peaks=self._peaks, now_s=self._utcnow().timestamp())
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Volcast telemetry: register limits skipped (%s)", type(err).__name__)
            return self._limits

    def _driver(self) -> dict | None:
        """Blok `driver` (oba tryby) z cechami integracji dopisanymi w jednym miejscu."""
        local = bool(getattr(self._executor, "local_switch", False))
        if self._direct is not None:
            block = direct_driver_block(profile=self._choice.profile, access="direct",
                                        capabilities=None if self._direct.trial else self._direct_caps,
                                        local_switch=local, limits=self._direct_limits(), readings_source=self._sources)
        else:
            unsupported = self._unsupported()
            block = driver_block(choice=self._choice, control_mode=self._entry.options.get(OPT_CONTROL_MODE),
                                 mapped_keys=[k for k in self._profile_map if k not in unsupported],
                                 local_switch=local, limits=self._limits, readings_source=self._sources)
        if block is not None:
            block["features"] = list(DRIVER_FEATURES)
        rt = self.control_runtime
        if rt is not None:
            try:
                control = rt.control_block()
            except Exception as err:  # noqa: BLE001 — stan sterowania nigdy nie zabiera telemetrii
                _LOGGER.debug("Volcast control block skipped (%s)", type(err).__name__)
            else:
                # Bez profilu: sam `control` (chmura czyta go niezależnie od `driver.id`).
                block = {**(block or {}), "control": control}
        return block

    def _loads_block(self) -> list | None:
        try:
            block = self._loads.read()
        except Exception as err:  # noqa: BLE001 — ładowarki nigdy nie zabierają telemetrii
            if not self._loads_warned:
                self._loads_warned = True
                _LOGGER.warning("Volcast EV charger readout skipped (%s)", type(err).__name__)
            return None
        self._loads_warned = False
        return block

    async def async_flush(self) -> bool:
        """Jeden odczyt do chmury; True = przyjęty. Nigdy nie rzuca."""
        if self._busy:
            return False
        self._busy = True
        try:
            return await self._async_flush()
        except Exception as err:  # noqa: BLE001 — telemetria nigdy nie psuje reszty
            _LOGGER.debug("Volcast telemetry flush failed (%s)", type(err).__name__)
            return False
        finally:
            self._busy = False

    def _current(self) -> tuple[dict, dict[str, float | None]]:
        """Bieżące odczyty profilu (albo rejestrów) i encji wskazanych ręcznie.

        Przy okazji odświeża źródło każdego odczytu (`self._sources`) i klucze z encją, z której nic
        nie wyszło (`self._dropped`) — z tych samych wartości, które idą w telemetrii."""
        domain = self._choice.integration_domain if self._choice else None
        register: Mapping = {}
        dropped: set[str] = set()
        if self._direct is not None:
            fresh = self._direct_fresh()
            prof = self._direct_readings(fresh)
            register = fresh.values if fresh is not None else {}
            entity: dict = {}
        else:
            raw = {k: r for k, e in self._profile_map.items() if (r := self._raw(e)) is not None}
            if domain:
                prof, lost = normalize_readings_report(raw, self._choice.profile, domain)
                dropped.update(lost)
                dropped.update(k for k in self._profile_map if k not in raw)      # encji nie ma w HA
            else:
                prof = {}
            entity = dict(prof)
        manual: dict[str, float | None] = {}
        for key, eid in self._manual_map.items():
            manual[key] = self._manual(key, eid)
        # Encja ręczna wygrywa także nieczytelna (jak w `_values`) — wtedy klucz nie ma źródła.
        entity.update(manual)
        dropped.difference_update(k for k, v in manual.items() if v is not None)
        dropped.update(k for k, v in manual.items() if v is None)
        typed = {"battery_capacity_kwh": self._limits.get("battery_capacity_kwh")} \
            if isinstance(self._limits, Mapping) else {}
        self._sources = readings_sources(entity=entity,
                                         register={k: v for k, v in register.items() if k not in manual},
                                         manual=typed, declared=self._declared)
        self._dropped = tuple(sorted(dropped))
        return prof, manual

    def readings_report(self) -> dict:
        """Diagnostyka: bieżące źródło każdego odczytu i klucze z encją bez odczytu (bez `entity_id`)."""
        self._current()
        return {"readings_source": dict(self._sources), "readings_dropped": list(self._dropped)}

    def build_live_reading(self) -> dict | None:
        """Lekki odczyt na żywo (bez cen, `driver`, `extra`, ładowarek); None = nic do wysłania.

        Nie rusza stanu cen ani ładowarek — minutowy odczyt idzie dalej bez zmian."""
        prof, manual = self._current()
        return build_live_reading(now_utc=self._utcnow(), profile_readings=prof, manual=manual)

    async def _async_flush(self) -> bool:
        now = self._utcnow()
        prof, manual = self._current()
        try:
            priced = self._prices(now)
        except Exception as err:  # noqa: BLE001 — ceny nigdy nie zabierają telemetrii
            _LOGGER.debug("Volcast prices block skipped (%s)", type(err).__name__)
            priced = None
        reading = build_reading(
            loads=self._loads_block(),
            now_utc=now, profile_readings=prof, manual=manual,
            driver=self._driver(),
            extra=self._extra(), prices=priced[0] if priced else None)
        if reading is None:
            return False
        result = await self._cloud.async_post_telemetry(reading)
        ok = result.ok is True
        rt = self.control_runtime
        if ok and rt is not None:
            try:
                await rt.async_persist_control_meta()
            except Exception as err:  # noqa: BLE001 — zapis licznika nie psuje przyjętej telemetrii
                _LOGGER.debug("Volcast control meta save failed (%s)", type(err).__name__)
        if ok and priced:
            self._prices_fp, self._prices_at = priced[1], now
        if ok and self._on_signals is not None:
            try:
                await self._on_signals(result.signals_raw)
            except Exception as err:  # noqa: BLE001 — sygnały nie psują przyjętej telemetrii
                _LOGGER.debug("Volcast telemetry: signals handling failed (%s)", type(err).__name__)
        return ok
