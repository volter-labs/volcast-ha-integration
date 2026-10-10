"""Rekomendacja ścieżki sterowania z rozpoznania, profili, sondy i mapy encji (czysta logika)."""
import pytest

from custom_components.volcast.core.control.recommend import (
    DIRECT, DIRECT_WITH_INTEGRATION_DATA, ENTITIES, ORIGINS, PATHS, REASONS, UNSUPPORTED, Recommendation,
    recommend)
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.discovery.identify import Identity
from custom_components.volcast.core.discovery.probe import ProbeReport
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from tests.core.profile_fixtures import ms_profile

MODE_MAP = {"mode": "select.inverter_ems_mode", "soc": "sensor.inverter_battery_soc"}
CORE = {"goodwe": "core", "sma": "core"}
# Lista zamknięta kontraktu `driver.control.recommendation.reason` (chmura odrzuca inne kody).
CONTRACT_REASONS = {"integration_write_entities", "no_integration_identify_ok", "integration_read_only",
                    "no_profile", "no_write_path"}


def _profile(*, status="verified", entry_status="draft", ems=False, modbus="draft"):
    raw = ms_profile()
    raw["status"] = status
    raw["modbus"]["status"] = modbus
    integ = raw["ha"]["integrations"][0]
    integ["status"], integ["ems"] = entry_status, ems
    integ["entities"]["mode"] = {"domain": "select", "unique_id_regex": "^goodwe-ems_mode-"}
    return profile_from_dict(raw)


def _report(domain="goodwe", model="GW10K-ET", manufacturer="GoodWe"):
    return {"inverters": [{"domain": domain, "matched_by": "domain", "config_entry_title": "Inverter",
                           "devices": [{"manufacturer": manufacturer, "model": model}], "entities": []}]}


def _probe(profile_id="test-ms", *, direct_available=True, model="GW10K-ET"):
    ident = Identity(profile_id, "goodwe_udp", 8899, 247, model, 10000.0, device_fp="fp")
    return ProbeReport(ident, {"mode": True}, (), direct_available, None, "draft", 3, ())


# ── 6 scenariuszy reguł rekomendacji ────────────────────────────────────


def test_integration_writing_mode_recommends_entities_from_rung_one():
    rec = recommend(_report(), [_profile()], None, "direct_not_found", MODE_MAP, origins=CORE)
    assert (rec.path, rec.reason, rec.ladder_start) == (ENTITIES, "integration_write_entities", 1)
    assert rec.integration == {"domain": "goodwe", "name": "GoodWe", "origin": "core"}
    assert rec.device == {"manufacturer": "GoodWe", "model": "GW10K-ET"}
    assert rec.conflicts == ()


def test_verified_profile_and_verified_entry_start_at_the_control_write():
    rec = recommend(_report(), [_profile(entry_status="verified")], None, None, MODE_MAP)
    assert (rec.path, rec.ladder_start) == (ENTITIES, 4)
    # profil draft z wpisem verified to nadal draft — drabina od początku
    rec = recommend(_report(), [_profile(status="draft", entry_status="verified")], None, None, MODE_MAP)
    assert (rec.path, rec.ladder_start) == (ENTITIES, 1)


def test_ems_flag_alone_is_not_a_conflict():
    rec = recommend(_report(), [_profile(ems=True)], None, None, MODE_MAP)
    assert rec.path == ENTITIES and rec.conflicts == () and rec.conflicts_payload() == []


def test_builtin_goodwe_in_entities_mode_has_no_conflict():
    goodwe = load_builtin("goodwe-et")
    choice = ProfileChoice(goodwe, "goodwe", "GW8KN-ET")
    rec = recommend(_report(model="GW8KN-ET"), [goodwe], None, None,
                    {"mode": "select.goodwe_inverter_operation_mode"}, choice=choice, origins=CORE)
    assert rec.path == ENTITIES and rec.conflicts == ()


def test_no_integration_and_identified_inverter_recommends_direct():
    rec = recommend({"inverters": []}, [_profile()], _probe(), None, {})
    assert (rec.path, rec.reason, rec.ladder_start) == (DIRECT, "no_integration_identify_ok", 1)
    assert rec.integration is None
    assert rec.device == {"manufacturer": "Test", "model": "GW10K-ET"}
    # zweryfikowana ścieżka rejestrów (profil + modbus), próba udana, oferta bez odmowy → zapis kontrolny
    rec = recommend({"inverters": []}, [_profile(modbus="verified")], _probe(), None, {})
    assert (rec.path, rec.ladder_start) == (DIRECT, 4)
    # bez udanej próby zapisu weryfikacja zaczyna od początku; odmowa nie jest powodem rekomendacji
    rec = recommend({"inverters": []}, [_profile(modbus="verified")], _probe(direct_available=False),
                    "direct_unverified", {})
    assert (rec.path, rec.reason, rec.ladder_start) == (DIRECT, "no_integration_identify_ok", 1)


def test_read_only_integration_of_the_probed_inverter_recommends_direct_with_integration_data():
    rec = recommend(_report(), [_profile()], _probe(), None, {"soc": "sensor.inverter_battery_soc"}, origins=CORE)
    assert (rec.path, rec.reason) == (DIRECT_WITH_INTEGRATION_DATA, "integration_read_only")
    assert rec.integration == {"domain": "goodwe", "name": "GoodWe", "origin": "core"}
    assert "entity_map" not in rec.to_payload()


def test_without_a_profile_the_path_is_unsupported():
    rec = recommend(_report(domain="sma", model="Sunny Boy", manufacturer="SMA"), [], None,
                    "direct_not_found", {})
    assert (rec.path, rec.reason, rec.ladder_start) == (UNSUPPORTED, "no_profile", 1)
    # integracja bez profilu nie jest „pierwszą z brzegu” danych rekomendacji
    assert rec.integration is None and rec.device is None
    assert recommend(None, [], None, None, None).path == UNSUPPORTED


# ── przypadki brzegowe ─────────────────────────────────────────────────


def test_unrelated_integration_is_not_a_data_source_for_the_probed_inverter():
    rec = recommend(_report(domain="sma", model="Sunny Boy", manufacturer="SMA"), [_profile()], _probe(), None, {})
    assert (rec.path, rec.reason, rec.integration) == (DIRECT, "no_integration_identify_ok", None)
    assert rec.device == {"manufacturer": "Test", "model": "GW10K-ET"}


def test_integration_matched_by_profile_candidates_or_brand_is_the_data_source():
    report = _report(manufacturer="Other", model="X")
    report["inverters"][0]["profile_candidates"] = ["test-ms", "test-other"]
    assert recommend(report, [_profile()], _probe(), None, {}).path == DIRECT_WITH_INTEGRATION_DATA
    by_brand = _report(domain="solarman", manufacturer="Test Energy", model="X")
    assert recommend(by_brand, [_profile()], _probe(), None, {}).path == DIRECT_WITH_INTEGRATION_DATA


def test_profile_without_any_write_path_is_unsupported_with_its_own_reason():
    rec = recommend(_report(), [_profile()], None, "direct_not_found", {"soc": "sensor.x"}, origins=CORE)
    assert (rec.path, rec.reason) == (UNSUPPORTED, "no_write_path")
    assert rec.integration == {"domain": "goodwe", "name": "GoodWe", "origin": "core"}


def test_integration_with_mode_wins_over_an_identified_inverter():
    assert recommend(_report(), [_profile()], _probe(), None, MODE_MAP).path == ENTITIES


def test_static_clash_goes_to_conflicts_and_keeps_the_ladder_at_rung_one():
    rec = recommend({"inverters": []}, [_profile(modbus="verified")], _probe(), "direct_conflict", {},
                    conflicts=("modbus", "volcast", "modbus"))
    assert (rec.path, rec.reason, rec.ladder_start) == (DIRECT, "no_integration_identify_ok", 1)
    assert [(c["kind"], c["label"]) for c in rec.conflicts] == [("entry", "modbus"), ("entry", "volcast")]


def test_failed_conflict_check_is_not_reported_as_an_observed_entry():
    rec = recommend({"inverters": []}, [_profile()], _probe(), "direct_conflict", {}, conflicts=("unknown",))
    assert rec.conflicts_payload() == [{"kind": "entry", "label": "unknown", "evidence": "conflict check failed"}]


def test_explicit_choice_overrides_report_hints():
    prof = _profile(entry_status="verified")
    rec = recommend({"inverters": []}, [prof], None, None, MODE_MAP,
                    choice=ProfileChoice(prof, "goodwe", "GW8KN-ET"))
    assert (rec.path, rec.ladder_start) == (ENTITIES, 4)
    assert rec.device == {"manufacturer": None, "model": "GW8KN-ET"}


# ── ładunek wg kontraktu `driver.control.recommendation` ────────────────


def test_contract_lists_are_mirrored():
    assert set(REASONS) == CONTRACT_REASONS
    assert set(ORIGINS) == {"core", "custom"}
    assert set(PATHS) == {"entities", "direct", "direct_with_integration_data", "unsupported"}


@pytest.mark.parametrize("args", [
    (_report(), [_profile()], None, None, MODE_MAP),
    ({"inverters": []}, [_profile()], _probe(), "direct_conflict", {}),
    (_report(), [_profile()], _probe(), None, {}),
    (_report(), [_profile()], None, None, {}),
    (None, [], None, None, None),
])
def test_every_emitted_reason_is_a_contract_code(args):
    payload = recommend(*args).to_payload()
    assert payload["reason"] in CONTRACT_REASONS and payload["path"] in PATHS


def test_payload_shape_for_entities():
    rec = recommend(_report(), [_profile()], None, None, {**MODE_MAP, "power_w": "number.Bad-Id"}, origins=CORE)
    assert rec.to_payload() == {
        "path": "entities", "reason": "integration_write_entities", "ladder_start": 1,
        "integration": {"domain": "goodwe", "name": "GoodWe", "origin": "core"},
        "device": {"manufacturer": "GoodWe", "model": "GW10K-ET"},
        # posortowane po kluczu; entity_id spoza wzorca kontraktu nie idzie do chmury
        "entity_map": [{"key": "mode", "entity_id": "select.inverter_ems_mode"},
                       {"key": "soc", "entity_id": "sensor.inverter_battery_soc"}],
    }


def test_origin_outside_the_contract_is_omitted():
    rec = recommend(_report(), [_profile()], None, None, MODE_MAP, origins={"goodwe": "domain"})
    assert rec.to_payload()["integration"] == {"domain": "goodwe", "name": "GoodWe"}
    bad = Recommendation(ENTITIES, "integration_write_entities", 1, integration={"domain": "Bad Domain"})
    assert "integration" not in bad.to_payload()


def test_payload_limits_entity_map_and_text_lengths():
    big = {f"k{i:02d}": f"sensor.e{i:02d}" for i in range(30)}
    rec = Recommendation(path=ENTITIES, reason="x" * 100, ladder_start=1,
                         integration={"domain": "d" * 32, "name": "n" * 80, "origin": "custom"},
                         device={"manufacturer": "m" * 80, "model": None}, entity_map=tuple(big.items()))
    p = rec.to_payload()
    assert len(p["entity_map"]) == 24
    assert len(p["reason"]) == 64 and len(p["integration"]["name"]) == 64
    assert p["device"] == {"manufacturer": "m" * 64}
    long_id = "sensor." + "a" * 70
    assert Recommendation(ENTITIES, "r", 1, entity_map=(("soc", long_id),)).to_payload().get("entity_map") is None
    bad_key = Recommendation(ENTITIES, "r", 1, entity_map=(("Bad Key", "sensor.x"),))
    assert bad_key.to_payload().get("entity_map") is None


def test_conflicts_payload_is_capped_at_eight():
    rec = recommend({"inverters": []}, [_profile()], _probe(), "direct_conflict", {},
                    conflicts=tuple(f"dom{i}" for i in range(12)))
    out = rec.conflicts_payload()
    assert len(out) == 8 and all(set(c) == {"kind", "label", "evidence"} for c in out)
    assert all(len(c["evidence"]) <= 120 and len(c["label"]) <= 64 for c in out)


@pytest.mark.parametrize("path", [ENTITIES, DIRECT, DIRECT_WITH_INTEGRATION_DATA, UNSUPPORTED])
def test_ladder_start_is_identify_or_control_write(path):
    assert Recommendation(path, "r", 4).to_payload()["ladder_start"] == 4
    assert Recommendation(path, "r", 1).to_payload()["ladder_start"] == 1
    assert 1 <= Recommendation(path, "r", 9).to_payload()["ladder_start"] <= 4
    assert Recommendation(path, "r", 0).to_payload()["ladder_start"] == 1


# ── wpis ze skonfigurowanym celem bezpośrednim (tożsamość znana bez sondy) ──


GW_TARGET = {"profile_id": "goodwe-et", "host": "inverter.lan", "device_fp": "abcd"}


def _goodwe_report(host="box.lan", model="GW-HUB"):
    report = _report(model=model)
    report["inverters"][0]["host"] = host
    return report


def test_configured_direct_target_with_an_unrelated_integration_recommends_direct():
    goodwe = load_builtin("goodwe-et")
    rec = recommend(_goodwe_report(), [goodwe], None, None, {}, target=GW_TARGET, origins=CORE)
    assert (rec.path, rec.reason, rec.ladder_start) == (DIRECT, "no_integration_identify_ok", 4)
    assert rec.integration is None and rec.device == {"manufacturer": "GoodWe", "model": None}


def test_configured_direct_target_without_any_integration_recommends_direct():
    goodwe = load_builtin("goodwe-et")
    rec = recommend({"inverters": []}, [goodwe], None, None, {}, target=GW_TARGET)
    assert (rec.path, rec.reason, rec.ladder_start) == (DIRECT, "no_integration_identify_ok", 4)
    # model z sondy tego samego urządzenia (ten sam odcisk), gdy jest
    probe = ProbeReport(Identity("goodwe-et", "goodwe_udp", 8899, 247, "GW8KN-ET", 8000.0, device_fp="abcd"),
                        {"mode": True}, (), True, None, "verified", 3, ())
    rec = recommend({"inverters": []}, [goodwe], probe, None, {}, target=GW_TARGET)
    assert rec.device == {"manufacturer": "GoodWe", "model": "GW8KN-ET"}


def test_configured_direct_target_wins_over_an_entities_path_of_another_device():
    goodwe = load_builtin("goodwe-et")
    rec = recommend(_goodwe_report(), [goodwe], None, None, {"mode": "select.box_mode"}, target=GW_TARGET,
                    choice=ProfileChoice(goodwe, "goodwe", "GW-HUB"))
    assert (rec.path, rec.ladder_start) == (DIRECT, 4)


def test_read_only_integration_on_the_target_address_is_the_data_source():
    goodwe = load_builtin("goodwe-et")
    rec = recommend(_goodwe_report(host="inverter.lan"), [goodwe], None, None, {}, target=GW_TARGET,
                    origins=CORE)
    assert (rec.path, rec.reason, rec.ladder_start) == (DIRECT_WITH_INTEGRATION_DATA, "integration_read_only", 4)
    assert rec.integration["domain"] == "goodwe"


def test_configured_draft_target_starts_at_identify_and_keeps_the_clash_as_conflict():
    rec = recommend({"inverters": []}, [_profile()], None, None, {}, ("modbus",),
                    target={"profile_id": "test-ms", "host": "inverter.lan", "device_fp": "abcd"})
    assert (rec.path, rec.ladder_start) == (DIRECT, 1)
    assert [c["label"] for c in rec.conflicts] == ["modbus"]


def test_probed_inverter_at_another_address_than_the_brand_integration_recommends_direct():
    from custom_components.volcast.core.discovery.identify import Candidate
    goodwe = load_builtin("goodwe-et")
    probe = ProbeReport(Identity("goodwe-et", "goodwe_udp", 8899, 247, "GW8KN-ET", 8000.0, device_fp="abcd"),
                        {"mode": True}, (), True, None, "verified", 3, (),
                        candidate=Candidate(host="inverter.lan", source="udp_48899"))
    rec = recommend(_goodwe_report(), [goodwe], probe, None, {})
    assert (rec.path, rec.integration) == (DIRECT, None)
    rec = recommend(_goodwe_report(host="inverter.lan"), [goodwe], probe, None, {})
    assert rec.path == DIRECT_WITH_INTEGRATION_DATA


def test_without_a_configured_target_or_identity_nothing_changes():
    rec = recommend(_goodwe_report(), [load_builtin("goodwe-et")], None, None, {}, target=None)
    assert (rec.path, rec.reason) == (UNSUPPORTED, "no_write_path")
    # cel z nieznanym profilem nie udaje rozpoznanego falownika
    rec = recommend({"inverters": []}, [], None, None, {}, target={"profile_id": "nope", "device_fp": "abcd"})
    assert (rec.path, rec.reason) == (UNSUPPORTED, "no_profile")
