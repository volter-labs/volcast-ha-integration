"""Przepięcie falownika po zmianie adresu od końca do końca: fałszywa sieć + prawdziwy wykonawca i Relocator.

Falownik to symulator z pętli zwrotnej (`tests/sim`) za fałszywą siecią: ta mapuje adresy z bloków
dokumentacyjnych (RFC 5737) na symulatory, więc falownik „zmienia adres" przez przepięcie trasy. Rzeczywiste
są: połączenie bezpośrednie, wykonawca (hamulec trybu wymuszonego, własność, magazyn), Relocator (skan
`sweep`, identyfikacja `discover`, decyzja `decide_relocation`, kolizje), słuchacz opcji i rozładunek wpisu,
zgłoszenia w Naprawach (`sync_readings_issue`). Zegar jest fałszywy (kroki po 30 s), sieć — atrapą na poziomie
transportu i gniazd (rozgłoszenie / unicast UDP, TCP connect).

Przybliżenia (świadome): zasilanie `readings_missing` to atrapa nad wiekiem odczytu połączenia (prawdziwy
nadajnik telemetrii wymaga całego wpisu); przeładowanie wpisu to `async_unload_control` + ponowne złożenie
wykonawcy i połączenia (bez platform HA i bez fetchera planu — plan wraca z magazynu); jeden restart procesu
to nowy `hass.data` przy tym samym magazynie.
"""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import logging
import socket
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

import custom_components.volcast as integ
from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control import direct as direct_mod
from custom_components.volcast.control import direct_search as ds
from custom_components.volcast.control import executor as ex_mod
from custom_components.volcast.control import relocator as rl
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.device_io import DirectIO
from custom_components.volcast.control.direct import DirectConnection
from custom_components.volcast.control.executor import VolcastExecutor
from custom_components.volcast.control.store import _SALT_CACHE, ControlStore
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.discovery import identify as identify_mod
from custom_components.volcast.core.discovery import sweep as sweep_mod
from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.profile import profile_from_dict
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.slot import parse_schedule
from custom_components.volcast.core.transports.base import RequestTimeout
from custom_components.volcast.core.transports.base import check_target as real_check_target
from custom_components.volcast.core.transports.factory import make_transport
from custom_components.volcast.diagnostics import async_get_config_entry_diagnostics
from tests.control.ha_fakes import NOW
from tests.control.test_executor import plan
from tests.control.test_executor_direct import (GW_V, MODE, SALT, TickClock, _thaw, gw_raw_word,
                                                gw_target)
from tests.sim.device import Faults, RegisterBank
from tests.sim.fixtures import GOODWE_UNREADABLE, goodwe_words
from tests.sim.servers import goodwe_udp_server, modbus_tcp_server

OLD, NEW, TWIN, HA_IP = "192.0.2.10", "192.0.2.42", "192.0.2.77", "192.0.2.5"
ADDRESSES = (OLD, NEW, TWIN, HA_IP)
STEP_S = 30.0
NEUTRAL_MODE_WORD = 1
CHARGE_MODE_WORD = 11
# Plan ładowania kończy się między cyklem z przepięciem (240 s od startu) a pierwszym cyklem po nim (270 s):
# czas wykonawcy płynie z zegarem testu i startuje o 30 s po początku slotu.
EXPIRES_AFTER_RELOCATION_S = 290
FORCED_ISSUES = {"direct_unreachable", "forced_mode_stuck"}


# ── fałszywa sieć ─────────────────────────────────────────────────────────


def _dead_port() -> int:
    """Port bez nasłuchu (po zamknięciu gniazda) — adres bez falownika."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeNet:
    """Adres → symulator. Trasa znika i pojawia się w trakcie testu (`drop`, `put`, `move`)."""

    def __init__(self, monkeypatch) -> None:
        self.devices: dict[str, tuple] = {}              # host → (sim, rodzaj transportu)
        self.broadcast = True                            # False = rozgłoszenie bez odpowiedzi (HA w kontenerze)
        self.broadcasts = 0
        self.unicasts: list[str] = []
        self.connects: list[str] = []
        self._doc_nets = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24"))
        for mod in (direct_mod, identify_mod):            # adres dokumentacyjny jest „lokalny" tylko w tym teście
            monkeypatch.setattr(mod, "check_target", self._check_target)
        monkeypatch.setattr(ds, "make_transport", self.make)       # także `search_factory` i `_default_factory`
        # Skan chodzi po RFC 1918; testy używają bloków dokumentacyjnych — dopuszczamy je tylko w teście.
        monkeypatch.setattr(sweep_mod, "_PRIVATE", sweep_mod._PRIVATE + self._doc_nets)

    def _check_target(self, host, *, allow_loopback=False):
        try:
            if any(ipaddress.ip_address(host) in n for n in self._doc_nets):
                return host
        except ValueError:
            pass
        return real_check_target(host, allow_loopback=allow_loopback)

    def put(self, host, sim, kind) -> None:
        self.devices[host] = (sim, kind)

    def drop(self, host) -> None:
        self.devices.pop(host, None)

    def move(self, old, new) -> None:
        self.devices[new] = self.devices.pop(old)

    # transport: ramki idą na symulator trasowany pod tym adresem; gdy trasa zniknie albo wskaże inne urządzenie,
    # wymiana kończy się ciszą (ramka wychodzi, odpowiedzi brak) — tak jak przy falowniku pod innym adresem
    def make(self, cfg, **kw):
        kw.pop("clock", None)
        kw["allow_loopback"] = True
        found = self.devices.get(cfg.host)
        sim = found[0] if found is not None else None
        transport = make_transport(replace(cfg, host="127.0.0.1", port=sim.port if sim is not None else _dead_port(),
                                           timeout_s=1.0 if sim is not None else 0.1, gap_s=0.0, read_tries=2,
                                           backoff_min_s=0.01, backoff_max_s=0.02), **kw)
        real = transport._transact

        async def routed(req, on_send=None):
            now = self.devices.get(cfg.host)
            if sim is not None and now is not None and now[0] is sim:
                return await real(req, on_send)
            if on_send is not None:
                on_send()
            transport.stats.requests += 1
            transport.stats.timeouts += 1
            transport.stats.consecutive_timeouts += 1
            await asyncio.sleep(0.01)
            raise RequestTimeout(silent=True)

        transport._transact = routed
        return transport

    async def udp_probe(self, *, target=None, timeout=None, **_kw):
        if target is None:
            self.broadcasts += 1
            hosts = [h for h, (_s, k) in self.devices.items() if k == "goodwe_udp"] if self.broadcast else []
        else:
            self.unicasts.append(target)
            hosts = [target] if target in self.devices and self.devices[target][1] == "goodwe_udp" else []
        return SimpleNamespace(replies=[SimpleNamespace(ip=h, logger_serial=None) for h in hosts])

    async def connect(self, host, _port, _timeout=0.5) -> bool:
        self.connects.append(host)
        return host in self.devices and self.devices[host][1] == "modbus_tcp"

    async def adapters(self):
        return [{"enabled": True, "ipv4": [{"address": HA_IP, "network_prefix": 24}]}]


class IssueBook:
    """Aktywne zgłoszenia w Naprawach (id → argumenty) i ich historia."""

    def __init__(self, monkeypatch) -> None:
        self.active: dict[str, dict] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []

        def create(hass, domain, issue_id, **kw):
            self.created.append(issue_id)
            self.active[issue_id] = kw

        def delete(hass, domain, issue_id):
            self.deleted.append(issue_id)
            self.active.pop(issue_id, None)

        for mod in (rt_mod, ex_mod, direct_mod):
            monkeypatch.setattr(mod.ir, "async_create_issue", create)
            monkeypatch.setattr(mod.ir, "async_delete_issue", delete)

    def keys(self) -> set[str]:
        """Aktywne zgłoszenia łącza po kluczu tłumaczenia (bez id wpisu)."""
        return {kw.get("translation_key") for kw in self.active.values()} & FORCED_ISSUES

    def ever(self, key: str) -> bool:
        return any(i.startswith(key) for i in self.created)


@pytest.fixture
def net(monkeypatch):
    return FakeNet(monkeypatch)


@pytest.fixture
def book(monkeypatch):
    return IssueBook(monkeypatch)


@pytest_asyncio.fixture
async def spawn():
    """Dodatkowe symulatory (obcy falownik, bliźniak); zamykane po teście."""
    servers = []

    async def make(kind, words=None, **bank_kw):
        bank = RegisterBank(words if words is not None else goodwe_words(), unreadable=GOODWE_UNREADABLE, **bank_kw)
        start = goodwe_udp_server if kind == "goodwe_udp" else modbus_tcp_server
        server = await start(bank, Faults())
        servers.append(server)
        return server

    yield make
    for server in servers:
        await server.close()


def _words_with_serial(serial: str) -> dict[int, int]:
    """Rejestry falownika z innym numerem seryjnym (16 znaków ASCII w 35003…35010) — inny egzemplarz tego modelu."""
    raw = serial.encode("ascii")
    assert len(raw) == 16
    words = goodwe_words()
    for i in range(8):
        words[35003 + i] = int.from_bytes(raw[2 * i:2 * i + 2], "big")
    return words


def _charge_plan(ttl_s: int, sid: str = "gc") -> dict:
    """Ładowanie z sieci przez `ttl_s` s od 10:00 (czas wykonawcy płynie razem z zegarem testu)."""
    end = (NOW + timedelta(seconds=ttl_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return plan(sid=sid, slots=[{"from": "2026-09-23T10:00:00Z", "to": end, "mode": "charge",
                                 "charge_source": "grid", "power_w": 3000, "price_pln_kwh": 0.3}])


# ── wpis: prawdziwi wykonawca i Relocator, atrapy sieci i HA ──────────────


class Env:
    def __init__(self, make_hass, net: FakeNet, book: IssueBook, profile, target: dict) -> None:
        self.make_hass, self.net, self.book, self.profile = make_hass, net, book, profile
        self.clock = TickClock()
        self.t0 = self.clock()
        self.options = {"control_mode": "direct", "direct_target": dict(target)}
        self.entry = SimpleNamespace(domain=DOMAIN, entry_id="e1", options=self.options, data={},
                                     disabled_by=None, add_update_listener=lambda _l: None,
                                     async_create_background_task=lambda hass, coro, name:
                                     asyncio.get_running_loop().create_task(coro))
        self.store = ControlStore(make_hass(), "e1")     # trwały magazyn: przeżywa restart procesu
        self.updates: list[dict] = []
        self.pending: list[asyncio.Task] = []
        self.reloads = 0

    # ── składanie / rozładunek ──

    def _new_process(self) -> None:
        hass = self.make_hass(entries=[self.entry])
        hass.data[_SALT_CACHE] = SALT
        hass.data[DOMAIN] = {}
        hass.config_entries.async_get_entry = lambda eid: self.entry if eid == self.entry.entry_id else None
        hass.config_entries.async_update_entry = self._update_entry
        hass.config_entries.async_reload = self._reload
        self.hass = hass

    def _update_entry(self, entry, *, options) -> None:
        self.updates.append(copy.deepcopy(options))
        entry.options = options
        self.options = options
        self.pending.append(asyncio.ensure_future(integ._async_update_listener(self.hass, entry)))

    def build(self) -> None:
        t = self.entry.options["direct_target"]
        self.conn = DirectConnection(self.hass, self.entry, self.profile, t, trial=False, salt=SALT,
                                     transport_factory=self.net.make, allow_loopback=True, clock=self.clock,
                                     unreadable=t.get("unreadable", ()))
        self.io = DirectIO(self.conn, self.profile, trial=False, unreadable=t.get("unreadable", ()),
                           capabilities=t.get("capabilities"), salt=SALT, clock=self.clock)
        self.ex = VolcastExecutor(self.hass, self.entry, choice=ProfileChoice(self.profile, None, None), mapped={},
                                  rated_power_w=8000.0, store=self.store, clock=self.clock,
                                  utcnow=lambda: NOW + timedelta(seconds=30 + self.clock() - self.t0), io=self.io)
        self.rt = SimpleNamespace(
            executor=self.ex, direct=self.conn, unsubs=[], options_at_setup=dict(self.entry.options),
            fetcher=None, verification=None, loads_executor=None, hub=None, live=None, channel=None,
            telemetry=SimpleNamespace(async_stop=_noop), entry=self.entry, choice=ProfileChoice(self.profile, None, None),
            mapped={}, last_probe=[])
        self.rel = rl.Relocator(self.hass, self.entry, self.conn,
                                control_change=lambda old, new: rt_mod.control_identity_changed(self.rt, old, new),
                                onboarding_active=lambda: False, adapters=self.net.adapters,
                                udp_probe=self.net.udp_probe, connect=self.net.connect, clock=self.clock)
        self.rt.unsubs.append(self.rel.stop)
        self.rt.relocator = self.rel
        self.hass.data[DOMAIN][self.entry.entry_id] = {"control": self.rt}

    async def start(self, plan_raw: dict | None = None) -> "Env":
        if not hasattr(self, "hass"):
            self._new_process()
        self.build()
        await self.conn.async_start()
        await self.ex.async_start()
        if plan_raw is not None:
            await self.ex.async_on_plan(plan_raw, parse_schedule(plan_raw))
            await self.ex.async_set_consent(True)
            await self.ex.async_set_local_switch(True)
        await self.conn.async_poll()
        return self

    async def _reload(self, _entry_id) -> None:
        self.reloads += 1
        await rt_mod.async_unload_control(self.hass, self.rt)
        await self.start()

    async def restart_process(self) -> None:
        """Restart HA: rozładunek jak przy zatrzymaniu, nowy `hass.data`, ten sam magazyn i opcje."""
        await rt_mod.async_unload_control(self.hass, self.rt)
        self._new_process()
        await self.start()

    async def close(self) -> None:
        for task in self.pending:
            task.cancel()
        self.rel.stop()
        await self.ex.async_stop()
        await self.conn.async_stop()

    # ── krok zegara ──

    def sync(self) -> None:
        rt_mod.sync_readings_issue(self.hass, self.entry, _Readings(self.conn), rt=self.rt)

    async def settle(self) -> None:
        while self.pending:
            done, self.pending = self.pending, []
            await asyncio.gather(*done)

    async def step(self, seconds: float = STEP_S, *, before_check=None) -> None:
        """Czas płynie; odczyt, cykl wykonawcy, Naprawy, wyzwalacz Relocatora (do końca wyszukiwania),
        słuchacz opcji i ewentualne przeładowanie wpisu."""
        self.clock.advance(seconds)
        await self.conn.async_poll()
        await self.ex.async_tick()
        self.sync()
        if before_check is not None:
            before_check()
        self.rel.check()
        if self.rel.task is not None:
            await self.rel.task
        await self.settle()

    async def run_until(self, done, *, limit_s: float = 900.0) -> float:
        """Kroki do spełnienia warunku; zwraca czas od startu pętli (s). Przekroczenie limitu = błąd."""
        t = 0.0
        while not done():
            assert t < limit_s, "condition not met within the limit"
            await self.step()
            t += STEP_S
        return t

    # ── podglądy ──

    @property
    def host(self) -> str:
        return self.entry.options["direct_target"]["host"]

    async def diagnostics(self) -> dict:
        entry = SimpleNamespace(entry_id=self.entry.entry_id, options=self.entry.options,
                                data={"api_key": "vk_x"})
        return await async_get_config_entry_diagnostics(self.hass, entry)


async def _noop() -> None:
    return None


class _Readings:
    """Atrapa telemetrii dla `readings_missing`: odczyty „bezpośrednie" tylko, gdy ostatni jest świeży."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def readings_report(self):
        src = "direct" if self.conn.age_s() < 60.0 else "none"
        return {"readings_source": {"soc": src, "pv_power_w": src, "grid_power_w": src}, "readings_dropped": []}


async def _env(make_hass, net, book, sim, kind="goodwe_udp", *, plan_raw, profile=GW_V, target=None) -> Env:
    sim.bank.poke(MODE, NEUTRAL_MODE_WORD)           # falownik w trybie zwykłym: tryb wymuszony zapisze dopiero plan
    net.put(OLD, sim, kind)
    env = Env(make_hass, net, book, profile, target or gw_target(sim, kind, host=OLD))
    await env.start(plan_raw)
    await env.ex.async_tick()
    return env


async def _assert_private(env: Env, book: IssueBook, caplog) -> None:
    """Żaden adres nie trafia do diagnostyki, placeholderów Napraw ani logów."""
    blob = json.dumps(await env.diagnostics()) + json.dumps([kw.get("translation_placeholders") for kw in
                                                              book.active.values()], default=str)
    blob += caplog.text + json.dumps([r.getMessage() for r in caplog.records])
    for addr in ADDRESSES:
        assert addr not in blob
    assert all(not kw.get("translation_placeholders") for kw in book.active.values())


def _device_writes(sim) -> list[tuple]:
    return [e for e in sim.log if e[0] in (6, 16)]


def _only_host_changed(before: dict, after: dict, new_host: str) -> None:
    assert {k: v for k, v in after.items() if k != "direct_target"} == \
        {k: v for k, v in before.items() if k != "direct_target"}
    old_t, new_t = before["direct_target"], after["direct_target"]
    assert new_t["host"] == new_host and {k: v for k, v in new_t.items() if k != "host"} == \
        {k: v for k, v in old_t.items() if k != "host"}


# ── (1) i warianty: falownik zmienia adres w trakcie trybu wymuszonego ───


async def _move_and_recover(env, net, sim, book, caplog, *, new_host=NEW):
    """Wspólny przebieg: tryb wymuszony, zmiana adresu, wyszukiwanie, przepięcie, pierwszy cykl na nowym adresie."""
    bank = sim.bank
    assert gw_raw_word(bank, MODE) == CHARGE_MODE_WORD and env.ex.owned
    owner_before = (await env.store.async_load()).owner
    snapshot_before = dict((await env.store.async_load()).snapshot)
    await env.step()
    await env.step()
    assert book.keys() == set() and env.updates == []              # sprawny falownik: bez Napraw
    options_before = copy.deepcopy(env.options)
    net.move(OLD, new_host)                                       # falownik zmienia adres (DHCP)
    t_moved = 0.0
    while not env.updates:
        assert t_moved < 600.0
        await env.step()
        t_moved += STEP_S
    # ≤ 180 s ciszy wyzwala wyszukiwanie (próg + jeden takt sprawdzania od ostatniego odczytu)
    assert t_moved <= rl.RELOCATE_AFTER_S + 2 * STEP_S
    assert env.reloads == 1 and env.host == new_host
    assert len(env.updates) == 1
    _only_host_changed(options_before, env.updates[0], new_host)   # zapis zmienia WYŁĄCZNIE host celu
    # obie Naprawy zdążyły się pojawić przed przepięciem i jeszcze stoją (nikt nie zdjął ich po przeładowaniu)
    assert env.book.ever("direct_unreachable") and env.book.ever("forced_mode_stuck")
    assert book.keys() == FORCED_ISSUES
    n = len(bank.writes)
    assert gw_raw_word(bank, MODE) == CHARGE_MODE_WORD            # rozładunek po martwym łączu nic nie zmienił
    # własność przeżyła przeładowanie: ten sam odcisk urządzenia i migawka, rekord bez Naprawy „porzucony"
    stored = await env.store.async_load()
    assert env.ex.owned and stored.snapshot == snapshot_before
    assert stored.owner["device"] == owner_before["device"]
    assert stored.owner["device_target"] == owner_before["device_target"]
    assert not any(i.startswith("control_record_dropped") for i in book.created)
    # pierwszy cykl wykonawcy na nowym adresie zdejmuje tryb wymuszony
    await env.step()
    assert len(bank.writes) > n and gw_raw_word(bank, MODE) == NEUTRAL_MODE_WORD
    assert "neutral_brake" in env.ex.last_decision.notes or env.ex.last_decision.status == "write"
    assert book.keys() == set()                                   # obie Naprawy zniknęły
    assert not [i for i in book.active if i.startswith(("direct_unreachable", "forced_mode_stuck"))]
    assert env.rel.state.relocations == 1 and env.rel.state.last_outcome == "relocated"
    await _assert_private(env, book, caplog)


@pytest.mark.asyncio
async def test_inverter_changes_address_during_a_forced_mode(make_hass, net, book, goodwe_udp_sim, caplog):
    caplog.set_level(logging.DEBUG)
    env = await _env(make_hass, net, book, goodwe_udp_sim, plan_raw=_charge_plan(EXPIRES_AFTER_RELOCATION_S))
    try:
        await _move_and_recover(env, net, goodwe_udp_sim, book, caplog)
        assert not net.unicasts or NEW in net.unicasts            # skan szedł po /24 starego adresu
    finally:
        await env.close()


@pytest.mark.asyncio
async def test_plan_still_valid_after_relocation_keeps_the_mode_and_clears_repairs(make_hass, net, book,
                                                                                    goodwe_udp_sim, caplog):
    """Plan nadal wymaga ładowania: pierwszy cykl niczego nie zmienia (falownik już zgodny), Naprawy znikają."""
    caplog.set_level(logging.DEBUG)
    env = await _env(make_hass, net, book, goodwe_udp_sim, plan_raw=_charge_plan(3600))
    try:
        bank = goodwe_udp_sim.bank
        await env.step()
        net.move(OLD, NEW)
        await env.run_until(lambda: bool(env.updates))
        assert book.keys() == FORCED_ISSUES
        n = len(bank.writes)
        await env.step()
        assert env.ex.last_decision.reason == "nothing_to_write" and len(bank.writes) == n
        assert gw_raw_word(bank, MODE) == CHARGE_MODE_WORD and book.keys() == set()
        await _assert_private(env, book, caplog)
    finally:
        await env.close()


@pytest.mark.asyncio
async def test_modbus_tcp_inverter_is_found_by_the_sweep_alone(make_hass, net, book, modbus_tcp_sim, caplog):
    caplog.set_level(logging.DEBUG)
    env = await _env(make_hass, net, book, modbus_tcp_sim, "modbus_tcp", plan_raw=_charge_plan(EXPIRES_AFTER_RELOCATION_S))
    try:
        await _move_and_recover(env, net, modbus_tcp_sim, book, caplog)
        assert net.broadcasts == 0 and NEW in net.connects         # bez rozgłoszenia: tylko skan TCP
    finally:
        await env.close()


@pytest.mark.asyncio
async def test_no_broadcast_reply_but_unicast_works(make_hass, net, book, goodwe_udp_sim, caplog):
    caplog.set_level(logging.DEBUG)
    net.broadcast = False                                          # HA w kontenerze bez sieci hosta
    env = await _env(make_hass, net, book, goodwe_udp_sim, plan_raw=_charge_plan(EXPIRES_AFTER_RELOCATION_S))
    try:
        await _move_and_recover(env, net, goodwe_udp_sim, book, caplog)
        assert net.broadcasts >= 1 and NEW in net.unicasts
    finally:
        await env.close()


# ── (4) obcy falownik, (5) brak jednoznacznego odcisku ───────────────────


async def _outage(env, steps: int = 18) -> None:
    for _ in range(steps):
        await env.step()


@pytest.mark.asyncio
async def test_foreign_inverter_is_never_adopted_nor_written(make_hass, net, book, goodwe_udp_sim, spawn, caplog):
    caplog.set_level(logging.DEBUG)
    foreign = await spawn("goodwe_udp", _words_with_serial("ZZ00000000000001"))
    env = await _env(make_hass, net, book, goodwe_udp_sim, plan_raw=_charge_plan(150))
    try:
        net.drop(OLD)                                              # nasz falownik znika
        net.put(NEW, foreign, "goodwe_udp")                        # w sieci jest tylko cudzy, innego egzemplarza
        await _outage(env)                                         # 12 min: kilka wyszukiwań (0, +5, +15 min)
        assert env.rel.state.attempts >= 2 and env.rel.state.last_outcome == "foreign_only"
        assert env.rel.state.last_candidates == 1                  # cudzy falownik został zidentyfikowany i odrzucony
        assert env.updates == [] and env.reloads == 0 and env.host == OLD
        assert _device_writes(foreign) == [] and foreign.bank.writes == []
        assert book.keys() == FORCED_ISSUES                        # Naprawy zostają
        await _assert_private(env, book, caplog)
    finally:
        await env.close()


@pytest.mark.asyncio
async def test_two_devices_with_the_same_serial_are_ambiguous(make_hass, net, book, goodwe_udp_sim, spawn, caplog):
    caplog.set_level(logging.DEBUG)
    a, b = await spawn("goodwe_udp"), await spawn("goodwe_udp")    # ten sam serial co nasz falownik
    env = await _env(make_hass, net, book, goodwe_udp_sim, plan_raw=_charge_plan(150))
    try:
        net.drop(OLD)
        net.put(NEW, a, "goodwe_udp")
        net.put(TWIN, b, "goodwe_udp")
        await _outage(env, 8)
        assert env.rel.state.last_outcome == "ambiguous" and env.updates == []
        assert _device_writes(a) == [] and _device_writes(b) == []
        assert book.keys() == FORCED_ISSUES
        await _assert_private(env, book, caplog)
    finally:
        await env.close()


@pytest.mark.asyncio
async def test_identical_models_without_a_readable_serial_are_never_relocated(make_hass, net, book, goodwe_udp_sim,
                                                                              spawn, caplog):
    """Profil bez rejestru seriala: odcisk `model|moc` ma też bliźniak — żadnej sondy, żadnego przepięcia."""
    caplog.set_level(logging.DEBUG)
    raw = _thaw(GW_V.raw)
    del raw["identify"]["registers"]["serial"]
    weak = profile_from_dict(raw, origin="package")
    fp = device_fingerprint(SALT, weak, RegisterImage(goodwe_words()))
    assert fp is not None
    twin = await spawn("goodwe_udp")
    env = await _env(make_hass, net, book, goodwe_udp_sim, plan_raw=_charge_plan(150), profile=weak,
                     target=gw_target(goodwe_udp_sim, "goodwe_udp", host=OLD, device_fp=fp))
    try:
        net.drop(OLD)
        net.put(NEW, twin, "goodwe_udp")
        await _outage(env, 8)
        assert env.rel.state.last_outcome == "not_found" and env.updates == [] and env.host == OLD
        assert net.broadcasts == 0 and net.unicasts == [] and net.connects == []   # słaby odcisk: bez sondy
        assert _device_writes(twin) == [] and book.keys() == FORCED_ISSUES
        await _assert_private(env, book, caplog)
    finally:
        await env.close()


# ── (6) restart HA w trakcie przerwy ─────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_mid_outage_raises_the_stuck_mode_warning_again(make_hass, net, book, goodwe_udp_sim, caplog):
    caplog.set_level(logging.DEBUG)
    env = await _env(make_hass, net, book, goodwe_udp_sim, plan_raw=_charge_plan(150))
    try:
        bank = goodwe_udp_sim.bank
        await env.step()
        net.drop(OLD)                                              # falownik wyłączony / poza zasięgiem
        await env.run_until(lambda: book.keys() == FORCED_ISSUES)
        assert (await env.store.async_load()).last_mode == "charge_battery"      # utrwalony ostatni tryb
        book.created.clear()
        book.active.clear()                                        # nowy proces: pusta rejestracja Napraw
        await env.restart_process()
        assert env.ex.last_written_mode == "charge_battery"        # po restarcie z magazynu
        await env.run_until(lambda: book.keys() == FORCED_ISSUES)
        assert book.ever("forced_mode_stuck")
        # falownik wraca pod nowym adresem: przepięcie, hamulec, Naprawy znikają
        net.put(NEW, goodwe_udp_sim, "goodwe_udp")
        await env.run_until(lambda: bool(env.updates))
        await env.step()
        assert gw_raw_word(bank, MODE) == NEUTRAL_MODE_WORD and book.keys() == set()
        await _assert_private(env, book, caplog)
    finally:
        await env.close()


# ── (7) falownik wraca pod stary adres ───────────────────────────────────


@pytest.mark.asyncio
async def test_inverter_back_at_the_old_address_changes_no_options(make_hass, net, book, goodwe_udp_sim, caplog):
    caplog.set_level(logging.DEBUG)
    env = await _env(make_hass, net, book, goodwe_udp_sim, plan_raw=_charge_plan(3600))
    try:
        sim = goodwe_udp_sim
        await env.step()
        net.drop(OLD)
        for _ in range(5):                                         # 150 s ciszy: jeszcze przed progiem wyszukiwania
            await env.step()
        assert book.keys() == {"direct_unreachable"}
        # próg 180 s: Naprawy pełne, a w chwili wyszukiwania falownik już odpowiada pod starym adresem
        await env.step(before_check=lambda: net.put(OLD, sim, "goodwe_udp"))
        assert env.rel.state.last_outcome == "same_host"
        assert env.updates == [] and env.reloads == 0 and env.host == OLD and env.rel.state.relocations == 0
        assert book.keys() == FORCED_ISSUES
        await env.step()                                           # łącze odzyskane tym samym połączeniem
        assert env.updates == [] and book.keys() == set()
        assert gw_raw_word(sim.bank, MODE) == CHARGE_MODE_WORD     # plan nadal ładuje; nic nie zdejmowało trybu
        await _assert_private(env, book, caplog)
    finally:
        await env.close()
