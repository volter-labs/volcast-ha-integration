"""Transport `ha_modbus`: rejestry przez istniejący hub integracji `modbus` Home Assistanta.

Wtyczka nie otwiera żadnego połączenia z falownikiem (magistrala RS485 ma jednego mastera — hub
użytkownika). Interfejs jak `RegisterTransport` (`base.py`), więc kodowanie, odczyt przed zapisem,
budżet NVM i werdykty zostają w `RegisterWriter`.

Zapis — usługa `modbus.write_register` (`blocking=True`) z `hub`, adresem urządzenia, `address`
i `value`: lista słów przy FC 16 (także jedno słowo — HA wysyła listę jako FC 16), liczba przy FC 6.
Nazwa parametru adresu urządzenia w usłudze (źródło: `homeassistant/components/modbus`, HA core):
* HA ≤ 2022.3 — wyłącznie `unit` (wymagany);
* HA 2022.4 … dev (2026-11; sprawdzone m.in. 2023.1, 2025.1, 2026.5.4, 2026.10.4) — `slave` albo
  `unit` (wykluczające się, domyślnie 1). `device_address` to w HA 2026.x tylko klucz konfiguracji YAML
  czujnika, NIE pole usługi (schemat usługi jest ścisły — nieznany klucz to `vol.Invalid`).
Nazwę bierzemy ze schematu zarejestrowanej usługi (`device_address` → `slave` → `unit` — nowsza nazwa
przed przestarzałym aliasem); bez schematu `slave` (minimalna wersja HA wtyczki to 2024.4).

HA nie zwraca wyniku magistrali: `ModbusHub.async_pb_call` loguje błąd i zwraca None, usługa kończy
się bez wyjątku. Zakończenie usługi NIE potwierdza zapisu, więc `write` zawsze kończy się
`WriteUnconfirmed` (dla pisarza: „brak echa, zapis mógł dojść”) — rozstrzyga wyłącznie odczyt zwrotny
(= zamówiona → OK, inna → ERROR; nigdy DENIED z ciszy magistrali). Odrzucenie przed wykonaniem
(brak usługi, zły schemat — `vol.Invalid`, nieznany hub — w HA 2026.5 `KeyError` z `hub_collect`,
w nowszych `ServiceValidationError`) to `LinkDown` — nic nie wysłano, brak zapisu w toku. `on_send`
(budżet NVM) liczy się tuż przed wywołaniem, także gdy HA potem je odrzuci (ostrożnie: za dużo, nie za mało).
Wywołania HA NIGDY nie przerywamy (anulowanie w środku transakcji pymodbus na hubie użytkownika zostawia
spóźnioną odpowiedź dla jego następnego odczytu): czekamy na nie pod `asyncio.shield` najwyżej
`write_timeout_s` (domyślnie 60 s ≥ najgorszy przypadek HA: `timeout` 3 s × (1 + `retries` 3) = 12 s
plus kolejka na zamku huba). Po naszym limicie albo anulowaniu wołającego wywołanie HA biegnie dalej,
a zapis w toku jest oznaczany od razu i ponownie, gdy wywołanie się skończy.

Odczyt — obraz rejestrów z SUROWYCH czujników użytkownika, mapa {adres → entity_id} (holding, FC 3,
adres urządzenia huba). Brak w mapie, brak encji, `unavailable`/`unknown`, stan nieliczbowy albo
nieskończony = `MissingRegister` (nigdy 0). Liczba niecałkowita (skala/precyzja w YAML) albo spoza
zakresu u16/i16 = `RegisterValueError` (błąd konfiguracji czujnika); i16 ujemne → słowo U2.

Świeżość odczytu zwrotnego: rejestr zapisany (albo możliwie zapisany) o czasie T jest czytelny dopiero,
gdy stan czujnika jest stemplowany PO T. Stempel = późniejszy z `last_reported` i `last_updated` —
HA nie przesuwa `last_updated`, gdy wartość się nie zmienia (zapis tej samej wartości albo zapis,
który nie doszedł), a `last_reported` (HA ≥ 2024.3) przesuwa przy każdym zapisie stanu, a czujnik
`modbus` zapisuje stan po każdym odpytaniu. T = chwila PO zakończeniu wywołania usługi: hub wykonuje
wywołania pod jednym zamkiem, a czujnik zapisuje stan zaraz po swoim odczycie, więc odpytanie sprzed
zapisu nie dostanie stempla późniejszego niż T. Odczyt nieświeżego rejestru prosi raz o odświeżenie
(`homeassistant.update_entity`, bez czekania) i czeka do terminu `readback_timeout_s` liczonego od
zapisu; po terminie `RequestTimeout` (odczyt zwrotny nieznany, nie sukces). Rejestry niezapisane
czytamy od razu, bez względu na wiek.

WARUNEK mapy: wyłącznie czujniki bazowe platformy `modbus` (`sensor.*`), jeden na rejestr, które same
czytają swój rejestr przez hub. Czujniki `slave_count`/`virtual_count` (`SlaveSensor`, encja koordynatora)
i encje innych integracji odrzucamy jak brak rejestru: `update_entity` przepisuje ich stary stan z nowym
stemplem bez odczytu magistrali (fałszywa świeżość, a dla `SlaveSensor` traceback w logu HA). Taki rejestr
to `UnsupportedSensor` (podklasa `MissingRegister` — wołający dalej fail-closed) z encją i przyczyną, żeby
raport mógł wskazać prawdziwy powód (np. czujnik YAML bez `unique_id` nie jest w rejestrze encji).
Sprawdzenie — `sensor_check(entity_id) -> przyczyna | None` wstrzyknięte przez warstwę HA; domyślne
(`bus_sensor_problem`) używa wyłącznie publicznych cech: domena `sensor`, wpis w rejestrze encji z platformą
`modbus`, encja w komponencie `sensor` i brak `coordinator` (publiczny atrybut `CoordinatorEntity` —
`SlaveSensor`); bez importu HA. Kontrakt pilnuje `tests_ha/test_ha_modbus_transport_real_ha.py`.

Podpięcie pod połączenie trybu bezpośredniego (ten sam `kind == "direct"`, inny transport):
* tożsamość — rejestry identyfikacji (Deye: 0, 3–7, 20–21) czytane zwykłym `read`, gdy są w mapie;
* wiek obrazu — `image_at_mono(adresy)` / `last_read_at_mono`: NAJSTARSZY stempel czujników na zegarze
  monotonicznym transportu (`clock`, ten sam co wykonawcy), nie chwila startu odpytywania;
* drugi sterownik — `conflict` (obce wywołanie `modbus.write_register`/`write_coil` na naszym hubie
  i urządzeniu w ciągu `conflict_hold_s`; także `stats.stray`);
* `poll_s` połączenia jest poza transportem (odczyt stanu jest lokalny; świeżość daje `scan_interval`
  czujników użytkownika).

W komunikatach adresy rejestrów, nazwy klas błędów i (tylko `UnsupportedSensor`) entity_id — bez nazwy huba.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .base import LinkDown, OnSend, RequestTimeout, Sleep, TransportError, TransportStats
from .modbus_frames import FC_READ, FC_WRITE_MULTIPLE, FC_WRITE_SINGLE

_LOGGER = logging.getLogger(__name__)

HA_MODBUS_KIND = "ha_modbus"

MODBUS_DOMAIN = "modbus"
WRITE_SERVICE = "write_register"
# Kolejność preferencji nazwy parametru adresu urządzenia (gdy schemat usługi ją deklaruje).
UNIT_PARAMS = ("device_address", "slave", "unit")
DEFAULT_UNIT_PARAM = "slave"

# Blok TOU w naszym fragmencie YAML odpytywany co 30 s; prośba o odświeżenie zwykle wystarcza od
# razu, inaczej naturalne odpytanie — dwa okresy + zapas.
DEFAULT_READBACK_TIMEOUT_S = 65.0
DEFAULT_READBACK_POLL_S = 0.5
DEFAULT_WRITE_TIMEOUT_S = 60.0       # ≥ 3 s × (1 + 3 ponowień) HA + kolejka na zamku huba
# Obcy zapis na naszym urządzeniu trzyma `conflict` tyle, co `CLEAR_AFTER_S` monitora rywalizacji.
DEFAULT_CONFLICT_HOLD_S = 1800.0
# Własny zapis rozpoznany WYŁĄCZNIE po tożsamości słownika danych usługi (HA przekazuje go do zdarzenia
# `call_service` bez kopii, `core.py` `ServiceRegistry.async_call`).
_OWN_RECENT = 64
EVENT_CALL_SERVICE = "call_service"
DEFAULT_HUB = "modbus_hub"
BACKGROUND_TASK_NAME = "volcast modbus hub write"
_FOREIGN_SERVICES = frozenset({"write_register", "write_coil"})

_NO_VALUE = ("unavailable", "unknown", "")
# Odrzucenie wywołania przed wykonaniem (nic nie poszło na magistralę) — po nazwie klasy w MRO, bo
# rdzeń nie importuje HA (`test_core_no_ha_imports`): `ServiceValidationError` (w HA także
# `ServiceNotFound`, nieznany hub, modbus niezaładowany) i `vol.Invalid` (schemat usługi). Sufiks —
# atrapy testów HA nazywają się `_FakeServiceValidationError` itp. `KeyError` — nieznany hub w HA 2026.5
# (`hub_collect[...]` przed jakimkolwiek wywołaniem magistrali).
_NOT_SENT_SUFFIXES = ("ServiceNotFound", "ServiceValidationError")
_NOT_SENT_EXACT = frozenset({"Invalid", "KeyError"})
_U16_MAX = 0xFFFF
_I16_MIN = -0x8000


# ── błędy ─────────────────────────────────────────────────────────────────


class MissingRegister(TransportError):
    """Rejestr bez wartości: brak w mapie, brak encji, niedostępna albo stan nieliczbowy."""

    def __init__(self, addrs: Sequence[int], message: str | None = None) -> None:
        self.addrs = tuple(addrs)
        super().__init__(message or "missing registers: " + _fmt(self.addrs))


# Przyczyny `UnsupportedSensor` (stałe — raport kreatora/weryfikacji tłumaczy je dla użytkownika).
NOT_SENSOR_DOMAIN = "not_sensor_domain"
REGISTRY_UNAVAILABLE = "entity_registry_unavailable"
NOT_REGISTERED = "not_in_entity_registry"          # np. czujnik YAML bez `unique_id`
NOT_MODBUS_PLATFORM = "not_modbus_platform"
NOT_LOADED = "entity_not_loaded"
SLAVE_SENSOR = "slave_or_virtual_sensor"            # `slave_count`/`virtual_count`
CHECK_FAILED = "check_failed"                       # sprawdzenie samo się wyłożyło (zmiana w HA?)


class UnsupportedSensor(MissingRegister):
    """Rejestr zmapowany na encję, która nie jest bazowym czujnikiem huba `modbus` czytającym swój
    rejestr. `details` — krotki (adres, entity_id, przyczyna)."""

    def __init__(self, details: Sequence[tuple[int, str, str]]) -> None:
        self.details = tuple(details)
        self.entity_ids = tuple(d[1] for d in self.details)
        self.reasons = tuple(d[2] for d in self.details)
        listed = "; ".join(f"{a} {eid}: {why}" for a, eid, why in self.details[:8])
        more = " …" if len(self.details) > 8 else ""
        super().__init__([d[0] for d in self.details],
                         "register entities are not direct modbus hub sensors (need a modbus platform sensor "
                         f"with unique_id, without slave_count/virtual_count): {listed}{more}")


class RegisterValueError(TransportError):
    """Błąd konfiguracji czujnika: wartość niecałkowita albo spoza zakresu u16/i16."""

    def __init__(self, addrs: Sequence[int]) -> None:
        self.addrs = tuple(addrs)
        super().__init__("register sensor value outside u16/i16 (check data_type/scale): " + _fmt(self.addrs))


class WriteUnconfirmed(TransportError):
    """Usługa wywołana; HA nie zwraca wyniku magistrali — o zapisie rozstrzyga odczyt zwrotny."""


def _fmt(addrs: Sequence[int]) -> str:
    return ", ".join(str(a) for a in addrs[:16]) + (" …" if len(addrs) > 16 else "")


# ── konfiguracja ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HaModbusConfig:
    hub: str                                  # nazwa huba `modbus` użytkownika
    device_address: int                       # adres urządzenia (slave) falownika na tym hubie
    registers: Mapping[int, str]              # adres rejestru holding → entity_id surowego czujnika
    readback_timeout_s: float = DEFAULT_READBACK_TIMEOUT_S
    readback_poll_s: float = DEFAULT_READBACK_POLL_S   # odstęp sprawdzania świeżości po zapisie
    conflict_hold_s: float = DEFAULT_CONFLICT_HOLD_S
    write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _pos(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def _unit_ok(v) -> bool:
    # 0 = rozgłoszenie (zapis do wszystkich urządzeń na magistrali, bez odpowiedzi) — nigdy;
    # 248–255 zarezerwowane w Modbus — adres pojedynczego urządzenia to 1..247
    return _int(v) and 1 <= v <= 247


def validate_ha_modbus_config(cfg: HaModbusConfig) -> None:
    """ValueError przy konfiguracji spoza zakresów (komunikat bez nazw huba i encji)."""
    if not isinstance(cfg.hub, str) or not cfg.hub or cfg.hub != cfg.hub.strip():
        raise ValueError("hub name required")
    if not _unit_ok(cfg.device_address):
        raise ValueError("device address must be 1..247")
    if not isinstance(cfg.registers, Mapping):
        raise ValueError("register map required")
    for addr, eid in cfg.registers.items():
        if not _int(addr) or not 0 <= addr <= _U16_MAX:
            raise ValueError("register address out of range")
        if not isinstance(eid, str) or eid.count(".") != 1 or not all(eid.split(".")):
            raise ValueError(f"register {addr}: entity id required")
    if not _pos(cfg.readback_timeout_s) or not _pos(cfg.readback_poll_s) \
            or not _pos(cfg.write_timeout_s) or not _pos(cfg.conflict_hold_s):
        raise ValueError("timeouts must be positive")


# ── pomocnicze ────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_BAD = object()


def _word(state) -> int | None | object:
    """Słowo z surowego czujnika: int, None (brak wartości) albo `_BAD` (błąd konfiguracji)."""
    if state is None:
        return None
    raw = getattr(state, "state", None)
    if not isinstance(raw, str) or raw.strip() in _NO_VALUE:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    if v != math.floor(v):
        return _BAD
    iv = int(v)
    if not _I16_MIN <= iv <= _U16_MAX:
        return _BAD
    return iv & _U16_MAX


def _stamp(state) -> datetime | None:
    """Ostatni zapis stanu czujnika: późniejszy z `last_reported` i `last_updated`."""
    stamps = [s for s in (getattr(state, "last_reported", None), getattr(state, "last_updated", None))
              if isinstance(s, datetime)]
    try:
        return max(stamps) if stamps else None
    except TypeError:                          # mieszane strefy — brak pewnego stempla
        return None


def _fresh(state, written: datetime) -> bool:
    stamp = _stamp(state)
    try:
        return stamp is not None and stamp > written
    except TypeError:
        return False


def _not_sent(err: BaseException) -> bool:
    """Czy HA odrzucił wywołanie przed wykonaniem (brak usługi, nieznany hub, zły schemat)."""
    return any(c.__name__ in _NOT_SENT_EXACT or c.__name__.endswith(_NOT_SENT_SUFFIXES)
               for c in type(err).__mro__)


def bus_sensor_problem(hass, entity_id: str) -> str | None:
    """None, gdy encja to bazowy czujnik platformy `modbus`, który sam czyta swój rejestr przez hub;
    inaczej przyczyna (stałe wyżej). Wyłącznie publiczne cechy, bez importu HA: domena `sensor`, wpis
    w rejestrze encji (`hass.data["entity_registry"]`, `RegistryEntry.platform`), encja w komponencie
    `sensor` (`hass.data["sensor"].get_entity`) i brak `coordinator` — publicznego atrybutu
    `CoordinatorEntity` (`SlaveSensor` z `slave_count`/`virtual_count` nim jest, czujnik bazowy nie).
    Wyjątek w sprawdzeniu (np. zmiana API HA) → `CHECK_FAILED`, nie ciche „brak rejestru”."""
    if not isinstance(entity_id, str) or not entity_id.startswith("sensor."):
        return NOT_SENSOR_DOMAIN
    try:
        data = getattr(hass, "data", None)
        registry = data.get("entity_registry") if isinstance(data, Mapping) else None
        if registry is None:
            return REGISTRY_UNAVAILABLE
        entry = registry.async_get(entity_id)
        if entry is None:
            return NOT_REGISTERED
        if getattr(entry, "platform", None) != MODBUS_DOMAIN:
            return NOT_MODBUS_PLATFORM
        component = data.get("sensor")
        entity = component.get_entity(entity_id) if component is not None else None
        if entity is None:
            return NOT_LOADED
        if hasattr(entity, "coordinator"):
            return SLAVE_SENSOR
    except Exception as err:  # noqa: BLE001 — sprawdzenie, nie ścieżka danych
        return f"{CHECK_FAILED}: {type(err).__name__}"
    return None


def _schema_fields(hass) -> frozenset[str] | None:
    """Pola schematu zarejestrowanej usługi `modbus.write_register` albo None (nie da się odczytać)."""
    services = None
    try:
        getter = getattr(hass.services, "async_services_for_domain", None)
        if callable(getter):
            services = getter(MODBUS_DOMAIN)
        else:
            services = hass.services.async_services().get(MODBUS_DOMAIN)
    except Exception:  # noqa: BLE001 — introspekcja pomocnicza; domyślna nazwa niżej
        return None
    svc = services.get(WRITE_SERVICE) if isinstance(services, Mapping) else None
    inner = getattr(getattr(svc, "schema", None), "schema", None)
    if not isinstance(inner, Mapping):
        return None
    return frozenset(str(getattr(k, "schema", k)) for k in inner)


def _is_modbus_write(data) -> bool:
    """Filtr zdarzenia `call_service`: zapis domeny `modbus` (rejestr albo cewka)."""
    return isinstance(data, Mapping) and data.get("domain") == MODBUS_DOMAIN \
        and data.get("service") in _FOREIGN_SERVICES


_is_modbus_write._hass_callback = True  # type: ignore[attr-defined]


# ── transport ─────────────────────────────────────────────────────────────


class HaModbusTransport:
    """`RegisterTransport` przez hub `modbus` Home Assistanta (zapis usługą, odczyt z czujników)."""

    kind = HA_MODBUS_KIND

    def __init__(self, hass: Any, cfg: HaModbusConfig, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Sleep = asyncio.sleep, utcnow: Callable[[], datetime] = _utcnow,
                 sensor_check: Callable[[str], str | None] | None = None) -> None:
        validate_ha_modbus_config(cfg)
        self.hass = hass
        self.cfg = cfg
        self.stats = TransportStats()
        self._registers: dict[int, str] = dict(cfg.registers)
        self._clock = clock
        self._sleep = sleep
        self._utcnow = utcnow
        # warunek mapy (czujnik bazowy `modbus`) — warstwa HA może wstrzyknąć własne sprawdzenie
        self._sensor_check = sensor_check if sensor_check is not None \
            else (lambda eid: bus_sensor_problem(hass, eid))
        # zapisy w toku: adres → (czas zapisu UTC, termin odczytu zwrotnego na zegarze monotonicznym)
        self._pending: dict[int, tuple[datetime, float]] = {}
        self._closed = False
        # odczyt: najstarszy stempel czujników ostatniego udanego odczytu (zegar monotoniczny)
        self.last_read_at_mono: float | None = None
        # obce zapisy na naszym hubie i urządzeniu (drugi sterownik) — sygnał `conflict`
        self.foreign_writes = 0
        self.last_foreign_write_mono: float | None = None
        self.last_foreign_write_addr: int | None = None
        self._own: deque[dict] = deque(maxlen=_OWN_RECENT)
        self._unsub: Callable[[], None] | None = None
        bus = getattr(hass, "bus", None)
        listen = getattr(bus, "async_listen", None)
        if callable(listen):
            # słuchacz synchroniczny (oznaczony jak `@callback`) z filtrem — bez zadania na KAŻDE
            # wywołanie usługi w całym HA
            self._unsub = listen(EVENT_CALL_SERVICE, self._on_call_service, event_filter=_is_modbus_write)

    # ── odczyt ──

    async def read(self, addr: int, count: int, *, tries: int | None = None,
                   fc: int = FC_READ, unit: int | None = None) -> list[int]:
        """Słowa `addr … addr+count-1` z czujników. `tries` bez znaczenia (odczyt stanu jest lokalny)."""
        self._check_open()
        if not _int(addr) or not _int(count) or count < 1 or addr < 0 or addr + count > _U16_MAX + 1:
            raise ValueError("register range out of bounds")
        addrs = range(addr, addr + count)
        if fc != FC_READ or (unit is not None and unit != self.cfg.device_address):
            raise MissingRegister(addrs)       # mapa: tylko rejestry holding urządzenia huba
        refresh_asked = False
        while True:
            words, stale = self._image(addrs)
            if not stale:
                self.stats.last_ok_mono = self._clock()
                self.last_read_at_mono = self.image_at_mono(addrs)
                self.stats.consecutive_timeouts = 0
                return words
            now = self._clock()
            deadline = max(self._pending[a][1] for a in stale)
            if now >= deadline:
                self.stats.timeouts += 1
                self.stats.consecutive_timeouts += 1
                raise RequestTimeout("register sensors not refreshed after write")
            if not refresh_asked:
                refresh_asked = True
                await self._request_refresh(stale)
                continue                       # odświeżenie mogło już dojść — sprawdź przed czekaniem
            await self._sleep(min(self.cfg.readback_poll_s, deadline - now))

    def _image(self, addrs: range) -> tuple[list[int], list[int]]:
        """(słowa, adresy jeszcze nieświeże po zapisie); brak/niepoprawna wartość — wyjątek."""
        words: list[int] = []
        missing: list[int] = []
        bad: list[int] = []
        stale: list[int] = []
        unsupported: list[tuple[int, str, str]] = []
        for a in addrs:
            eid = self._registers.get(a)
            why = self._problem(eid) if eid is not None else None
            if why is not None:
                unsupported.append((a, eid, why))
                continue
            state = self.hass.states.get(eid) if eid is not None else None
            w = _word(state)
            if w is None:
                missing.append(a)
                continue
            if w is _BAD:
                bad.append(a)
                continue
            words.append(w)
            pending = self._pending.get(a)
            if pending is not None:
                if _fresh(state, pending[0]):
                    del self._pending[a]       # odświeżony po zapisie — dalej zwykły odczyt
                else:
                    stale.append(a)
        if unsupported:
            raise UnsupportedSensor(unsupported)    # przyczyna konfiguracji ważniejsza niż brak wartości
        if missing:
            raise MissingRegister(missing)
        if bad:
            raise RegisterValueError(bad)
        return words, stale

    def _problem(self, eid: str) -> str | None:
        try:
            return self._sensor_check(eid)
        except Exception as err:  # noqa: BLE001 — wstrzyknięte sprawdzenie nie może przepuścić encji
            return f"{CHECK_FAILED}: {type(err).__name__}"

    def _sensor_state(self, addr: int):
        """Stan czujnika rejestru albo None (brak w mapie, encja spoza warunku mapy, brak stanu)."""
        eid = self._registers.get(addr)
        if eid is None or self._problem(eid) is not None:
            return None
        return self.hass.states.get(eid)

    async def _request_refresh(self, addrs: list[int]) -> None:
        """Jedna prośba o odpytanie czujników (bez czekania); porażka = czekamy na zwykłe odpytanie."""
        ids = list(dict.fromkeys(self._registers[a] for a in addrs if a in self._registers))
        services = self.hass.services
        if not ids or not services.has_service("homeassistant", "update_entity"):
            return
        try:
            await services.async_call("homeassistant", "update_entity", {"entity_id": ids}, blocking=False)
        except Exception as err:  # noqa: BLE001 — tylko przyspieszenie; świeżość i tak sprawdzana
            _LOGGER.debug("register sensor refresh request failed: %s", type(err).__name__)

    # ── zapis ──

    async def write(self, addr: int, values: Sequence[int], *, function: int,
                    on_send: OnSend | None = None, may_resend: Callable[[], bool] | None = None,
                    unit: int | None = None) -> None:
        """Jedno wywołanie `modbus.write_register` (bez ponowień — `may_resend` bez znaczenia).

        Zawsze kończy się wyjątkiem: `WriteUnconfirmed` (wywołano; wynik nieznany — odczyt zwrotny),
        `LinkDown` (odrzucone przed wykonaniem; nic nie wysłano) albo ValueError (złe żądanie, bez
        wywołania). `on_send` (budżet NVM) — tuż przed wywołaniem usługi. Wywołania HA nie anulujemy
        nigdy (`asyncio.shield`): po `write_timeout_s` — `WriteUnconfirmed`, po anulowaniu wołającego —
        `CancelledError`; w obu przypadkach wywołanie HA kończy się samo, a jego koniec odnawia zapis w toku."""
        self._check_open()
        words = list(values)
        if function not in (FC_WRITE_SINGLE, FC_WRITE_MULTIPLE):
            raise ValueError("not a write function")
        if not words or (function == FC_WRITE_SINGLE and len(words) != 1):
            raise ValueError("bad number of words for the write function")
        if not all(_int(w) and 0 <= w <= _U16_MAX for w in words):
            raise ValueError("word out of range")
        if not _int(addr) or addr < 0 or addr + len(words) > _U16_MAX + 1:
            raise ValueError("register range out of bounds")
        if unit is not None and not _unit_ok(unit):
            raise ValueError("device address must be 1..247")
        device = self.cfg.device_address if unit is None else unit
        services = self.hass.services
        if not services.has_service(MODBUS_DOMAIN, WRITE_SERVICE):
            raise LinkDown("modbus write service not available")
        data = {"hub": self.cfg.hub, self._unit_param(): device, "address": addr,
                "value": words if function == FC_WRITE_MULTIPLE else words[0]}
        if on_send is not None:
            on_send()
        self._own.append(data)
        self.stats.requests += 1
        call = self._start(services.async_call(MODBUS_DOMAIN, WRITE_SERVICE, data, blocking=True))
        n = len(words)
        try:
            await asyncio.wait_for(asyncio.shield(call), self.cfg.write_timeout_s)
        except asyncio.CancelledError:
            if call.done() and not call.cancelled():
                self._settled(call, addr, n, device)
            else:
                self._mark_written(addr, n, device)        # może jeszcze dojść — wywołanie HA biegnie dalej
                call.add_done_callback(lambda c: self._settled(c, addr, n, device))
            raise
        except asyncio.TimeoutError:
            self._mark_written(addr, n, device)
            call.add_done_callback(lambda c: self._settled(c, addr, n, device))
            raise WriteUnconfirmed("modbus write still running after the write timeout") from None
        except Exception as err:  # noqa: BLE001 — klasyfikacja niżej; komunikat bez danych wywołania
            if _not_sent(err):
                raise LinkDown(f"modbus write rejected: {type(err).__name__}") from None
            self._mark_written(addr, n, device)
            raise WriteUnconfirmed(f"modbus write outcome unknown: {type(err).__name__}") from None
        self._mark_written(addr, n, device)
        raise WriteUnconfirmed("modbus service does not report the bus result")

    def _start(self, coro) -> asyncio.Future:
        """Wywołanie usługi jako zadanie tła HA (`async_create_background_task`), żeby zapis, na który już
        nie czekamy, nie był zadaniem poza HA. Przy zatrzymaniu HA ANULUJE zadania tła (nie czeka na nie) —
        w tym samym etapie zamyka hub `modbus`, więc nie wydłużamy zatrzymania. Bez tej funkcji — zwykłe
        zadanie."""
        create = getattr(self.hass, "async_create_background_task", None)
        if callable(create):
            return create(coro, BACKGROUND_TASK_NAME)
        return asyncio.ensure_future(coro)

    def _settled(self, call: asyncio.Future, addr: int, count: int, device: int) -> None:
        """Koniec wywołania HA, na które już nie czekamy: zapis w toku od chwili końca (chyba że HA je
        odrzucił przed wykonaniem). Wyjątek odebrany — bez ostrzeżenia „never retrieved”."""
        if call.cancelled():
            self._mark_written(addr, count, device)
            return
        err = call.exception()
        if err is not None and _not_sent(err):
            return
        self._mark_written(addr, count, device)

    def _mark_written(self, addr: int, count: int, device: int) -> None:
        if device != self.cfg.device_address:
            return                             # inne urządzenie — poza obrazem rejestrów tej mapy
        written = self._utcnow()
        deadline = self._clock() + self.cfg.readback_timeout_s
        for a in range(addr, addr + count):
            self._pending[a] = (written, deadline)

    def _unit_param(self) -> str:
        fields = _schema_fields(self.hass)
        if fields:
            for name in UNIT_PARAMS:
                if name in fields:
                    return name
        return DEFAULT_UNIT_PARAM

    # ── wiek obrazu rejestrów ──

    def image_at_mono(self, addrs: Iterable[int]) -> float | None:
        """Chwila odczytu obrazu tych rejestrów na zegarze MONOTONICZNYM transportu: NAJSTARSZY stempel
        ich czujników (obraz w połowie odświeżony po zapisie wielu rejestrów nie jest świeży).

        Stempel czujnika = późniejszy z `last_reported` i `last_updated` — `last_updated` stoi, gdy
        wartość się nie zmienia, więc rejestr bez zmian wyglądałby na wiecznie stary. Przeliczenie
        z UTC: teraz_mono − (teraz_utc − stempel), wiek ≥ 0. None — rejestr bez czujnika albo stempla."""
        stamps = []
        for a in addrs:
            state = self._sensor_state(a)
            stamp = _stamp(state) if state is not None else None
            if stamp is None:
                return None
            stamps.append(stamp)
        if not stamps:
            return None
        try:
            age = (self._utcnow() - min(stamps)).total_seconds()
        except TypeError:
            return None
        return self._clock() - max(0.0, age)

    # ── obcy zapis (drugi sterownik na tym samym hubie) ──

    @property
    def conflict(self) -> bool:
        """Obcy zapis do naszego urządzenia na naszym hubie w ciągu `conflict_hold_s`.

        Widzimy wyłącznie wywołania usług `modbus.write_register`/`write_coil` w tym HA (automatyzacje,
        skrypty, inne integracje wołające usługę). Niewidoczne: encje zapisu platformy `modbus` (switch,
        climate, fan, light, cover — piszą przez `hub.async_pb_call` bez usługi; łapie je statyczna
        kontrola konfliktów) i zapisy spoza HA (aplikacja producenta przez tę samą bramkę, drugi master
        na RS485) — te łapie porównanie obrazu rejestrów z ostatnim
        zapisem (wykrywanie obcych zmian) i statyczna kontrola konfliktów integracji."""
        last = self.last_foreign_write_mono
        return last is not None and self._clock() - last < self.cfg.conflict_hold_s

    def _on_call_service(self, event) -> None:
        data = getattr(event, "data", None) or {}
        if not _is_modbus_write(data):
            return
        sdata = data.get("service_data")
        if not isinstance(sdata, Mapping) or self._is_own(sdata):
            return
        if sdata.get("hub", DEFAULT_HUB) != self.cfg.hub:
            return
        device = next((sdata[k] for k in UNIT_PARAMS if k in sdata), 1)
        try:
            # 0 = rozgłoszenie — dociera też do naszego falownika
            ours = int(device) in (0, self.cfg.device_address)
        except (TypeError, ValueError):
            ours = True                        # adres nieustalony → fail-closed
        if not ours:
            return
        self.foreign_writes += 1
        self.stats.stray += 1                  # sygnał innego klienta także dla liczników łącza
        self.last_foreign_write_mono = self._clock()
        addr = sdata.get("address")
        self.last_foreign_write_addr = addr if _int(addr) else None
        _LOGGER.warning("foreign modbus write to the controlled device (register %s)",
                        self.last_foreign_write_addr)

    def _is_own(self, sdata: Mapping) -> bool:
        return any(sdata is data for data in self._own)

    # HA uruchamia słuchacza oznaczonego jak `@callback` w pętli, bez zadania (rdzeń nie importuje HA)
    _on_call_service._hass_callback = True  # type: ignore[attr-defined]

    # ── cykl życia ──

    async def reset_channel(self) -> None:
        """Bez kanału własnego (hub należy do HA) — nic do resetowania."""

    async def close(self) -> None:
        self._closed = True
        if self._unsub is not None:
            unsub, self._unsub = self._unsub, None
            unsub()

    def _check_open(self) -> None:
        if self._closed:
            raise LinkDown("transport closed")
