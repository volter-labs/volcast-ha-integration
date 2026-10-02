"""Wykrywanie ładowarek EV po rolach encji w obrębie jednego urządzenia HA.

Czyste funkcje, bez importów HA. Klasyfikator tylko wskazuje encje pełniące role
(status, nastawa, start/stop, moc, energia); wartości statusu nie są tu interpretowane.

Znane ograniczenie: status musi mieć listę stanów `options` (sensor enum). Ładowarka bez
enumerowanego statusu — tylko tekstowy sensor albo czujnik wtyczki on/off (binary_sensor)
— nie jest wykrywana automatycznie; on/off nie odróżnia podłączenia od ładowania.
Opcje statusu dzielimy na słowa tylko po znakach innych niż litery/cyfry, więc zbiór w
CamelCase ("SuspendedEV", "Charging", "Available") bez osobnego stanu złącza w rodzaju
"Preparing" też nie daje statusu ("SuspendedEV" to jedno słowo).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import ChargerFinding, ChargerRole, DeviceSnap, EntitySnap, StateSnap

_TOKEN_RE = re.compile(r"[^a-z0-9]+")

# role opcjonalne w kolejności raportowania braków (status jest zawsze wymagany)
OPTIONAL_ROLES: tuple[str, ...] = ("setpoint", "start_stop", "power", "energy")

# opcje statusu: musi być stan ładowania ORAZ osobny stan złącza/auta — samo "charging"
# ma też stan baterii falownika, a "charger_disconnected" to stacja dokująca odkurzacza
_CHARGE_PREFIXES = ("charg",)
_CONNECTOR_PREFIXES = ("plug", "unplug", "connect", "disconnect", "vehicle", "prepar")
_CONNECTOR_WORDS = frozenset({"car", "ev"})

# nastawa: jednostka i rozsądny górny zakres (odsiewa np. prąd ładowania baterii falownika)
_SETPOINT_MAX: dict[str, float] = {"A": 80.0, "W": 50_000.0}
_SETPOINT_HINTS = ("charg", "current", "limit", "power")
_SWITCH_HINTS = ("charg", "allow", "enable", "start", "stop", "pause", "resume")
_START_HINTS = ("start", "resume")
_STOP_HINTS = ("stop", "pause")
_STATUS_HINTS = ("status", "state")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.split(text.lower()) if t]


# litery, których NFKD nie rozkłada na literę bazową + znak diakrytyczny
_FOLD = str.maketrans({"ł": "l", "đ": "d", "ø": "o", "ß": "ss", "æ": "ae", "œ": "oe",
                       "þ": "th", "ı": "i"})


def _slug(text: str) -> str:
    # przybliżenie slugu HA: transliteracja do ASCII, potem słowa łączone "_"
    folded = unicodedata.normalize("NFKD", text.lower().translate(_FOLD))
    return "_".join(_tokens(folded.encode("ascii", "ignore").decode("ascii")))


def _text(e: EntitySnap, prefix: str) -> str:
    # entity_id zaczyna się od slugu nazwy urządzenia ("ev_charger_child_lock") — zdejmujemy
    # tylko ten prefiks; nazwa encji i translation_key liczą się w całości. unique_id pomijamy:
    # jego kształt zależy od integracji i często niesie nazwę urządzenia
    object_id = e.entity_id.split(".", 1)[-1].removeprefix(prefix)
    return f"{object_id} {e.original_name or ''} {e.translation_key or ''}"


def _has(e: EntitySnap, hints: tuple[str, ...], prefix: str) -> bool:
    # początek słowa, nie podciąg: "restart" to nie "start", "discharge" to nie "charg"
    return any(t.startswith(hints) for t in _tokens(_text(e, prefix)))


def _kind(e: EntitySnap) -> str:
    return e.entity_id.split(".", 1)[0]


def _attr(e: EntitySnap, states: dict[str, StateSnap], key: str) -> Any:
    # najpierw rejestr (capabilities), potem atrybuty stanu
    if e.capabilities and key in e.capabilities:
        return e.capabilities[key]
    st = states.get(e.entity_id)
    return st.attributes.get(key) if st else None


def _device_class(e: EntitySnap, states: dict[str, StateSnap]) -> str | None:
    return e.device_class or _attr(e, states, "device_class")


def _unit(e: EntitySnap, states: dict[str, StateSnap]) -> str | None:
    return e.unit or _attr(e, states, "unit_of_measurement")


def _options(e: EntitySnap, states: dict[str, StateSnap]) -> tuple[str, ...]:
    raw = _attr(e, states, "options")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(o) for o in raw)


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _any_option(options: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
    return any(t.startswith(prefixes) for o in options for t in _tokens(o))


def _connector_option(options: tuple[str, ...]) -> bool:
    # stan złącza/auta w opcji BEZ słowa o ładowaniu ("charger_disconnected" się nie liczy)
    for o in options:
        toks = _tokens(o)
        if any(t.startswith(_CHARGE_PREFIXES) for t in toks):
            continue
        if any(t in _CONNECTOR_WORDS or t.startswith(_CONNECTOR_PREFIXES) for t in toks):
            return True
    return False


def _pick(cands: list[EntitySnap], hints: tuple[str, ...],
          prefix: str) -> EntitySnap | None:
    # pierwszeństwo encji z podpowiedzią w nazwie, potem stabilnie po entity_id
    if not cands:
        return None
    return sorted(cands, key=lambda e: (not _has(e, hints, prefix), e.entity_id))[0]


def _status(ents: list[EntitySnap], states: dict[str, StateSnap],
            prefix: str) -> ChargerRole | None:
    enums = [e for e in ents if _kind(e) == "sensor"
             and _any_option(_options(e, states), _CHARGE_PREFIXES)
             and _connector_option(_options(e, states))]
    best = _pick(enums, _STATUS_HINTS, prefix)
    if best is None:
        return None
    return ChargerRole(best.entity_id, "sensor", options=_options(best, states))


def _setpoint(ents: list[EntitySnap], states: dict[str, StateSnap],
              prefix: str) -> ChargerRole | None:
    cands: list[tuple[EntitySnap, str, float, float, float | None]] = []
    for e in ents:
        unit = _unit(e, states)
        if _kind(e) != "number" or unit not in _SETPOINT_MAX:
            continue
        lo, hi = _num(_attr(e, states, "min")), _num(_attr(e, states, "max"))
        if lo is None or hi is None or not 0 <= lo < hi <= _SETPOINT_MAX[unit]:
            continue
        cands.append((e, unit, lo, hi, _num(_attr(e, states, "step"))))
    best = _pick([c[0] for c in cands], _SETPOINT_HINTS, prefix)
    if best is None:
        return None
    _, unit, lo, hi, step = next(c for c in cands if c[0] is best)
    return ChargerRole(best.entity_id, "number", unit=unit, min=lo, max=hi, step=step)


def _start_stop(ents: list[EntitySnap], states: dict[str, StateSnap],
                prefix: str) -> dict[str, ChargerRole]:
    switch = _pick([e for e in ents if _kind(e) == "switch" and _has(e, _SWITCH_HINTS, prefix)],
                   _CHARGE_PREFIXES, prefix)
    if switch is not None:
        return {"start_stop": ChargerRole(switch.entity_id, "switch")}
    selects = [e for e in ents if _kind(e) == "select"
               and _any_option(_options(e, states), _START_HINTS)
               and _any_option(_options(e, states), _STOP_HINTS)]
    select = _pick(selects, _CHARGE_PREFIXES, prefix)
    if select is not None:
        return {"start_stop": ChargerRole(select.entity_id, "select",
                                          options=_options(select, states))}
    buttons = [e for e in ents if _kind(e) == "button"]
    start = _pick([e for e in buttons if _has(e, _START_HINTS, prefix)], _CHARGE_PREFIXES, prefix)
    stop = _pick([e for e in buttons if _has(e, _STOP_HINTS, prefix)], _CHARGE_PREFIXES, prefix)
    if start is not None and stop is not None and start is not stop:
        return {"start": ChargerRole(start.entity_id, "button"),
                "stop": ChargerRole(stop.entity_id, "button")}
    return {}


def _measure(ents: list[EntitySnap], states: dict[str, StateSnap], prefix: str,
             device_class: str, units: tuple[str, ...], prefer_total: bool) -> ChargerRole | None:
    cands = [e for e in ents if _kind(e) == "sensor"
             and (_device_class(e, states) == device_class or _unit(e, states) in units)]
    if not cands:
        return None

    def key(e: EntitySnap) -> tuple[bool, bool, str]:
        total = _attr(e, states, "state_class") in ("total", "total_increasing")
        return (prefer_total and not total, not _has(e, _CHARGE_PREFIXES, prefix), e.entity_id)

    best = sorted(cands, key=key)[0]
    return ChargerRole(best.entity_id, "sensor", unit=_unit(best, states))


def _confidence(missing: tuple[str, ...]) -> str:
    # bez licznika energii — niska
    if "energy" in missing:
        return "low"
    return "high" if not missing else "medium"


def _finding(dev: DeviceSnap, ents: list[EntitySnap],
             states: dict[str, StateSnap]) -> ChargerFinding | None:
    slug = dev.slug if dev.slug is not None else _slug(dev.name or "")
    prefix = f"{slug}_" if slug else ""
    status = _status(ents, states, prefix)
    if status is None:
        return None
    roles: dict[str, ChargerRole] = {"status": status}
    setpoint = _setpoint(ents, states, prefix)
    if setpoint is not None:
        roles["setpoint"] = setpoint
    roles.update(_start_stop(ents, states, prefix))
    has_control = "setpoint" in roles or "start_stop" in roles or "start" in roles
    if not has_control:
        return None
    power = _measure(ents, states, prefix, "power", ("W", "kW"), prefer_total=False)
    if power is not None:
        roles["power"] = power
    energy = _measure(ents, states, prefix, "energy", ("Wh", "kWh", "MWh"), prefer_total=True)
    if energy is not None:
        roles["energy"] = energy
    present = set(roles) | ({"start_stop"} if "start" in roles else set())
    missing = tuple(r for r in OPTIONAL_ROLES if r not in present)
    platform = next((e.platform for e in ents if e.entity_id == status.entity_id), None)
    return ChargerFinding(
        device_id=dev.id, name=dev.name, manufacturer=dev.manufacturer, model=dev.model,
        config_entry_id=dev.config_entry_ids[0] if dev.config_entry_ids else None,
        platform=platform, roles=roles, missing=missing,
        confidence=_confidence(missing),
    )


def classify_chargers(
    devices: list[DeviceSnap],
    entities: list[EntitySnap],
    states: dict[str, StateSnap],
) -> list[ChargerFinding]:
    """Znaleziska ładowarek: urządzenie ze statusem i (nastawą lub start/stop)."""
    by_device: dict[str, list[EntitySnap]] = {}
    for e in entities:
        if e.device_id and not e.disabled:
            by_device.setdefault(e.device_id, []).append(e)
    found = (_finding(d, by_device.get(d.id, []), states) for d in devices)
    return [f for f in found if f is not None]
