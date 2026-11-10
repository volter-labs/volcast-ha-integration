"""Integracje cenowe Home Assistanta jako DANE — jedyne miejsce wtyczki, które zna je z nazwy.

Warstwa CZYSTA — bez importów Home Assistanta (jak `core/prices.py`). Jeden wpis na integrację:
domena HA, które jej encje są cenami prądu, wskazówki ról (kupno/sprzedaż), akcje z odpowiedzią
(nazwa, szablon danych, pola od użytkownika, kształt odpowiedzi), waluta, skala i odświeżanie.

Kod (wykrywanie, formularz opcji, onboarding, `price_setup`, `price_actions`, `prices`) czyta te
dane ogólnie. Tam, gdzie integracje różnią się RODZAJEM zachowania, wpis wybiera nazwaną zdolność
(enum poniżej), nigdy własną funkcję ani `if domena == …`. Nowa integracja = nowy wpis.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class EntityUse(Enum):
    """Co wtyczka robi z encjami integracji."""
    #: Encji nie wykrywamy; wpis niesie tylko wiedzę o kształcie ceny (adapter atrybutów).
    NONE = "none"
    #: Encje idą do raportu wykrywania, ale nie podpowiadamy ich w konfiguracji — kształtu
    #: atrybutów nie potwierdziliśmy (fixture albo kod źródłowy integracji).
    REPORTED = "reported"
    #: Raport wykrywania i podpowiedź w konfiguracji (onboarding, wykryte źródła w opcjach).
    OFFERED = "offered"


class Dates(Enum):
    """Parametry dat jednej doby rynku, które `price_actions` dokłada do wywołania akcji."""
    #: Bez dat: jedno wywołanie, prognoza krocząca od teraz.
    ROLLING = "rolling"
    #: `{"date": "RRRR-MM-DD"}`.
    DATE = "date"
    #: `{"start": dzień, "end": ten sam dzień}` (same daty).
    DAYS = "days"
    #: `{"start": północ, "end": następna północ}` (ISO z offsetem strefy rynku).
    DATETIMES = "datetimes"


class ResponseShape(Enum):
    """Gdzie w odpowiedzi akcji leży lista wpisów cen."""
    #: Lista pod kluczem `response_key`.
    KEY = "key"
    #: Pod `response_key` słownik domów → lista; dom z opcji `home` albo jedyny.
    BY_HOME = "by_home"
    #: Słownik stref → lista; strefa ze źródła (dane akcji pod `area_key`).
    BY_AREA = "by_area"


class FieldKind(Enum):
    """Rodzaj pola formularza: widżet i normalizacja wartości."""
    #: Tekst wolny, zapisany bez zmian; pusty = brak.
    TEXT = "text"
    #: Dokładnie jedna strefa rynku (bez przecinków), bez spacji na brzegach.
    AREA = "area"
    #: Kod waluty, bez spacji, wielkimi literami.
    CURRENCY = "currency"
    #: Skala ceny (`kWh` / `MWh`), bez spacji na brzegach.
    UNIT = "unit"
    #: Tak/nie.
    FLAG = "flag"


class Target(Enum):
    """Gdzie trafia wartość pola formularza w zapisanej opcji źródła."""
    #: Klucz opcji obok `action` / `data` (czyta go `price_actions.parse_source`).
    OPTION = "option"
    #: Klucz danych akcji (idzie do Home Assistanta w wywołaniu).
    DATA = "data"


@dataclass(frozen=True)
class FormField:
    """Pole, o które formularz pyta użytkownika przy źródle z akcji."""
    #: Nazwa pola formularza (klucz tłumaczenia w kroku `price_source_details`).
    name: str
    kind: FieldKind
    required: bool
    target: Target
    #: Klucz w opcji albo w danych akcji.
    key: str


@dataclass(frozen=True)
class PriceAction:
    """Akcja HA z odpowiedzią (`return_response`), z której umiemy czytać ceny."""
    #: „domena.usługa”.
    name: str
    #: Klucze danych akcji, bez których źródło nie działa.
    required: tuple[str, ...]
    dates: Dates
    response: ResponseShape = ResponseShape.KEY
    #: Klucz listy (KEY) albo słownika domów (BY_HOME) w odpowiedzi.
    response_key: str | None = "prices"
    #: Klucz atrybutu, pod którym adapter atrybutów szuka listy wpisów.
    list_key: str = "prices"
    #: Waluta stała dla integracji (odpowiedź jej nie niesie).
    currency: str | None = None
    #: Skala ceny stała dla integracji — przez jednostkę, jak w atrybutach encji.
    unit: str | None = None
    #: Skala i waluta są ustawieniem integracji, odpowiedź ich nie niesie — oba pola w opcji źródła.
    needs_unit: bool = False
    #: Klucz danych akcji z JEDNĄ strefą rynku (odpowiedź ma serię na strefę).
    area_key: str | None = None
    #: Klucz danych akcji z walutą (wymaganą) — to też waluta serii.
    currency_key: str | None = None
    #: Własne zwykłe odświeżenie (s); None = odświeżenie domyślne.
    refresh_s: float | None = None


@dataclass(frozen=True)
class SourceSide:
    """Szablon jednej strony (kupno albo sprzedaż) źródła z akcji."""
    action: str
    #: Klucz danych akcji z id wpisu integracji; None = akcja go nie przyjmuje.
    entry_key: str | None = "config_entry"
    #: Dane stałe (np. kanał), w tej kolejności w zapisanej opcji.
    data: tuple[tuple[str, Any], ...] = ()
    #: Nazwy pól formularza (`PriceIntegration.form`), które ta strona przyjmuje.
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScaledInteger:
    """Cena zagnieżdżona we wpisie serii jako liczba CAŁKOWITA w ułamkowej skali waluty.

    Ułamek pod tą ścieżką oznacza inną skalę — wpis jest wtedy nie do odczytania.
    """
    path: tuple[str, ...]
    #: Wartość ÷ dzielnik = cena w walucie.
    divisor: int


@dataclass(frozen=True)
class PriceIntegration:
    #: Domena integracji HA = platforma jej encji = domena jej akcji.
    domain: str
    entities: EntityUse = EntityUse.OFFERED
    #: Domeny encji HA, w których integracja trzyma serie cen.
    entity_domains: tuple[str, ...] = ("sensor",)
    #: Człony id encji (dzielone po `.` i `_`; „a_b” = kolejne człony), które znaczą „to nie cena prądu”.
    excluded_tokens: tuple[str, ...] = ()
    #: Podciągi id encji, które znaczą stronę sprzedaży (dokładane do ogólnego słownika ról).
    sell_words: tuple[str, ...] = ()
    #: Człony id encji mówiące tylko o stronie — odpadają przy szukaniu „pnia” pary kupno/sprzedaż.
    side_tokens: tuple[str, ...] = ()
    #: Waluta kraju integracji, gdy seria jej nie podaje (integracja działa w jednym kraju).
    currency: str | None = None
    #: (początek jednostki, waluta): jednostka zaczyna się symbolem, którego ogólny adapter nie
    #: rozstrzyga (np. `$`), a dla tej integracji jest jednoznaczny. Atrybut `currency` wygrywa.
    symbol_currency: tuple[tuple[str, str], ...] = ()
    #: Ceny zagnieżdżone we wpisach serii (gdy wpis nie ma zwykłego klucza ceny).
    scaled_values: tuple[ScaledInteger, ...] = ()
    #: Akcje z odpowiedzią, które umiemy zamienić na ceny.
    actions: tuple[PriceAction, ...] = ()
    #: Źródło z akcji podpowiadane w konfiguracji: kupno, opcjonalnie sprzedaż, pola formularza.
    buy: SourceSide | None = None
    sell: SourceSide | None = None
    form: tuple[FormField, ...] = ()


_AMBER_FORECASTS = "amberelectric.get_forecasts"
_TIBBER_PRICES = "tibber.get_prices"
_NORDPOOL_PRICES = "nordpool.get_prices_for_date"
_ENTSOE_PRICES = "entsoe.get_energy_prices"
_ENERGYZERO_PRICES = "energyzero.get_energy_prices"
_EASYENERGY_USAGE = "easyenergy.get_energy_usage_prices"
_EASYENERGY_RETURN = "easyenergy.get_energy_return_prices"

_CURRENCY_REQUIRED = FormField("currency", FieldKind.CURRENCY, True, Target.OPTION, "currency")
_INCL_VAT = FormField("incl_vat", FieldKind.FLAG, True, Target.DATA, "incl_vat")

#: Kolejność wpisów = kolejność integracji z akcją w podpowiedziach onboardingu.
INTEGRATIONS: tuple[PriceIntegration, ...] = (
    # Amber (rdzeń HA, tylko Australia). Prognoza krocząca kanału (general / controlled_load / feed_in)
    # w $/kWh; feed_in w odpowiedzi jest już dodatni = zarabiasz. Cena zmienia się co 5 min, więc
    # odświeżamy częściej. Kanał controlled_load to osobny obwód (np. bojler), nie cena domu.
    PriceIntegration(
        domain="amberelectric",
        excluded_tokens=("controlled_load",),
        sell_words=("feed_in", "feedin"),
        side_tokens=("general", "feed", "in", "feedin"),
        currency="AUD",
        symbol_currency=(("$", "AUD"),),
        actions=(PriceAction(_AMBER_FORECASTS, required=("config_entry_id", "channel_type"), dates=Dates.ROLLING,
                             response_key="forecasts", list_key="forecasts", currency="AUD", refresh_s=30 * 60),),
        buy=SourceSide(_AMBER_FORECASTS, entry_key="config_entry_id", data=(("channel_type", "general"),)),
        sell=SourceSide(_AMBER_FORECASTS, entry_key="config_entry_id", data=(("channel_type", "feed_in"),)),
    ),
    # Tibber (rdzeń HA): odpowiedź to słownik domów konta; waluty nie niesie.
    PriceIntegration(
        domain="tibber",
        actions=(PriceAction(_TIBBER_PRICES, required=(), dates=Dates.DATETIMES, response=ResponseShape.BY_HOME),),
        buy=SourceSide(_TIBBER_PRICES, entry_key=None, fields=("home", "currency")),
        form=(FormField("home", FieldKind.TEXT, False, Target.OPTION, "home"),
              FormField("currency", FieldKind.CURRENCY, False, Target.OPTION, "currency")),
    ),
    # Nord Pool (rdzeń HA): ceny za MWh, odpowiedź to słownik stref. `areas` wysyłamy jako jeden tekst,
    # nie listę: starszy rdzeń HA sprawdza `areas` przez `vol.In` PRZED `ensure_list`, więc lista nie
    # przechodzi walidacji; tekst przyjmuje każda wersja.
    # Znane ograniczenie: `date` to doba dostawy w CET. W strefach EET (FI, kraje bałtyckie) doba CET
    # zaczyna się o 01:00 lokalnie, więc para dziś+jutro zaczyna się godzinę PO lokalnej północy i kończy
    # godzinę po końcu jutra; seria jest ciągła, przesunięta. Między 00:00 a 01:00 lokalnie bieżącej
    # godziny w serii nie ma.
    PriceIntegration(
        domain="nordpool",
        actions=(PriceAction(_NORDPOOL_PRICES, required=("config_entry", "areas", "currency"), dates=Dates.DATE,
                             response=ResponseShape.BY_AREA, response_key=None, unit="MWh",
                             area_key="areas", currency_key="currency"),),
        buy=SourceSide(_NORDPOOL_PRICES, fields=("area", "currency")),
        form=(FormField("area", FieldKind.AREA, True, Target.DATA, "areas"),
              FormField("currency", FieldKind.CURRENCY, True, Target.DATA, "currency")),
    ),
    # ENTSO-e (HACS): skala (`energy_scale` kWh/MWh) i waluta są ustawieniem integracji, odpowiedź ich nie
    # niesie. Atrybut `prices` encji bywa wczoraj + dziś + jutro.
    PriceIntegration(
        domain="entsoe",
        actions=(PriceAction(_ENTSOE_PRICES, required=("config_entry",), dates=Dates.DATETIMES, needs_unit=True),),
        buy=SourceSide(_ENTSOE_PRICES, fields=("unit", "currency")),
        form=(FormField("unit", FieldKind.UNIT, True, Target.OPTION, "unit"), _CURRENCY_REQUIRED),
    ),
    # EnergyZero (rdzeń HA, Holandia): odrzuca całe wywołanie, gdy jutra jeszcze nie opublikowano —
    # stąd dziś i jutro osobnymi wywołaniami (dotyczy każdej akcji z datami).
    PriceIntegration(
        domain="energyzero",
        actions=(PriceAction(_ENERGYZERO_PRICES, required=("config_entry", "incl_vat"), dates=Dates.DAYS,
                             currency="EUR"),),
        buy=SourceSide(_ENERGYZERO_PRICES, fields=("incl_vat",)),
        form=(_INCL_VAT,),
    ),
    # EasyEnergy (rdzeń HA, Holandia): osobne akcje poboru i oddania; VAT dotyczy tylko poboru.
    PriceIntegration(
        domain="easyenergy",
        actions=(PriceAction(_EASYENERGY_USAGE, required=("config_entry", "incl_vat"), dates=Dates.DAYS,
                             currency="EUR"),
                 PriceAction(_EASYENERGY_RETURN, required=("config_entry",), dates=Dates.DAYS, currency="EUR")),
        buy=SourceSide(_EASYENERGY_USAGE, fields=("incl_vat",)),
        sell=SourceSide(_EASYENERGY_RETURN),
        form=(_INCL_VAT,),
    ),
    # Octopus Energy (HACS, Wielka Brytania): stawki w encjach `event` (`…_current_day_rates`); encje gazu
    # mają kształt cen prądu, ale prądem nie są.
    PriceIntegration(
        domain="octopus_energy",
        entity_domains=("sensor", "event"),
        excluded_tokens=("gas",),
        currency="GBP",
    ),
    # Pstryk (HACS, Polska): lista „All prices” wpisów bez końca przedziału (długość z odstępu startów).
    PriceIntegration(domain="pstryk", currency="PLN"),
    # Frank Energie (HACS): `prices` z wpisami `from` / `till` / `price` — kształt potwierdzony fixture'em
    # `tests/fixtures/prices/frank_energie_prices.json`.
    PriceIntegration(domain="frank_energie"),
    # EPEX Spot (HACS): domena potwierdzona, kształt atrybutów nie — tylko raport wykrywania.
    PriceIntegration(domain="epex_spot", entities=EntityUse.REPORTED),
    # Domeny nie udało się potwierdzić w żadnym źródle; zostaje w raporcie (nieznana domena nic nie łapie).
    PriceIntegration(domain="pstryk_aio", entities=EntityUse.REPORTED),
    # Zonneplan (HACS): cena jako `price_tax_included.amount` w skali 1e-7 waluty. Encji nie wykrywamy
    # (domena niepotwierdzona); kształt ceny rozumie adapter atrybutów każdej encji.
    PriceIntegration(
        domain="zonneplan_one",
        entities=EntityUse.NONE,
        scaled_values=(ScaledInteger(("price_tax_included", "amount"), 10_000_000),),
    ),
)

_BY_DOMAIN: dict[str, PriceIntegration] = {i.domain: i for i in INTEGRATIONS}

#: Akcje z odpowiedzią po nazwie („domena.usługa”).
ACTIONS: dict[str, PriceAction] = {a.name: a for i in INTEGRATIONS for a in i.actions}

#: Platformy encji w raporcie wykrywania.
REPORTED_PLATFORMS: frozenset[str] = frozenset(i.domain for i in INTEGRATIONS if i.entities is not EntityUse.NONE)
#: Platformy encji podpowiadane w konfiguracji (podzbiór raportowanych).
OFFERED_PLATFORMS: frozenset[str] = frozenset(i.domain for i in INTEGRATIONS if i.entities is EntityUse.OFFERED)
#: Domeny wpisów integracji, z których akcji umiemy zbudować źródło (kolejność jak w `INTEGRATIONS`).
ACTION_DOMAINS: tuple[str, ...] = tuple(i.domain for i in INTEGRATIONS if i.buy is not None)
#: Domeny encji HA z cenami podpowiadanych integracji (bez powtórzeń, w kolejności wpisów).
ENTITY_DOMAINS: tuple[str, ...] = tuple(dict.fromkeys(
    d for i in INTEGRATIONS if i.entities is EntityUse.OFFERED for d in i.entity_domains))
#: Ceny zagnieżdżone, które adapter atrybutów sprawdza w każdym wpisie serii.
SCALED_VALUES: tuple[ScaledInteger, ...] = tuple(v for i in INTEGRATIONS for v in i.scaled_values)
#: Wskazówki ról ze wszystkich wpisów — nazwy encji bywają wspólne, więc działają dla każdej platformy.
SELL_WORDS: tuple[str, ...] = tuple(dict.fromkeys(w for i in INTEGRATIONS for w in i.sell_words))
SIDE_TOKENS: frozenset[str] = frozenset(t for i in INTEGRATIONS for t in i.side_tokens)


def integration(domain: str | None) -> PriceIntegration | None:
    """Wpis integracji po domenie (= platformie encji); None dla nieznanej."""
    return _BY_DOMAIN.get(domain) if isinstance(domain, str) else None
