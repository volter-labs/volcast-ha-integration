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


def test_currency_atrybut_ma_pierwszenstwo_nad_jednostka():
    attrs = {"currency": "eur", "unit_of_measurement": "PLN/kWh"}
    assert currency_from_attributes(attrs, "PLN") == "EUR"


@pytest.mark.parametrize("jednostka", ["ct/kWh", "c/kWh", "gr/kWh", "CT/KWH"])
def test_currency_centy_daja_none_niezaleznie_od_fallbacku(jednostka):
    """Jednostka centowa to NIE jest brak informacji — to informacja, że wartości
    są w setnych. Podstawienie fallbacku wysłałoby ceny 100x za duże."""
    assert currency_from_attributes({"unit_of_measurement": jednostka}, "EUR") is None
    assert currency_from_attributes({"unit_of_measurement": jednostka}, None) is None


@pytest.mark.parametrize("jednostka", ["PLN/MWh", "EUR/MWh", "PLN/Wh"])
def test_currency_inny_mianownik_niz_kwh_daje_none(jednostka):
    """Ceny kontraktu są za kWh. Za MWh to ta sama pomyłka rzędu wielkości."""
    assert currency_from_attributes({"unit_of_measurement": jednostka}, "PLN") is None


def test_currency_mianownik_kwh_bez_wzgledu_na_wielkosc_liter():
    assert currency_from_attributes({"unit_of_measurement": "PLN/KWH"}, None) == "PLN"


def test_currency_fallback_tylko_gdy_encja_nic_nie_mowi():
    """Jednostka OBECNA i nierozpoznana bije fallback — encja coś deklaruje,
    tylko my tego nie umiemy bezpiecznie zinterpretować."""
    assert currency_from_attributes({"unit_of_measurement": "ct/kWh"}, "PLN") is None
    assert currency_from_attributes({"unit_of_measurement": "PLN/MWh"}, "PLN") is None
    # Brak jednostki albo jednostka bez ukośnika — fallback wchodzi.
    assert currency_from_attributes({}, "PLN") == "PLN"
    assert currency_from_attributes({"unit_of_measurement": "kWh"}, "PLN") == "PLN"


@pytest.mark.parametrize("zla", ["zł/kWh", "zloty/kWh", "PL/kWh", "PLNN/kWh"])
def test_currency_niebedaca_trzyliterowym_kodem_daje_none(zla):
    assert currency_from_attributes({"unit_of_measurement": zla}, None) is None


@pytest.mark.parametrize("zla", ["zloty", "euro", "zł", "PL", "€"])
def test_currency_zly_atrybut_lub_fallback_daje_none(zla):
    """Kod waluty to trzy litery ASCII — kontrakt chmury nie zna innych."""
    assert currency_from_attributes({"currency": zla}, None) is None
    assert currency_from_attributes({}, zla) is None


def test_currency_odrzuca_nie_ascii_wygladajace_na_trzyliterowe():
    assert currency_from_attributes({"currency": "PLＮ"}, None) is None


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
