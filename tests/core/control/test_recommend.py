"""Rekomendacja ścieżki sterowania z rozpoznania, profili, sondy i mapy encji (czysta logika)."""
import pytest

from custom_components.volcast.core.control.recommend import (
    DIRECT, DIRECT_WITH_INTEGRATION_DATA, ENTITIES, UNSUPPORTED, Recommendation, recommend)
from custom_components.volcast.core.discovery.identify import Identity
from custom_components.volcast.core.discovery.probe import ProbeReport
from custom_components.volcast.core.profile import profile_from_dict
from tests.core.profile_fixtures import ms_profile

MODE_MAP = {"mode": "select.inverter_ems_mode", "soc": "sensor.inverter_battery_soc"}


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


# ── 6 scenariuszy reguł D4 ───────────────────────────────────────────────


def test_integration_writing_mode_recommends_entities_from_rung_one():
    rec = recommend(_report(), [_profile()], None, "direct_not_found", MODE_MAP)
    assert (rec.path, rec.reason, rec.ladder_start) == (ENTITIES, "integration_writes", 1)
    assert rec.integration == {"domain": "goodwe", "name": "GoodWe", "origin": "domain"}
    assert rec.device == {"manufacturer": "GoodWe", "model": "GW10K-ET"}
    assert rec.conflicts == ()


def test_verified_profile_and_verified_entry_start_at_the_control_write():
    rec = recommend(_report(), [_profile(entry_status="verified")], None, None, MODE_MAP)
    assert (rec.path, rec.ladder_start) == (ENTITIES, 3)
    # profil draft z wpisem verified to nadal draft — drabina od początku
    rec = recommend(_report(), [_profile(status="draft", entry_status="verified")], None, None, MODE_MAP)
    assert (rec.path, rec.ladder_start) == (ENTITIES, 1)


def test_ems_integration_is_an_entry_conflict_at_once():
    rec = recommend(_report(), [_profile(ems=True)], None, None, MODE_MAP)
    assert rec.path == ENTITIES
    assert [(c["kind"], c["label"]) for c in rec.conflicts] == [("entry", "goodwe")]
    # jawna flaga wygrywa nad profilem (wołający wie lepiej, np. z opcji integracji)
    assert recommend(_report(), [_profile(ems=True)], None, None, MODE_MAP, ems_flag=False).conflicts == ()


def test_no_integration_and_identified_inverter_recommends_direct():
    rec = recommend({"inverters": []}, [_profile()], _probe(), None, {})
    assert (rec.path, rec.reason, rec.ladder_start) == (DIRECT, "identified", 1)
    assert rec.integration is None
    assert rec.device == {"manufacturer": "Test", "model": "GW10K-ET"}
    # zweryfikowana ścieżka rejestrów (profil + modbus) i próba udana → od zapisu kontrolnego
    rec = recommend({"inverters": []}, [_profile(modbus="verified")], _probe(), None, {})
    assert (rec.path, rec.ladder_start) == (DIRECT, 3)
    # bez udanej próby zapisu weryfikacja zaczyna od początku
    rec = recommend({"inverters": []}, [_profile(modbus="verified")], _probe(direct_available=False),
                    "direct_unverified", {})
    assert (rec.path, rec.reason, rec.ladder_start) == (DIRECT, "direct_unverified", 1)


def test_read_only_integration_and_identified_inverter_recommends_direct_with_integration_data():
    rec = recommend(_report(), [_profile()], _probe(), None, {"soc": "sensor.inverter_battery_soc"})
    assert (rec.path, rec.reason) == (DIRECT_WITH_INTEGRATION_DATA, "integration_read_only")
    assert rec.integration["domain"] == "goodwe"
    assert "entity_map" not in rec.to_payload()


def test_without_a_profile_the_path_is_unsupported():
    rec = recommend(_report(domain="sma", model="Sunny Boy", manufacturer="SMA"), [], None,
                    "direct_not_found", {})
    assert (rec.path, rec.reason, rec.ladder_start) == (UNSUPPORTED, "no_profile", 1)
    assert rec.integration == {"domain": "sma", "name": "SMA", "origin": "domain"}
    assert recommend(None, [], None, None, None).path == UNSUPPORTED


# ── przypadki brzegowe ─────────────────────────────────────────────────


def test_profile_without_any_write_path_is_unsupported():
    rec = recommend(_report(), [_profile()], None, "direct_not_found", {"soc": "sensor.x"})
    assert (rec.path, rec.reason) == (UNSUPPORTED, "no_write_path")


def test_integration_with_mode_wins_over_an_identified_inverter():
    assert recommend(_report(), [_profile()], _probe(), None, MODE_MAP).path == ENTITIES


def test_static_clash_is_reported_as_entry_conflicts_with_the_offer_reason():
    rec = recommend({"inverters": []}, [_profile()], _probe(), "direct_conflict", {},
                    conflicts=("modbus", "volcast", "modbus"))
    assert (rec.path, rec.reason) == (DIRECT, "direct_conflict")
    assert [(c["kind"], c["label"]) for c in rec.conflicts] == [("entry", "modbus"), ("entry", "volcast")]


def test_explicit_choice_overrides_report_hints():
    from custom_components.volcast.core.control.select import ProfileChoice
    prof = _profile(entry_status="verified")
    rec = recommend({"inverters": []}, [prof], None, None, MODE_MAP,
                    choice=ProfileChoice(prof, "goodwe", "GW8KN-ET"))
    assert (rec.path, rec.ladder_start) == (ENTITIES, 3)
    assert rec.device == {"manufacturer": None, "model": "GW8KN-ET"}


# ── ładunek wg kontraktu `driver.control.recommendation` ────────────────


def test_payload_shape_for_entities():
    rec = recommend(_report(), [_profile()], None, None, {**MODE_MAP, "power_w": "number.Bad-Id"})
    assert rec.to_payload() == {
        "path": "entities", "reason": "integration_writes", "ladder_start": 1,
        "integration": {"domain": "goodwe", "name": "GoodWe", "origin": "domain"},
        "device": {"manufacturer": "GoodWe", "model": "GW10K-ET"},
        # posortowane po kluczu; entity_id spoza wzorca kontraktu nie idzie do chmury
        "entity_map": [{"key": "mode", "entity_id": "select.inverter_ems_mode"},
                       {"key": "soc", "entity_id": "sensor.inverter_battery_soc"}],
    }


def test_payload_limits_entity_map_and_text_lengths():
    big = {f"k{i:02d}": f"sensor.e{i:02d}" for i in range(30)}
    rec = Recommendation(path=ENTITIES, reason="x" * 100, ladder_start=1,
                         integration={"domain": "d" * 80, "name": "n" * 80, "origin": "domain"},
                         device={"manufacturer": "m" * 80, "model": None}, entity_map=tuple(big.items()))
    p = rec.to_payload()
    assert len(p["entity_map"]) == 24
    assert len(p["reason"]) == 64 and len(p["integration"]["domain"]) == 64
    assert p["device"] == {"manufacturer": "m" * 64}
    long_id = "sensor." + "a" * 70
    assert Recommendation(ENTITIES, "r", 1, entity_map=(("soc", long_id),)).to_payload().get("entity_map") is None


def test_conflicts_payload_is_capped_at_eight():
    rec = recommend({"inverters": []}, [_profile()], _probe(), "direct_conflict", {},
                    conflicts=tuple(f"dom{i}" for i in range(12)))
    out = rec.conflicts_payload()
    assert len(out) == 8 and all(set(c) == {"kind", "label", "evidence"} for c in out)
    assert all(len(c["evidence"]) <= 120 and len(c["label"]) <= 64 for c in out)


@pytest.mark.parametrize("path", [ENTITIES, DIRECT, DIRECT_WITH_INTEGRATION_DATA, UNSUPPORTED])
def test_ladder_start_always_in_contract_range(path):
    assert 1 <= Recommendation(path, "r", 3).to_payload()["ladder_start"] <= 3
