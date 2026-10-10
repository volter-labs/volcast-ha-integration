"""Odczyt jednego rejestru na łączu, którego odpowiedź nie wskazuje żądania (GoodWe UDP).

Odpowiedź FC 3 w ramce RTU nie niesie adresu, a moduł Wi-Fi GoodWe bywa, że odpowiada na żądanie
POPRZEDNIĄ odpowiedzią (identyczna ramka, kilka razy z rzędu). Transport przyjmuje każdą ramkę
o zgodnej jednostce, funkcji i długości — nieaktualna odpowiedź tej samej długości przechodzi.
Dlatego pisarz i sonda czytają rejestr tak, żeby kolejne żądania różniły się długością odpowiedzi:

* widoki rejestru (`key_views`): bloki z `modbus.verify_blocks` profilu, które go obejmują (bloki
  czytane przez urządzenie referencyjne — dozwolone), a na końcu sam rejestr;
* `pick_view` wybiera pierwszy widok o długości innej niż ostatnie żądanie odczytu na łączu
  (`last_read_count` transportu); gdy takiego nie ma (rejestr tylko pojedynczo), wołający wysyła
  najpierw blok rozdzielający (`separator_blocks`): znany blok o długości różnej od KAŻDEGO widoku
  rejestru i nieobejmujący go — po nim nieaktualna odpowiedź ma inną długość niż odczyt rejestru.

Wyjątek Modbus nie ma długości: wyjątek 2 przyjmuje się jako ostateczny („rejestru nie ma”) dopiero,
gdy powtórzy się w odczycie rejestru po bloku rozdzielającym z poprawną odpowiedzią (wtedy poprzednia
odpowiedź modułu nie jest wyjątkiem). Wyjątek 2 na bloku to nie werdykt o rejestrze — tylko o bloku.

Ryzyko resztkowe: obca ramka wyjątku innego klienta (np. drugiej integracji odpytującej 47760) pasuje
do każdego żądania FC 3 — potwierdzenie wyjątku 2 wymaga jednak dwóch takich zbiegów (przed i po bloku
rozdzielającym), a ramka obca tej samej długości co nasz odczyt (blok Boxa 4 słowa) daje niezgodę
odczytów przed zapisem (ERROR, nic nie wysłano), nie fałszywy wynik.

Łącza z korelacją odpowiedzi (Modbus TCP — identyfikator transakcji, V5 — numer sekwencji) i łącze
strumieniowe RTU (kanał resetowany po każdym przekroczeniu czasu) czytają po staremu.
"""
from __future__ import annotations

from typing import Iterable

from ..registers import FC_HOLDING
from .blocks import block_fc

Block = tuple[int, int]

# Rodzaje transportu bez korelacji odpowiedzi z żądaniem i z modułem powtarzającym odpowiedzi.
UNCORRELATED_KINDS = frozenset({"goodwe_udp"})


def needs_disambiguation(transport) -> bool:
    return getattr(transport, "kind", None) in UNCORRELATED_KINDS


def _covers(block: Block, addr: int) -> bool:
    return block[0] <= addr < block[0] + block[1]


def key_views(profile, addr: int, *, skip: Iterable[Block] = ()) -> tuple[Block, ...]:
    """Odczyty obejmujące rejestr: znane bloki (bez `skip` — odrzuconych przez urządzenie), potem sam rejestr."""
    bad = set(skip)
    blocks = tuple(b for b in profile.modbus.verify_blocks if _covers(b, addr) and b not in bad)
    return (*blocks, (addr, 1))


def pick_view(views: Iterable[Block], prev_count: int | None) -> Block | None:
    """Pierwszy widok o długości innej niż poprzednie żądanie odczytu; None = wszystkie tej samej."""
    views = tuple(views)
    if prev_count is None:
        return views[0] if views else None
    return next((v for v in views if v[1] != prev_count), None)


def _holding_known(profile) -> tuple[Block, ...]:
    """Znane bloki (weryfikacyjne, potem identyfikacja) w rejestrach holding. Blok identyfikacji
    z rejestrów input (FC 4) nie jest kandydatem: wołający czytają rozdzielacz jako `(adres, liczba)`,
    a odczyt FC 3 pod adresem bloku input to inny rejestr."""
    return tuple(b for b in (*profile.modbus.verify_blocks, *profile.modbus.identify_reads)
                 if block_fc(b) == FC_HOLDING)


def separator_blocks(profile, addr: int) -> tuple[Block, ...]:
    """Znane bloki (weryfikacyjne, potem identyfikacja) o długości różnej od każdego widoku rejestru
    i nieobejmujące go, w kolejności prób; pusto = profil takiego nie ma. Wołający bierze następny,
    gdy blok nie dał poprawnej odpowiedzi (inny model może nie mieć rejestru bloku)."""
    counts = {n for _, n in key_views(profile, addr)}
    return tuple(dict.fromkeys(b for b in _holding_known(profile)
                               if b[1] not in counts and not _covers(b, addr)))


def separators_for(profile, block: Block) -> tuple[Block, ...]:
    """Znane bloki o długości innej niż `block` i z nim rozłączne — rozdzielenie dwóch odczytów
    tej samej długości w cyklu odpytywania (kolejność prób jak `separator_blocks`)."""
    b0, b1 = block[0], block[0] + block[1]
    return tuple(dict.fromkeys(b for b in _holding_known(profile)
                               if b[1] != block[1] and (b[0] + b[1] <= b0 or b[0] >= b1)))


def _unproven_single(profile, block: Block) -> bool:
    """Pojedynczy rejestr zapisu, którego żaden znany blok nie obejmuje — może być niedozwolony na
    tym modelu (GW8KN-ET: 47760 → wyjątek 2)."""
    if block[1] != 1:
        return False
    write = profile.raw.get("write", {})
    addrs = {s["addr"] for k, s in write.items() if isinstance(s, dict) and isinstance(s.get("addr"), int)}
    return block[0] in addrs and not any(_covers(b, block[0]) for b in profile.modbus.verify_blocks)


def poll_order(profile, plan: Iterable[Block]) -> list[Block]:
    """Kolejność bloków cyklu odpytywania na łączu bez korelacji odpowiedzi: sąsiednie bloki o różnej
    długości (powtórzona odpowiedź poprzedniego bloku nie pasuje do następnego), a pojedyncze rejestry
    zapisu bez znanego bloku — na końcu (ich wyjątek 2 nie trafia w środek cyklu; wyjątek w następnym
    bloku i tak nie jest werdyktem — `RegisterClient`). Pierwszy blok nie jest pojedynczy (poprzedni
    cykl kończy się pojedynczym). Wybór: wśród bloków o długości innej niż poprzedni ten, którego
    długość jest najczęstsza wśród pozostałych (inaczej zostają na końcu dwa równe); remis — kolejność planu."""
    plan = list(plan)
    tail = [b for b in plan if _unproven_single(profile, b)]
    rest = [b for b in plan if b not in tail]
    out: list[Block] = []
    prev: int | None = 1
    while rest:
        freq: dict[int, int] = {}
        for b in rest:
            freq[b[1]] = freq.get(b[1], 0) + 1
        allowed = [b for b in rest if b[1] != prev] or rest
        pick = max(allowed, key=lambda b: (freq[b[1]], -rest.index(b)))
        rest.remove(pick)
        out.append(pick)
        prev = pick[1]
    return out + tail


def prev_read_count(transport, fallback: int | None) -> int | None:
    """Długość ostatniego żądania odczytu na łączu (`last_read_count`); bez niej — `fallback`."""
    n = getattr(transport, "last_read_count", None)
    return n if isinstance(n, int) and not isinstance(n, bool) else fallback
