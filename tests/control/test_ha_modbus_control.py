"""Sterowanie przez istniejący hub `modbus` HA: transport `ha_modbus` pod połączeniem trybu bezpośredniego
(`kind == "direct"`), na atrapie huba — usługa `modbus.write_register` zapisuje do banku rejestrów, surowe
czujniki `volcast_reg_<adres>` odświeżają się na żądanie (`update_entity`) albo zwykłym odpytaniem (`scan`).

Bez sieci: żadnego gniazda (fabryka transportu sieciowego w tych testach rzuca)."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.volcast.const import DOMAIN
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.device_io import DirectIO
from custom_components.volcast.control.direct import DirectConnection, target_fingerprint
from custom_components.volcast.control.executor import VolcastExecutor
from custom_components.volcast.control.verification import VerificationRunner
from custom_components.volcast.core.control.ladder import RUNNING, STOPPED
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.modbus.blocks import read_plan
from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.transports.ha_modbus import HA_MODBUS_KIND, HaModbusTransport
from tests.control.test_executor_direct import (  # noqa: F401 — `issues` to fixture
    DEYE_V, SALT, TOU_RAW, Harness, _deye, issues)
from tests.control.test_verification import P, Gate
from tests.core.control.tou_helpers import NOW as TOU_NOW
from tests.core.transports.test_ha_modbus import FakeBus, FakeServices
from tests.sim.fixtures import deye_words

HUB, UNIT = "deye_hub", 1
TOU = range(146, 178)
_UTC0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def hub_words() -> dict[int, int]:
    """Słowa falownika: nagranie Deye + słowa bloków odczytu bez pola profilu (np. 589) jako 0."""
    words = deye_words()
    for start, n, *_ in [*read_plan(DEYE_V), *DEYE_V.modbus.identify_reads]:
        for a in range(start, start + n):
            words.setdefault(a, 0)
    return words


def hub_target(**over) -> dict:
    fp = device_fingerprint(SALT, DEYE_V, RegisterImage(hub_words()))
    return {"profile_id": "deye-sg", "transport": HA_MODBUS_KIND, "hub": HUB, "unit_id": UNIT, "device_fp": fp,
            "capabilities": {"tou": True}, **over}


class Registry:
    def __init__(self) -> None:
        self.entities: dict[str, SimpleNamespace] = {}

    def async_get(self, entity_id):
        return self.entities.get(entity_id)


class Component:
    def __init__(self) -> None:
        self.by_id: dict[str, object] = {}

    def get_entity(self, entity_id):
        return self.by_id.get(entity_id)

    @property
    def entities(self):
        return list(self.by_id.values())


class States:
    def __init__(self) -> None:
        self._s: dict[str, SimpleNamespace] = {}

    def get(self, entity_id):
        return self._s.get(entity_id)

    def set(self, entity_id, state, stamp) -> None:
        self._s[entity_id] = SimpleNamespace(entity_id=entity_id, state=str(state), attributes={},
                                             last_updated=stamp, last_reported=stamp)

    def async_entity_ids(self, domain=None):
        return [e for e in self._s if domain is None or e.startswith(domain + ".")]

    def async_all(self, domain=None):
        return [s for e, s in self._s.items() if domain is None or e.startswith(domain + ".")]


class HubSensor:
    """Kształt bazowego czujnika `modbus` HA: hub, adres urządzenia, rejestr, typ wejścia, liczba słów, odpytanie."""

    def __init__(self, addr: int, *, hub: str = HUB, unit: int = UNIT, scan: int = 30) -> None:
        self._hub = SimpleNamespace(name=hub)
        self._device_address = unit
        self._address = addr
        self._input_type = "holding"
        self._count = 1
        self._scan_interval = scan
        self._data_type, self._swap, self._scale, self._offset, self._precision = "uint16", None, 1, 0, 0


class FakeHub:
    """Hub `modbus` w HA użytkownika: bank rejestrów falownika, usługa zapisu, czujniki surowe."""

    def __init__(self, hass, clock, words: dict[int, int]) -> None:
        self.hass, self.clock = hass, clock
        self.bank = dict(words)
        self.writes: list[dict] = []                 # dane wywołań `modbus.write_register` (w kolejności)
        self.on_write = None                         # podgląd stanu wykonawcy w chwili zapisu
        hass.states = States()
        hass.bus = FakeBus()
        hass.late_events = False
        hass.data["entity_registry"] = Registry()
        hass.data["sensor"] = Component()
        hass.services = FakeServices(hass)
        hass.services.handlers[("modbus", "write_register")] = self._write
        hass.services.handlers[("homeassistant", "update_entity")] = self._update
        self.entities = {a: f"sensor.volcast_reg_{a}" for a in self.bank}
        for a, eid in self.entities.items():
            self.add_sensor(a, eid)

    def add_sensor(self, addr: int, eid: str) -> None:
        self.entities[addr] = eid
        self.hass.data["entity_registry"].entities[eid] = SimpleNamespace(
            entity_id=eid, platform="modbus", unique_id=f"volcast_reg_{addr}")
        self.hass.data["sensor"].by_id[eid] = HubSensor(addr)
        self.refresh(addr)

    def sensor(self, addr: int) -> HubSensor:
        return self.hass.data["sensor"].by_id[self.entities[addr]]

    def remove_sensor(self, addr: int) -> None:
        eid = self.entities.pop(addr)
        self.hass.data["entity_registry"].entities.pop(eid, None)
        self.hass.data["sensor"].by_id.pop(eid, None)
        self.hass.states._s.pop(eid, None)

    def utcnow(self) -> datetime:
        return _UTC0 + timedelta(seconds=self.clock.now)

    def refresh(self, addr: int) -> None:
        self.clock.advance(0.01)                     # odczyt na magistrali trwa — stan stemplowany później
        self.hass.states.set(self.entities[addr], self.bank[addr], self.utcnow())

    def scan(self, addrs=None) -> None:
        """Zwykłe odpytanie czujników (`scan_interval`) — wszystkich albo podanych."""
        for a in sorted(self.entities if addrs is None else addrs):
            if a in self.entities:
                self.refresh(a)

    def words(self, addrs) -> list[int]:
        return [self.bank[a] for a in addrs]

    async def _write(self, data: dict) -> None:
        self.writes.append(copy.deepcopy(data))
        if self.on_write is not None:
            self.on_write(data)
        value = data["value"]
        for i, v in enumerate(value if isinstance(value, list) else [value]):
            self.bank[data["address"] + i] = v

    async def _update(self, data: dict) -> None:
        ids = data["entity_id"]
        for a, eid in list(self.entities.items()):
            if eid in ids:
                self.refresh(a)

    async def foreign_write(self, addr: int, value: int) -> None:
        """Drugi sterownik użytkownika: ten sam hub i urządzenie, własne wywołanie usługi."""
        await self.hass.services.async_call("modbus", "write_register",
                                            {"hub": HUB, "slave": UNIT, "address": addr, "value": [value]},
                                            blocking=True)


def _no_socket(*_a, **_k):
    raise AssertionError("ha_modbus must not open a network transport")


class HubHarness(Harness):
    """Wykonawca + połączenie trybu bezpośredniego z transportem `ha_modbus` na atrapie huba."""

    def __init__(self, make_hass, *, target=None, words=None, **kw) -> None:
        self._words = words if words is not None else hub_words()
        super().__init__(make_hass, DEYE_V, target or hub_target(), utc=lambda: TOU_NOW, rated=10000.0, **kw)

    def _compose(self) -> None:
        if not hasattr(self, "hub"):
            self.hub = FakeHub(self.hass, self.clock, self._words)
        t = self.target
        self.conn = DirectConnection(self.hass, self.entry, self.profile, t, trial=self.trial, salt=SALT,
                                     transport_factory=_no_socket, clock=self.clock, unreadable=(),
                                     hub_transport_factory=self._transport)
        self.io = DirectIO(self.conn, self.profile, trial=self.trial, unreadable=(),
                           capabilities=t.get("capabilities"), salt=SALT, clock=self.clock)
        self.ex = VolcastExecutor(self.hass, self.entry, choice=ProfileChoice(self.profile, None, None), mapped={},
                                  rated_power_w=self.rated, store=self.store, clock=self.clock, utcnow=self.utc,
                                  io=self.io)

    scan_on_start = True                             # HA odpytuje czujniki tuż po starcie transportu

    def _transport(self, hass, cfg, *, clock):
        t = HaModbusTransport(hass, cfg, clock=clock, sleep=self.clock.sleep, utcnow=self.hub.utcnow)
        if self.scan_on_start:
            self.hub.scan()
        return t

    async def cycle(self, advance: float = 61.0, *, scan=None) -> None:
        self.clock.advance(advance)
        self.hub.scan(scan)
        await self.conn.async_poll()
        await self.ex.async_tick()

    def service_writes(self) -> list[dict]:
        return list(self.hub.writes)


# ── połączenie: start, mapa, kolizje ──────────────────────────────────────


@pytest.mark.asyncio
async def test_hub_target_starts_without_a_socket_and_reads_the_sensor_image(make_hass, issues):
    h = HubHarness(make_hass)
    try:
        await h.conn.async_start()
        await h.conn.async_poll()
        assert h.conn.refused() is None and h.conn.identity == "confirmed"
        assert isinstance(h.conn.client.transport, HaModbusTransport)
        assert h.conn.map_report is not None and h.conn.map_report.ok
        r = h.conn.reading
        assert r is not None and r.values["soc"] == h.hub.bank[588] and r.programs is not None
        assert h.io.kind == "direct" and h.io.identity_confirmed()
        assert h.service_writes() == []                     # odczyt nie wywołuje usługi zapisu
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_incomplete_register_map_refuses_control_and_retries(make_hass, issues):
    h = HubHarness(make_hass)
    h.hub.remove_sensor(589)                                # słowo bloku SoC bez czujnika: blok nieczytelny
    sleeps = []

    async def no_wait(delay):
        sleeps.append(delay)
    h.conn._sleep = no_wait
    try:
        await h.conn.async_start()
        assert h.conn.refused() == "ha_modbus_map" and h.conn.client is None
        assert 589 in h.conn.map_report.missing and not h.conn.map_report.ok
        assert not h.io.identity_confirmed()
        h.hub.add_sensor(589, "sensor.volcast_reg_589")      # użytkownik dodał czujnik z fragmentu YAML
        await h.conn._retry_task
        assert sleeps and h.conn.refused() is None and h.conn.identity == "confirmed"
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [{"hub": ""}, {"hub": " deye_hub"}, {"hub": None}, {"unit_id": 0},
                                  {"unit_id": 248}, {"unit_id": "1"}])
async def test_bad_hub_target_is_refused_for_good(make_hass, issues, over):
    h = HubHarness(make_hass, target=hub_target(**over))
    try:
        await h.conn.async_start()
        assert h.conn.refused() == "bad_target" and h.conn.client is None
        assert h.conn._retry_task is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_second_connection_to_the_same_hub_device_is_in_use(make_hass, issues):
    h = HubHarness(make_hass)
    other = DirectConnection(h.hass, h.entry, DEYE_V, hub_target(), trial=False, salt=SALT,
                             transport_factory=_no_socket, clock=h.clock, unreadable=(),
                             hub_transport_factory=h._transport)
    try:
        await h.conn.async_start()
        await other.async_start()
        assert h.conn.refused() is None and other.refused() == "direct_in_use"
    finally:
        await other.async_stop()
        await h.close()


def _writer_entity(hub: str, unit: int, addr: int = 166, write_type: str = "write_register"):
    return SimpleNamespace(platform=SimpleNamespace(platform_name="modbus"), _hub=SimpleNamespace(name=hub),
                           _device_address=unit, _address=addr, _write_type=write_type,
                           entity_id="switch.inverter_export")


@pytest.mark.asyncio
@pytest.mark.parametrize("hub, unit, addr, write_type, refused", [
    (HUB, UNIT, 166, "write_register", "direct_conflict:modbus_write_entity"),     # SoC programu 1
    (HUB, 0, 146, "write_register", "direct_conflict:modbus_write_entity"),        # rozgłoszenie
    (HUB, UNIT, 300, "write_register", None),                                       # rejestr spoza naszych
    (HUB, UNIT, 166, "write_coil", None),                                           # cewka
    (HUB, 2, 166, "write_register", None),
    ("heat_pump", UNIT, 166, "write_register", None)])
async def test_modbus_write_entity_conflicts_only_on_our_registers(make_hass, issues, hub, unit, addr, write_type,
                                                                   refused):
    h = HubHarness(make_hass)
    switches = Component()
    switches.by_id["switch.inverter_export"] = _writer_entity(hub, unit, addr, write_type)
    h.hass.data["switch"] = switches
    try:
        await h.conn.async_start()
        assert h.conn.refused() == refused
        raised = [(i, p) for i, k, p in issues.created if k == "ha_modbus_write_entity"]
        if refused:
            text = raised[0][1]["entities"] if raised else ""
            assert "switch.inverter_export" in text and str(addr) in text
        else:
            assert raised == []
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", [
    SimpleNamespace(platform=SimpleNamespace(platform_name="modbus"), entity_id="switch.x"),        # nic nie wiemy
    SimpleNamespace(platform=SimpleNamespace(platform_name="modbus"), _hub=SimpleNamespace(name=HUB),
                    _device_address=UNIT, entity_id="switch.x")])                                   # rejestr nieznany
async def test_modbus_write_entity_with_an_unreadable_target_refuses_control(make_hass, issues, entity):
    h = HubHarness(make_hass)
    switches = Component()
    switches.by_id["switch.x"] = entity
    h.hass.data["switch"] = switches
    try:
        await h.conn.async_start()
        assert h.conn.refused() == "direct_conflict:modbus_write_entity"
        raised = [p for _i, k, p in issues.created if k == "ha_modbus_write_entity"]
        assert raised and "switch.x: ?" in raised[0]["entities"]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_other_volcast_entry_on_the_same_hub_device_refuses_control(make_hass, issues):
    h = HubHarness(make_hass)
    h.hass.config_entries._entries.append(SimpleNamespace(
        domain=DOMAIN, entry_id="e2", data={}, disabled_by=None,
        options={"control_mode": "direct", "direct_target": hub_target()}))
    try:
        await h.conn.async_start()
        assert h.conn.refused() == "direct_in_use"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_one_foreign_service_write_on_our_hub_is_a_conflict_but_our_own_is_not(make_hass, issues):
    h = HubHarness(make_hass)
    try:
        await h.conn.async_start()
        t = h.conn.client.transport
        with pytest.raises(Exception):
            await t.write(166, [50], function=16)            # własny zapis (WriteUnconfirmed)
        assert h.conn.conflict is False
        await h.hub.foreign_write(167, 40)
        assert h.conn.conflict is True and h.conn.monitor.state == "ok"   # bez 3 obcych ramek monitora
    finally:
        await h.close()


def test_target_fingerprint_binds_the_hub_and_device_and_keeps_host_targets():
    t = hub_target()
    assert target_fingerprint(t, SALT) != target_fingerprint({**t, "hub": "other"}, SALT)
    assert target_fingerprint(t, SALT) != target_fingerprint({**t, "unit_id": 2}, SALT)
    assert HUB not in target_fingerprint(t, SALT)
    # cel sieciowy: odcisk bez zmian (rekordy własności zapisane przez poprzednie wersje)
    import hashlib
    import hmac
    host = {"transport": "modbus_rtu", "host": "192.168.1.9", "port": 8899, "unit_id": 1}
    want = hmac.new(SALT, b"modbus_rtu|192.168.1.9|8899|1", hashlib.sha256).hexdigest()[:16]
    assert target_fingerprint(host, SALT) == want


def test_hub_or_device_change_is_always_a_control_change():
    """Tożsamość z czujników wiąże się z hubem i urządzeniem celu — zmiana huba albo adresu to inne sterowanie,
    także dla falownika z odciskiem z seriala (bez przenoszenia własności po samym serialu)."""
    old = {"control_mode": "direct", "direct_target": hub_target()}
    for over in ({"hub": "other_hub"}, {"unit_id": 2}):
        new = {"control_mode": "direct", "direct_target": hub_target(**over)}
        assert rt_mod.control_options_changed(old, new) is True
        assert rt_mod.control_options_changed(old, new, profile=DEYE_V) is True


@pytest.mark.asyncio
async def test_owner_record_of_another_hub_does_not_match(make_hass, issues):
    h = HubHarness(make_hass)
    other = HubHarness(make_hass, target=hub_target(hub="renamed_hub"))
    record = h.io.owner()
    assert "device_target" not in record                     # bez odcisku niezależnego od huba
    assert h.io.owner_matches(record) and not other.io.owner_matches(record)


@pytest.mark.asyncio
async def test_relocator_never_runs_for_the_hub_transport(make_hass, issues):
    h = HubHarness(make_hass)
    rt = SimpleNamespace(direct=h.conn, unsubs=[], relocator=None)
    assert rt_mod.start_relocator(h.hass, h.entry, rt) is None and rt.unsubs == []


# ── wykonawca: pełny cykl okien czasowych ─────────────────────────────────


@pytest.mark.asyncio
async def test_full_tou_cycle_through_the_hub_service(make_hass, issues):
    """OFF → 6 programów → ON, wyłącznie wywołaniami `modbus.write_register` na hubie użytkownika."""
    h = await HubHarness(make_hass).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert h.ex.last_decision.status == "write" and h.ex.owned
        calls = h.service_writes()
        assert calls and all(c["hub"] == HUB and c["slave"] == UNIT and isinstance(c["value"], list)
                             for c in calls)
        first, last = calls[0], calls[-1]
        assert first["address"] == 146 and first["value"][0] & 1 == 0          # harmonogram wyłączony
        assert last["address"] == 146 and last["value"][0] & 1 == 1            # i włączony na końcu
        written = {c["address"] + i for c in calls[1:-1] for i in range(len(c["value"]))}
        assert 146 not in written
        # każdy z 6 programów dostał zapis (pola równe planowi pisarz pomija)
        fields = {"start": 148, "power_w": 154, "soc": 166, "grid_charge": 172}
        assert all(any(base + i in written for base in fields.values()) for i in range(6))
        n = len(calls)
        await h.cycle()
        assert len(h.service_writes()) == n and h.ex.last_decision.reason == "nothing_to_write"
        # obraz z czujników po zapisie = to, co wykonawca zapisał (6 programów i włącznik)
        device = h.conn.reading.device
        mine = {k: v for k, v in h.ex._memory.last_written.items() if k.startswith("tou")}
        assert mine and all(device[k] == v for k, v in mine.items()) and device["tou_enabled"] == 1.0
        assert {f"tou.{i}.{f}" for i in range(1, 7) for f in fields} <= set(device)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_hub_writes_the_same_tou_image_as_the_direct_transport(make_hass, rtu_tcp_sim, deye_bank, issues):
    h = await HubHarness(make_hass).start(raw=TOU_RAW)
    d = await _deye(make_hass, rtu_tcp_sim).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        await d.ex.async_tick()
        assert h.hub.words(TOU) == deye_bank.read(146, 32)
    finally:
        await d.close()
        await h.close()


@pytest.mark.asyncio
async def test_snapshot_is_taken_before_the_first_write(make_hass, issues):
    h = HubHarness(make_hass)
    owner = h.hub.words(TOU)
    seen = []
    h.hub.on_write = lambda data: seen.append((copy.deepcopy(h.ex._state.tou_snapshot), h.ex._state.owned,
                                               copy.deepcopy(h.store._store._data)))
    await h.start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert seen, "no write"
        snap, owned, stored = seen[0]
        assert owned and snap is not None and snap["tou_word"] == owner[0]
        assert [p[3] for p in snap["programs"]] == owner[172 - 146:178 - 146]   # surowe słowa ładowania
        assert [p[2] for p in snap["programs"]] == owner[166 - 146:172 - 146]
        assert "tou_snapshot" in repr(stored) and str(owner[0]) in repr(stored)  # zapisana przed zapisem
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_disabling_control_restores_the_snapshot(make_hass, issues):
    h = HubHarness(make_hass)
    before = h.hub.words(TOU)
    await h.start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert h.hub.words(TOU) != before and h.ex.owned
        await h.ex.async_set_consent(False)
        await h.cycle()
        assert h.hub.words(TOU) == before                     # programy i słowo włącznika właściciela
        assert not h.ex.owned and h.ex._state.tou_snapshot is None
        assert h.ex.last_decision.status == "restore"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_without_consent_there_are_no_service_calls(make_hass, issues):
    h = await HubHarness(make_hass).start(raw=TOU_RAW, consent=False)
    try:
        await h.ex.async_tick()
        for _ in range(3):
            await h.cycle()
        assert h.service_writes() == [] and not h.ex.owned
        assert [c for c in h.hass.services.calls if c[:2] == ("modbus", "write_register")] == []
    finally:
        await h.close()


# ── próba (drabina weryfikacji) ───────────────────────────────────────────


async def _trial(monkeypatch, make_hass):
    from custom_components.volcast.control import verification as ver_mod
    created = []
    monkeypatch.setattr(ver_mod.ir, "async_create_issue", lambda hass, domain, issue_id, **kw: created.append(kw))
    monkeypatch.setattr(ver_mod.ir, "async_delete_issue", lambda hass, domain, issue_id: None)
    monkeypatch.setattr(ver_mod, "async_dispatcher_send", lambda hass, sig: None)
    owner = hub_words()
    h = await HubHarness(make_hass).start(raw=TOU_RAW)
    h.ex.verification = Gate(True)
    await h.ex.async_tick()
    assert h.ex.owned and h.hub.words(TOU) != [owner[a] for a in TOU]
    await h.cycle()
    runner = VerificationRunner(SimpleNamespace(data={}), SimpleNamespace(entry_id="e1", options={}), h.ex,
                                params=P, start_rung=1, salt=SALT, utcnow=lambda: TOU_NOW,
                                track_point=lambda hass, action, when: (lambda: None))
    await runner.async_start()
    assert (runner.ladder.state.rung, runner.ladder.state.state) == (3, RUNNING)
    return h, runner, owner, created


@pytest.mark.asyncio
async def test_trial_own_restore_with_staggered_sensor_refresh_is_not_a_foreign_write(monkeypatch, make_hass,
                                                                                       issues):
    """Nasz zapis wielu rejestrów (powrót do programów właściciela) i czujniki odświeżane po kolei: brak fałszywego
    `foreign_write`. Regułę najstarszego stempla przypina `tests/core/modbus/test_client_image_age.py` — tutaj
    zapisane rejestry chroni już świeżość zapisów w toku transportu."""
    h, runner, owner, created = await _trial(monkeypatch, make_hass)
    lad = runner.ladder
    try:
        h.clock.advance(61.0)
        await h.ex.async_tick()                                 # powrót: bramka planu zamknięta w próbie
        assert h.hub.words(TOU) == [owner[a] for a in TOU] and not h.ex.owned
        await runner.async_step()                               # odczyt sprzed powrotu
        assert (lad.state.rung, lad.state.state) == (3, RUNNING)
        first_half = [a for a in sorted(h.hub.entities) if a < 160]
        await h.cycle(5.0, scan=first_half)                     # połowa czujników po zapisie
        await runner.async_step()
        assert (lad.state.rung, lad.state.state) == (3, RUNNING)
        await h.cycle(5.0, scan=[a for a in sorted(h.hub.entities) if a >= 160])
        await runner.async_step()
        await h.cycle()
        await runner.async_step()
        assert (lad.state.rung, lad.state.state) == (3, RUNNING) and created == []
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_trial_foreign_service_write_on_our_hub_stops_it(monkeypatch, make_hass, issues):
    h, runner, _owner, _created = await _trial(monkeypatch, make_hass)
    lad = runner.ladder
    try:
        await runner.async_step()
        assert (lad.state.rung, lad.state.state) == (3, RUNNING)
        await h.hub.foreign_write(167, h.hub.bank[167] + 7)    # drugi sterownik: SoC programu 2
        await runner.async_step()
        assert (lad.state.rung, lad.state.state) == (3, STOPPED) and lad.state.stop_reason == "foreign_write"
    finally:
        await h.close()


# ── telemetria i diagnostyka: raport mapy bez entity_id ───────────────────


@pytest.mark.asyncio
async def test_map_report_reaches_telemetry_and_diagnostics_without_entity_ids(make_hass, issues):
    import json

    from custom_components.volcast.control.telemetry import TelemetrySender
    from tests.control.test_telemetry_direct import Cloud, Executor, _hass
    h = HubHarness(make_hass)
    h.hub.remove_sensor(589)
    h.hass.data["sensor"].by_id["sensor.volcast_reg_588"] = SimpleNamespace(coordinator=object())  # slave_count
    try:
        await h.conn.async_start()
        report = h.conn.map_report
        assert report is not None and not report.ok
        sender = TelemetrySender(_hass(), SimpleNamespace(entry_id="e1", options={"control_mode": "direct"}),
                                 Cloud(), Executor(), choice=ProfileChoice(DEYE_V, None, None), profile_map={},
                                 manual_map={}, grid_negate=False, limits=None, utcnow=lambda: TOU_NOW,
                                 direct=h.conn, direct_capabilities=None)
        block = sender._extra()["direct"]["map"]
        assert 589 in block["missing"] and {"addr": 588, "cause": "slave_or_virtual_sensor"} in block["unsupported"]
        assert "sensor." not in json.dumps(block) and HUB not in json.dumps(block)
    finally:
        await h.close()


# ── tożsamość wiąże się z celem zapisu (hub i urządzenie czujników) ───────


@pytest.mark.asyncio
@pytest.mark.parametrize("target_over, sensor_over, cause", [
    ({"hub": "other_hub", "unit_id": 2}, None, "sensor_other_device"),   # cel inny niż czujniki
    ({"unit_id": 2}, None, "sensor_other_device"),                        # np. drugi falownik równoległy
    ({}, (0, {"_device_address": 2}), "sensor_other_device"),             # identyfikacja z innego urządzenia
    ({}, (588, {"_hub": SimpleNamespace(name="meter_hub")}), "sensor_other_device"),
    ({}, (166, {"_address": 167}), "sensor_other_register"),
    ({}, (146, {"_hub": None}), "sensor_device_unknown"),
    ({}, (166, {"_scale": 10, "_swap": "byte"}), "sensor_not_raw"),     # próba recenzenta: obraz TOU przekłamany
    ({}, (588, {"_offset": 1}), "sensor_not_raw"),
])
async def test_sensors_not_proven_on_the_write_target_refuse_control(make_hass, issues, target_over, sensor_over,
                                                                     cause):
    h = HubHarness(make_hass, target=hub_target(**target_over))
    if sensor_over is not None:
        addr, attrs = sensor_over
        for k, v in attrs.items():
            setattr(h.hub.sensor(addr), k, v)
    try:
        await h.conn.async_start()
        assert h.conn.refused() == "ha_modbus_map" and h.conn.client is None
        assert cause in {u.cause for u in h.conn.map_report.unsupported}
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_target_on_another_hub_and_device_never_writes(make_hass, issues):
    """Próba recenzenta: czujniki (z tożsamością) na `deye_hub`/1, cel zapisu `other_hub`/2 — przedtem tożsamość
    „potwierdzona” i zapisy TOU szły do innego urządzenia."""
    h = await HubHarness(make_hass, target=hub_target(hub="other_hub", unit_id=2)).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        await h.cycle()
        assert h.conn.identity != "confirmed" and not h.ex.owned
        assert h.service_writes() == []
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("addr, attrs", [(3, {"_device_address": 2}), (166, {"_device_address": 2}),
                                         (588, {"_scale": 10})])
async def test_sensor_changed_mid_run_stops_identity_at_the_next_poll(make_hass, issues, addr, attrs):
    """Bez wymuszania terminu tożsamości: czujnik zmieniony pod działającym połączeniem (YAML + przeładowanie
    `modbus`) zatrzymuje odczyty i zapisy już przy następnym odpytaniu, a nowa własność (migawka TOU) nie
    powstaje z obrazu innego urządzenia."""
    h = await HubHarness(make_hass).start(raw=TOU_RAW, consent=False)
    try:
        await h.ex.async_tick()
        assert h.conn.identity == "confirmed" and not h.ex.owned
        for k, v in attrs.items():
            setattr(h.hub.sensor(addr), k, v)
        assert h.conn.identity_check_due()                     # przed zapisem: mapa sprawdzona na nowo
        await h.cycle(10.0)
        assert h.conn.identity != "confirmed" and h.conn.reading is None
        await h.ex.async_set_consent(True)
        await h.cycle(10.0)
        await h.ex.async_tick()
        assert not await h.io.async_identity_ok()
        assert h.service_writes() == [] and not h.ex.owned and h.ex._state.tou_snapshot is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_sensor_moved_while_owning_stops_writes_at_once(make_hass, issues):
    h = await HubHarness(make_hass).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        assert h.ex.owned
        snap = copy.deepcopy(h.ex._state.tou_snapshot)
        n = len(h.service_writes())
        h.hub.sensor(166)._device_address = 2
        h.hub.bank[167] = 99                                  # obraz „innego urządzenia” wygląda na obcy zapis
        await h.cycle(400.0)
        await h.cycle(400.0)
        assert len(h.service_writes()) == n and h.ex._state.tou_snapshot == snap
        assert h.conn.identity != "confirmed"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_sensor_moved_to_another_device_while_running_stops_identity(make_hass, issues):
    h = await HubHarness(make_hass).start(raw=TOU_RAW, consent=False)
    try:
        assert h.conn.identity == "confirmed"
        h.hub.sensor(3)._device_address = 2                   # YAML zmieniony pod działającym połączeniem
        h.conn._identity_at = -1e9                            # potwierdzenie przeterminowane
        assert not await h.io.async_identity_ok()
        await h.cycle()
        assert h.conn.identity != "confirmed" and h.conn.reading is None
        assert "sensor_other_device" in {u.cause for u in h.conn.map_report.unsupported}
        h.hub.sensor(3)._device_address = UNIT
        await h.cycle()
        assert h.conn.identity == "confirmed" and h.conn.reading is not None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_map_refusal_raises_a_repair_with_addresses_and_causes_only(make_hass, issues):
    h = HubHarness(make_hass)
    h.hub.remove_sensor(589)
    h.hub.sensor(0)._device_address = 2

    async def no_wait(_delay):
        return None
    h.conn._sleep = no_wait
    try:
        await h.conn.async_start()
        raised = [(i, p) for i, k, p in issues.created if k == "ha_modbus_map"]
        assert raised and raised[0][0] == "ha_modbus_map_e1"
        text = raised[0][1]["registers"]
        assert "589" in text and "0" in text and "sensor_other_device" in text and "sensor." not in text
        h.hub.add_sensor(589, "sensor.volcast_reg_589")
        h.hub.sensor(0)._device_address = UNIT
        await h.conn._retry_task
        assert h.conn.refused() is None and "ha_modbus_map_e1" in issues.deleted
    finally:
        await h.close()


# ── dostępność odczytu: wiek obrazu a odpytywanie czujników ───────────────


@pytest.mark.asyncio
async def test_sensor_scan_slower_than_the_poll_does_not_flap_availability(make_hass, issues):
    """Czujniki co 30 s, połączenie co 10 s: wiek obrazu (najstarszy stempel) przekracza 3 × poll, ale encje
    i telemetria zostają dostępne — próg wynika z odpytywania czujników; bramki zapisu dalej widzą prawdziwy wiek."""
    from custom_components.volcast.control.direct_sensors import stale_limit_s
    from custom_components.volcast.control.telemetry import TelemetrySender
    from tests.control.test_telemetry_direct import Cloud, Executor, _hass
    h = HubHarness(make_hass)
    try:
        await h.conn.async_start()
        sender = TelemetrySender(_hass(), SimpleNamespace(entry_id="e1", options={"control_mode": "direct"}),
                                 Cloud(), Executor(), choice=ProfileChoice(DEYE_V, None, None), profile_map={},
                                 manual_map={}, grid_negate=False, limits=None, utcnow=lambda: TOU_NOW,
                                 direct=h.conn, direct_capabilities=None)
        ages = []
        for step in range(12):                                # 120 s: 4 odpytania czujników, 12 połączenia
            h.clock.advance(10.0)
            if step % 3 == 0:
                h.hub.scan()
            await h.conn.async_poll()
            h.clock.advance(9.0)                              # najgorsza faza: tuż przed następnym odpytaniem
            ages.append(h.conn.age_s())
            assert h.conn.age_s() < stale_limit_s(h.conn)
            assert sender._direct_status() == "ok"
        assert max(ages) > 3 * h.conn.poll_s                  # stary próg by migał
        assert stale_limit_s(h.conn) >= 3 * h.conn.poll_s + 2 * 30.0
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_sensors_that_stop_updating_still_go_stale(make_hass, issues):
    from custom_components.volcast.control.direct_sensors import stale_limit_s
    h = HubHarness(make_hass)
    try:
        await h.conn.async_start()
        await h.conn.async_poll()
        h.clock.advance(stale_limit_s(h.conn) + 1.0)          # hub stoi: stany bez nowych stempli
        await h.conn.async_poll()
        assert not h.conn.age_s() < stale_limit_s(h.conn)
    finally:
        await h.close()


def test_network_connection_keeps_the_poll_based_limit():
    from custom_components.volcast.control.direct_sensors import STALE_FACTOR, stale_limit_s
    conn = SimpleNamespace(poll_s=10.0)
    assert stale_limit_s(conn) == STALE_FACTOR * 10.0


# ── kolizja w pracy: przyczyna, budżet NVM ────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_service_write_has_its_own_conflict_reason(make_hass, issues):
    h = await HubHarness(make_hass).start(raw=TOU_RAW)
    try:
        assert h.conn.conflict_reason is None
        await h.hub.foreign_write(167, 40)
        assert h.conn.conflict_reason == "modbus_service_write"
        assert rt_mod.lan_client_seen(h.conn) == "modbus_service_write"
        await h.cycle()
        raised = [p for _i, k, p in issues.created if k == "direct_conflict"]
        assert raised and raised[-1]["reason"] == "modbus_service_write"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_every_hub_service_write_counts_against_the_nvm_budget(make_hass, issues):
    h = await HubHarness(make_hass).start(raw=TOU_RAW)
    try:
        await h.ex.async_tick()
        n = len(h.service_writes())
        counts = h.ex._memory.budget.counts(h.ex._now_wall())
        assert n > 0 and sum(counts.values()) == n
    finally:
        await h.close()


# ── restart HA: stany odtworzone (sprzed restartu) ze świeżym stemplem ────


@pytest.mark.asyncio
async def test_restored_states_after_an_ha_restart_give_no_takeover_until_a_real_poll(make_hass, issues):
    """Po restarcie HA czujniki pokazują słowa sprzed restartu ze świeżym stemplem (odtworzenie), a falownik
    ma już inne programy TOU. Do pierwszego prawdziwego odpytania: bez tożsamości, odczytu, migawki i zapisu;
    potem migawka z PRAWDZIWYCH słów."""
    h = HubHarness(make_hass)
    h.scan_on_start = False                          # hub jeszcze się nie połączył — brak odpytania
    restored = {a: h.hub.bank[a] for a in TOU}
    for a in (166, 167, 172):                        # w czasie przerwy właściciel zmienił programy
        h.hub.bank[a] = (h.hub.bank[a] + 5) & 0xFFFF
    live = h.hub.words(TOU)
    for eid in list(h.hass.data["sensor"].by_id):    # start HA: encje z rejestru jako `unavailable` (zaślepka)
        h.hass.data["sensor"].by_id[eid] = HubSensor(int(eid.rsplit("_", 1)[1]))
        h.hass.states.set(eid, "unavailable", h.hub.utcnow())
    await h.start(raw=TOU_RAW)
    try:
        h.clock.advance(1.0)                         # dodanie encji: odtworzone słowa, świeży stempel
        for a, eid in h.hub.entities.items():
            h.hass.states.set(eid, restored.get(a, h.hub.bank[a]), h.hub.utcnow())
        await h.conn.async_poll()
        await h.ex.async_tick()
        assert h.conn.identity != "confirmed" and h.conn.reading is None
        assert not h.ex.owned and h.ex._state.tou_snapshot is None and h.service_writes() == []
        await h.cycle()                              # pierwsze prawdziwe odpytanie huba
        await h.ex.async_tick()
        assert h.conn.identity == "confirmed" and h.ex.owned
        snap = h.ex._state.tou_snapshot
        assert [p[2] for p in snap["programs"]] == live[166 - 146:172 - 146]
        assert [p[3] for p in snap["programs"]] == live[172 - 146:178 - 146]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_service_write_evidence_survives_a_static_conflict(make_hass, issues):
    h = await HubHarness(make_hass).start(raw=TOU_RAW)
    try:
        await h.hub.foreign_write(167, 40)
        h.conn.static_conflicts = ("solarman",)              # kolizja statyczna wykryta przy ponownym sprawdzeniu
        assert h.conn.conflict_reason == "solarman"
        assert rt_mod.lan_client_seen(h.conn) == "modbus_service_write"   # dowód dla serwera nie ginie
    finally:
        await h.close()
