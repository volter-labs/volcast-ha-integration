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

#: Przedrostki jednostek oznaczające setne części waluty (centy, grosze).
#: Nie przeliczamy ich na jednostkę główną, więc taka encja nie daje waluty
#: WCALE — podstawienie fallbacku wysłałoby ceny 100x za duże.
_JEDNOSTKI_CENTOWE = ("CT", "C", "GR")

#: Mianownik jednostki, który kontrakt uznaje za cenę energii. `PLN/MWh` to ta
#: sama klasa pomyłki co centy — rząd wielkości, tyle że w drugą stronę.
_MIANOWNIK_CENY = "KWH"

#: Kod waluty kontraktu: dokładnie trzy litery ASCII (ISO 4217). „zł", „euro"
#: czy „PL" walutą w rozumieniu chmury nie są.
_KOD_WALUTY = re.compile(r"^[A-Z]{3}$")


def _kod_waluty(tekst: Any) -> str | None:
    """Znormalizowany kod ISO 4217 albo None."""
    if not isinstance(tekst, str):
        return None
    kod = tekst.strip().upper()
    return kod if kod.isascii() and _KOD_WALUTY.match(kod) else None


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


def _przedzialy(
    attrs: Mapping[str, Any] | None, tz: tzinfo, now: datetime | None
) -> list[_Przedzial] | None:
    """Znormalizuj atrybuty jednej encji. None = dane niespójne."""
    if not isinstance(attrs, Mapping):
        return None
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
        return _ciagla(seria)
    return _z_list_liczb(attrs, tz, now)


# ── API publiczne ───────────────────────────────────────────────────────────


def currency_from_attributes(attrs: Mapping[str, Any] | None, fallback: str | None) -> str | None:
    """Waluta z atrybutów encji: `currency`, potem `unit_of_measurement`.

    Jednostki centowe (`ct/kWh`, `c/kWh`, `gr/kWh`) świadomie schodzą do
    `fallback` — nie przeliczamy setnych na jednostkę główną, bo z samej
    jednostki nie wynika, o którą walutę chodzi.

    None oznacza „nie wiem" — wołający pomija wtedy blok `prices`.
    """
    if isinstance(attrs, Mapping):
        jawna = attrs.get("currency")
        if isinstance(jawna, str) and jawna.strip():
            # Atrybut OBECNY i niezrozumiały nie schodzi do fallbacku: encja coś
            # deklaruje, a my nie mamy prawa podstawić za nią czegoś innego.
            return _kod_waluty(jawna)
        jednostka = attrs.get("unit_of_measurement")
        if isinstance(jednostka, str) and "/" in jednostka:
            licznik, _, mianownik = jednostka.partition("/")
            if mianownik.strip().upper() != _MIANOWNIK_CENY:
                return None
            if licznik.strip().upper() in _JEDNOSTKI_CENTOWE:
                return None
            return _kod_waluty(licznik)
    # Fallback z opcji integracji wchodzi TYLKO wtedy, gdy encja nie mówi nic.
    return _kod_waluty(fallback)


def intervals_from_attributes(
    attrs_buy: Mapping[str, Any] | None,
    attrs_sell: Mapping[str, Any] | None = None,
    currency: str | None = None,
    tz: tzinfo = timezone.utc,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Zamień atrybuty encji cenowych na listę przedziałów kontraktu.

    `attrs_sell` jest opcjonalne (rynki bez wykupu nadwyżek) — bez niego każdy
    przedział ma `sell=None`. Zepsute atrybuty SPRZEDAŻY degradują samo `sell`
    do None, bo brak ceny sprzedaży planner obsługuje; zepsute atrybuty ZAKUPU
    kasują cały wynik.

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
    if attrs_sell is not None:
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


def fingerprint(intervals: Sequence[Mapping[str, Any]]) -> str:
    """Odcisk treści przedziałów — do wysyłki „tylko gdy się zmieniło".

    Kanoniczny JSON (posortowane klucze, bez spacji) + sha1. Nie jest to funkcja
    kryptograficzna, tylko porównywalny skrót.
    """
    kanoniczny = json.dumps(list(intervals), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(kanoniczny.encode("utf-8")).hexdigest()
