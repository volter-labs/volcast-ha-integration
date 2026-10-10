"""Limity wykonawcy (`driver.limits`): moc znamionowa z nazwy modelu albo rejestru + korekta ręczna,
moc ładowania/rozładowania z limitów prądu BMS i pojemność baterii z rejestrów profilu.

Górna granica mocy to ta sama stała, którą sprawdzają strażnicy (`MAX_POWER_W`) —
jedno źródło; falownik powyżej niej nie jest obsługiwany.

Limity z BMS (kontrakt sygnałów §6.1, `_shared/executor-limits.ts` w chmurze):
`max_charge_w = bms_charge_current_max_a × battery_voltage_v`, tak samo rozładowanie, przycięte do mocy
znamionowej i zaokrąglone W DÓŁ do pełnych `BMS_POWER_STEP_W` (napięcie baterii pływa co odczyt — bez
kroku limit zmieniałby się w każdej telemetrii, a chmura zapisuje go przy każdej zmianie). Wartość
nieprawdopodobna albo brak odczytu = brak pola (nigdy 0 ani śmieci). Sensowne zakresy:
- prąd BMS `BMS_CURRENT_RANGE_A` 1–1000 A (baterie HV ~10–50 A, LV 48 V do kilkuset A); 0 A (pełna
  bateria, blokada BMS) to stan chwilowy, nie zdolność baterii — pomijamy;
- napięcie baterii `BATTERY_VOLTAGE_RANGE_V` 40–1000 V (od najniższych LV 48 V do najwyższych HV);
- iloczyn w zakresie mocy limitu 100 W – `MAX_POWER_W`; pojemność `BATTERY_CAPACITY_RANGE_KWH`.

BMS obniża prąd przy wysokim SoC i w zimnie, a planer potrzebuje zdolności baterii, nie chwilowego
ograniczenia. Dlatego próbkę mocy ładowania bierzemy tylko przy SoC < `CHARGE_SAMPLE_SOC_MAX`, próbkę
rozładowania tylko przy SoC > `DISCHARGE_SAMPLE_SOC_MIN` (nieznane SoC = brak próbki), a `PeakLimit` trzyma
szczyt takich próbek z ostatniej doby (kubełki godzinowe). Szczyt żyje w pamięci: po restarcie HA albo
przeładowaniu opcji (także przy pierwszym parowaniu) pola nie ma, dopóki nie przyjdzie próbka z pasma —
chmura bierze wtedy moc z konfiguracji. Za niski limit szkodzi planowi bardziej niż za wysoki (za wysoki
tnie moc znamionowa i sam BMS), więc wartości z dławionej próbki nie wysyłamy nigdy.

Te limity trafiają wyłącznie do chmury (planer); lokalny tor zapisu ich nie czyta.
"""
from __future__ import annotations

import math
import re
from typing import Mapping

from ..guards import MAX_POWER_W

_KW = re.compile(r"(?<![\d.])(\d{1,2}(?:[.,]\d)?)K(?!WH)")
_W_MIN, _W_MAX = 100.0, MAX_POWER_W
_RATED_MIN, _RATED_MAX = 1_000.0, MAX_POWER_W
# Zakres mocy znamionowej (W) — także dla pola w opcjach integracji.
RATED_POWER_RANGE_W = (_RATED_MIN, _RATED_MAX)
_KWH_MIN, _KWH_MAX = 0.5, 200.0
# Zakres pojemności baterii (kWh) — także dla pola w opcjach integracji.
BATTERY_CAPACITY_RANGE_KWH = (_KWH_MIN, _KWH_MAX)
# Dozwolone źródła limitów w bloku `driver.limits` (kontrakt telemetrii).
LIMIT_SOURCES = frozenset({"profile", "registers", "entities", "user"})
# Limity z BMS: zakresy sensowności i krok mocy (patrz docstring modułu).
BMS_CURRENT_RANGE_A = (1.0, 1000.0)
BATTERY_VOLTAGE_RANGE_V = (40.0, 1000.0)
BMS_POWER_STEP_W = 100
# Pasmo SoC, w którym BMS nie dławi prądu: powyżej ok. 80 % BMS obniża dopuszczalny prąd ładowania
# (ramka z GW8KN-ET: 9 A przy 83 % zamiast ok. 25 A), poniżej ok. 20 % — prąd rozładowania. Próbka spoza
# pasma to chwilowe ograniczenie, nie zdolność baterii — do szczytu nie wchodzi.
CHARGE_SAMPLE_SOC_MAX = 80
DISCHARGE_SAMPLE_SOC_MIN = 20
# Pole mocy → czy SoC pozwala wziąć próbkę (SoC już sprawdzone jako liczba 0–100).
_SAMPLE_BAND = {"max_charge_w": lambda soc: soc < CHARGE_SAMPLE_SOC_MAX,
                "max_discharge_w": lambda soc: soc > DISCHARGE_SAMPLE_SOC_MIN}
# Okno szczytu limitu BMS i szerokość kubełka.
PEAK_WINDOW_S = 24 * 3600.0
_PEAK_BUCKET_S = 3600.0
# Pola mocy liczone z prądu BMS: pole limitu → pole `$.limits` profilu wskazujące klucz `$.read`.
BMS_POWER_FIELDS = {"max_charge_w": "max_charge_register", "max_discharge_w": "max_discharge_register"}


def rated_power_from_model(model: str | None) -> float | None:
    if not isinstance(model, str):
        return None
    m = _KW.search(model)
    if m is None:
        return None
    watts = float(round(float(m.group(1).replace(",", ".")) * 1000.0))
    return watts if _RATED_MIN <= watts <= _RATED_MAX else None


def _ok(v, lo: float, hi: float) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and lo <= v <= hi)


def bms_power_w(current_a, voltage_v, rated_power_w=None) -> float | None:
    """Moc limitu z prądu BMS × napięcie baterii; przycięta do mocy znamionowej (gdy sensowna),
    w dół do `BMS_POWER_STEP_W`. None = odczyt nieprawdopodobny albo moc poza zakresem limitu."""
    if not (_ok(current_a, *BMS_CURRENT_RANGE_A) and _ok(voltage_v, *BATTERY_VOLTAGE_RANGE_V)):
        return None
    watts = float(current_a) * float(voltage_v)
    if _ok(rated_power_w, _RATED_MIN, _RATED_MAX):
        watts = min(watts, float(rated_power_w))
    watts = math.floor(watts / BMS_POWER_STEP_W) * BMS_POWER_STEP_W
    return float(watts) if _W_MIN <= watts <= _W_MAX else None


def executor_limits(*, rated_power_w: float | None, battery_capacity_kwh: float | None = None,
                    max_charge_w: float | None = None, max_discharge_w: float | None = None,
                    source: str = "entities") -> dict | None:
    """Blok `driver.limits`; pole spoza zakresu odpada."""
    if source not in LIMIT_SOURCES:
        raise ValueError(f"unknown limits source: {source!r}")
    out: dict = {}
    if _ok(rated_power_w, _RATED_MIN, _RATED_MAX):
        out["rated_power_w"] = round(rated_power_w)
    for key, v in (("max_charge_w", max_charge_w), ("max_discharge_w", max_discharge_w)):
        if _ok(v, _W_MIN, _W_MAX):
            out[key] = round(v)
    if _ok(battery_capacity_kwh, _KWH_MIN, _KWH_MAX):
        out["battery_capacity_kwh"] = round(battery_capacity_kwh, 2)
    if not out:
        return None
    out["source"] = source
    return out


class PeakLimit:
    """Najwyższa moc limitu z ostatniej doby (kubełki godzinowe). Próbka None albo nieprawdopodobna
    nie kasuje szczytu; po dobie bez sensownej próbki — None."""

    def __init__(self, window_s: float = PEAK_WINDOW_S, bucket_s: float = _PEAK_BUCKET_S) -> None:
        self._n = max(1, int(round(window_s / bucket_s)))
        self._bucket_s = bucket_s
        self._buckets: dict[int, float] = {}

    def _index(self, now_s: float) -> int:
        return math.floor(now_s / self._bucket_s)

    def observe(self, value, now_s: float) -> None:
        if not _ok(value, _W_MIN, _W_MAX):
            return
        i = self._index(now_s)
        self._buckets[i] = max(self._buckets.get(i, 0.0), float(value))
        # Przycinamy względem „teraz", nie najnowszego kubełka: po skoku zegara do przodu i korekcie
        # kubełki „z przyszłości" znikają, a bieżące próbki zostają.
        for k in [k for k in self._buckets if k > i or k <= i - self._n]:
            del self._buckets[k]

    def peak(self, now_s: float) -> float | None:
        i = self._index(now_s)
        live = [v for k, v in self._buckets.items() if i - self._n < k <= i]
        return max(live) if live else None


def register_limits(base: Mapping | None, limits_spec: Mapping, values: Mapping, *,
                    peaks: Mapping[str, PeakLimit] | None = None, now_s: float | None = None) -> dict | None:
    """Limity z rejestrów profilu dołożone do limitów bazowych (`direct_limits`: moc znamionowa,
    pojemność z opcji, źródło). Profil wskazuje klucze w `$.limits.max_charge_register`,
    `max_discharge_register`, `capacity_register`; bez wskazania pola nie liczymy. Pojemność wpisana
    przez właściciela wygrywa z rejestrem. Próbka mocy tylko w paśmie SoC bez dławienia BMS (patrz
    `CHARGE_SAMPLE_SOC_MAX`, `DISCHARGE_SAMPLE_SOC_MIN`); bez próbki w paśmie pola nie ma. `peaks`
    (z `now_s`) — szczyt z doby zamiast chwilowej mocy."""
    base = dict(base or {})
    rated = base.get("rated_power_w")
    soc = values.get("soc")
    soc_ok = _ok(soc, 0.0, 100.0)

    def read(field: str):
        key = limits_spec.get(field)
        return values.get(key) if isinstance(key, str) else None

    powers: dict[str, float | None] = {}
    for key, field in BMS_POWER_FIELDS.items():
        w = base.get(key)
        if w is None:
            in_band = soc_ok and _SAMPLE_BAND[key](soc)
            w = bms_power_w(read(field), values.get("battery_voltage_v"), rated) if in_band else None
            if peaks is not None and key in peaks and now_s is not None:
                peaks[key].observe(w, now_s)
                w = peaks[key].peak(now_s)
            if w is not None and _ok(rated, _RATED_MIN, _RATED_MAX):
                w = min(w, float(rated))          # moc znamionowa obniżona po zapisaniu szczytu
        powers[key] = w
    capacity = base.get("battery_capacity_kwh")
    if capacity is None:
        capacity = read("capacity_register")
    source = base.get("source")
    return executor_limits(rated_power_w=rated, battery_capacity_kwh=capacity,
                           max_charge_w=powers["max_charge_w"], max_discharge_w=powers["max_discharge_w"],
                           source=source if source in LIMIT_SOURCES else "registers")
