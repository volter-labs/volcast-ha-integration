"""Jeden cykl sterowania: plan → intencja → guardy → dopasowanie → throttling → zapisy.

Czysta decyzja; wywołania usług robi warstwa HA. Każdy wyjątek = cykl bez zapisów
(fail-closed). Bramki (zgoda, przełącznik, wybrany tryb, weryfikacja, pauza) nie
skracają obliczeń — decyzja jest liczona zawsze, żeby „próba na sucho" pokazywała,
co by poszło; zapis wykonuje się tylko przy statusie WRITE.

Tryb i jego nastawa mocy to JEDNA GRUPA: idą razem albo wcale (zmierzone: standby
honoruje Xset jako nastawę ładowania, więc tryb na starej mocy albo moc w starym
trybie ładuje z sieci). Zasady, które trzymają grupę:
* bez encji trybu sterowania nie ma wcale; inna nastawa bez encji (`ents.mapped` jej
  nie ma — brak mapowania albo encja niedostępna) jest nieobsługiwana: wypada z planu
  po strażnikach, jak nastawa odrzucona przez falownik. Akcja, której bez niej nie da
  się bezpiecznie wykonać, schodzi do trybu neutralnego (`_degrade`): moc dla trybu
  z mocą (tryb na starej mocy to inna komenda), próg SoC dla rozładowania, ogranicznik
  eksportu dla rozładowania z zakazem eksportu (0 W). Ładowanie, tryb neutralny i postój
  ogranicznika nie potrzebują (para tylko wypada) — tryb neutralny i postój nie zależą
  od żadnej z nich —
  slot zapasowy i zejście do rezerwy (I-1) idą zawsze;
* para ogranicznika eksportu (`EXPORT_PAIR`) jest nieobsługiwana, wstrzymana i zapisywana
  razem — połowa pary daje zakaz bez skutku albo 0 W przy nieznanym przełączniku;
* zmieniony parametr, który w tym cyklu nie pójdzie (interwał I-6, jednostka encji
  się zmieniła), wstrzymuje zmianę trybu, a z nią moc;
* zmiana trybu wstrzymana (I-6, I-8) wstrzymuje moc — chyba że falownik już ma tryb
  z planu, wtedy sama moc jest zwykłą korektą nastawy;
* tryb albo moc, których falownik nie obsługuje, wyłączają intencję na całą sesję;
* parametr niedopasowalny do zakresu encji blokuje cały cykl;
* sprzedaż z mocą `slot_live_export`: moc baterii po strażnikach zamieniana na nastawę
  eksportu z odczytów PV i poboru tego cyklu (`live_export`), zanim zobaczy ją dopasowanie
  i throttling; nastawa pod minimum encji mocy = slot w trybie neutralnym. Cel rejestrowy
  liczy ją z odczytu falownika, bez ścieżki zapasowej (brak ważnego odczytu = tryb
  neutralny) i ze szczytem poboru z okna jako strefą martwą NVM (`live_export`).
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
from ..engines.sell_xset import sell_ceiling
from ..entity_map import EntityWrite, entity_value
from ..guard_state import DirectionLimiter, WriteBudget, WriteThrottle
from ..guards import GuardContext, GuardResult, apply_guards, temperature_ok
from ..slot import Schedule, effective_action
from ..write_sequence import WriteReport
from .caps import REQUIRED_WRITE_KEYS
from ..params import Params
from .group_writes import order_group, power_first
from .latch import ReserveLatch
from .entity_fit import fit_params
from .target import EntityTarget, WriteTarget, _device_view
from . import live_export as lx
from .live_export import LiveExport, LiveExportMemory

WRITE, DRY_RUN, IDLE, BLOCKED, ERROR = "write", "dry_run", "idle", "blocked", "error"
# Powrót do trybu bazowego zamiast wstrzymania (wyczerpany budżet NVM przy trybie wymuszonym):
# wykonawca pisze tryb bazowy poza budżetem, reszta kluczy czeka na przesunięcie okna.
RESTORE = "restore"

# Tryb i nastawa, która nadaje mu znaczenie — zapisywane razem albo wcale.
_MODE_GROUP = frozenset({"mode", "power_w"})
# Ogranicznik eksportu: znaczy coś tylko razem (zakaz = włączony + 0 W) — obie encje albo żadna.
EXPORT_PAIR = frozenset({"export_limit_w", "export_limit_enabled"})
_DIRECTIONAL = ("charge", "discharge")
# Kwant rejestru: plan niesie ułamki (625,6 W), falownik pokaże 626 — to nie rozjazd.
_QUANTUM = 1.0
# Powroty do trybu bazowego poza budżetem: najwyżej tyle prób w oknie doby.
_RESTORE_CAP = 24
_RESTORE_WINDOW_S = 86400.0
# Ponowienie zapisu odrzuconego przy niezmienionym planie i stanie urządzenia: pierwsza odmowa
# wstrzymuje na max(I-6 profilu, 5 min) (`backoff_base_s`) — dłużej zamieniłoby jedną odmowę
# w godzinę bez sterowania. Dopiero KOLEJNA identyczna odmowa (ta sama prośba, ten sam stan
# urządzenia) podwaja wstrzymanie, najwyżej do 1 h: urządzenie stojące na swoim limicie nie
# pali pamięci nieulotnej co 5 min. Ruch w stronę bezpieczną nie jest wstrzymywany nigdy.
# Klucze, których ZMNIEJSZENIE jest ruchem w stronę bezpieczną (mniej mocy, mniej eksportu).
_SAFE_WHEN_LOWER = ("power_w", "export_limit_w")
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
    # budżet ramek zapisu do NVM w oknie dobowym (z profilu); None = bez budżetu
    budget: WriteBudget | None = None
    # klucz → (zamówiona, rzeczywista) po zapisie przyciętym przez urządzenie (OK_ADJUSTED):
    # rzeczywista liczy się jako osiągnięta dla TEJ zamówionej — bez ponownych zapisów,
    # dopóki plan nie zmieni wartości (albo ktoś nie zmieni jej na urządzeniu)
    adjusted: dict[str, tuple[float | str, float | str]] = field(default_factory=dict)
    # tryb bezpośredni: klucz → (zamówiona, wartość na urządzeniu, chwila) po odmowie (DENIED —
    # ramka poszła, urządzenie odpowiedziało, rejestr bez zmian, np. stoi już na swoim limicie).
    # Ta sama prośba przy tym samym stanie urządzenia nie jest ponawiana przez `hold_s`
    # (max(I-6, 5 min), podwajane przy powtórzonej odmowie do 1 h); klucz dalej wstrzymuje tryb
    # (warunek niespełniony), ale nie pali pamięci nieulotnej co cykl. Nigdy nie dotyczy ruchu
    # w stronę bezpieczną (`_safe_mode`, `_toward_safety`). Wpis: (zamówiona, urządzenie, chwila, hold_s).
    denied: dict[str, tuple[float | str, float | str, float, float]] = field(default_factory=dict)
    # powroty do trybu bazowego poza budżetem (wyczerpany budżet przy trybie wymuszonym):
    # odwrót jak grupy (start max(I-6, 5 min), podwajany do 1 h), limit prób na dobę
    restore_backoff_s: float = 0.0
    restore_until: float | None = None
    restore_attempts: list[float] = field(default_factory=list)
    budget_restore_ineffective: bool = False
    # okna czasowe (TOU): chwila ostatniego przepisania programów i ostatniej ramki włącznika
    # (zegar monotoniczny) — przepisanie najwyżej raz na godzinę, ponowne włączenie po I-6
    tou_rewrite_at: float | None = None
    tou_enable_at: float | None = None
    # koniec ostatniej naszej wymiany TOU — odczyt rozpoczęty wcześniej nie jest podstawą decyzji
    tou_write_end: float | None = None
    # wyłączenia harmonogramu w stronę bezpieczną (poza budżetem): limit na dobę
    tou_safety_offs: list[float] = field(default_factory=list)
    # sprzedaż z mocą `slot_live_export`: ostatni ważny pobór i nastawa zapisana w slocie
    live_export: LiveExportMemory = field(default_factory=LiveExportMemory)

    @classmethod
    def for_profile(cls, profile) -> "ControlMemory":
        return cls(WriteThrottle(profile.min_interval_s),
                   DirectionLimiter(max(1, profile.max_direction_changes_per_hour)), ReserveLatch(),
                   backoff_base_s=max(float(profile.min_interval_s), _BACKOFF_MIN_S),
                   budget=WriteBudget.for_profile(profile))

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
    # Moc PV i pobór domu [W] z wiekiem odczytu per klucz; None = brak mapowania albo odczytu.
    # Świadomie poza `soc_age_s`: opcjonalny czujnik nie może wstrzymać zapisów przez I-9.
    pv_power_w: float | None = None
    pv_age_s: float | None = None
    load_power_w: float | None = None
    load_age_s: float | None = None


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
    # wartości właściciela (migawka) kluczy, które zmieniliśmy — cel, gdy plan nie ma zdania
    owner_values: Mapping[str, float | str] = field(default_factory=dict)


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
    # rodzaj celu zapisu: w trybie bezpośrednim ramki liczy pisarz rejestrów, nie `commit`
    target_kind: str = "entities"
    # widok urządzenia, na którym decyzja stanęła (tryb bezpośredni: pamięć odmów)
    device: dict[str, float | str] = field(default_factory=dict)
    # sprzedaż przeliczona na nastawę eksportu z odczytów (None = intencja bez przeliczenia)
    live_export: LiveExport | None = None

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


def decide_cycle(*, profile, schedule: Schedule | None, now_utc: datetime, now_mono: float,
                 tele: Telemetry, limits: Limits, gates: Gates, memory: ControlMemory,
                 ents: EntityContext | None = None, target: WriteTarget | None = None) -> CycleDecision:
    """`ents` (tryb encji, jak dotąd) albo `target` (dowolny cel) — dokładnie jedno z nich."""
    try:
        if (ents is None) == (target is None):
            raise TypeError("decide_cycle: podaj dokładnie jedno z ents/target")
        if target is None:
            target = EntityTarget(ents)
        return _decide(profile, schedule, now_utc, now_mono, tele, limits, target, gates, memory)
    except Exception as err:  # noqa: BLE001 — każdy błąd decyzji = brak zapisów
        return CycleDecision(ERROR, f"exception:{type(err).__name__}")


def _decide(profile, schedule, now_utc, now_mono, tele, limits, target: WriteTarget, gates,
            memory) -> CycleDecision:
    # Obserwacja poboru domu w KAŻDYM cyklu (każdy tryb, także na sucho i przy blokadzie):
    # ścieżka zapasowa sprzedaży potrzebuje najnowszego ważnego odczytu, nie tego ze sprzedaży.
    memory.live_export = memory.live_export.with_load(
        lx.valid_load(tele.load_power_w, tele.load_age_s, profile.max_state_age_s))
    if target.kind == "direct":
        # Szczyt poboru netto z okna (strefa martwa sprzedaży w trybie bezpośrednim) i ostatnia ważna
        # para (PV, pobór) do przetrzymania jednej nieważnej próbki — też co cykl.
        pv, load = _direct_readings(tele, profile, _rated_or_none(limits))
        memory.live_export = memory.live_export.with_net(
            now_mono, None if pv is None or load is None else load - pv,
            lx.DIRECT_PEAK_WINDOW_S).with_pair(pv, load, now_mono)
    if gates.control_mode != target.kind:
        return CycleDecision(IDLE, "no_mode_chosen")
    if profile.control_model != "mode_setpoint":
        return CycleDecision(IDLE, "tou_preview_only")
    missing = target.missing_keys(profile)
    if any(k in REQUIRED_WRITE_KEYS for k in missing):
        return CycleDecision(IDLE, "missing_entities", unmapped=missing)
    if schedule is None:
        return CycleDecision(IDLE, "no_plan")

    slot, is_fallback = schedule.effective_slot(now_utc)
    mapped_slot = map_slot(slot, profile, limits.rated_power_w, leave_uncapped_export=True)
    mapped_slot = replace(mapped_slot, params=_owner_export(mapped_slot.params, _owner_values(target)))
    common: dict[str, Any] = dict(intent=mapped_slot.intent, fallback=is_fallback)
    # SoC i temperatura to osobne encje: świeży SoC nic nie mówi o temperaturze.
    if target.has_temperature() and tele.battery_temp_c is None:
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

    # Nastawy bez encji wypadają PO strażnikach (ci mogli je dopisać: I-1, I-4).
    without = tuple(k for k in _with_pair(missing) if getattr(guard.params, k, None) is not None)
    planned, degraded = _degrade(guard.params, set(without), profile)
    planned = replace(planned, **{k: None for k in without})
    # Sprzedaż: moc baterii → nastawa eksportu z odczytów tego cyklu (po strażnikach, przed
    # dopasowaniem i throttlingiem — histereza i uzgadnianie widzą już nastawę eksportu).
    dry = _gate_reason(gates, memory, now_mono) is not None
    planned, live, live_notes = _live_export(planned, guard.params, mapped_slot.intent, slot, profile,
                                             tele, limits, target, memory, now_mono, keep=dry)
    degraded = degraded or lx.NOTE_SELL_BELOW_MIN in live_notes or (live is not None and live.degraded)
    params, adjusted, unfit = target.fit(planned, profile)
    if unfit:
        # Klucz, którego encja nie przyjmie, to warunek trybu — tryb nie idzie, nic nie idzie.
        # Notatki sprzedaży zostają: bez nich blokada nie mówi, że sprzedaż stoi i dlaczego.
        return CycleDecision(BLOCKED, "entity_range_unknown", unmapped=unfit, notes=tuple(live_notes),
                             live_export=live, **common)
    flat = params.flatten()
    group_unsupported = _unsupported_group(flat, profile, target, memory)
    if group_unsupported:
        reason = "mode_unsupported" if group_unsupported[0].startswith("mode:") else "power_unsupported"
        return CycleDecision(BLOCKED, reason, flat=flat,
                             dropped_unsupported=group_unsupported, **common)
    device = target.device_view(flat, profile)
    # Uzgodnienie z tym, co falownik naprawdę ma (tylko klucze planu, tylko czytelne).
    # Zły typ odczytu rzuca TypeError po drodze (pamięć wcześniejszych kluczy mogła już
    # zniknąć) — cykl kończy się bez zapisów, następny też, więc to bezpieczne.
    memory.throttle.reconcile(device)
    # Odczyt rozstrzyga niepewność — także kluczy spoza bieżącego planu.
    memory.uncertain -= set(target.device_view(dict.fromkeys(memory.uncertain), profile))
    mode_now = device.get("mode")
    if isinstance(mode_now, str) and mode_now.startswith("?"):
        # Ktoś inny ustawił tryb, którego profil nie zna — nie walczymy i nie cofamy do
        # naszego; wstrzymanie i powiadomienie należą do reguły przejęcia.
        return CycleDecision(BLOCKED, "foreign_mode", flat=flat, takeover=True, **common)
    # To, co falownik już ma, nie jedzie wcale — także po restarcie, gdy pamięć
    # throttlingu jest pusta (inaczej każdy reload = zapis wszystkich nastaw do NVM).
    settled = {k for k in flat if k in device and (same_value(device[k], flat[k])
                                                   or _adjusted_reached(memory, k, flat[k], device[k]))}
    due = memory.throttle.filter(flat, now_mono) - settled
    # Do zmiany na falowniku: to, co pójdzie teraz, i to, co czeka w interwale I-6.
    need = due | (memory.throttle.pending(flat, now_mono) - settled)
    notes: list[str] = (["degraded"] if degraded else []) + list(live_notes)
    unsupported = _with_pair(memory.unsupported)
    refused = set() if _safe_mode(flat.get("mode"), profile) else {
        k for k in due
        if not _toward_safety(k, flat[k], device.get(k))
        and _denied_again(memory, k, flat[k], device.get(k), now_mono)}
    if refused:
        due -= refused                      # zostaje w `need`: dalej wstrzymuje tryb
        notes.append("denied_hold")
    # Budżet NVM: klucz ponad budżetem nie idzie, a jako zmieniony warunek wstrzymuje tryb
    # (jak I-6); tryb/moc ponad budżetem = grupa czeka. Wyjątek: tryb wymuszony nie może stać
    # do końca okna — wtedy powrót do trybu bazowego (poza budżetem).
    # Kandydaci budżetu: WYŁĄCZNIE klucze, które naprawdę trzeba zapisać (klucz bez odczytu,
    # ale zgodny z pamięcią, nie jest zapisem — nie może wywołać wstrzymania ani powrotu).
    base_mode = (profile.raw.get("baseline") or {}).get("mode")
    if base_mode is not None and device.get("mode") == base_mode:
        memory.restore_backoff_s, memory.restore_until = 0.0, None
    blocked: set[str] = set()
    if memory.budget is not None:
        blocked = memory.budget.exhausted(need - unsupported, now_utc.timestamp())
    if blocked:
        due -= blocked
        notes.append("nvm_budget")
        restore, note = _budget_restore(device, profile, target, memory, gates, now_mono, common)
        if restore is not None:
            return restore
        if note is not None:
            notes.append(note)
    _, runtime_unmapped = target.writes(params, profile, None)
    allowed = due - unsupported - set(runtime_unmapped)
    # Para ogranicznika: członek, który musi się zmienić, a nie pójdzie → nie idzie żaden
    # (i wstrzymuje tryb jak każdy niedoszły warunek — niżej).
    if EXPORT_PAIR & (need - allowed) and EXPORT_PAIR & allowed:
        allowed -= EXPORT_PAIR
        notes.append("export_held")
    held_by = [k for k in runtime_unmapped if k != "mode"]
    # Zmieniony warunek, który w tym cyklu nie dojdzie („nieobsługiwany" nie dojdzie nigdy).
    unsettled = (need - allowed - unsupported) - {"mode"}
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
    writes, unmapped = target.writes(params, profile, allowed)
    writes, restore, restore_flat, ambiguous_safe = _group_layout(writes, params, device, profile,
                                                                  target, memory)
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
        dropped_unsupported=tuple(sorted({*(unsupported & set(flat)), *without})),
        notes=tuple(notes),
        restore=restore, restore_flat=restore_flat, restore_ambiguous_safe=ambiguous_safe,
        restore_direction=(profile.mode_direction(restore_flat["mode"])
                           if "mode" in restore_flat else None), target_kind=target.kind,
        device=dict(device) if target.kind == "direct" else {}, live_export=live, **common)


def _denied_again(memory: ControlMemory, key: str, planned, current, now_mono: float) -> bool:
    entry = memory.denied.get(key)
    if entry is None or current is None:
        return False
    req, dev, at, hold_s = entry
    return same_value(req, planned) and same_value(dev, current) and 0.0 <= now_mono - at < hold_s


def _safe_mode(mode, profile) -> bool:
    """Plan prowadzi do trybu bazowego albo neutralnego — wtedy żaden klucz (także warunek trybu)
    nie jest wstrzymywany pamięcią odmowy: powrót do bezpiecznego stanu idzie przy każdej okazji."""
    if not isinstance(mode, str) or mode not in profile.modes:
        return False
    base = (profile.raw.get("baseline") or {}).get("mode")
    return mode in (base, profile.neutral_mode) or profile.mode_direction(mode) == "neutral"


def _toward_safety(key: str, planned, current) -> bool:
    """Zmiana, która zmniejsza moc albo eksport — nigdy wstrzymywana pamięcią odmowy."""
    if key not in _SAFE_WHEN_LOWER:
        return False
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (planned, current)):
        return False
    return abs(planned) < abs(current)


def note_denied(memory: ControlMemory, key: str, requested, device_value, now_mono: float, *,
                max_hold_s: float = _BACKOFF_MAX_S) -> None:
    """Pamięć prawdziwej odmowy: wstrzymanie `backoff_base_s` (max(I-6, 5 min)), podwajane przy
    identycznej, powtórzonej odmowie (ta sama prośba, ten sam stan urządzenia) do `max_hold_s`."""
    prev = memory.denied.get(key)
    hold = memory.backoff_base_s
    if prev is not None and same_value(prev[0], requested) and same_value(prev[1], device_value):
        hold = min(max(prev[3] * 2.0, hold), max_hold_s)
    memory.denied[key] = (requested, device_value, now_mono, hold)


def _adjusted_reached(memory: ControlMemory, key: str, planned, actual) -> bool:
    entry = memory.adjusted.get(key)
    return entry is not None and same_value(entry[0], planned) and same_value(entry[1], actual)


def _budget_restore(device, profile, target: WriteTarget, memory: ControlMemory, gates: Gates,
                    now_mono: float, common: Mapping[str, Any]) -> tuple[CycleDecision | None, str | None]:
    """Decyzja `RESTORE` przy wyczerpanym budżecie, gdy urządzenie jest w trybie wymuszonym.

    Wymuszony = kierunek `charge`/`discharge` albo `idle` z mocą > 0 (albo nieznaną).
    Tryb nieznany albo już neutralny → zwykłe wstrzymanie. Powrót jest poza budżetem, ale
    nie poza ochroną przed pętlą: odwrót (od max(I-6, 5 min), podwajany do 1 h, kasowany
    odczytem trybu bazowego) i limit prób na dobę; po limicie — wstrzymanie i znacznik
    `nvm_budget_restore_ineffective` (urządzenie wraca do trybu wymuszonego samo).
    """
    base = (profile.raw.get("baseline") or {}).get("mode")
    prev_mode, prev_power = _previous(device, profile, memory)
    if base is None or prev_mode is None or prev_mode == base:
        return None, None
    direction = profile.mode_direction(prev_mode)
    forced = direction in _DIRECTIONAL or (direction == "idle" and (prev_power is None or prev_power > 0.0))
    if not forced:
        return None, None
    memory.restore_attempts = [t for t in memory.restore_attempts if 0.0 <= now_mono - t < _RESTORE_WINDOW_S]
    if len(memory.restore_attempts) >= _RESTORE_CAP:
        memory.budget_restore_ineffective = True
        return None, "nvm_budget_restore_ineffective"
    until = memory.restore_until
    if until is not None and until - memory.restore_backoff_s <= now_mono < until:
        return None, "nvm_budget_restore_wait"
    back = Params(mode=base)
    writes, _ = target.writes(back, profile, {"mode"})
    reason = _gate_reason(gates, memory, now_mono)
    return CycleDecision(RESTORE if reason is None else DRY_RUN, reason or "nvm_budget",
                         writes=writes, flat=back.flatten(), direction=profile.mode_direction(base),
                         notes=("nvm_budget",), target_kind=target.kind, **common), None



def _live_export(planned: Params, guarded: Params, intent: str, slot, profile, tele: Telemetry,
                 limits: Limits, target: WriteTarget, memory: ControlMemory, now_mono: float, *, keep: bool
                 ) -> tuple[Params, LiveExport | None, tuple[str, ...]]:
    """Nastawa eksportu dla KOŃCOWEJ intencji sprzedaży z mocą `slot_live_export`.

    Końcowa = po strażnikach i `_degrade`: slot zdjęty do trybu neutralnego (I-1, brak
    encji) ma już inny tryb i nie jest przeliczany. Moc baterii — z nastaw po strażnikach;
    pułap — ogranicznik z nastaw po strażnikach, tylko gdy włączony (zakaz = 0 W; bez
    zdania planu — włączony ogranicznik odczytany z falownika), i moc
    znamionowa, a gdy jej nie znamy (konfiguracja bez tej opcji) — górna granica zakresu
    encji mocy; bez obu sprzedaż stoi (0 W, notatka `sell_no_rated`). PV
    i pobór wyłącznie z `tele` (wartość i wiek z jednego odczytu). Nastawa poniżej minimum
    encji mocy nie idzie ani podniesiona, ani pominięta: cały slot schodzi do trybu
    neutralnego jak przy degradacji. `keep` (próba na sucho) = pamięć zapisanej nastawy
    nietknięta (pobór obserwuje każdy cykl, na początku `_decide`).
    """
    mem = memory.live_export
    live_kind = profile.power_kind(intent) == lx.LIVE_EXPORT_KIND
    if not live_kind or planned.mode != profile.intent(intent)["mode"] or planned.power_w is None:
        if not keep:
            memory.live_export = mem.forget_written()
        return planned, None, ()
    if not isinstance(target, EntityTarget):
        return _live_export_direct(planned, guarded, intent, slot, profile, tele, limits, target, memory,
                                   now_mono, keep=keep)
    ents = target.ents
    max_age = profile.max_state_age_s
    rated = limits.rated_power_w
    if not (math.isfinite(rated) and rated > 0.0):
        rated = _power_entity_max_w(profile, ents)
    enabled, limit = guarded.export_limit_enabled, guarded.export_limit_w
    if enabled is None and limit is None:
        # Plan bez zdania o ograniczniku i nie my go ustawialiśmy: włączony ogranicznik
        # właściciela na falowniku (np. wymóg operatora) i tak tnie eksport — to też pułap.
        dev = _device_view(ents.readings, dict.fromkeys(EXPORT_PAIR), profile, ents)
        on, value = dev.get("export_limit_enabled"), dev.get("export_limit_w")
        if isinstance(on, float) and on >= 0.5 and isinstance(value, float):
            enabled, limit = True, value
    live, mem = lx.compute(
        key=(slot.start, slot.end, intent), battery_w=planned.power_w,
        pv_w=lx.valid_reading(tele.pv_power_w, tele.pv_age_s, max_age, lx.pv_limit_w(rated)),
        load_w=lx.valid_load(tele.load_power_w, tele.load_age_s, max_age),
        export_limit_w=lx.export_ceiling(enabled, limit), rated_power_w=rated, memory=mem)
    notes = ([live.note()] + ([lx.NOTE_SELL_NO_LOAD] if live.no_load else [])
             + ([lx.NOTE_SELL_NO_RATED] if live.no_rated else []))
    if _below_entity_min(live.xset_w, profile, ents):
        planned, _ = _degrade(planned, {"power_w"}, profile)
        if not keep:
            memory.live_export = mem.forget_written()
        return planned, replace(live, below_min=True), (*notes, lx.NOTE_SELL_BELOW_MIN)
    if not keep:
        memory.live_export = mem
    return replace(planned, power_w=live.xset_w), live, tuple(notes)


def _rated_or_none(limits: Limits) -> float | None:
    rated = limits.rated_power_w
    return rated if isinstance(rated, (int, float)) and math.isfinite(rated) and rated > 0.0 else None


def _direct_readings(tele: Telemetry, profile, rated: float | None) -> tuple[float | None, float | None]:
    max_age = profile.max_state_age_s
    return (lx.valid_reading(tele.pv_power_w, tele.pv_age_s, max_age, lx.pv_limit_w(rated)),
            lx.valid_load(tele.load_power_w, tele.load_age_s, max_age))


def _live_export_direct(planned: Params, guarded: Params, intent: str, slot, profile, tele: Telemetry,
                        limits: Limits, target: WriteTarget, memory: ControlMemory, now_mono: float, *,
                        keep: bool) -> tuple[Params, LiveExport, tuple[str, ...]]:
    """Nastawa eksportu sprzedaży na celu rejestrowym (wzór i pułap jak w trybie encji).

    PV i pobór z odczytu falownika tego cyklu (`tele`, wiek = wiek odczytu). Brak ważnego
    odczytu któregoś z nich albo nieznana moc znamionowa = slot w trybie neutralnym (bez
    ścieżki zapasowej z ostatnim poborem — nie zgadujemy). Pobór we wzorze = szczyt poboru
    netto z okna `DIRECT_PEAK_WINDOW_S` (strefa martwa NVM; opis w `live_export`). Wynik
    ujemny = 0 W przy zachowanym trybie sprzedaży (bateria kryje sam dom), jak referencja.
    """
    mem = memory.live_export
    key = (slot.start, slot.end, intent)
    rated = _rated_or_none(limits)
    pv, load = _direct_readings(tele, profile, rated)
    held = False
    if rated is not None and (pv is None or load is None):
        # Chwilowy błąd odczytu (jedna próbka): ostatnia ważna para przez JEDEN cykl — bez migania
        # tryb neutralny ↔ sprzedaż; druga nieważna próbka z rzędu = tryb neutralny (niżej).
        pair = mem.held_pair(now_mono)
        if pair is not None:
            (pv, load), held = pair, True
    if rated is None or pv is None or load is None:
        no_reading = pv is None or load is None
        planned, _ = _degrade(planned, {"power_w"}, profile)
        if not keep:
            memory.live_export = mem.forget_written()
        live = LiveExport(key=key, battery_w=float(guarded.power_w or 0.0), pv_w=pv, load_w=load, xset_w=0.0,
                          no_load=False, no_rated=rated is None, no_reading=no_reading, degraded=True)
        notes = ([lx.NOTE_SELL_NO_READING] if no_reading else []) + ([lx.NOTE_SELL_NO_RATED] if rated is None else [])
        return planned, live, tuple(notes)
    enabled, limit = guarded.export_limit_enabled, guarded.export_limit_w
    if enabled is None and limit is None:
        # Plan bez zdania o ograniczniku: włączony ogranicznik właściciela na falowniku też jest pułapem.
        dev = target.device_view(dict.fromkeys(EXPORT_PAIR), profile)
        on, value = dev.get("export_limit_enabled"), dev.get("export_limit_w")
        if isinstance(on, float) and on >= 0.5 and isinstance(value, float):
            enabled, limit = True, value
    peak = mem.net_peak(now_mono, lx.DIRECT_PEAK_WINDOW_S)
    net = load - pv
    effective_load = pv + max(net, peak if peak is not None else net)
    export_limit = lx.export_ceiling(enabled, limit)
    live, mem = lx.compute(
        key=key, battery_w=planned.power_w, pv_w=pv, load_w=effective_load,
        export_limit_w=export_limit, rated_power_w=rated, memory=mem)
    notes = [live.note()] + ([lx.NOTE_SELL_READING_HELD] if held else [])
    # Puste okno (restart albo > okno bez ważnych próbek): PV, które dopycha nastawę do pułapu
    # (min(moc znamionowa, limit eksportu) — jak w `sell_xset`), czeka na drugą próbkę (szczyt okna
    # z dwóch próbek maskuje pojedynczy błąd PV). Do tego czasu nastawa bez PV (bateria − pobór) —
    # bateria oddaje najwyżej plan.
    no_pv = max(0.0, float(planned.power_w or 0.0) - load)
    ceiling = sell_ceiling(rated, export_limit)
    if mem.window_count(now_mono, lx.DIRECT_PEAK_WINDOW_S) < 2 and live.xset_w >= ceiling - 0.5 \
            and no_pv < live.xset_w:
        live = replace(live, xset_w=no_pv)
        notes.append(lx.NOTE_SELL_PV_UNCONFIRMED)
    if not keep:
        memory.live_export = mem
    return replace(planned, power_w=live.xset_w), live, tuple(notes)


def _below_entity_min(power_w: float, profile, ents: EntityContext) -> bool:
    """Nastawa mocy pod minimum encji: dopasowanie nie przyjmie jej bez podniesienia.

    Encja bez poprawnego zakresu to inny przypadek (cykl blokuje `entity_range_unknown`).
    """
    _, _, unfit = fit_params(Params(power_w=power_w), profile, ents.domain, ents.mapped,
                             ents.units, ents.attrs)
    if "power_w" not in unfit:
        return False
    attrs = ents.attrs.get(ents.mapped.get("power_w", "")) or {}
    return _entity_range(attrs) is not None


def _entity_range(attrs: Mapping[str, Any]) -> tuple[float, float] | None:
    """(min, max) encji w jej jednostkach; None bez poprawnego zakresu."""
    lo, hi = attrs.get("min"), attrs.get("max")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in (lo, hi)) or lo > hi:
        return None
    return float(lo), float(hi)


def _power_entity_max_w(profile, ents: EntityContext) -> float | None:
    """Największa nastawa [W], jaką przyjmie encja mocy — ta sama granica co w `fit_params`.

    Zakres przeliczony tą samą drogą co odczyt encji (jednostka, transformacja profilu);
    None bez encji, bez poprawnego zakresu albo bez dodatniej granicy.
    """
    rng = _entity_range(ents.attrs.get(ents.mapped.get("power_w", "")) or {})
    if rng is None:
        return None
    bounds = []
    for v in rng:
        try:
            w = entity_value("power_w", repr(v), profile, ents.domain, unit=ents.units.get("power_w"))
        except (KeyError, ValueError, TypeError):
            return None
        if not isinstance(w, float) or not math.isfinite(w):
            return None
        bounds.append(w)
    top = max(bounds)
    return top if top > 0.0 else None


def _degrade(params: Params, without: set[str], profile) -> tuple[Params, bool]:
    """Akcja bez nastawy, której wymaga, schodzi do trybu neutralnego (bez mocy).

    Moc — tryb, który jej używa; próg SoC — rozładowanie; ogranicznik eksportu — tylko
    rozładowanie z zakazem eksportu (0 W; pułap to nie warunek bezpieczeństwa). Ładowanie
    go nie potrzebuje: tryb neutralny i tak oddaje nadwyżkę PV, a ładowanie przy ujemnej
    cenie to najcenniejsza akcja. Tryb neutralny i postój idą bez nich. Pozostałe nastawy
    planu zostają (wołający i tak zdejmuje te bez encji).
    """
    mode = params.mode
    if mode is None or mode == profile.neutral_mode:
        return params, False
    direction = profile.mode_direction(mode)
    needed = ("power_w" in without
              or ("soc_min" in without and direction == "discharge")
              or (bool(without & EXPORT_PAIR) and direction == "discharge"
                  and params.export_limit_w == 0.0))
    if not needed:
        return params, False
    return replace(params, mode=profile.neutral_mode, power_w=None), True


def _owner_values(target: WriteTarget) -> Mapping[str, float | str]:
    """Wartości właściciela z migawki kluczy, które zmieniliśmy (cel encji i rejestrowy)."""
    return getattr(target, "owner_values", None) or {}


def _with_pair(keys) -> set[str]:
    """Klucze z dopełnioną parą ogranicznika eksportu (jeden członek = oba)."""
    out = set(keys)
    return out | EXPORT_PAIR if out & EXPORT_PAIR else out


def _owner_export(params: Params, owner: Mapping[str, float | str]) -> Params:
    """Plan bez zdania o ograniczniku eksportu: po naszym zaostrzeniu — powrót do wartości
    właściciela (para razem albo wcale); bez niego ogranicznika nie ruszamy."""
    if params.export_limit_enabled is not None or params.export_limit_w is not None:
        return params
    enabled, limit = owner.get("export_limit_enabled"), owner.get("export_limit_w")
    if not isinstance(enabled, float) or not isinstance(limit, float):
        return params
    return replace(params, export_limit_enabled=enabled >= 0.5, export_limit_w=limit)


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


def _group_layout(writes: list, params: Params, device: Mapping[str, float | str],
                  profile, target: WriteTarget, memory: ControlMemory
                  ) -> tuple[list, dict[str, Any], dict[str, float | str], tuple[str, ...]]:
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
    back_params, _, unfit = target.fit(Params(mode=prev_mode, power_w=prev_power), profile)
    back, _ = target.writes(back_params, profile, None)
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


def _unsupported_group(flat: Mapping[str, float | str], profile, target: WriteTarget,
                       memory: ControlMemory) -> tuple[str, ...]:
    """Członkowie grupy, których falownik nie obsługuje: tryb per opcja, moc per klucz.

    Opcję trybu sprawdzamy też z góry na liście `options` encji — inaczej pierwszy cykl
    zapisałby moc przed trybem, którego falownik i tak nie przyjmie.
    """
    out: list[str] = []
    mode = flat.get("mode")
    if isinstance(mode, str):
        if f"mode:{mode}" in memory.unsupported or target.mode_option_unknown(mode, profile):
            out.append(f"mode:{mode}")
    if "power_w" in flat and "power_w" in memory.unsupported:
        out.append("power_w")
    return tuple(out)


def commit(decision: CycleDecision, report: WriteReport, memory: ControlMemory, now_mono: float, *,
           now_wall: float | None = None) -> None:
    """Pamięć po wykonaniu: throttling tylko dla zapisów udanych, I-8 tylko gdy tryb poszedł.

    Decyzja inna niż WRITE niczego nie wykonała — nie zostawia śladu w pamięci.
    Runda „zapis → cofnięcie" (raport wykonawcy grupowego) liczy się w obu budżetach:
    throttling pamięta wartość cofniętą z chwilą cofnięcia (I-6), ogranicznik kierunku
    dostaje oba kierunki (I-8), a grupa wchodzi w odwrót.
    """
    if decision.status not in (WRITE, RESTORE):
        return
    if decision.status == RESTORE:
        memory.restore_attempts.append(now_mono)
        memory.restore_backoff_s = (memory.backoff_base_s if memory.restore_backoff_s <= 0.0
                                    else min(memory.restore_backoff_s * 2.0, _BACKOFF_MAX_S))
        memory.restore_until = now_mono + memory.restore_backoff_s
    restored = list(getattr(report, "restored", ()))
    restore_failed = list(getattr(report, "restore_failed", ()))
    _count_budget(decision, report, memory, restored, restore_failed, now_wall)
    # Zapis z wartością przyciętą przez urządzenie (OK_ADJUSTED): pamięć trzyma wartość
    # RZECZYWISTĄ z odczytu zwrotnego, nie zamówioną.
    actual = getattr(report, "actual", None) or {}
    flat = {**decision.flat, **{k: v for k, v in actual.items() if k in decision.flat}}
    memory.throttle.record(flat, report.written, now_mono)
    for key in report.written:
        if key in flat:
            memory.last_written[key] = flat[key]
        if key in actual and key in decision.flat:
            memory.adjusted[key] = (decision.flat[key], actual[key])
        else:
            memory.adjusted.pop(key, None)
        memory.denied.pop(key, None)
    if decision.target_kind == "direct":
        amb = getattr(report, "ambiguous", None)
        definite = set(report.failed) - set(report.failed if amb is None else amb)
        for key in definite:
            if key in decision.flat and key in decision.device:
                note_denied(memory, key, decision.flat[key], decision.device[key], now_mono)
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
    _commit_live_export(decision, report, memory, restored, restore_failed, ambiguous)
    group = [w.key for w in decision.writes if w.key in _MODE_GROUP]
    if restored or restore_failed:
        step = (memory.backoff_base_s if memory.group_backoff_s <= 0.0
                else min(memory.group_backoff_s * 2.0, _BACKOFF_MAX_S))
        memory.group_backoff_s = step
        memory.group_backoff_until = now_mono + step
    elif group and all(k in report.written for k in group):
        memory.group_backoff_s = 0.0
        memory.group_backoff_until = None


def _count_budget(decision: CycleDecision, report: WriteReport, memory: ControlMemory,
                  restored: list[str], restore_failed: list[str], now_wall: float | None) -> None:
    """Budżet NVM w trybie encji: każde wywołanie usługi zapisu i osobno cofnięcia.

    W trybie bezpośrednim ramki liczy pisarz rejestrów (`on_send`, dokładna liczba ramek,
    także ponowionych) — tu nic. `now_wall=None` = bez liczenia (testy, próba).
    """
    if now_wall is None or memory.budget is None or decision.target_kind != "entities":
        return
    ambiguous = getattr(report, "ambiguous", None)
    calls = dict.fromkeys([*report.written, *(ambiguous or ()), *report.failed])
    for key in calls:
        memory.budget.note(key, now_wall)
    for key in dict.fromkeys([*restored, *restore_failed]):
        memory.budget.note(key, now_wall)


def _commit_live_export(decision: CycleDecision, report: WriteReport, memory: ControlMemory,
                        restored: list[str], restore_failed: list[str], ambiguous: list[str]) -> None:
    """Nastawa eksportu sprzedaży staje się „zapisaną" dopiero po udanym zapisie mocy.

    Każda porażka w cyklu (odrzucenie, wynik niepewny, cofnięcie) kasuje pamięć — następny
    cykl liczy od nowa zamiast trzymać wartość, której falownik mógł nie dostać. Moc, która
    w tym cyklu nie szła (interwał, już zgodna), pamięci nie zmienia.
    """
    live = decision.live_export
    mem = memory.live_export
    if live is None or live.below_min or live.degraded:
        memory.live_export = mem.forget_written()
        return
    if report.failed or report.unsupported or ambiguous or restored or restore_failed:
        memory.live_export = mem.forget_written()
    elif "power_w" in report.written and "power_w" in decision.flat:
        memory.live_export = mem.with_written(live.key, float(decision.flat["power_w"]))
