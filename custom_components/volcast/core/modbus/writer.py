"""Pisarz rejestrów: odczyt PRZED zapisem, zapis, odczyt zwrotny — wynik z porównania.

Żaden rejestr nie jest pisany bez możliwości odczytu zwrotnego, chyba że profil jawnie
wymienia go w `modbus.echo_only` (potwierdzenie samym echem, jak urządzenie referencyjne).
Odczyt przed zapisem (ramka FC 3 — na GoodWe UDP dwie — bez kosztu NVM) pozwala odróżnić „nie ustawił”
od „ustawił inaczej”. Odmowa (DENIED) to WYŁĄCZNIE prawdziwa odmowa urządzenia: ramka zapisu
poszła, urządzenie odpowiedziało, a rejestr został bez zmian. Wszystko, co nie wysłało zapisu
(nieudany odczyt przed zapisem, adres spoza profilu), to ERROR — chwilowa awaria nie może
zostać zapamiętana jako odmowa (rdzeń wstrzymuje ponowienie odmówionej prośby).

„Odczyt” w tabeli to odczyt PO ponowieniach: każdy odczyt (przed zapisem i zwrotny), który
nie dostał odpowiedzi (cisza, zerwanie, wyjątek Modbus ≠ 2), jest ponawiany najwyżej
`READ_RETRIES` (2) razy, po jednej próbie transportu i po przerwie przed każdym ponowieniem
(`RegisterWriter.read_retry_delays`): GoodWe UDP 0,75 s; transport strumieniowy przeczekuje
swoje czekanie na ponowne połączenie (backoff_min_s, potem podwojone; domyślnie 1,1 s i 2,1 s);
transport nieznany 1,1 s. Ramka ZAPISU nigdy nie jest ponawiana przez pisarza: jedna próba zapisu na
klucz. Transport UDP wysyła ją w tej próbie drugi raz wyłącznie po całkowitej ciszy (jak Box; wartość
bezwzględna); zwykły zapis tylko, gdy `may_resend` pisarza (budżet NVM klucza) pozwala PRZED
wysłaniem, powrót do trybu bazowego (`async_write_restore`, `async_write_outside_budget`) zawsze — każda
wysyłka idzie przez `on_send`, więc budżet liczy obie, a `stats.write_resends` pokazuje ponowienie.
Dodatkowy czas na jeden odczyt: suma przerw + READ_RETRIES × timeout_s (+ odstępy gap_s;
na transporcie strumieniowym + connect_timeout_s na każde ponowne połączenie). GoodWe UDP
(timeout 2 s, odstęp 0,3 s): ≤ 5,5 s + odstępy na odczyt; odczyt przed zapisem to tam dwa
odczyty (niżej), plus ewentualne odczekanie przed ponownym odczytem zwrotnym. Bez strat klucz to
~4 wymiany po ~0,5 s (≈ 2 s), z odczekaniem ≈ 5 s; sesja 6 kluczy ≈ 12–20 s — dłużej niż
STOP_WRITE_TIMEOUT_S (10 s), więc zatrzymanie częściej przestaje czekać na zapis w toku (runtime
i tak czeka na starego pisarza przed nowym — to nie błąd poprawności). Wyjątek 2 przy odczycie nie
jest ponawiany. „Brak” = wszystkie próby bez odpowiedzi.

Łącze bez korelacji odpowiedzi (GoodWe UDP, `views.py`): moduł Wi-Fi bywa, że odpowiada poprzednią
odpowiedzią, a odpowiedź FC 3 nie niesie adresu. Każdy odczyt rejestru ma długość odpowiedzi inną niż
poprzedni odczyt na łączu (blok z `modbus.verify_blocks` albo sam rejestr; rejestr czytany tylko
pojedynczo — najpierw blok rozdzielający). Odczyt przed zapisem to DWA takie odczyty, które muszą
się zgodzić; wyjątek 2 jest ostateczny dopiero, gdy powtórzy się w odczycie samego rejestru po
bloku rozdzielającym z poprawną odpowiedzią. Niezgoda (także wyjątek 2 tylko raz) → ERROR, nic
nie wysłano. Wyjątek 2 na bloku to nie werdykt o rejestrze — odczyt wraca do samego rejestru.

| echo                      | odczyt zwrotny                 | wynik |
|---------------------------|--------------------------------|-------|
| —  (odczyt przed: wyjątek 2, na UDP potwierdzony) | —    | UNSUPPORTED (nic nie wysłano, bez ponowień) |
| —  (odczyt przed: brak / niezgodny) | —                    | ERROR (nic nie wysłano) |
| wyjątek 2                 | —                              | UNSUPPORTED |
| dowolne                   | = zamówiona                    | OK |
| dowolne                   | brak                           | ERROR |
| zgodne                    | = sprzed zapisu                | odczekanie i ponowny odczyt (niżej) |
| wyjątek ≠ 2, 5            | = sprzed zapisu                | DENIED |
| zgodne / wyjątek ≠ 2, 5   | inna, w stronę bezpieczną      | OK_ADJUSTED z wartością rzeczywistą (tylko liczby) |
| zgodne / wyjątek ≠ 2, 5   | inna, w stronę groźną / nie-liczba | ERROR |
| brak / wyjątek 5          | ≠ zamówiona                    | ERROR (zapis mógł jeszcze dojść) |

Echo zgodne, a odczyt zwrotny = sprzed zapisu: falownik stosuje nastawę z opóźnieniem (GW8KN-ET:
pierwszy odczyt po echu jeszcze stary, nowa wartość < 1 s później). Zamiast DENIED — najwyżej
`READBACK_SETTLE_READS` (2) ponowne odczyty, każdy po `write_policy.readback_settle_s` profilu
(domyślnie 1,5 s). Pierwszy inny niż sprzed zapisu idzie do tabeli (= zamówiona → OK, inna → wiersze
„inna”); DENIED dopiero, gdy ostatni nadal pokazuje wartość sprzed zapisu; ponowny odczyt bez
odpowiedzi → ERROR. Ponowne odczyty to tylko FC 3 — próba zapisu nadal jedna.

Pola bitowe (bit ładowania z sieci programu, włącznik harmonogramu) są składane na słowie
z odczytu PRZED zapisem, nie z obrazu ostatniego odpytania. Włącznik harmonogramu, który już
jest w żądanym stanie według tego świeżego odczytu, nie jest wysyłany (OK bez ramki) — dzięki
temu wyłączenie przed przepisaniem programów jest zawsze w sekwencji, a kosztuje ramkę tylko
wtedy, gdy harmonogram naprawdę działa. `tou_word` to surowe słowo włącznika (powrót do słowa
właściciela, bez składania bitów).

Ponowną wysyłkę (tylko UDP, przy całkowitej ciszy) i reset kanału po przekroczeniu czasu
robi transport — odczyt zwrotny (i każde jego ponowienie) idzie świeżym kanałem. Odczyt
porównuje całe słowo (pola bitowe niosą bity właściciela). Jeden zamek na pisarza: odczyt przed,
zapis i odczyt zwrotny jednego klucza (z ponowieniami) nie przeplatają się z innym zapisem. Do tego
sesja na wyłączność łącza (`BaseTransport.exclusive`) na odczyty przed, zapis i odczyt zwrotny —
żądanie odpytywania czeka do jej końca. Na czas odczekania przed ponownym odczytem zwrotnym łącze
jest zwalniane (odpytywanie nie czeka sekund); każdy ponowny odczyt bierze sesję znowu i dopiero
w niej wybiera długość odczytu.
Zapis warunkowy powrotu (`only_from`, hamulec do trybu neutralnego): ramka idzie tylko, gdy odczyt
przed zapisem pokazał jedną z dozwolonych wartości; inna → `KEPT` bez ramki (klucz tylko z echem —
zawsze `KEPT`, bo warunku nie da się potwierdzić).
Pisarz nigdy nie rzuca.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Callable, Collection, Iterable

from ..profile import DEFAULT_READBACK_SETTLE_S
from ..registers import RegisterWrite
from ..transports.base import ModbusException, TransportError
from ..transports.stream import StreamTransport
from ..write_sequence import DENIED, ERROR, OK, OK_ADJUSTED, UNSUPPORTED, AdjustedOutcome
from .client import RegisterClient
from .views import key_views, needs_disambiguation, pick_view, prev_read_count, separator_blocks

_LOGGER = logging.getLogger(__name__)
_ECHO_OK, _ECHO_EXCEPTION, _ECHO_NONE = "ok", "exception", "none"
_EXC_ACKNOWLEDGE = 5                   # „przyjęte, w trakcie” — jak brak potwierdzenia
# Klucze liczbowe, dla których wartość przycięta przez urządzenie ma sens jako „zastosowana”.
_NUMERIC_ENCODINGS = ("watts", "percent")
# Ponowienie ODCZYTU (przed zapisem i zwrotnego) po porażce pierwszej próby: tyle dodatkowych
# odczytów, każdy jedną próbą transportu, po przerwie (`RegisterWriter.read_retry_delays`).
# Pisarz nigdy nie ponawia ramki zapisu (ponowna wysyłka po ciszy — tylko transport UDP, w budżecie NVM).
READ_RETRIES = 2
READ_RETRY_BACKOFF_S = 0.75            # GoodWe UDP: bez czekania na ponowne połączenie
READ_RETRY_FALLBACK_S = 1.1            # transport nieznany: ≥ domyślne backoff_min_s (1 s) + zapas
_RECONNECT_MARGIN_S = 0.1              # zapas ponad czekanie transportu strumieniowego
# Ponowne odczyty zwrotne po odczekaniu, zanim wartość sprzed zapisu stanie się odmową.
READBACK_SETTLE_READS = 2
# Zapis warunkowy (`only_from`): świeży odczyt przed zapisem pokazał wartość spoza dozwolonych —
# nic nie wysłano (np. tryb zmieniony przez właściciela od ostatniego odpytania). Dla wykonawcy
# grupowego to porażka bez niejednoznaczności (nie ERROR — rejestr na pewno nietknięty).
KEPT = "kept"
_sleep = asyncio.sleep                 # podmieniane w testach


@dataclass(frozen=True)
class _Settle:
    """Wynik wymiany do rozstrzygnięcia po odczekaniu (zapis z wartością już złożoną)."""
    w: RegisterWrite
    before: int


class _Unconfirmed(TransportError):
    """Odczyty bez zgody (łącze bez korelacji odpowiedzi) — wynik niepewny, nic nie wysłano."""


def _is_illegal_address(err: TransportError) -> bool:
    """Wyjątek Modbus 2: rejestr nie istnieje — odpowiedź ostateczna, bez ponawiania."""
    return isinstance(err, ModbusException) and err.code == 2


_TOU_WORD_KEYS = ("tou_enable", "tou_word")


def _matches(key: str, keys: frozenset[str]) -> bool:
    """Klucz w zbiorze; `tou` obejmuje pola programów (`tou.<i>.<pole>`) i słowo włącznika."""
    return key in keys or ("tou" in keys and (key.startswith("tou.") or key in _TOU_WORD_KEYS))


def expected_address(profile, key: str) -> int | None:
    """Adres rejestru klucza według profilu; None = klucz spoza map zapisu."""
    spec = profile.raw["write"]
    if key.startswith("tou."):
        parts = key.split(".")
        tp = spec.get("tou_program")
        if tp is None or len(parts) != 3 or not parts[1].isdigit() or parts[2] not in tp \
                or not 1 <= int(parts[1]) <= tp["count"]:
            return None
        return tp[parts[2]]["addr"] + int(parts[1]) - 1
    if key == "tou_word":                  # surowe słowo włącznika (powrót do słowa właściciela)
        key = "tou_enable"
    s = spec.get(key)
    return s.get("addr") if isinstance(s, dict) else None


# Kierunek bezpieczny odchylenia: -1 = rzeczywista nie większa niż zamówiona (moc, limit eksportu,
# górny próg), +1 = nie mniejsza (dolny próg). Odchylenie w drugą stronę = ERROR (np. minimalna moc
# urządzenia ponad zamówioną: tryb pracowałby na większej mocy niż w planie).
_SAFE_DEVIATION = {"power_w": -1, "export_limit_w": -1, "soc_max": -1, "soc_min": 1, "tou.power_w": -1}


def _numeric_actual(profile, key: str, word: int, requested: int) -> float | None:
    """Rzeczywista wartość klucza liczbowego z odczytu, gdy odchylenie jest w stronę bezpieczną;
    None dla trybu, przełączników, bitów, startów, SoC programów i odchylenia w stronę groźną."""
    if key.startswith("tou."):
        name = "tou." + key.split(".")[-1]
    else:
        name = key
        if ((profile.raw["write"].get(key) or {}).get("encode")) not in _NUMERIC_ENCODINGS:
            return None
    direction = _SAFE_DEVIATION.get(name)
    if direction is None or (word - requested) * direction < 0:
        return None
    return float(word)


def _fresh_value(profile, key: str, value: int, before: int) -> int:
    """Pole bitowe: odczyt-modyfikacja-zapis na słowie ŚWIEŻO odczytanym przed zapisem
    (bity właściciela zmienione od ostatniego odpytania nie mogą zostać nadpisane)."""
    write = profile.raw["write"]
    if key.startswith("tou.") and key.endswith(".grid_charge"):
        mask = 1 << write["tou_program"]["grid_charge"]["bit"]
        return (before & ~mask & 0xFFFF) | (value & mask)
    if key == "tou_enable":
        spec = write["tou_enable"]
        ebit = 1 << spec["enable_bit"]
        if not value & ebit:
            return before & ~ebit & 0xFFFF
        # ON: bit włącznika i wszystkie dni maski (plan działa codziennie); pozostałe bity zostają
        return (before | ebit | spec.get("day_mask", 0)) & 0xFFFF
    return value                                   # `tou_word`: surowe słowo właściciela


class RegisterWriter:
    def __init__(self, client: RegisterClient, profile, *,
                 on_send: Callable[[str], None] | None = None, unreadable: Iterable[str] = (),
                 may_resend: Callable[[str], bool] | None = None, confirm_write_illegal: bool = False,
                 illegal_once: set[str] | None = None) -> None:
        self.client = client
        self.profile = profile
        # Wyjątek 2 w odpowiedzi na ZAPIS (odczyt rejestru działa): bez potwierdzenia nie jest werdyktem.
        # Pierwszy raz rozstrzyga odczyt zwrotny (nietknięty → DENIED, zapisany → OK), dopiero powtórka
        # w późniejszym zapisie tego klucza → UNSUPPORTED (wykonawca trzyma to na całą sesję). Udany zapis
        # klucza kasuje pierwszy raz. Domyślnie wyłączone: aplikator wzorcowy (wektory złote) — od razu.
        # `illegal_once` od wołającego: zbiór „pierwszych razy” przeżywa nowego pisarza (ponowne
        # połączenie), inaczej prawdziwy wyjątek 2 na łączu, które się zrywa, nigdy by się nie potwierdził.
        self._confirm_illegal = confirm_write_illegal
        self._illegal_once: set[str] = illegal_once if illegal_once is not None else set()
        self._on_send = on_send
        # pytane przez transport PRZED ponowną wysyłką zapisu (budżet NVM); None = bez sprawdzenia;
        # tylko zwykłe zapisy — powrót do trybu bazowego idzie poza budżetem
        self._may_resend = may_resend
        self._budgeted = True
        self._function = profile.modbus.write_function
        self._lock = asyncio.Lock()
        # potwierdzane samym echem — wyłącznie z jawnej listy profilu
        self.echo_only: frozenset[str] = frozenset(profile.modbus.echo_only)
        # bez odczytu zwrotnego (z sondy): nie pisane wcale, nawet bez próby odczytu
        self.unreadable: frozenset[str] = frozenset(unreadable)
        # łącze bez korelacji odpowiedzi (GoodWe UDP): odczyty o zmiennej długości i potwierdzanie
        self._disambiguate = needs_disambiguation(client.transport)
        self._last_count: int | None = None          # długość ostatniego odczytu (transport bez licznika)

    async def async_write(self, w: RegisterWrite) -> str:
        return await self._guarded(w, skip_equal=False, budgeted=True)

    async def async_write_restore(self, w: RegisterWrite, *, only_from: Collection[int] | None = None) -> str:
        """Zapis powrotu do trybu bazowego: o tym, czy ramka w ogóle idzie, rozstrzyga świeży odczyt
        PRZED zapisem (rejestr już ma wartość bazową → OK bez ramki), nie odczyt z cyklu. Poza
        budżetem NVM: ponowna wysyłka po ciszy nigdy nie jest odmawiana (liczona przez `on_send`).

        `only_from` (hamulec do trybu neutralnego): ramka idzie tylko, gdy ten sam świeży odczyt
        (pod wyłącznością łącza) pokazał jedną z tych wartości; inna → `KEPT`, nic nie wysłano.
        Klucz bez odczytu przed zapisem (tylko echo) nie spełnia warunku nigdy."""
        return await self._guarded(w, skip_equal=True, budgeted=False, only_from=only_from)

    async def async_write_outside_budget(self, w: RegisterWrite) -> str:
        """Zwykły zapis (bez pomijania równych) poza budżetem NVM — powrót do trybu bazowego po
        wyczerpaniu budżetu: ponowna wysyłka po ciszy nie jest odmawiana (liczona przez `on_send`)."""
        return await self._guarded(w, skip_equal=False, budgeted=False)

    async def _guarded(self, w: RegisterWrite, *, skip_equal: bool, budgeted: bool,
                       only_from: Collection[int] | None = None) -> str:
        try:
            async with self._lock:
                self._budgeted = budgeted              # pod zamkiem pisarza — jeden zapis naraz
                out = await self._write(w, skip_equal=skip_equal, only_from=only_from)
                if out in (OK, OK_ADJUSTED):
                    self._illegal_once.discard(w.key)
                return out
        except Exception as err:  # noqa: BLE001 — pisarz nie rzuca; zapis niepewny
            _LOGGER.warning("direct write of %s failed: %s", w.key, type(err).__name__)
            return ERROR

    async def _write(self, w: RegisterWrite, *, skip_equal: bool = False,
                     only_from: Collection[int] | None = None) -> str:
        if expected_address(self.profile, w.key) != w.addr or isinstance(w.value, bool) \
                or not isinstance(w.value, int) or not 0 <= w.value <= 0xFFFF:
            _LOGGER.error("direct write of %s refused: address or value outside the profile", w.key)
            return ERROR                   # nic nie wysłano — to nie odmowa urządzenia
        if _matches(w.key, self.unreadable):
            return UNSUPPORTED             # bez odczytu zwrotnego nie piszemy (nie da się przywrócić)
        if only_from is not None and _matches(w.key, self.echo_only):
            return KEPT                    # warunku nie da się potwierdzić odczytem — nie piszemy na ślepo
        async with self._link():
            out = await self._exchange(w, skip_equal=skip_equal, only_from=only_from)
        if not isinstance(out, _Settle):
            return out
        # Łącze zwolnione na czas odczekania (odpytywanie nie czeka sekund); każdy ponowny odczyt
        # bierze je znowu i dopiero wtedy wybiera długość (`_read_once`).
        w, before = out.w, out.before
        back = await self._settle(w.addr, before)
        if back is None:
            return ERROR
        if back == w.value:
            return OK
        return self._judge(w, before, back)

    def _link(self):
        """Wyłączność łącza na sekwencję klucza (`BaseTransport.exclusive`): żądanie odpytywania nie
        wejdzie między wybór długości odczytu a jego wysłanie ani między odczyty, zapis i odczyt
        zwrotny — inaczej powtórzona przez moduł odpowiedź odpytywania tej samej długości byłaby
        wzięta za odpowiedź pisarza. Transport bez tej funkcji (atrapy) — bez blokady."""
        exclusive = getattr(self.client.transport, "exclusive", None)
        return exclusive() if callable(exclusive) else contextlib.nullcontext()

    def _judge(self, w: RegisterWrite, before: int, back: int) -> str:
        if back == before:
            return DENIED
        actual = _numeric_actual(self.profile, w.key, back, w.value)
        if actual is None:
            return ERROR                   # tryb/bit/start inny albo odchylenie w groźną stronę
        _LOGGER.warning("direct write of %s applied with a different value by the inverter", w.key)
        return AdjustedOutcome(actual)

    async def _exchange(self, w: RegisterWrite, *, skip_equal: bool, only_from: Collection[int] | None = None):
        """Odczyt(y) przed, zapis, odczyt zwrotny — pod wyłącznością łącza. Wynik albo `_Settle`
        (echo zgodne, odczyt zwrotny = sprzed zapisu: ponowne odczyty po odczekaniu)."""
        if _matches(w.key, self.echo_only):
            return await self._write_echo_only(w)
        try:
            before = await self._read_before(w.addr)
        except TransportError as err:
            if _is_illegal_address(err):
                return UNSUPPORTED
            _LOGGER.debug("pre-write read of %s failed: %s", w.key, type(err).__name__)
            return ERROR                   # nic nie wysłano; chwilowa awaria, nie odmowa
        w = RegisterWrite(w.key, w.addr, _fresh_value(self.profile, w.key, w.value, before))
        if (skip_equal or w.key in _TOU_WORD_KEYS) and w.value == before:
            return OK                      # rejestr już w tym stanie (świeży odczyt) — bez ramki NVM
        if only_from is not None and before not in only_from:
            return KEPT                    # wartość spoza warunku (np. tryb właściciela) — bez ramki
        echo = await self._send(w)
        if echo == UNSUPPORTED:
            if not self._first_illegal(w.key):
                return UNSUPPORTED
            # Pierwszy wyjątek 2 na zapis: odczyt zwrotny rozstrzyga, czy rejestr jest nietknięty.
            back = await self._read_back(w.addr)
            if back is None:
                return ERROR
            if back == w.value:
                return OK                  # zapis doszedł — wyjątek był cudzą/starą odpowiedzią
            return DENIED if back == before else ERROR
        back = await self._read_back(w.addr)
        if back is None:
            return ERROR
        if back == w.value:
            return OK
        if echo == _ECHO_NONE:
            return ERROR
        if back == before and echo == _ECHO_OK:
            return _Settle(w, before)
        return self._judge(w, before, back)

    async def _send(self, w: RegisterWrite) -> str:
        gate = self._may_resend is not None and self._budgeted     # powrót do bazy — bez bramki budżetu
        extra = {"may_resend": lambda: self._resend_ok(w.key)} if gate else {}
        try:
            await self.client.transport.write(w.addr, [w.value], function=self._function,
                                              on_send=lambda: self._sent(w.key), **extra)
            return _ECHO_OK
        except ModbusException as err:
            if err.code == 2:
                return UNSUPPORTED
            # wyjątek nie dowodzi, że rejestr jest nietknięty; 5 = „przyjęte, w trakcie”
            return _ECHO_NONE if err.code == _EXC_ACKNOWLEDGE else _ECHO_EXCEPTION
        except TransportError as err:
            _LOGGER.debug("direct write of %s: %s", w.key, type(err).__name__)
            return _ECHO_NONE              # brak echa / zerwanie: zapis mógł dojść

    async def _write_echo_only(self, w: RegisterWrite) -> str:
        echo = await self._send(w)
        if echo == UNSUPPORTED:
            # bez odczytu zwrotnego: pierwszy raz odmowa na ten cykl, powtórka — nieobsługiwany
            return DENIED if self._first_illegal(w.key) else UNSUPPORTED
        return OK if echo == _ECHO_OK else ERROR

    def _first_illegal(self, key: str) -> bool:
        """Wyjątek 2 na zapis klucza po raz pierwszy (gdy potwierdzanie jest włączone; zakres — `illegal_once`)."""
        if not self._confirm_illegal or key in self._illegal_once:
            return False
        self._illegal_once.add(key)
        return True

    async def _settle(self, addr: int, before: int) -> int | None:
        """Echo potwierdzone, a odczyt zwrotny pokazał wartość sprzed zapisu: falownik stosuje nastawę
        z opóźnieniem (GW8KN-ET: nowa wartość < 1 s po echu). Najwyżej `READBACK_SETTLE_READS`
        ponownych odczytów, każdy po `readback_settle_s` profilu; pierwsza wartość inna niż sprzed
        zapisu rozstrzyga. None = ponowny odczyt bez odpowiedzi (zapis mógł dojść — ERROR)."""
        settle = getattr(self.profile, "readback_settle_s", DEFAULT_READBACK_SETTLE_S)
        back: int | None = before
        for _ in range(READBACK_SETTLE_READS):
            await _sleep(settle)
            async with self._link():
                back = await self._read_back(addr)
            if back != before:
                break
        return back

    async def _read_back(self, addr: int) -> int | None:
        try:
            return await self._read(addr)
        except TransportError as err:
            _LOGGER.debug("read-back failed: %s", type(err).__name__)
            return None

    def read_retry_delays(self) -> tuple[float, ...]:
        """Przerwy przed kolejnymi ponowieniami odczytu. Transport strumieniowy (TCP, RTU przez TCP,
        V5) po nieudanym połączeniu albo zerwaniu nie łączy się ponownie przed `backoff_min_s`,
        a przy kolejnej porażce czeka dwa razy dłużej — przerwa ponowienia i to czekanie przeczekuje
        (inaczej ponowienie kończy się od razu `LinkDown`). GoodWe UDP nie ma czekania na połączenie."""
        transport = self.client.transport
        if isinstance(transport, StreamTransport):
            base = transport.cfg.backoff_min_s
            return tuple(max(READ_RETRY_BACKOFF_S, base * 2 ** i + _RECONNECT_MARGIN_S)
                         for i in range(READ_RETRIES))
        if getattr(transport, "kind", None) == "goodwe_udp":
            return (READ_RETRY_BACKOFF_S,) * READ_RETRIES
        return (READ_RETRY_FALLBACK_S,) * READ_RETRIES

    async def _read_before(self, addr: int) -> int:
        """Wartość sprzed zapisu. Łącze z korelacją odpowiedzi: jeden odczyt (`_read`).

        Łącze bez korelacji (`views.py`): dwa odczyty o RÓŻNEJ długości odpowiedzi muszą się zgodzić
        (nieaktualna albo obca odpowiedź jednego z nich daje inne słowo) — inaczej `_Unconfirmed`
        (ERROR, nic nie wysłano). Wyjątek 2 jest ostateczny (UNSUPPORTED) dopiero, gdy powtórzy się
        w odczycie samego rejestru po bloku rozdzielającym z poprawną odpowiedzią; wartość w tym
        odczycie albo wyjątek 2 tylko w drugim odczycie → `_Unconfirmed`."""
        if not self._disambiguate:
            return await self._read(addr)
        try:
            first = await self._read(addr)
        except TransportError as err:
            if not _is_illegal_address(err):
                raise
            await self._separate(addr, None)
            await self._read_view(addr, (addr, 1), None)       # wyjątek 2 ponownie → wychodzi
            raise _Unconfirmed("exception 2 not repeated") from None
        try:
            second = await self._read(addr)
        except TransportError as err:
            if _is_illegal_address(err):
                raise _Unconfirmed("exception 2 after a value") from None
            raise
        if second != first:
            raise _Unconfirmed("pre-write reads disagree")
        return first

    async def _read(self, addr: int) -> int:
        """Odczyt jednego rejestru z ograniczonym ponowieniem (tylko odczyt, nigdy zapis).

        Pierwsza próba z pełną liczbą prób transportu; po porażce (cisza, zerwanie, wyjątek
        Modbus ≠ 2) najwyżej `READ_RETRIES` ponowień po jednej próbie transportu, każde po
        przerwie z `read_retry_delays` — przerwa rozdziela odczyt od chwilowego zatoru modułu
        (inny klient odpytujący ten sam moduł Wi-Fi). Woła się pod zamkiem pisarza.
        Ostatni błąd wychodzi do wołającego."""
        try:
            return await self._read_once(addr, None)
        except TransportError as err:
            if _is_illegal_address(err):
                raise
            last = err
        for delay in self.read_retry_delays():
            await _sleep(delay)
            try:
                return await self._read_once(addr, 1)
            except TransportError as err:
                if _is_illegal_address(err):
                    raise
                last = err
        raise last

    async def _read_once(self, addr: int, tries: int | None) -> int:
        """Jedna próba odczytu rejestru. Łącze bez korelacji: widok o długości innej niż poprzedni odczyt
        (gdy rejestr czyta się tylko pojedynczo — najpierw blok rozdzielający); wyjątek 2 na bloku to
        nie werdykt o rejestrze — próba wraca do samego rejestru."""
        if not self._disambiguate:
            if tries is None:
                return await self.client.read_register(addr)
            return await self.client.read_register(addr, tries=tries)
        skip: set[tuple[int, int]] = set()
        while True:
            views = key_views(self.profile, addr, skip=skip)
            view = pick_view(views, prev_read_count(self.client.transport, self._last_count))
            if view is None:
                await self._separate(addr, tries)
                view = pick_view(views, prev_read_count(self.client.transport, self._last_count)) or views[0]
            try:
                return await self._read_view(addr, view, tries)
            except ModbusException as err:
                if err.code != 2 or view == (addr, 1):
                    raise
                skip.add(view)

    async def _read_view(self, addr: int, view: tuple[int, int], tries: int | None) -> int:
        self._last_count = view[1]
        words = await self.client.read_block(view[0], view[1], tries=tries)
        return words[addr - view[0]]

    async def _separate(self, addr: int, tries: int | None) -> None:
        """Blok rozdzielający (znany, innej długości niż każdy widok rejestru) z poprawną odpowiedzią —
        po nim poprzednia odpowiedź modułu nie pasuje do odczytu rejestru. Wyjątek Modbus na bloku →
        następny kandydat (inny model może nie mieć rejestru bloku); inna porażka albo brak kandydata
        → `_Unconfirmed`."""
        for block in separator_blocks(self.profile, addr):
            self._last_count = block[1]
            try:
                await self.client.read_block(block[0], block[1], tries=tries)
                return
            except ModbusException:
                continue
            except TransportError as err:
                raise _Unconfirmed(f"separator read failed: {type(err).__name__}") from None
        raise _Unconfirmed("no separator block answered")

    def _resend_ok(self, key: str) -> bool:
        """Czy transport może wysłać zapis drugi raz (po ciszy) — np. budżet NVM klucza nie wyczerpany.
        Błąd sprawdzenia = bez ponownej wysyłki (wynik rozstrzyga odczyt zwrotny)."""
        try:
            return bool(self._may_resend(key))
        except Exception as err:  # noqa: BLE001 — sprawdzenie nie może zepsuć wymiany
            _LOGGER.error("write resend check failed for %s: %s", key, type(err).__name__)
            return False

    def _sent(self, key: str) -> None:
        if self._on_send is None:
            return
        try:
            self._on_send(key)
        except Exception as err:  # noqa: BLE001 — licznik nie może zepsuć wymiany
            _LOGGER.error("write counter failed for %s: %s", key, type(err).__name__)


class NoWriteWriter:
    """Pisarz trybu próbnego: nigdy nie wysyła, zawsze DENIED (druga, niezależna blokada)."""

    def __init__(self) -> None:
        self.blocked_attempts = 0

    async def async_write(self, w) -> str:
        self.blocked_attempts += 1
        _LOGGER.error("direct write blocked in trial mode: %s", getattr(w, "key", "?"))
        return DENIED
