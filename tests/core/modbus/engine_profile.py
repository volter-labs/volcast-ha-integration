"""Profil testowy z konstrukcjami silnika zapisu: przełącznik zdalnego sterowania, dwie nastawy mocy
32-bitowe wg kierunku (jedna ze skalą i odwrotną kolejnością słów), próg SoC ze skalą i rejestry na
adresie urządzenia innym niż adres połączenia."""
from custom_components.volcast.core.profile import profile_from_dict
from tests.core.profile_fixtures import ms_profile

PLANT = 247                     # adres urządzenia rejestrów sterowania (inny niż połączenia)


def directional_raw() -> dict:
    p = ms_profile()
    p["unit_id"] = 1
    w = p["write"]
    w.pop("power_w")
    w["mode"] = {"addr": 40031, "type": "u16", "encode": "mode", "unit_id": PLANT}
    w["enable"] = {"addr": 40029, "type": "u16", "encode": "on_off", "on": 1, "off": 0, "unit_id": PLANT}
    w["charge_power_w"] = {"addr": 40032, "type": "u32", "encode": "watts", "unit_id": PLANT}
    w["discharge_power_w"] = {"addr": 40034, "type": "u32", "encode": "watts", "scale": 10,
                              "word_order": "lo_hi", "unit_id": PLANT}
    w["soc_min"] = {"addr": 40046, "type": "u16", "encode": "percent", "scale": 0.1, "unit_id": PLANT}
    p["read"]["mode_value"] = {"addr": 40031, "type": "u16", "unit_id": PLANT}
    p["read"]["enable_state"] = {"addr": 40029, "type": "u16", "unit_id": PLANT}
    p["write_policy"].update(order=["soc_min", "charge_power_w", "discharge_power_w", "export_limit_w",
                                    "export_limit_enabled", "enable", "mode"], power_by_direction=True)
    p["modbus"]["probe_keys"] = ["mode", "charge_power_w", "discharge_power_w"]
    p["modbus"]["write_function"] = 6
    p["ha"]["integrations"][0]["entities"].update(
        enable={"domain": "switch", "unique_id_regex": "^x-remote_ems-"},
        charge_power_w={"domain": "number", "unique_id_regex": "^x-charge_limit-"},
        discharge_power_w={"domain": "number", "unique_id_regex": "^x-discharge_limit-"})
    return p


def directional_profile(mutate=None):
    raw = directional_raw()
    if mutate is not None:
        mutate(raw)
    return profile_from_dict(raw, origin="package")
