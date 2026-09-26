import pytest

from custom_components.volcast.core.registers import (
    RegisterError, RegisterImage, decode, read_values,
)


def test_types_scale_sign_undef():
    img = RegisterImage.from_blocks({100: [0xFFFF, 0x0001, 0x86A0, 0xFFFF, 0xFFFF, 0x3F80, 0x0000,
                                           0x4757, 0x384B]})
    assert decode({"addr": 100, "type": "u16"}, img) == 65535
    assert decode({"addr": 100, "type": "i16"}, img) == -1
    assert decode({"addr": 101, "type": "u32"}, img) == 100000
    assert decode({"addr": 101, "type": "u32", "scale": 0.1}, img) == pytest.approx(10000.0)
    assert decode({"addr": 103, "type": "u32", "undef": 4294967295}, img) == 0
    assert decode({"addr": 105, "type": "f32"}, img) == 1.0
    assert decode({"addr": 100, "type": "i16", "sign": -1}, img) == 1
    assert decode({"addr": 107, "type": "ascii", "len": 2}, img) == "GW8K"


def test_missing_register_raises():
    with pytest.raises(RegisterError):
        decode({"addr": 1, "type": "u16"}, RegisterImage.from_blocks({}))


def test_read_values_sum_ref_and_missing():
    img = RegisterImage.from_blocks({10: [100, 50, 0xFFE0]})
    rmap = {"pv_power_w": {"sum": [{"addr": 10, "type": "u16"}, {"addr": 11, "type": "u16"}]},
            "active_power_w": {"addr": 12, "type": "i16"},
            "load_power_w": {"sum": [{"ref": "pv_power_w"}, {"ref": "active_power_w", "sign": -1}]},
            "soc": {"addr": 999, "type": "u16"}}
    v = read_values(rmap, img)
    assert (v["pv_power_w"], v["active_power_w"], v["load_power_w"], v["soc"]) == (150, -32, 182, None)
