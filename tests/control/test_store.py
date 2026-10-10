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


TOU_SNAP = {"programs": [[0, 3000, 80, 1], [300, 5000, 20, 0], [600, 5000, 20, 0], [840, 5000, 20, 0],
                         [1080, 4000, 30, 0], [1320, 3000, 80, 1]], "tou_word": 255}


def test_tou_snapshot_roundtrip_and_legacy_store():
    st = ControlStore(object(), "e1")
    asyncio.run(st._store.async_save({"owned": True}))
    assert asyncio.run(st.async_load()).tou_snapshot is None          # stary magazyn bez pola
    s = ControlState(owned=True, tou_snapshot=TOU_SNAP)
    asyncio.run(st.async_save(s))
    assert asyncio.run(st.async_load()).tou_snapshot == TOU_SNAP


def test_bad_tou_snapshot_dropped_with_warning(caplog):
    import copy
    st = ControlStore(object(), "e1")
    bad = []
    for mutate in (lambda d: d["programs"].pop(), lambda d: d["programs"][0].__setitem__(0, 1440),
                   lambda d: d["programs"][1].__setitem__(2, 101), lambda d: d["programs"][2].__setitem__(1, 1.5),
                   lambda d: d["programs"][3].__setitem__(3, True), lambda d: d.__setitem__("tou_word", 70000),
                   lambda d: d.__setitem__("programs", "x")):
        snap = copy.deepcopy(TOU_SNAP)
        mutate(snap)
        bad.append(snap)
    bad += ["x", [1], {"programs": TOU_SNAP["programs"]}]
    with caplog.at_level("WARNING"):
        for snap in bad:
            asyncio.run(st._store.async_save({"owned": True, "tou_snapshot": snap}))
            assert asyncio.run(st.async_load()).tou_snapshot is None
    assert "time-of-use snapshot" in caplog.text


def test_nvm_log_roundtrip_legacy_and_corrupt_entries():
    st = ControlStore(object(), "e1")
    s = ControlState(owned=True, nvm_log=[["mode", 1_790_000_000.0], ["power_w", 1_790_000_060.0]])
    asyncio.run(st.async_save(s))
    assert asyncio.run(st.async_load()).nvm_log == s.nvm_log
    asyncio.run(st._store.async_save({"owned": True}))                      # stary magazyn bez pola
    assert asyncio.run(st.async_load()).nvm_log == []
    asyncio.run(st._store.async_save({"nvm_log": [["mode", 1.0], ["", 2.0], ["x", "3"], ["y", float("inf")],
                                                  "z", ["a", True], ["b", 4]]}))
    assert asyncio.run(st.async_load()).nvm_log == [["mode", 1.0], ["b", 4.0]]


def test_installation_salt_is_stable_and_outside_the_control_record():
    from types import SimpleNamespace

    from custom_components.volcast.control.store import async_installation_salt
    hass = SimpleNamespace(data={})
    salt = asyncio.run(async_installation_salt(hass))
    assert isinstance(salt, bytes) and len(salt) == 16
    assert asyncio.run(async_installation_salt(hass)) == salt
    assert "salt" not in ControlState.__dataclass_fields__
    other = asyncio.run(async_installation_salt(SimpleNamespace(data={})))
    assert other != salt                                                     # losowa na instalację


def _ladder_record():
    from datetime import datetime, timezone

    from custom_components.volcast.core.control.ladder import Ladder, LadderParams

    lad = Ladder(3, LadderParams(24, 15, 500), device_key="3f9a1c0e7b2d4a6f")
    lad.tick(datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc))
    return lad.to_record()


def test_verification_and_plan_only_round_trip_and_default():
    st = ControlStore(object(), "e1")
    loaded = asyncio.run(st.async_load())
    assert loaded.verification == {} and loaded.plan_only is False
    s = ControlState(verification=_ladder_record(), plan_only=True)
    asyncio.run(st.async_save(s))
    back = asyncio.run(st.async_load())
    assert back.verification == s.verification and back.plan_only is True


def test_malformed_verification_record_is_dropped_with_a_warning(caplog):
    st = ControlStore(object(), "e1")
    for bad in ({"v": 1, "device_key": "123"}, ["x"], "verified"):
        asyncio.run(st._store.async_save({"verification": bad, "plan_only": "yes"}))
        caplog.clear()
        loaded = asyncio.run(st.async_load())
        assert loaded.verification == {} and loaded.plan_only is False
        assert "verification" in caplog.text
