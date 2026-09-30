"""Nastawa eksportu slotu sprzedaży liczona na żywo z odczytów (funkcja czysta).

Moc slotu sprzedaży to moc baterii po stronie AC, a tryb sprzedaży falownika traktuje
nastawę jako EKSPORT DO SIECI ponad pokrycie domu — wykonawca przelicza ją w każdym
cyklu z bieżących odczytów. Ta sama logika i ta sama tabela przypadków co w
implementacji referencyjnej (złote wektory `sell`); zmiany tylko w parze.

Moduł nie zna czasu, stanu ani encji: ostatni znany pobór domu i poprzednio zapisaną
nastawę dostaje od wołającego.
"""
from __future__ import annotations

import math

#: Histereza zapisu, W: poprzednio zapisana nastawa zostaje, dopóki nowa różni się od
#: niej o mniej niż tyle (zmiana slotu/trybu = wołający podaje `prev_xset_w=None`).
SELL_XSET_HYSTERESIS_W = 150.0


def _finite(x: float | None) -> float | None:
    """Liczba skończona albo `None` (None/NaN/±inf/nie-liczba/poza zakresem float → brak odczytu)."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _non_negative(x: float | None) -> float:
    """Moc fizyczna: brak/NaN/inf/ujemna liczy się jak 0."""
    v = _finite(x)
    return v if v is not None and v > 0.0 else 0.0


def sell_xset(
    *,
    battery_w: float | None,
    pv_w: float | None,
    load_w: float | None,
    readings_ok: bool,
    last_known_load_w: float | None,
    export_limit_w: float | None,
    rated_power_w: float | None,
    prev_xset_w: float | None,
) -> float:
    """Nastawa eksportu (W) dla slotu sprzedaży.

    * odczyty świeże i kompletne: `bateria + PV − dom`;
    * `readings_ok=False` albo PV/dom brak lub niefinitny: `bateria − ostatni znany dom`,
      a bez ostatniego znanego domu → 0 (nie eksportujemy w ciemno);
    * moce ujemne / NaN / inf liczą się jak 0;
    * wynik przycięty do `[0, min(limit eksportu, moc znamionowa)]`; limit `None` = brak
      limitu, limit NaN/inf/ujemny = eksport zablokowany; znamionowa nieznana/niedodatnia
      = pułap 0;
    * histereza: poprzednia nastawa zostaje, gdy świeża > 0, `0 <= prev <= pułap`
      i `|świeża − prev| < SELL_XSET_HYSTERESIS_W`.

    Obowiązki wołającego: przed wywołaniem odświeżyć `last_known_load_w` bieżącym ważnym
    odczytem domu; `prev_xset_w` to nastawa ostatnio ZAPISANA do falownika.
    """
    ceiling = _non_negative(rated_power_w)
    if export_limit_w is not None:
        ceiling = min(ceiling, _non_negative(export_limit_w))

    battery = _non_negative(battery_w)
    pv = _finite(pv_w)
    load = _finite(load_w)
    known_load = _finite(last_known_load_w)
    if readings_ok and pv is not None and load is not None:
        raw = battery + _non_negative(pv) - _non_negative(load)
    elif known_load is not None:
        # Bez świeżego PV zakładamy 0 PV — mniej eksportu, nigdy więcej.
        raw = battery - _non_negative(known_load)
    else:
        raw = 0.0

    fresh = min(max(raw, 0.0), ceiling)

    prev = _finite(prev_xset_w)
    if (fresh > 0.0 and prev is not None and 0.0 <= prev <= ceiling
            and abs(fresh - prev) < SELL_XSET_HYSTERESIS_W):
        return prev + 0.0  # −0.0 z pamięci wołającego oddajemy jako +0.0
    return fresh
