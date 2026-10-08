"""Odczyt jednego rejestru na łączu, którego odpowiedź nie wskazuje żądania (GoodWe UDP).

Odpowiedź FC 3 w ramce RTU nie niesie adresu, a moduł Wi-Fi GoodWe bywa, że odpowiada na żądanie
POPRZEDNIĄ odpowiedzią (identyczna ramka, kilka razy z rzędu). Transport przyjmuje każdą ramkę
o zgodnej jednostce, funkcji i długości — nieaktualna odpowiedź tej samej długości przechodzi.
Dlatego pisarz i sonda czytają rejestr tak, żeby kolejne żądania różniły się długością odpowiedzi:

* widoki rejestru (`key_views`): bloki z `modbus.verify_blocks` profilu, które go obejmują (bloki
  czytane przez urządzenie referencyjne — dozwolone), a na końcu sam rejestr;
* `pick_view` wybiera pierwszy widok o długości innej niż ostatnie żądanie odczytu na łączu
  (`last_read_count` transportu); gdy takiego nie ma (rejestr tylko pojedynczo), wołający wysyła
  najpierw blok rozdzielający (`separator_block`): znany blok o długości różnej od KAŻDEGO widoku
  rejestru i nieobejmujący go — po nim nieaktualna odpowiedź ma inną długość niż odczyt rejestru.

Wyjątek Modbus nie ma długości: wyjątek 2 przyjmuje się jako ostateczny („rejestru nie ma”) dopiero,
gdy powtórzy się w odczycie rejestru po bloku rozdzielającym z poprawną odpowiedzią (wtedy poprzednia
odpowiedź modułu nie jest wyjątkiem). Wyjątek 2 na bloku to nie werdykt o rejestrze — tylko o bloku.

Łącza z korelacją odpowiedzi (Modbus TCP — identyfikator transakcji, V5 — numer sekwencji) i łącze
strumieniowe RTU (kanał resetowany po każdym przekroczeniu czasu) czytają po staremu.
"""
from __future__ import annotations

from typing import Iterable

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


def separator_block(profile, addr: int) -> Block | None:
    """Znany blok (bloki weryfikacyjne, potem identyfikacja) o długości różnej od każdego widoku
    rejestru i nieobejmujący go; None = profil takiego nie ma."""
    counts = {n for _, n in key_views(profile, addr)}
    for block in (*profile.modbus.verify_blocks, *profile.modbus.identify_reads):
        if block[1] not in counts and not _covers(block, addr):
            return block
    return None


def prev_read_count(transport, fallback: int | None) -> int | None:
    """Długość ostatniego żądania odczytu na łączu (`last_read_count`); bez niej — `fallback`."""
    n = getattr(transport, "last_read_count", None)
    return n if isinstance(n, int) and not isinstance(n, bool) else fallback
