"""Raport weryfikacji huba `modbus`: identyfikacja (rejestr 0) i porównanie odczytów huba z encjami użytkownika."""
from datetime import datetime, timezone

import pytest

from custom_components.volcast.core.control import hub_check as hc
from custom_components.volcast.core.registers import RegisterImage
from tests.control.test_ha_modbus_control import hub_words

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def deye():
    from tests.control.test_executor_direct import DEYE_V
    return DEYE_V


def image(**over):
    words = hub_words()
    for addr, v in over.items():
        words[int(addr.lstrip("r"))] = v & 0xFFFF
    return RegisterImage(words)


def build(deye, img, user, **kw):
    return hc.build_report(deye, img, user, now=NOW, **kw)


def test_match_is_ok(deye):
    img = image(r588=55, r590=-1200)
    rep = build(deye, img, {"soc": 54.0, "battery_power_w": -1150.0})
    assert rep["verdict"] == hc.OK and rep["identity_ok"] is True and not hc.blocks_control(rep)
    soc = rep["quantities"]["soc"]
    assert (soc["hub"], soc["user"], soc["deviation"], soc["verdict"]) == (55.0, 54.0, 1.0, hc.OK)
    assert rep["quantities"]["battery_power_w"]["deviation"] == 50.0
    assert rep["checked_at"] == "2026-01-02T03:04:05+00:00"


def test_soc_mismatch_blocks_control(deye):
    rep = build(deye, image(r588=55, r590=0), {"soc": 80.0, "battery_power_w": 0.0})
    assert rep["verdict"] == hc.MISMATCH and hc.blocks_control(rep)
    assert rep["quantities"]["soc"]["verdict"] == hc.MISMATCH
    assert rep["quantities"]["battery_power_w"]["verdict"] == hc.OK


def test_soc_tolerance_edge(deye):
    ok = build(deye, image(r588=55), {"soc": 50.0})["quantities"]["soc"]["verdict"]
    bad = build(deye, image(r588=55), {"soc": 49.0})["quantities"]["soc"]["verdict"]
    assert (ok, bad) == (hc.OK, hc.MISMATCH)


def test_battery_power_band_is_relative_with_a_floor(deye):
    # 10 kW: 15 % = 1500 W; 400 W: podłoga 300 W
    assert build(deye, image(r590=10000), {"battery_power_w": 8600.0})["quantities"][
        "battery_power_w"]["verdict"] == hc.OK
    assert build(deye, image(r590=10000), {"battery_power_w": 8400.0})["quantities"][
        "battery_power_w"]["verdict"] == hc.MISMATCH
    assert build(deye, image(r590=400), {"battery_power_w": 80.0})["quantities"][
        "battery_power_w"]["verdict"] == hc.MISMATCH
    assert build(deye, image(r590=400), {"battery_power_w": 150.0})["quantities"][
        "battery_power_w"]["verdict"] == hc.OK


def test_inverted_sign_is_a_mismatch_with_a_hint(deye):
    rep = build(deye, image(r590=-2000), {"battery_power_w": 2000.0})
    q = rep["quantities"]["battery_power_w"]
    assert q["verdict"] == hc.MISMATCH and q["sign_inverted"] is True and hc.blocks_control(rep)


def test_wrong_device_type_in_register_zero_is_reported(deye):
    rep = build(deye, image(r0=9999, r588=55), {"soc": 55.0})
    assert rep["identity_ok"] is False and rep["verdict"] == hc.IDENTITY_MISMATCH
    assert rep["device_type"] == 9999 and hc.blocks_control(rep)


def test_missing_user_entity_cannot_compare_and_does_not_block(deye):
    rep = build(deye, image(r588=55, r590=100), {})
    assert rep["verdict"] == hc.CANNOT_COMPARE and not hc.blocks_control(rep)
    assert {q["verdict"] for q in rep["quantities"].values()} == {hc.CANNOT_COMPARE}
    assert rep["quantities"]["soc"]["user"] is None and rep["quantities"]["soc"]["deviation"] is None


def test_partial_when_one_quantity_cannot_be_compared(deye):
    rep = build(deye, image(r588=55, r590=100), {"soc": 55.0})
    assert rep["verdict"] == hc.PARTIAL and not hc.blocks_control(rep)


def test_mismatch_wins_over_cannot_compare(deye):
    rep = build(deye, image(r588=55), {"soc": 10.0})
    assert rep["verdict"] == hc.MISMATCH


def test_hub_soc_out_of_range_is_a_mismatch(deye):
    rep = build(deye, image(r588=400), {"soc": 100.0})
    assert rep["quantities"]["soc"]["verdict"] == hc.MISMATCH


def test_report_has_no_entity_ids_or_names(deye):
    rep = build(deye, image(r588=55), {"soc": 55.0})
    text = repr(rep)
    assert "sensor." not in text
    assert set(rep) == {"verdict", "identity_ok", "device_type", "checked_at", "quantities"}
