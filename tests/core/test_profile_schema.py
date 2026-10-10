import json

import pytest

from custom_components.volcast.core.profile import ProfileError, load_profile
from custom_components.volcast.core.profile_schema import validate_profile
from tests.core.profile_fixtures import ms_profile, tw_profile


def test_fixtures_are_valid():
    assert validate_profile(ms_profile()) == []
    assert validate_profile(tw_profile()) == []


def test_oversized_int_is_profile_error_not_overflow(tmp_path):
    # `json.loads` przyjmuje int dowolnej długości; walidator nie może rzucić
    # OverflowError próbując go zamienić na float (np. w math.isfinite).
    raw = ms_profile()
    raw["write_policy"]["min_interval_s"] = int("1" + "0" * 400)
    f = tmp_path / f"{raw['id']}.json"
    f.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProfileError) as ei:
        load_profile(f)
    assert any("$.write_policy.min_interval_s" in e for e in ei.value.errors)


def _errs(p):
    return "\n".join(validate_profile(p))


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p.update(extra=1), "$.extra"),
    (lambda p: p.pop("id"), "$.id"),
    (lambda p: p.update(id="GoodWe ET"), "$.id"),
    (lambda p: p.update(schema_version=2), "$.schema_version"),
    (lambda p: p.update(status="beta"), "$.status"),
    (lambda p: p["read"].update(sock={"addr": 1, "type": "u16"}), "$.read.sock"),
    (lambda p: p["read"]["soc"].update(type="u8"), "$.read.soc.type"),
    (lambda p: p["read"]["soc"].update(addr=70000), "$.read.soc.addr"),
    (lambda p: p["read"]["load_power_w"]["sum"].append({"ref": "nope"}), "$.read.load_power_w.sum[2].ref"),
    (lambda p: p["intents"]["sell"].update(mode="sell"), "$.intents.sell.mode"),
    (lambda p: p["intents"].pop("standby"), "$.intents.standby"),
    (lambda p: p.update(neutral_mode="general"), "$.neutral_mode"),
    (lambda p: p["write_policy"].update(order=["mode", "soc_min", "power_w",
                                               "export_limit_w", "export_limit_enabled"]),
     "$.write_policy.order"),
    (lambda p: p["write_policy"]["order"].remove("soc_min"), "$.write_policy.order"),
    (lambda p: p["ha"]["integrations"][0]["entities"]["soc"].update(unique_id_regex="(["),
     "unique_id_regex"),
    (lambda p: p["ha"]["integrations"][0]["entities"].update(foo={"domain": "sensor",
                                                                  "unique_id_regex": "x"}),
     "entities.foo"),
    (lambda p: p["limits"].update(rated_power_register="nope"), "$.limits.rated_power_register"),
    (lambda p: p["capabilities"].pop("standby"), "$.capabilities.standby"),
    (lambda p: p["capabilities"].update(time_windows=6), "$.capabilities.time_windows"),
    (lambda p: p["write"]["power_w"].update(encode="percent"), "$.write.power_w.encode"),
    # zły typ zamiast tekstu → błąd ze ścieżką, nigdy TypeError
    (lambda p: p["limits"].update(rated_power_register=["rated_power_w"]), "$.limits.rated_power_register"),
    (lambda p: p["intents"]["sell"].update(mode=["sell_power"]), "$.intents.sell.mode"),
    (lambda p: p.update(neutral_mode=["auto"]), "$.neutral_mode"),
    (lambda p: p["baseline"].update(mode=["auto"]), "$.baseline.mode"),
    (lambda p: p["read"]["load_power_w"]["sum"].append({"ref": ["pv_power_w"]}),
     "$.read.load_power_w.sum[2].ref"),
    (lambda p: p["write_policy"].update(order=[1, 2, 3, 4, 5]), "$.write_policy.order"),
    (lambda p: p["identify"].update(registers=["x"]), "$.identify.registers"),
    (lambda p: p["modes"]["auto"].update(value=[1]), "$.modes.auto.value"),
    (lambda p: p["read"]["pv_power_w"].update(scale=0.1), "$.read.pv_power_w.scale"),
    # liczby niekończone (NaN/Infinity) nie mogą przejść walidacji
    (lambda p: p["limits"]["battery_temp_c"].update(min=float("nan")), "$.limits.battery_temp_c.min"),
    (lambda p: p["write_policy"].update(min_interval_s=float("inf")), "$.write_policy.min_interval_s"),
    # mode_setpoint wymaga co najmniej 1 zmiany kierunku na godzinę
    (lambda p: p["write_policy"].update(max_direction_changes_per_hour=0),
     "$.write_policy.max_direction_changes_per_hour"),
    # "slot" (moc gwarantowana przez slot) na intencji, która tej gwarancji nie ma,
    # crashowałoby silnik przy float(None) — walidator musi to zatrzymać na wejściu
    (lambda p: p["intents"]["standby"].update(power="slot"), "$.intents.standby.power"),
    (lambda p: p["intents"]["self_consume"].update(power="slot"), "$.intents.self_consume.power"),
    (lambda p: p["intents"]["charge_pv"].update(power="slot"), "$.intents.charge_pv.power"),
    # domena encji HA musi pasować do klucza — inaczej zapis trafia do złej usługi
    (lambda p: p["ha"]["integrations"][0]["entities"].update(
        mode={"domain": "number", "unique_id_regex": "x"}), "entities.mode.domain"),
    (lambda p: p["ha"]["integrations"][0]["entities"].update(
        power_w={"domain": "select", "unique_id_regex": "x"}), "entities.power_w.domain"),
    (lambda p: p["ha"]["integrations"][0]["entities"]["soc_min"].update(domain="switch"),
     "entities.soc_min.domain"),
    (lambda p: p["ha"]["integrations"][0]["entities"].update(
        export_limit_enabled={"domain": "number", "unique_id_regex": "x"}),
     "entities.export_limit_enabled.domain"),
    (lambda p: p["ha"]["integrations"][0]["entities"]["soc"].update(domain="number"),
     "entities.soc.domain"),
    (lambda p: p["ha"]["integrations"][0]["entities"].update(
        tou_1_start={"domain": "number", "unique_id_regex": "x"}), "entities.tou_1_start.domain"),
    (lambda p: p["ha"]["integrations"][0]["entities"].update(
        tou_1_grid_charge={"domain": "select", "unique_id_regex": "x"}),
     "entities.tou_1_grid_charge.domain"),
    (lambda p: p["ha"]["integrations"][0]["entities"].update(
        tou_1_soc={"domain": "time", "unique_id_regex": "x"}), "entities.tou_1_soc.domain"),
])
def test_mode_setpoint_errors(mutate, needle):
    p = ms_profile()
    mutate(p)
    assert needle in _errs(p)


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p["write"]["tou_program"].update(count=5), "$.write.tou_program.count"),
    (lambda p: p["intents"].update(self_consume=None), "$.intents.self_consume"),
    (lambda p: p["intents"]["charge_grid"].update(soc="full"), "$.intents.charge_grid.soc"),
    (lambda p: p["tou"].update(time_step_min=7), "$.tou.time_step_min"),
    (lambda p: p["tou"].update(field_order=["soc", "soc", "power_w", "start"]), "$.tou.field_order"),
    (lambda p: p.pop("tou"), "$.tou"),
    (lambda p: p["write"]["tou_program"]["grid_charge"].pop("bit"), "grid_charge.bit"),
    # zły typ zamiast listy → błąd ze ścieżką, nigdy TypeError
    (lambda p: p["tou"].update(field_order="soc,power_w,grid_charge,start"), "$.tou.field_order"),
    # liczby niekończone (NaN/Infinity) nie mogą przejść walidacji
    (lambda p: p["tou"].update(soc_tolerance_pp=float("nan")), "$.tou.soc_tolerance_pp"),
])
def test_time_window_errors(mutate, needle):
    p = tw_profile()
    mutate(p)
    assert needle in _errs(p)


def test_time_window_ignores_zero_direction_changes():
    # max_direction_changes_per_hour == 0 nie jest błędem dla time_window
    p = tw_profile()
    assert p["write_policy"]["max_direction_changes_per_hour"] == 0
    assert validate_profile(p) == []


def test_not_a_dict():
    assert validate_profile([]) == ["$: oczekiwano obiektu"]


def test_baseline_export_flag_optional_and_negate_transform_allowed():
    p = ms_profile()
    p["baseline"] = {"mode": "auto"}
    p["ha"]["integrations"][0]["entities"]["grid_power_w"] = {
        "domain": "sensor", "unique_id_regex": "^x-", "transform": "negate"}
    assert validate_profile(p) == []


def test_baseline_export_flag_when_present_must_be_bool():
    p = ms_profile()
    p["baseline"]["export_limit_enabled"] = "yes"
    assert "$.baseline.export_limit_enabled" in _errs(p)


@pytest.mark.parametrize("mode", ["discharge_battery", "sell_power", "charge_battery"])
def test_neutral_mode_must_not_force_charge_or_discharge(mode):
    # I-1 podmienia tryb rozładowania na neutralny — neutralny wymuszający ruch
    # baterii zamieniłby blokadę w „rozładuj na starej nastawie".
    p = ms_profile()
    p["neutral_mode"] = mode
    assert "$.neutral_mode" in _errs(p)


@pytest.mark.parametrize("mode", ["auto", "battery_standby"])
def test_neutral_mode_neutral_or_idle_is_valid(mode):
    p = ms_profile()
    p["neutral_mode"] = mode
    assert validate_profile(p) == []


def test_ref_cycle_two_keys_is_rejected():
    p = ms_profile()
    p["read"]["pv_power_w"]["sum"].append({"ref": "load_power_w"})
    errs = _errs(p)
    assert "cykl odwołań" in errs
    assert "$.read.load_power_w.sum" in errs or "$.read.pv_power_w.sum" in errs


def test_ref_cycle_longer_chain_is_rejected():
    p = ms_profile()
    p["read"]["battery_power_w"] = {"sum": [{"ref": "grid_power_w"}]}
    p["read"]["grid_power_w"] = {"sum": [{"ref": "load_power_w"}]}
    p["read"]["pv_power_w"]["sum"].append({"ref": "battery_power_w"})
    errs = validate_profile(p)
    cyc = [e for e in errs if "cykl odwołań" in e]
    assert len(cyc) == 1, errs
    for k in ("pv_power_w", "battery_power_w", "grid_power_w", "load_power_w"):
        assert k in cyc[0]


def test_ref_chain_without_cycle_is_valid():
    p = ms_profile()
    p["read"]["grid_power_w"] = {"sum": [{"ref": "load_power_w"}]}
    assert validate_profile(p) == []


@pytest.mark.parametrize("field,value", [
    ("min_interval_s", -5), ("min_interval_s", -0.1),
    ("max_state_age_s", 0), ("max_state_age_s", -1), ("max_state_age_s", 0.0),
])
def test_write_policy_lower_bounds(field, value):
    p = ms_profile()
    p["write_policy"][field] = value
    assert f"$.write_policy.{field}" in _errs(p)


@pytest.mark.parametrize("field,value", [("min_interval_s", 0), ("max_state_age_s", 0.5)])
def test_write_policy_boundary_values_accepted(field, value):
    p = ms_profile()
    p["write_policy"][field] = value
    assert validate_profile(p) == []


# ── sekcja dostępu bezpośredniego (`modbus`), budżet zapisów, włącznik TOU ──


def test_builtin_profiles_validate():
    from custom_components.volcast.core.profile import PROFILES_DIR, builtin_ids
    for pid in builtin_ids():
        raw = json.loads((PROFILES_DIR / f"{pid}.json").read_text(encoding="utf-8"))
        assert validate_profile(raw) == [], pid


def test_modbus_section_required():
    for p in (ms_profile(), tw_profile()):
        p.pop("modbus")
        assert "$.modbus: brak wymaganego pola" in _errs(p)


def test_modbus_status_enum():
    p = ms_profile()
    p["modbus"]["status"] = "tested"
    assert "$.modbus.status" in _errs(p)
    for ok in ("draft", "verified"):
        p["modbus"]["status"] = ok
        assert validate_profile(p) == []


@pytest.mark.parametrize("bad", [3, 5, 15, 17, True, "6", None])
def test_write_function_enum(bad):
    p = ms_profile()
    p["modbus"]["write_function"] = bad
    assert "$.modbus.write_function" in _errs(p)


@pytest.mark.parametrize("bad", [0, 126, True, "10"])
def test_max_read_registers_bounds(bad):
    p = ms_profile()
    p["modbus"]["max_read_registers"] = bad
    assert "$.modbus.max_read_registers" in _errs(p)


def test_transport_options_subset_of_transports():
    p = ms_profile()                                   # transports: tylko goodwe_udp
    p["modbus"]["transport_options"]["modbus_tcp"] = {"port": 502, "timeout_ms": 2000, "gap_ms": 50}
    assert "$.modbus.transport_options.modbus_tcp" in _errs(p)
    p = ms_profile()
    p["modbus"]["transport_options"]["serial"] = {"port": 1, "timeout_ms": 2000, "gap_ms": 50}
    assert "$.modbus.transport_options.serial" in _errs(p)


@pytest.mark.parametrize("field,bad", [
    ("port", 0), ("port", 65536), ("port", "502"), ("port", True),
    ("timeout_ms", 199), ("timeout_ms", 10001), ("timeout_ms", 2000.5),
    ("gap_ms", -1), ("gap_ms", 2001),
])
def test_transport_options_ranges(field, bad):
    p = ms_profile()
    p["modbus"]["transport_options"]["goodwe_udp"][field] = bad
    assert f"$.modbus.transport_options.goodwe_udp.{field}" in _errs(p)


def test_transport_options_shape():
    p = ms_profile()
    p["modbus"]["transport_options"]["goodwe_udp"]["host"] = "x"
    assert "$.modbus.transport_options.goodwe_udp.host: nieznane pole" in _errs(p)
    p = ms_profile()
    p["modbus"]["transport_options"]["goodwe_udp"].pop("gap_ms")
    assert "$.modbus.transport_options.goodwe_udp.gap_ms: brak wymaganego pola" in _errs(p)
    p = ms_profile()
    p["modbus"]["transport_options"] = []
    assert "$.modbus.transport_options" in _errs(p)


@pytest.mark.parametrize("reads,needle", [
    ([], "$.modbus.identify_reads"),
    ("35000", "$.modbus.identify_reads"),
    ([{"addr": -1, "count": 1}], "$.modbus.identify_reads[0].addr"),
    ([{"addr": 65536, "count": 1}], "$.modbus.identify_reads[0].addr"),
    ([{"addr": 0, "count": 0}], "$.modbus.identify_reads[0].count"),
    ([{"addr": 0, "count": 126}], "$.modbus.identify_reads[0].count"),
    ([{"addr": 65500, "count": 37}], "$.modbus.identify_reads[0]"),
    ([{"addr": 1, "count": 1, "type": "u16"}], "$.modbus.identify_reads[0].type"),
    ([{"addr": 1}], "$.modbus.identify_reads[0].count"),
])
def test_identify_reads_bounds(reads, needle):
    p = ms_profile()
    p["modbus"]["identify_reads"] = reads
    assert needle in _errs(p)


def test_identify_reads_upper_edge_is_valid():
    p = ms_profile()
    p["modbus"]["identify_reads"] = [{"addr": 65535, "count": 1}, {"addr": 65411, "count": 125}]
    assert validate_profile(p) == []


def test_probe_keys_subset_of_write():
    p = ms_profile()                                   # bez zapisu soc_max
    p["modbus"]["probe_keys"] = ["mode", "soc_max"]
    assert "$.modbus.probe_keys" in _errs(p)
    p = ms_profile()
    p["modbus"]["probe_keys"] = ["mode", "mode"]
    assert "$.modbus.probe_keys" in _errs(p)
    p = ms_profile()
    p["modbus"]["probe_keys"] = ["tou"]                # „tou" tylko przy tou_program
    assert "$.modbus.probe_keys" in _errs(p)
    p = ms_profile()
    p["modbus"]["probe_keys"] = "mode"
    assert "$.modbus.probe_keys" in _errs(p)
    p = tw_profile()
    p["modbus"]["probe_keys"] = ["tou"]
    assert validate_profile(p) == []


def test_echo_only_optional_subset_of_write():
    p = ms_profile()
    p["modbus"]["echo_only"] = ["mode"]
    assert validate_profile(p) == []
    for bad in (["soc_max"], ["mode", "mode"], "mode", ["tou"]):
        p = ms_profile()
        p["modbus"]["echo_only"] = bad
        assert "$.modbus.echo_only" in _errs(p)
    p = tw_profile()
    p["modbus"]["echo_only"] = ["tou"]
    assert validate_profile(p) == []


def test_echo_only_parsed_into_modbus_spec():
    from custom_components.volcast.core.profile import profile_from_dict
    raw = ms_profile()
    assert profile_from_dict(raw).modbus.echo_only == ()
    raw["modbus"]["echo_only"] = ["mode"]
    assert profile_from_dict(raw).modbus.echo_only == ("mode",)


def test_modbus_unknown_field_and_status_note():
    p = ms_profile()
    p["modbus"]["host"] = "x"
    assert "$.modbus.host: nieznane pole" in _errs(p)
    p = ms_profile()
    p["modbus"]["status_note"] = ""
    assert "$.modbus.status_note" in _errs(p)
    p["modbus"]["status_note"] = "note"
    assert validate_profile(p) == []


def test_nvm_budget_total_at_least_per_key():
    p = ms_profile()
    p["write_policy"]["nvm_budget"] = {"window_h": 24, "per_key": 144, "total": 100}
    assert "$.write_policy.nvm_budget.total" in _errs(p)
    p["write_policy"]["nvm_budget"] = {"window_h": 24, "per_key": 144, "total": 144}
    assert validate_profile(p) == []


@pytest.mark.parametrize("budget,needle", [
    ({"window_h": 0, "per_key": 1, "total": 1}, "$.write_policy.nvm_budget.window_h"),
    ({"window_h": 169, "per_key": 1, "total": 1}, "$.write_policy.nvm_budget.window_h"),
    ({"window_h": 24, "per_key": 0, "total": 1}, "$.write_policy.nvm_budget.per_key"),
    ({"window_h": 24, "per_key": 1}, "$.write_policy.nvm_budget.total"),
    ({"window_h": 24, "per_key": 1, "total": 1, "x": 1}, "$.write_policy.nvm_budget.x"),
    ([24, 1, 1], "$.write_policy.nvm_budget"),
])
def test_nvm_budget_shape(budget, needle):
    p = ms_profile()
    p["write_policy"]["nvm_budget"] = budget
    assert needle in _errs(p)


def test_nvm_budget_is_optional():
    p = ms_profile()
    assert "nvm_budget" not in p["write_policy"] and validate_profile(p) == []


def _tw_with_enable(**enable):
    p = tw_profile()
    p["write"]["tou_enable"] = {"addr": 146, "enable_bit": 0, "day_mask": 254, **enable}
    return p


def test_tou_enable_valid_with_tou_program():
    p = _tw_with_enable()
    p["read"]["tou_enabled"] = {"addr": 146, "type": "u16"}
    assert validate_profile(p) == []


def test_tou_enable_requires_tou_program():
    p = ms_profile()
    p["write"]["tou_enable"] = {"addr": 146, "enable_bit": 0, "day_mask": 254}
    assert "$.write.tou_enable" in _errs(p)


def test_tou_enable_day_mask_excludes_enable_bit():
    assert "$.write.tou_enable.day_mask" in _errs(_tw_with_enable(day_mask=255))
    assert "$.write.tou_enable.day_mask" in _errs(_tw_with_enable(enable_bit=3, day_mask=0x08))
    assert validate_profile(_tw_with_enable(enable_bit=15, day_mask=0x7FFF)) == []


@pytest.mark.parametrize("field,bad", [("addr", 70000), ("enable_bit", 16), ("day_mask", 65536),
                                       ("day_mask", -1), ("enable_bit", True)])
def test_tou_enable_ranges(field, bad):
    assert f"$.write.tou_enable.{field}" in _errs(_tw_with_enable(**{field: bad}))


def test_tou_enable_is_not_a_separate_write_order_entry():
    # Włącznik należy do sekwencji TOU — kolejność zapisów zostaje ["tou"].
    p = _tw_with_enable()
    assert p["write_policy"]["order"] == ["tou"] and validate_profile(p) == []


def test_read_tou_enabled_needs_tou_enable_at_same_address():
    p = tw_profile()
    p["read"]["tou_enabled"] = {"addr": 146, "type": "u16"}
    assert "$.read.tou_enabled" in _errs(p)
    p = _tw_with_enable()
    p["read"]["tou_enabled"] = {"addr": 147, "type": "u16"}
    assert "$.read.tou_enabled" in _errs(p)


# ── bloki odczytu znane jako dozwolone (`modbus.verify_blocks`) ──


def test_verify_blocks_optional_and_parsed():
    from custom_components.volcast.core.profile import profile_from_dict
    p = ms_profile()
    assert "verify_blocks" not in p["modbus"] and profile_from_dict(p).modbus.verify_blocks == ()
    p["modbus"]["verify_blocks"] = [{"addr": 47509, "count": 4}, {"addr": 45353, "count": 4}]
    assert validate_profile(p) == []
    assert profile_from_dict(p).modbus.verify_blocks == ((47509, 4), (45353, 4))


@pytest.mark.parametrize("bad", [
    "x", [], [{"addr": 47509}], [{"addr": 47509, "count": 0}], [{"addr": 47509, "count": 126}],
    [{"addr": 70000, "count": 1}], [{"addr": 65535, "count": 2}],
    [{"addr": 47509, "count": 4}, {"addr": 47509, "count": 4}],
    [{"addr": 1000, "count": 4}],                       # nie obejmuje żadnego rejestru zapisu
])
def test_verify_blocks_rejected(bad):
    p = ms_profile()
    p["modbus"]["verify_blocks"] = bad
    assert "$.modbus.verify_blocks" in _errs(p)


def test_verify_blocks_within_max_read_registers():
    p = ms_profile()
    p["modbus"]["max_read_registers"] = 2
    p["modbus"]["verify_blocks"] = [{"addr": 47509, "count": 4}]
    assert "$.modbus.verify_blocks" in _errs(p)


# ── odczekanie przed ponownym odczytem zwrotnym (`write_policy.readback_settle_s`) ──


@pytest.mark.parametrize("value", [0, 0.5, 1.5, 10])
def test_readback_settle_optional_and_in_range(value):
    p = ms_profile()
    assert "readback_settle_s" not in p["write_policy"] and validate_profile(p) == []
    p["write_policy"]["readback_settle_s"] = value
    assert validate_profile(p) == []


@pytest.mark.parametrize("value", [-0.1, 10.5, float("inf"), float("nan"), True, "1.5", None])
def test_readback_settle_out_of_range_rejected(value):
    p = ms_profile()
    p["write_policy"]["readback_settle_s"] = value
    assert "$.write_policy.readback_settle_s" in _errs(p)


# ── kod funkcji odczytu (`fc`: 3 holding, 4 input) ──


def test_read_function_defaults_and_input_registers_are_valid():
    p = ms_profile()
    p["read"]["soc"]["fc"] = 4
    p["read"]["active_power_w"]["fc"] = 3
    p["read"]["pv_power_w"]["sum"][0]["fc"] = 4
    p["identify"]["model_register"]["fc"] = 4
    p["identify"]["registers"]["rated_power_w"]["fc"] = 4
    p["modbus"]["identify_reads"] = [{"addr": 35000, "count": 33, "fc": 4}, {"addr": 35001, "count": 1, "fc": 3}]
    assert validate_profile(p) == []


@pytest.mark.parametrize("fc", [5, 0, 6, 16, "4", True, 4.0, None])
def test_read_function_outside_3_and_4_is_rejected(fc):
    for mutate, needle in (
            (lambda p: p["read"]["soc"].__setitem__("fc", fc), "$.read.soc.fc"),
            (lambda p: p["read"]["pv_power_w"]["sum"][0].__setitem__("fc", fc), "$.read.pv_power_w.sum[0].fc"),
            (lambda p: p["identify"]["model_register"].__setitem__("fc", fc), "$.identify.model_register.fc"),
            (lambda p: p["modbus"]["identify_reads"][0].__setitem__("fc", fc), "$.modbus.identify_reads[0].fc")):
        p = ms_profile()
        mutate(p)
        assert f"{needle}: tylko 3 (holding) albo 4 (input)" in _errs(p), needle


def test_write_registers_take_no_read_function():
    # Zapis i odczyt zwrotny zawsze w rejestrach holding — `fc` w specyfikacji zapisu to błąd profilu.
    p = ms_profile()
    p["write"]["mode"]["fc"] = 4
    assert "$.write.mode.fc: nieznane pole" in _errs(p)


def test_ha_integration_model_regex_is_optional_and_validated():
    p = ms_profile()
    p["ha"]["integrations"][0]["model_regex"] = ["(?i)goodwe", "^GW"]
    assert validate_profile(p) == []
    for bad, where in (("(?i)goodwe", "model_regex"), ([], "model_regex"), (["(["], "model_regex[0]"),
                       ([3], "model_regex")):
        p["ha"]["integrations"][0]["model_regex"] = bad
        assert f"$.ha.integrations[0].{where}" in _errs(p), bad


# ── opcjonalny blok `verification` (parametry drabiny weryfikacji) ─────────


@pytest.mark.parametrize("block", [
    {"trial_hours": 24, "window_minutes": 15, "window_power_w": 500},
    {"trial_hours": 1, "window_minutes": 5, "window_power_w": 100},
    {"trial_hours": 72, "window_minutes": 60, "window_power_w": 3000},
    {"trial_hours": 12},
])
def test_verification_block_accepted(block):
    p = ms_profile()
    p["verification"] = block
    assert validate_profile(p) == []


@pytest.mark.parametrize("block,needle", [
    ({"trial_hours": 0}, "$.verification.trial_hours"),
    ({"trial_hours": 73}, "$.verification.trial_hours"),
    ({"trial_hours": 24.5}, "$.verification.trial_hours"),
    ({"trial_hours": True}, "$.verification.trial_hours"),
    ({"window_minutes": 4}, "$.verification.window_minutes"),
    ({"window_minutes": 61}, "$.verification.window_minutes"),
    ({"window_power_w": 99}, "$.verification.window_power_w"),
    ({"window_power_w": 3001}, "$.verification.window_power_w"),
    ({"window_power_w": "500"}, "$.verification.window_power_w"),
    ({"trial_hours": 24, "skip": True}, "$.verification.skip"),
    ({}, "$.verification"),
    ([], "$.verification"),
])
def test_verification_block_rejected(block, needle):
    p = ms_profile()
    p["verification"] = block
    assert needle in _errs(p)


def test_verification_block_is_optional():
    p = ms_profile()
    assert "verification" not in p and validate_profile(p) == []
