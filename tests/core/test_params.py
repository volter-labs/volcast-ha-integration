from custom_components.volcast.core.params import Params, TouProgram


def test_flatten_skips_none_and_encodes_bool():
    p = Params(mode="auto", soc_min=20.0, export_limit_enabled=False)
    assert p.flatten() == {"mode": "auto", "soc_min": 20.0, "export_limit_enabled": 0.0}


def test_flatten_tou_one_based():
    p = Params(tou=(TouProgram(0, 3000.0, 20.0, False), TouProgram(120, 5000.0, 90.0, True)))
    f = p.flatten()
    assert f["tou.1.start"] == 0.0 and f["tou.2.grid_charge"] == 1.0 and f["tou.2.soc"] == 90.0
