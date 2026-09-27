import asyncio

from custom_components.volcast.control.store import ControlState, ControlStore


def test_roundtrip_and_defaults():
    st = ControlStore(object(), "e1")
    loaded = asyncio.run(st.async_load())
    assert loaded == ControlState()          # pusty magazyn = wszystko wyłączone
    s = ControlState(plan_raw={"slots": []}, consent=True, local_switch=True, owned=True,
                     snapshot={"soc_min": 15.0}, history_imported_at="2026-09-27T00:00:00+00:00",
                     owner={"profile": "goodwe-et", "domain": "goodwe", "mode_entity": "select.x"},
                     restore_keys=["mode", "power_w"], taken_over=["soc_max"])
    asyncio.run(st.async_save(s))
    assert asyncio.run(st.async_load()) == s


def test_corrupt_fields_fail_closed():
    st = ControlStore(object(), "e1")
    asyncio.run(st._store.async_save({"consent": "yes", "local_switch": 1, "owned": "true",
                                      "snapshot": [1], "plan_raw": "x"}))
    assert asyncio.run(st.async_load()) == ControlState()


def test_owner_keeps_only_string_values():
    st = ControlStore(object(), "e1")
    asyncio.run(st._store.async_save({"owned": True, "owner": {"profile": "goodwe-et", "domain": 1}}))
    assert asyncio.run(st.async_load()).owner == {"profile": "goodwe-et"}
    asyncio.run(st._store.async_save({"owned": True, "owner": ["x"]}))
    assert asyncio.run(st.async_load()).owner == {}


def test_restore_keys_missing_or_corrupt_is_legacy_none():
    st = ControlStore(object(), "e1")
    asyncio.run(st._store.async_save({"owned": True}))
    loaded = asyncio.run(st.async_load())
    assert loaded.restore_keys is None and loaded.taken_over == []
    asyncio.run(st._store.async_save({"owned": True, "restore_keys": "mode", "taken_over": [1, "mode"]}))
    loaded = asyncio.run(st.async_load())
    assert loaded.restore_keys is None and loaded.taken_over == ["mode"]
    asyncio.run(st._store.async_save({"owned": True, "restore_keys": ["mode", 3]}))
    assert asyncio.run(st.async_load()).restore_keys == ["mode"]


def test_owner_mode_uid_persisted_beside_the_owner_record():
    # Rekord `owner` w magazynie zostaje w kształcie czytelnym dla poprzedniej wersji.
    st = ControlStore(object(), "e1")
    owner = {"profile": "goodwe-et", "domain": "goodwe", "mode_uid": "goodwe-ems_mode-X", "mode_entity": "select.x"}
    s = ControlState(owned=True, snapshot={"soc_min": 15.0}, owner=dict(owner))
    asyncio.run(st.async_save(s))
    raw = st._store._data
    assert raw["owner"] == {"profile": "goodwe-et", "domain": "goodwe", "mode_entity": "select.x"}
    assert raw["owner_mode_uid"] == "goodwe-ems_mode-X"
    assert s.owner == owner                                  # stan w pamięci nietknięty
    assert asyncio.run(st.async_load()) == s


def test_owner_mode_uid_ignored_without_owner_record_or_when_not_a_string():
    st = ControlStore(object(), "e1")
    asyncio.run(st._store.async_save({"owned": True, "owner": {}, "owner_mode_uid": "u"}))
    assert asyncio.run(st.async_load()).owner == {}
    asyncio.run(st._store.async_save({"owned": True, "owner": {"profile": "p"}, "owner_mode_uid": 5}))
    assert asyncio.run(st.async_load()).owner == {"profile": "p"}
