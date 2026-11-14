"""Własność w trybie bezpośrednim przeżywa zmianę adresu falownika (ten sam egzemplarz pod nowym IP).

`owner.target` zostaje odciskiem celu z adresem (`target_fingerprint`) dla BIEŻĄCEGO adresu — poprzednia
wersja wtyczki porównuje rekord ściśle i po powrocie do niej (rollback) dalej go uznaje. Dla odcisku z serialem
obok leży odcisk bez adresu (`device_target`: `transport|port|unit_id|device_fp`, w magazynie poza rekordem jako
`owner_device_target`); po zmianie adresu rekord pasuje przez niego, a wykonawca przy starcie przepisuje
`target` na nowy adres. Rekord bez tego pola (starsza wersja) — dopasowanie z adresem jak dotąd, pole dopisywane
przy starcie pod tym samym adresem. Bez odcisku, z odciskiem słabym (`model|moc`) albo niepoprawnym — dawne
zachowanie. Zmiana samego hosta (i `logger_serial`) w opcjach nie jest zmianą sterowania: brak powrotu przez
starego wykonawcę po martwym łączu, rekord zostaje, a hamulec zdejmuje tryb wymuszony w pierwszym cyklu.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.control.device_io import DirectIO
from custom_components.volcast.control.direct import device_target_fingerprint, target_fingerprint
from custom_components.volcast.control.store import ControlState, ControlStore
from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.registers import RegisterImage
from tests.control.test_direct_safeguards import _stale
from tests.control.test_executor_direct import (GW_V, MODE, SALT, Harness, gw_raw_word, gw_target, issues,
                                                verified)  # noqa: F401 — `issues` to fikstura
from tests.sim.fixtures import goodwe_words

FOX_V = verified("foxess-h")
GW_FP = device_fingerprint(SALT, GW_V, RegisterImage(goodwe_words()))
OLD_HOST, NEW_HOST = "192.0.2.10", "192.0.2.20"


def _gw(**over) -> dict:
    return {"profile_id": "goodwe-et", "transport": "goodwe_udp", "host": OLD_HOST, "port": 8899, "unit_id": 247,
            "device_fp": GW_FP, "unreadable": ["soc_max"], "capabilities": {"mode": True}, **over}


def _io(target, profile=GW_V) -> DirectIO:
    return DirectIO(SimpleNamespace(target=target), profile, trial=False, salt=SALT)


def _legacy(target, profile=GW_V) -> dict:
    """Rekord w kształcie poprzedniej wersji (i jej `owner()`): odcisk celu z adresem, bez pola obok."""
    fp = target.get("device_fp")
    return {"profile": profile.id, "mode": "direct", "target": target_fingerprint(target, SALT),
            "device": fp if isinstance(fp, str) else ""}


async def _stored_raw(make_hass, owner: dict) -> tuple[dict, ControlStore]:
    """Zapis przez magazyn nowej wersji → surowe dane (tak czyta je poprzednia wersja)."""
    store = ControlStore(make_hass(), "e1")
    await store.async_save(ControlState(owned=True, snapshot={"mode": "auto"}, owner=owner))
    return await store._store.async_load(), store


# ── odcisk własności ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_record_is_still_recognised_by_the_previous_version(make_hass):
    t = _gw()
    owner = _io(t).owner()
    assert owner == {**_legacy(t), "device_target": device_target_fingerprint(t, GW_FP, SALT)}
    raw, store = await _stored_raw(make_hass, owner)
    # Poprzednia wersja: `dict(raw["owner"]) == owner()` — rekord w magazynie jest dokładnie jej kształtem.
    assert raw["owner"] == _legacy(t) and raw["owner_device_target"] == owner["device_target"]
    assert (await store.async_load()).owner == owner                     # nowa wersja odczytuje pole obok
    assert OLD_HOST not in str(raw)


@pytest.mark.asyncio
async def test_record_without_owner_keeps_no_side_field(make_hass):
    raw, store = await _stored_raw(make_hass, {})
    assert "owner_device_target" not in raw and (await store.async_load()).owner == {}


def test_host_change_matches_through_the_host_less_print():
    old, new = _gw(), _gw(host=NEW_HOST)
    record = _io(old).owner()
    assert _io(new).owner_matches(record)
    assert _io(new).owner()["target"] != record["target"]               # przepisanie na nowy adres przy starcie
    assert _io(new).owner()["device_target"] == record["device_target"]
    v5 = _io(_gw(transport="solarman_v5", logger_serial=1)).owner()
    assert _io(_gw(transport="solarman_v5", host=NEW_HOST, logger_serial=2)).owner_matches(v5)


@pytest.mark.parametrize("over", [{"device_fp": "f" * 16}, {"port": 502}, {"unit_id": 1},
                                  {"transport": "modbus_tcp"}])
def test_owner_never_matches_another_device_or_link(over):
    record = _io(_gw()).owner()
    for io in (_io(_gw(host=NEW_HOST, **over)), _io(_gw(**over))):
        assert not io.owner_matches(record) and not io.owner_matches(_legacy(_gw()))


def test_host_less_print_needs_the_same_profile_and_device():
    record = _io(_gw()).owner()
    new = _io(_gw(host=NEW_HOST))
    for key, value in (("device", "f" * 16), ("profile", "deye-sg"), ("mode", "entities")):
        assert not new.owner_matches({**record, key: value})
    assert not new.owner_matches({**record, "device_target": "0" * 16})
    deye = _io(_gw(profile_id="deye-sg", host=NEW_HOST), verified("deye-sg"))
    assert not deye.owner_matches(record)


def test_record_without_the_field_matches_the_same_address_only():
    legacy = _legacy(_gw())
    assert _io(_gw()).owner_matches(legacy)                             # pole dopisywane przy starcie
    # Bez pola obok rekordu z adresem nie da się sprawdzić pod innym adresem — jak dotąd (fail-closed).
    assert not _io(_gw(host=NEW_HOST)).owner_matches(legacy)
    assert not _io(_gw(device_fp="f" * 16)).owner_matches(legacy)


@pytest.mark.parametrize("fp", [None, "", "ABCDEF0123456789", "abc", 12])
def test_owner_without_valid_fingerprint_is_host_bound_as_before(fp):
    t = _gw(device_fp=fp)
    assert _io(t).owner() == _legacy(t)
    moved = _io(_gw(device_fp=fp, host=NEW_HOST))
    assert not moved.owner_matches(_io(t).owner())
    # pole obok nie otwiera dopasowania bez adresu, gdy cel nie ma odcisku z seriala
    assert not moved.owner_matches({**_io(t).owner(), "device_target": "0" * 16})


def test_weak_fingerprint_is_host_bound_as_before():
    # Profil bez rejestru seriala: odcisk `model|moc` — dwa identyczne modele mają ten sam.
    fox = {"profile_id": "foxess-h", "transport": "modbus_tcp", "host": OLD_HOST, "port": 502, "unit_id": 247,
           "device_fp": "0123456789abcdef"}
    assert _io(fox, FOX_V).owner() == _legacy(fox, FOX_V)
    moved = _io({**fox, "host": NEW_HOST}, FOX_V)
    assert not moved.owner_matches(_io(fox, FOX_V).owner())
    assert not moved.owner_matches({**_io(fox, FOX_V).owner(),
                                    "device_target": device_target_fingerprint(fox, fox["device_fp"], SALT)})
    # Jawne oznaczenie słabego odcisku w celu wygrywa z profilem.
    weak = _gw(fp_kind="model")
    assert _io(weak).owner() == _legacy(weak)
    assert not _io(_gw(fp_kind="model", host=NEW_HOST)).owner_matches(_io(_gw()).owner())
    assert _io(_gw(fp_kind="serial")).owner() == _io(_gw()).owner()


# ── wykonawca: migracja rekordu i zmiana hosta ────────────────────────────


async def _seed(make_hass, owner: dict) -> ControlStore:
    store = ControlStore(make_hass(), "e1")
    await store.async_save(ControlState(
        consent=True, local_switch=True, owned=True, snapshot={"mode": "auto", "soc_min": 5.0},
        owner=owner, restore_keys=["mode", "power_w"]))
    return store


@pytest.mark.asyncio
async def test_record_without_the_field_gains_it_at_start(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    target = gw_target(goodwe_udp_sim)
    legacy = _legacy(target)
    store = await _seed(make_hass, legacy)
    h = await Harness(make_hass, GW_V, target, store=store).start()
    try:
        assert h.ex.owned and h.ex._state.snapshot == {"mode": "auto", "soc_min": 5.0}
        assert h.ex._state.owner == {**legacy, "device_target": device_target_fingerprint(target, GW_FP, SALT)}
        assert (await store.async_load()).owner == h.ex._state.owner
        assert (await store._store.async_load())["owner"] == legacy          # poprzednia wersja nadal go uznaje
        assert "control_record_dropped" not in issues.keys()
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_host_change_keeps_ownership_and_brake_neutralises(make_hass, goodwe_udp_sim, goodwe_bank, issues):
    target = gw_target(goodwe_udp_sim)                       # nowy adres falownika
    goodwe_bank.poke(MODE, 11)                                # charge_battery zapisany pod starym adresem
    store = await _seed(make_hass, _io({**target, "host": OLD_HOST}).owner())
    h = await Harness(make_hass, GW_V, target, store=store).start()
    try:
        assert h.ex.owned and h.ex._state.snapshot == {"mode": "auto", "soc_min": 5.0}
        assert "control_record_dropped" not in issues.keys()
        # rekord przepisany na NOWY adres (poprzednia wersja uzna go po rollbacku), pole obok bez zmian
        assert h.ex._state.owner == h.io.owner()
        assert h.ex._state.owner["target"] == target_fingerprint(target, SALT)
        assert (await store._store.async_load())["owner"] == _legacy(target)
        _stale(h)
        await h.ex.async_tick()
        assert gw_raw_word(goodwe_bank, MODE) == 1 and "neutral_brake" in h.ex.last_decision.notes
    finally:
        await h.close()


@pytest.mark.parametrize("record", [
    lambda t: _io({**t, "host": OLD_HOST, "device_fp": "f" * 16}).owner(),   # inny egzemplarz
    lambda t: _legacy({**t, "host": OLD_HOST}),                              # rekord bez pola, inny adres
])
@pytest.mark.asyncio
async def test_foreign_or_unverifiable_record_is_still_dropped(make_hass, goodwe_udp_sim, goodwe_bank, issues,
                                                               record):
    target = gw_target(goodwe_udp_sim)
    goodwe_bank.poke(MODE, 11)
    store = await _seed(make_hass, record(target))
    h = Harness(make_hass, GW_V, target, store=store)
    try:
        await h.ex.async_start()
        assert not h.ex.owned and h.ex._state.snapshot == {} and h.ex._state.owner == {}
        assert "control_record_dropped" in issues.keys()
    finally:
        await h.close()


# ── zmiana opcji: tożsamość sterowania bez hosta ──────────────────────────


class _OwnedDirect:
    def __init__(self, order, profile=GW_V, *, trial=False, fail=False):
        self.order, self.owned, self.profile, self.fail = order, True, profile, fail
        self.io = SimpleNamespace(kind="direct", trial=trial)

    async def async_restore_now(self):
        self.order.append("restore")
        if self.fail:
            raise OSError("dead link")
        self.owned = False


def _opts(target) -> dict:
    return {"control_mode": "direct", "direct_target": target}


def test_host_only_change_is_not_a_control_change_for_the_running_profile():
    old = _opts(_gw(logger_serial=1))
    assert not rt_mod.control_options_changed(old, _opts(_gw(host=NEW_HOST, logger_serial=2)), profile=GW_V)
    assert not rt_mod.control_options_changed(old, _opts(_gw(host=NEW_HOST, logger_serial=1, fp_kind="serial")),
                                              profile=GW_V)
    # bez profilu (czysta funkcja jak dotąd) — każda zmiana hosta to zmiana
    assert rt_mod.control_options_changed(old, _opts(_gw(host=NEW_HOST, logger_serial=1)))


@pytest.mark.parametrize("over", [{"device_fp": "f" * 16}, {"port": 502}, {"unit_id": 1},
                                  {"transport": "modbus_tcp"}, {"profile_id": "deye-sg"}, {"device_fp": None},
                                  {"fp_kind": "model"}])
def test_other_target_changes_still_count(over):
    old = _opts(_gw())
    assert rt_mod.control_options_changed(old, _opts(_gw(host=NEW_HOST, **over)), profile=GW_V)
    assert rt_mod.control_options_changed(old, _opts(_gw(**over)), profile=GW_V)


def test_weak_or_missing_fingerprint_host_change_still_counts():
    fox = {"profile_id": "foxess-h", "transport": "modbus_tcp", "host": OLD_HOST, "port": 502, "unit_id": 247,
           "device_fp": "0123456789abcdef"}
    assert rt_mod.control_options_changed(_opts(fox), _opts({**fox, "host": NEW_HOST}), profile=FOX_V)
    bare = _gw(device_fp=None)
    assert rt_mod.control_options_changed(_opts(bare), _opts({**bare, "host": NEW_HOST}), profile=GW_V)


@pytest.mark.asyncio
async def test_host_only_change_skips_restore_through_the_old_executor():
    order: list = []
    rt = SimpleNamespace(executor=_OwnedDirect(order))
    old, new = _opts(_gw()), _opts(_gw(host=NEW_HOST))
    assert await rt_mod.async_restore_if_control_changed(rt, old, new) is False
    assert order == [] and rt.executor.owned
    assert await rt_mod.async_control_change_allowed(rt, old, new) is True and order == []
    assert await rt_mod.async_restore_if_control_changed(rt, old, _opts(_gw(unit_id=1))) is True
    assert order == ["restore"]


@pytest.mark.asyncio
async def test_host_change_restores_when_the_running_executor_is_not_direct():
    # Próba (trial) albo tryb encji: dawne zachowanie — zmiana hosta to zmiana sterowania.
    for kind, trial in (("direct", True), ("entities", False)):
        order: list = []
        ex = _OwnedDirect(order)
        ex.io = SimpleNamespace(kind=kind, trial=trial)
        rt = SimpleNamespace(executor=ex)
        assert await rt_mod.async_restore_if_control_changed(rt, _opts(_gw()), _opts(_gw(host=NEW_HOST))) is True
        assert order == ["restore"]


@pytest.mark.asyncio
async def test_failed_restore_still_blocks_a_real_target_change():
    order: list = []
    rt = SimpleNamespace(executor=_OwnedDirect(order, fail=True))
    assert await rt_mod.async_control_change_allowed(rt, _opts(_gw()), _opts(_gw(unit_id=1))) is False


def _listener(rt, options_now):
    from tests.test_setup_paired import _listener_hass
    return _listener_hass(rt, options_now)


@pytest.mark.asyncio
async def test_update_listener_host_only_change_reloads_without_restore():
    import custom_components.volcast as integ
    from tests.test_setup_paired import fake_runtime
    rt = fake_runtime()
    rt.options_at_setup = _opts(_gw())
    hass, entry, order = _listener(rt, _opts(_gw(host=NEW_HOST)))
    rt.executor = _OwnedDirect(order)
    frozen = []
    rt.executor.freeze = lambda: frozen.append(True)
    await integ._async_update_listener(hass, entry)
    assert order == ["reload"] and frozen == [] and rt.executor.owned


@pytest.mark.asyncio
async def test_update_listener_other_target_change_restores_first():
    import custom_components.volcast as integ
    from tests.test_setup_paired import fake_runtime
    rt = fake_runtime()
    rt.options_at_setup = _opts(_gw())
    hass, entry, order = _listener(rt, _opts(_gw(host=NEW_HOST, port=502)))
    rt.executor = _OwnedDirect(order)
    frozen = []
    rt.executor.freeze = lambda: frozen.append(True)
    await integ._async_update_listener(hass, entry)
    assert order == ["restore", "reload"] and frozen == [True]
