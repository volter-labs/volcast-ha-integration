"""Silnik tryb+nastawa: slot → intencja → parametry (port referencyjnego mappera).

Kod rozstrzyga WYŁĄCZNIE intencję slotu; nazwę trybu i rodzaj nastawy mocy bierze
z tabeli `intents` profilu (marka to dane, nie kod). Ten moduł nie zna czasu, rejestrów
ani guardów — guardy są warstwą nad nim i nie jest z nich zwolniony.

Pułapki zmierzone na sprzęcie referencyjnym (zachowane przez tabelę profilu GoodWe):
* standby honoruje nastawę mocy jako nastawę ŁADOWANIA → intencja `standby` ma `power: zero`;
* `discharge_battery` ma nastawę stałą i głodzi dom z sieci → sprzedaż to `sell_power`;
* `auto` bez nastawy (zapis kosztuje NVM, falownik jej w tym trybie nie czyta).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..params import Params
from ..profile import Profile
from ..slot import Action, Slot, slot_direction

NOTE_OK = "ok"
NOTE_POWER_WITHOUT_DIRECTION = "power_without_direction"
NOTE_DIRECTION_WITHOUT_POWER = "direction_without_power"


@dataclass(frozen=True)
class MappedSlot:
    intent: str
    params: Params
    note: str


def slot_intent(slot: Slot) -> tuple[str, str]:
    """Intencja slotu i nota: czego mapper świadomie nie wykonał."""
    direction = slot_direction(slot)
    if direction is None:
        # HOLD dzieli z IDLE fizykę (stój, jawne zero); różni je tylko eksport ze slotu.
        intent = "standby" if slot.action in (Action.IDLE, Action.HOLD) else "self_consume"
        # Moc bez kierunku to liczba bez komendy.
        return intent, NOTE_POWER_WITHOUT_DIRECTION if slot.power_w is not None else NOTE_OK
    if direction is Action.CHARGE:
        if slot.charge_source == "pv":
            return "charge_pv", NOTE_OK          # nadwyżkę PV falownik ładuje sam
        if slot.power_w is not None:
            return "charge_grid", NOTE_OK
        # Ładowanie bez mocy pracowałoby na nastawie z POPRZEDNIEGO slotu.
        return "self_consume", NOTE_DIRECTION_WITHOUT_POWER
    if slot.discharge_purpose == "self":
        return "self_consume", NOTE_OK           # `auto` śledzi pobór lepiej niż nastawa
    if slot.power_w is not None:
        # Tylko JAWNE `sell` = sprzedaż ponad dom; brak celu = stary kontrakt, „oddaj tyle".
        return ("sell" if slot.discharge_purpose == "sell" else "discharge_forced"), NOTE_OK
    return "self_consume", NOTE_DIRECTION_WITHOUT_POWER


def _clip_power(power_w: float, rated_power_w: float) -> float:
    w = power_w if power_w > 0.0 else 0.0
    if rated_power_w > 0.0 and w > rated_power_w:
        w = rated_power_w
    return w


def map_slot(slot: Slot, profile: Profile, rated_power_w: float) -> MappedSlot:
    if profile.control_model != "mode_setpoint":
        raise ValueError(f"profil {profile.id} nie jest modelu mode_setpoint")
    intent, note = slot_intent(slot)
    spec = profile.intent(intent)
    power: float | None = None
    if spec["power"] == "slot":
        # Intencja gwarantuje moc; walidator profilu (POWERED_INTENTS) odrzuca `slot`
        # na intencjach, które tej gwarancji nie dają. Tor zapisu i tak jej nie ufa ślepo:
        # brak liczby jest błędem danych, nie zerem, więc kończymy jawnym wyjątkiem
        # zamiast TypeError z `float(None)`.
        if slot.power_w is None:
            raise ValueError(f"intencja {intent!r} wymaga power_w, slot go nie niesie")
        power = _clip_power(float(slot.power_w), rated_power_w)
    elif spec["power"] == "zero":
        power = 0.0

    # `soc_target` znaczy co innego zależnie od KIERUNKU (nie od trybu końcowego).
    soc_min = soc_max = None
    if slot.soc_target is not None:
        if slot_direction(slot) is Action.CHARGE:
            soc_max = slot.soc_target
        else:
            soc_min = slot.soc_target      # pamiętana nastawa — rezerwa jako dolny próg

    # Jeden ogranicznik eksportu: zakaz = włączony z 0 W; brak pułapu = wyłączony.
    if not slot.export_allowed:
        export_enabled, export_w = True, 0.0
    elif slot.export_limit_w is not None:
        export_enabled, export_w = True, slot.export_limit_w
    else:
        export_enabled, export_w = False, None

    return MappedSlot(intent=intent, note=note, params=Params(
        mode=spec["mode"], power_w=power, soc_min=soc_min, soc_max=soc_max,
        export_limit_w=export_w, export_limit_enabled=export_enabled))
