"""Ocena znaku mocy sieci i baterii z bilansu na syntetycznych dobach (bez HA)."""
import json
import math
import random

import pytest

from custom_components.volcast.core.discovery.sign_check import (
    BATTERY,
    GRID,
    LOAD,
    MIN_SAMPLES,
    PV,
    check_signs,
)

SIZES = (3, 10, 50)                      # kWp: mała / średnia / duża instalacja
COMBOS = [(False, False), (True, False), (False, True), (True, True)]
STEPS = 288                              # doba statystyk 5-minutowych


# ── generator doby (konwencja Volcast: sieć + = pobór, bateria + = rozładowanie) ──

def make_day(kwp: float, *, seed: int, battery: bool = True, battery_idle: bool = False,
             zero_grid: bool = False, battery_kwh_per_kwp: float = 1.0,
             battery_kw_per_kwp: float = 0.5, soc0: float = 0.3, cloudy: float = 0.3,
             battery_first: bool = False) -> dict:
    """Doba: PV (sinus dnia z chmurami), zużycie (baza, rano, wieczór, skoki czajnika), bateria
    autokonsumpcyjna (ładuje nadwyżkę w południe, rozładowuje wieczorem), sieć = reszta bilansu.
    `zero_grid` — falownik z ograniczeniem eksportu: nadwyżki PV obcinane, sieć ≈ 0.
    `battery_first` — tryb „najpierw bateria”: PV ładuje baterię przed zasileniem domu (dom z sieci)."""
    rng = random.Random(seed)
    cap = kwp * 1000 * battery_kwh_per_kwp
    pmax = kwp * 1000 * battery_kw_per_kwp
    soc = cap * soc0
    cloud = 1.0
    pv, grid, bat, load = [], [], [], []
    for i in range(STEPS):
        h = i / 12
        cloud = min(1.0, max(1.0 - cloudy, cloud + rng.gauss(0, cloudy / 4)))
        sun = math.sin(math.pi * (h - 6) / 14) if 6 < h < 20 else 0.0
        p = kwp * 1000 * 0.85 * max(sun, 0.0) ** 1.2 * cloud
        base = 120 + 40 * kwp
        l_ = base + (100 * kwp if 6 <= h < 8 else 0) + (150 * kwp if 17 <= h < 22 else 0)
        if rng.random() < 0.03:
            l_ += 200 * kwp
        b = 0.0
        if battery and not battery_idle:
            surplus = p - l_
            if battery_first and p > 0 and soc < cap:
                b = -min(p, pmax, (cap - soc) * 12)
            elif surplus > 0:
                b = -min(surplus, pmax, (cap - soc) * 12)
            else:
                b = min(-surplus, pmax, soc * 12)
            soc -= b / 12
        if zero_grid:
            p = min(p, l_ - b)           # obcięcie PV do zużycia + ładowania
        g = l_ - p - b
        if zero_grid and g < 0:
            g = 0.0
        pv.append(p if p > 0 else -3.0)  # falownik w nocy: kilka W poniżej zera
        grid.append(g)
        bat.append(b)
        load.append(l_)
    return {"pv": pv, "grid": grid, "battery": bat, "load": load, "battery_present": battery}


def raw_samples(day: dict, *, grid_negate: bool = False, battery_negate: bool = False,
                rel_noise: float = 0.01, abs_noise: float = 10.0, seed: int = 0,
                load: bool = True, shift: dict | None = None) -> list[dict]:
    """Odczyty czujników w ich własnej konwencji znaku, z szumem; `shift` = {klucz: próbki opóźnienia}."""
    rng = random.Random(seed + 1000)
    series = {
        PV: day["pv"],
        GRID: [-v if grid_negate else v for v in day["grid"]],
        LOAD: day["load"],
    }
    if day["battery_present"]:
        series[BATTERY] = [-v if battery_negate else v for v in day["battery"]]
    if not load:
        del series[LOAD]
    for key, lag in (shift or {}).items():
        s = series[key]
        series[key] = [s[max(0, i - lag)] for i in range(len(s))]
    out = []
    for i in range(STEPS):
        row = {}
        for key, s in series.items():
            v = s[i]
            row[key] = v * (1 + rng.gauss(0, rel_noise)) + rng.gauss(0, abs_noise)
        out.append(row)
    return out


def _expect(res, grid: bool | None, battery: bool | None) -> None:
    assert (res.grid.negate, res.battery.negate) == (grid, battery), res.as_dict()


# ── cztery kombinacje znaku × macierz instalacji ────────────────────────────────

@pytest.mark.parametrize("kwp", SIZES)
@pytest.mark.parametrize("grid_negate,battery_negate", COMBOS)
def test_four_sign_combinations_across_installation_sizes(kwp, grid_negate, battery_negate):
    day = make_day(kwp, seed=7)
    res = check_signs(raw_samples(day, grid_negate=grid_negate, battery_negate=battery_negate, seed=7))
    _expect(res, grid_negate, battery_negate)
    assert res.grid.reason is None and res.battery.reason is None
    assert res.grid.margin > 0.8 and res.battery.margin > 0.8
    assert res.samples == STEPS
    assert res.grid.samples >= 12 and res.battery.samples >= 12


@pytest.mark.parametrize("kwp", SIZES)
@pytest.mark.parametrize("grid_negate", [False, True])
def test_no_battery_checks_grid_only(kwp, grid_negate):
    day = make_day(kwp, seed=3, battery=False)
    res = check_signs(raw_samples(day, grid_negate=grid_negate, seed=3))
    _expect(res, grid_negate, None)
    assert res.battery.reason == "no_sensor"
    assert res.battery.samples == 0


@pytest.mark.parametrize("kwp", SIZES)
def test_idle_battery_does_not_block_the_grid(kwp):
    day = make_day(kwp, seed=11, battery_idle=True)
    res = check_signs(raw_samples(day, grid_negate=True, seed=11))
    _expect(res, True, None)
    assert res.battery.reason == "battery_idle"


@pytest.mark.parametrize("kwp", SIZES)
@pytest.mark.parametrize("battery_negate", [False, True])
def test_export_limited_inverter_leaves_grid_idle_but_checks_battery(kwp, battery_negate):
    day = make_day(kwp, seed=5, zero_grid=True, battery_kwh_per_kwp=4, battery_kw_per_kwp=3, soc0=0.9)
    res = check_signs(raw_samples(day, battery_negate=battery_negate, seed=5))
    _expect(res, None, battery_negate)
    assert res.grid.reason == "grid_idle"


# ── odporność: szum, przesunięcie uśrednień, NaN/inf, PV ujemne w nocy ─────────

@pytest.mark.parametrize("kwp", SIZES)
@pytest.mark.parametrize("grid_negate,battery_negate", COMBOS)
def test_noisy_sensors_still_decide(kwp, grid_negate, battery_negate):
    day = make_day(kwp, seed=21)
    res = check_signs(raw_samples(day, grid_negate=grid_negate, battery_negate=battery_negate,
                                  rel_noise=0.04, abs_noise=40, seed=21))
    _expect(res, grid_negate, battery_negate)


@pytest.mark.parametrize("kwp", SIZES)
@pytest.mark.parametrize("lagged", [GRID, LOAD, BATTERY])
def test_one_sample_averaging_shift_still_decides(kwp, lagged):
    day = make_day(kwp, seed=13, cloudy=0.5)
    res = check_signs(raw_samples(day, grid_negate=True, battery_negate=True, seed=13, shift={lagged: 1}))
    _expect(res, True, True)


def test_nan_inf_none_and_text_samples_are_skipped():
    day = make_day(10, seed=17)
    rows = raw_samples(day, grid_negate=True, seed=17)
    rng = random.Random(99)
    spoiled = 0
    for row in rows:
        if rng.random() < 0.1:
            row[rng.choice([PV, GRID, BATTERY, LOAD])] = rng.choice(
                [None, float("nan"), float("inf"), float("-inf"), "unavailable"])
            spoiled += 1
    res = check_signs(rows)
    _expect(res, True, False)
    assert res.samples == STEPS - spoiled


def test_pv_slightly_negative_at_night_is_tolerated():
    day = make_day(3, seed=2)
    rows = raw_samples(day, seed=2)
    for row in rows:
        if row[PV] < 50:
            row[PV] = -8.0
    _expect(check_signs(rows), False, False)


def test_heavy_noise_never_gives_a_wrong_recommendation():
    """Konserwatyzm: przy dowolnym szumie i przesunięciu zalecenie jest trafne albo go nie ma."""
    decided = {}
    for rel, absolute in ((0.04, 0.01), (0.08, 0.02), (0.12, 0.03)):
        decided[rel] = 0
        for kwp in SIZES:
            for seed in range(6):
                for gn, bn in COMBOS:
                    day = make_day(kwp, seed=seed, cloudy=0.6)
                    rows = raw_samples(day, grid_negate=gn, battery_negate=bn, rel_noise=rel,
                                       abs_noise=absolute * kwp * 1000, seed=seed, shift={GRID: 1})
                    res = check_signs(rows)
                    assert res.grid.negate in (gn, None), res.as_dict()
                    assert res.battery.negate in (bn, None), res.as_dict()
                    decided[rel] += (res.grid.negate is not None) + (res.battery.negate is not None)
    # umiarkowany szum nadal rozstrzyga; skrajny kończy się brakiem zalecenia, nie zgadywaniem
    assert decided[0.04] > 0


# ── za mało próbek, zapasowa próbka bieżąca ────────────────────────────────────

def test_single_current_sample_is_too_few():
    res = check_signs([{PV: 2000.0, GRID: -1500.0, BATTERY: 0.0, LOAD: 500.0}])
    _expect(res, None, None)
    assert res.grid.reason == res.battery.reason == "too_few_samples"
    assert res.samples == 1


def test_short_history_is_too_few():
    rows = raw_samples(make_day(10, seed=1), seed=1)[: MIN_SAMPLES - 1]
    res = check_signs(rows)
    assert res.grid.reason == "too_few_samples" and res.battery.reason == "too_few_samples"


def _two_phase_rows(n: int, *, import_w: float = 500.0) -> list[dict]:
    """Połowa próbek: noc z poborem; połowa: dzień z oddawaniem. Sieć „eksport dodatni”."""
    rows = []
    for i in range(n):
        if i % 2:
            rows.append({PV: 0.0, GRID: -import_w, LOAD: import_w})
        else:
            rows.append({PV: 2000.0, GRID: 1500.0, LOAD: 500.0})
    return rows


def test_exactly_min_samples_is_enough():
    """Granica: 2 h statystyk 5-minutowych (24 próbki) wystarcza, 23 nie."""
    assert check_signs(_two_phase_rows(23)).grid.reason == "too_few_samples"
    res = check_signs(_two_phase_rows(24))
    assert res.grid.negate is True and res.samples == 24


@pytest.mark.parametrize("active,expected", [(11, None), (12, False)])
def test_an_hour_of_battery_flow_is_needed(active, expected):
    """Granica: bateria z przepływem w 11 próbkach to spoczynek, w 12 (godzina) już oceniana."""
    rows = [{**row, BATTERY: 0.0} for row in _two_phase_rows(40)]
    for i in range(active):
        rows[i] = {PV: 0.0, GRID: -200.0, BATTERY: 1000.0, LOAD: 1200.0}
    res = check_signs(rows)
    assert res.battery.negate is expected
    assert res.battery.reason == ("battery_idle" if expected is None else None)


def test_flow_below_the_absolute_floor_is_idle():
    """Mała instalacja: przepływ 90 W to jeszcze szum (próg 100 W), 110 W już przepływ."""
    rows = [{PV: 0.0, GRID: -90.0, LOAD: 90.0} for _ in range(30)]
    assert check_signs(rows).grid.reason == "grid_idle"
    rows = [{PV: 0.0, GRID: -110.0, LOAD: 110.0} for _ in range(30)]
    assert check_signs(rows).grid.negate is True


def test_flow_threshold_scales_with_the_installation_and_ignores_spikes():
    """Duża instalacja (skala 50 kW → próg 1,5 kW): bateria 1 kW to spoczynek. Jedna zepsuta próbka
    10 MW nie podnosi skali (95. percentyl), więc sieć nadal jest oceniana."""
    rows = [{PV: 50000.0, GRID: 47000.0, BATTERY: -1000.0, LOAD: 2000.0} for _ in range(20)]
    rows += [{PV: 0.0, GRID: -1000.0, BATTERY: 1000.0, LOAD: 2000.0} for _ in range(20)]
    rows.append({PV: 0.0, GRID: -1e7, BATTERY: 0.0, LOAD: 1e7})
    res = check_signs(rows)
    assert res.battery.negate is None and res.battery.reason == "battery_idle"
    assert res.grid.negate is True


def test_empty_input():
    res = check_signs([])
    assert res.samples == 0
    assert res.grid.reason == "no_sensor" and res.battery.reason == "no_sensor"


def test_assigned_sensor_without_any_value_makes_samples_unusable():
    rows = raw_samples(make_day(10, seed=4), seed=4)
    for row in rows:
        row[BATTERY] = None
    res = check_signs(rows)
    assert res.samples == 0
    assert res.grid.reason == "too_few_samples" and res.battery.reason == "too_few_samples"


# ── przypadki zwodnicze ────────────────────────────────────────────────────────

@pytest.mark.parametrize("grid_negate,battery_negate", COMBOS)
def test_load_computed_by_the_integration_closes_for_one_combination(grid_negate, battery_negate):
    """Zużycie liczone przez integrację z jej własnych odczytów: bilans domyka się dokładnie tylko
    dla konwencji integracji — to poprawny sygnał."""
    day = make_day(10, seed=8)
    rows = raw_samples(day, grid_negate=grid_negate, battery_negate=battery_negate, seed=8)
    sg = -1 if grid_negate else 1
    sb = -1 if battery_negate else 1
    for row in rows:
        row[LOAD] = row[PV] + sg * row[GRID] + sb * row[BATTERY]
    res = check_signs(rows)
    _expect(res, grid_negate, battery_negate)
    assert res.residual_share < 0.01


def test_load_computed_with_the_wrong_sign_is_rejected():
    """Integracja liczy zużycie naiwnie (PV + sieć + bateria) z sieci „eksport dodatni”: bilans
    domyka się dla złej kombinacji, ale zużycie wychodzi ujemne — dane niewiarygodne."""
    day = make_day(10, seed=8)
    rows = raw_samples(day, grid_negate=True, seed=8)
    for row in rows:
        row[LOAD] = row[PV] + row[GRID] + row[BATTERY]
    res = check_signs(rows)
    _expect(res, None, None)
    assert res.grid.reason == res.battery.reason == "ambiguous"


def _inverter_output_as_pv(day: dict, rows: list[dict]) -> None:
    """Jako PV przypisano moc wyjściową falownika hybrydowego (PV + bateria) — wielkość fizyczna,
    niezależna od konwencji znaku czujnika baterii."""
    for i, row in enumerate(rows):
        row[PV] = max(day["pv"][i], 0.0) + day["battery"][i]


@pytest.mark.parametrize("mode", ["self_consumption", "battery_first", "grid_charging"])
def test_pv_sensor_reading_inverter_output_never_gives_a_wrong_recommendation(mode):
    """Moc wyjściowa falownika jako PV: bateria liczona podwójnie (reszta ±bateria dla obu znaków).
    Macierz 3/10/50 kW × 6 seedów × 4 kombinacje: bateria nigdy z zaleceniem, sieć trafna albo bez."""
    for kwp in SIZES:
        for seed in range(6):
            day = make_day(kwp, seed=seed, battery_first=mode == "battery_first")
            if mode == "grid_charging":
                for i in range(0, 36):   # 0:00–3:00 ładowanie z sieci 0,3 kW/kWp
                    day["battery"][i] = -300.0 * kwp
                    day["grid"][i] = day["load"][i] + 300.0 * kwp
            for gn, bn in COMBOS:
                rows = raw_samples(day, grid_negate=gn, battery_negate=bn, seed=seed)
                _inverter_output_as_pv(day, rows)
                res = check_signs(rows)
                assert res.battery.negate is None, (kwp, seed, gn, bn, res.as_dict())
                assert res.grid.negate in (gn, None), (kwp, seed, gn, bn, res.as_dict())


EXPORT_WINDOWS = {"peak": range(204, 216), "night": range(12, 24)}   # 17:00–18:00, 1:00–2:00


def _battery_export(day: dict, window: range, watts: float) -> None:
    """Bateria sprzedaje do sieci przez godzinę (rozładowanie ponad zużycie)."""
    for i in window:
        day["battery"][i] = watts
        day["grid"][i] = day["load"][i] - max(day["pv"][i], 0.0) - watts


# „PV” mierzy coś innego niż całą produkcję: f(produkcja, bateria) albo None = czujnik nieprzypisany
PV_KINDS = {
    "production": lambda p, b: p,
    "inverter_output": lambda p, b: p + b,
    "half_of_strings": lambda p, b: 0.5 * p,
    "microinverters_unseen": lambda p, b: 0.6 * p,
    "kw_reported_as_w": lambda p, b: p / 1000,
    "w_reported_as_kw": lambda p, b: p * 1000,
    "reversed": lambda p, b: -p,
    "unassigned": None,
}
DAY_KINDS = {
    "self_consumption": {},
    "night_import": {"battery_kwh_per_kwp": 0.5, "soc0": 0.1},
    "battery_first": {"battery_first": True},
    "export_peak": {"battery_kwh_per_kwp": 3, "battery_kw_per_kwp": 1, "soc0": 0.8, "export": "peak"},
    "export_night": {"battery_kwh_per_kwp": 3, "battery_kw_per_kwp": 1, "soc0": 0.8, "export": "night"},
    "no_battery": {"battery": False},
}


def _day(kind: str, kwp: float, seed: int) -> dict:
    kw = dict(DAY_KINDS[kind])
    export = kw.pop("export", None)
    day = make_day(kwp, seed=seed, **kw)
    if export:
        _battery_export(day, EXPORT_WINDOWS[export], 500.0 * kwp)
    return day


# ── brak czujnika zużycia: żadnych zaleceń ─────────────────────────────────────

@pytest.mark.parametrize("day_kind", sorted(DAY_KINDS))
@pytest.mark.parametrize("pv_kind", ["production", "inverter_output", "half_of_strings", "unassigned"])
def test_no_load_sensor_never_gives_a_recommendation(day_kind, pv_kind):
    """Bez czujnika zużycia dane są nieodróżnialne od odwróconego znaku (bateria nieprzypisana albo
    widoczna częściowo, PV widzące część produkcji, inne źródło poza pomiarem) — żadnych zaleceń, także
    przy czujniku baterii zablokowanym na 0. Macierz 3/10/50 kW × 2 seedy × kombinacje znaków."""
    pv_of = PV_KINDS[pv_kind]
    for kwp in SIZES:
        for seed in range(2):
            day = _day(day_kind, kwp, seed)
            for gn, bn in COMBOS:
                for stuck in (False, True) if day["battery_present"] else (False,):
                    rows = raw_samples(day, grid_negate=gn, battery_negate=bn, seed=seed, load=False)
                    for i, row in enumerate(rows):
                        if stuck:
                            row[BATTERY] = 0.0
                        if pv_of is None:
                            del row[PV]
                        else:
                            row[PV] = pv_of(max(day["pv"][i], 0.0), day["battery"][i])
                    res = check_signs(rows)
                    _expect(res, None, None)
                    assert res.grid.reason == "no_load_sensor"
                    assert res.battery.reason == ("no_load_sensor" if day["battery_present"] else "no_sensor")


def test_no_load_sensor_keeps_the_more_specific_reasons():
    """`too_few_samples` i `no_sensor` mają pierwszeństwo przed `no_load_sensor`."""
    rows = raw_samples(make_day(10, seed=1), seed=1, load=False)
    res = check_signs(rows[: MIN_SAMPLES - 1])
    assert res.grid.reason == res.battery.reason == "too_few_samples"
    for row in rows:
        del row[BATTERY]
    res = check_signs(rows)
    assert (res.grid.reason, res.battery.reason) == ("no_load_sensor", "no_sensor")
    assert res.residual_share is None


@pytest.mark.parametrize("day_kind", ["self_consumption", "night_import", "export_peak", "export_night"])
@pytest.mark.parametrize("pv_kind", sorted(PV_KINDS))
def test_unassigned_battery_with_load_sensor_never_gives_a_wrong_grid_recommendation(day_kind, pv_kind):
    """Z czujnikiem zużycia bateria nieprzypisana psuje bilans (brakuje jej przepływu) — wynik
    `ambiguous` albo trafny (np. moc wyjściowa falownika jako PV zawiera baterię), nigdy zły."""
    pv_of = PV_KINDS[pv_kind]
    for kwp in SIZES:
        for seed in range(3):
            day = _day(day_kind, kwp, seed)
            for gn in (False, True):
                rows = raw_samples(day, grid_negate=gn, seed=seed)
                for i, row in enumerate(rows):
                    del row[BATTERY]
                    if pv_of is None:
                        del row[PV]
                    else:
                        row[PV] = pv_of(max(day["pv"][i], 0.0), day["battery"][i])
                res = check_signs(rows)
                assert res.grid.negate in (gn, None), (kwp, seed, gn, res.as_dict())


def test_inverter_output_as_pv_with_load_sensor_is_ambiguous_for_the_battery():
    """Z czujnikiem zużycia reszta bilansu to ±bateria dla obu znaków — `ambiguous`."""
    day = make_day(10, seed=12)
    rows = raw_samples(day, battery_negate=True, seed=12)
    _inverter_output_as_pv(day, rows)
    res = check_signs(rows)
    assert res.battery.negate is None and res.battery.reason == "ambiguous"


def test_reversed_pv_sensor_is_ambiguous():
    day = make_day(10, seed=16)
    rows = raw_samples(day, grid_negate=True, seed=16)
    for row in rows:
        row[PV] = -row[PV]
    res = check_signs(rows)
    _expect(res, None, None)
    assert res.grid.reason == res.battery.reason == "ambiguous"


def test_few_large_samples_cannot_outvote_the_rest():
    """Przewagę w sumie dają 10 dużych próbek, a 30 pozostałych (czujnik zużycia gubi 400 W przy
    małym przepływie) wskazuje znak przeciwny — brak zalecenia."""
    rows = [{PV: 3000.0, GRID: 200.0, LOAD: 2800.0} for _ in range(30)]
    rows += [{PV: 0.0, GRID: 5000.0, LOAD: 4600.0} for _ in range(10)]
    res = check_signs(rows)
    assert res.grid.negate is None and res.grid.reason == "ambiguous"
    assert res.grid.margin >= 0.5       # sama przewaga w sumie by wystarczyła
    assert res.residual_share <= 0.2


def test_battery_charged_only_from_grid_is_ambiguous():
    """Tylko nocne ładowanie z sieci: sieć ≈ −bateria, więc odwrócenie obu znaków prawie domyka
    bilans — żadnego zalecenia."""
    rng = random.Random(1)
    rows = []
    for _ in range(60):
        charge = 2000 + rng.gauss(0, 20)
        load = 200 + rng.gauss(0, 10)
        rows.append({PV: 0.0, GRID: load + charge, BATTERY: -charge, LOAD: load})
    res = check_signs(rows)
    _expect(res, None, None)
    assert res.grid.reason == res.battery.reason == "ambiguous"


def test_load_sensor_missing_a_circuit_still_decides():
    """Czujnik zużycia nie widzi stałego obwodu 300 W (np. pompa poza falownikiem)."""
    day = make_day(10, seed=14)
    rows = raw_samples(day, grid_negate=True, battery_negate=True, seed=14)
    for row in rows:
        row[LOAD] -= 300
    _expect(check_signs(rows), True, True)


def test_unrelated_load_sensor_is_ambiguous():
    """Jako zużycie wybrano coś niezwiązanego (stałe 50 W): bilans się nie domyka."""
    day = make_day(10, seed=15)
    rows = raw_samples(day, grid_negate=True, seed=15)
    for row in rows:
        row[LOAD] = 50.0
    res = check_signs(rows)
    assert res.grid.negate in (True, None) and res.battery.negate in (False, None)
    assert res.residual_share > 0.2
    _expect(res, None, None)


# ── wynik jako JSON ────────────────────────────────────────────────────────────

def test_as_dict_is_json_serialisable():
    res = check_signs(raw_samples(make_day(3, seed=7), grid_negate=True, seed=7))
    d = res.as_dict()
    json.dumps(d)
    assert set(d) == {"grid", "battery", "samples", "residual_share"}
    assert set(d["grid"]) == {"negate", "reason", "samples", "margin"}
    assert d["grid"]["negate"] is True and d["battery"]["negate"] is False
    assert 0.0 <= d["grid"]["margin"] <= 1.0


def test_as_dict_for_too_few_samples():
    d = check_signs([]).as_dict()
    json.dumps(d)
    assert d["residual_share"] is None
    assert d["grid"] == {"negate": None, "reason": "no_sensor", "samples": 0, "margin": 0.0}
