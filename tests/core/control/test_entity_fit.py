import json

import pytest

from custom_components.volcast.core.control.entity_fit import control_writes, fit_number, fit_params
from custom_components.volcast.core.params import Params
from custom_components.volcast.core.profile import PROFILES_DIR, load_builtin, profile_from_dict

GW = load_builtin("goodwe-et")
MAPPED = {"mode": "select.ems_mode", "power_w": "number.ems_power", "soc_min": "number.dod",
          "soc_max": "number.soc_upper", "export_limit_w": "number.export_limit",
          "export_limit_enabled": "switch.export_limit"}
UNITS_W = {"power_w": "W", "soc_min": "%", "soc_max": "%", "export_limit_w": "W"}
ATTRS = {"number.ems_power": {"min": 0, "max": 10000, "step": 1},
         "number.dod": {"min": 0, "max": 99, "step": 1},
         "number.soc_upper": {"min": 10, "max": 100, "step": 1},
         "number.export_limit": {"min": 0, "max": 10000, "step": 1}}


def test_fit_number_clip_and_step():
    assert fit_number(625.6, {"min": 0, "max": 10000, "step": 1}) == 626
    assert fit_number(120.0, {"min": 0, "max": 99, "step": 1}) == 99
    assert fit_number(5.432, {"min": 0, "max": 10, "step": 0.1}) == pytest.approx(5.4)
    assert fit_number(9.99, {"min": 0, "max": 10, "step": 0.3}) == pytest.approx(9.9)   # krok nie wychodzi poza max
    assert fit_number(5.0, {"max": 10}) is None                                         # bez min = zakres nieznany
    assert fit_number(5.0, {"min": 10, "max": 0}) is None


def test_control_writes_requires_units():
    with pytest.raises(TypeError):
        control_writes(Params(mode="auto"), GW, "goodwe", MAPPED, keys=None, units=None)  # type: ignore[arg-type]


def test_kw_entity_receives_kw():
    writes, _ = control_writes(Params(power_w=5000.0), GW, "goodwe", MAPPED, keys=None,
                               units={**UNITS_W, "power_w": "kW"})
    assert writes[0].data == {"value": 5.0}


def test_dod_clipped_to_entity_max():
    p, adjusted, unfit = fit_params(Params(soc_min=0.0), GW, "goodwe", MAPPED, UNITS_W, ATTRS)
    # próg 0 % = DoD 100 > max 99 → DoD 99 → próg 1 % (bezpieczniej: wyżej)
    assert p.soc_min == 1.0 and adjusted == ("soc_min",) and unfit == ()


def test_fit_is_idempotent_no_rewrite():
    attrs = {**ATTRS, "number.ems_power": {"min": 0, "max": 10, "step": 0.1}}
    units = {**UNITS_W, "power_w": "kW"}
    p1, _, _ = fit_params(Params(power_w=5432.0), GW, "goodwe", MAPPED, units, attrs)
    p2, adjusted, _ = fit_params(p1, GW, "goodwe", MAPPED, units, attrs)
    assert p1.power_w == pytest.approx(5400.0) and p2 == p1 and adjusted == ()


def test_unknown_range_is_unfit_not_guessed():
    attrs = {**ATTRS, "number.export_limit": {}}
    _, _, unfit = fit_params(Params(export_limit_w=0.0), GW, "goodwe", MAPPED, UNITS_W, attrs)
    assert unfit == ("export_limit_w",)


def test_non_numeric_keys_untouched():
    p, adjusted, unfit = fit_params(Params(mode="auto", export_limit_enabled=True), GW, "goodwe",
                                    MAPPED, UNITS_W, ATTRS)
    assert p == Params(mode="auto", export_limit_enabled=True) and adjusted == () and unfit == ()


def test_control_writes_units_is_keyword_and_mandatory():
    with pytest.raises(TypeError):
        control_writes(Params(mode="auto"), GW, "goodwe", MAPPED, keys=None)  # type: ignore[call-arg]


def test_float_noise_is_not_an_adjustment():
    # 0.1 kW krok, wartość po przeliczeniu W → kW z szumem binarnym — to nie jest zmiana
    attrs = {**ATTRS, "number.ems_power": {"min": 0, "max": 10, "step": 0.1}}
    units = {**UNITS_W, "power_w": "kW"}
    for w in (100.0, 300.0, 700.0, 700.0000000001, 2300.0, 6700.0):
        p, adjusted, unfit = fit_params(Params(power_w=w), GW, "goodwe", MAPPED, units, attrs)
        assert p.power_w == w and adjusted == () and unfit == ()


def test_unit_incompatible_entity_is_not_fitted_or_written():
    writes, unmapped = control_writes(Params(export_limit_w=500.0), GW, "goodwe", MAPPED, keys=None,
                                      units={**UNITS_W, "export_limit_w": "%"})
    assert writes == [] and unmapped == ["export_limit_w"]


# --- zaokrąglanie do kroku nigdy nie luzuje limitów strażników ---------------------------

KW01 = {**ATTRS, "number.ems_power": {"min": 0, "max": 10, "step": 0.1},
        "number.export_limit": {"min": 0, "max": 10, "step": 0.1}}
UNITS_KW = {**UNITS_W, "power_w": "kW", "export_limit_w": "kW"}


def _twice(params, units=UNITS_KW, attrs=KW01, profile=GW):
    p1, adj1, unfit1 = fit_params(params, profile, "goodwe", MAPPED, units, attrs)
    p2, adj2, unfit2 = fit_params(p1, profile, "goodwe", MAPPED, units, attrs)
    assert p2 == p1 and adj2 == () and unfit2 == ()          # drugi przebieg = brak zapisu
    return p1, adj1, unfit1


def test_power_rounds_down_not_over_guard_cap():
    p, adjusted, unfit = _twice(Params(power_w=3680.0))
    assert p.power_w == pytest.approx(3600.0) and adjusted == ("power_w",) and unfit == ()


def test_export_limit_rounds_down():
    p, adjusted, _ = _twice(Params(export_limit_w=3680.0))
    assert p.export_limit_w == pytest.approx(3600.0) and adjusted == ("export_limit_w",)


def test_soc_min_rounds_up_through_inverted_dod():
    # próg 20.5 % = DoD 79.5 → DoD w dół do 79 → próg 21 % (nigdy 20 % pod rezerwą)
    p, adjusted, _ = _twice(Params(soc_min=20.5))
    assert p.soc_min == 21.0 and adjusted == ("soc_min",)


def test_soc_max_rounds_down():
    attrs = {**KW01, "number.soc_upper": {"min": 10, "max": 100, "step": 1}}
    p, adjusted, _ = _twice(Params(soc_max=80.5), attrs=attrs)
    assert p.soc_max == 80.0 and adjusted == ("soc_max",)


@pytest.mark.parametrize("params", [
    Params(power_w=3700.0), Params(power_w=700.0), Params(export_limit_w=300.0),
    Params(soc_min=20.0), Params(soc_max=80.0),
])
def test_value_exactly_on_step_is_untouched(params):
    p, adjusted, unfit = _twice(params)
    assert p == params and adjusted == () and unfit == ()


def test_fit_number_directions():
    attrs = {"min": 0, "max": 10, "step": 0.1}
    assert fit_number(3.68, attrs, rounding="down") == pytest.approx(3.6)
    assert fit_number(3.61, attrs, rounding="up") == pytest.approx(3.7)
    assert fit_number(0.7, attrs, rounding="down") == pytest.approx(0.7)   # 0.7/0.1 = 6.999…
    assert fit_number(0.7, attrs, rounding="up") == pytest.approx(0.7)
    assert fit_number(3.65, attrs) == pytest.approx(3.7)                    # domyślnie najbliższy
    with pytest.raises(ValueError):
        fit_number(1.0, attrs, rounding="sideways")


def test_step_not_dividing_range():
    attrs = {"min": 0, "max": 9.95, "step": 0.1}
    assert fit_number(9.99, attrs, rounding="down") == pytest.approx(9.9)   # w zakresie
    assert fit_number(9.93, attrs, rounding="up") is None                   # 10.0 > max → niedopasowalne
    assert fit_number(20.0, attrs, rounding="down") == pytest.approx(9.9)   # obcięte i w dół
    nonzero = {"min": 0.05, "max": 1.0, "step": 0.1}                        # siatka 0.05, 0.15, …, 0.95
    assert fit_number(1.0, nonzero, rounding="down") == pytest.approx(0.95)
    assert fit_number(0.96, nonzero, rounding="up") is None
    assert fit_number(0.0, nonzero, rounding="up") == pytest.approx(0.05)


def test_upward_rounding_beyond_entity_max_is_unfit():
    # profil bez odwrócenia DoD: encja trzyma próg wprost, więc „bezpiecznie" = w górę
    raw = json.loads((PROFILES_DIR / "goodwe-et.json").read_text(encoding="utf-8"))
    for integ in raw["ha"]["integrations"]:
        integ["entities"]["soc_min"].pop("transform", None)
    plain = profile_from_dict(raw)
    attrs = {**KW01, "number.dod": {"min": 0, "max": 99.5, "step": 1}}
    p, adjusted, unfit = fit_params(Params(soc_min=99.2), plain, "goodwe", MAPPED, UNITS_KW, attrs)
    assert unfit == ("soc_min",) and adjusted == () and p.soc_min == 99.2
    p, adjusted, _ = fit_params(Params(soc_min=20.5), plain, "goodwe", MAPPED, UNITS_KW, attrs)
    assert p.soc_min == 21.0 and adjusted == ("soc_min",)
