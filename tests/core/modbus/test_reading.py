from datetime import datetime, timezone

import pytest

from custom_components.volcast.core.modbus.reading import DirectReading, build_reading, current_programs
from custom_components.volcast.core.params import TouProgram
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage

from .helpers import deye_image, goodwe_image

GW = load_builtin("goodwe-et")
DEYE = load_builtin("deye-sg")
AT = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def _read(profile, image):
    return build_reading(profile, image, at_mono=100.0, at_utc=AT)


def test_goodwe_reading_from_golden_frames():
    r = _read(GW, goodwe_image())
    assert isinstance(r, DirectReading)
    # wartości jak w dekodowaniu referencyjnym (test_goodwe_profile.py / test_goodwe_et.c)
    assert r.values["soc"] == 83 and r.values["grid_power_w"] == 2240
    assert r.device["mode"] == "charge_battery"                     # mode_value 11
    assert r.device["power_w"] == 8846.0 and r.device["export_limit_w"] == 16000.0
    assert r.device["export_limit_enabled"] == 0.0 and r.device["soc_min"] == 5.0
    assert "soc_max" not in r.device                                # 47760 nieczytelny
    assert r.programs is None and r.tou_enabled is None
    assert (r.at_mono, r.at_utc) == (100.0, AT)


def test_unknown_mode_value_is_marked_foreign():
    img = goodwe_image()
    words = {a: img.words(a, 1)[0] for a in (47509, 47510, 47512)}
    r = _read(GW, RegisterImage({**words, 47511: 99}))
    assert r.device["mode"] == "?99"


def test_missing_registers_leave_device_keys_out():
    r = _read(GW, RegisterImage({47511: 1}))
    assert r.device == {"mode": "auto"}
    assert r.values["soc"] is None


def test_export_limit_enabled_is_normalised_to_one():
    r = _read(GW, RegisterImage({47509: 2}))
    assert r.device["export_limit_enabled"] == 1.0


def test_deye_lo_hi_u32_decoded():
    r = _read(DEYE, deye_image())
    assert r.values["pv_energy_total_kwh"] == pytest.approx(12345.6)
    assert r.values["soc"] == 64 and r.values["battery_power_w"] == -1500
    assert r.values["pv_power_w"] == 2100


def test_current_programs_hhmm_and_bit():
    progs = current_programs(DEYE, deye_image())
    assert [p.start_min for p in progs] == [0, 300, 600, 840, 1080, 1320]
    assert progs[0] == TouProgram(start_min=0, power_w=3000.0, soc=80.0, grid_charge=True)
    assert [p.grid_charge for p in progs] == [True, False, False, False, False, True]
    # Bit sieci czytany z bitu profilu, pozostałe bity słowa bez znaczenia.
    assert current_programs(DEYE, deye_image(**{"173": 0b11100}))[1].grid_charge is False
    assert current_programs(DEYE, deye_image(**{"173": 0b11101}))[1].grid_charge is True


@pytest.mark.parametrize("word", [2460, 2400, 1260, 9999])
def test_current_programs_invalid_hhmm_is_none(word):
    assert current_programs(DEYE, deye_image(**{"150": word})) is None


def test_current_programs_missing_register_is_none():
    assert current_programs(DEYE, RegisterImage({148: 0})) is None
    assert current_programs(GW, goodwe_image()) is None          # profil bez programów


def test_serial_never_in_values():
    for profile, image in ((GW, goodwe_image()), (DEYE, deye_image())):
        r = _read(profile, image)
        assert "serial" not in r.values
        text = repr(r.values) + repr(r.device)
        assert "GOLDENSERIAL" not in text and "SYNTHSER" not in text


def test_device_view_has_tou_keys():
    r = _read(DEYE, deye_image())
    for i in range(1, 7):
        for f in ("start", "power_w", "soc", "grid_charge"):
            assert f"tou.{i}.{f}" in r.device
    assert r.device["tou.2.start"] == 300.0 and r.device["tou.1.grid_charge"] == 1.0
    assert r.device["tou.6.soc"] == 80.0 and r.device["tou.3.power_w"] == 5000.0
    assert r.device["tou_enabled"] == 1.0 and r.tou_enabled is True
    assert len(r.programs) == 6


def test_tou_disabled_bit_and_bad_programs():
    r = _read(DEYE, deye_image(**{"146": 0x00FE}))
    assert r.tou_enabled is False and r.device["tou_enabled"] == 0.0
    r = _read(DEYE, deye_image(**{"150": 2460}))
    assert r.programs is None
    assert not any(k.startswith("tou.") for k in r.device)       # niepewne programy — brak widoku pól
    assert r.device["tou_enabled"] == 1.0
    r = _read(DEYE, RegisterImage({}))
    assert r.tou_enabled is None and "tou_enabled" not in r.device


def test_write_only_key_read_from_its_own_register():
    from datetime import datetime, timezone
    from custom_components.volcast.core.modbus.reading import build_reading
    from custom_components.volcast.core.profile import load_builtin
    from custom_components.volcast.core.registers import RegisterImage
    gw = load_builtin("goodwe-et")
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert build_reading(gw, RegisterImage({47760: 95}), at_mono=0.0, at_utc=at).device["soc_max"] == 95.0
    assert "soc_max" not in build_reading(gw, RegisterImage({}), at_mono=0.0, at_utc=at).device


def test_reading_takes_input_registers_from_their_own_space():
    # `fc: 4` — odczyt z rejestrów input; ten sam adres w holding to inny rejestr.
    from custom_components.volcast.core.profile import profile_from_dict
    from tests.core.profile_fixtures import ms_profile
    raw = ms_profile()
    raw["read"]["soc"] = {"addr": 47511, "type": "u16", "fc": 4}
    p = profile_from_dict(raw)
    r = _read(p, RegisterImage.from_blocks({47509: [0, 0, 1, 0], (4, 47511): [55]}))
    assert r.values["soc"] == 55
    assert _read(p, RegisterImage.from_blocks({47509: [0, 0, 1, 0]})).values["soc"] is None
