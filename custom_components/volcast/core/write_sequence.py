"""Wykonanie zapisów w kolejności profilu.

Parametry są warunkami, w jakich tryb ma zadziałać, więc idą PRZED trybem. Gdy
parametr się nie zapisał (błąd, odmowa), tryb jest WSTRZYMANY — nowy tryb na
starych nastawach wykonałby komendę, której nikt nie zamówił (zmierzone: standby
ze starym Xset ładował z sieci). „Nieobsługiwany" (wyjątek Modbus 2) nie jest
błędem: falownik definitywnie tego nie umie, nie ma na co czekać.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, Sequence, TypeVar

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
    # klucz → nazwa klasy wyjątku z callbacku; sama klasa, bez treści komunikatu
    # (treść transportu bywa z adresem hosta, a raport trafia do diagnostyki)
    errors: dict[str, str] = field(default_factory=dict)


OnException = Callable[[str, BaseException], None]


def run_writes(writes: Sequence[W], write: Callable[[W], str], *,
               on_exception: OnException | None = None) -> WriteReport:
    """`write` powinien zwrócić jeden z OK/UNSUPPORTED/DENIED/ERROR i nie rzucać.

    Wyjątek z callbacku (np. transportu) liczy się jak ERROR dla tego klucza —
    pętla nie może przerwać się w połowie i zgubić raport o tym, co już zaszło.
    `on_exception(klucz, wyjątek)` pozwala wołającemu zalogować przyczynę.
    """
    rep = WriteReport()
    param_failed = False
    for w in writes:
        if w.key == "mode" and param_failed:
            rep.mode_held = True
            continue
        try:
            outcome = write(w)
        except Exception as err:  # noqa: BLE001 — każdy wyjątek to ERROR tego klucza
            outcome = _swallow(rep, w.key, err, on_exception)
        param_failed = _account(rep, w.key, outcome, param_failed)
    return rep


async def async_run_writes(writes: Sequence[W], write: Callable[[W], Awaitable[str]], *,
                           on_exception: OnException | None = None) -> WriteReport:
    """Bliźniak `run_writes` dla pisarza asynchronicznego — ta sama semantyka, krok w krok.

    Anulowanie (`CancelledError`, poza `Exception`) nie jest błędem zapisu i przechodzi dalej.
    """
    rep = WriteReport()
    param_failed = False
    for w in writes:
        if w.key == "mode" and param_failed:
            rep.mode_held = True
            continue
        try:
            outcome = await write(w)
        except Exception as err:  # noqa: BLE001 — każdy wyjątek to ERROR tego klucza
            outcome = _swallow(rep, w.key, err, on_exception)
        param_failed = _account(rep, w.key, outcome, param_failed)
    return rep


def _swallow(rep: WriteReport, key: str, err: Exception, on_exception: OnException | None) -> str:
    """Wyjątek pisarza → ERROR klucza; w raporcie sama klasa, bez treści (może mieć adres)."""
    rep.errors[key] = type(err).__name__
    if on_exception is not None:
        try:
            on_exception(key, err)
        except Exception:  # noqa: BLE001 — wadliwy callback logujący nie przerywa sekwencji
            pass
    return ERROR


def _account(rep: WriteReport, key: str, outcome: str, param_failed: bool) -> bool:
    """Wpisuje wynik do raportu; zwraca, czy od teraz tryb ma być wstrzymany."""
    if outcome == OK:
        if key not in rep.written:
            rep.written.append(key)
    elif outcome == UNSUPPORTED:
        rep.unsupported.append(key)
    else:
        rep.failed.append(key)
        if key != "mode":
            return True
    return param_failed
