"""Zapis cyklu z trybem i nastawą mocy jako JEDNĄ grupą — warstwa nad `run_writes`.

`run_writes` (parytet z urządzeniem brzegowym) pisze w kolejności profilu: moc idzie
przed trybem, a tryb jest wstrzymany, gdy parametr po mocy się nie zapisał. Na
falowniku zostaje wtedy stary tryb z nową mocą — standby honoruje ją jako nastawę
ładowania z sieci. Ten moduł tego nie dopuszcza:

1. najpierw klucze spoza grupy (warunki trybu), zwykłym `run_writes`;
2. jeśli któryś się nie zapisał albo falownik go nie obsługuje — grupa czeka cały cykl;
3. grupa w bezpiecznej kolejności: „mniejsza moc wygrywa stan przejściowy" —
   spadek mocy: moc, potem tryb; wzrost mocy (albo moc nieznana): tryb, potem moc;
4. gdy drugi członek grupy się nie zapisał po udanym pierwszym, pierwszy wraca od razu
   do poprzedniej wartości z urządzenia, a raport niesie błąd. ERROR jest jednak
   niejednoznaczny (zgubione potwierdzenie, przekroczony czas — zapis mógł dojść):
   wtedy cofamy tylko klucz z `ambiguous_safe`, czyli taki, którego powrót z KAŻDĄ
   wartością drugiego członka nie daje postoju z mocą > 0 ani ładowania ponad
   poprzednią moc. Inaczej pierwszy zostaje (`restore_held`) — przy znanej i aktualnej
   poprzedniej mocy kolejność grupy daje wtedy pomniejszoną wersję zamówionej komendy;
   przy mocy nieznanej (tryb pierwszy) nowy tryb pracuje na nieznanej, starej nastawie —
   świadomy kompromis. Następny cykl odczytuje urządzenie i poprawia brakujący klucz.

Po nieudanym cofnięciu — przy znanej poprzedniej mocy — zostaje najwyżej pomniejszona
wersja zamówionej komendy (nowy tryb na mniejszej, starej mocy albo stary tryb na
mniejszej, nowej mocy). Klucze z niejednoznacznym wynikiem raport wymienia w `ambiguous`.
Kolejność grupy układa cykl (`order_group`), bo on zna poprzednią moc; wykonawca
pisze grupę w kolejności z listy. Raport (`GroupReport`) idzie do `cycle.commit`, który
liczy rundę zapis→cofnięcie w I-6/I-8 i włącza odwrót grupy.

Dla wykonawcy HA: KAŻDY zapis trybu i mocy — cykl, ale też powrót do stanu bazowego
po utracie zgody (`_restore`) — idzie przez `run_group_writes`/`async_run_group_writes`
z grupą ułożoną przez `order_group(power_first(...))`, nigdy wprost przez `run_writes`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Collection, Generator, Iterable, Mapping, Sequence, TypeVar

from ..write_sequence import ERROR, OnException, WriteReport, async_run_writes, run_writes

GROUP_KEYS = ("mode", "power_w")

W = TypeVar("W")
_Plan = Generator[list, WriteReport, "GroupReport"]


@dataclass
class GroupReport(WriteReport):
    # klucze zapisane, a potem cofnięte do poprzedniej wartości (na urządzeniu stan sprzed)
    restored: list[str] = field(default_factory=list)
    # klucze, których cofnięcie się nie udało albo nie było do czego wrócić
    restore_failed: list[str] = field(default_factory=list)
    # klucze zostawione bez cofnięcia, bo błąd drugiego członka był niejednoznaczny
    restore_held: list[str] = field(default_factory=list)
    # klucze nieudane (także nieudane cofnięcie) z wynikiem ERROR — zapis mógł dojść
    ambiguous: list[str] = field(default_factory=list)
    group_skipped: bool = False

    @property
    def error(self) -> bool:
        """Grupa rozjechała się w trakcie cyklu (z cofnięciem albo bez)."""
        return bool(self.restored or self.restore_failed or self.restore_held)


def power_first(new_power: float, previous_power: float | None) -> bool:
    """Czy moc idzie przed trybem: tak przy spadku mocy; przy nieznanej — tylko dla zera."""
    if previous_power is None:
        return new_power <= 0.0
    return new_power <= previous_power


def order_group(writes: Iterable[W], *, power_first: bool) -> list[W]:
    """Klucze spoza grupy (w kolejności wejścia), potem grupa w bezpiecznej kolejności."""
    items = list(writes)
    rest = [w for w in items if w.key not in GROUP_KEYS]
    group = [w for w in items if w.key in GROUP_KEYS]
    group.sort(key=lambda w: (w.key == "mode") if power_first else (w.key != "mode"))
    return rest + group


def _merge(rep: GroupReport, part: WriteReport) -> None:
    rep.written += [k for k in part.written if k not in rep.written]
    rep.unsupported += part.unsupported
    rep.failed += part.failed
    rep.mode_held = rep.mode_held or part.mode_held
    rep.errors.update(part.errors)


def _hold(rep: GroupReport, held: Sequence) -> None:
    rep.group_skipped = True
    if any(w.key == "mode" for w in held):
        rep.mode_held = True


def _plan(writes: Sequence, restore: Mapping[str, object], ambiguous_safe: Collection[str],
          outcomes: Mapping[str, str]) -> _Plan:
    """Kroki zapisu jako generator: wysyła partie do `run_writes`, dostaje ich raporty."""
    rep = GroupReport()
    rest = [w for w in writes if w.key not in GROUP_KEYS]
    group = [w for w in writes if w.key in GROUP_KEYS]
    if rest:
        _merge(rep, (yield rest))
    if not group:
        return rep
    if rep.failed or rep.unsupported:
        _hold(rep, group)                 # warunek trybu nie doszedł — grupa czeka
        return rep
    first = yield group[:1]
    _merge(rep, first)
    if len(group) == 1:
        return rep
    if group[0].key not in first.written:
        _hold(rep, group[1:])             # pierwszy nie poszedł — drugi też nie
        return rep
    second = yield group[1:]
    _merge(rep, second)
    if group[1].key in second.written:
        return rep
    key = group[0].key
    if outcomes.get(group[1].key) == ERROR and key not in ambiguous_safe:
        rep.restore_held.append(key)      # drugi mógł dojść — cofnięcie mogłoby być groźne
        return rep
    undo = restore.get(key)
    if undo is None:
        rep.restore_failed.append(key)
        return rep
    back = yield [undo]
    if key in back.written:
        rep.written.remove(key)           # na urządzeniu znów stan sprzed — nic nie zapisano
        rep.restored.append(key)
    else:
        rep.restore_failed.append(key)
        rep.errors.update({f"{k}:restore": v for k, v in back.errors.items()})
    return rep


def _with_ambiguous(rep: GroupReport, outcomes: Mapping[str, str]) -> GroupReport:
    rep.ambiguous = [k for k in dict.fromkeys([*rep.failed, *rep.restore_failed])
                     if outcomes.get(k) == ERROR]
    return rep


def run_group_writes(writes: Sequence[W], write: Callable[[W], str], *,
                     restore: Mapping[str, W] | None = None, ambiguous_safe: Collection[str] = (),
                     on_exception: OnException | None = None) -> GroupReport:
    """Zapis grupowy; `restore` = zapisy przywracające poprzednią wartość członka grupy,
    `ambiguous_safe` = klucze, które wolno cofnąć także po niejednoznacznym ERROR."""
    outcomes: dict[str, str] = {}

    def noted(w: W) -> str:
        outcomes[w.key] = ERROR               # wyjątek pisarza = ERROR (jak w `run_writes`)
        outcomes[w.key] = write(w)
        return outcomes[w.key]

    plan = _plan(writes, restore or {}, ambiguous_safe, outcomes)
    try:
        batch = next(plan)
        while True:
            batch = plan.send(run_writes(batch, noted, on_exception=on_exception))
    except StopIteration as done:
        return _with_ambiguous(done.value, outcomes)


async def async_run_group_writes(writes: Sequence[W], write: Callable[[W], Awaitable[str]], *,
                                 restore: Mapping[str, W] | None = None,
                                 ambiguous_safe: Collection[str] = (),
                                 on_exception: OnException | None = None) -> GroupReport:
    """Bliźniak `run_group_writes` dla pisarza asynchronicznego — te same kroki."""
    outcomes: dict[str, str] = {}

    async def noted(w: W) -> str:
        outcomes[w.key] = ERROR
        outcomes[w.key] = await write(w)
        return outcomes[w.key]

    plan = _plan(writes, restore or {}, ambiguous_safe, outcomes)
    try:
        batch = next(plan)
        while True:
            batch = plan.send(await async_run_writes(batch, noted, on_exception=on_exception))
    except StopIteration as done:
        return _with_ambiguous(done.value, outcomes)
