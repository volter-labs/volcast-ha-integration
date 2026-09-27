import asyncio

from custom_components.volcast.control.store import ControlState, ControlStore


def test_roundtrip_and_defaults():
    st = ControlStore(object(), "e1")
    loaded = asyncio.run(st.async_load())
    assert loaded == ControlState()          # pusty magazyn = wszystko wyłączone
    s = ControlState(plan_raw={"slots": []}, consent=True, local_switch=True, owned=True,
                     snapshot={"soc_min": 15.0}, history_imported_at="2026-09-27T00:00:00+00:00",
                     owner={"profile": "goodwe-et", "domain": "goodwe", "mode_entity": "select.x"})
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
