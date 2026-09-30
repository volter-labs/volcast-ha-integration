"""Guardy bezstanowe — port referencyjnego wykonawcy (`vb_guards.c`).

Mapper TŁUMACZY, guardy decydują, czy wolno i w jakiej postaci. I-10 odrzuca CAŁĄ
komendę (falownik z połową nastaw to stan, któremu ma zapobiec kolejność zapisu).
I-9/I-3(temperatura) wstrzymują wszystkie zapisy. I-1 broni WYŁĄCZNIE przed
rozładowaniem i jest nieostre (`<=`) — z progu nie da się rozładować ani o wat.
Brak ceny to nie cena zero (I-4 nie blokuje na podstawie braku danych).
Nieznana granica sprzętowa (0) nie jest nakładana — zgadywanie gorsze niż brak.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .params import Params, TouProgram
from .profile import Profile
from .slot import Action

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_DEGRADED = "degraded"
MAX_POWER_W = 30000.0          # sanity-check I-10; twardą granicę nakłada I-3
_SOC_RATE_PP_PER_MIN = 4.0     # I-9 tempo (warstwa HA)
_SOC_JUMP_FLOOR_PP = 5.0
_DEFAULT_MAX_STATE_AGE_S = 300.0  # jak Box: nieprawidłowy limit wieku nie wyłącza I-9


@dataclass(frozen=True)
class GuardContext:
    soc: float | None
    soc_age_s: float
    temperature_ok: bool
    soc_reserve: float
    action: Action
    price_pln_kwh: float | None = None
    max_charge_w: float = 0.0
    max_export_w: float = 0.0
    max_state_age_s: float = 300.0
    backup_mode: bool = False
    previous_soc: float | None = None
    previous_soc_gap_s: float | None = None
    # Stan zatrzasku rezerwy (warstwa wykonawcy). None = porównanie `soc <= rezerwa`
    # jak w referencyjnym wykonawcy — złote wektory nie niosą tego pola.
    reserve_engaged: bool | None = None


@dataclass(frozen=True)
class GuardResult:
    status: str
    write_allowed: bool
    invariant: str | None
    note: str
    params: Params


def temperature_ok(temp_c: float | None, profile: Profile) -> bool:
    # Bez odczytu temperatury nie blokujemy drugi raz — brak odczytu łapie I-9.
    if temp_c is None:
        return True
    return profile.temp_min_c < temp_c < profile.temp_max_c


def _reject(invariant: str, note: str) -> GuardResult:
    return GuardResult(STATUS_DEGRADED, False, invariant, note, Params())


def _finite(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def max_state_age(max_state_age_s: object) -> float:
    """Limit wieku odczytu I-9; nieprawidłowy limit nie wyłącza kontroli (domyślne 300 s)."""
    return float(max_state_age_s) if _finite(max_state_age_s) and max_state_age_s > 0 \
        else _DEFAULT_MAX_STATE_AGE_S


def state_fresh(age_s: object, max_state_age_s: object) -> bool:
    """Reguła świeżości I-9: wiek skończony, nieujemny i nie większy niż limit.

    Wiek NaN/ujemny to błąd zegara albo odczytu, `inf` = brak znacznika czasu — nieświeży.
    """
    return _finite(age_s) and 0 <= age_s <= max_state_age(max_state_age_s)


def _pct_ok(v: float | None) -> bool:
    return v is None or (math.isfinite(v) and 0.0 <= v <= 100.0)


def _pow_ok(v: float | None) -> bool:
    return v is None or (math.isfinite(v) and 0.0 <= v <= MAX_POWER_W)


def _tou_ok(tou: tuple[TouProgram, ...], profile: Profile) -> str | None:
    last = -1
    for i, p in enumerate(tou, start=1):
        if not (0 <= p.start_min < 1440) or p.start_min % profile.time_step_min:
            return f"program {i}: start poza dobą albo poza krokiem {profile.time_step_min} min"
        if p.start_min <= last:
            return f"program {i}: starty muszą rosnąć"
        last = p.start_min
        if not _pct_ok(p.soc) or not _pow_ok(p.power_w):
            return f"program {i}: SoC albo moc poza zakresem"
    return None


def apply_guards(params: Params, ctx: GuardContext, profile: Profile) -> GuardResult:
    writes = profile.raw["write"]
    # ── I-10: sanityzacja, fail-closed ──
    if params.mode is not None and params.mode not in profile.modes:
        return _reject("I-10", f"nieznany tryb {params.mode!r} — odrzucam całą komendę")
    for name in ("soc_min", "soc_max"):
        if not _pct_ok(getattr(params, name)):
            return _reject("I-10", f"{name} poza 0..100 — odrzucam całą komendę")
    for name in ("power_w", "export_limit_w"):
        if not _pow_ok(getattr(params, name)):
            return _reject("I-10", f"{name} poza zakresem — odrzucam całą komendę")
    if params.tou is not None:
        problem = _tou_ok(params.tou, profile)
        if problem:
            return _reject("I-10", problem)

    # Rezerwa spoza 0..100 (albo NaN) gasiłaby porównania I-1/I-7 bez śladu — fail-closed.
    if ctx.soc_reserve is None or not _pct_ok(ctx.soc_reserve):
        return _reject("I-10", f"rezerwa SoC={ctx.soc_reserve} poza 0..100 — odrzucam całą komendę")

    # ── I-9: świeżość i wiarygodność odczytu ──
    max_age = max_state_age(ctx.max_state_age_s)
    if ctx.soc is None:
        return _reject("I-9", "brak odczytu SoC — wstrzymuję zapisy")
    if not state_fresh(ctx.soc_age_s, max_age):
        return _reject("I-9", f"odczyt nieświeży albo o nieznanym wieku (limit {max_age:.0f} s)")
    if not (0.0 <= ctx.soc <= 100.0):
        return _reject("I-9", f"SoC={ctx.soc} fizycznie niemożliwy")
    if ctx.previous_soc is not None and ctx.previous_soc_gap_s is not None \
            and ctx.previous_soc_gap_s <= max_age:
        allowed = max(_SOC_JUMP_FLOOR_PP, _SOC_RATE_PP_PER_MIN * ctx.previous_soc_gap_s / 60.0)
        if abs(ctx.soc - ctx.previous_soc) > allowed:
            return _reject("I-9", "skok SoC szybszy niż fizycznie możliwy")

    # ── I-3: okno temperatur — bije nawet rezerwę ──
    if not ctx.temperature_ok:
        return _reject("I-3", "falownik/BMS poza oknem temperatur — wstrzymuję zapisy")

    # ── I-7: tryb backup — rezerwa nienaruszalna ──
    if ctx.backup_mode and params.soc_min is not None and params.soc_min < ctx.soc_reserve:
        return _reject("I-7", "tryb backup: plan obniża próg poniżej rezerwy")

    out = params
    status, invariant, note = STATUS_OK, None, ""

    # ── I-1: SoC <= rezerwa (nieostro); zatrzask wykonawcy, gdy jest, ma pierwszeństwo ──
    below_reserve = (ctx.reserve_engaged if ctx.reserve_engaged is not None
                     else ctx.soc <= ctx.soc_reserve)
    if below_reserve:
        changed = False
        wants_discharge = ctx.action is Action.DISCHARGE or (
            out.mode is not None and profile.mode_direction(out.mode) == "discharge")
        if wants_discharge:
            if out.mode is not None and profile.mode_direction(out.mode) == "discharge":
                out = replace(out, mode=profile.neutral_mode)
                changed = True
            if out.power_w is not None:
                # Moc ma ZNIKNĄĆ, nie zostać wyzerowana (powrót trybu dałby „rozładuj 0 W").
                out = replace(out, power_w=None)
                changed = True
        if "soc_min" in writes and (out.soc_min is None or out.soc_min < ctx.soc_reserve):
            out = replace(out, soc_min=ctx.soc_reserve)
            changed = True
        if changed:
            status, invariant = STATUS_PARTIAL, "I-1"
            cmp = "zatrzask rezerwy" if ctx.reserve_engaged is not None else "<= rezerwa"
            note = f"SoC={ctx.soc:.1f}% {cmp} {ctx.soc_reserve:.1f}% — rozładowanie zdjęte"

    # ── I-1: program TOU jest podłogą, którą falownik utrzymuje samodzielnie
    # przez całą dobę — podnosimy ją do rezerwy niezależnie od bieżącego SoC.
    if out.tou is not None and any(p.soc < ctx.soc_reserve for p in out.tou):
        out = replace(out, tou=tuple(replace(p, soc=max(p.soc, ctx.soc_reserve)) for p in out.tou))
        status, invariant = STATUS_PARTIAL, "I-1"
        note = f"program TOU pod rezerwą {ctx.soc_reserve:.1f}% — podniesiony"

    # ── I-3: przycięcie do granic sprzętowych (0 = nieznana, nie przycinamy) ──
    if ctx.max_charge_w > 0 and out.power_w is not None and out.power_w > ctx.max_charge_w:
        out = replace(out, power_w=ctx.max_charge_w)
        status, invariant, note = STATUS_PARTIAL, "I-3", f"moc przycięta do {ctx.max_charge_w:.0f} W"
    if ctx.max_export_w > 0 and out.export_limit_w is not None and out.export_limit_w > ctx.max_export_w:
        out = replace(out, export_limit_w=ctx.max_export_w)
        status, invariant, note = STATUS_PARTIAL, "I-3", f"limit eksportu przycięty do {ctx.max_export_w:.0f} W"
    if ctx.max_charge_w > 0 and out.tou is not None and any(p.power_w > ctx.max_charge_w for p in out.tou):
        out = replace(out, tou=tuple(replace(p, power_w=min(p.power_w, ctx.max_charge_w)) for p in out.tou))
        status, invariant, note = STATUS_PARTIAL, "I-3", f"moc programów przycięta do {ctx.max_charge_w:.0f} W"

    # ── I-4: nie eksportuj przy cenie <= 0 (tylko gdy profil ma ogranicznik eksportu) ──
    if ctx.price_pln_kwh is not None and ctx.price_pln_kwh <= 0 and "export_limit_w" in writes:
        changed = False
        if out.export_limit_w is None or out.export_limit_w != 0.0:
            out = replace(out, export_limit_w=0.0)
            changed = True
        if out.export_limit_enabled is not True:
            out = replace(out, export_limit_enabled=True)
            changed = True
        if changed:
            status, invariant = STATUS_PARTIAL, "I-4"
            note = f"cena {ctx.price_pln_kwh:.3f} PLN/kWh <= 0 — eksport zablokowany"

    return GuardResult(status, True, invariant, note, out)
