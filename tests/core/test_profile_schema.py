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
    assert profile_from_dict(raw, origin="package").modbus.echo_only == ()
    raw["modbus"]["echo_only"] = ["mode"]
    assert profile_from_dict(raw, origin="package").modbus.echo_only == ("mode",)


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
    assert "verify_blocks" not in p["modbus"] and profile_from_dict(p, origin="package").modbus.verify_blocks == ()
    p["modbus"]["verify_blocks"] = [{"addr": 47509, "count": 4}, {"addr": 45353, "count": 4}]
    assert validate_profile(p) == []
    assert profile_from_dict(p, origin="package").modbus.verify_blocks == ((47509, 4), (45353, 4))


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


# ── liczniki energii, limity BMS i flaga `verify` ──

_NOTE = "candidate from the vendor map — unverified"


def _with_counters(status="verified"):
    p = ms_profile()
    p["status"] = status
    p["read"].update({
        "battery_voltage_v": {"addr": 35180, "type": "u16", "scale": 0.1},
        "load_energy_total_kwh": {"addr": 35203, "type": "u32", "scale": 0.1, "verify": True, "note": _NOTE},
        "load_energy_day_kwh": {"addr": 35205, "type": "u16", "scale": 0.1, "verify": True, "note": _NOTE},
        "battery_charge_total_kwh": {"addr": 35206, "type": "u32", "scale": 0.1, "verify": True, "note": _NOTE},
        "battery_discharge_total_kwh": {"addr": 35209, "type": "u32", "scale": 0.1, "verify": False},
        "bms_charge_current_max_a": {"addr": 37004, "type": "u16", "verify": True, "note": _NOTE},
        "bms_discharge_current_max_a": {"addr": 37005, "type": "u16", "verify": True, "note": _NOTE},
        "battery_capacity_kwh": {"addr": 37050, "type": "u16", "scale": 0.1, "verify": True, "note": _NOTE},
    })
    p["limits"].update(max_charge_register="bms_charge_current_max_a",
                       max_discharge_register="bms_discharge_current_max_a",
                       capacity_register="battery_capacity_kwh")
    return p


def test_schema_accepts_counter_keys_and_verify_flag():
    from custom_components.volcast.core.control.readings import EXTRA_KEYS
    from custom_components.volcast.core.profile_schema import READ_KEYS
    assert set(EXTRA_KEYS) <= READ_KEYS                     # jedno źródło listy kluczy
    assert validate_profile(_with_counters()) == []
    assert validate_profile(_with_counters("draft")) == []


def test_limits_registers_are_optional():
    p = ms_profile()
    assert "max_charge_register" not in p["limits"] and validate_profile(p) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p["read"]["load_energy_total_kwh"].update(verify="yes"), "$.read.load_energy_total_kwh.verify"),
    (lambda p: p["read"]["load_energy_total_kwh"].update(verify=1), "$.read.load_energy_total_kwh.verify"),
    (lambda p: p["read"]["load_energy_total_kwh"].update(note=""), "$.read.load_energy_total_kwh.note"),
    (lambda p: p["read"]["load_energy_total_kwh"].update(note=["x"]), "$.read.load_energy_total_kwh.note"),
    # rejestr do weryfikacji nigdy nie wpływa na zapis: flaga tylko przy licznikach i limitach
    (lambda p: p["read"]["soc"].update(verify=True), "$.read.soc.verify"),
    (lambda p: p["read"]["soc"].update(verify=False), "$.read.soc.verify"),
    (lambda p: p["read"]["pv_power_w"]["sum"][0].update(verify=True), "$.read.pv_power_w.sum[0].verify"),
    (lambda p: p["identify"]["registers"]["rated_power_w"].update(verify=True),
     "$.identify.registers.rated_power_w.verify"),
    (lambda p: p["write"]["mode"].update(verify=True), "$.write.mode.verify"),
    (lambda p: p["limits"].update(max_charge_register="nope"), "$.limits.max_charge_register"),
    (lambda p: p["limits"].update(max_charge_register="soc"), "$.limits.max_charge_register"),
    (lambda p: p["limits"].update(max_discharge_register="bms_charge_current_max_a"),
     "$.limits.max_discharge_register"),
    (lambda p: p["limits"].update(capacity_register=7), "$.limits.capacity_register"),
    (lambda p: p["read"].pop("bms_charge_current_max_a"), "$.limits.max_charge_register"),
    (lambda p: p["read"].pop("battery_voltage_v"), "$.limits.max_charge_register"),
    (lambda p: p["limits"].update(max_export_register="x"), "$.limits.max_export_register"),
])
def test_counter_and_limit_fields_rejected(mutate, needle):
    p = _with_counters()
    mutate(p)
    assert needle in _errs(p)


def test_verify_flag_warns_only_in_verified_profile():
    from custom_components.volcast.core.profile_schema import profile_warnings
    warned = profile_warnings(_with_counters())
    keys = {"load_energy_total_kwh", "load_energy_day_kwh", "battery_charge_total_kwh",
            "bms_charge_current_max_a", "bms_discharge_current_max_a", "battery_capacity_kwh"}
    assert len(warned) == len(keys)
    for key in keys:
        assert any(w.startswith(f"$.read.{key}.verify:") for w in warned), key
    assert not any("battery_discharge_total_kwh" in w for w in warned)   # verify: false
    assert profile_warnings(_with_counters("draft")) == []               # cały profil i tak testowy
    assert profile_warnings(ms_profile()) == []
    assert profile_warnings("not a profile") == []                        # nigdy wyjątek


# ── opcjonalny blok `work_mode` (tylko odczyt trybu pracy falownika) ─────────

def _wm():
    return {"read": {"addr": 35000, "type": "u16"}, "neutral_values": [0],
            "ha": {"domain": "select", "unique_id_regex": "work_mode$", "neutral_options": ["general"]},
            "poll_s": 300}


def test_work_mode_block_accepted_and_accessor():
    from custom_components.volcast.core.profile import profile_from_dict
    p = ms_profile()
    p["work_mode"] = _wm()
    assert validate_profile(p) == []
    wm = profile_from_dict(p, origin="package").work_mode
    assert wm.read_addr == 35000 and wm.neutral_values == (0,) and wm.poll_s == 300
    assert wm.ha_unique_id_regex == "work_mode$" and wm.ha_neutral_options == ("general",)


def test_work_mode_absent_accessor_none():
    from custom_components.volcast.core.profile import profile_from_dict
    assert profile_from_dict(ms_profile(), origin="package").work_mode is None


def _wm_mut(f):
    p = ms_profile()
    p["work_mode"] = _wm()
    f(p["work_mode"])
    return p


@pytest.mark.parametrize("mutate,needle", [
    (lambda w: w.pop("neutral_values"), "$.work_mode.neutral_values"),
    (lambda w: w.update(neutral_values=[]), "$.work_mode.neutral_values"),
    (lambda w: w.update(neutral_values=["0"]), "$.work_mode.neutral_values"),
    (lambda w: w["read"].update(type="u8"), "$.work_mode.read.type"),
    (lambda w: w["read"].update(addr=70000), "$.work_mode.read.addr"),
    (lambda w: w.pop("read"), "$.work_mode.read"),
    (lambda w: w.update(poll_s=299), "$.work_mode.poll_s"),
    (lambda w: w.update(poll_s="300"), "$.work_mode.poll_s"),
    (lambda w: w["ha"].update(neutral_options=[]), "$.work_mode.ha.neutral_options"),
    (lambda w: w["ha"].update(domain="sensor"), "$.work_mode.ha.domain"),
    (lambda w: w["ha"].update(unique_id_regex="("), "$.work_mode.ha.unique_id_regex"),
    (lambda w: w.update(extra=1), "$.work_mode.extra"),
])
def test_work_mode_block_rejected(mutate, needle):
    assert needle in _errs(_wm_mut(mutate))


# --- wymagana wersja silnika (`min_engine`) ----------------------------------------------------------

@pytest.mark.parametrize("engine", [1, 2, 3, 99])
def test_min_engine_optional_positive_int(engine):
    for p in (ms_profile(), tw_profile()):
        p["min_engine"] = engine
        assert validate_profile(p) == []


@pytest.mark.parametrize("engine", [0, -1, True, False, "2", 2.0, 1.5, None, [2]])
def test_min_engine_must_be_an_int_of_at_least_one(engine):
    p = ms_profile()
    p["min_engine"] = engine
    assert "$.min_engine" in _errs(p)


def test_unknown_top_level_field_still_an_error_next_to_min_engine():
    p = ms_profile()
    p.update(min_engine=1, min_engines=1)
    assert "$.min_engines: nieznane pole" in _errs(p)


def test_engine_version_of_this_release():
    from custom_components.volcast.core import profile_schema
    assert profile_schema.PROFILE_ENGINE == 2


def test_regex_issues_report_every_unsafe_pattern_with_its_path():
    from custom_components.volcast.core.profile_schema import regex_issues
    p = ms_profile()
    assert regex_issues(p) == []
    p["future"] = {"label_regex": "^(a+)+$"}
    assert [path for path, _ in regex_issues(p)] == ["$.future.label_regex"]


# --- konstrukcje silnika: przełącznik zdalnego sterowania, moc wg kierunku, zapis 32-bitowy,
# --- adres urządzenia per rejestr, stan wyjściowy i powrót bez planu ---------------------------------

_ORDER_V2 = ["soc_min", "charge_power_w", "discharge_power_w", "export_limit_w", "export_limit_enabled",
             "enable", "mode"]


def _engine_v2() -> dict:
    """Profil mode_setpoint z WSZYSTKIMI nowymi konstrukcjami naraz (wzór marki z przełącznikiem
    zdalnego sterowania i dwiema nastawami mocy 32-bitowymi na innym adresie urządzenia)."""
    p = ms_profile()
    w = p["write"]
    w.pop("power_w")
    w["charge_power_w"] = {"addr": 40032, "type": "u32", "encode": "watts", "scale": 1,
                           "word_order": "hi_lo", "unit_id": 247}
    w["discharge_power_w"] = {"addr": 40034, "type": "u32", "encode": "watts", "word_order": "lo_hi"}
    w["enable"] = {"addr": 40029, "type": "u16", "encode": "on_off", "on": 1, "off": 0}
    w["soc_min"]["scale"] = 0.1
    w["soc_min"]["unit_id"] = 1
    p["read"]["enable_state"] = {"addr": 40029, "type": "u16"}
    p["read"]["charge_power_w"] = {"addr": 40032, "type": "u32", "unit_id": 247}
    p["read"]["discharge_power_w"] = {"addr": 40034, "type": "u32", "word_order": "lo_hi"}
    p["read"]["soc"]["unit_id"] = 1
    p["read"]["load_power_w"]["sum"].append({"addr": 30000, "type": "i16", "unit_id": 1})
    p["baseline"]["enable"] = False
    p["write_policy"].update(order=list(_ORDER_V2), power_by_direction=True, release_without_plan_s=900)
    p["modbus"]["probe_keys"] = ["mode", "enable"]
    p["ha"]["integrations"][0]["entities"].update(
        enable={"domain": "switch", "unique_id_regex": "^x-remote_ems-"},
        charge_power_w={"domain": "number", "unique_id_regex": "^x-charge_limit-"},
        discharge_power_w={"domain": "number", "unique_id_regex": "^x-discharge_limit-"})
    return p


def test_engine_constructs_valid_profile():
    assert validate_profile(_engine_v2()) == []


def test_engine_constructs_are_each_optional():
    """Każda konstrukcja z osobna (przełącznik bez mocy wg kierunku i odwrotnie)."""
    p = ms_profile()
    p["write"]["enable"] = {"addr": 40029, "type": "u16", "encode": "on_off", "on": 0xAA, "off": 0x55}
    p["write_policy"]["order"].insert(-1, "enable")
    assert validate_profile(p) == []
    p = _engine_v2()
    p["write"].pop("enable")
    p["write_policy"]["order"].remove("enable")
    p["read"].pop("enable_state")
    p["baseline"].pop("enable")
    p["modbus"]["probe_keys"] = ["mode"]
    p["ha"]["integrations"][0]["entities"].pop("enable")
    assert validate_profile(p) == []
    p = ms_profile()
    p["write"]["power_w"].update(type="u32", scale=10)
    p["write_policy"]["release_without_plan_s"] = 60
    assert validate_profile(p) == []


def _mut(fn):
    def run():
        p = _engine_v2()
        fn(p)
        return p
    return run


def _drop_enable(p):
    p["write"].pop("enable")
    p["write_policy"]["order"].remove("enable")
    p["modbus"]["probe_keys"] = ["mode"]


@pytest.mark.parametrize("build,needle", [
    # typ zapisu: tylko u16/u32; u32 tylko dla nastaw liczbowych
    (_mut(lambda p: p["write"]["charge_power_w"].update(type="i32")), "$.write.charge_power_w.type"),
    (_mut(lambda p: p["write"]["mode"].update(type="u32")), "$.write.mode.type"),
    (_mut(lambda p: p["write"]["enable"].update(type="u32")), "$.write.enable.type"),
    (_mut(lambda p: p["write"]["export_limit_enabled"].update(type="u32")),
     "$.write.export_limit_enabled.type"),
    (_mut(lambda p: p["write"]["charge_power_w"].update(addr=65535)), "$.write.charge_power_w.addr"),
    # kolejność słów tylko przy u32
    (_mut(lambda p: p["write"]["export_limit_w"].update(word_order="hi_lo")),
     "$.write.export_limit_w.word_order"),
    (_mut(lambda p: p["write"]["charge_power_w"].update(word_order="big")),
     "$.write.charge_power_w.word_order"),
    # skala: liczba > 0, tylko przy nastawach liczbowych
    (_mut(lambda p: p["write"]["charge_power_w"].update(scale=0)), "$.write.charge_power_w.scale"),
    (_mut(lambda p: p["write"]["charge_power_w"].update(scale=-1)), "$.write.charge_power_w.scale"),
    (_mut(lambda p: p["write"]["charge_power_w"].update(scale=float("nan"))), "$.write.charge_power_w.scale"),
    (_mut(lambda p: p["write"]["charge_power_w"].update(scale="1")), "$.write.charge_power_w.scale"),
    (_mut(lambda p: p["write"]["mode"].update(scale=1)), "$.write.mode.scale"),
    (_mut(lambda p: p["write"]["enable"].update(scale=1)), "$.write.enable.scale"),
    # adres urządzenia per zapis i odczyt
    (_mut(lambda p: p["write"]["charge_power_w"].update(unit_id=256)), "$.write.charge_power_w.unit_id"),
    (_mut(lambda p: p["write"]["charge_power_w"].update(unit_id=True)), "$.write.charge_power_w.unit_id"),
    (_mut(lambda p: p["read"]["soc"].update(unit_id=-1)), "$.read.soc.unit_id"),
    (_mut(lambda p: p["read"]["load_power_w"]["sum"][2].update(unit_id="1")),
     "$.read.load_power_w.sum[2].unit_id"),
    # adres urządzenia nie w identyfikacji (czytana blokami połączenia)
    (_mut(lambda p: p["identify"]["registers"]["rated_power_w"].update(unit_id=1)),
     "$.identify.registers.rated_power_w.unit_id"),
    # przełącznik: kodowanie on_off z dwiema różnymi wartościami
    (_mut(lambda p: p["write"]["enable"].update(encode="bool")), "$.write.enable.encode"),
    (_mut(lambda p: p["write"]["enable"].pop("on")), "$.write.enable.on"),
    (_mut(lambda p: p["write"]["enable"].pop("off")), "$.write.enable.off"),
    (_mut(lambda p: p["write"]["enable"].update(on=0)), "$.write.enable.off"),
    (_mut(lambda p: p["write"]["enable"].update(on=70000)), "$.write.enable.on"),
    (_mut(lambda p: p["write"]["enable"].update(off=True)), "$.write.enable.off"),
    (_mut(lambda p: p["write"]["export_limit_enabled"].update(on=1, off=0)),
     "$.write.export_limit_enabled.on"),
    # odczyt stanu przełącznika tylko przy zapisie przełącznika
    (_mut(lambda p: (_drop_enable(p), p["baseline"].pop("enable"))), "$.read.enable_state"),
    # kolejność: nastawy mocy przed przełącznikiem, tryb ostatni
    (_mut(lambda p: p["write_policy"].update(order=["soc_min", "enable", "charge_power_w",
                                                    "discharge_power_w", "export_limit_w",
                                                    "export_limit_enabled", "mode"])),
     "$.write_policy.order"),
    (_mut(lambda p: p["write_policy"].update(order=["soc_min", "charge_power_w", "discharge_power_w",
                                                    "export_limit_w", "export_limit_enabled", "mode",
                                                    "enable"])),
     "$.write_policy.order"),
    # moc wg kierunku: wymaga obu nastaw, wyklucza power_w, nastawy kierunkowe wymagają flagi
    (_mut(lambda p: (p["write"].pop("discharge_power_w"),
                     p["write_policy"]["order"].remove("discharge_power_w"))),
     "$.write_policy.power_by_direction"),
    (_mut(lambda p: (p["write"].update(power_w={"addr": 47512, "type": "u16", "encode": "watts"}),
                     p["write_policy"]["order"].insert(0, "power_w"))),
     "$.write_policy.power_by_direction"),
    (_mut(lambda p: p["write_policy"].update(power_by_direction=False)), "$.write.charge_power_w"),
    (_mut(lambda p: p["write_policy"].pop("power_by_direction")), "$.write.discharge_power_w"),
    (_mut(lambda p: p["write_policy"].update(power_by_direction="yes")), "$.write_policy.power_by_direction"),
    # moc ze slotu przy mocy wg kierunku tylko w trybie ładowania/rozładowania
    (_mut(lambda p: p["intents"]["charge_grid"].update(mode="auto")), "$.intents.charge_grid.mode"),
    (_mut(lambda p: p["write"]["charge_power_w"].update(encode="percent")),
     "$.write.charge_power_w.encode"),
    # stan wyjściowy przełącznika: bool i tylko przy zapisie przełącznika
    (_mut(lambda p: p["baseline"].update(enable="off")), "$.baseline.enable"),
    (_mut(lambda p: (_drop_enable(p), p["read"].pop("enable_state"))), "$.baseline.enable"),
    # powrót bez planu: liczba całkowita sekund w zakresie
    (_mut(lambda p: p["write_policy"].update(release_without_plan_s=59)),
     "$.write_policy.release_without_plan_s"),
    (_mut(lambda p: p["write_policy"].update(release_without_plan_s=86401)),
     "$.write_policy.release_without_plan_s"),
    (_mut(lambda p: p["write_policy"].update(release_without_plan_s=900.5)),
     "$.write_policy.release_without_plan_s"),
    (_mut(lambda p: p["write_policy"].update(release_without_plan_s=True)),
     "$.write_policy.release_without_plan_s"),
    # encje HA: przełącznik = switch, nastawy kierunkowe = number, stan przełącznika nie jest encją
    (_mut(lambda p: p["ha"]["integrations"][0]["entities"]["enable"].update(domain="number")),
     "entities.enable.domain"),
    (_mut(lambda p: p["ha"]["integrations"][0]["entities"]["charge_power_w"].update(domain="sensor")),
     "entities.charge_power_w.domain"),
    (_mut(lambda p: p["ha"]["integrations"][0]["entities"].update(
        enable_state={"domain": "switch", "unique_id_regex": "x"})), "entities.enable_state"),
])
def test_engine_construct_rule_errors(build, needle):
    assert needle in _errs(build())


@pytest.mark.parametrize("key,spec", [
    ("enable", {"addr": 1, "type": "u16", "encode": "on_off", "on": 1, "off": 0}),
    ("charge_power_w", {"addr": 1, "type": "u16", "encode": "watts"}),
])
def test_new_write_keys_only_for_mode_setpoint(key, spec):
    p = tw_profile()
    p["write"][key] = spec
    p["write_policy"]["order"].insert(0, key)
    assert f"$.write.{key}" in _errs(p)


@pytest.mark.parametrize("field,value", [("power_by_direction", True), ("release_without_plan_s", 900)])
def test_new_write_policy_fields_only_for_mode_setpoint(field, value):
    p = tw_profile()
    p["write_policy"][field] = value
    assert f"$.write_policy.{field}" in _errs(p)


def test_u32_write_covers_two_registers_for_verify_blocks():
    p = _engine_v2()
    p["modbus"]["verify_blocks"] = [{"addr": 40033, "count": 1}]     # drugie słowo nastawy u32
    assert validate_profile(p) == []


def _overlap_errors(p) -> list[str]:
    return [e for e in validate_profile(p) if "nachodzi na rejestr zapisu" in e]


# Błąd przy kluczu późniejszym w `$.write`, z nazwą klucza wcześniejszego (kolejność _engine_v2: mode,
# soc_min, export_limit_w, export_limit_enabled, charge_power_w, discharge_power_w, enable).
@pytest.mark.parametrize("mutate,path,other", [
    # drugie słowo nastawy u32 na adresie innego klucza
    (lambda p: p["write"]["export_limit_w"].update(addr=40033), "$.write.charge_power_w.addr", "export_limit_w"),
    # dwie nastawy u32 zachodzące jednym słowem
    (lambda p: p["write"]["discharge_power_w"].update(addr=40033, unit_id=247),
     "$.write.discharge_power_w.addr", "charge_power_w"),
    # przełącznik na słowie nastawy mocy
    (lambda p: p["write"]["enable"].update(addr=40035), "$.write.enable.addr", "discharge_power_w"),
    # ten sam adres i ten sam adres urządzenia (jawny = domyślny profilu)
    (lambda p: p["write"]["soc_min"].update(addr=40029, unit_id=247), "$.write.enable.addr", "soc_min"),
    (lambda p: p["write"]["soc_min"].update(addr=40029, unit_id=1) or p["write"]["enable"].update(unit_id=1),
     "$.write.enable.addr", "soc_min"),
])
def test_write_registers_must_not_overlap(mutate, path, other):
    p = _engine_v2()
    mutate(p)
    errs = _overlap_errors(p)
    assert len(errs) == 1, errs
    assert errs[0].startswith(f"{path}: nachodzi na rejestr zapisu {other} ")
    assert [e for e in validate_profile(p) if e not in errs] == []


def test_same_write_address_on_another_unit_is_allowed():
    p = _engine_v2()
    p["write"]["soc_min"].update(addr=40029, unit_id=1)      # enable: 40029 na adresie domyślnym (247)
    assert validate_profile(p) == []
    p["write"]["soc_min"].update(addr=40033, unit_id=1)      # słowo u32 charge_power_w, ale unit 247
    assert validate_profile(p) == []


def test_write_overlap_covers_time_window_programs():
    p = tw_profile()
    p["write"]["tou_enable"] = {"addr": 157, "enable_bit": 0, "day_mask": 254}   # w programach mocy 154..159
    errs = _overlap_errors(p)
    assert len(errs) == 1 and errs[0].startswith("$.write.tou_enable.addr: nachodzi na rejestr zapisu "
                                                 "tou_program ")
    p["write"]["tou_enable"]["addr"] = 146
    assert validate_profile(p) == []


def test_builtin_profiles_use_no_new_engine_construct():
    """Profile z pakietu walidują się bez zmian i nie sięgają po nowe konstrukcje (wszystkie opt-in)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "volcast" / "profiles"
    files = sorted(root.glob("*.json"))
    assert len(files) == 10
    for f in files:
        raw = json.loads(f.read_text(encoding="utf-8"))
        assert validate_profile(raw) == [], f.name
        assert not {"enable", "charge_power_w", "discharge_power_w"} & set(raw["write"]), f.name
        assert not {"power_by_direction", "release_without_plan_s"} & set(raw["write_policy"]), f.name
        assert "enable" not in raw["baseline"], f.name
        assert all(s.get("type", "u16") == "u16" and "scale" not in s and "unit_id" not in s
                   for k, s in raw["write"].items() if k not in ("tou_program", "tou_enable")), f.name
