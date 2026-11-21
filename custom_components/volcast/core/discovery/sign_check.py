"""Ocena konwencji znaku mocy sieci i baterii z bilansu energii. Czyste funkcje, bez HA.

Konwencja docelowa Volcast: sieć dodatnia = pobór, bateria dodatnia = rozładowanie, więc
`PV + sieć + bateria ≈ zużycie`. Dla wyrównanych w czasie próbek (np. statystyki 5-minutowe z doby)
liczymy resztę bilansu dla czterech kombinacji znaków surowych odczytów sieci i baterii. Wynik to
ZALECENIE dla użytkownika (`negate`: odwrócić znak czujnika?), nigdy automat — fałszywe zalecenie jest
gorsze niż brak, więc progi są konserwatywne, a niepewność kończy się `negate=None` z kodem powodu.

Z czujnikiem zużycia sieć i bateria oceniane są niezależnie (marginalnie: najlepsza kombinacja z danym znakiem jednej
wielkości przy dowolnym znaku drugiej) — bateria w spoczynku nie blokuje oceny sieci i odwrotnie.

Bez czujnika zużycia bilans z trzech wielkości nie rozstrzyga. Jedyny niezawodny sygnał: zużycie nie
może być ujemne — ale tylko wtedy, gdy żadne źródło nie umyka czujnikom. Bateria umyka łatwo: czujnik
nieprzypisany, zablokowany na 0, druga bateria poza pomiarem, „PV” = moc wyjściowa falownika
(PV + bateria); bateria oddająca do sieci wygląda wtedy jak odwrócony znak sieci, a znak samej baterii
— jak odwrócony przy ładowaniu. Dlatego bez czujnika zużycia:
- baterii NIE oceniamy nigdy (`no_load_sensor`);
- sieć oceniamy wyłącznie w instalacji NA PEWNO bez baterii (`battery_present=False` od warstwy HA i brak
  przypisanego czujnika mocy baterii — przypisany czujnik przy `False` to sprzeczne wejście). Każda
  instalacja z baterią albo o nieznanym stanie (`True`, `None` — domyślnie) dostaje `no_load_sensor`.
W instalacji bez baterii dowodem są wyłącznie próbki nocne (|PV| < tolerancji): dom może wtedy tylko
pobierać, a pobór odczytany jako oddawanie zdradza odwrócony znak. W dzień niewidoczna część produkcji
(część stringów, mikroinwertery poza pomiarem) pozwala „oddawać” ponad widoczne PV przy poprawnym znaku.
Wymagamy też, by czujnik PV widocznie produkował (godzina próbek) — nieprzypisane PV albo kW podane jako W
czyniłyby „nocą” cały dzień. Dom bez nocnego poboru ponad tolerancję zostaje bez oceny (`no_load_sensor`).

Z czujnikiem zużycia `battery_present` nie ma wpływu. Bateria obecna, ale nieprzypisana, psuje tam
bilans (brakuje jej przepływu): wynik `ambiguous` albo trafny, gdy „PV” to moc wyjściowa falownika
(zawiera baterię).

Z czujnikiem zużycia moc wyjściowa falownika jako „PV” liczy baterię podwójnie: reszta to ±bateria dla
obu znaków, więc ocena baterii kończy się `ambiguous`. PV albo zużycie wyraźnie ujemne w wielu próbkach
oznacza czujnik mierzący coś innego — też `ambiguous`.

Wynik jest zgodny z czujnikiem zużycia: zużycie wyliczane z tych samych surowych odczytów (np. szablonem
`PV + sieć + bateria`) domyka bilans dla konwencji, w której je policzono, poprawnej czy nie — takiego
czujnika nie należy podawać jako zużycia (rozstrzyga warstwa HA).

Wejście: sekwencja map `{pole: wartość w W}` z kluczami pól telemetrii (`PV`, `GRID`, `BATTERY`, `LOAD`).
Klucz obecny w którejkolwiek próbce (nawet z `None`) = czujnik przypisany; próbka jest użyteczna tylko
z liczbami skończonymi dla wszystkich przypisanych czujników. Brak czujnika PV = 0 W (bilans się wtedy
nie domknie i wynik będzie `ambiguous`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

# klucze próbek — nazwy pól telemetrii („Installation details”)
PV = "pv_power_w"
GRID = "grid_power_w"
BATTERY = "battery_power_w"
LOAD = "load_power_w"
QUANTITIES: tuple[str, ...] = (PV, GRID, BATTERY, LOAD)

# podstawa oceny
BASIS_BALANCE = "balance"            # pełny bilans z czujnikiem zużycia
BASIS_LOAD_BOUND = "load_bound"      # bez zużycia: tylko granica „zużycie ≥ 0”

# kody powodów braku zalecenia
NO_SENSOR = "no_sensor"              # wielkość nieprzypisana — nie ma czego oceniać
TOO_FEW_SAMPLES = "too_few_samples"
GRID_IDLE = "grid_idle"
BATTERY_IDLE = "battery_idle"
NO_LOAD_SENSOR = "no_load_sensor"    # bez zużycia brak fizycznie niemożliwych próbek do rozstrzygnięcia
AMBIGUOUS = "ambiguous"

# ── progi (konserwatywne: wolimy brak zalecenia niż złe) ─────────────────────
# minimum użytecznych próbek: 2 h statystyk 5-minutowych — mniej to migawka, nie przebieg (pojedyncza
# próbka bieżących stanów zawsze kończy się `too_few_samples`)
MIN_SAMPLES = 24
# minimum próbek z wyraźnym przepływem ocenianej wielkości: godzina pracy, więc jedno zdarzenie
# (czajnik, chmura, przesunięcie uśrednień) nie przeważy
MIN_ACTIVE_SAMPLES = 12
# próg „wyraźnego przepływu” i tolerancji szumu: co najmniej 100 W (szum czujników, pobór własny
# falownika, PV ujemne w nocy o kilka W), a w dużych instalacjach 3 % skali — dokładność pomiaru i błąd
# uśredniania rosną z mocą
FLOW_FLOOR_W = 100.0
FLOW_SHARE = 0.03
# skala instalacji: 95. percentyl największego przepływu w próbce (odporny na pojedyncze skoki)
SCALE_PERCENTILE = 0.95
# przewaga w bilansie: (reszta złego znaku − reszta najlepszego) / (2 · Σ|przepływu|) ∈ [0, 1]; 1 = zły znak
# chybia o cały przepływ, 0 = brak różnicy. Połowa możliwej różnicy odsiewa przypadki, w których
# błędy systematyczne (brakujący obwód, straty) są porównywalne z przepływem
MIN_SEPARATION = 0.5
# zgodność: odsetek próbek z wyraźnym przepływem, w których wybrany znak daje mniejszą resztę —
# przewaga nie może wynikać z kilku dużych próbek
MIN_AGREEMENT = 0.8
# domknięcie bilansu: reszta najlepszej kombinacji / przepływ przez instalację (Σ(|PV|+|sieć|+|bat|+|zuż|)/2).
# Straty falownika i brakujący drobny obwód to kilka–kilkanaście %; powyżej 20 % czujniki nie mierzą
# tego, co zakładamy, i żaden znak nie jest wiarygodny
MAX_RESIDUAL_SHARE = 0.2
# PV albo zużycie wyraźnie ujemne (poniżej −tolerancji) w więcej niż 2 % próbek = czujnik mierzy coś
# innego (moc wyjściowa falownika, zużycie liczone złym znakiem) — wynik `ambiguous`
MAX_IMPLAUSIBLE_SHARE = 0.02
# bez zużycia (instalacja bez baterii): wygrywający znak sieci może mieć „ujemne zużycie” w najwyżej 3 %
# próbek nocnych (zmierzch przy przesuniętych uśrednieniach, szum) …
NO_LOAD_MAX_BEST_SHARE = 0.03
# … a zły znak musi mieć co najmniej 10× więcej takich próbek niż dobry (przewaga 1 − dobry/zły ≥ 0,9)
NO_LOAD_MIN_ADVANTAGE = 0.9
# … i co najmniej 12 takich próbek (godzina) — pojedyncze przełączenia nie są dowodem
NO_LOAD_MIN_EVIDENCE = 12

_SIGNS = (1, -1)
_COMBOS: tuple[tuple[int, int], ...] = tuple((sg, sb) for sg in _SIGNS for sb in _SIGNS)


# ── wynik ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignVerdict:
    """Zalecenie dla jednej wielkości. `negate=True` — odczyt czujnika trzeba odwrócić, by spełnić
    konwencję Volcast; `None` — brak zalecenia, powód w `reason`. `samples` — próbki z wyraźnym
    przepływem tej wielkości; `margin` ∈ [0, 1] — przewaga wybranego znaku nad przeciwnym."""
    negate: bool | None
    reason: str | None
    samples: int
    margin: float

    def as_dict(self) -> dict[str, Any]:
        return {"negate": self.negate, "reason": self.reason, "samples": self.samples,
                "margin": round(self.margin, 3)}


@dataclass(frozen=True)
class SignCheck:
    """Wynik oceny: sieć i bateria osobno; `samples` — użyteczne próbki; `residual_share` — domknięcie
    bilansu najlepszej kombinacji (tylko podstawa `balance` z wystarczającą liczbą próbek)."""
    grid: SignVerdict
    battery: SignVerdict
    samples: int
    basis: str
    residual_share: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"grid": self.grid.as_dict(), "battery": self.battery.as_dict(), "samples": self.samples,
                "basis": self.basis,
                "residual_share": None if self.residual_share is None else round(self.residual_share, 3)}


# ── pomocnicze ──────────────────────────────────────────────────────────────

# próbka użyteczna: (pv, sieć, bateria, zużycie | None przy braku czujnika zużycia)
_Row = tuple[float, float, float, float | None]
# powód rozstrzygnięty przed porównaniem kombinacji (albo None)
_Pre = Callable[[str], SignVerdict | None]

def _num(v: Any) -> float | None:
    """Liczba skończona albo None (None, NaN, inf, tekst stanu typu „unavailable”, bool)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _verdict(reason: str | None, negate: bool | None = None, samples: int = 0,
             margin: float = 0.0) -> SignVerdict:
    return SignVerdict(negate=negate, reason=reason, samples=samples, margin=max(0.0, min(1.0, margin)))


def _marginal(cost: Mapping[tuple[int, int], float], axis: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """Najlepsza kombinacja dla każdego znaku wielkości `axis` (0 sieć, 1 bateria) przy dowolnym znaku
    drugiej; zwraca (kombinacja wygrywająca, najlepsza kombinacja z przeciwnym znakiem)."""
    best_for = {s: min((c for c in _COMBOS if c[axis] == s), key=lambda c: cost[c]) for s in _SIGNS}
    plus, minus = best_for[1], best_for[-1]
    return (plus, minus) if cost[plus] <= cost[minus] else (minus, plus)


# ── ocena ───────────────────────────────────────────────────────────────────

def check_signs(samples: Iterable[Mapping[str, Any]], *,
                battery_present: bool | None = None) -> SignCheck:
    """Zalecenie odwrócenia znaku czujnika sieci i baterii na podstawie bilansu (patrz opis modułu).

    Kontrakt wejścia: każda próbka to mapa `{PV|GRID|BATTERY|LOAD: moc w W}`. Czujnik przypisany przez
    użytkownika MUSI mieć klucz w próbkach także wtedy, gdy wartości brak (`None`) — obecność klucza
    w którejkolwiek próbce oznacza „czujnik przypisany”, jego brak „czujnika nie ma” (`no_sensor`, a dla
    zużycia — tryb `load_bound`). Przypisany czujnik bez wartości w danej próbce wyklucza tę próbkę.

    `battery_present` — czy instalacja ma baterię (wie to warstwa HA, nie próbki): `True`, `None` = nie
    wiadomo (domyślnie), `False` — WYŁĄCZNIE gdy instalacja na pewno nie ma żadnej baterii. Liczy się tylko
    bez czujnika zużycia: sieć jest wtedy oceniana jedynie przy `False` i bez przypisanego czujnika mocy
    baterii (przypisany czujnik przy `False` = sprzeczne wejście); inaczej `no_load_sensor`, bo bateria
    oddająca do sieci — niewidoczna albo widoczna tylko częściowo — wygląda jak odwrócony znak sieci.
    Z czujnikiem zużycia bez wpływu.
    """
    rows = list(samples)
    assigned = {q for row in rows for q in QUANTITIES if q in row}
    has = {GRID: GRID in assigned, BATTERY: BATTERY in assigned}
    basis = BASIS_BALANCE if LOAD in assigned else BASIS_LOAD_BOUND

    data: list[_Row] = []
    for row in rows:
        vals: dict[str, float] = {}
        for q in assigned:
            v = _num(row.get(q))
            if v is None:
                break
            vals[q] = v
        else:
            data.append((vals.get(PV, 0.0), vals.get(GRID, 0.0), vals.get(BATTERY, 0.0), vals.get(LOAD)))
    n = len(data)

    def undecided(reason: str) -> SignCheck:
        return SignCheck(grid=_verdict(reason if has[GRID] else NO_SENSOR),
                         battery=_verdict(reason if has[BATTERY] else NO_SENSOR),
                         samples=n, basis=basis)

    if n < MIN_SAMPLES:
        return undecided(TOO_FEW_SAMPLES)

    peaks = sorted(max(abs(p), abs(g), abs(b), abs(l or 0.0)) for p, g, b, l in data)
    scale = peaks[int(SCALE_PERCENTILE * (n - 1))]
    tol = max(FLOW_FLOOR_W, FLOW_SHARE * scale)

    # PV ujemne o mniej niż tolerancja (pobór własny falownika w nocy) = 0; głębiej = czujnik niewiarygodny
    implausible = sum(1 for p, _g, _b, l in data if p < -tol or (l is not None and l < -tol))
    data = [(max(p, 0.0), g, b, l) for p, g, b, l in data]
    suspect = implausible > MAX_IMPLAUSIBLE_SHARE * n

    active = {GRID: [i for i, d in enumerate(data) if abs(d[1]) >= tol],
              BATTERY: [i for i, d in enumerate(data) if abs(d[2]) >= tol]}
    idle_reason = {GRID: GRID_IDLE, BATTERY: BATTERY_IDLE}

    def pre(q: str) -> SignVerdict | None:
        """Powód rozstrzygnięty przed porównaniem kombinacji (brak czujnika, spoczynek, zły czujnik)."""
        if not has[q]:
            return _verdict(NO_SENSOR)
        if len(active[q]) < MIN_ACTIVE_SAMPLES:
            return _verdict(idle_reason[q], samples=len(active[q]))
        if suspect:
            return _verdict(AMBIGUOUS, samples=len(active[q]))
        return None

    if basis == BASIS_BALANCE:
        return _by_balance(data, active, pre, n)
    # bez zużycia sieć tylko w instalacji na pewno bez baterii; przypisany czujnik baterii przy
    # `battery_present=False` to sprzeczne wejście — też bez oceny
    battery_free = battery_present is False and not has[BATTERY]
    return _by_load_bound(data, active, pre, n, tol, battery_free)


def _by_balance(data: list[_Row], active: Mapping[str, list[int]], pre: _Pre, n: int) -> SignCheck:
    res = {c: [p + c[0] * g + c[1] * b - l for p, g, b, l in data] for c in _COMBOS}
    err = {c: sum(abs(r) for r in rs) for c, rs in res.items()}
    throughput = sum(abs(p) + abs(g) + abs(b) + abs(l) for p, g, b, l in data) / 2
    best = min(_COMBOS, key=lambda c: err[c])
    share = err[best] / throughput if throughput > 0 else 0.0

    def judge(q: str, axis: int) -> SignVerdict:
        known = pre(q)
        if known is not None:
            return known
        idx = active[q]
        win, alt = _marginal(err, axis)
        flow = sum(abs(d[axis + 1]) for d in data)
        separation = (err[alt] - err[win]) / (2 * flow) if flow > 0 else 0.0
        agree = sum(1 for i in idx if abs(res[win][i]) < abs(res[alt][i])) / len(idx)
        if share > MAX_RESIDUAL_SHARE or separation < MIN_SEPARATION or agree < MIN_AGREEMENT:
            return _verdict(AMBIGUOUS, samples=len(idx), margin=separation)
        return _verdict(None, negate=win[axis] == -1, samples=len(idx), margin=separation)

    return SignCheck(grid=judge(GRID, 0), battery=judge(BATTERY, 1), samples=n,
                     basis=BASIS_BALANCE, residual_share=share)


def _by_load_bound(data: list[_Row], active: Mapping[str, list[int]], pre: _Pre, n: int,
                   tol: float, battery_free: bool) -> SignCheck:
    def judge_grid() -> SignVerdict:
        known = pre(GRID)
        if known is not None:
            return known
        samples = len(active[GRID])
        # bateria — przypisana czy nie, cała czy częściowo widoczna — oddająca do sieci jest
        # nieodróżnialna od odwróconego znaku sieci
        if not battery_free:
            return _verdict(NO_LOAD_SENSOR, samples=samples)
        # „noc” ma sens tylko przy czujniku PV, który widać produkuje (nie pusty, nie kW podane jako W)
        if sum(1 for p, _g, _b, _l in data if p >= tol) < MIN_ACTIVE_SAMPLES:
            return _verdict(NO_LOAD_SENSOR, samples=samples)
        # dowody tylko z nocy (|PV| < tolerancji): bez baterii żadne źródło nie pracuje, więc dom może
        # wyłącznie pobierać. Niewidoczna część produkcji (jeden string, mikroinwertery poza czujnikiem)
        # jest wtedy też ≈ 0, a w dzień pozwalałaby „oddawać” ponad widoczne PV przy poprawnym znaku.
        rows = [g for p, g, _b, _l in data if p < tol]
        # liczba próbek z fizycznie niemożliwym („ujemnym”) zużyciem dla każdego znaku sieci
        bad = {s: sum(1 for g in rows if s * g < -tol) for s in _SIGNS}
        win, alt = (1, -1) if bad[1] <= bad[-1] else (-1, 1)
        advantage = 1 - bad[win] / bad[alt] if bad[alt] else 0.0
        if bad[alt] < NO_LOAD_MIN_EVIDENCE:
            return _verdict(NO_LOAD_SENSOR, samples=samples, margin=advantage)
        if bad[win] > NO_LOAD_MAX_BEST_SHARE * len(rows) or advantage < NO_LOAD_MIN_ADVANTAGE:
            return _verdict(AMBIGUOUS, samples=samples, margin=advantage)
        return _verdict(None, negate=win == -1, samples=samples, margin=advantage)

    def judge_battery() -> SignVerdict:
        # bez zużycia baterii nie oceniamy wcale (opis modułu: bateria widoczna tylko częściowo)
        known = pre(BATTERY)
        return known if known is not None else _verdict(NO_LOAD_SENSOR, samples=len(active[BATTERY]))

    return SignCheck(grid=judge_grid(), battery=judge_battery(), samples=n, basis=BASIS_LOAD_BOUND)
