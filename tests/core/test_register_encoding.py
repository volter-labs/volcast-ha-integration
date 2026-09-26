import pytest

from custom_components.volcast.core.params import Params, TouProgram
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from custom_components.volcast.core.registers import RegisterError, RegisterImage, encode_writes
from tests.core.profile_fixtures import tw_profile

GW = load_builtin("goodwe-et")
TW = profile_from_dict(tw_profile())


def test_goodwe_rounding_and_clamping_like_c():
    ws = encode_writes(Params(mode="auto", soc_min=19.5, power_w=70000.0, export_limit_w=625.4,
                              export_limit_enabled=True), GW)
    assert [(w.key, w.addr, w.value) for w in ws] == [
        ("soc_min", 45356, 20), ("power_w", 47512, 65535), ("export_limit_w", 47510, 625),
        ("export_limit_enabled", 47509, 1), ("mode", 47511, 1)]


def test_keys_filter_keeps_order():
    ws = encode_writes(Params(mode="sell_power", power_w=400.0, soc_min=20.0), GW, keys={"mode", "power_w"})
    assert [w.key for w in ws] == ["power_w", "mode"]


def test_tou_encoding_hhmm_and_bit_preserves_other_bits():
    cur = RegisterImage.from_blocks({172: [0b10, 0, 0, 0, 0, 0]})
    progs = tuple(TouProgram(m, 1000.0 * (i + 1), 20.0 + i, i == 0)
                  for i, m in enumerate((0, 150, 360, 600, 1020, 1380)))
    ws = {w.key: w for w in encode_writes(Params(tou=progs), TW, current=cur)}
    assert (ws["tou.2.start"].addr, ws["tou.2.start"].value) == (149, 230)
    assert (ws["tou.6.start"].addr, ws["tou.6.start"].value) == (153, 2300)
    assert (ws["tou.1.grid_charge"].addr, ws["tou.1.grid_charge"].value) == (172, 0b11)
    assert ws["tou.2.grid_charge"].value == 0
    assert (ws["tou.3.power_w"].addr, ws["tou.3.power_w"].value) == (156, 3000)
    assert list(ws)[:4] == ["tou.1.soc", "tou.1.power_w", "tou.1.grid_charge", "tou.1.start"]


def test_bit_field_without_current_image_is_error():
    with pytest.raises(RegisterError):
        encode_writes(Params(tou=(TouProgram(0, 1000.0, 20.0, True),)), TW)
