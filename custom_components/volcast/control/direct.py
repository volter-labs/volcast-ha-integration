"""Połączenie bezpośrednie z falownikiem w HA: cykl życia, odpytywanie, kolizje, tożsamość.

Warstwa HA robi wyłącznie I/O i cykl życia; decyzje są w rdzeniu (`core/`).

* Start: kontrola adresu (literał lokalny, bez gniazda przy odmowie), rejestr hostów wpisów
  (`hass.data[DOMAIN]["direct_hosts"]` — jedno połączenie na host), kolizja statyczna z innymi
  wpisami (fail-closed). Odmowa = żadnego gniazda. Odmowa, która może minąć (kolizja, host zajęty),
  i nieoczekiwany wyjątek startu (`start_failed`, zgłoszenie w Naprawach, najwyżej `START_RETRIES`
  prób) — ponowny start z rosnącą przerwą; zły cel — nigdy.
* Odpytywanie co `poll_s` (przy kolizji co `DIRECT_SLOW_POLL_S`); błąd łącza nie kasuje
  ostatniego odczytu (wiek rośnie), słuchacze wołani po każdej próbie. Kolizja statyczna
  sprawdzana znowu co 10 min, liczniki transportu karmią `ContentionMonitor`.
* Tożsamość (odcisk z solą instalacji) potwierdzana przy starcie, po ponownym połączeniu, po serii
  ≥ 3 przekroczeń czasu, na UDP co godzinę (w odpytywaniu) i na żądanie wykonawcy
  (`identity_check_due`). Zdarzenie łącza albo wiek potwierdzenia → `pending` aż do udanego
  ponownego sprawdzenia (bez nowych odczytów stanu). `mismatch` → odczyt znika (sensory
  niedostępne), odczyty wstrzymane poza sprawdzaniem tożsamości co 10 min, zgłoszenie w Naprawach.
  Zapisy i decyzje trybu próbnego wymagają `identity == "confirmed"` (rozstrzyga wykonawca).
* Odpytywanie z zegara HA biegnie na pętli zdarzeń (`@callback`), zadanie w tle wpisu.

* Cel przez hub `modbus` HA (`transport: ha_modbus`, `hub`, `unit_id`): ten sam `kind == "direct"` i te
  same bramki, inny transport (`HaModbusTransport` — zapis usługą `modbus.write_register` huba użytkownika,
  odczyt z jego surowych czujników). Bez gniazda i bez adresu IP: rejestracja celu po `hub|urządzenie`,
  kolizje statyczne bez porównania adresów (`hub_static_conflicts`; nasz hub nie jest kolizją, encje zapisu
  platformy `modbus` na nim — tak). Start wymaga mapy czujników zgodnej z profilem (`MapReport.ok`: każdy
  blok odczytu w całości zmapowany) — inaczej odmowa `ha_modbus_map` z ponowieniem (czujniki mogą dojść
  później, np. po starcie HA). Obcy zapis usługą na naszym hubie i urządzeniu (`transport.conflict`) to
  kolizja od razu, bez progu monitora. Bez relokatora (nie ma adresu do szukania).

Logi: wyłącznie nazwy klas błędów i liczniki — nigdy host, port, numer seryjny, numer loggera ani nazwa huba.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import time
from datetime import timedelta
from typing import Awaitable, Callable, Iterable, Mapping

import homeassistant.util.dt as dt_util
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from ..const import (CONTROL_MODE_DIRECT, DIRECT_POLL_S, DIRECT_SLOW_POLL_S, DOMAIN, OPT_CONTROL_MODE,
                     OPT_DIRECT_TARGET, OPT_DIRECT_TRIAL)
from ..core.control.conflict import (CONFLICT_DOMAINS, MODBUS_DOMAIN, MODBUS_WRITE_DOMAINS, SELF_DOMAIN,
                                    ContentionMonitor, EntrySnap, HubWriter, conflicting_writers, hub_static_conflicts,
                                    static_conflicts)
from ..core.discovery.known import HOST_KEYS
from ..core.discovery.relocate import FP_SERIAL
from ..core.modbus.blocks import make_block, write_addresses
from ..core.modbus.client import RegisterClient
from ..core.modbus.reading import DirectReading
from ..core.transports.base import (
    ModbusException, RequestTimeout, TransportConfig, TransportError, TransportStats, check_target, validate_config)
from ..core.transports.factory import make_transport
from ..core.transports.ha_modbus import (HA_MODBUS_KIND, HaModbusConfig, HaModbusTransport, modbus_entity_device,
                                         modbus_writer_registers)
from ..core.transports.ha_modbus_map import MapReport, build_register_map
from .work_mode import WorkModeTracker, classify_direct

_LOGGER = logging.getLogger(__name__)
# Kolejne ciche przekroczenia czasu odczytu trybu pracy, po których odstęp odczytów rośnie (x2, x4, ...).
WORK_MODE_FAIL_LIMIT = 3
WORK_MODE_BACKOFF_MAX_S = 86400.0   # przerwa między odczytami trybu pracy nie rośnie ponad dobę
_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")

STATIC_CHECK_S = 600.0              # kolizja statyczna sprawdzana znowu co 10 min
RETRY_MIN_S = 60.0                  # odmowa startu (kolizja, host zajęty): ponowny start po 1 min,
RETRY_MAX_S = 600.0                 # potem co 2×, najwyżej co 10 min
START_RETRIES = 5                   # nieoczekiwany wyjątek przy starcie: tyle prób, potem do przeładowania
START_FAILED = "start_failed"
IDENTITY_RETRY_S = 600.0            # przy niezgodnej tożsamości — tylko sprawdzanie co 10 min
IDENTITY_UDP_MAX_AGE_S = 3600.0     # UDP (bez połączenia): tożsamość co godzinę
IDENTITY_MAX_AGE_S = 3600.0         # przed sesją zapisów: potwierdzenie nie starsze niż godzina
TIMEOUT_STREAK = 3
RESOLVE_TIMEOUT_S = 2.0
FRESH_WAIT_S = 10.0                 # powrót czeka na odpytywanie w toku najwyżej tyle
HUB_MAP = "ha_modbus_map"           # odmowa: mapa czujników huba nie pokrywa bloków odczytu profilu
HUB_WRITER_ISSUE = "ha_modbus_write_entity"  # zgłoszenie: encje zapisu `modbus` na naszych rejestrach
HUB_SERVICE_WRITE = "modbus_service_write"   # przyczyna kolizji: obce wywołanie usługi zapisu na naszym hubie
HUB_DEFAULT_SCAN_S = 60.0           # odpytywanie czujników huba, gdy nie da się go ustalić
_ISSUE_LIST_MAX = 16                # najwyżej tyle pozycji w opisie zgłoszenia (Naprawy)
_FP_HEX = frozenset("0123456789abcdef")

Resolver = Callable[[str], Awaitable[Iterable[str] | None]]


def target_fingerprint(target: Mapping, salt: bytes) -> str:
    """Odcisk celu połączenia (własność w trybie bezpośrednim): HMAC z solą instalacji — bez adresu jawnie.
    Cel przez hub `modbus` HA: hub i adres urządzenia zamiast hosta i portu."""
    if target.get("transport") == HA_MODBUS_KIND:
        msg = f"{HA_MODBUS_KIND}|{target.get('hub')}|{target.get('unit_id')}"
    else:
        msg = f"{target.get('transport')}|{target.get('host')}|{target.get('port')}|{target.get('unit_id')}"
    return hmac.new(bytes(salt), msg.encode(), hashlib.sha256).hexdigest()[:16]


def hub_key(target: Mapping) -> str | None:
    """Klucz celu przez hub `modbus` HA (`hub|urządzenie`) albo None przy złym celu: niepusta nazwa huba bez
    białych znaków na brzegach, adres urządzenia 1..247 (0 = rozgłoszenie — nigdy)."""
    hub, unit = target.get("hub"), target.get("unit_id")
    if not isinstance(hub, str) or not hub or hub != hub.strip():
        return None
    if not isinstance(unit, int) or isinstance(unit, bool) or not 1 <= unit <= 247:
        return None
    return f"{HA_MODBUS_KIND}|{hub}|{unit}"


def _make_hub_transport(hass, cfg: HaModbusConfig, *, clock) -> HaModbusTransport:
    return HaModbusTransport(hass, cfg, clock=clock)


def serial_identity(target: Mapping, profile) -> str | None:
    """Odcisk urządzenia celu, gdy jednoznacznie wskazuje egzemplarz — wtedy własność i tożsamość sterowania
    nie zależą od adresu (zmiana IP to nie inne urządzenie).

    Tylko odcisk z seriala: poprawny odcisk (16 cyfr szesnastkowych), profil celu = podany profil i ten profil
    ma rejestr seriala (`device_fingerprint` daje wtedy wyłącznie `profil|serial`, bez seriala — brak odcisku).
    Jawne `fp_kind` inne niż `serial` (np. `model`) też wyłącza. None = dawne zachowanie (odcisk z adresem):
    brak odcisku, odcisk słaby `model|moc` (dwa identyczne modele są nie do odróżnienia) albo brak profilu.
    """
    if not isinstance(target, Mapping) or profile is None:
        return None
    if target.get("transport") == HA_MODBUS_KIND:
        # hub `modbus` HA: tożsamość z czujników wiąże się z hubem i urządzeniem celu (`build_register_map`
        # z `device`) — zmiana huba albo adresu urządzenia to inne sterowanie, bez własności po samym serialu
        return None
    fp = _expected_fp(target.get("device_fp"))
    if fp is None or target.get("profile_id") != getattr(profile, "id", None):
        return None
    if target.get("fp_kind", FP_SERIAL) != FP_SERIAL:
        return None
    raw = getattr(profile, "raw", None)
    identify = raw.get("identify") if isinstance(raw, Mapping) else None
    registers = identify.get("registers") if isinstance(identify, Mapping) else None
    return fp if isinstance(registers, Mapping) and "serial" in registers else None


def device_target_fingerprint(target: Mapping, device_fp: str, salt: bytes) -> str:
    """Odcisk celu bez adresu (własność przy odcisku z seriala, `serial_identity`): HMAC z solą instalacji
    z `transport|port|unit_id|device_fp` — przeżywa zmianę adresu tego samego egzemplarza."""
    msg = f"device|{target.get('transport')}|{target.get('port')}|{target.get('unit_id')}|{device_fp}"
    return hmac.new(bytes(salt), msg.encode(), hashlib.sha256).hexdigest()[:16]


def _expected_fp(value) -> str | None:
    """Oczekiwany odcisk urządzenia: dokładnie 16 małych cyfr szesnastkowych, inaczej brak."""
    if isinstance(value, str) and len(value) == 16 and set(value) <= _FP_HEX:
        return value
    return None


async def _default_resolve(name: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(name, None)
    return [str(info[4][0]) for info in infos]


def _hosts(entry) -> list[str]:
    out: list[str] = []
    for source in (getattr(entry, "data", None), getattr(entry, "options", None)):
        if not isinstance(source, Mapping):
            continue
        for key in HOST_KEYS:
            v = source.get(key)
            if isinstance(v, str) and v.strip() and v.strip() not in out:
                out.append(v.strip())
    return out


def _literal(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False


async def _addresses(hosts: list[str], resolve: Resolver, timeout_s: float) -> tuple[str, ...] | None:
    """Adresy hostów wpisu; () = wpis bez hosta (chmura); None, gdy któryś nie daje się ustalić (fail-closed)."""
    if not hosts:
        return ()
    out: list[str] = []
    for host in hosts:
        if _literal(host):
            out.append(host)
            continue
        try:
            found = await asyncio.wait_for(resolve(host), timeout_s)
        except Exception:  # noqa: BLE001 — nierozwiązywalny albo przekroczony czas = nieznany
            return None
        found = [a for a in (found or ()) if isinstance(a, str)]
        if not found:
            return None
        out.extend(found)
    return tuple(out)


def _active_target(entry) -> Mapping | None:
    opts = getattr(entry, "options", None) or {}
    target = opts.get(OPT_DIRECT_TARGET)
    active = opts.get(OPT_CONTROL_MODE) == CONTROL_MODE_DIRECT or opts.get(OPT_DIRECT_TRIAL) is True
    return target if isinstance(target, Mapping) and active else None


def _direct_hub(entry) -> tuple[str, int | None] | None:
    """Cel przez hub `modbus` innego wpisu `volcast`: (hub, urządzenie); None = brak takiego celu."""
    target = _active_target(entry)
    if target is None or target.get("transport") != HA_MODBUS_KIND:
        return None
    hub, unit = target.get("hub"), target.get("unit_id")
    if not isinstance(hub, str):
        return None                                      # nieznany hub — jak cel bez adresu (kolizja)
    return hub, unit if isinstance(unit, int) and not isinstance(unit, bool) else None


def hub_writers(hass) -> list[HubWriter] | None:
    """Encje zapisu platformy `modbus` (piszą przez hub bez usługi): hub, adres urządzenia i rejestry zapisu.

    Publiczne: komponent domeny w `hass.data[<domena>]` (`EntityComponent.entities`),
    `entity.platform.platform_name`. Hub, urządzenie i rejestry — prywatne atrybuty encji `modbus`, czytane
    wyłącznie przez pomocników `ha_modbus` (brak = nieustalone = kolizja). Wyjątek = None (kolizja)."""
    out: list[HubWriter] = []
    try:
        data = getattr(hass, "data", None)
        for domain in sorted(MODBUS_WRITE_DOMAINS):
            component = data.get(domain) if isinstance(data, Mapping) else None
            for entity in list(getattr(component, "entities", None) or ()):
                platform = getattr(entity, "platform", None)
                if getattr(platform, "platform_name", None) != MODBUS_DOMAIN:
                    continue
                hub, unit = modbus_entity_device(entity)
                eid = getattr(entity, "entity_id", None)
                out.append(HubWriter(hub, unit, modbus_writer_registers(entity, domain),
                                     eid if isinstance(eid, str) else None))
    except Exception as err:  # noqa: BLE001 — „nie wiemy” = kolizja
        _LOGGER.debug("Volcast modbus hub writer check failed: %s", type(err).__name__)
        return None
    return out


def _direct_host(entry) -> tuple[str, ...]:
    """Cel połączenia bezpośredniego innego wpisu `volcast` (() = wpis bez takiego połączenia)."""
    opts = getattr(entry, "options", None) or {}
    target = opts.get(OPT_DIRECT_TARGET)
    active = opts.get(OPT_CONTROL_MODE) == CONTROL_MODE_DIRECT or opts.get(OPT_DIRECT_TRIAL) is True
    if not isinstance(target, Mapping) or not active:
        return ()
    host = target.get("host")
    return (host,) if isinstance(host, str) and host else ("",)


async def async_entry_snaps(hass, self_entry_id: str, *, resolve: Resolver | None = None,
                            timeout_s: float = RESOLVE_TIMEOUT_S) -> list[EntrySnap]:
    """Wpisy konfiguracji istotne dla kolizji: integracje falowników, `modbus` i inne wpisy `volcast`.

    Hosty z `HOST_KEYS` w `data` i `options`; nazwy rozwiązywane (limit `timeout_s`); nazwa
    nierozwiązywalna albo przekroczony czas → `addresses=None` (kolizja, fail-closed); brak hosta →
    `addresses=()` (integracja chmurowa, nie kolizja).
    """
    resolve = resolve or _default_resolve
    snaps: list[EntrySnap] = []
    for entry in hass.config_entries.async_entries():
        domain = getattr(entry, "domain", None)
        disabled = getattr(entry, "disabled_by", None) is not None
        if domain == SELF_DOMAIN:
            snaps.append(EntrySnap(domain, _direct_host(entry), disabled,
                                   is_self=getattr(entry, "entry_id", None) == self_entry_id,
                                   hub=_direct_hub(entry)))
        elif domain in CONFLICT_DOMAINS:
            addresses = None if disabled else await _addresses(_hosts(entry), resolve, timeout_s)
            # HA (do 2026.9) konfiguruje `modbus` tylko w YAML — wpisów tej domeny nie ma; gdyby powstały,
            # nazwa huba to `data["name"]` (jak w YAML), a tytuł tylko zapasowo. Inna nazwa = kolizja (fail-closed).
            data = getattr(entry, "data", None)
            name = data.get("name") if isinstance(data, Mapping) else None
            name = name if isinstance(name, str) else getattr(entry, "title", None)
            hub = (name, None) if domain == MODBUS_DOMAIN and isinstance(name, str) else None
            snaps.append(EntrySnap(domain, addresses, disabled, hub=hub))
    return snaps


class DirectConnection:
    def __init__(self, hass, entry, profile, target: Mapping, *, trial: bool, salt: bytes,
                 poll_s: float = DIRECT_POLL_S, transport_factory=make_transport, clock=time.monotonic,
                 utcnow=dt_util.utcnow, allow_loopback: bool = False, unreadable: Iterable[str],
                 resolve: Resolver | None = None, unsupported: Iterable[str] = (),
                 hub_transport_factory=_make_hub_transport) -> None:
        self._hass = hass
        self._entry = entry
        self.profile = profile
        self.target = dict(target)
        self.trial = trial
        self._salt = bytes(salt)
        self.poll_s = float(poll_s)
        self._factory = transport_factory
        self._hub_factory = hub_transport_factory
        # cel przez hub `modbus` HA: wynik walidacji mapy czujników przy ostatnim starcie (adresy i przyczyny)
        self.map_report: MapReport | None = None
        self._map_issue = f"ha_modbus_map_{getattr(entry, 'entry_id', '')}"
        self._writer_issue = f"ha_modbus_write_entity_{getattr(entry, 'entry_id', '')}"
        self._map_issue_open = False
        self._writer_issue_open = False
        self._clock = clock
        self._utcnow = utcnow
        self._allow_loopback = allow_loopback
        self.unreadable = frozenset(unreadable)
        # klucze bez rejestru według sondy (wyjątek 2) — poza cyklem odpytywania: na łączu bez korelacji
        # odpowiedzi ich wyjątek byłby „poprzednią odpowiedzią” pasującą do następnego żądania
        self.unsupported = frozenset(unsupported)
        self._resolve = resolve
        self.reading: DirectReading | None = None
        self.monitor = ContentionMonitor()
        self.client: RegisterClient | None = None
        self.identity = "unknown"
        self.static_conflicts: tuple[str, ...] = ()
        self._refused: str | None = None
        self._host: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsub_timer: Callable[[], None] | None = None
        self._stopped = False
        self._started = False
        self._poll_lock = asyncio.Lock()
        # tryb pracy falownika; runtime podmienia na wspólny `rt.work_mode` (ten sam tracker co odczyt z encji)
        self.work_mode = WorkModeTracker()
        self._work_mode_at: float | None = None
        self._work_mode_fails = 0       # ciche przekroczenia czasu z rzędu
        self._work_mode_off = False     # nieużyteczna odpowiedź: rejestru nie czytamy do końca połączenia
        self._poll_task: asyncio.Task | None = None
        self._timer_task: asyncio.Task | None = None
        self._last_poll: float | None = None
        self._last_static: float | None = None
        self._identity_at: float | None = None
        self._identity_tried: float | None = None
        self._recheck = False
        self._seen_reconnects = 0
        self._seen_peer_resets = 0
        self._identity_issue = f"direct_identity_changed_{getattr(entry, 'entry_id', '')}"
        self.fresh_wait_s = FRESH_WAIT_S
        # Własność z wcześniejszej sesji: kolizja statyczna przy starcie nie odcina powrotu do trybu
        # bazowego — połączenie rusza w stanie kolizji (zapisy planu stoją, powrót idzie).
        self.allow_conflicted_restore = False
        # Ponowny start po odmowie (kolizja, host zajęty) z rosnącą przerwą; zły cel — nigdy.
        self.retry_min_s = RETRY_MIN_S
        self.retry_max_s = RETRY_MAX_S
        self._retries = 0
        self._start_failures = 0
        self._start_issue = f"direct_start_failed_{getattr(entry, 'entry_id', '')}"
        self._retry_task: asyncio.Task | None = None
        self._sleep = asyncio.sleep

    # ── stan ──

    @property
    def stats(self) -> TransportStats:
        t = self.client.transport if self.client is not None else None
        return getattr(t, "stats", None) or TransportStats()

    @property
    def hub_mode(self) -> bool:
        """Cel przez hub `modbus` HA (transport `ha_modbus`) zamiast połączenia sieciowego."""
        return self.target.get("transport") == HA_MODBUS_KIND

    @property
    def image_slack_s(self) -> float:
        """Hub HA: obraz rejestrów jest tak stary jak najwolniej odpytywany czujnik — o tyle (×2) więcej wolno mu
        mieć lat, zanim encje i telemetria uznają odczyt za nieaktualny. Bramki zapisu liczą prawdziwy wiek."""
        if not self.hub_mode:
            return 0.0
        scan = self.map_report.max_scan_s if self.map_report is not None else None
        return 2.0 * (scan if scan is not None else HUB_DEFAULT_SCAN_S)

    @property
    def conflict_reason(self) -> str | None:
        """Kod przyczyny bieżącej kolizji (do zgłoszenia w Naprawach i dowodów sterownika); None = brak."""
        if self.monitor.state == "conflict":
            return self.monitor.reason or "bus"
        if self.static_conflicts:
            return self.static_conflicts[0]
        transport = self.client.transport if self.client is not None else None
        if getattr(transport, "conflict", False) is True:
            return HUB_SERVICE_WRITE
        return None

    @property
    def conflict(self) -> bool:
        # transport przez hub HA widzi obce wywołania usługi zapisu na naszym hubie — kolizja od razu
        transport = self.client.transport if self.client is not None else None
        return bool(self.static_conflicts) or self.monitor.state == "conflict" \
            or getattr(transport, "conflict", False) is True

    def refused(self) -> str | None:
        return self._refused

    def age_s(self) -> float:
        if self.reading is None:
            return math.inf
        return max(0.0, self._clock() - self.reading.at_mono)

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)
        return remove

    def poll_due(self, now_mono: float | None = None) -> bool:
        now = self._clock() if now_mono is None else now_mono
        if self._last_poll is None:
            return True
        interval = DIRECT_SLOW_POLL_S if self.conflict else self.poll_s
        return now - self._last_poll >= interval - 1e-6

    def identity_check_due(self, now_mono: float | None = None) -> bool:
        """Czy przed sesją zapisów trzeba potwierdzić tożsamość urządzenia: brak potwierdzenia, ponowne
        połączenie, seria przekroczeń czasu albo potwierdzenie starsze niż godzina.

        Dla bramek zapisu to za mało: zapis i decyzja próbna wymagają też `identity == "confirmed"`."""
        now = self._clock() if now_mono is None else now_mono
        self._observe_link()
        self._hub_binding_lost()
        if self.identity != "confirmed" or self._recheck or self._identity_at is None:
            return True
        limit = IDENTITY_UDP_MAX_AGE_S if self.target.get("transport") == "goodwe_udp" else IDENTITY_MAX_AGE_S
        return not 0.0 <= now - self._identity_at < limit

    # ── cykl życia ──

    def _config(self) -> TransportConfig | None:
        kind = self.target.get("transport")
        opts = self.profile.modbus.transport_options.get(kind) if isinstance(kind, str) else None
        if opts is None:
            return None
        serial = self.target.get("logger_serial")
        cfg = TransportConfig(kind=kind, host=str(self.target.get("host")), port=self.target.get("port"),
                              unit=self.target.get("unit_id"), timeout_s=opts["timeout_ms"] / 1000.0,
                              gap_s=opts["gap_ms"] / 1000.0,
                              logger_serial=serial if kind == "solarman_v5" else None)
        validate_config(cfg)
        return cfg

    async def async_start(self) -> None:
        if self._started or self._stopped:
            return
        self._started = True
        if self.hub_mode:
            cfg, host = None, hub_key(self.target)
        else:
            try:
                cfg = self._config()
                host = check_target(str(self.target.get("host")), allow_loopback=self._allow_loopback)
            except (ValueError, TypeError, KeyError):
                cfg, host = None, None
        if (cfg is None and not self.hub_mode) or host is None:
            self._refused = "bad_target"
            _LOGGER.warning("Volcast direct connection refused: target is not a valid %s",
                            "modbus hub and device address" if self.hub_mode else "local address")
            return
        hosts = self._hass.data.setdefault(DOMAIN, {}).setdefault("direct_hosts", {})
        if hosts.get(host) is not None:                  # inne połączenie (także tego samego wpisu) żyje
            self._refuse("direct_in_use", "the inverter address is already in use")
            return
        clash = await self._static_check(host)
        if self._stopped:                                # zatrzymane w trakcie startu: nic nie zostawiamy
            return
        if hosts.get(host) is not None:
            self._refuse("direct_in_use", "the inverter address is already in use")
            return
        if clash and self.allow_conflicted_restore and SELF_DOMAIN not in clash:
            self.static_conflicts = clash
            _LOGGER.warning("Volcast direct connection: another integration uses this inverter — connecting "
                            "only to return it to its own settings")
        elif clash:
            self.static_conflicts = ()
            self._refuse("direct_in_use" if SELF_DOMAIN in clash else f"direct_conflict:{clash[0]}",
                         "another integration uses this inverter")
            return
        hosts[host] = self
        self._host = host
        try:
            if self.hub_mode:
                await self._start_hub()
            else:
                await self._start_registered(cfg)
        except Exception as err:  # noqa: BLE001 — nieoczekiwany wyjątek: odmowa z ponowieniem, nie „łączenie”
            await self._release()                        # bez rejestracji hosta i bez otwartego transportu
            self.client = None
            self._start_failed(err)
            return
        except BaseException:
            await self._release()                        # anulowanie nie zostawia rejestracji hosta
            raise
        if self._start_failures and self._refused is None:
            self._start_failures = 0
            ir.async_delete_issue(self._hass, DOMAIN, self._start_issue)

    def _start_failed(self, err: Exception) -> None:
        """Wyjątek przy starcie: odmowa `start_failed`, zgłoszenie w Naprawach, ograniczona liczba prób."""
        self._refused = START_FAILED
        self._start_failures += 1
        if self._start_failures == 1:
            ir.async_create_issue(self._hass, DOMAIN, self._start_issue, is_fixable=False, severity=_WARNING,
                                  translation_key="direct_start_failed")
        if self._start_failures < START_RETRIES:
            _LOGGER.warning("Volcast direct connection start failed (%s) — retrying", type(err).__name__)
            self._schedule_retry()
        else:
            _LOGGER.warning("Volcast direct connection start failed (%s) — giving up until Volcast is reloaded",
                            type(err).__name__)

    def _refuse(self, reason: str, why: str) -> None:
        """Odmowa startu, która może minąć (kolizja, host zajęty): ponowny start z rosnącą przerwą."""
        self._refused = reason
        if self._retries == 0:
            _LOGGER.warning("Volcast direct connection refused: %s — checking again later", why)
        self._schedule_retry()

    def _schedule_retry(self) -> None:
        if self._stopped or (self._retry_task is not None and not self._retry_task.done()):
            return
        delay = min(self.retry_min_s * (2 ** self._retries), self.retry_max_s)
        self._retries += 1
        coro = self._async_retry(delay)
        create = getattr(self._entry, "async_create_background_task", None)
        if callable(create):
            self._retry_task = create(self._hass, coro, name="volcast_direct_retry")
        else:
            self._retry_task = asyncio.get_running_loop().create_task(coro)

    async def _async_retry(self, delay: float) -> None:
        await self._sleep(delay)
        if self._stopped:
            return
        self._retry_task = None                          # kolejna odmowa zaplanuje następną próbę
        self._started = False
        self._refused = None
        await self.async_start()
        if self._refused is None and self.client is not None and not self._stopped:
            _LOGGER.warning("Volcast direct connection started after an earlier refusal")
            self._retries = 0
            await self.async_poll()

    async def _start_registered(self, cfg: TransportConfig) -> None:
        try:
            transport = self._factory(cfg, allow_loopback=self._allow_loopback)
        except Exception as err:  # noqa: BLE001 — zła konfiguracja: bez połączenia
            _LOGGER.warning("Volcast direct connection not started: %s", type(err).__name__)
            self._refused = "bad_target"
            await self._release()
            return
        await self._start_client(transport)

    async def _start_hub(self) -> None:
        """Cel przez hub `modbus` HA: mapa czujników → transport `ha_modbus`. Mapa niezgodna z profilem
        (`MapReport.ok` — każdy blok odczytu i identyfikacji w całości zmapowany na czujniki bazowe huba) →
        odmowa `ha_modbus_map` z ponowieniem; czytamy wyłącznie zmapowane rejestry."""
        report = self._hub_map()
        self.map_report = report
        if not report.ok:
            await self._release()
            self._sync_map_issue(report)
            self._refuse(HUB_MAP, "the modbus hub sensors do not cover the inverter profile registers")
            return
        self._sync_map_issue(report)
        try:
            cfg = HaModbusConfig(hub=str(self.target.get("hub")), device_address=int(self.target.get("unit_id")),
                                 registers=dict(report.registers))
            transport = self._hub_factory(self._hass, cfg, clock=self._clock)
        except Exception as err:  # noqa: BLE001 — zła konfiguracja: bez połączenia
            _LOGGER.warning("Volcast direct connection not started: %s", type(err).__name__)
            self._refused = "bad_target"
            await self._release()
            return
        await self._start_client(transport)

    def _hub_map(self, *, hints: bool = True) -> MapReport:
        """Mapa czujników celu przez hub, związana z hubem i urządzeniem ZAPISU: każdy czujnik (także tożsamości)
        musi dowodnie czytać z tego samego huba i adresu urządzenia, do którego piszemy."""
        explicit = self.target.get("registers")
        return build_register_map(self._hass, self.profile,
                                  explicit=explicit if isinstance(explicit, Mapping) else None,
                                  device=(str(self.target.get("hub")), int(self.target.get("unit_id"))), hints=hints)

    def _hub_map_valid(self) -> bool:
        """Ponowne sprawdzenie mapy (YAML mógł się zmienić pod działającym połączeniem): zgodna z profilem
        i z celem zapisu oraz ta sama, na której zbudowano transport."""
        try:
            report = self._hub_map(hints=False)
        except Exception as err:  # noqa: BLE001 — „nie wiemy” = niepotwierdzone
            _LOGGER.debug("Volcast modbus hub map check failed: %s", type(err).__name__)
            return False
        self.map_report = report
        self._sync_map_issue(report)
        cfg = getattr(self.client.transport, "cfg", None) if self.client is not None else None
        return report.ok and dict(getattr(cfg, "registers", {}) or {}) == dict(report.registers)

    def _hub_binding_lost(self) -> bool:
        """Hub HA: mapa sprawdzana na nowo przy KAŻDYM odpytaniu i przed każdą sesją zapisów (tylko lokalne
        wyszukania w rejestrze encji). Niezgodna → tożsamość `pending`, bez odczytów — żadna decyzja, migawka TOU
        ani zapis nie powstaje z obrazu innego urządzenia albo przekłamanych czujników. True = utracona."""
        if not self.hub_mode or self.client is None or self._hub_map_valid():
            return False
        if self.identity != "pending":
            _LOGGER.warning("Volcast direct control: the modbus hub sensors no longer read the raw registers of the "
                            "controlled device — writes stopped")
        self.identity = "pending"
        self._recheck = True
        self.reading = None
        return True

    def _sync_issue(self, issue_id: str, attr: str, key: str, placeholders: dict | None) -> None:
        """Zgłoszenie w Naprawach: utworzone (z treścią) albo usunięte; bez powtórzeń."""
        try:
            if placeholders is not None:
                if getattr(self, attr) == placeholders:
                    return                       # ta sama treść — bez ponownego zgłoszenia przy każdym odpytaniu
                ir.async_create_issue(self._hass, DOMAIN, issue_id, is_fixable=False, severity=_WARNING,
                                      translation_key=key, translation_placeholders=placeholders)
                setattr(self, attr, dict(placeholders))
            elif getattr(self, attr):
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
                setattr(self, attr, False)
        except Exception as err:  # noqa: BLE001 — zgłoszenie nie psuje połączenia
            _LOGGER.debug("Volcast modbus hub issue update failed: %s", type(err).__name__)

    def _sync_map_issue(self, report: MapReport | None) -> None:
        """Adresy i przyczyny (bez entity_id) — co poprawić w czujnikach."""
        if report is None or report.ok:
            self._sync_issue(self._map_issue, "_map_issue_open", HUB_MAP, None)
            return
        items = [f"{a}: missing" for a in report.missing]
        items += [f"{u.addr}: {u.cause}" for u in report.unsupported]
        items += [f"{a}: duplicate" for a in report.duplicates]
        if not report.registry_available:
            items.append("entity_registry_unavailable")
        more = " …" if len(items) > _ISSUE_LIST_MAX else ""
        self._sync_issue(self._map_issue, "_map_issue_open", HUB_MAP,
                         {"registers": ", ".join(items[:_ISSUE_LIST_MAX]) + more})

    def _sync_writer_issue(self, clash: list[HubWriter], ours: frozenset[int]) -> None:
        """Encje zapisu `modbus` na naszych rejestrach: encja i rejestr (`?` = nieustalony) — lokalnie."""
        if not clash:
            self._sync_issue(self._writer_issue, "_writer_issue_open", HUB_WRITER_ISSUE, None)
            return
        items = []
        for w in clash:
            regs = sorted(w.addrs & ours) if w.addrs is not None else []
            items.append(f"{w.entity_id or '?'}: {', '.join(str(a) for a in regs) or '?'}")
        more = " …" if len(items) > _ISSUE_LIST_MAX else ""
        self._sync_issue(self._writer_issue, "_writer_issue_open", HUB_WRITER_ISSUE,
                         {"entities": "; ".join(items[:_ISSUE_LIST_MAX]) + more})

    async def _start_client(self, transport) -> None:
        # Połączenie próbne nagrywa surowe ramki odczytów (diagnostyka, złote wektory — zamaskowane).
        self.client = RegisterClient(transport, self.profile, clock=self._clock, utcnow=self._utcnow, salt=self._salt,
                                     unreadable=self.unreadable | self.unsupported, record=self.trial)
        await self.async_confirm_identity()
        if self._stopped:                                # stop w trakcie potwierdzania: bez zegara
            await self._release()
            return
        try:
            self._unsub_timer = async_track_time_interval(self._hass, self._on_timer,
                                                          timedelta(seconds=self.poll_s))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Volcast direct polling timer not started: %s", type(err).__name__)

    async def async_stop(self, *, forget: bool = False) -> None:
        """Zatrzymanie; `forget=True` (usunięcie wpisu albo zmiana celu) kasuje też zgłoszenie tożsamości."""
        if forget:
            try:
                ir.async_delete_issue(self._hass, DOMAIN, self._identity_issue)
            except Exception:  # noqa: BLE001
                pass
        if self._stopped:
            return
        self._stopped = True
        if self._start_failures:
            try:
                ir.async_delete_issue(self._hass, DOMAIN, self._start_issue)
            except Exception:  # noqa: BLE001
                pass
        self._sync_issue(self._map_issue, "_map_issue_open", HUB_MAP, None)
        self._sync_issue(self._writer_issue, "_writer_issue_open", HUB_WRITER_ISSUE, None)
        if self._unsub_timer is not None:
            try:
                self._unsub_timer()
            except Exception:  # noqa: BLE001
                pass
            self._unsub_timer = None
        for task in (self._poll_task, self._timer_task, self._retry_task):
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        await self._release()
        self._listeners.clear()

    async def _release(self) -> None:
        """Zwolnienie hosta (tylko własnej rejestracji) i zamknięcie transportu; powtarzalne."""
        if self._host is not None:
            hosts = self._hass.data.get(DOMAIN, {}).get("direct_hosts", {})
            if hosts.get(self._host) is self:
                hosts.pop(self._host, None)
            self._host = None
        if self.client is not None:
            try:
                await self.client.transport.close()
            except Exception:  # noqa: BLE001
                pass

    @callback
    def _on_timer(self, _now=None) -> None:
        """Zegar HA: na pętli zdarzeń (bez `@callback` HA uruchomiłby to w wątku roboczym)."""
        if self._stopped or self._refused is not None or not self.poll_due() or self._poll_lock.locked():
            return
        if self._timer_task is not None and not self._timer_task.done():
            return
        create = getattr(self._entry, "async_create_background_task", None)
        if callable(create):
            self._timer_task = create(self._hass, self.async_poll(), name="volcast_direct_poll")
        else:
            self._timer_task = self._hass.async_create_task(self.async_poll())

    # ── odpytywanie ──

    async def async_poll(self) -> DirectReading | None:
        """Jeden odczyt; nie rzuca. Błąd łącza zostawia poprzedni odczyt (wiek rośnie)."""
        if self._stopped or self._refused is not None or self.client is None or self._poll_lock.locked():
            return self.reading if not self._stopped and self._refused is None else None
        async with self._poll_lock:
            self._poll_task = asyncio.current_task()
            try:
                return await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — odpytywanie nigdy nie rzuca
                _LOGGER.debug("Volcast direct poll failed: %s", type(err).__name__)
                return self.reading
            finally:
                self._poll_task = None

    async def async_read_fresh(self, after_mono: float | None) -> DirectReading | None:
        """Odczyt rozpoczęty PO `after_mono` (np. po końcu naszego zapisu): czeka na odpytywanie w toku
        (najwyżej `fresh_wait_s`), potem czyta sam. None = brak świeżego odczytu (nie zgadujemy)."""
        def fresh(r):
            return r is not None and (after_mono is None or r.at_mono > after_mono)

        if self._stopped or self._refused is not None or self.client is None:
            return None
        if fresh(self.reading):
            return self.reading
        try:
            await asyncio.wait_for(self._poll_lock.acquire(), self.fresh_wait_s)
        except asyncio.TimeoutError:
            return None
        try:
            if not fresh(self.reading) and not self._stopped:
                # Osobne zadanie: zatrzymanie połączenia anuluje odczyt, nigdy zadanie wołającego (wykonawcy).
                task = asyncio.get_running_loop().create_task(self._poll())
                self._poll_task = task
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    task.cancel()
                    raise
                finally:
                    self._poll_task = None
                if not task.cancelled() and task.exception() is not None:
                    _LOGGER.debug("Volcast direct fresh read failed: %s", type(task.exception()).__name__)
        finally:
            self._poll_lock.release()
        return self.reading if fresh(self.reading) else None

    async def _poll(self) -> DirectReading | None:
        now = self._clock()
        self._last_poll = now
        if self._last_static is None or now - self._last_static >= STATIC_CHECK_S:
            self.static_conflicts = await self._static_check(self._host or "")
            if self.static_conflicts:
                _LOGGER.warning("Volcast direct control: another integration now uses this inverter")
        if self.identity == "mismatch":
            if self._identity_tried is None or now - self._identity_tried >= IDENTITY_RETRY_S:
                await self.async_confirm_identity()
            self._notify()
            return self.reading
        self._observe_link()
        self._hub_binding_lost()                       # hub HA: mapa czujników przy każdym odpytaniu
        if self.identity == "confirmed" and self._identity_stale(now):
            self.identity = "pending"                  # UDP: tożsamość co godzinę, przed odczytem stanu
        if self.identity != "confirmed" or self._recheck:
            await self.async_confirm_identity()
            if self.identity == "mismatch":
                self._notify()
                return None
            if self.identity == "pending":             # niepotwierdzone po zdarzeniu łącza: bez odczytu stanu
                self._tick_monitor(now)
                return self.reading
        try:
            reading = await self.client.read_state()
        except TransportError as err:
            _LOGGER.debug("Volcast direct read failed: %s", type(err).__name__)
            reading = None
        self._observe_link()
        if self._recheck and reading is not None:
            await self.async_confirm_identity()        # ponowne połączenie w trakcie odczytu
        if self.identity in ("mismatch", "pending"):
            reading = None                             # odczyt z urządzenia, którego nie potwierdziliśmy
        if reading is not None:
            self.reading = reading
        self._tick_monitor(now)
        if self.identity == "confirmed":
            await self._read_work_mode(now)
        return self.reading if self.identity != "mismatch" else None

    async def _read_work_mode(self, now: float) -> None:
        """Tryb pracy falownika: JEDEN rejestr (FC3), tylko odczyt, co `work_mode.poll_s`. Idzie na tym samym
        kliencie i pod tym samym `_poll_lock` co bloki (jedno gniazdo — moduł Wi-Fi GoodWe toleruje jednego
        klienta i odpowiada poprzednią odpowiedzią na ramki o tej samej długości; cykl kończy się odczytem
        1 rejestru, więc odczyt trybu pracy idzie przez ten sam mechanizm rozdzielania co bloki cyklu —
        blok rozdzielający o innej długości, gdy poprzedni odczyt miał tę samą). Nie przy kolizji; błąd nie zmienia stanu i nie
        rusza wykonawcy ani zgody; bez zapisów i bez budżetu NVM."""
        wm = getattr(self.profile, "work_mode", None)
        if wm is None or self.conflict or self.client is None or self._work_mode_off:
            return
        if self.hub_mode and (self.map_report is None or wm.read_addr not in self.map_report.registers):
            return                       # hub HA: wyłącznie rejestry z mapy czujników
        if self._work_mode_at is not None and now - self._work_mode_at < self._work_mode_interval(wm.poll_s):
            return
        self._work_mode_at = now
        value = None
        unusable = False                 # odpowiedź nieużyteczna (nie cisza): nie ponawiać tego rejestru
        try:
            if self.client._disambiguate:
                block = make_block(wm.read_addr, 1, 3)
                async with self.client._session():
                    word = (await self.client._separated(block, [0.0], tries=1))[0]
            else:
                word = (await self.client.read_block(wm.read_addr, 1, 3, tries=1))[0]
            if wm.read_type == "u16":
                value = int(word)
            elif wm.read_type == "i16":
                value = int(word) - 0x10000 if word >= 0x8000 else int(word)
        except RequestTimeout as err:
            # cisza = przekroczenie czasu (liczone do progu); ramka złej długości = nieużyteczna odpowiedź
            unusable = not err.silent
            _LOGGER.debug("Volcast direct work mode read failed: %s", type(err).__name__)
        except ModbusException:
            unusable = True              # wyjątek Modbus: rejestr nieobsługiwany
        except TransportError as err:
            _LOGGER.debug("Volcast direct work mode read failed: %s", type(err).__name__)
        except (IndexError, TypeError, ValueError):
            value = None
            unusable = True
        if unusable:
            # nieużyteczna odpowiedź (wyjątek Modbus, ramka złej długości) wyłącza odczyt od razu i na stałe:
            # na modelu bez tego rejestru każda próba dawałaby ramki obce do liczników transportu
            # (fałszywa kolizja)
            if not self._work_mode_off:
                self._work_mode_off = True
                _LOGGER.info("Volcast direct: operation mode register %d gives no usable reply; "
                             "not reading it again on this connection", wm.read_addr)
        elif value is None:
            # cisza (zgubiony datagram UDP przy tries=1): od WORK_MODE_FAIL_LIMIT z rzędu odstęp rośnie
            # wykładniczo zamiast wyłączać odczyt. W tym czasie zgłaszany stan to ostatni udany odczyt —
            # może być nieaktualny, dopóki kolejny odczyt się nie uda.
            self._work_mode_fails += 1
        else:
            self._work_mode_fails = 0
        self.work_mode.update(classify_direct(value, wm.neutral_values))

    def _work_mode_interval(self, poll_s: float) -> float:
        """Odstęp do następnego odczytu trybu pracy: `poll_s`, po WORK_MODE_FAIL_LIMIT cichych przekroczeniach
        czasu z rzędu `poll_s` x 2, x 4, ... (najwyżej doba)."""
        over = self._work_mode_fails - WORK_MODE_FAIL_LIMIT + 1
        if over <= 0:
            return poll_s
        return min(poll_s * 2 ** min(over, 32), max(WORK_MODE_BACKOFF_MAX_S, poll_s))

    def _reset_work_mode_backoff(self) -> None:
        """Łącze nawiązane na nowo i tożsamość potwierdzona: przerwa w odczytach trybu pracy od zera
        (nie dotyczy wyłączenia po nieużytecznej odpowiedzi)."""
        self._work_mode_fails = 0

    def _tick_monitor(self, now: float) -> None:
        stats = self.stats
        self.monitor.note_stats(stats, now)
        self.monitor.tick(now)
        self._notify()

    def _identity_stale(self, now: float) -> bool:
        if self.target.get("transport") != "goodwe_udp" or self._identity_at is None:
            return False
        return not 0.0 <= now - self._identity_at < IDENTITY_UDP_MAX_AGE_S

    def _observe_link(self) -> None:
        """Zdarzenie łącza (ponowne połączenie, zerwanie, seria przekroczeń czasu) → ponowne sprawdzenie;
        potwierdzona tożsamość spada do `pending` aż do udanego sprawdzenia."""
        stats = self.stats
        event = stats.reconnects > self._seen_reconnects or stats.peer_resets > self._seen_peer_resets
        self._seen_reconnects = stats.reconnects
        self._seen_peer_resets = stats.peer_resets
        if event or stats.consecutive_timeouts >= TIMEOUT_STREAK:
            self._recheck = True
            if self.identity == "confirmed":
                self.identity = "pending"

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception as err:  # noqa: BLE001 — słuchacz nie psuje odpytywania
                _LOGGER.debug("Volcast direct listener failed: %s", type(err).__name__)

    # ── kolizje, tożsamość ──

    async def _static_check(self, host: str) -> tuple[str, ...]:
        self._last_static = self._clock()
        try:
            snaps = await async_entry_snaps(self._hass, self._entry.entry_id, resolve=self._resolve)
            if self.hub_mode:
                hub, unit = str(self.target.get("hub")), int(self.target.get("unit_id"))
                ours, writers = write_addresses(self.profile), hub_writers(self._hass)
                self._sync_writer_issue(conflicting_writers(hub, unit, writers or (), ours), ours)
                return hub_static_conflicts(hub, unit, snaps, writers, ours)
            return static_conflicts(host, snaps)
        except Exception as err:  # noqa: BLE001 — „nie wiemy” = kolizja
            _LOGGER.debug("Volcast direct conflict check failed: %s", type(err).__name__)
            return ("unknown",)

    async def async_confirm_identity(self) -> str:
        """Odczyt rejestrów identyfikacyjnych i porównanie z odciskiem celu."""
        if self.client is None or self._stopped:
            return self.identity
        expected = _expected_fp(self.target.get("device_fp"))
        if expected is None:                           # bez oczekiwanego odcisku nie ma czego potwierdzać
            self.identity = "unknown"
            return self.identity
        now = self._clock()
        self._identity_tried = now
        if self._hub_binding_lost():
            return self.identity             # czujniki (także tożsamości) nie czytają dowodnie z celu zapisu
        try:
            fp = await self.client.read_identity()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Volcast direct identity read failed: %s", type(err).__name__)
            fp = None
        self._observe_link()
        previous = self.identity
        if fp is None or self._stopped:
            # nieudane sprawdzenie: `pending`/`mismatch` trwają, pierwsze sprawdzenie zostaje `unknown`
            self.identity = previous if previous in ("pending", "mismatch") else "unknown"
            return self.identity
        if self._recheck:
            self._reset_work_mode_backoff()            # potwierdzenie po zdarzeniu łącza
        self._recheck = False
        self._identity_at = now
        if hmac.compare_digest(fp.encode(), expected.encode()):
            self.identity = "confirmed"
            if previous != "confirmed" and previous != "pending":
                ir.async_delete_issue(self._hass, DOMAIN, self._identity_issue)
        else:
            self.identity = "mismatch"
            self.reading = None
            if previous != "mismatch":
                _LOGGER.warning("Volcast direct control: a different device answers at the inverter address")
                ir.async_create_issue(self._hass, DOMAIN, self._identity_issue, is_fixable=False,
                                      severity=_WARNING, translation_key="direct_identity_changed")
        return self.identity
