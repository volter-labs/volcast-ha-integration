"""Asercje wielokrotnego użytku dla profilu marki — wspólny szablon testów każdego profilu."""
import re

from custom_components.volcast.core.modbus.identity import identity_info
from custom_components.volcast.core.registers import RegisterImage, read_values

NEUTRAL_DIRECTIONS = ("neutral", "idle")


def assert_identify_matches(profile, image: RegisterImage) -> None:
    info = identity_info(profile, image)
    assert info["matched"], f"{profile.id}: obraz nie pasuje do identyfikacji profilu ({info})"


def assert_identify_rejects(profile, image: RegisterImage) -> None:
    assert not identity_info(profile, image)["matched"], f"{profile.id}: obcy obraz został rozpoznany"


def decode_reads(profile, image: RegisterImage) -> dict:
    """Wszystkie klucze `read` (sum/ref/scale/sign/word_order/fc) przez rdzeń; brak rejestru = None."""
    return read_values(profile.raw["read"], image)


def assert_intents_consistent(profile) -> None:
    raw = profile.raw
    intents = raw["intents"]
    if raw["control_model"] == "mode_setpoint":
        modes = raw["modes"]
        for name, spec in intents.items():
            assert spec["mode"] in modes, f"intent {name}: tryb {spec['mode']!r} spoza modes"
        assert modes[raw["neutral_mode"]]["direction"] in NEUTRAL_DIRECTIONS
        assert raw["baseline"]["mode"] in modes
    else:
        assert intents.get("self_consume") is not None, "brak intentu self_consume"
        assert raw["capabilities"]["time_windows"] == raw["tou"]["programs"]


def assert_ha_regexes_compile(profile) -> None:
    for integ in profile.raw["ha"]["integrations"]:
        for key, ent in integ["entities"].items():
            re.compile(ent["unique_id_regex"])  # błąd = test czerwony z nazwą klucza w śladzie
            assert ent["unique_id_regex"], f"{integ['domain']}.{key}: pusty regex"


def assert_capabilities_match_writes(profile) -> None:
    raw = profile.raw
    caps, write = raw["capabilities"], raw["write"]
    if raw["control_model"] == "time_window":
        prog = write.get("tou_program", {})
        has_power, has_soc = "power_w" in prog, "soc" in prog
        floor = ceiling = has_soc
    else:
        has_power = "power_w" in write
        floor, ceiling = "soc_min" in write, "soc_max" in write
    expected = {
        "set_power_w": has_power,
        "set_soc_floor": floor,
        "set_soc_ceiling": ceiling,
        "limit_export": "export_limit_w" in write or "export_limit_enabled" in write,
    }
    for cap, want in expected.items():
        assert caps[cap] is want, f"capability {cap}={caps[cap]} nie zgadza się z kluczami zapisu"
