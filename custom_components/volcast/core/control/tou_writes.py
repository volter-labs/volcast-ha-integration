"""Sekwencja zapisów okien czasowych (programy TOU) i powrót do programów właściciela.

Kolejność przepisania: (1) włącznik harmonogramu OFF — zawsze, gdy zmieniają się programy
(pisarz sprawdza stan świeżym odczytem i nie wysyła ramki, gdy harmonogram już jest wyłączony;
odczyt z cyklu mógł być sprzed naszego ostatniego zapisu) — wyłączony harmonogram to zwykła
samokonsumpcja, więc każdy stan pośredni jest bezpieczny, a na urządzeniu nigdy nie działa
program z polami pół starymi, pół nowymi; (2) programy 1..N, pola wg `tou.field_order`;
(3) włącznik ON na końcu, tylko gdy nic nie zostało wstrzymane.

Pierwsze pole programu `i`, które nie poszło (ERROR, DENIED, UNSUPPORTED albo wstrzymane
z góry przez interwał/budżet — `pre_held`), wstrzymuje resztę pól `i`, programy `> i` i
włącznik ON. Wstrzymanie z powodu porażki po wyłączeniu włącznika w tym cyklu =
`restore_needed` (wykonawca od razu przywraca programy właściciela). Wstrzymanie wyłącznie
przez interwał/budżet powrotu nie wymaga: włącznik OFF to stan bezpieczny, następny cykl
dokończy. Nieudany zapis OFF = nic dalej nie idzie (programy nietknięte).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Collection, Generator, Mapping, Sequence

from ..engines.time_window import baseline_programs, program_diff
from ..params import Params, TouProgram
from ..registers import RegisterWrite, encode_tou_enable, encode_writes
from ..write_sequence import ERROR, OK, OK_ADJUSTED, OnException, WriteReport, _account, _swallow

ENABLE = "tou_enable"
# surowe słowo włącznika właściciela (powrót) — pisarz nie składa bitów, porównuje całe słowo
TOU_WORD = "tou_word"


@dataclass
class TouReport(WriteReport):
    held: list[str] = field(default_factory=list)
    restore_needed: bool = False
    # klucze z wynikiem ERROR (zapis mógł dojść) — dla pamięci niepewności
    ambiguous: list[str] = field(default_factory=list)
    enable_written: bool = False
    # każda wysłana ramka po kolei (włącznik dwa razy, gdy OFF i ON) — do liczenia budżetu
    frames: list[str] = field(default_factory=list)


_Plan = Generator[RegisterWrite, str, TouReport]


def _plan(writes: Sequence[RegisterWrite], pre_held: Collection[str]) -> _Plan:
    """Kroki sekwencji jako generator: oddaje zapis, dostaje jego wynik."""
    rep = TouReport()
    disabled = False
    failed = False
    holding = False
    for pos, w in enumerate(writes):
        if pos == 0 and w.key == ENABLE and len(writes) > 1:
            outcome = yield w
            _note(rep, w.key, outcome)
            if outcome not in (OK, OK_ADJUSTED):
                rep.held = [x.key for x in writes[1:]]
                return rep
            disabled = True
            continue
        if holding:
            rep.held.append(w.key)
            continue
        if w.key in pre_held:
            rep.held.append(w.key)
            holding = True
            continue
        outcome = yield w
        _note(rep, w.key, outcome)
        if outcome in (OK, OK_ADJUSTED):
            if w.key == ENABLE:
                rep.enable_written = True
            continue
        holding = True
        failed = True
    rep.restore_needed = disabled and failed
    return rep


def _note(rep: TouReport, key: str, outcome: str) -> None:
    rep.frames.append(key)
    _account(rep, key, outcome, False)
    if outcome == ERROR and key not in rep.ambiguous:
        rep.ambiguous.append(key)


def run_tou_writes(writes: Sequence[RegisterWrite], write: Callable[[RegisterWrite], str], *,
                   pre_held: Collection[str] = (), on_exception: OnException | None = None) -> TouReport:
    """`write` zwraca OK/OK_ADJUSTED/UNSUPPORTED/DENIED/ERROR; wyjątek = ERROR tego klucza."""
    plan = _plan(writes, set(pre_held))
    errors = WriteReport()
    try:
        w = next(plan)
        while True:
            try:
                outcome = write(w)
            except Exception as err:  # noqa: BLE001 — wyjątek pisarza = ERROR tego klucza
                outcome = _swallow(errors, w.key, err, on_exception)
            w = plan.send(outcome)
    except StopIteration as done:
        done.value.errors.update(errors.errors)
        return done.value


async def async_run_tou_writes(writes: Sequence[RegisterWrite], write: Callable[[RegisterWrite], Awaitable[str]], *,
                               pre_held: Collection[str] = (), on_exception: OnException | None = None) -> TouReport:
    """Bliźniak `run_tou_writes` dla pisarza asynchronicznego — te same kroki."""
    plan = _plan(writes, set(pre_held))
    errors = WriteReport()
    try:
        w = next(plan)
        while True:
            try:
                outcome = await write(w)
            except Exception as err:  # noqa: BLE001
                outcome = _swallow(errors, w.key, err, on_exception)
            w = plan.send(outcome)
    except StopIteration as done:
        done.value.errors.update(errors.errors)
        return done.value


# ── migawka właściciela i powrót ──────────────────────────────────────────


def _word(reading, addr: int) -> int | None:
    try:
        return reading.image.words(addr, 1)[0]
    except Exception:  # noqa: BLE001 — brak rejestru w obrazie
        return None


def tou_snapshot(reading, profile) -> dict | None:
    """Programy i włącznik właściciela: `{"programs": [[start_min, power_w, soc, grid_word]…],
    "tou_word": int}` — pole ładowania z sieci i włącznik jako SUROWE słowa (powrót odtwarza
    bity właściciela, także te, których nie sterujemy). Brak odczytu → None."""
    write = profile.raw["write"]
    tp, en = write.get("tou_program"), write.get("tou_enable")
    if reading.programs is None or tp is None or en is None:
        return None
    tou_word = _word(reading, en["addr"])
    grid = [_word(reading, tp["grid_charge"]["addr"] + i) for i in range(len(reading.programs))]
    if tou_word is None or any(g is None for g in grid):
        return None
    return {"programs": [[p.start_min, int(p.power_w), int(p.soc), g] for p, g in zip(reading.programs, grid)],
            "tou_word": tou_word}


SNAPSHOT_PROGRAMS = 6
_SNAP_RANGES = ((0, 1439), (0, 65535), (0, 100), (0, 65535))


def _uint(v, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def validate_tou_snapshot(raw: Any, count: int = SNAPSHOT_PROGRAMS) -> dict | None:
    """Migawka programów właściciela z magazynu: dokładnie `count` programów, liczby całkowite
    w zakresach słów; cokolwiek innego → None (bez migawki powrót idzie do programów bazowych)."""
    if not isinstance(raw, dict) or not _uint(raw.get("tou_word"), 0, 0xFFFF):
        return None
    programs = raw.get("programs")
    if not isinstance(programs, list) or len(programs) != count:
        return None
    out = []
    for p in programs:
        if not isinstance(p, list) or len(p) != 4 or not all(_uint(v, lo, hi) for v, (lo, hi) in zip(p, _SNAP_RANGES)):
            return None
        out.append(list(p))
    return {"programs": out, "tou_word": raw["tou_word"]}


def _snapshot_programs(profile, snapshot: Mapping[str, Any]) -> tuple[TouProgram, ...]:
    bit = 1 << profile.raw["write"]["tou_program"]["grid_charge"]["bit"]
    return tuple(TouProgram(start_min=int(s), power_w=float(p), soc=float(c), grid_charge=bool(int(g) & bit))
                 for s, p, c, g in snapshot["programs"])


def tou_restore_writes(profile, snapshot: Mapping[str, Any] | None, reading, *, soc_reserve: float,
                       rated_power_w: float) -> list[RegisterWrite]:
    """Powrót do programów właściciela: włącznik OFF (gdy zmieniają się programy — pisarz nie
    wysyła ramki, gdy już wyłączony) → różnice programów → SUROWE słowo włącznika właściciela na
    końcu (`tou_word`: jego bit włącznika i jego dni, dokładnie). Bez migawki: programy bazowe
    (samokonsumpcja) i włącznik OFF."""
    en = profile.raw["write"]["tou_enable"]
    ebit = 1 << en["enable_bit"]
    word = _word(reading, en["addr"])
    if word is None or reading.programs is None:
        raise ValueError("powrót TOU wymaga odczytu programów i włącznika")
    if snapshot is not None:
        target = _snapshot_programs(profile, snapshot)
        owner_word = int(snapshot["tou_word"])
    else:
        target = baseline_programs(profile, soc_reserve, rated_power_w)
        owner_word = None
    diff = program_diff(target, reading.programs)
    out: list[RegisterWrite] = []
    after = word
    if diff or (owner_word is None and word & ebit):
        off = encode_tou_enable(False, word, profile)
        out.append(off)
        after = off.value
    out.extend(encode_writes(Params(tou=target), profile, keys=diff, current=reading.image))
    if owner_word is not None and owner_word != after:
        out.append(RegisterWrite(TOU_WORD, en["addr"], owner_word))
    return out
