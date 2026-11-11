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


def test_sum_register_term_applies_sign_once():
    # 0xFFE0 jako i16 to -32; z odwróconym znakiem oczekujemy +32, nie -32 (podwójne odwrócenie).
    img = RegisterImage.from_blocks({12: [0xFFE0]})
    rmap = {"g": {"sum": [{"addr": 12, "type": "i16", "sign": -1}]}}
    assert read_values(rmap, img)["g"] == 32


def test_ref_cycle_raises():
    img = RegisterImage.from_blocks({})
    rmap = {"a": {"sum": [{"ref": "b"}]}, "b": {"sum": [{"ref": "a"}]}}
    with pytest.raises(RegisterError):
        read_values(rmap, img)


def test_undef_ignored_on_f32():
    # Wzorzec bitowy 1.0f32 potraktowany jako liczba całkowita akurat równa "undef" —
    # dla f32 to nie ma znaczenia, undef dotyczy tylko sum liczników PV (u32).
    img = RegisterImage.from_blocks({105: [0x3F80, 0x0000]})
    assert decode({"addr": 105, "type": "f32", "undef": 0x3F800000}, img) == 1.0


def _engine_profile(**write_overrides):
    from custom_components.volcast.core.profile import profile_from_dict
    from tests.core.profile_fixtures import ms_profile
    raw = ms_profile()
    raw["write"]["enable"] = {"addr": 40029, "type": "u16", "encode": "on_off", "on": 0xAA, "off": 0x55}
    raw["write_policy"]["order"].insert(-1, "enable")
    for key, extra in write_overrides.items():
        raw["write"][key].update(extra)
    return profile_from_dict(raw, origin="package")


def test_encode_on_off_writes_profile_values():
    from custom_components.volcast.core.params import Params
    from custom_components.volcast.core.registers import encode_writes
    prof = _engine_profile()
    on = encode_writes(Params(power_w=0, enable=True, mode="auto"), prof)
    assert [(w.key, w.addr, w.value) for w in on] == [("power_w", 47512, 0), ("enable", 40029, 0xAA),
                                                      ("mode", 47511, 1)]
    off = encode_writes(Params(enable=False), prof)
    assert [(w.key, w.value) for w in off] == [("enable", 0x55)]


# --- zapis 32-bitowy, skala, adres urządzenia per rejestr, moc wg kierunku -----------------------------

from custom_components.volcast.core.params import Params  # noqa: E402
from custom_components.volcast.core.registers import RegisterWrite, encode_writes  # noqa: E402
from tests.core.modbus.engine_profile import PLANT, directional_profile  # noqa: E402


def test_u32_write_two_words_high_first_on_own_unit():
    w, = encode_writes(Params(charge_power_w=2500), directional_profile())
    assert w == RegisterWrite("charge_power_w", 40032, 2500, count=2, word_order="hi_lo", unit_id=PLANT)
    assert w.words() == [0, 2500]
    big, = encode_writes(Params(charge_power_w=70000), directional_profile())
    assert big.words() == [1, 70000 - 65536]


def test_u32_low_word_first_and_scale_divides():
    """Kierunek skali jak przy odczycie (wartość = surowa × skala), więc przy zapisie surowa = wartość / skala."""
    w, = encode_writes(Params(discharge_power_w=70000), directional_profile())
    assert (w.value, w.count, w.word_order, w.unit_id) == (7000, 2, "lo_hi", PLANT)
    assert w.words() == [7000, 0]


def test_scaled_value_rounds_toward_the_safe_side():
    prof = directional_profile()
    # moc: w dół (nigdy więcej niż w planie); dolny próg SoC: w górę
    assert encode_writes(Params(discharge_power_w=2509), prof)[0].value == 250
    assert encode_writes(Params(soc_min=55.55), prof)[0].value == 556
    assert encode_writes(Params(soc_min=55), prof)[0].value == 550
    assert encode_writes(Params(soc_min=0.3), prof)[0].value == 3          # 0.3 / 0.1 bez błędu float


def test_u32_and_scaled_range_clamped():
    prof = directional_profile()
    assert encode_writes(Params(charge_power_w=2 ** 40), prof)[0].value == 0xFFFFFFFF
    assert encode_writes(Params(charge_power_w=-5), prof)[0].value == 0
    assert encode_writes(Params(soc_min=150), prof)[0].value == 1000           # 100 % / 0,1
    with pytest.raises(RegisterError):
        encode_writes(Params(charge_power_w=float("nan")), prof)


def test_u16_without_new_fields_is_unchanged():
    """Klucz bez typu u32, skali i unit_id: to samo słowo, jedno, na adresie połączenia."""
    w, = encode_writes(Params(export_limit_w=4000.4), directional_profile())
    assert w == RegisterWrite("export_limit_w", 47510, 4000)
    assert (w.count, w.word_order, w.unit_id, w.words()) == (1, "hi_lo", None, [4000])


@pytest.mark.parametrize("mode,key", [("charge_battery", "charge_power_w"), ("sell_power", "discharge_power_w"),
                                      ("discharge_battery", "discharge_power_w")])
def test_power_by_direction_picks_the_set_point_of_the_mode(mode, key):
    writes = encode_writes(Params(mode=mode, power_w=3000), directional_profile())
    assert [w.key for w in writes] == [key, "mode"]
    assert writes[0].unit_id == PLANT and writes[1].unit_id == PLANT
    # filtr kluczy w konwencji planu (`power_w`) obejmuje nastawę kierunku
    only = encode_writes(Params(mode=mode, power_w=3000), directional_profile(), keys={"power_w"})
    assert [w.key for w in only] == [key]


@pytest.mark.parametrize("mode", ["auto", "battery_standby"])
def test_power_by_direction_neutral_mode_writes_no_set_point(mode):
    assert [w.key for w in encode_writes(Params(mode=mode, power_w=0), directional_profile())] == ["mode"]


def test_power_by_direction_without_mode_or_with_conflict_refused():
    prof = directional_profile()
    with pytest.raises(RegisterError):
        encode_writes(Params(power_w=3000), prof)            # kierunek nieznany — nic nie wysłane
    with pytest.raises(RegisterError):
        encode_writes(Params(mode="charge_battery", power_w=3000, charge_power_w=2000), prof)
    same = encode_writes(Params(mode="charge_battery", power_w=3000, charge_power_w=3000), prof)
    assert [w.key for w in same] == ["charge_power_w", "mode"]
    # jawne nastawy obu kierunków (np. zero obu przy przejęciu) — bez planu mocy
    both = encode_writes(Params(charge_power_w=0, discharge_power_w=0), prof)
    assert [(w.key, w.words()) for w in both] == [("charge_power_w", [0, 0]), ("discharge_power_w", [0, 0])]


def test_enable_and_mode_on_own_unit():
    writes = encode_writes(Params(enable=True, mode="auto"), directional_profile())
    assert [(w.key, w.addr, w.value, w.unit_id) for w in writes] == [("enable", 40029, 1, PLANT),
                                                                     ("mode", 40031, 1, PLANT)]


def test_power_w_without_direction_flag_unchanged():
    """Profil bez `power_by_direction`: `power_w` jak dotąd (bez trybu też)."""
    w, = encode_writes(Params(power_w=2500), _engine_profile())
    assert w == RegisterWrite("power_w", 47512, 2500)


def test_u32_power_w_without_direction_flag():
    w, = encode_writes(Params(power_w=100000), _engine_profile(power_w={"type": "u32", "word_order": "lo_hi"}))
    assert (w.addr, w.words()) == (47512, [100000 - 65536, 1])


def test_register_write_word_split_refuses_out_of_range():
    with pytest.raises(RegisterError):
        RegisterWrite("x", 1, 0x10000).words()
    with pytest.raises(RegisterError):
        RegisterWrite("x", 1, 1 << 32, count=2).words()
    with pytest.raises(RegisterError):
        RegisterWrite("x", 1, 5, count=2, word_order="??").words()


def test_read_with_own_unit_id_from_its_unit_image_only():
    """Odczyt z innego adresu urządzenia czyta wyłącznie bloki tego adresu — nigdy słowo połączenia."""
    img = RegisterImage.from_blocks({10: [100], (3, 10, 1): [7], (4, 20, 1): [0, 9]})
    v = read_values({"soc": {"addr": 10, "type": "u16", "unit_id": 1},
                     "pv_power_w": {"sum": [{"addr": 20, "type": "u32", "fc": 4, "unit_id": 1}]},
                     "grid": {"addr": 10, "type": "u16", "unit_id": 2},
                     "active_power_w": {"addr": 10, "type": "u16"}}, img)
    assert v == {"soc": 7, "pv_power_w": 9, "grid": None, "active_power_w": 100}
    assert img.words(10, 1, 3, 1) == [7] and img.words(10, 1) == [100]
    with pytest.raises(RegisterError):
        img.words(20, 2, 4)                     # input połączenia nie ma tego słowa
