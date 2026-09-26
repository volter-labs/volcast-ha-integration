"""Wykonanie zapisów w kolejności profilu.

Parametry są warunkami, w jakich tryb ma zadziałać, więc idą PRZED trybem. Gdy
parametr się nie zapisał (błąd, odmowa), tryb jest WSTRZYMANY — nowy tryb na
starych nastawach wykonałby komendę, której nikt nie zamówił (zmierzone: standby
ze starym Xset ładował z sieci). „Nieobsługiwany" (wyjątek Modbus 2) nie jest
błędem: falownik definitywnie tego nie umie, nie ma na co czekać.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence, TypeVar

OK = "ok"
UNSUPPORTED = "unsupported"
DENIED = "denied"
ERROR = "error"


class _Keyed(Protocol):
    key: str


W = TypeVar("W", bound=_Keyed)


@dataclass
class WriteReport:
    written: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    mode_held: bool = False


def run_writes(writes: Sequence[W], write: Callable[[W], str]) -> WriteReport:
    rep = WriteReport()
    param_failed = False
    for w in writes:
        if w.key == "mode" and param_failed:
            rep.mode_held = True
            continue
        outcome = write(w)
        if outcome == OK:
            if w.key not in rep.written:
                rep.written.append(w.key)
        elif outcome == UNSUPPORTED:
            rep.unsupported.append(w.key)
        else:
            rep.failed.append(w.key)
            if w.key != "mode":
                param_failed = True
    return rep
