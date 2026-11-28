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

Poll blok zawierający niezmapowany rejestr kończy się w całości błędem (patrz `ha_modbus`), a plan odczytu
scala klucze przez dziury (do 16 słów). Dlatego „wymagane” to słowa BLOKÓW, które czytnik naprawdę woła
(`read_plan(profile)` + `modbus.identify_reads`), nie same pola profilu: `ok` gwarantuje czytelność każdego
bloku. `blocks` mówi per blok, których słów brakuje; `mapped_ranges` to bloki w pełni czytelne.
Bloki spoza obsługi transportu (rejestry input `fc: 4`, własny `unit_id`) są zgłaszane jako nieobsługiwane.

Prywatność: `telemetry()` zawiera wyłącznie adresy i przyczyny (stałe z `ha_modbus`) oraz liczniki — nigdy
entity_id ani nazwę huba. Pełny raport (`UnsupportedEntry.entity_id`, `unregistered_sensors`) jest dla UI
kreatora (lokalnie).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from ..modbus.blocks import block_fc, block_unit, read_plan
from ..registers import FC_HOLDING
from .ha_modbus import CHECK_FAILED, MODBUS_DOMAIN, bus_sensor_problem, modbus_scan_s, modbus_sensor_problem

UNIQUE_ID_PREFIX = "volcast_reg_"
_MAX_ADDR = 0xFFFF

UNSUPPORTED_INPUT_REGISTERS = "input_registers_unsupported"   # `fc: 4` — transport czyta tylko holding
UNSUPPORTED_UNIT_OVERRIDE = "unit_override_unsupported"       # blok z własnym `unit_id`

SensorCheck = Callable[[str], "str | None"]


@dataclass(frozen=True)
class UnsupportedEntry:
    addr: int
    entity_id: str          # tylko lokalnie (UI); nie do telemetrii
    cause: str


@dataclass(frozen=True)
class BlockStatus:
    start: int
    count: int
    missing: tuple[int, ...]       # słowa bloku bez (użytecznej) encji
    cause: str | None = None       # blok poza obsługą transportu (`UNSUPPORTED_*`)

    @property
    def readable(self) -> bool:
        return not self.missing and self.cause is None


@dataclass(frozen=True)
class MapReport:
    registers: Mapping[int, str]                # adres → entity_id (wszystkie zmapowane, także nieobsługiwane)
    required: tuple[int, ...]
    missing: tuple[int, ...]                    # wymagane bez żadnej encji (posortowane)
    unsupported: tuple[UnsupportedEntry, ...]   # zmapowane, ale transport ich nie przyjmie
    duplicates: tuple[int, ...]                 # adresy niejednoznaczne (kilka encji na adres albo jedna encja
                                                # na kilka adresów) — wyłączone z mapy
    blocks: tuple[BlockStatus, ...]             # bloki odczytu, które czytnik woła (poll + identyfikacja)
    mapped_ranges: tuple[tuple[int, int], ...]  # (start, liczba) bloków w pełni czytelnych
    unregistered_sensors: tuple[str, ...]       # czujniki YAML `modbus` bez `unique_id` (wskazówka, lokalnie)
    invalid_entries: int = 0                    # wpisy jawnej mapy odrzucone (zły adres/encja)
    registry_available: bool = True
    max_scan_s: float | None = None             # najdłuższe odpytywanie czujników mapy (przy `device`)

    @property
    def ok(self) -> bool:
        return (self.registry_available and not (self.missing or self.unsupported)
                and all(b.readable for b in self.blocks))

    def telemetry(self) -> dict[str, Any]:
        """Adresy, przyczyny i liczniki — bez entity_id."""
        return {
            "ok": self.ok,
            "missing": list(self.missing),
            "unsupported": [{"addr": u.addr, "cause": u.cause} for u in self.unsupported],
            "duplicates": list(self.duplicates),
            "ranges": [list(r) for r in self.mapped_ranges],
            "blocks": [{"start": b.start, "count": b.count, "missing": list(b.missing),
                        "cause": b.cause} for b in self.blocks],
            "unregistered_count": len(self.unregistered_sensors),
            "invalid_entries": self.invalid_entries,
            "registry_available": self.registry_available,
            "max_scan_s": self.max_scan_s,
        }


def _plan(profile) -> list[tuple[int, int, int, int | None]]:
    """Bloki odczytu, które czytnik woła: (start, liczba, fc, unit) — poll + identyfikacja."""
    out = [(b[0], b[1], block_fc(b), block_unit(b)) for b in read_plan(profile)]
    out += [(b[0], b[1], block_fc(b), block_unit(b)) for b in profile.modbus.identify_reads]
    return out


def required_addresses(profile) -> frozenset[int]:
    """Słowa wszystkich bloków odczytu (poll z dziurami scalonymi przez plan + identyfikacja). Rejestry zapisu
    są w planie (odczyt zwrotny i obraz TOU), więc nie trzeba ich dodawać osobno."""
    return frozenset(a for start, n, _fc, _u in _plan(profile) for a in range(start, start + n))


def registers_from_explicit(explicit: Mapping[Any, Any]) -> tuple[dict[int, str], int]:
    """(mapa, liczba odrzuconych wpisów). Klucze mogą być napisami (JSON); encja musi być niepustym napisem."""
    out: dict[int, str] = {}
    bad = 0
    for k, v in explicit.items():
        if type(k) is int:
            addr = k
        elif isinstance(k, str) and k.isascii() and k.isdigit():
            addr = int(k)
        else:
            bad += 1
            continue
        if not 0 <= addr <= _MAX_ADDR or not isinstance(v, str) or not v:
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
    if (not (tail.isascii() and tail.isdigit()) or len(tail) > 5 or (len(tail) > 1 and tail[0] == "0")
            or int(tail) > _MAX_ADDR):
        return None
    return int(tail)


def registers_from_registry(hass) -> tuple[dict[int, str], tuple[int, ...]]:
    """(mapa, adresy niejednoznaczne). Czujniki `sensor` platformy `modbus` z `unique_id` `volcast_reg_<adres>`;
    adres z dwiema encjami jest wyłączony z mapy
    (rejestr HA pilnuje unikalności `unique_id`, więc to obrona; zapis z zerami wiodącymi odrzucamy).
    Konwencja nie wiąże czujnika z hubem ani adresem urządzenia — jeden falownik na konwencję; wiązanie z celem
    zapisu sprawdza `build_register_map(device=...)`. Brak rejestru encji → pusta mapa."""
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


def _sensor_entity(hass, entity_id: str):
    component = hass.data.get("sensor") if isinstance(getattr(hass, "data", None), Mapping) else None
    return component.get_entity(entity_id) if component is not None else None


def build_register_map(hass, profile, *, explicit: Mapping[Any, Any] | None = None,
                       sensor_check: SensorCheck | None = None,
                       device: tuple[str, int] | None = None, hints: bool = True) -> MapReport:
    """Mapa (jawna albo z rejestru encji) + walidacja względem `profile`.

    `device` = (hub, adres urządzenia) celu zapisu: każdy czujnik mapy musi DOWODNIE czytać z tego huba
    i urządzenia, swój rejestr holding jako jedno słowo (`modbus_sensor_problem`) — inaczej jest nieobsługiwany
    (tożsamość i odczyt zwrotny z innego urządzenia niż zapis). Bez `device` — jak dotąd (podgląd kreatora)."""
    check = sensor_check or (lambda eid: bus_sensor_problem(hass, eid))
    invalid = 0
    duplicates: tuple[int, ...] = ()
    if explicit is not None:
        registers, invalid = registers_from_explicit(explicit)
        available = True
    else:
        registers, duplicates = registers_from_registry(hass)
        available = _registry(hass) is not None
    # ta sama encja pod kilkoma adresami = błąd (np. copy-paste w kreatorze): zrzut TOU byłby fałszywy
    owners: dict[str, list[int]] = {}
    for a, e in registers.items():
        owners.setdefault(e, []).append(a)
    shared = sorted(a for addrs in owners.values() if len(addrs) > 1 for a in addrs)
    registers = {a: e for a, e in registers.items() if a not in set(shared)}
    duplicates = tuple(sorted(set(duplicates) | set(shared)))
    required = sorted(required_addresses(profile))
    bad: list[UnsupportedEntry] = []
    scan_all: list[float | None] = []
    for addr in sorted(registers):
        try:
            why = check(registers[addr])
        except Exception as err:  # noqa: BLE001 — wstrzyknięte sprawdzenie nie może przepuścić encji
            why = f"{CHECK_FAILED}: {type(err).__name__}"
        scans: list[float | None] = []
        if why is None and device is not None:
            try:
                entity = _sensor_entity(hass, registers[addr])
                why = modbus_sensor_problem(entity, addr, device[0], device[1])
                scans.append(modbus_scan_s(entity))
            except Exception as err:  # noqa: BLE001 — sprawdzenie nie może przepuścić encji
                why = f"{CHECK_FAILED}: {type(err).__name__}"
        if why is not None:
            bad.append(UnsupportedEntry(addr, registers[addr], why))
        else:
            scan_all.extend(scans)
    bad_addrs = {u.addr for u in bad}
    blocks = []
    for start, n, fc, unit in _plan(profile):
        cause = (UNSUPPORTED_INPUT_REGISTERS if fc != FC_HOLDING
                 else UNSUPPORTED_UNIT_OVERRIDE if unit is not None else None)
        miss = tuple(a for a in range(start, start + n) if a not in registers or a in bad_addrs)
        blocks.append(BlockStatus(start, n, miss, cause))
    return MapReport(
        registers=dict(registers),
        required=tuple(required),
        missing=tuple(a for a in required if a not in registers),
        unsupported=tuple(bad),
        duplicates=duplicates,
        blocks=tuple(blocks),
        mapped_ranges=tuple((b.start, b.count) for b in blocks if b.readable),
        # wskazówka dla kreatora (przegląd wszystkich czujników) — bez niej przy częstym ponownym sprawdzaniu
        unregistered_sensors=unregistered_modbus_sensors(hass) if explicit is None and hints else (),
        invalid_entries=invalid,
        registry_available=available,
        max_scan_s=(None if not scan_all or None in scan_all else max(scan_all)),
    )
