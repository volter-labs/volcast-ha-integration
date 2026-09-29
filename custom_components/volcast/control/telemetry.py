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
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util
from homeassistant.helpers.event import async_track_time_interval

from ..const import (CONTROL_MODE_ENTITIES, OPT_CONTROL_MODE, OPT_PRICE_BUY, OPT_PRICE_CURRENCY,
                     OPT_PRICE_SELL)
from ..core.control.caps import capabilities_for
from ..core.control.readings import RawState, manual_reading, normalize_readings
from ..core.control.select import ProfileChoice
from ..core.prices import currency_from_attributes, fingerprint, intervals_from_attributes
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


def driver_block(*, choice: ProfileChoice | None, control_mode: str | None, mapped_keys: Iterable[str],
                 local_switch: bool, limits: dict | None) -> dict | None:
    """Deklaracja wykonawcy; możliwości tylko w trybie encji z integracją i modelem `mode_setpoint`."""
    if choice is None:
        return None
    block: dict = {"id": choice.profile.id, "model": choice.profile.control_model,
                   "local_switch_enabled": bool(local_switch)}
    if (choice.integration_domain and control_mode == CONTROL_MODE_ENTITIES
            and choice.profile.control_model == "mode_setpoint"):
        block["capabilities"] = capabilities_for(choice.profile, mapped_keys)
    if limits is not None:
        block["limits"] = limits
    return block


def direct_driver_block(*, profile, access: str, capabilities: Mapping[str, bool] | None, local_switch: bool,
                        limits: dict | None) -> dict:
    """Deklaracja wykonawcy w trybie bezpośrednim — bez seriala i adresu."""
    block: dict = {"id": profile.id, "model": profile.control_model, "local_switch_enabled": bool(local_switch),
                   "access": access}
    if capabilities is not None:
        block["capabilities"] = dict(capabilities)
    if limits is not None:
        block["limits"] = limits
    return block




def _number(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return round(float(v), 3)


def build_reading(*, now_utc: datetime, profile_readings: Mapping[str, float | str],
                  manual: Mapping[str, float | None], driver: dict | None, extra: dict,
                  prices: dict | None) -> dict | None:
    """Odczyt kontraktu; encja wskazana ręcznie ma pierwszeństwo (także gdy nieczytelna).

    Bez wartości monitoringu odczyt i tak idzie, gdy niesie ceny — chmura nie ma dla cen
    z HA żadnego zapasu, a blok `driver` jedzie razem z nimi. None tylko wtedy, gdy nie
    ma ani wartości, ani cen.
    """
    values: dict = {}
    for key, name in TELEMETRY_FIELDS.items():
        v = _number(manual.get(key) if key in manual else profile_readings.get(key))
        if v is not None:
            values[name] = v
    mode = profile_readings.get("mode")
    if isinstance(mode, str):
        values["ems_mode"] = mode
    if not values and prices is None:
        return None
    reading = {"timestamp": now_utc.isoformat(), **values, "extra": {"volcast": extra}}
    if driver is not None:
        reading["driver"] = driver
    if prices is not None:
        reading["prices"] = prices
    return reading


class TelemetrySender:
    def __init__(self, hass, entry, cloud, executor, *, choice: ProfileChoice | None,
                 profile_map: Mapping[str, str], manual_map: Mapping[str, str], grid_negate: bool,
                 limits: dict | None, utcnow=dt_util.utcnow, direct=None,
                 direct_capabilities: Mapping[str, bool] | None = None) -> None:
        self._hass, self._entry, self._cloud, self._executor = hass, entry, cloud, executor
        self._choice = choice
        self._profile_map = dict(profile_map) if choice and choice.integration_domain else {}
        self._manual_map = dict(manual_map)
        self._negate = grid_negate
        self._limits = limits
        self._utcnow = utcnow
        self._direct = direct
        self._direct_caps = dict(direct_capabilities) if direct_capabilities is not None else None
        self._prices_fp: str | None = None
        self._prices_at: datetime | None = None
        self._unsub = None
        self._busy = False

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

    def _direct_readings(self) -> dict:
        r = self._direct_fresh()
        if r is None:
            return {}
        out = {k: v for k, v in r.values.items() if k in TELEMETRY_FIELDS}
        mode = r.device.get("mode")
        if isinstance(mode, str) and mode in self._choice.profile.modes:
            out["mode"] = mode
        return out

    def _driver(self) -> dict | None:
        local = bool(getattr(self._executor, "local_switch", False))
        if self._direct is not None:
            return direct_driver_block(profile=self._choice.profile, access="direct",
                                       capabilities=None if self._direct.trial else self._direct_caps,
                                       local_switch=local, limits=self._limits)
        unsupported = self._unsupported()
        return driver_block(choice=self._choice, control_mode=self._entry.options.get(OPT_CONTROL_MODE),
                            mapped_keys=[k for k in self._profile_map if k not in unsupported],
                            local_switch=local, limits=self._limits)

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

    async def _async_flush(self) -> bool:
        now = self._utcnow()
        domain = self._choice.integration_domain if self._choice else None
        if self._direct is not None:
            prof = self._direct_readings()
        else:
            raw = {k: r for k, e in self._profile_map.items() if (r := self._raw(e)) is not None}
            prof = normalize_readings(raw, self._choice.profile, domain) if domain else {}
        manual: dict[str, float | None] = {}
        for key, eid in self._manual_map.items():
            manual[key] = self._manual(key, eid)
        try:
            priced = self._prices(now)
        except Exception as err:  # noqa: BLE001 — ceny nigdy nie zabierają telemetrii
            _LOGGER.debug("Volcast prices block skipped (%s)", type(err).__name__)
            priced = None
        reading = build_reading(
            now_utc=now, profile_readings=prof, manual=manual,
            driver=self._driver(),
            extra=self._extra(), prices=priced[0] if priced else None)
        if reading is None:
            return False
        ok = await self._cloud.async_post_telemetry(reading) is True
        if ok and priced:
            self._prices_fp, self._prices_at = priced[1], now
        return ok
