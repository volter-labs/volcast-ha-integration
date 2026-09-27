"""Połączenie bezpośrednie z falownikiem w HA: cykl życia, odpytywanie, kolizje, tożsamość.

Warstwa HA robi wyłącznie I/O i cykl życia; decyzje są w rdzeniu (`core/`).

* Start: kontrola adresu (literał lokalny, bez gniazda przy odmowie), rejestr hostów wpisów
  (`hass.data[DOMAIN]["direct_hosts"]` — jedno połączenie na host), kolizja statyczna z innymi
  wpisami (fail-closed). Odmowa = żadnego gniazda.
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

Logi: wyłącznie nazwy klas błędów i liczniki — nigdy host, port, numer seryjny ani numer loggera.
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
from ..core.control.conflict import CONFLICT_DOMAINS, SELF_DOMAIN, ContentionMonitor, EntrySnap, static_conflicts
from ..core.discovery.known import HOST_KEYS
from ..core.modbus.client import RegisterClient
from ..core.modbus.reading import DirectReading
from ..core.transports.base import TransportConfig, TransportError, TransportStats, check_target, validate_config
from ..core.transports.factory import make_transport

_LOGGER = logging.getLogger(__name__)
_WARNING = getattr(getattr(ir, "IssueSeverity", None), "WARNING", "warning")

STATIC_CHECK_S = 600.0              # kolizja statyczna sprawdzana znowu co 10 min
IDENTITY_RETRY_S = 600.0            # przy niezgodnej tożsamości — tylko sprawdzanie co 10 min
IDENTITY_UDP_MAX_AGE_S = 3600.0     # UDP (bez połączenia): tożsamość co godzinę
IDENTITY_MAX_AGE_S = 3600.0         # przed sesją zapisów: potwierdzenie nie starsze niż godzina
TIMEOUT_STREAK = 3
RESOLVE_TIMEOUT_S = 2.0
FRESH_WAIT_S = 10.0                 # powrót czeka na odpytywanie w toku najwyżej tyle
_FP_HEX = frozenset("0123456789abcdef")

Resolver = Callable[[str], Awaitable[Iterable[str] | None]]


def target_fingerprint(target: Mapping, salt: bytes) -> str:
    """Odcisk celu połączenia (własność w trybie bezpośrednim): HMAC z solą instalacji — bez adresu jawnie."""
    msg = f"{target.get('transport')}|{target.get('host')}|{target.get('port')}|{target.get('unit_id')}"
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
    """Adresy hostów wpisu; None, gdy którykolwiek nie daje się ustalić (fail-closed)."""
    if not hosts:
        return None
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

    Hosty z `HOST_KEYS` w `data` i `options`; nazwy rozwiązywane (limit `timeout_s`); brak hosta,
    nazwa nierozwiązywalna albo przekroczony czas → `addresses=None` (kolizja, fail-closed).
    """
    resolve = resolve or _default_resolve
    snaps: list[EntrySnap] = []
    for entry in hass.config_entries.async_entries():
        domain = getattr(entry, "domain", None)
        disabled = getattr(entry, "disabled_by", None) is not None
        if domain == SELF_DOMAIN:
            snaps.append(EntrySnap(domain, _direct_host(entry), disabled,
                                   is_self=getattr(entry, "entry_id", None) == self_entry_id))
        elif domain in CONFLICT_DOMAINS:
            addresses = None if disabled else await _addresses(_hosts(entry), resolve, timeout_s)
            snaps.append(EntrySnap(domain, addresses, disabled))
    return snaps


class DirectConnection:
    def __init__(self, hass, entry, profile, target: Mapping, *, trial: bool, salt: bytes,
                 poll_s: float = DIRECT_POLL_S, transport_factory=make_transport, clock=time.monotonic,
                 utcnow=dt_util.utcnow, allow_loopback: bool = False, unreadable: Iterable[str],
                 resolve: Resolver | None = None) -> None:
        self._hass = hass
        self._entry = entry
        self.profile = profile
        self.target = dict(target)
        self.trial = trial
        self._salt = bytes(salt)
        self.poll_s = float(poll_s)
        self._factory = transport_factory
        self._clock = clock
        self._utcnow = utcnow
        self._allow_loopback = allow_loopback
        self.unreadable = frozenset(unreadable)
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

    # ── stan ──

    @property
    def stats(self) -> TransportStats:
        t = self.client.transport if self.client is not None else None
        return getattr(t, "stats", None) or TransportStats()

    @property
    def conflict(self) -> bool:
        return bool(self.static_conflicts) or self.monitor.state == "conflict"

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
        try:
            cfg = self._config()
            host = check_target(str(self.target.get("host")), allow_loopback=self._allow_loopback)
        except (ValueError, TypeError, KeyError):
            cfg, host = None, None
        if cfg is None or host is None:
            self._refused = "bad_target"
            _LOGGER.warning("Volcast direct connection refused: target is not a valid local address")
            return
        hosts = self._hass.data.setdefault(DOMAIN, {}).setdefault("direct_hosts", {})
        if hosts.get(host) is not None:                  # inne połączenie (także tego samego wpisu) żyje
            self._refused = "direct_in_use"
            _LOGGER.warning("Volcast direct connection refused: the inverter address is already in use")
            return
        clash = await self._static_check(host)
        if self._stopped:                                # zatrzymane w trakcie startu: nic nie zostawiamy
            return
        if hosts.get(host) is not None:
            self._refused = "direct_in_use"
            _LOGGER.warning("Volcast direct connection refused: the inverter address is already in use")
            return
        if clash and self.allow_conflicted_restore and SELF_DOMAIN not in clash:
            self.static_conflicts = clash
            _LOGGER.warning("Volcast direct connection: another integration uses this inverter — connecting "
                            "only to return it to its own settings")
        elif clash:
            self._refused = "direct_in_use" if SELF_DOMAIN in clash else f"direct_conflict:{clash[0]}"
            self.static_conflicts = ()
            _LOGGER.warning("Volcast direct connection refused: another integration uses this inverter")
            return
        hosts[host] = self
        self._host = host
        try:
            await self._start_registered(cfg)
        except BaseException:
            await self._release()                        # nieoczekiwany wyjątek nie zostawia rejestracji hosta
            raise

    async def _start_registered(self, cfg: TransportConfig) -> None:
        try:
            transport = self._factory(cfg, allow_loopback=self._allow_loopback)
        except Exception as err:  # noqa: BLE001 — zła konfiguracja: bez połączenia
            _LOGGER.warning("Volcast direct connection not started: %s", type(err).__name__)
            self._refused = "bad_target"
            await self._release()
            return
        self.client = RegisterClient(transport, self.profile, clock=self._clock, utcnow=self._utcnow, salt=self._salt,
                                     unreadable=self.unreadable)
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
        if self._unsub_timer is not None:
            try:
                self._unsub_timer()
            except Exception:  # noqa: BLE001
                pass
            self._unsub_timer = None
        for task in (self._poll_task, self._timer_task):
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
                self._poll_task = asyncio.current_task()
                try:
                    await self._poll()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Volcast direct fresh read failed: %s", type(err).__name__)
                finally:
                    self._poll_task = None
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
        return self.reading if self.identity != "mismatch" else None

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
