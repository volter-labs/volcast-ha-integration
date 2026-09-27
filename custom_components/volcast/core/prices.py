"""Normalizacja atrybutów cenowych encji Home Assistanta do przedziałów.

Warstwa CZYSTA — bez importów Home Assistanta, żeby dała się testować zwykłym
`pytest` (tak jak `guards` i `schedule`). Odczyt stanu encji i wysyłka bloku
`prices` w telemetrii to sprawa wołającego (telemetria); tutaj jest sama
zamiana atrybutów na listę przedziałów.

Kontrakt (odpowiednik TS-owego `PriceInterval` po stronie chmury):

    {"startAt": "2026-09-14T22:00:00Z",  # ISO 8601, chwila absolutna, UTC
     "minutes": 15 | 30 | 60,
     "buy": float,
     "sell": float | None,
     "currency": "PLN"}

Dwa źródła atrybutów (pierwszeństwo w tej kolejności):

1. `raw_today` / `raw_tomorrow` — listy wpisów `{start, end, value}` (konwencja
   Nord Pool i pochodnych). Długość przedziału bierzemy z `end - start`.
2. `today` / `tomorrow` — listy samych liczb plus `interval_min` (domyślnie 60);
   chwile odtwarzamy od północy dnia lokalnego.

REGUŁA NADRZĘDNA: dane niespójne (inna długość przedziału, wartość nieliczbowa,
start bez strefy) dają PUSTĄ listę, nigdy listę częściową. Dziurawa doba cen jest
dla plannera gorsza niż brak cen — brak umie obsłużyć (degradacja do poprzedniego
dnia), dziury nie widzi wcale i planuje tak, jakby tych godzin nie było.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Mapping, Sequence

#: Rozdzielczości, które chmura przyjmuje w `PriceInterval.minutes`.
DOZWOLONE_MINUTY: frozenset[int] = frozenset({15, 30, 60})

#: Domyślna długość przedziału w wariancie fallback (`today` / `tomorrow`).
DOMYSLNY_INTERVAL_MIN = 60

#: Ogranicznik rozmiaru POJEDYNCZEJ listy (dzisiaj albo jutro): dwie doby.
#: Encja z dłuższą serią nie jest dobą cen, którą umiemy zinterpretować —
#: nie budujemy z niej wielotysięcznej listy. Limit jest na listę, nie na sumę,
#: bo dzisiaj+jutro w dobie zmiany czasu daje legalne 49 godzin.
MAX_MINUT_SERII = 60 * 24 * 2

#: Litery skandynawskie i polskie w symbolach jednostek (`Øre` DKK/NOK, `Öre`
#: SEK, `zł` PLN) — sprowadzamy je do zwykłych liter ASCII, żeby porównanie
#: z listami niżej działało bez względu na diakrytyk.
_DIAKRYTYKI = str.maketrans({"Ø": "O", "ø": "o", "Ö": "O", "ö": "o", "Ł": "L", "ł": "l"})


def _znormalizuj(tekst: str) -> str:
    """Wielkie litery, bez diakrytyków — postać do porównań z listami liczników."""
    return tekst.translate(_DIAKRYTYKI).strip().upper()


#: Tokeny centowe (setne części waluty), po normalizacji i odcięciu kropki na
#: końcu (`ct.`, `gr.`) → waluta, którą NAZWA tokenu jednoznacznie wskazuje,
#: albo None, gdy nazwa jest wspólna wielu walutom (nie da się zgadnąć bez
#: atrybutu `currency`). `p`/`pence` to grosz brytyjski (GBP), `gr`/`grosz(e)`
#: to grosz polski (PLN) — to JEDYNE dwa jednoznaczne tokeny w tym zestawie.
_TOKENY_CENTOWE: dict[str, str | None] = {
    "CT": None, "C": None, "CENT": None, "CENTS": None, "¢": None,
    "ORE": None,  # øre/öre po normalizacji diakrytyków
    "P": "GBP", "PENCE": "GBP",
    "GR": "PLN", "GROSZ": "PLN", "GROSZE": "PLN",
}

#: Symbole walut spoza ISO 4217 (nie trzyliterowe), po normalizacji → kod.
_SYMBOLE_WALUT: dict[str, str] = {"ZL": "PLN", "€": "EUR", "£": "GBP"}

#: Symbole/skróty walutowe WIELOZNACZNE: licznik jest rozpoznany (jednostka
#: przechodzi walidację, mianownik ma sens), ale sam NIE wskazuje jednej
#: konkretnej waluty (korona i dolar są w wielu krajach) — waluta musi wtedy
#: przyjść z atrybutu `currency`.
_SYMBOLE_NIEJEDNOZNACZNE: frozenset[str] = frozenset({"KR", "$"})


#: Mianowniki jednostki, które kontrakt rozpoznaje jako cenę energii, razem z
#: DZIELNIKIEM do ceny „za kWh" — CAŁKOWITYM, żeby dzielenie było dokładne
#: (mnożenie przez 0.001/0.01 nie jest, bo te ułamki nie mają skończonego
#: rozwinięcia binarnego). `MWh` przeliczamy dokładnie (÷1000) — to
#: jednoznaczna konwersja, nie zgadywanie. Każdy inny mianownik (np. `Wh`)
#: odrzuca encję CAŁKOWICIE.
_DZIELNIKI_MIANOWNIKA: dict[str, int] = {"KWH": 1, "MWH": 1000}

#: Kod waluty kontraktu: dokładnie trzy litery ASCII (ISO 4217). „zł", „euro"
#: czy „PL" walutą w rozumieniu chmury nie są. Sam KSZTAŁT (trzyliterowy ASCII)
#: wystarcza dla atrybutu `currency`/fallbacku integracji — to pole wprost
#: deklaruje walutę, użytkownik już ją wybrał w opcjach integracji.
_KOD_WALUTY = re.compile(r"^[A-Z]{3}$")

#: Kody walut ISO 4217 dopuszczone jako LICZNIK jednostki (`unit_of_measurement`).
#: W przeciwieństwie do atrybutu `currency` (kształt wystarczy), licznik jednostki
#: jest tekstem wolnym z encji obcej integracji — samokształt trzyliterowy nie
#: wystarcza (np. „foo/kWh" ma kształt kodu, ale kodem nie jest), więc licznik
#: sprawdzamy wprost względem realnych kodów walut.
_KODY_WALUT_ISO: frozenset[str] = frozenset({
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
    "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL",
    "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "CNY",
    "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP",
    "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD",
    "GNF", "GTQ", "GYD", "HKD", "HNL", "HRK", "HTG", "HUF", "IDR", "ILS",
    "INR", "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR",
    "KMF", "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD",
    "LSL", "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU",
    "MUR", "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK",
    "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG",
    "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK",
    "SGD", "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SYP", "SZL", "THB",
    "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX",
    "USD", "UYU", "UZS", "VES", "VND", "VUV", "WST", "XAF", "XCD", "XOF",
    "XPF", "YER", "ZAR", "ZMW", "ZWL",
})


def _kod_waluty(tekst: Any) -> str | None:
    """Znormalizowany kod KSZTAŁTEM ISO 4217 (trzy litery ASCII) albo None."""
    if not isinstance(tekst, str):
        return None
    kod = tekst.strip().upper()
    return kod if kod.isascii() and _KOD_WALUTY.match(kod) else None


def _kod_waluty_iso(tekst: Any) -> str | None:
    """Jak `_kod_waluty`, ale sprawdzony też względem REALNYCH kodów ISO 4217
    (`_KODY_WALUT_ISO`), nie tylko kształtu. Atrybut `currency` i fallback z
    opcji integracji to tekst wolny — obcy integrator albo ręczny wpis
    użytkownika — więc sam kształt trzyliterowy nie odróżnia „FOO" od
    prawdziwego kodu, tak samo jak przy liczniku jednostki."""
    kod = _kod_waluty(tekst)
    return kod if kod in _KODY_WALUT_ISO else None


def _rozbierz_jednostke(attrs: Mapping[str, Any]) -> tuple[float, bool, str | None] | None:
    """`unit_of_measurement` (+ `price_in_cents`) → (dzielnik, czy centowa, kod z licznika).

    None oznacza, że jednostka jest OBECNA i JEDNOZNACZNIE nie jest ceną
    energii kontraktu — encja jest wtedy odrzucana CAŁKOWICIE, bez względu na
    to, co mówi atrybut `currency`. Takie przypadki:
    - mianownik inny niż kWh/MWh (np. `Wh`);
    - licznik PUSTY (np. `/kWh`) albo spoza jawnej listy dozwolonych: realny
      kod ISO 4217 (zbiór `_KODY_WALUT_ISO` — sam KSZTAŁT trzyliterowy nie
      wystarcza, „foo" go ma i mimo to jest odrzucany), symbol jednoznacznej
      waluty (`zł`, `€`, `£`), symbol wieloznaczny (`kr`, `$` — jednostka OK,
      ale sama waluta wymaga atrybutu) albo token centowy (`ct`, `c`,
      `cent(s)`, `¢`, `øre`/`öre`, `p`/`pence`, `gr`/`grosz(e)`, z opcjonalną
      kropką na końcu jak `ct.`/`gr.`). Zgadywanie JAKIEGOKOLWIEK innego,
      nierozpoznanego licznika (np. „foo/kWh") to ta sama pomyłka rzędu
      wielkości co centy czy MWh bez kontroli — dlatego licznik spoza listy
      odrzuca encję, NAWET gdy atrybut `currency` jest podany wprost;
    - atrybut `price_in_cents` PRZECZY jednostce — jawne `True` przy liczniku,
      który oznacza CAŁĄ walutę (kod ISO, symbol jednoznaczny `zł`/`€`/`£`
      albo symbol wieloznaczny `kr`/`$`; `EUR/kWh`, `€/kWh` i `kr/kWh`+`SEK`
      nie mogą być jednocześnie "w euro"/"w koronach" i "w centach"), albo
      jawne `False` przy liczniku centowym (`ct/kWh`). Tokeny centowe `p`/`gr`
      SĄ centami, więc `price_in_cents=True` obok nich nie jest sprzecznością.

    Brak jednostki albo jednostka bez ukośnika nie niesie informacji o
    mianowniku (nie ma z czym uzgadniać sprzeczności), ale `price_in_cents`
    nadal jest HONOROWANY jako jedyne źródło skali — waluta i tak przychodzi
    skądinąd (`currency` albo fallback), więc sama flaga niczego nie blokuje.
    """
    jednostka = attrs.get("unit_of_measurement")
    if not isinstance(jednostka, str) or "/" not in jednostka:
        centowy_bez_ukosnika = attrs.get("price_in_cents") is True
        return (100.0 if centowy_bez_ukosnika else 1.0), centowy_bez_ukosnika, None

    licznik, _, mianownik = jednostka.partition("/")
    dzielnik_mianownika = _DZIELNIKI_MIANOWNIKA.get(mianownik.strip().upper())
    if dzielnik_mianownika is None:
        return None

    # Kropka na końcu skrótu (`ct.`, `gr.`) to ten sam token co bez niej.
    licznik_znorm = _znormalizuj(licznik).rstrip(".")
    if not licznik_znorm:
        return None  # licznik pusty (np. "/kWh") — nic do rozpoznania

    centowy = False
    kod_z_jednostki: str | None = None
    # Licznik oznacza CAŁĄ walutę (kod ISO, symbol jednoznaczny albo symbol
    # wieloznaczny) — w przeciwieństwie do tokenu centowego, to `price_in_cents
    # =True` obok niego zawsze jest sprzecznością, niezależnie od tego, czy
    # sama waluta jest już znana (`kod_z_jednostki`) czy dopiero czeka na atrybut.
    jednostka_calej_waluty = False

    if licznik_znorm in _TOKENY_CENTOWE:
        centowy = True
        kod_z_jednostki = _TOKENY_CENTOWE[licznik_znorm]
    elif licznik_znorm in _SYMBOLE_WALUT:
        kod_z_jednostki = _SYMBOLE_WALUT[licznik_znorm]
        jednostka_calej_waluty = True
    elif licznik_znorm in _SYMBOLE_NIEJEDNOZNACZNE:
        kod_z_jednostki = None
        jednostka_calej_waluty = True
    elif licznik_znorm in _KODY_WALUT_ISO:
        kod_z_jednostki = licznik_znorm
        jednostka_calej_waluty = True
    else:
        # Licznik nierozpoznany — nie jest realnym kodem ISO 4217 (np. „foo",
        # mimo trzyliterowego kształtu), symbolem ani tokenem centowym z list
        # wyżej — odrzucamy CAŁKOWICIE, nawet z atrybutem `currency` obecnym
        # (ten sam błąd co centy/MWh bez kontroli: zgadywanie nierozpoznanego
        # licznika).
        return None

    centy_flaga = attrs.get("price_in_cents")
    centy_flaga = centy_flaga if isinstance(centy_flaga, bool) else None
    if centy_flaga is True:
        if jednostka_calej_waluty:
            return None
        centowy = True
    elif centy_flaga is False and centowy:
        return None

    return float(dzielnik_mianownika * (100 if centowy else 1)), centowy, kod_z_jednostki


# ── Pomocnicze parsery ──────────────────────────────────────────────────────


def _liczba(wartosc: Any) -> float | None:
    """Skończona liczba albo None. Bool NIE jest ceną, mimo że jest int-em."""
    if isinstance(wartosc, bool) or wartosc is None:
        return None
    if isinstance(wartosc, (int, float)):
        liczba = float(wartosc)
    elif isinstance(wartosc, str):
        try:
            liczba = float(wartosc.strip().replace(",", "."))
        except ValueError:
            return None
    else:
        return None
    return liczba if math.isfinite(liczba) else None


def _chwila(wartosc: Any) -> datetime | None:
    """Chwila absolutna z tekstu ISO albo z `datetime`.

    Bez strefy czasowej zwracamy None — „00:00" bez offsetu nie wskazuje żadnej
    konkretnej chwili, a zgadywanie strefy encji to zgadywanie cudzych pieniędzy.
    """
    # Ułamek sekundy obcinamy od razu: `startAt` znakuje przedział, nie pomiar,
    # a mikrosekundy psułyby i długość przedziału, i parowanie ze sprzedażą.
    if isinstance(wartosc, datetime):
        return wartosc.replace(microsecond=0) if wartosc.tzinfo is not None else None
    if not isinstance(wartosc, str):
        return None
    tekst = wartosc.strip()
    if tekst.endswith(("Z", "z")):
        tekst = tekst[:-1] + "+00:00"
    try:
        chwila = datetime.fromisoformat(tekst)
    except ValueError:
        return None
    return chwila.replace(microsecond=0) if chwila.tzinfo is not None else None


def _minuty(dlugosc: timedelta) -> int | None:
    """Długość przedziału w minutach, o ile jest jedną z dozwolonych."""
    sekundy = dlugosc.total_seconds()
    if sekundy <= 0 or sekundy % 60:
        return None
    minuty = int(sekundy // 60)
    return minuty if minuty in DOZWOLONE_MINUTY else None


# ── Dwa warianty atrybutów ──────────────────────────────────────────────────

#: Wewnętrzna postać pośrednia: (chwila startu, cena, długość w minutach).
_Przedzial = tuple[datetime, float, int]


def _z_raw(wpisy: Sequence[Any]) -> list[_Przedzial] | None:
    """Jedna lista `raw_*` (dzisiaj ALBO jutro). None = dane niespójne."""
    wynik: list[_Przedzial] = []
    dlugosci: set[int] = set()
    for wpis in wpisy:
        if not isinstance(wpis, Mapping):
            return None
        start = _chwila(wpis.get("start"))
        koniec = _chwila(wpis.get("end"))
        cena = _liczba(wpis.get("value"))
        if start is None or koniec is None or cena is None:
            return None
        minuty = _minuty(koniec - start)
        if minuty is None:
            return None
        dlugosci.add(minuty)
        # Mieszanka rozdzielczości w jednej encji to dane niespójne, nie doba
        # o zmiennym kroku — nie zgadujemy, która część jest prawdziwa.
        if len(dlugosci) > 1:
            return None
        wynik.append((start, cena, minuty))
        if len(wynik) * minuty > MAX_MINUT_SERII:
            return None
    return wynik


def _ciagla(przedzialy: list[_Przedzial]) -> list[_Przedzial] | None:
    """Uporządkuj serię i sprawdź, że NIE MA w niej dziury ani zakładki.

    Ciągłość jest obietnicą tego modułu, nie sugestią: dziura w środku doby jest
    dla plannera nierozróżnialna od doby krótszej, a zakładka to podwójnie
    wyceniona godzina. Jedno i drugie kasuje całą serię.

    Kolejność wpisów w atrybutach encji ciągłością nie jest — porządkujemy sami,
    a przy powtórzonej chwili zostaje wpis napotkany jako PIERWSZY.
    """
    if not przedzialy:
        return przedzialy
    # Mieszanka rozdzielczości między listami (dzisiaj 60 min, jutro 15 min)
    # — w jednej liście łapie to `_z_raw`, tutaj domykamy szew dzisiaj/jutro.
    if len({minuty for _s, _c, minuty in przedzialy}) > 1:
        return None

    po_chwili: dict[datetime, _Przedzial] = {}
    for start, cena, minuty in przedzialy:
        po_chwili.setdefault(start.astimezone(timezone.utc), (start, cena, minuty))

    uporzadkowane = [po_chwili[chwila] for chwila in sorted(po_chwili)]
    for (poprzedni, _c1, minuty), (nastepny, _c2, _m2) in zip(uporzadkowane, uporzadkowane[1:]):
        if nastepny - poprzedni != timedelta(minutes=minuty):
            return None
    return uporzadkowane


def _z_list_liczb(
    attrs: Mapping[str, Any], tz: tzinfo, now: datetime | None
) -> list[_Przedzial] | None:
    """Wariant `today`/`tomorrow` + `interval_min`. None = dane niespójne."""
    surowy_krok = attrs.get("interval_min", DOMYSLNY_INTERVAL_MIN)
    krok = _liczba(surowy_krok)
    if krok is None or int(krok) not in DOZWOLONE_MINUTY:
        return None
    minuty = int(krok)

    # `now` przychodzi zwykle w UTC (tak podaje czas Home Assistant), a doba cen
    # jest dobą RYNKU — bez przeliczenia na `tz` o 23:30Z liczylibyśmy północ
    # poprzedniego dnia lokalnego i przesunęli całą dobę o 24 h.
    teraz = (now or datetime.now(tz)).astimezone(tz)
    # Północ dnia LOKALNEGO w strefie rynku; dalej chodzimy już chwilami
    # absolutnymi, więc doba zmiany czasu (23/25 godzin) wychodzi sama.
    # Uwaga [poza zakresem]: w strefach przestawiających czas DOKŁADNIE o północy
    # (np. America/Santiago) `fold=0` wskaże pierwsze z dwóch wystąpień, czyli
    # godzinę za wcześnie. Europe/Warsaw przestawia o 02:00/03:00, więc nie dotyczy.
    polnoc = datetime(teraz.year, teraz.month, teraz.day, tzinfo=tz)

    wynik: list[_Przedzial] = []
    for klucz, przesuniecie in (("today", 0), ("tomorrow", 1)):
        wartosci = attrs.get(klucz)
        if not isinstance(wartosci, (list, tuple)):
            continue
        if len(wartosci) * minuty > MAX_MINUT_SERII:
            return None
        poczatek_doby = (polnoc + timedelta(days=przesuniecie)).astimezone(timezone.utc)
        for i, surowa in enumerate(wartosci):
            cena = _liczba(surowa)
            if cena is None:
                return None
            wynik.append((poczatek_doby + timedelta(minutes=minuty * i), cena, minuty))
    # Ta sama obietnica ciągłości co przy `raw_*`: krótka lista `today` zostawia
    # dziurę do północy, a dziury planner nie odróżnia od krótszej doby.
    return _ciagla(wynik)


def _przeskaluj(
    przedzialy: list[_Przedzial] | None, dzielnik: float
) -> list[_Przedzial] | None:
    """Podziel ceny przez dzielnik jednostki (za MWh ÷1000, centy ÷100 — albo oba naraz)."""
    if przedzialy is None or dzielnik == 1.0:
        return przedzialy
    return [(start, cena / dzielnik, minuty) for start, cena, minuty in przedzialy]


def _przedzialy(
    attrs: Mapping[str, Any] | None, tz: tzinfo, now: datetime | None
) -> list[_Przedzial] | None:
    """Znormalizuj atrybuty jednej encji. None = dane niespójne.

    Jednostka jest walidowana tu tak samo jak w `currency_from_attributes`
    (przez ten sam `_rozbierz_jednostke`) — inny mianownik niż kWh/MWh (albo
    sprzeczny `price_in_cents`) odrzuca CAŁĄ serię, a za MWh/w centach
    wartości są przeliczane dokładnie.
    """
    if not isinstance(attrs, Mapping):
        return None
    rozbior = _rozbierz_jednostke(attrs)
    if rozbior is None:
        return None
    dzielnik, _centowy, _kod_z_jednostki = rozbior
    raw_today = attrs.get("raw_today")
    if isinstance(raw_today, (list, tuple)) and raw_today:
        seria = _z_raw(list(raw_today))
        if seria is None:
            return None
        raw_tomorrow = attrs.get("raw_tomorrow")
        # Brak jutra to normalny stan dnia przed publikacją, nie błąd.
        if isinstance(raw_tomorrow, (list, tuple)):
            jutro = _z_raw(list(raw_tomorrow))
            if jutro is None:
                return None
            seria += jutro
        return _przeskaluj(_ciagla(seria), dzielnik)
    return _przeskaluj(_z_list_liczb(attrs, tz, now), dzielnik)


# ── API publiczne ───────────────────────────────────────────────────────────


def currency_from_attributes(attrs: Mapping[str, Any] | None, fallback: str | None) -> str | None:
    """Waluta z atrybutów encji: `currency`, potem `unit_of_measurement`.

    Jednostka jest walidowana ZAWSZE, niezależnie od tego, czy atrybut
    `currency` jest obecny — jawny atrybut nie omija sprawdzenia jednostki,
    tylko jest z nią uzgadniany:
    - inny mianownik niż kWh/MWh (np. `Wh`), albo licznik spoza jawnej listy
      dozwolonych (patrz `_rozbierz_jednostke`), odrzuca encję CAŁKOWICIE;
    - większość tokenów centowych (`ct/kWh`, `Øre/kWh`, `¢/kWh`, ...) i oba
      symbole wieloznaczne (`kr`, `$`) same nie wskazują JEDNEJ konkretnej
      waluty — musi wtedy przyjść z `currency`. Dwa tokeny centowe SĄ
      jednoznaczne (`gr`/`grosz(e)` → PLN, `p`/`pence` → GBP) i wskazują
      walutę same, bez atrybutu;
    - jeśli licznik jednostki (kod ISO, symbol jednoznaczny albo token centowy
      jednoznaczny) I atrybut `currency` są oba rozpoznawalne, ale się przeczą,
      to dane niespójne → None, „atrybut wygrywa" nie istnieje;
    - atrybut `currency` jest sprawdzany względem REALNYCH kodów ISO 4217, nie
      tylko kształtu — `FOO` nie przechodzi, mimo trzyliterowego kształtu.

    Przeliczenie samej WARTOŚCI ceny (za MWh ÷1000, w centach ÷100) robi
    `intervals_from_attributes` — ta funkcja tylko ustala walutę.

    None oznacza „nie wiem" — wołający pomija wtedy blok `prices`.
    """
    if isinstance(attrs, Mapping):
        rozbior = _rozbierz_jednostke(attrs)
        if rozbior is None:
            return None
        _dzielnik, _centowy, kod_z_jednostki = rozbior
        jednostka = attrs.get("unit_of_measurement")
        ma_ukosnik = isinstance(jednostka, str) and "/" in jednostka

        jawna = attrs.get("currency")
        if isinstance(jawna, str) and jawna.strip():
            # Atrybut OBECNY i niezrozumiały nie schodzi do fallbacku: encja coś
            # deklaruje, a my nie mamy prawa podstawić za nią czegoś innego.
            kod_jawny = _kod_waluty_iso(jawna)
            if kod_jawny is None:
                return None
            if kod_z_jednostki is not None and kod_z_jednostki != kod_jawny:
                return None
            return kod_jawny
        if kod_z_jednostki is not None:
            return kod_z_jednostki
        if ma_ukosnik:
            # Jednostka jest OBECNA i rozpoznana (przeszła `_rozbierz_jednostke`),
            # ale sama waluty nie wskazuje — token centowy wieloznaczny (`ct`,
            # `¢`, `øre`, ...) albo symbol wieloznaczny (`kr`, `$`); licznik
            # NIEROZPOZNANY w ogóle już wcześniej dał None w `_rozbierz_jednostke`.
            # Bez jawnego atrybutu `currency` nie zgadujemy.
            return None
    # Fallback z opcji integracji wchodzi TYLKO wtedy, gdy encja nie mówi nic.
    return _kod_waluty_iso(fallback)


def _sprzedaz_dopuszczalna(attrs: Mapping[str, Any], waluta: str) -> bool:
    """Czy atrybuty SPRZEDAŻY wolno użyć obok bloku o walucie `waluta`.

    Dopuszczalne w DWÓCH przypadkach:
    (a) sprzedaż nie deklaruje NICZEGO o walucie — nie ma (niepustego)
        atrybutu `currency`, jednostka nie daje kodu waluty, a licznik nie
        jest nawet symbolem wieloznacznym (`kr`, `$`, który TO byłby
        deklaracją, tylko nierozstrzygniętą) — wtedy dziedziczy walutę bloku
        bez zastrzeżeń (np. `ct/kWh` bez atrybutu);
    (b) sprzedaż ma własną, w pełni rozstrzygniętą walutę i ta waluta jest
        RÓWNA `waluta`.

    Każda inna kombinacja — atrybut sprzeczny z walutą bloku, atrybut
    sprzeczny z WŁASNĄ jednostką sprzedaży, symbol wieloznaczny rozstrzygnięty
    na inną walutę, atrybut o kształcie kodu ale nierealny — degraduje CAŁĄ
    sprzedaż do None. `buy` tym się nie przejmuje.
    """
    jawna = attrs.get("currency")
    ma_atrybut = isinstance(jawna, str) and jawna.strip() != ""

    rozbior = _rozbierz_jednostke(attrs)
    kod_z_jednostki = rozbior[2] if rozbior is not None else None

    jednostka = attrs.get("unit_of_measurement")
    licznik_wieloznaczny = False
    if isinstance(jednostka, str) and "/" in jednostka:
        licznik, _, _ = jednostka.partition("/")
        licznik_wieloznaczny = _znormalizuj(licznik).rstrip(".") in _SYMBOLE_NIEJEDNOZNACZNE

    if rozbior is not None and not ma_atrybut and kod_z_jednostki is None and not licznik_wieloznaczny:
        return True  # nic nie deklaruje — dziedziczy walutę bloku

    return currency_from_attributes(attrs, None) == waluta


def intervals_from_attributes(
    attrs_buy: Mapping[str, Any] | None,
    attrs_sell: Mapping[str, Any] | None = None,
    currency: str | None = None,
    tz: tzinfo = timezone.utc,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Zamień atrybuty encji cenowych na listę przedziałów kontraktu.

    `attrs_sell` jest opcjonalne (rynki bez wykupu nadwyżek) — bez niego każdy
    przedział ma `sell=None`. Zepsute atrybuty SPRZEDAŻY (w tym waluta INNA niż
    kupna) degradują samo `sell` do None, bo brak ceny sprzedaży planner
    obsługuje; zepsute atrybuty ZAKUPU kasują cały wynik.

    Zwraca listę posortowaną chronologicznie, bez duplikatów `startAt` (zostaje
    pierwszy napotkany wpis), albo pustą listę, gdy danych nie da się zaufać.
    """
    waluta = _kod_waluty(currency)
    if waluta is None:
        # Przedział bez waluty jest dla chmury bezużyteczny — wołający pomija blok.
        return []

    zakup = _przedzialy(attrs_buy, tz, now)
    if not zakup:
        return []

    sprzedaz: dict[datetime, float] = {}
    # Waluta sprzedaży musi zgadzać się z walutą bloku (kupna) — encja sprzedaży
    # bez ŻADNEJ deklaracji waluty jest przyjmowana bez zastrzeżeń, ale każda
    # deklaracja (atrybut, symbol jednoznaczny albo wieloznaczny rozstrzygnięty),
    # która nie zgadza się z `waluta`, ALBO jest sama w sobie niespójna, degraduje
    # całą sprzedaż do None — nigdy tylko „brak informacji" (patrz `_sprzedaz_dopuszczalna`).
    if attrs_sell is not None and _sprzedaz_dopuszczalna(attrs_sell, waluta):
        for start, cena, _minuty in _przedzialy(attrs_sell, tz, now) or []:
            # Klucz JAWNIE w UTC — kupno i sprzedaż bywają zapisane innym
            # offsetem tej samej chwili (+02:00 kontra Z).
            sprzedaz.setdefault(start.astimezone(timezone.utc), cena)

    wynik: dict[datetime, dict[str, Any]] = {}
    for start, cena, minuty in zakup:
        chwila = start.astimezone(timezone.utc).replace(microsecond=0)
        if chwila in wynik:
            continue  # duplikat — zostaje pierwszy napotkany
        wynik[chwila] = {
            "startAt": chwila.isoformat().replace("+00:00", "Z"),
            "minutes": minuty,
            "buy": cena,
            "sell": sprzedaz.get(chwila),
            "currency": waluta,
        }
    return [wynik[chwila] for chwila in sorted(wynik)]


def has_usable_prices_now(
    attrs_buy: Mapping[str, Any] | None,
    fallback_currency: str | None,
    tz: tzinfo = timezone.utc,
    now: datetime | None = None,
) -> bool:
    """Czy encja (samo `buy`) daje pełną, zaufaną serię cen JUŻ TERAZ.

    Do onboardingu (wybór encji ceny): oferujemy encję jako źródło cen tylko
    wtedy, gdy jej bieżące atrybuty naprawdę dają się zamienić na przedziały —
    nie „może kiedyś", jeśli użytkownik uzupełni `interval_min` albo zmieni
    jednostkę. Krok „prices" onboardingu nie wolno oznaczać jako zrobiony na
    podstawie samego wyboru encji, tylko na podstawie tego wyniku.

    „Pełna seria JUŻ TERAZ" oznacza konkretnie: seria pokrywa bieżącą godzinę
    (`now`) i sięga co najmniej 6 h w przód — samo „jakieś dwie godziny
    `today`" albo seria sprzed miesiąca (encja martwa) to nie jest gotowość.
    """
    waluta = currency_from_attributes(attrs_buy, fallback_currency)
    if waluta is None:
        return False
    przedzialy = intervals_from_attributes(attrs_buy, None, waluta, tz, now)
    if not przedzialy:
        return False
    chwila = (now or datetime.now(tz)).astimezone(timezone.utc)
    pierwszy = datetime.fromisoformat(przedzialy[0]["startAt"].replace("Z", "+00:00"))
    ostatni = datetime.fromisoformat(przedzialy[-1]["startAt"].replace("Z", "+00:00"))
    koniec_serii = ostatni + timedelta(minutes=przedzialy[-1]["minutes"])
    return pierwszy <= chwila < koniec_serii and koniec_serii >= chwila + timedelta(hours=6)


def fingerprint(intervals: Sequence[Mapping[str, Any]]) -> str:
    """Odcisk treści przedziałów — do wysyłki „tylko gdy się zmieniło".

    Kanoniczny JSON (posortowane klucze, bez spacji) + sha1. Nie jest to funkcja
    kryptograficzna, tylko porównywalny skrót.
    """
    kanoniczny = json.dumps(list(intervals), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(kanoniczny.encode("utf-8")).hexdigest()
