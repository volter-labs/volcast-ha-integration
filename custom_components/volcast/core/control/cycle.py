"""Jeden cykl sterowania: plan → intencja → guardy → dopasowanie → throttling → zapisy.

Czysta decyzja; wywołania usług robi warstwa HA. Każdy wyjątek = cykl bez zapisów
(fail-closed). Bramki (zgoda, przełącznik, wybrany tryb, weryfikacja, pauza) nie
skracają obliczeń — decyzja jest liczona zawsze, żeby „próba na sucho" pokazywała,
co by poszło; zapis wykonuje się tylko przy statusie WRITE.

Tryb i jego nastawa mocy to JEDNA GRUPA: idą razem albo wcale (zmierzone: standby
honoruje Xset jako nastawę ładowania, więc tryb na starej mocy albo moc w starym
trybie ładuje z sieci). Zasady, które trzymają grupę:
* każdy klucz zapisu profilu musi mieć encję — inaczej sterowania nie ma wcale;
* zmieniony parametr, który w tym cyklu nie pójdzie (interwał I-6, jednostka encji
  się zmieniła), wstrzymuje zmianę trybu, a z nią moc;
* zmiana trybu wstrzymana (I-6, I-8) wstrzymuje moc — chyba że falownik już ma tryb
  z planu, wtedy sama moc jest zwykłą korektą nastawy;
* tryb albo moc, których falownik nie obsługuje, wyłączają intencję na całą sesję;
* parametr niedopasowalny do zakresu encji blokuje cały cykl.
Awaria zapisu w trakcie cyklu to już sprawa wykonawcy grupowego (`group_writes`):
cykl układa grupę w bezpiecznej kolejności i podaje zapisy cofające (`restore`).
Po każdym cofnięciu grupa czeka (odwrót: max(min_interval_s, 300 s), podwajany do
1 h, kasowany pełnym udanym zapisem grupy), a obie połowy rundy liczą się w I-6/I-8.
Obcy tryb na urządzeniu (czytelna opcja spoza profilu) = żadnych zapisów i sygnał
przejęcia (`takeover`) — nie nadpisujemy i nie cofamy cudzego trybu.

Zatrzask rezerwy dostaje WYŁĄCZNIE odczyt, który przeszedł sanityzację i świeżość
strażników (I-10, I-9) — strażnicy liczą najpierw próbę z założonym zatrzaskiem;
blokada sprzed I-1 nie zależy od zatrzasku, więc próba rozstrzyga ją bez karmienia
zatrzasku nieużywalnym SoC. Czas `now_mono` pochodzi z zegara monotonicznego.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from ..engines.mode_setpoint import map_slot
from ..entity_map import EntityWrite, entity_value
from ..guard_state import DirectionLimiter, WriteThrottle
from ..guards import GuardContext, GuardResult, apply_guards, temperature_ok
from ..slot import Schedule, effective_action
from ..write_sequence import WriteReport
from .caps import missing_write_keys
from ..params import Params
from .entity_fit import control_writes, fit_params
from .group_writes import order_group, power_first
from .latch import ReserveLatch

WRITE, DRY_RUN, IDLE, BLOCKED, ERROR = "write", "dry_run", "idle", "blocked", "error"

# Tryb i nastawa, która nadaje mu znaczenie — zapisywane razem albo wcale.
_MODE_GROUP = frozenset({"mode", "power_w"})
_DIRECTIONAL = ("charge", "discharge")
_NO_READING = ("unavailable", "unknown", "")
# Kwant rejestru: plan niesie ułamki (625,6 W), falownik pokaże 626 — to nie rozjazd.
_QUANTUM = 1.0
# Odwrót grupy po cofnięciu: start nie krótszy niż 5 min, podwajany do 1 h.
_BACKOFF_MIN_S = 300.0
_BACKOFF_MAX_S = 3600.0


@dataclass
class ControlMemory:
    throttle: WriteThrottle
    limiter: DirectionLimiter
    latch: ReserveLatch
    unsupported: set[str] = field(default_factory=set)
    paused_until: float | None = None
    last_written: dict[str, float | str] = field(default_factory=dict)
    # klucze o nieznanym stanie po niejednoznacznym błędzie zapisu; znikają po udanym
    # zapisie albo odczycie. Dla nich pamięć nie jest poprzednią wartością.
    uncertain: set[str] = field(default_factory=set)
    # odwrót grupy tryb+moc po cofnięciu (zegar monotoniczny); 0 = brak odwrotu
    group_backoff_s: float = 0.0
    group_backoff_until: float | None = None
    backoff_base_s: float = _BACKOFF_MIN_S

    @classmethod
    def for_profile(cls, profile) -> "ControlMemory":
        return cls(WriteThrottle(profile.min_interval_s),
                   DirectionLimiter(max(1, profile.max_direction_changes_per_hour)), ReserveLatch(),
                   backoff_base_s=max(float(profile.min_interval_s), _BACKOFF_MIN_S))

    def in_backoff(self, now_mono: float) -> bool:
        """Czy grupa czeka; zegar cofnięty poza okno = odwrót minął (jak throttling)."""
        until = self.group_backoff_until
        return until is not None and until - self.group_backoff_s <= now_mono < until


@dataclass(frozen=True)
class Gates:
    consent: bool | None
    local_switch: bool
    control_mode: str | None
    verified: bool


@dataclass(frozen=True)
class Telemetry:
    soc: float | None
    soc_age_s: float
    battery_temp_c: float | None
    previous_soc: float | None = None
    previous_soc_gap_s: float | None = None


@dataclass(frozen=True)
class Limits:
    rated_power_w: float
    max_charge_w: float = 0.0
    max_export_w: float = 0.0


@dataclass(frozen=True)
class EntityContext:
    domain: str
    mapped: Mapping[str, str]
    units: Mapping[str, str | None]
    attrs: Mapping[str, Mapping[str, Any]]
    readings: Mapping[str, float | str]


@dataclass
class CycleDecision:
    status: str
    reason: str
    writes: list[EntityWrite] = field(default_factory=list)
    flat: dict[str, float | str] = field(default_factory=dict)
    direction: str | None = None
    intent: str | None = None
    fallback: bool = False
    guard: GuardResult | None = None
    adjusted: tuple[str, ...] = ()
    unmapped: tuple[str, ...] = ()
    dropped_unsupported: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # klucz grupy → zapis przywracający poprzednią wartość (dla wykonawcy grupowego)
    restore: dict[str, EntityWrite] = field(default_factory=dict)
    # te same wartości w postaci `Params.flatten()` i kierunek trybu cofającego (dla I-6/I-8)
    restore_flat: dict[str, float | str] = field(default_factory=dict)
    restore_direction: str | None = None
    # klucze, które wolno cofnąć także po niejednoznacznym ERROR drugiego członka grupy
    restore_ambiguous_safe: tuple[str, ...] = ()
    # urządzenie ma czytelny tryb spoza profilu — zmiana z zewnątrz, nie nadpisujemy
    takeover: bool = False

    def summary(self) -> dict:
        """Mały, JSON-owalny obraz decyzji (telemetria, atrybuty encji) — bez nastaw i notatek strażnika."""
        return {
            "status": self.status, "reason": self.reason, "intent": self.intent,
            "fallback": self.fallback,
            "guard": None if self.guard is None else {"status": self.guard.status,
                                                      "invariant": self.guard.invariant},
            "would_write": [w.key for w in self.writes], "adjusted": list(self.adjusted),
            "unmapped": list(self.unmapped), "dropped_unsupported": list(self.dropped_unsupported),
            "notes": list(self.notes), "takeover": self.takeover,
        }


def same_value(a: float | str, b: float | str) -> bool:
    """Czy odczyt z urządzenia równa się nastawie (tryb po nazwie, liczby z kwantem rejestru)."""
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(float(a) - float(b)) < _QUANTUM


def _gate_reason(g: Gates, memory: ControlMemory, now_mono: float) -> str | None:
    if g.consent is not True:
        return "no_consent"
    if not g.local_switch:
        return "local_off"
    if memory.paused_until is not None and now_mono < memory.paused_until:
        return "paused"
    if not g.verified:
        return "unverified_profile"
    return None


def _device_view(readings: Mapping[str, Any], flat: Mapping[str, float | str], profile,
                 ents: EntityContext) -> dict[str, float | str]:
    """Odczyty kluczy planu w postaci `Params.flatten()`; nieczytelne znikają (brak ≠ rozjazd).

    Wykonawca podaje odczyty już znormalizowane; surowy stan encji (`unavailable`,
    `on`/`off`, opcja wyboru, liczba jako tekst) przechodzi przez ten sam przekład co
    odczyt encji. Liczba tam, gdzie tryb jest nazwą, zostaje — uzgadnianie odrzuci ją
    jako błąd wołającego (cykl bez zapisów). Czytelna opcja spoza profilu to odczyt
    RÓŻNY od każdego trybu (znacznik `?opcja`), nie brak odczytu — nasz tryb wraca.
    """
    out: dict[str, float | str] = {}
    for key in flat:
        if key not in readings:
            continue
        value = readings[key]
        if value is None:
            continue
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            if not math.isfinite(value):
                continue
            value = float(value)
        elif isinstance(value, str) and not (key == "mode" and value in profile.modes):
            try:
                value = entity_value(key, value, profile, ents.domain, unit=ents.units.get(key))
            except (KeyError, ValueError, TypeError):
                value = None
            if value is None:
                if key != "mode" or readings[key] in _NO_READING:
                    continue
                value = "?" + readings[key]
        out[key] = value
    return out


def decide_cycle(*, profile, schedule: Schedule | None, now_utc: datetime, now_mono: float,
                 tele: Telemetry, limits: Limits, ents: EntityContext, gates: Gates,
                 memory: ControlMemory) -> CycleDecision:
    try:
        return _decide(profile, schedule, now_utc, now_mono, tele, limits, ents, gates, memory)
    except Exception as err:  # noqa: BLE001 — każdy błąd decyzji = brak zapisów
        return CycleDecision(ERROR, f"exception:{type(err).__name__}")


def _decide(profile, schedule, now_utc, now_mono, tele, limits, ents, gates, memory) -> CycleDecision:
    if gates.control_mode != "entities":
        return CycleDecision(IDLE, "no_mode_chosen")
    if profile.control_model != "mode_setpoint":
        return CycleDecision(IDLE, "tou_preview_only")
    missing = missing_write_keys(profile, ents.mapped)
    if missing:
        return CycleDecision(IDLE, "missing_entities", unmapped=missing)
    if schedule is None:
        return CycleDecision(IDLE, "no_plan")

    slot, is_fallback = schedule.effective_slot(now_utc)
    mapped_slot = map_slot(slot, profile, limits.rated_power_w)
    common: dict[str, Any] = dict(intent=mapped_slot.intent, fallback=is_fallback)
    # SoC i temperatura to osobne encje: świeży SoC nic nie mówi o temperaturze.
    if "battery_temp_c" in ents.mapped and tele.battery_temp_c is None:
        return CycleDecision(BLOCKED, "temperature_unknown", **common)

    reserve = schedule.fallback.soc_reserve
    ctx = GuardContext(
        soc=tele.soc, soc_age_s=tele.soc_age_s,
        temperature_ok=temperature_ok(tele.battery_temp_c, profile),
        soc_reserve=reserve, action=effective_action(slot), price_pln_kwh=slot.price_pln_kwh,
        max_charge_w=limits.max_charge_w, max_export_w=limits.max_export_w,
        max_state_age_s=profile.max_state_age_s, previous_soc=tele.previous_soc,
        previous_soc_gap_s=tele.previous_soc_gap_s, reserve_engaged=True)
    # Próba z założonym zatrzaskiem: odrzucenie sprzed I-1 (I-10, I-9, I-3, I-7) nie zależy
    # od zatrzasku, a zatrzask nie może zobaczyć odczytu, którego strażnicy nie przyjęli.
    probe = apply_guards(mapped_slot.params, ctx, profile)
    if not probe.write_allowed:
        return CycleDecision(BLOCKED, f"guard:{probe.invariant}", guard=probe, **common)
    engaged = memory.latch.engaged(tele.soc, reserve, now_mono)
    guard = apply_guards(mapped_slot.params, replace(ctx, reserve_engaged=engaged), profile)
    common["guard"] = guard
    if not guard.write_allowed:
        return CycleDecision(BLOCKED, f"guard:{guard.invariant}", **common)

    params, adjusted, unfit = fit_params(guard.params, profile, ents.domain, ents.mapped,
                                         ents.units, ents.attrs)
    if unfit:
        # Klucz, którego encja nie przyjmie, to warunek trybu — tryb nie idzie, nic nie idzie.
        return CycleDecision(BLOCKED, "entity_range_unknown", unmapped=unfit, **common)
    flat = params.flatten()
    unsupported = _unsupported_group(flat, profile, ents, memory)
    if unsupported:
        reason = "mode_unsupported" if unsupported[0].startswith("mode:") else "power_unsupported"
        return CycleDecision(BLOCKED, reason, flat=flat,
                             dropped_unsupported=unsupported, **common)
    device = _device_view(ents.readings, flat, profile, ents)
    # Uzgodnienie z tym, co falownik naprawdę ma (tylko klucze planu, tylko czytelne).
    # Zły typ odczytu rzuca TypeError po drodze (pamięć wcześniejszych kluczy mogła już
    # zniknąć) — cykl kończy się bez zapisów, następny też, więc to bezpieczne.
    memory.throttle.reconcile(device)
    # Odczyt rozstrzyga niepewność — także kluczy spoza bieżącego planu.
    memory.uncertain -= set(_device_view(ents.readings, dict.fromkeys(memory.uncertain), profile, ents))
    mode_now = device.get("mode")
    if isinstance(mode_now, str) and mode_now.startswith("?"):
        # Ktoś inny ustawił tryb, którego profil nie zna — nie walczymy i nie cofamy do
        # naszego; wstrzymanie i powiadomienie należą do reguły przejęcia.
        return CycleDecision(BLOCKED, "foreign_mode", flat=flat, takeover=True, **common)
    # To, co falownik już ma, nie jedzie wcale — także po restarcie, gdy pamięć
    # throttlingu jest pusta (inaczej każdy reload = zapis wszystkich nastaw do NVM).
    settled = {k for k in flat if k in device and same_value(device[k], flat[k])}
    due = memory.throttle.filter(flat, now_mono) - settled
    # Do zmiany na falowniku: to, co pójdzie teraz, i to, co czeka w interwale I-6.
    need = due | (memory.throttle.pending(flat, now_mono) - settled)
    _, runtime_unmapped = control_writes(params, profile, ents.domain, ents.mapped,
                                         keys=None, units=ents.units)
    allowed = due - memory.unsupported - set(runtime_unmapped)

    notes: list[str] = []
    held_by = [k for k in runtime_unmapped if k != "mode"]
    # Zmieniony warunek, który w tym cyklu nie dojdzie („nieobsługiwany" nie dojdzie nigdy).
    unsettled = (need - allowed - memory.unsupported) - {"mode"}
    if held_by or ("mode" in need and unsettled):
        allowed.discard("mode")
        notes.append("mode_held")
    direction = profile.mode_direction(params.mode) if params.mode is not None else None
    prev_mode, prev_power = _previous(device, profile, memory)
    # Tryb przed mocą może skończyć się cofnięciem trybu, czyli dwiema zmianami kierunku.
    # Gdy budżet I-8 mieści jedną, a nie dwie, i poprzednia moc jest znana, grupa idzie
    # bez cofnięcia trybu: przy porażce mocy zostaje nowy tryb na starej, mniejszej mocy
    # (pomniejszona komenda) i jedna zmiana. Zdrowa ścieżka nie traci budżetu.
    round_trip = (direction in _DIRECTIONAL and "power_w" in need and params.power_w is not None
                  and not power_first(params.power_w, prev_power) and prev_mode is not None
                  and profile.mode_direction(prev_mode) in _DIRECTIONAL
                  and profile.mode_direction(prev_mode) != direction)
    no_mode_restore = False
    if "mode" in allowed and direction is not None \
            and not memory.limiter.allows(direction, now_mono, round_trip=round_trip):
        if round_trip and prev_power is not None and memory.limiter.allows(direction, now_mono):
            no_mode_restore = True
        else:
            allowed.discard("mode")
            notes.append("I-8")
    # Odwrót dotyczy tylko zmiany OBU członków (tylko taka może skończyć się cofnięciem);
    # korekta jednego klucza — np. po zapisie, który doszedł mimo błędu — idzie od razu.
    if _MODE_GROUP <= need and _MODE_GROUP & allowed and memory.in_backoff(now_mono):
        allowed -= _MODE_GROUP                  # po cofnięciu grupa odczekuje
        notes.append("group_backoff")
    # Grupa: członek, który musi się zmienić, a nie pójdzie → nie idzie żaden.
    if _MODE_GROUP & (need - allowed) and _MODE_GROUP & allowed:
        allowed -= _MODE_GROUP
        notes.append("group_held")
    writes, unmapped = control_writes(params, profile, ents.domain, ents.mapped,
                                      keys=allowed, units=ents.units)
    writes, restore, restore_flat, ambiguous_safe = _group_layout(writes, params, device, profile,
                                                                  ents, memory)
    if no_mode_restore:
        restore = {k: v for k, v in restore.items() if k != "mode"}
        restore_flat = {k: v for k, v in restore_flat.items() if k != "mode"}
        ambiguous_safe = tuple(k for k in ambiguous_safe if k != "mode")
    reason = _gate_reason(gates, memory, now_mono)
    status = WRITE if reason is None else DRY_RUN
    if status == WRITE and not writes:
        status, reason = IDLE, "nothing_to_write"
    return CycleDecision(
        status, reason or "ok", writes=writes, flat=flat,
        direction=direction if any(w.key == "mode" for w in writes) else None,
        adjusted=adjusted, unmapped=tuple(dict.fromkeys([*held_by, *unmapped])),
        dropped_unsupported=tuple(sorted(memory.unsupported & set(flat))), notes=tuple(notes),
        restore=restore, restore_flat=restore_flat, restore_ambiguous_safe=ambiguous_safe,
        restore_direction=(profile.mode_direction(restore_flat["mode"])
                           if "mode" in restore_flat else None), **common)


def _previous(device: Mapping[str, float | str], profile, memory: ControlMemory
              ) -> tuple[str | None, float | None]:
    """Poprzedni tryb i moc: odczyt, a bez odczytu — pamięć throttlingu (nasz zapis albo
    przyjęty odczyt). `last_written` się nie nadaje — po odczycie, który rozstrzygnął
    niepewność, zostaje przy naszym starym zapisie. Klucz niepewny nie ma poprzedniej wartości."""
    def remembered(key: str):
        return None if key in memory.uncertain else memory.throttle.known(key)

    prev_power = device.get("power_w")
    if not isinstance(prev_power, float):
        prev_power = remembered("power_w")
        prev_power = prev_power if isinstance(prev_power, float) else None
    prev_mode = device.get("mode") if "mode" in device else remembered("mode")
    return (prev_mode if prev_mode in profile.modes else None), prev_power


def _group_layout(writes: list[EntityWrite], params: Params, device: Mapping[str, float | str],
                  profile, ents: EntityContext, memory: ControlMemory
                  ) -> tuple[list[EntityWrite], dict[str, EntityWrite], dict[str, float | str],
                             tuple[str, ...]]:
    """Grupa na końcu, w bezpiecznej kolejności, i zapisy cofające do stanu z urządzenia.

    Poprzednia wartość: odczyt z urządzenia, a bez żadnego odczytu — pamięć throttlingu
    (obcego trybu tu nie ma — cykl zatrzymał się wcześniej). Cofnięcie dopasowane do
    zakresu encji (niedopasowalne = brak cofnięcia); nigdy do trybu postoju przy mocy > 0
    — to odtworzyłoby ładowanie z sieci.

    Po niejednoznacznym ERROR drugi członek mógł dojść, więc cofnięcie pierwszego wolno
    tylko wtedy, gdy z KAŻDĄ wartością drugiego nie da postoju z mocą ani ładowania
    ponad poprzednią moc: tryb (pierwszy) — gdy wracamy do trybu innego niż postój
    i ładowanie; moc (pierwsza) — gdy planowany tryb nie jest postojem ani ładowaniem.
    """
    prev_mode, prev_power = _previous(device, profile, memory)
    new_power = params.power_w
    first = power_first(new_power, prev_power) if new_power is not None else False
    ordered = order_group(writes, power_first=first)
    keys = {w.key for w in ordered}
    if not {"mode", "power_w"} <= keys:
        return ordered, {}, {}, ()
    if prev_mode is not None and profile.mode_direction(prev_mode) == "idle" \
            and (prev_power is None or prev_power > 0.0):
        prev_mode = None
    back_params, _, unfit = fit_params(Params(mode=prev_mode, power_w=prev_power), profile, ents.domain,
                                       ents.mapped, ents.units, ents.attrs)
    back, _ = control_writes(back_params, profile, ents.domain, ents.mapped, keys=None, units=ents.units)
    back_flat = back_params.flatten()
    restore = {w.key: w for w in back if w.key not in unfit}
    restore_flat = {k: back_flat[k] for k in restore}
    risky = ("idle", "charge")
    head = next(w.key for w in ordered if w.key in _MODE_GROUP)
    if head == "mode":
        safe = "mode" in restore_flat and profile.mode_direction(restore_flat["mode"]) not in risky
    else:
        safe = params.mode is not None and profile.mode_direction(params.mode) not in risky
    return ordered, restore, restore_flat, ((head,) if safe else ())


def _unsupported_group(flat: Mapping[str, float | str], profile, ents: EntityContext,
                       memory: ControlMemory) -> tuple[str, ...]:
    """Członkowie grupy, których falownik nie obsługuje: tryb per opcja, moc per klucz.

    Opcję trybu sprawdzamy też z góry na liście `options` encji — inaczej pierwszy cykl
    zapisałby moc przed trybem, którego falownik i tak nie przyjmie.
    """
    out: list[str] = []
    mode = flat.get("mode")
    if isinstance(mode, str):
        options = (ents.attrs.get(ents.mapped.get("mode", "")) or {}).get("options")
        known = not isinstance(options, (list, tuple)) or profile.modes[mode].ha_option in options
        if f"mode:{mode}" in memory.unsupported or not known:
            out.append(f"mode:{mode}")
    if "power_w" in flat and "power_w" in memory.unsupported:
        out.append("power_w")
    return tuple(out)


def commit(decision: CycleDecision, report: WriteReport, memory: ControlMemory, now_mono: float) -> None:
    """Pamięć po wykonaniu: throttling tylko dla zapisów udanych, I-8 tylko gdy tryb poszedł.

    Decyzja inna niż WRITE niczego nie wykonała — nie zostawia śladu w pamięci.
    Runda „zapis → cofnięcie" (raport wykonawcy grupowego) liczy się w obu budżetach:
    throttling pamięta wartość cofniętą z chwilą cofnięcia (I-6), ogranicznik kierunku
    dostaje oba kierunki (I-8), a grupa wchodzi w odwrót.
    """
    if decision.status != WRITE:
        return
    restored = list(getattr(report, "restored", ()))
    restore_failed = list(getattr(report, "restore_failed", ()))
    memory.throttle.record(decision.flat, report.written, now_mono)
    for key in report.written:
        if key in decision.flat:
            memory.last_written[key] = decision.flat[key]
    if restored:
        memory.throttle.record(decision.restore_flat, restored, now_mono)
        for key in restored:
            if key in decision.restore_flat:
                memory.last_written[key] = decision.restore_flat[key]
    # Stan nieznany: ERROR (także przy cofnięciu) bywa zapisem, który doszedł. Pamięć
    # nie może uznać takiego klucza za zgodny ani za poprzednią wartość; odstęp I-6
    # liczy się od próby. Raport bez podziału wyników — każda porażka jest niepewna.
    ambiguous = getattr(report, "ambiguous", None)
    ambiguous = list(report.failed) if ambiguous is None else list(ambiguous)
    memory.uncertain -= {*report.written, *restored}
    memory.uncertain |= set(ambiguous)
    memory.throttle.mark_unknown(ambiguous, now_mono)
    # Tryb nieobsługiwany zapamiętujemy per opcja: inne tryby dalej działają.
    mode = decision.flat.get("mode")
    memory.unsupported |= {f"mode:{mode}" if key == "mode" and isinstance(mode, str) else key
                           for key in report.unsupported}
    # I-8 liczy tryb, który doszedł do falownika (także na chwilę) albo mógł dojść (ERROR);
    # tak samo tryb cofający. Zapis odrzucony na pewno kierunku nie zmienia.
    mode_maybe = "mode" in report.written or "mode" in restored or "mode" in ambiguous
    if decision.direction is not None and mode_maybe:
        memory.limiter.record(decision.direction, now_mono)
        back_maybe = "mode" in restored or ("mode" in restore_failed and "mode" in ambiguous)
        if back_maybe and decision.restore_direction is not None:
            memory.limiter.record(decision.restore_direction, now_mono)
        # Wynik niepewny albo powrót do trybu neutralnego: ostatni kierunek na falowniku
        # jest nieznany — następna zmiana liczy się w każdą stronę.
        if "mode" in ambiguous or (back_maybe and decision.restore_direction not in _DIRECTIONAL):
            memory.limiter.mark_unknown()
    group = [w.key for w in decision.writes if w.key in _MODE_GROUP]
    if restored or restore_failed:
        step = (memory.backoff_base_s if memory.group_backoff_s <= 0.0
                else min(memory.group_backoff_s * 2.0, _BACKOFF_MAX_S))
        memory.group_backoff_s = step
        memory.group_backoff_until = now_mono + step
    elif group and all(k in report.written for k in group):
        memory.group_backoff_s = 0.0
        memory.group_backoff_until = None
