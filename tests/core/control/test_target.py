"""Cele zapisu cyklu: encje (dotychczasowy kod) i rejestry."""
import pytest

from custom_components.volcast.core.control.cycle import EntityContext
from custom_components.volcast.core.control.target import EntityTarget, RegisterTarget
from custom_components.volcast.core.params import Params
from custom_components.volcast.core.registers import RegisterWrite, encode_writes
from tests.core.golden import load_golden, params_from_golden

from .conftest import goodwe_reading

ENTS = EntityContext(
    domain="goodwe",
    mapped={"mode": "select.ems_mode", "power_w": "number.ems_power", "soc_min": "number.dod",
            "soc_max": "number.soc_upper", "export_limit_w": "number.export_limit",
            "export_limit_enabled": "switch.export_limit"},
    units={"power_w": "W", "soc_min": "%", "soc_max": "%", "export_limit_w": "W"},
    attrs={"select.ems_mode": {"options": ["auto", "sell_power"]},
           "number.ems_power": {"min": 0, "max": 10000, "step": 1}},
    readings={"mode": "auto", "power_w": "unavailable"})


def test_entity_target_kind_and_helpers(goodwe_profile):
    t = EntityTarget(ENTS)
    assert t.kind == "entities" and t.missing_keys(goodwe_profile) == ()
    assert t.has_temperature() is False
    assert t.mode_option_unknown("sell_power", goodwe_profile) is False
    assert t.mode_option_unknown("charge_battery", goodwe_profile) is True
    assert t.device_view({"mode": 1, "power_w": 1}, goodwe_profile) == {"mode": "auto"}
    writes, unmapped = t.writes(Params(mode="auto"), goodwe_profile, None)
    assert [w.key for w in writes] == ["mode"] and unmapped == ()


def test_register_target_basics(goodwe_profile, reading_auto):
    t = RegisterTarget(reading_auto)
    assert t.kind == "direct" and t.missing_keys(goodwe_profile) == ()
    assert t.has_temperature() is True
    assert t.mode_option_unknown("charge_battery", goodwe_profile) is False
    assert t.device_view({"mode": 0, "power_w": 0, "soc_max": 0}, goodwe_profile) == {
        "mode": "auto", "power_w": 8846.0}                       # 47760 nieczytelny w nagraniu


def test_register_fit_clamps_percent_and_reports_adjusted(goodwe_profile, reading_auto):
    t = RegisterTarget(reading_auto)
    p, adjusted, unfit = t.fit(Params(mode="auto", soc_min=20.4, soc_max=95.6, power_w=70000.7,
                                      export_limit_w=-5.0), goodwe_profile)
    assert (p.soc_min, p.soc_max, p.power_w, p.export_limit_w) == (21.0, 95.0, 65535.0, 0.0)
    assert adjusted == ("soc_min", "soc_max", "power_w", "export_limit_w") and unfit == ()
    same, adjusted, _ = t.fit(Params(mode="auto", soc_min=20.0, power_w=2500.0), goodwe_profile)
    assert adjusted == () and same.power_w == 2500.0


def test_register_writes_use_current_image_for_bit_fields():
    from custom_components.volcast.core.params import TouProgram
    from custom_components.volcast.core.profile import load_builtin
    from custom_components.volcast.core.modbus.reading import build_reading
    from custom_components.volcast.core.registers import RegisterImage
    from tests.core.golden import T0
    from tests.sim.fixtures import deye_words
    deye = load_builtin("deye-sg")
    words = deye_words()
    words[172] = 0b10                                           # bit właściciela
    t = RegisterTarget(build_reading(deye, RegisterImage(words), at_mono=0.0, at_utc=T0))
    progs = tuple(TouProgram(start_min=i * 240, power_w=1000.0, soc=50.0, grid_charge=True) for i in range(6))
    writes, _ = t.writes(Params(tou=progs), deye, {"tou.1.grid_charge"})
    assert writes == [RegisterWrite("tou.1.grid_charge", 172, 0b11)]


def test_register_target_never_writes_unreadable(goodwe_profile, reading_auto):
    t = RegisterTarget(reading_auto, unreadable=frozenset({"soc_max"}))
    writes, unmapped = t.writes(Params(mode="auto", soc_min=20.0, soc_max=90.0), goodwe_profile, None)
    assert [w.key for w in writes] == ["soc_min", "mode"] and unmapped == ()
    writes, _ = t.writes(Params(mode="auto", soc_max=90.0), goodwe_profile, {"soc_max", "mode"})
    assert [w.key for w in writes] == ["mode"]


def test_golden_mapper_vectors_register_path(goodwe_profile, reading_auto):
    t = RegisterTarget(reading_auto)
    for vec in load_golden("mapper")["vectors"]:
        params = params_from_golden(vec["params"], goodwe_profile)
        writes, _ = t.writes(params, goodwe_profile, None)
        assert [(w.addr, w.value) for w in writes] == \
            [(w.addr, w.value) for w in encode_writes(params, goodwe_profile)]
    for vec in load_golden("applier")["vectors"]:
        if vec["fail_kind"] is None:
            writes, _ = t.writes(params_from_golden(vec["params"], goodwe_profile), goodwe_profile, None)
            assert [[w.addr, w.value] for w in writes] == vec["writes"]


def test_register_target_unreadable_tou_prefix():
    from custom_components.volcast.core.params import TouProgram
    from custom_components.volcast.core.profile import load_builtin
    from custom_components.volcast.core.modbus.reading import build_reading
    from custom_components.volcast.core.registers import RegisterImage
    from tests.core.golden import T0
    from tests.sim.fixtures import deye_words
    deye = load_builtin("deye-sg")
    t = RegisterTarget(build_reading(deye, RegisterImage(deye_words()), at_mono=0.0, at_utc=T0),
                       unreadable=frozenset({"tou"}))
    progs = tuple(TouProgram(start_min=i * 240, power_w=1000.0, soc=50.0, grid_charge=True) for i in range(6))
    assert t.writes(Params(tou=progs), deye, None) == ([], ())
    assert t.writes(Params(tou=progs), deye, {"tou.1.soc"}) == ([], ())
