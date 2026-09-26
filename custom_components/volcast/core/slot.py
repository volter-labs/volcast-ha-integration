"""Plan z warstwy sterowania — model slotu i parser FAIL-CLOSED (bez importów HA).

Jeden zły slot unieważnia cały plan: częściowo przyjęty harmonogram jest
w energetyce groźniejszy niż odrzucony. Kierunek i pola opisowe muszą się zgadzać.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class Action(str, Enum):
    CHARGE = "charge"
    DISCHARGE = "discharge"
    SELF_CONSUME = "self_consume"
    IDLE = "idle"
    # Bateria stoi, ale nadwyżka PV idzie do sieci (fizyka IDLE, inny eksport).
    HOLD = "hold"


class InvalidSchedule(ValueError):
    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(f"{field_name}: {message}")
        self.field = field_name
        self.message = message


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    action: Action
    charge_source: str | None = None       # "pv" | "grid"
    discharge_purpose: str | None = None   # "self" | "sell"
    power_w: float | None = None
    soc_target: float | None = None
    price_pln_kwh: float | None = None
    export_allowed: bool = True
    export_limit_w: float | None = None

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def covers(self, moment: datetime) -> bool:
        # moment musi mieć strefę — naive rzuci TypeError przy porównaniu.
        return self.start <= moment < self.end


@dataclass(frozen=True)
class Fallback:
    """Zachowanie po wygaśnięciu planu. O jego kształcie decyduje chmura, nie kod."""

    action: Action = Action.SELF_CONSUME
    soc_reserve: float = 20.0

    def as_slot(self, start: datetime, end: datetime) -> Slot:
        # Fallback nie ma ceny, więc eksport jest ślepy — zablokowany (jak urządzenie brzegowe).
        return Slot(start=start, end=end, action=self.action,
                    soc_target=self.soc_reserve, export_allowed=False)


@dataclass(frozen=True)
class Schedule:
    schedule_id: str
    generated_at: datetime | None
    slots: tuple[Slot, ...]
    fallback: Fallback
    control_enabled: bool

    def slot_for(self, moment: datetime) -> Slot | None:
        for s in self.slots:
            if s.covers(moment):
                return s
        return None

    def effective_slot(self, moment: datetime) -> tuple[Slot, bool]:
        """Slot do wykonania i flaga „to fallback". Nigdy „zostaw ostatnią nastawę".

        `moment` musi mieć strefę — naive rzuci TypeError przy porównaniu.
        """
        s = self.slot_for(moment)
        if s is not None:
            return s, False
        return self.fallback.as_slot(moment, moment + timedelta(hours=1)), True


_SOURCES = ("pv", "grid")
_PURPOSES = ("self", "sell")


# Pełny znacznik czasu ISO-8601 ze strefą — bez niej godzina jest niejednoznaczna.
# Odrzuca m.in. formę bez strefy, samą datę, zapis zbity i separator spacją.
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def _dt(raw: Any, name: str) -> datetime:
    if not isinstance(raw, str) or not _TIMESTAMP_RE.match(raw):
        raise InvalidSchedule(name, f"oczekiwano czasu ISO ze strefą, jest {raw!r}")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as err:
        raise InvalidSchedule(name, f"zły czas {raw!r}") from err


def _num(raw: dict[str, Any], key: str, where: str) -> float | None:
    v = raw.get(key)
    if v is None:
        return None
    # bool to podtyp int — True przeszłoby jako 1.0 (cicha podmiana intencji).
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise InvalidSchedule(f"{where}.{key}", f"musi być liczbą, jest {v!r}")
    try:
        f = float(v)
    except OverflowError as err:
        raise InvalidSchedule(f"{where}.{key}", "liczba poza zakresem") from err
    if not math.isfinite(f):
        raise InvalidSchedule(f"{where}.{key}", "NaN/inf")
    return f


def _flag(raw: dict[str, Any], key: str, default: bool, where: str) -> bool:
    v = raw.get(key)
    if v is None:
        return default
    if not isinstance(v, bool):
        # bool("false") == True — odwróciłoby zakaz eksportu w zgodę.
        raise InvalidSchedule(f"{where}.{key}", f"musi być true/false, jest {v!r}")
    return v


def _choice(raw: dict[str, Any], key: str, allowed: tuple[str, ...], where: str) -> str | None:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or v not in allowed:
        raise InvalidSchedule(f"{where}.{key}", f"{v!r} spoza {list(allowed)}")
    return v


def _action(raw: dict[str, Any], where: str) -> Action:
    v = raw.get("mode")
    try:
        return Action(v)
    except ValueError as err:
        raise InvalidSchedule(f"{where}.mode", f"nieznany tryb {v!r}") from err


def _parse_slot(raw: Any, idx: int) -> Slot:
    where = f"slots[{idx}]"
    if not isinstance(raw, dict):
        raise InvalidSchedule(where, "slot musi być obiektem")
    start, end = _dt(raw.get("from"), f"{where}.from"), _dt(raw.get("to"), f"{where}.to")
    if end <= start:
        raise InvalidSchedule(where, "slot kończy się nie później niż zaczyna")
    action = _action(raw, where)
    src = _choice(raw, "charge_source", _SOURCES, where)
    purp = _choice(raw, "discharge_purpose", _PURPOSES, where)
    # Tryb i pola opisowe muszą opisywać JEDEN kierunek.
    if action in (Action.IDLE, Action.HOLD) and (src or purp):
        raise InvalidSchedule(f"{where}.mode", f"mode={action.value!r} nie może nieść kierunku")
    if action is Action.CHARGE and purp:
        raise InvalidSchedule(f"{where}.discharge_purpose", "charge z discharge_purpose")
    if action is Action.DISCHARGE and src:
        raise InvalidSchedule(f"{where}.charge_source", "discharge z charge_source")
    if src and purp:
        raise InvalidSchedule(f"{where}.charge_source", "charge_source i discharge_purpose naraz")
    limit = _num(raw, "export_limit_w", where)
    if limit is not None and limit < 0:
        raise InvalidSchedule(f"{where}.export_limit_w", "nie może być ujemny")
    return Slot(start=start, end=end, action=action, charge_source=src, discharge_purpose=purp,
                power_w=_num(raw, "power_w", where), soc_target=_num(raw, "soc_target", where),
                price_pln_kwh=_num(raw, "price_pln_kwh", where),
                export_allowed=_flag(raw, "export_allowed", True, where), export_limit_w=limit)


def parse_schedule(raw: Any) -> Schedule:
    if not isinstance(raw, dict):
        raise InvalidSchedule("schedule", "dokument musi być obiektem")
    if not isinstance(raw.get("slots"), list):
        # Brak `slots` to błąd kształtu, nie „pusty plan".
        raise InvalidSchedule("slots", "brak listy slotów")
    slots = [_parse_slot(s, i) for i, s in enumerate(raw["slots"])]
    slots.sort(key=lambda s: s.start)          # sort stabilny
    for i in range(1, len(slots)):
        if slots[i].start < slots[i - 1].end:
            # Sloty zachodzące na siebie to niejednoznaczny plan — całość odrzucona.
            raise InvalidSchedule(f"slots[{i}]", "sloty zachodzą na siebie")
    fb_raw = raw.get("fallback")
    if fb_raw is None:
        fb_raw = {}
    elif not isinstance(fb_raw, dict):
        # Fallback to ostatnia linia obrony — zły kształt nie może cicho zniknąć.
        raise InvalidSchedule("fallback", "musi być obiektem")
    reserve = _num(fb_raw, "soc_reserve", "fallback")
    fallback = Fallback(action=_action(fb_raw, "fallback") if "mode" in fb_raw else Action.SELF_CONSUME,
                        soc_reserve=20.0 if reserve is None else reserve)
    sid = raw.get("schedule_id")
    if sid is None:
        sid = ""
    elif not isinstance(sid, str):
        raise InvalidSchedule("schedule_id", f"musi być tekstem, jest {sid!r}")
    gen = raw.get("generated_at")
    if gen is None or gen == "":
        generated_at = None
    elif not isinstance(gen, str):
        raise InvalidSchedule("generated_at", f"musi być tekstem, jest {gen!r}")
    else:
        generated_at = _dt(gen, "generated_at")
    return Schedule(
        schedule_id=sid,
        generated_at=generated_at,
        slots=tuple(slots),
        fallback=fallback,
        # Brak pola = False. Starsza chmura nie może dać prawa sterowania.
        control_enabled=raw.get("control_enabled") is True,
    )


def slot_direction(slot: Slot) -> Action | None:
    """Kierunek przepływu baterii; może siedzieć WYŁĄCZNIE w polach opisowych."""
    if slot.action in (Action.CHARGE, Action.DISCHARGE):
        return slot.action
    if slot.discharge_purpose is not None:
        return Action.DISCHARGE
    if slot.charge_source is not None:
        return Action.CHARGE
    return None


def effective_action(slot: Slot) -> Action:
    """Intencja dla guardów — nigdy „może nic"."""
    d = slot_direction(slot)
    if d is not None:
        return d
    if slot.action in (Action.IDLE, Action.HOLD):
        return slot.action
    return Action.SELF_CONSUME
