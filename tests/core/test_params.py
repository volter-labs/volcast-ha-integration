from custom_components.volcast.core.params import Params, TouProgram


def test_flatten_skips_none_and_encodes_bool():
    p = Params(mode="auto", soc_min=20.0, export_limit_enabled=False)
    assert p.flatten() == {"mode": "auto", "soc_min": 20.0, "export_limit_enabled": 0.0}


def test_flatten_engine_keys():
    from custom_components.volcast.core.params import WRITE_PARAMS
    assert {"enable", "charge_power_w", "discharge_power_w"} <= set(WRITE_PARAMS)
    p = Params(mode="charge", charge_power_w=2500, discharge_power_w=0.0, enable=True)
    assert p.flatten() == {"mode": "charge", "charge_power_w": 2500.0, "discharge_power_w": 0.0,
                           "enable": 1.0}
    assert Params(enable=False).flatten() == {"enable": 0.0}
    assert Params().flatten() == {}


def test_flatten_tou_one_based():
    p = Params(tou=(TouProgram(0, 3000.0, 20.0, False), TouProgram(120, 5000.0, 90.0, True)))
    f = p.flatten()
    assert f["tou.1.start"] == 0.0 and f["tou.2.grid_charge"] == 1.0 and f["tou.2.soc"] == 90.0
