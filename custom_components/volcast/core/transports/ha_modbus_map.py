"""Mapa {adres rejestru → entity_id} dla transportu `ha_modbus` i jej walidacja względem profilu.

Źródła mapy:
* konwencja `unique_id: volcast_reg_<adres>` czujnika `modbus` (przez rejestr encji HA) albo
* jawna mapa z kreatora (`registers_from_explicit`).

Walidacja: wszystkie adresy, których profil wymaga (odczyt + zapis + pola TOU + identyfikacja), muszą być
zmapowane; brakujące wracają jako lista. Mapowanie na encję, której transport nie przyjmie (nie bazowy czujnik
platformy `modbus` — `sensor_check`, domyślnie `bus_sensor_problem`), to osobna lista z przyczyną.

Czujniki YAML bez `unique_id` nie mają wpisu w rejestrze encji, więc po konwencji ich nie widać. Wykrywamy je
na ile się da, wyłącznie publicznymi cechami: encje `sensor.*` bez wpisu w rejestrze, których platforma
(`entity.platform.platform_name`) to `modbus`. Adresu z takiej encji nie odczytamy (konfiguracji YAML w
`hass.data` nie ma po stronie wtyczki), więc to tylko wskazówka dla użytkownika: „dodaj `unique_id`”.
Wykrycie jest „best effort” — zmiana wewnętrzności HA daje pustą listę, nie błąd.

Poll blok zawierający niezmapowany rejestr kończy się w całości błędem (patrz `ha_modbus`), dlatego raport
podaje też ciągłe zakresy w pełni zmapowanych, użytecznych adresów (`mapped_ranges`).

Prywatność: `telemetry()` zawiera wyłącznie adresy i przyczyny (stałe z `ha_modbus`) oraz liczniki — nigdy
entity_id ani nazwę huba. Pełny raport (`UnsupportedEntry.entity_id`, `unregistered_sensors`) jest dla UI
kreatora (lokalnie).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from ..modbus.blocks import _named_runs
from .ha_modbus import CHECK_FAILED, MODBUS_DOMAIN, bus_sensor_problem

UNIQUE_ID_PREFIX = "volcast_reg_"
_MAX_ADDR = 0xFFFF

SensorCheck = Callable[[str], "str | None"]


@dataclass(frozen=True)
class UnsupportedEntry:
    addr: int
    entity_id: str          # tylko lokalnie (UI); nie do telemetrii
    cause: str


@dataclass(frozen=True)
class MapReport:
    registers: Mapping[int, str]                # adres → entity_id (wszystkie zmapowane, także nieobsługiwane)
    required: tuple[int, ...]
    missing: tuple[int, ...]                    # wymagane bez żadnej encji (posortowane)
    unsupported: tuple[UnsupportedEntry, ...]   # zmapowane, ale transport ich nie przyjmie
    duplicates: tuple[int, ...]                 # adres z kilkoma encjami (niejednoznaczny, wyłączony z mapy)
    mapped_ranges: tuple[tuple[int, int], ...]  # (start, liczba) ciągłych, użytecznych adresów
    unregistered_sensors: tuple[str, ...]       # czujniki YAML `modbus` bez `unique_id` (wskazówka, lokalnie)
    invalid_entries: int = 0                    # wpisy jawnej mapy odrzucone (zły adres/encja)
    registry_available: bool = True

    @property
    def ok(self) -> bool:
        return self.registry_available and not (self.missing or self.unsupported)

    def telemetry(self) -> dict[str, Any]:
        """Adresy, przyczyny i liczniki — bez entity_id."""
        return {
            "ok": self.ok,
            "missing": list(self.missing),
            "unsupported": [{"addr": u.addr, "cause": u.cause} for u in self.unsupported],
            "duplicates": list(self.duplicates),
            "ranges": [list(r) for r in self.mapped_ranges],
            "unregistered_count": len(self.unregistered_sensors),
            "invalid_entries": self.invalid_entries,
            "registry_available": self.registry_available,
        }


def required_addresses(profile) -> frozenset[int]:
    """Wszystkie słowa, których profil potrzebuje: odczyt, zapis, pola TOU i identyfikacja."""
    out: set[int] = set()
    for _key, start, count, _fc, _unit in _named_runs(profile, include_identify=True):
        out.update(range(start, start + count))
    return frozenset(out)


def registers_from_explicit(explicit: Mapping[Any, Any]) -> tuple[dict[int, str], int]:
    """(mapa, liczba odrzuconych wpisów). Klucze mogą być napisami (JSON); encja musi być niepustym napisem."""
    out: dict[int, str] = {}
    bad = 0
    for k, v in explicit.items():
        try:
            addr = int(k)
        except (TypeError, ValueError):
            bad += 1
            continue
        if isinstance(k, bool) or not 0 <= addr <= _MAX_ADDR or not isinstance(v, str) or not v:
            bad += 1
            continue
        out[addr] = v
    return out, bad


def _registry(hass):
    data = getattr(hass, "data", None)
    return data.get("entity_registry") if isinstance(data, Mapping) else None


def _addr_from_unique_id(uid: Any) -> int | None:
    """`volcast_reg_<adres>` → adres; inne `unique_id` → None (bez `re`: wzorce tylko przez pomocnika profilu)."""
    if not isinstance(uid, str) or not uid.startswith(UNIQUE_ID_PREFIX):
        return None
    tail = uid[len(UNIQUE_ID_PREFIX):]
    if not (tail.isascii() and tail.isdigit()) or len(tail) > 5 or int(tail) > _MAX_ADDR:
        return None
    return int(tail)


def registers_from_registry(hass) -> tuple[dict[int, str], tuple[int, ...]]:
    """(mapa, adresy niejednoznaczne). Czujniki `sensor` platformy `modbus` z `unique_id` `volcast_reg_<adres>`;
    adres z dwiema encjami (np. dwa huby) jest wyłączony z mapy. Brak rejestru encji → pusta mapa."""
    registry = _registry(hass)
    entities = getattr(registry, "entities", None)
    if registry is None or entities is None:
        return {}, ()
    found: dict[int, list[str]] = {}
    try:
        for entry in list(entities.values()):
            if getattr(entry, "platform", None) != MODBUS_DOMAIN:
                continue
            eid = getattr(entry, "entity_id", None)
            if not isinstance(eid, str) or not eid.startswith("sensor."):
                continue
            addr = _addr_from_unique_id(getattr(entry, "unique_id", None))
            if addr is not None:
                found.setdefault(addr, []).append(eid)
    except Exception:  # noqa: BLE001 — zmiana API rejestru: brak mapy, nie wyjątek w kreatorze
        return {}, ()
    reg = {a: e[0] for a, e in found.items() if len(e) == 1}
    return reg, tuple(sorted(a for a, e in found.items() if len(e) > 1))


def unregistered_modbus_sensors(hass) -> tuple[str, ...]:
    """Czujniki platformy `modbus` bez wpisu w rejestrze encji (YAML bez `unique_id`); best effort."""
    try:
        registry = _registry(hass)
        component = hass.data.get("sensor")
        if registry is None or component is None:
            return ()
        out = []
        for eid in hass.states.async_entity_ids("sensor"):
            if registry.async_get(eid) is not None:
                continue
            entity = component.get_entity(eid)
            platform = getattr(entity, "platform", None)
            if getattr(platform, "platform_name", None) == MODBUS_DOMAIN:
                out.append(eid)
        return tuple(sorted(out))
    except Exception:  # noqa: BLE001
        return ()


def fully_mapped_ranges(registers: Iterable[int], exclude: Iterable[int] = ()) -> list[tuple[int, int]]:
    """Maksymalne ciągłe zakresy (start, liczba) zmapowanych adresów, bez `exclude`."""
    skip = set(exclude)
    addrs = sorted(a for a in set(registers) if a not in skip)
    out: list[tuple[int, int]] = []
    for a in addrs:
        if out and out[-1][0] + out[-1][1] == a:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((a, 1))
    return out


def build_register_map(hass, profile, *, explicit: Mapping[Any, Any] | None = None,
                       sensor_check: SensorCheck | None = None) -> MapReport:
    """Mapa (jawna albo z rejestru encji) + walidacja względem `profile`."""
    check = sensor_check or (lambda eid: bus_sensor_problem(hass, eid))
    invalid = 0
    duplicates: tuple[int, ...] = ()
    if explicit is not None:
        registers, invalid = registers_from_explicit(explicit)
        available = True
    else:
        registers, duplicates = registers_from_registry(hass)
        available = _registry(hass) is not None
    required = sorted(required_addresses(profile))
    bad: list[UnsupportedEntry] = []
    for addr in sorted(registers):
        try:
            why = check(registers[addr])
        except Exception as err:  # noqa: BLE001 — wstrzyknięte sprawdzenie nie może przepuścić encji
            why = f"{CHECK_FAILED}: {type(err).__name__}"
        if why is not None:
            bad.append(UnsupportedEntry(addr, registers[addr], why))
    return MapReport(
        registers=dict(registers),
        required=tuple(required),
        missing=tuple(a for a in required if a not in registers),
        unsupported=tuple(bad),
        duplicates=duplicates,
        mapped_ranges=tuple(fully_mapped_ranges(registers, exclude={u.addr for u in bad})),
        unregistered_sensors=unregistered_modbus_sensors(hass) if explicit is None else (),
        invalid_entries=invalid,
        registry_available=available,
    )
