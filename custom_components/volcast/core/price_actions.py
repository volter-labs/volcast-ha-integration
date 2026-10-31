"""Ceny z akcji Home Assistanta (`return_response`) zamiast z atrybutów encji.

Warstwa CZYSTA — bez importów Home Assistanta. Samo wywołanie akcji wstrzykuje
wołający (telemetria) jako `call(domain, service, data) -> odpowiedź`.

Część integracji cenowych (Nord Pool z rdzenia, Tibber, EnergyZero,
EasyEnergy) nie trzyma prognozy w atrybutach żadnej encji — daje ją tylko
akcja z odpowiedzią. Odpowiedź zamieniamy na ATRYBUTY w kształcie, który już
rozumie adapter atrybutów (`core/prices.py`), więc kontrakt przedziałów,
waluta, skala (za MWh ÷1000) i reguła nadrzędna „dane niespójne → pusta lista,
nigdy częściowa” są jedne dla encji i dla akcji.

Konfiguracja źródła (opcja wpisu, patrz `const.OPT_PRICE_*_ACTION`):

    {"action": "nordpool.get_prices_for_date",   # jedna z SUPPORTED_ACTIONS
     "data": {...},        # dane akcji od użytkownika (wpis integracji, strefa, VAT…)
     "currency": "NOK",    # waluta, gdy odpowiedź jej nie niesie (ENTSO-e: wymagana)
     "unit": "MWh",        # ENTSO-e (HACS): wymagana skala ceny, "kWh" albo "MWh"
     "home": "..."}        # opcjonalnie (Tibber): który dom z odpowiedzi

Parametry dat (`date` / `start` / `end`) dokłada ten moduł — doba RYNKU
(strefa wołającego), dziś i jutro osobnymi wywołaniami, bo część integracji
(EnergyZero) odrzuca całe wywołanie, gdy jutra jeszcze nie opublikowano.

Harmonogram (`PriceActionPoller.due`): co 6 h; po 13:00 czasu rynku co godzinę,
dopóki dane nie sięgają jutra; po błędzie odstępy 5, 10, 20, 40, 60, 60… min.
Błąd wywołania na DZIŚ (wyjątek, przekroczony czas, pusta albo zła odpowiedź)
kasuje dane źródła — brak sekcji `prices`, nigdy lista częściowa ani stara.
Błąd albo pustka na JUTRO to zwykły stan przed publikacją: zostaje pełne dziś.
W logu jest nazwa akcji i typ błędu, nigdy dane akcji ani treść wyjątku.
Na zewnątrz idzie tylko wynik ŚWIEŻY (`fresh_attributes`): ostatnie odświeżenie
udane, nie starsze niż odświeżenie z zapasem i obejmujące bieżącą dobę rynku.

Znane ograniczenia: doba rynku to strefa czasowa Home Assistanta (wtyczka nie zna
strefy rynku konta). Nord Pool liczy `date` jako dobę dostawy w CET — w strefach
EET (FI, kraje bałtyckie) para dziś+jutro zaczyna się godzinę przed lokalną
północą i kończy godzinę przed końcem jutra; seria jest ciągła, przesunięta.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Awaitable, Callable, Mapping

_LOGGER = logging.getLogger(__name__)

#: Limit czasu jednego wywołania akcji.
CALL_TIMEOUT_S = 30.0
#: Zwykłe odświeżenie.
REFRESH_S = 6 * 3600
#: Odświeżenie prognozy kroczącej Ambera (cena zmienia się co 5 min).
AMBER_REFRESH_S = 30 * 60
#: Od tej godziny czasu rynku pytamy co godzinę, dopóki nie ma jutra.
TOMORROW_FROM_HOUR = 13
TOMORROW_RETRY_S = 3600
#: Odstęp po pierwszym błędzie (podwajany) i jego sufit.
BACKOFF_FIRST_S = 300
BACKOFF_MAX_S = 3600
#: Zapas ponad odświeżenie, w którym wynik jest jeszcze świeży (wywołanie z limitem
#: czasu i tik zegara co minutę mieszczą się w nim z nawiązką).
FRESH_GRACE_S = 600
#: Skale cen, które umiemy podać adapterowi jako mianownik jednostki.
_UNITS = {"KWH": "kWh", "MWH": "MWh"}
#: Ogranicznik długości listy w JEDNEJ odpowiedzi (doba 5-min to 288 wpisów,
#: prognoza Ambera kilkaset) — dłuższa lista to nie doba cen, nie budujemy jej.
MAX_RESPONSE_ENTRIES = 1500


class InvalidPriceSource(ValueError):
    """Konfiguracja źródła cen z akcji jest niepełna albo nieznana."""


class InvalidPriceResponse(ValueError):
    """Odpowiedź akcji nie ma oczekiwanego kształtu albo jest pusta."""


def _day_bounds(day: date, tz: tzinfo) -> tuple[str, str]:
    """Północ doby `day` i następnej w strefie rynku, jako ISO z offsetem."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return start.isoformat(), end.isoformat()


def _dates_as_datetimes(day: date, tz: tzinfo) -> dict[str, str]:
    start, end = _day_bounds(day, tz)
    return {"start": start, "end": end}


def _dates_as_days(day: date, _tz: tzinfo) -> dict[str, str]:
    return {"start": day.isoformat(), "end": day.isoformat()}


def _date_param(day: date, _tz: tzinfo) -> dict[str, str]:
    return {"date": day.isoformat()}


def _list(value: Any) -> list:
    if not isinstance(value, list):
        raise InvalidPriceResponse("not a list")
    if len(value) > MAX_RESPONSE_ENTRIES:
        raise InvalidPriceResponse("too long")
    return value


def _key(name: str) -> Callable[[Any, "PriceActionSource"], list]:
    def extract(response: Any, _source: "PriceActionSource") -> list:
        if not isinstance(response, Mapping):
            raise InvalidPriceResponse("not a mapping")
        return _list(response.get(name))
    return extract


def _tibber(response: Any, source: "PriceActionSource") -> list:
    homes = response.get("prices") if isinstance(response, Mapping) else None
    if not isinstance(homes, Mapping):
        raise InvalidPriceResponse("no homes")
    if source.home is not None:
        return _list(homes.get(source.home))
    if len(homes) != 1:
        # Kilka domów bez wskazania — nie zgadujemy, czyj to rachunek.
        raise InvalidPriceResponse("ambiguous home")
    return _list(next(iter(homes.values())))


def _nordpool(response: Any, source: "PriceActionSource") -> list:
    if not isinstance(response, Mapping):
        raise InvalidPriceResponse("not a mapping")
    return _list(response.get(source.area))


@dataclass(frozen=True)
class _ActionSpec:
    #: Dane od użytkownika, bez których mapowanie nie ma sensu.
    required: tuple[str, ...]
    #: Parametry dat jednej doby rynku; None = jedno wywołanie (prognoza krocząca).
    dates: Callable[[date, tzinfo], dict[str, str]] | None
    extract: Callable[[Any, "PriceActionSource"], list]
    #: Klucz atrybutu, pod którym adapter szuka listy wpisów.
    list_key: str = "prices"
    #: Waluta stała dla integracji (odpowiedź jej nie niesie).
    currency: str | None = None
    #: Skala ceny stała dla integracji (Nord Pool: za MWh) — przez jednostkę, jak w atrybutach encji.
    unit: str | None = None
    #: Integracja ma własną skalę i walutę, a odpowiedź ich nie niesie — oba pola w konfiguracji.
    needs_unit: bool = False
    #: Zwykłe odświeżenie tego źródła (s). Prognoza krocząca (Amber) zmienia się co kilka minut, więc krócej.
    refresh_s: float = REFRESH_S


_SPECS: dict[str, _ActionSpec] = {
    # Prognoza krocząca kanału (general / controlled_load / feed_in), $/kWh;
    # feed_in w odpowiedzi jest już dodatni = zarabiasz.
    "amberelectric.get_forecasts": _ActionSpec(
        required=("config_entry_id", "channel_type"), dates=None, extract=_key("forecasts"),
        list_key="forecasts", currency="AUD", refresh_s=AMBER_REFRESH_S),
    "tibber.get_prices": _ActionSpec(required=(), dates=_dates_as_datetimes, extract=_tibber),
    "nordpool.get_prices_for_date": _ActionSpec(
        required=("config_entry", "areas", "currency"), dates=_date_param, extract=_nordpool, unit="MWh"),
    # HACS: skala (`energy_scale` kWh/MWh) i waluta są ustawieniem integracji, odpowiedź ich nie niesie.
    "entsoe.get_energy_prices": _ActionSpec(
        required=("config_entry",), dates=_dates_as_datetimes, extract=_key("prices"), needs_unit=True),
    "energyzero.get_energy_prices": _ActionSpec(
        required=("config_entry", "incl_vat"), dates=_dates_as_days, extract=_key("prices"), currency="EUR"),
    "easyenergy.get_energy_usage_prices": _ActionSpec(
        required=("config_entry", "incl_vat"), dates=_dates_as_days, extract=_key("prices"), currency="EUR"),
    "easyenergy.get_energy_return_prices": _ActionSpec(
        required=("config_entry",), dates=_dates_as_days, extract=_key("prices"), currency="EUR"),
}

#: Akcje, które umiemy zamienić na ceny.
SUPPORTED_ACTIONS: frozenset[str] = frozenset(_SPECS)


def _currency_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if len(code) == 3 and code.isascii() and code.isalpha() else None


@dataclass(frozen=True)
class PriceActionSource:
    action: str
    data: Mapping[str, Any] = field(default_factory=dict)
    currency: str | None = None
    home: str | None = None
    area: str | None = None
    unit: str | None = None

    @property
    def domain(self) -> str:
        return self.action.split(".", 1)[0]

    @property
    def service(self) -> str:
        return self.action.split(".", 1)[1]

    @property
    def refresh_s(self) -> float:
        return self.spec.refresh_s

    @property
    def spec(self) -> _ActionSpec:
        return _SPECS[self.action]


def parse_source(raw: Any) -> PriceActionSource:
    """Opcja wpisu → źródło. `InvalidPriceSource`, gdy nie da się jej użyć."""
    if not isinstance(raw, Mapping):
        raise InvalidPriceSource("not a mapping")
    action = raw.get("action")
    if not isinstance(action, str) or action.strip().lower() not in _SPECS:
        raise InvalidPriceSource("unsupported action")
    action = action.strip().lower()
    spec = _SPECS[action]
    data = raw.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, Mapping) or not all(isinstance(k, str) for k in data):
        raise InvalidPriceSource("data not a mapping")
    data = dict(data)
    if any(data.get(k) in (None, "", []) for k in spec.required):
        raise InvalidPriceSource("missing data")

    currency = raw.get("currency")
    if currency is not None:
        currency = _currency_code(currency)
        if currency is None:
            raise InvalidPriceSource("bad currency")
    home = raw.get("home")
    if home is not None and not isinstance(home, str):
        raise InvalidPriceSource("bad home")

    area = None
    if action == "nordpool.get_prices_for_date":
        areas = data["areas"]
        areas = [areas] if isinstance(areas, str) else areas
        # Jedna strefa: odpowiedź jest słownikiem stref, a blok cen ma jedną serię.
        if not isinstance(areas, list) or len(areas) != 1 or not isinstance(areas[0], str):
            raise InvalidPriceSource("one area required")
        area = areas[0].strip().upper()
        data["areas"] = [area]
        currency = _currency_code(data["currency"])
        if currency is None:
            raise InvalidPriceSource("bad currency")
        data["currency"] = currency
    if spec.currency is not None:
        currency = spec.currency
    unit = spec.unit
    if spec.needs_unit:
        raw_unit = raw.get("unit")
        unit = _UNITS.get(raw_unit.strip().upper()) if isinstance(raw_unit, str) else None
        if unit is None:
            raise InvalidPriceSource("missing unit")
        if currency is None:
            raise InvalidPriceSource("missing currency")
    return PriceActionSource(action=action, data=data, currency=currency, home=home or None, area=area,
                             unit=unit)


def build_calls(source: PriceActionSource, now: datetime, tz: tzinfo) -> list[tuple[int | None, dict]]:
    """Wywołania jednego odświeżenia: [(przesunięcie doby albo None, dane akcji)].

    Doby liczone w strefie RYNKU (`tz`), nie w UTC — o 23:30Z w Warszawie jest
    już jutro. Parametry dat nadpisują ewentualne klucze o tej nazwie od użytkownika.
    """
    spec = source.spec
    if spec.dates is None:
        return [(None, dict(source.data))]
    today = now.astimezone(tz).date()
    return [(offset, {**source.data, **spec.dates(today + timedelta(days=offset), tz)}) for offset in (0, 1)]


def _start_of(entry: Any) -> datetime | None:
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("start_time")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else None
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def attributes_from_responses(
    source: PriceActionSource, today_response: Any, tomorrow_response: Any = None, *,
    today: date, tz: tzinfo,
) -> tuple[dict[str, Any], date]:
    """Odpowiedzi akcji → (atrybuty dla adaptera, ostatnia doba rynku w danych).

    `today_response` musi dać niepustą listę (inaczej `InvalidPriceResponse`).
    `tomorrow_response` może być None, wyjątkiem albo pustą listą — wtedy dane
    obejmują samo dziś. Zła odpowiedź na jutro też oznacza „jutra nie ma”, nie
    psuje dziś; ciągłość szwu dziś/jutro sprawdza dalej adapter.
    """
    spec = source.spec
    entries = list(spec.extract(today_response, source))
    if not entries:
        raise InvalidPriceResponse("empty")
    last_day = today
    if spec.dates is None:
        # Prognoza krocząca (Amber): ostatnia doba rynku, w której zaczyna się wpis.
        starts = [s for s in map(_start_of, entries) if s is not None]
        if starts:
            last_day = max(starts).astimezone(tz).date()
    elif tomorrow_response is not None and not isinstance(tomorrow_response, BaseException):
        try:
            tomorrow = spec.extract(tomorrow_response, source)
        except InvalidPriceResponse:
            tomorrow = []
        if tomorrow:
            entries += tomorrow
            last_day = today + timedelta(days=1)
            if len(entries) > MAX_RESPONSE_ENTRIES:
                raise InvalidPriceResponse("too long")

    attrs: dict[str, Any] = {spec.list_key: entries}
    if source.currency is not None:
        attrs["currency"] = source.currency
        if source.unit is not None:
            attrs["unit_of_measurement"] = f"{source.currency}/{source.unit}"
    return attrs, last_day


def backoff_s(failures: int) -> float:
    """Odstęp po `failures` kolejnych błędach: 5, 10, 20, 40, 60, 60… min."""
    if failures <= 0:
        return 0.0
    return float(min(BACKOFF_FIRST_S * 2 ** min(failures - 1, 16), BACKOFF_MAX_S))


Caller = Callable[[str, str, dict], Awaitable[Any]]


class PriceActionPoller:
    """Odświeżanie jednego źródła cen z akcji; wynik w `attributes` (None = brak cen).

    `async_refresh` nigdy nie rzuca (poza anulowaniem zadania) i nie startuje
    drugi raz, gdy poprzednie odświeżenie jeszcze trwa.
    """

    def __init__(self, source: PriceActionSource, call: Caller, *, timeout_s: float = CALL_TIMEOUT_S) -> None:
        self.source = source
        self._call = call
        self._timeout_s = timeout_s
        self.attributes: dict[str, Any] | None = None
        self.busy = False
        self._last_ok: datetime | None = None
        self._last_try: datetime | None = None
        self._last_day: date | None = None
        self._failures = 0

    def due(self, now: datetime, tz: tzinfo) -> bool:
        if self.busy:
            return False
        if self._last_try is None:
            return True
        if self._failures:
            return (now - self._last_try).total_seconds() >= backoff_s(self._failures)
        age = (now - self._last_ok).total_seconds() if self._last_ok else math.inf
        if age >= self.source.refresh_s:
            return True
        local = now.astimezone(tz)
        today = local.date()
        no_tomorrow = self._last_day is None or self._last_day <= today
        if self._last_day is not None and self._last_day < today:
            return age >= TOMORROW_RETRY_S          # dane nie obejmują już nawet dziś
        return local.hour >= TOMORROW_FROM_HOUR and no_tomorrow and age >= TOMORROW_RETRY_S

    def fresh_attributes(self, now: datetime, tz: tzinfo) -> dict[str, Any] | None:
        """Wynik do wysłania albo None: tylko z udanego, niedawnego odświeżenia obejmującego dziś.

        Po błędzie `attributes` jest już None; tu dochodzi wynik, który się zestarzał (kolejne
        odświeżenie nie doszło do skutku) albo nie ma już bieżącej doby (minęła północ bez jutra).
        """
        if self.attributes is None or self._last_ok is None or self._last_day is None:
            return None
        if (now - self._last_ok).total_seconds() >= self.source.refresh_s + FRESH_GRACE_S:
            return None
        if self._last_day < now.astimezone(tz).date():
            return None
        return self.attributes

    async def _async_call(self, data: dict) -> Any:
        async with asyncio.timeout(self._timeout_s):
            return await self._call(self.source.domain, self.source.service, data)

    async def async_refresh(self, now: datetime, tz: tzinfo) -> bool:
        """Jedno odświeżenie; True = są ceny. Błąd na dziś kasuje dane źródła."""
        if self.busy:
            return False
        self.busy = True
        try:
            return await self._async_refresh(now, tz)
        finally:
            self.busy = False

    async def _async_refresh(self, now: datetime, tz: tzinfo) -> bool:
        self._last_try = now
        calls = build_calls(self.source, now, tz)
        try:
            today_response = await self._async_call(calls[0][1])
            tomorrow_response = None
            if len(calls) > 1:
                try:
                    tomorrow_response = await self._async_call(calls[1][1])
                except Exception as err:  # noqa: BLE001 — jutro przed publikacją to nie błąd źródła
                    _LOGGER.debug("Volcast price action %s: no prices for tomorrow yet (%s)",
                                  self.source.action, type(err).__name__)
            attrs, last_day = attributes_from_responses(
                self.source, today_response, tomorrow_response, today=now.astimezone(tz).date(), tz=tz)
        except Exception as err:  # noqa: BLE001 — błąd źródła nigdy nie wychodzi dalej
            self.attributes = None
            self._failures += 1
            level = logging.WARNING if self._failures == 1 else logging.DEBUG
            _LOGGER.log(level, "Volcast price action %s failed (%s); prices from it are skipped",
                        self.source.action, type(err).__name__)
            return False
        self.attributes = attrs
        self._last_ok, self._last_day, self._failures = now, last_day, 0
        return True
