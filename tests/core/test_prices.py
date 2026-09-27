"""Normalizacja atrybutów cenowych encji HA do przedziałów `PriceInterval`.

Moduł jest w `core/` (bez importów Home Assistanta), więc testy idą zwykłym
`pytest` bez żadnej instalacji Home Assistanta — tak samo jak `guards`
i `schedule`.

Kontrakt: `Volcast/docs/ha/price-feed-contract.md` §3–5.

Reguła nadrzędna tych testów: przy danych niespójnych moduł zwraca PUSTĄ listę,
nigdy listy częściowej. Dziura w dobie cen jest dla plannera gorsza niż brak cen
— brak umie obsłużyć (degradacja do wczoraj/taryfy), dziury nie widzi wcale.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.volcast.core.prices import (
    currency_from_attributes,
    fingerprint,
    has_usable_prices_now,
    intervals_from_attributes,
)

_WAW = ZoneInfo("Europe/Warsaw")
_UTC = timezone.utc


def _raw(start_local: datetime, ile: int, minuty: int, wartosc=0.5, *, jako_tekst=True):
    """Zbuduj listę wpisów `raw_*` (jak Nord Pool): start/end/value."""
    krok = timedelta(minutes=minuty)
    wpisy = []
    for i in range(ile):
        poczatek = start_local + i * krok
        koniec = poczatek + krok
        wartosc_i = wartosc(i) if callable(wartosc) else wartosc
        wpisy.append(
            {
                "start": poczatek.isoformat() if jako_tekst else poczatek,
                "end": koniec.isoformat() if jako_tekst else koniec,
                "value": wartosc_i,
            }
        )
    return wpisy


def _polnoc(rok=2026, miesiac=9, dzien=15) -> datetime:
    return datetime(rok, miesiac, dzien, 0, 0, tzinfo=_WAW)


# ── raw_today / raw_tomorrow ────────────────────────────────────────────────


def test_raw_today_24_godziny_daje_24_przedzialy_utc():
    attrs = {"raw_today": _raw(_polnoc(), 24, 60, wartosc=lambda i: 0.4 + i / 100)}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW)

    assert len(out) == 24
    # Północ w Warszawie we wrześniu to +02:00 → 22:00 UTC dnia poprzedniego.
    assert out[0]["startAt"] == "2026-09-14T22:00:00Z"
    assert out[0]["minutes"] == 60
    assert out[0]["buy"] == pytest.approx(0.4)
    assert out[0]["sell"] is None
    assert out[0]["currency"] == "PLN"
    assert out[-1]["startAt"] == "2026-09-15T21:00:00Z"
    assert out[-1]["buy"] == pytest.approx(0.63)


def test_raw_akceptuje_obiekty_datetime_w_atrybutach():
    """Część integracji wkłada do atrybutów `datetime`, nie tekst ISO."""
    attrs = {"raw_today": _raw(_polnoc(), 3, 60, jako_tekst=False)}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW)

    assert [w["startAt"] for w in out] == [
        "2026-09-14T22:00:00Z",
        "2026-09-14T23:00:00Z",
        "2026-09-15T00:00:00Z",
    ]


def test_raw_kwadranse_daje_minutes_15():
    attrs = {"raw_today": _raw(_polnoc(), 96, 15)}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW)

    assert len(out) == 96
    assert {w["minutes"] for w in out} == {15}


def test_raw_tomorrow_doklada_sie_do_dzisiaj():
    attrs = {
        "raw_today": _raw(_polnoc(), 24, 60),
        "raw_tomorrow": _raw(_polnoc(dzien=16), 24, 60),
    }

    out = intervals_from_attributes(attrs, None, "PLN", _WAW)

    assert len(out) == 48
    assert out[-1]["startAt"] == "2026-09-16T21:00:00Z"


def test_brak_raw_tomorrow_to_nie_blad():
    """Jutro bywa jeszcze nieopublikowane — zwracamy samo dzisiaj (§7 kontraktu)."""
    attrs = {"raw_today": _raw(_polnoc(), 24, 60), "raw_tomorrow": None}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW)

    assert len(out) == 24


def test_mieszane_dlugosci_przedzialow_daja_pusto():
    attrs = {
        "raw_today": _raw(_polnoc(), 2, 60) + _raw(_polnoc() + timedelta(hours=2), 4, 30)
    }

    assert intervals_from_attributes(attrs, None, "PLN", _WAW) == []


def test_dlugosc_spoza_15_30_60_daje_pusto():
    attrs = {"raw_today": _raw(_polnoc(), 12, 120)}

    assert intervals_from_attributes(attrs, None, "PLN", _WAW) == []


def test_wartosc_nieliczbowa_daje_pusto_a_nie_niepelna_dobe():
    wpisy = _raw(_polnoc(), 24, 60)
    wpisy[7]["value"] = "brak"

    assert intervals_from_attributes({"raw_today": wpisy}, None, "PLN", _WAW) == []


def test_wartosc_nieskonczona_daje_pusto():
    wpisy = _raw(_polnoc(), 24, 60)
    wpisy[3]["value"] = float("nan")

    assert intervals_from_attributes({"raw_today": wpisy}, None, "PLN", _WAW) == []


def test_wartosc_jako_tekst_liczbowy_jest_przyjmowana():
    attrs = {"raw_today": _raw(_polnoc(), 2, 60, wartosc="0.42")}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW)

    assert [w["buy"] for w in out] == [pytest.approx(0.42), pytest.approx(0.42)]


def test_brak_klucza_start_daje_pusto():
    wpisy = _raw(_polnoc(), 5, 60)
    del wpisy[2]["start"]

    assert intervals_from_attributes({"raw_today": wpisy}, None, "PLN", _WAW) == []


def test_start_bez_strefy_daje_pusto():
    """Bez offsetu nie wiemy, o której chwili mowa — nie zgadujemy."""
    wpisy = _raw(_polnoc(), 3, 60)
    wpisy[0]["start"] = "2026-09-15T00:00:00"

    assert intervals_from_attributes({"raw_today": wpisy}, None, "PLN", _WAW) == []


def test_duplikaty_usuwane_a_wynik_posortowany():
    dzien = _raw(_polnoc(), 3, 60, wartosc=lambda i: float(i))
    # Kolejność odwrotna + powtórzony pierwszy wpis z inną ceną.
    attrs = {"raw_today": list(reversed(dzien)) + [dict(dzien[0], value=9.9)]}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW)

    assert [w["startAt"] for w in out] == [
        "2026-09-14T22:00:00Z",
        "2026-09-14T23:00:00Z",
        "2026-09-15T00:00:00Z",
    ]
    # Zostaje PIERWSZY napotkany wpis dla danej chwili, nie ostatni (9.9 odpada).
    assert out[0]["buy"] == pytest.approx(0.0)
    assert 9.9 not in [w["buy"] for w in out]


def test_dziura_w_serii_raw_daje_pusto():
    """Ciągłość serii to obietnica, nie sugestia — brak godzin w środku doby
    planner odczytałby jako dobę krótszą, nie jako brak danych."""
    dzien = _raw(_polnoc(), 2, 60) + _raw(_polnoc() + timedelta(hours=9), 2, 60)

    assert intervals_from_attributes({"raw_today": dzien}, None, "PLN", _WAW) == []


def test_nachodzace_przedzialy_raw_daja_pusto():
    dzien = _raw(_polnoc(), 3, 60)
    dzien[2]["start"] = (_polnoc() + timedelta(minutes=90)).isoformat()
    dzien[2]["end"] = (_polnoc() + timedelta(minutes=150)).isoformat()

    assert intervals_from_attributes({"raw_today": dzien}, None, "PLN", _WAW) == []


def test_szew_dzis_jutro_jest_ciagly():
    attrs = {
        "raw_today": _raw(_polnoc(), 24, 60),
        "raw_tomorrow": _raw(_polnoc(dzien=16), 24, 60),
    }

    assert len(intervals_from_attributes(attrs, None, "PLN", _WAW)) == 48


def test_koniec_przed_poczatkiem_daje_pusto():
    wpisy = _raw(_polnoc(), 3, 60)
    wpisy[1]["end"], wpisy[1]["start"] = wpisy[1]["start"], wpisy[1]["end"]

    assert intervals_from_attributes({"raw_today": wpisy}, None, "PLN", _WAW) == []


def test_raw_seria_dluzsza_niz_dwie_doby_daje_pusto():
    assert intervals_from_attributes({"raw_today": _raw(_polnoc(), 49, 60)}, None, "PLN", _WAW) == []
    assert len(intervals_from_attributes({"raw_today": _raw(_polnoc(), 48, 60)}, None, "PLN", _WAW)) == 48


# ── fallback today / tomorrow ───────────────────────────────────────────────


def test_fallback_today_z_interval_min_30():
    teraz = datetime(2026, 9, 15, 13, 37, tzinfo=_WAW)
    attrs = {"today": [0.1 + i / 1000 for i in range(48)], "interval_min": 30}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz)

    assert len(out) == 48
    assert out[0]["startAt"] == "2026-09-14T22:00:00Z"
    assert out[0]["minutes"] == 30
    assert out[1]["startAt"] == "2026-09-14T22:30:00Z"
    assert out[-1]["startAt"] == "2026-09-15T21:30:00Z"


def test_fallback_domyslny_interval_min_to_60():
    teraz = datetime(2026, 9, 15, 5, 0, tzinfo=_WAW)
    attrs = {"today": [0.5] * 24}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz)

    assert len(out) == 24
    assert {w["minutes"] for w in out} == {60}


def test_fallback_tomorrow_startuje_od_polnocy_nastepnego_dnia():
    teraz = datetime(2026, 9, 15, 20, 0, tzinfo=_WAW)
    attrs = {"today": [0.5] * 24, "tomorrow": [0.6] * 24}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz)

    assert len(out) == 48
    assert out[24]["startAt"] == "2026-09-15T22:00:00Z"
    assert out[24]["buy"] == pytest.approx(0.6)


def test_fallback_doba_dst_25_wartosci_chodzi_po_chwilach():
    """Doba zmiany czasu ma 25 godzin — idziemy chwilami, nie ścianą zegara."""
    teraz = datetime(2026, 10, 25, 12, 0, tzinfo=_WAW)
    attrs = {"today": [0.5] * 25}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz)

    assert len(out) == 25
    assert out[0]["startAt"] == "2026-10-24T22:00:00Z"
    assert out[-1]["startAt"] == "2026-10-25T22:00:00Z"


def test_fallback_wartosc_nieliczbowa_daje_pusto():
    teraz = datetime(2026, 9, 15, 5, 0, tzinfo=_WAW)
    attrs = {"today": [0.5] * 23 + [None]}

    assert intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz) == []


def test_fallback_zly_interval_min_daje_pusto():
    teraz = datetime(2026, 9, 15, 5, 0, tzinfo=_WAW)
    attrs = {"today": [0.5] * 12, "interval_min": 120}

    assert intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz) == []


def test_raw_today_ma_pierwszenstwo_nad_today():
    teraz = datetime(2026, 9, 15, 5, 0, tzinfo=_WAW)
    attrs = {
        "raw_today": _raw(_polnoc(), 3, 60, wartosc=1.0),
        "today": [9.0] * 24,
    }

    out = intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz)

    assert len(out) == 3
    assert out[0]["buy"] == pytest.approx(1.0)


def test_brak_jakichkolwiek_atrybutow_cenowych_daje_pusto():
    assert intervals_from_attributes({}, None, "PLN", _WAW) == []


def test_pusty_raw_today_daje_pusto():
    assert intervals_from_attributes({"raw_today": []}, None, "PLN", _WAW) == []


def test_fallback_polnoc_liczona_w_strefie_rynku_a_nie_w_strefie_now():
    """`now` bywa w UTC (tak podaje czas HA) — dobę wyznacza `tz`, nie `now`.

    23:30Z to już 01:30 następnego dnia w Warszawie: dobą cen jest 16., nie 15.
    """
    teraz = datetime(2026, 9, 15, 23, 30, tzinfo=_UTC)
    attrs = {"today": [0.5] * 24}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz)

    assert out[0]["startAt"] == "2026-09-15T22:00:00Z"


def test_fallback_doba_dst_23_wartosci_chodzi_po_chwilach():
    """Doba wiosennej zmiany czasu ma 23 godziny — też chwilami, nie zegarem."""
    teraz = datetime(2026, 3, 29, 12, 0, tzinfo=_WAW)

    out = intervals_from_attributes({"today": [0.5] * 23}, None, "PLN", _WAW, now=teraz)

    assert len(out) == 23
    assert out[0]["startAt"] == "2026-03-28T23:00:00Z"
    assert out[-1]["startAt"] == "2026-03-29T21:00:00Z"


def test_fallback_dziala_dla_strefy_z_polowkowym_offsetem():
    tz = timezone(timedelta(hours=5, minutes=30))
    teraz = datetime(2026, 9, 15, 9, 0, tzinfo=tz)

    out = intervals_from_attributes({"today": [0.5] * 24}, None, "PLN", tz, now=teraz)

    assert out[0]["startAt"] == "2026-09-14T18:30:00Z"


def test_fallback_seria_dluzsza_niz_dwie_doby_daje_pusto():
    """Ogranicznik rozmiaru: encja z absurdalnie długą serią to nie nasze ceny."""
    teraz = datetime(2026, 9, 15, 5, 0, tzinfo=_WAW)

    assert intervals_from_attributes({"today": [0.5] * 49}, None, "PLN", _WAW, now=teraz) == []
    # Dwie doby (z zapasem na 25-godzinną dobę zmiany czasu) przechodzą.
    assert len(intervals_from_attributes({"today": [0.5] * 48}, None, "PLN", _WAW, now=teraz)) == 48


def test_fallback_dziura_na_szwie_dzis_jutro_daje_pusto():
    """Krótka lista `today` zostawia dziurę do północy — seria nie jest ciągła."""
    teraz = datetime(2026, 9, 15, 5, 0, tzinfo=_WAW)
    attrs = {"today": [0.5] * 20, "tomorrow": [0.6] * 24}

    assert intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz) == []


def test_fallback_pelny_dzis_i_jutro_jest_ciagly():
    teraz = datetime(2026, 9, 15, 5, 0, tzinfo=_WAW)
    attrs = {"today": [0.5] * 24, "tomorrow": [0.6] * 24}

    out = intervals_from_attributes(attrs, None, "PLN", _WAW, now=teraz)

    assert len(out) == 48


# ── sprzedaż ────────────────────────────────────────────────────────────────


def test_sell_parowany_po_chwili_startu():
    buy = {"raw_today": _raw(_polnoc(), 3, 60, wartosc=1.0)}
    sell = {"raw_today": _raw(_polnoc(), 3, 60, wartosc=lambda i: 0.1 * (i + 1))}

    out = intervals_from_attributes(buy, sell, "PLN", _WAW)

    assert [w["sell"] for w in out] == [
        pytest.approx(0.1),
        pytest.approx(0.2),
        pytest.approx(0.3),
    ]


def test_sell_krotszy_niz_buy_daje_none_w_brakujacych():
    buy = {"raw_today": _raw(_polnoc(), 4, 60, wartosc=1.0)}
    sell = {"raw_today": _raw(_polnoc(), 2, 60, wartosc=0.2)}

    out = intervals_from_attributes(buy, sell, "PLN", _WAW)

    assert [w["sell"] for w in out] == [pytest.approx(0.2), pytest.approx(0.2), None, None]


def test_brak_encji_sprzedazy_daje_same_none():
    buy = {"raw_today": _raw(_polnoc(), 3, 60, wartosc=1.0)}

    out = intervals_from_attributes(buy, None, "PLN", _WAW)

    assert [w["sell"] for w in out] == [None, None, None]


def test_zepsute_atrybuty_sprzedazy_nie_kasuja_zakupu():
    """Złe dane sprzedaży degradują `sell` do None — ale nie wywalają całego bloku."""
    buy = {"raw_today": _raw(_polnoc(), 3, 60, wartosc=1.0)}
    zle = _raw(_polnoc(), 3, 60, wartosc=0.2)
    zle[1]["value"] = "nic"

    out = intervals_from_attributes(buy, {"raw_today": zle}, "PLN", _WAW)

    assert len(out) == 3
    assert [w["sell"] for w in out] == [None, None, None]


def test_sell_o_innej_rozdzielczosci_paruje_tylko_wspolne_chwile():
    buy = {"raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0)}
    sell = {"raw_today": _raw(_polnoc(), 8, 15, wartosc=0.3)}

    out = intervals_from_attributes(buy, sell, "PLN", _WAW)

    assert [w["minutes"] for w in out] == [60, 60]
    assert [w["sell"] for w in out] == [pytest.approx(0.3), pytest.approx(0.3)]


def test_sell_parowany_po_chwili_a_nie_po_zapisie_offsetu():
    """Kupno zapisane jako +02:00, sprzedaż jako Z — ta sama chwila, ma się sparować."""
    buy = {"raw_today": _raw(_polnoc(), 3, 60, wartosc=1.0)}
    sell_wpisy = _raw(_polnoc(), 3, 60, wartosc=0.2)
    for w in sell_wpisy:
        for klucz in ("start", "end"):
            w[klucz] = (
                datetime.fromisoformat(w[klucz]).astimezone(_UTC).isoformat().replace("+00:00", "Z")
            )

    out = intervals_from_attributes(buy, {"raw_today": sell_wpisy}, "PLN", _WAW)

    assert [w["sell"] for w in out] == [pytest.approx(0.2)] * 3


# ── waluta ──────────────────────────────────────────────────────────────────


def test_currency_z_unit_of_measurement():
    assert currency_from_attributes({"unit_of_measurement": "PLN/kWh"}, None) == "PLN"


def test_currency_z_atrybutu_currency_wielkimi_literami():
    assert currency_from_attributes({"currency": "eur"}, None) == "EUR"


def test_currency_atrybut_sprzeczny_z_jednostka_daje_none():
    """Atrybut `currency` NIE bije jednostki — jeśli się przeczą, to dane
    niespójne, a nie „atrybut wygrywa"."""
    attrs = {"currency": "eur", "unit_of_measurement": "PLN/kWh"}
    assert currency_from_attributes(attrs, "PLN") is None


@pytest.mark.parametrize(
    "jednostka",
    [
        "ct/kWh", "c/kWh", "CT/KWH",
        "Cent/kWh", "cents/kWh", "¢/kWh",
        "Øre/kWh", "Öre/kWh", "ore/kWh",
    ],
)
def test_currency_centy_daja_none_niezaleznie_od_fallbacku(jednostka):
    """Jednostka centowa to NIE jest brak informacji — to informacja, że wartości
    są w setnych. Podstawienie fallbacku wysłałoby ceny 100x za duże. Zbiór
    liczników centowych jest niewrażliwy na wielkość liter i diakrytyki
    skandynawskie (Øre, Öre). Te tokeny nie NARZUCAJĄ żadnej konkretnej waluty
    (nazwa wspólna wielu krajom) — w przeciwieństwie do `p`/`gr` niżej."""
    assert currency_from_attributes({"unit_of_measurement": jednostka}, "EUR") is None
    assert currency_from_attributes({"unit_of_measurement": jednostka}, None) is None


@pytest.mark.parametrize(
    ("jednostka", "kod"),
    [("p/kWh", "GBP"), ("pence/kWh", "GBP"), ("gr/kWh", "PLN"),
     ("gr./kWh", "PLN"), ("Grosz/kWh", "PLN"), ("grosze/kWh", "PLN")],
)
def test_currency_centy_z_jednoznaczna_waluta_jest_narzucona(jednostka, kod):
    """`p`/`pence` (grosz brytyjski) i `gr`/`grosz(e)` (grosz polski) są jedynymi
    tokenami centowymi w tym zestawie, których nazwa wskazuje JEDNĄ, konkretną
    walutę — bez atrybutu `currency` ta waluta jest przyjmowana wprost."""
    assert currency_from_attributes({"unit_of_measurement": jednostka}, None) == kod
    assert currency_from_attributes({"unit_of_measurement": jednostka}, "EUR") == kod


@pytest.mark.parametrize("jednostka", ["p/kWh", "gr/kWh"])
def test_currency_centy_narzucona_sprzeczna_z_atrybutem_daje_none(jednostka):
    """Atrybut `currency` sprzeczny z walutą narzuconą przez token centowy (`p`
    → GBP, `gr` → PLN) to dane niespójne, nie „atrybut wygrywa"."""
    assert currency_from_attributes({"currency": "EUR", "unit_of_measurement": jednostka}, None) is None


def test_currency_price_in_cents_bez_ukosnika_nie_blokuje_fallbacku():
    """Bez ukośnika `price_in_cents` niesie tylko informację o SKALI wartości —
    waluta nadal przychodzi z fallbacku, jak dla dowolnej innej jednostki bez
    ukośnika (`kWh` sama w sobie nie mówi nic o walucie)."""
    attrs = {"unit_of_measurement": "kWh", "price_in_cents": True}
    assert currency_from_attributes(attrs, "EUR") == "EUR"


def test_price_in_cents_bez_ukosnika_przelicza_wartosci_przez_100():
    """`price_in_cents=True` bez ukośnika (jednostka `kWh` sama, albo brak
    atrybutu wcale) nadal skaluje wartości ÷100 — to jedyne źródło skali,
    kiedy jednostka nie ma licznika/mianownika do rozebrania."""
    attrs = {
        "unit_of_measurement": "kWh",
        "price_in_cents": True,
        "raw_today": _raw(_polnoc(), 2, 60, wartosc=123.45),
    }

    out = intervals_from_attributes(attrs, None, "EUR", _WAW)

    assert [w["buy"] for w in out] == [pytest.approx(1.2345), pytest.approx(1.2345)]


def test_currency_price_in_cents_true_z_kodem_waluty_w_liczniku_jest_sprzeczne():
    """`EUR/kWh` + `price_in_cents=True` się wykluczają — sensor nie może być
    jednocześnie "w euro" i "w centach euro" na tym samym liczniku."""
    attrs = {"currency": "EUR", "unit_of_measurement": "EUR/kWh", "price_in_cents": True}
    assert currency_from_attributes(attrs, None) is None


def test_currency_price_in_cents_false_z_licznikiem_centowym_jest_sprzeczne():
    attrs = {"currency": "PLN", "unit_of_measurement": "gr/kWh", "price_in_cents": False}
    assert currency_from_attributes(attrs, None) is None


@pytest.mark.parametrize(
    ("waluta", "jednostka"),
    [("NOK", "Øre/kWh"), ("SEK", "Öre/kWh")],
)
def test_currency_price_in_cents_true_z_jawna_waluta_dziala(waluta, jednostka):
    """Wzorzec custom Nord Pool: `currency` + jednostka centowa (skandynawska) +
    `price_in_cents=True` — waluta z atrybutu, wartość przeliczona w centach."""
    attrs = {"currency": waluta, "unit_of_measurement": jednostka, "price_in_cents": True}
    assert currency_from_attributes(attrs, None) == waluta


@pytest.mark.parametrize(
    ("jednostka", "waluta"),
    [("€/kWh", None), ("zł/kWh", None), ("£/kWh", None),
     ("EUR/kWh", None), ("kr/kWh", "SEK"), ("$/kWh", "AUD")],
)
def test_currency_price_in_cents_true_z_cala_waluta_jest_sprzeczne(jednostka, waluta):
    """`price_in_cents=True` obok licznika, który już oznacza CAŁĄ walutę (kod
    ISO, symbol jednoznaczny albo symbol wieloznaczny rozstrzygnięty przez
    atrybut) jest sprzeczne — sensor nie może być jednocześnie "w euro" i
    "w centach euro". W przeciwieństwie do `p`/`gr`, które SĄ centami, więc
    `price_in_cents=True` im nie przeczy."""
    attrs = {"unit_of_measurement": jednostka, "price_in_cents": True}
    if waluta is not None:
        attrs["currency"] = waluta
    assert currency_from_attributes(attrs, None) is None


@pytest.mark.parametrize(
    ("jednostka", "waluta"),
    [("€/kWh", None), ("zł/kWh", None), ("£/kWh", None), ("kr/kWh", "SEK")],
)
def test_price_in_cents_true_z_cala_waluta_odrzuca_cala_serie(jednostka, waluta):
    attrs = {
        "unit_of_measurement": jednostka,
        "price_in_cents": True,
        "raw_today": _raw(_polnoc(), 2, 60, wartosc=123.45),
    }
    if waluta is not None:
        attrs["currency"] = waluta

    assert intervals_from_attributes(attrs, None, waluta or "EUR", _WAW) == []


def test_currency_mianownik_wh_daje_none():
    """`Wh` nie jest dozwolonym przelicznikiem (ani kWh, ani MWh) — odrzucamy."""
    assert currency_from_attributes({"unit_of_measurement": "PLN/Wh"}, "PLN") is None


@pytest.mark.parametrize(("jednostka", "kod"), [("PLN/MWh", "PLN"), ("EUR/MWh", "EUR")])
def test_currency_mianownik_mwh_jest_dozwolony(jednostka, kod):
    """Za MWh JEST dozwolony przelicznik (dokładne ÷1000) — waluta z licznika."""
    assert currency_from_attributes({"unit_of_measurement": jednostka}, None) == kod


def test_currency_mianownik_kwh_bez_wzgledu_na_wielkosc_liter():
    assert currency_from_attributes({"unit_of_measurement": "PLN/KWH"}, None) == "PLN"


def test_currency_fallback_tylko_gdy_encja_nic_nie_mowi():
    """Jednostka centowa jest rozpoznana jako cena energii (przeliczalna), ale
    sama nie niesie kodu waluty — bez atrybutu `currency` fallback i tak nie
    wchodzi, bo licznik jednostki to nie ISO 4217, więc dane pozostają
    niespójne, a nie „nierozpoznane, więc zgadnij"."""
    assert currency_from_attributes({"unit_of_measurement": "ct/kWh"}, "PLN") is None
    # Brak jednostki albo jednostka bez ukośnika — fallback wchodzi.
    assert currency_from_attributes({}, "PLN") == "PLN"
    assert currency_from_attributes({"unit_of_measurement": "kWh"}, "PLN") == "PLN"


@pytest.mark.parametrize("zla", ["zloty/kWh", "PL/kWh", "PLNN/kWh", "foo/kWh"])
def test_currency_niebedaca_trzyliterowym_kodem_ani_symbolem_daje_none(zla):
    """Licznik spoza listy dozwolonych (kod ISO, symbol albo token centowy)
    odrzuca encję CAŁKOWICIE — nawet z atrybutem `currency` obecnym (zgadywanie
    nierozpoznanego licznika to ta sama pomyłka rzędu wielkości co centy albo
    MWh bez kontroli)."""
    assert currency_from_attributes({"unit_of_measurement": zla}, None) is None
    assert currency_from_attributes({"currency": "EUR", "unit_of_measurement": zla}, None) is None


def test_currency_licznik_pusty_daje_none():
    assert currency_from_attributes({"unit_of_measurement": "/kWh"}, None) is None
    assert currency_from_attributes({"currency": "EUR", "unit_of_measurement": "/kWh"}, None) is None


def test_currency_symbol_zl_bez_atrybutu_daje_pln():
    """`zł` jest w jawnej mapie symboli (`zł` → PLN) — waluta przychodzi wprost
    z symbolu, bez potrzeby atrybutu `currency`."""
    assert currency_from_attributes({"unit_of_measurement": "zł/kWh"}, None) == "PLN"


def test_currency_symbol_zl_z_pasujacym_atrybutem_dziala():
    attrs = {"currency": "PLN", "unit_of_measurement": "zł/kWh"}
    assert currency_from_attributes(attrs, None) == "PLN"


def test_currency_symbol_zl_ze_sprzecznym_atrybutem_daje_none():
    """Symbol `zł` mapuje na PLN — atrybut `currency` inny niż PLN to dane
    niespójne, nie „atrybut wygrywa"."""
    attrs = {"currency": "EUR", "unit_of_measurement": "zł/kWh"}
    assert currency_from_attributes(attrs, None) is None


@pytest.mark.parametrize(("jednostka", "kod"), [("€/kWh", "EUR"), ("£/kWh", "GBP")])
def test_currency_symbole_walutowe_bez_atrybutu(jednostka, kod):
    assert currency_from_attributes({"unit_of_measurement": jednostka}, None) == kod


@pytest.mark.parametrize("jednostka", ["kr/kWh", "$/kWh"])
def test_currency_symbole_wieloznaczne_wymagaja_atrybutu(jednostka):
    """`kr` i `$` są rozpoznane jako jednostka ceny energii (mianownik OK), ale
    same nie wskazują JEDNEJ waluty — bez atrybutu `currency` nie zgadujemy."""
    assert currency_from_attributes({"unit_of_measurement": jednostka}, None) is None
    assert currency_from_attributes({"unit_of_measurement": jednostka}, "EUR") is None
    assert currency_from_attributes({"currency": "SEK", "unit_of_measurement": jednostka}, None) == "SEK"


def test_currency_licznik_z_kropka_normalizowany():
    """Kropka końcowa (skrót `ct.`, `gr.`) jest odcinana przed rozpoznaniem —
    `ct.` to ten sam token co `ct`."""
    assert currency_from_attributes({"currency": "EUR", "unit_of_measurement": "ct./kWh"}, None) == "EUR"
    assert currency_from_attributes({"unit_of_measurement": "gr./kWh"}, None) == "PLN"


@pytest.mark.parametrize("zla", ["zloty", "euro", "zł", "PL", "€"])
def test_currency_zly_atrybut_lub_fallback_daje_none(zla):
    """Kod waluty to trzy litery ASCII — kontrakt chmury nie zna innych."""
    assert currency_from_attributes({"currency": zla}, None) is None
    assert currency_from_attributes({}, zla) is None


def test_currency_odrzuca_nie_ascii_wygladajace_na_trzyliterowe():
    assert currency_from_attributes({"currency": "PLＮ"}, None) is None


def test_currency_atrybut_o_ksztalcie_kodu_ale_nierealny_daje_none():
    """`FOO` ma kształt kodu ISO (trzy litery ASCII), ale nie jest realną
    walutą — atrybut `currency` jest sprawdzany względem listy realnych
    kodów, nie tylko kształtu (tak samo jak licznik jednostki)."""
    assert currency_from_attributes({"currency": "FOO"}, None) is None
    assert currency_from_attributes({"currency": "foo"}, "PLN") is None


def test_currency_fallback_o_ksztalcie_kodu_ale_nierealny_daje_none():
    assert currency_from_attributes({}, "FOO") is None


def test_currency_bez_niczego_daje_fallback_albo_none():
    assert currency_from_attributes({}, "PLN") == "PLN"
    assert currency_from_attributes({}, None) is None
    assert currency_from_attributes({"unit_of_measurement": "kWh"}, None) is None


def test_waluta_argumentu_tez_musi_byc_trzyliterowym_kodem():
    attrs = {"raw_today": _raw(_polnoc(), 2, 60)}

    assert intervals_from_attributes(attrs, None, "zloty", _WAW) == []


def test_currency_argument_jest_normalizowany_w_przedzialach():
    attrs = {"raw_today": _raw(_polnoc(), 2, 60)}

    out = intervals_from_attributes(attrs, None, "pln", _WAW)

    assert {w["currency"] for w in out} == {"PLN"}


def test_brak_waluty_daje_pusto():
    """Bez waluty przedział jest bezużyteczny — wołający ma pominąć blok `prices`."""
    attrs = {"raw_today": _raw(_polnoc(), 2, 60)}

    assert intervals_from_attributes(attrs, None, None, _WAW) == []


# ── przeliczenia jednostek (za MWh, w centach) ──────────────────────────────


def test_mwh_przelicza_wartosci_dokladnie_przez_1000():
    attrs = {
        "unit_of_measurement": "PLN/MWh",
        "raw_today": _raw(_polnoc(), 2, 60, wartosc=25.5),
    }

    out = intervals_from_attributes(attrs, None, "PLN", _WAW)

    # Dzielenie przez CAŁKOWITY 1000 jest dokładne — mnożenie przez 0.001 by nie było
    # (25.5 * 0.001 == 0.025500000000000002, a 25.5 / 1000 == 0.0255 bit w bit).
    assert [w["buy"] for w in out] == [0.0255, 0.0255]


@pytest.mark.parametrize(
    "jednostka",
    ["ct/kWh", "c/kWh", "gr/kWh", "CT/KWH", "Cent/kWh", "¢/kWh", "p/kWh", "ct./kWh", "gr./kWh"],
)
def test_centy_przelicza_wartosci_dokladnie_przez_100(jednostka):
    attrs = {
        "unit_of_measurement": jednostka,
        "raw_today": _raw(_polnoc(), 2, 60, wartosc=123.45),
    }

    out = intervals_from_attributes(attrs, None, "EUR", _WAW)

    assert [w["buy"] for w in out] == [1.2345, 1.2345]


def test_grosze_za_mwh_dzieli_przez_iloczyn_dokladnie():
    """Mianownik i licznik centowy naraz — jeden dzielnik (1000 * 100), nie dwa
    kolejne mnożenia przez ułamki dziesiętne."""
    attrs = {
        "unit_of_measurement": "gr/MWh",
        "raw_today": _raw(_polnoc(), 2, 60, wartosc=123.45),
    }

    out = intervals_from_attributes(attrs, None, "PLN", _WAW)

    assert [w["buy"] for w in out] == [0.0012345, 0.0012345]


def test_price_in_cents_true_z_kodem_waluty_w_liczniku_odrzuca_cala_serie():
    """`NOK/kWh` + `price_in_cents=True` się wykluczają (licznik już deklaruje
    walutę główną, nie centy) — dane niespójne, cała seria odrzucona."""
    attrs = {
        "unit_of_measurement": "NOK/kWh",
        "price_in_cents": True,
        "raw_today": _raw(_polnoc(), 2, 60, wartosc=123.45),
    }

    assert intervals_from_attributes(attrs, None, "NOK", _WAW) == []


def test_price_in_cents_true_z_jednostka_skandynawska_przelicza_przez_100():
    """Wzorzec custom Nord Pool: licznik to symbol centowy (`Øre`), waluta z
    atrybutu `currency`, `price_in_cents=True` potwierdza skalę."""
    attrs = {
        "currency": "NOK",
        "unit_of_measurement": "Øre/kWh",
        "price_in_cents": True,
        "raw_today": _raw(_polnoc(), 2, 60, wartosc=123.45),
    }

    out = intervals_from_attributes(attrs, None, "NOK", _WAW)

    assert [w["buy"] for w in out] == [1.2345, 1.2345]


def test_sell_w_centach_przelicza_niezaleznie_od_buy():
    buy = {"raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0)}
    sell = {"unit_of_measurement": "ct/kWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=50.0)}

    out = intervals_from_attributes(buy, sell, "EUR", _WAW)

    assert [w["sell"] for w in out] == [pytest.approx(0.5), pytest.approx(0.5)]


def test_inny_mianownik_niz_kwh_mwh_odrzuca_cala_serie():
    attrs = {
        "unit_of_measurement": "PLN/Wh",
        "raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0),
    }

    assert intervals_from_attributes(attrs, None, "PLN", _WAW) == []


def test_sell_z_nieprawidlowym_mianownikiem_degraduje_do_none():
    buy = {"raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0)}
    sell = {"unit_of_measurement": "PLN/Wh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0)}

    out = intervals_from_attributes(buy, sell, "PLN", _WAW)

    assert [w["sell"] for w in out] == [None, None]


def test_sell_z_inna_waluta_niz_blok_degraduje_do_none():
    """Sprzedaż deklaruje jawnie INNĄ walutę niż blok (kupno) — dane niespójne,
    degradujemy tylko `sell`, `buy` zostaje nietknięte."""
    buy = {"unit_of_measurement": "PLN/kWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0)}
    sell = {"currency": "EUR", "unit_of_measurement": "EUR/MWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=123.45)}

    out = intervals_from_attributes(buy, sell, "PLN", _WAW)

    assert [w["buy"] for w in out] == [1.0, 1.0]
    assert [w["sell"] for w in out] == [None, None]


def test_sell_atrybut_sprzeczny_z_jednostka_sprzedazy_degraduje_do_none():
    """Sprzedaż sama w sobie jest niespójna (atrybut `currency` przeczy jej
    WŁASNEJ jednostce) — to NIE jest „brak deklaracji", więc mimo że
    `currency_from_attributes(sell)` zwraca None, sprzedaż i tak degraduje,
    nie dziedziczy waluty bloku."""
    buy = {"unit_of_measurement": "PLN/kWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0)}
    sell = {"currency": "EUR", "unit_of_measurement": "PLN/kWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=500.0)}

    out = intervals_from_attributes(buy, sell, "PLN", _WAW)

    assert [w["buy"] for w in out] == [1.0, 1.0]
    assert [w["sell"] for w in out] == [None, None]


def test_sell_symbol_wieloznaczny_sprzeczny_z_atrybutem_kupna_degraduje_do_none():
    """Sprzedaż `kr/kWh` jest wieloznaczna, ale atrybut `currency` ją
    rozstrzyga na EUR — skoro to inna waluta niż blok (PLN), sprzedaż
    degraduje, mimo że sam symbol jednostki „mógłby" być czymkolwiek."""
    buy = {"unit_of_measurement": "PLN/kWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0)}
    sell = {"currency": "EUR", "unit_of_measurement": "kr/kWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=500.0)}

    out = intervals_from_attributes(buy, sell, "PLN", _WAW)

    assert [w["sell"] for w in out] == [None, None]


def test_sell_symbol_wieloznaczny_zgodny_z_atrybutem_kupna_jest_przyjety():
    """To samo `kr/kWh`, ale atrybut `currency` rozstrzyga je na walutę
    bloku (PLN) — sprzedaż jest przyjęta."""
    buy = {"unit_of_measurement": "PLN/kWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0)}
    sell = {"currency": "PLN", "unit_of_measurement": "kr/kWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=50.0)}

    out = intervals_from_attributes(buy, sell, "PLN", _WAW)

    assert [w["sell"] for w in out] == [pytest.approx(50.0), pytest.approx(50.0)]


def test_sell_atrybut_bez_kodu_iso_degraduje_do_none():
    """Sprzedaż DEKLARUJE walutę (atrybut `currency` obecny), ale ten atrybut
    nie jest realnym kodem ISO (waluta jest sprawdzana względem listy, nie
    tylko kształtu) — to nie jest „brak deklaracji", sprzedaż degraduje, nie
    dziedziczy waluty bloku."""
    buy = {"unit_of_measurement": "PLN/kWh", "raw_today": _raw(_polnoc(), 2, 60, wartosc=1.0)}
    sell = {"currency": "€", "raw_today": _raw(_polnoc(), 2, 60, wartosc=500.0)}

    out = intervals_from_attributes(buy, sell, "PLN", _WAW)

    assert [w["sell"] for w in out] == [None, None]


# ── odcisk ──────────────────────────────────────────────────────────────────


def test_fingerprint_stabilny_dla_tej_samej_tresci():
    attrs = {"raw_today": _raw(_polnoc(), 24, 60, wartosc=lambda i: i / 10)}
    a = intervals_from_attributes(attrs, None, "PLN", _WAW)
    b = intervals_from_attributes(attrs, None, "PLN", _WAW)

    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_zmienia_sie_po_zmianie_ceny():
    wpisy = _raw(_polnoc(), 24, 60, wartosc=lambda i: i / 10)
    a = intervals_from_attributes({"raw_today": wpisy}, None, "PLN", _WAW)

    inne = [dict(w) for w in wpisy]
    inne[5]["value"] = 99.0
    b = intervals_from_attributes({"raw_today": inne}, None, "PLN", _WAW)

    assert fingerprint(a) != fingerprint(b)


def test_fingerprint_pustej_listy_jest_szesnastkowy_i_stabilny():
    assert fingerprint([]) == fingerprint([])
    assert len(fingerprint([])) == 40


def test_startat_nie_niesie_mikrosekund():
    """`startAt` to znacznik przedziału, nie chwila pomiaru — ułamek sekundy
    z encji rozjeżdżałby odcisk i klucz parowania sprzedaży."""
    wpisy = _raw(_polnoc(), 2, 60)
    wpisy[0]["start"] = "2026-09-15T00:00:00.123456+02:00"

    out = intervals_from_attributes({"raw_today": wpisy}, None, "PLN", _WAW)

    assert out[0]["startAt"] == "2026-09-14T22:00:00Z"


# ── gotowość dla onboardingu ─────────────────────────────────────────────────


def test_has_usable_prices_now_prawda_dla_pelnej_serii():
    attrs = {"raw_today": _raw(_polnoc(), 24, 60)}

    # `now` = początek doby: pokrywa bieżącą godzinę i sięga 24 h w przód (≥ 6 h).
    assert has_usable_prices_now(attrs, "PLN", _WAW, _polnoc()) is True


def test_has_usable_prices_now_falsz_gdy_brak_serii():
    assert has_usable_prices_now({}, "PLN", _WAW) is False


def test_has_usable_prices_now_falsz_gdy_brak_waluty():
    attrs = {"raw_today": _raw(_polnoc(), 24, 60)}

    assert has_usable_prices_now(attrs, None, _WAW) is False


def test_has_usable_prices_now_falsz_dla_platformy_bez_wspieranych_atrybutow():
    """Encja istnieje i ma cenę bieżącą, ale nie żadną z dwóch wspieranych list
    (`raw_today`/`raw_tomorrow` albo `today`/`tomorrow`) — pokazujemy „jeszcze
    nieobsługiwana", nie fałszywie „gotowa"."""
    attrs = {"state": "0.42", "prices_today": [{"time": "00:00", "price": 0.42}]}

    assert has_usable_prices_now(attrs, "PLN", _WAW) is False


def test_has_usable_prices_now_falsz_dla_okrojonej_serii():
    """Dwie godziny `today` (doba obcięta, brak jutra) nie sięgają 6 h w przód
    od `now` — seria kompletna formalnie, ale bezużyteczna dla plannera już
    teraz."""
    attrs = {"currency": "PLN", "raw_today": _raw(_polnoc(), 2, 60)}

    assert has_usable_prices_now(attrs, None, _WAW, _polnoc()) is False


def test_has_usable_prices_now_falsz_dla_nieaktualnej_serii():
    """Seria jest kompletna (24 h), ale `now` jest miesiąc PO niej — encja
    martwa, seria nieaktualna, nie pokrywa bieżącej godziny."""
    attrs = {"raw_today": _raw(_polnoc(), 24, 60)}
    poza_seria = _polnoc() + timedelta(days=30)

    assert has_usable_prices_now(attrs, "PLN", _WAW, poza_seria) is False


def test_has_usable_prices_now_falsz_gdy_seria_zaczyna_sie_po_now():
    """Seria jest kompletna i sięga daleko w przód, ale zaczyna się PO `now`
    (np. encja opublikowała już jutro, a jeszcze nie dzisiaj) — nie pokrywa
    bieżącej godziny, więc nie jest gotowa JUŻ TERAZ."""
    jutro_10 = _polnoc() + timedelta(days=1, hours=10)
    attrs = {"raw_today": _raw(jutro_10, 24, 60)}

    assert has_usable_prices_now(attrs, "PLN", _WAW, _polnoc()) is False
