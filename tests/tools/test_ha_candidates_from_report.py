"""Narzędzie kandydatów encji: najpierw sprawdza, potem zapisuje; wyciek = odmowa bez pliku."""
import ast
import json
from pathlib import Path

import pytest

from tools.golden import ha_candidates_from_report as tool

TOOL_SRC = Path(tool.__file__)
FIXTURE = Path(__file__).resolve().parents[1] / "golden" / "goodwe_et" / "ha_candidates.json"


def _entity(unique_id, **kw):
    e = {"entity_domain": "sensor", "platform": "goodwe", "unique_id": unique_id,
         "translation_key": "battery_soc", "unit": "%"}
    e.update(kw)
    return e


def _write_report(tmp_path, entities):
    diag = {"data": {"report": {"schema": 1, "inverters": [{"entities": entities}]}}}
    src = tmp_path / "diag.json"
    src.write_text(json.dumps(diag))
    return src


def test_masks_placeholder_serial_and_hex_ids(tmp_path):
    src = _write_report(tmp_path, [
        _entity("goodwe-battery_soc-<SN>"),
        _entity("0123456789abcdef0123456789abcdef-ppv", translation_key="ppv"),
    ])
    dst = tmp_path / "out.json"
    tool.main(str(src), str(dst))
    out = json.loads(dst.read_text())
    assert out[0]["unique_id"] == f"goodwe-battery_soc-{tool.FAKE_SERIAL}"
    assert out[1]["unique_id"] == "<ID>-ppv"
    assert out[0]["entity_id"] == "sensor.e0"


@pytest.mark.parametrize("unique_id,hint", [
    ("goodwe-battery_soc-95048ESU224W0123", "battery_soc"),      # numer seryjny GoodWe
    ("goodwe-battery_soc-<SN>", "host 192.168.1.23"),             # IPv4
    ("goodwe-battery_soc-<SN>", "mac a4:cf:12:34:56:78"),         # MAC z dwukropkami
    ("goodwe-battery_soc-A4-CF-12-34-56-78", "battery_soc"),      # MAC z myślnikami
    ("goodwe-battery_soc-<SN>", "logger 2712345678"),             # 8+ cyfr pod rząd
    ("deye-battery_soc-SA3ES233N0Q1X", "battery_soc"),            # mieszany token 10+
])
def test_leak_refuses_without_writing(tmp_path, unique_id, hint):
    src = _write_report(tmp_path, [_entity(unique_id, translation_key=hint)])
    dst = tmp_path / "out.json"
    with pytest.raises(SystemExit):
        tool.main(str(src), str(dst))
    assert not dst.exists()


def test_leftover_placeholder_is_refused():
    assert tool.find_leaks('{"x": "<SN>"}')


@pytest.mark.parametrize("diag", [
    {"nothing": 1},
    {"schema": 1, "inverters": [{"entities": [{"entity_domain": "sensor", "platform": "goodwe"}]}]},
    {"schema": 1, "inverters": [{"entities": [_entity(["not", "text"])]}]},
    {"schema": 1, "inverters": "nope"},
])
def test_malformed_report_refuses_without_writing(tmp_path, diag):
    src = tmp_path / "diag.json"
    src.write_text(json.dumps(diag))
    dst = tmp_path / "out.json"
    with pytest.raises(SystemExit):
        tool.main(str(src), str(dst))
    assert not dst.exists()


def test_existing_fixture_passes_leak_check():
    assert tool.find_leaks(FIXTURE.read_text()) == []


def test_checks_do_not_rely_on_assert():
    # `python -O` usuwa asserty — kontrola wycieku musi być zwykłym wyjątkiem.
    tree = ast.parse(TOOL_SRC.read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Assert)]


def test_library_option_strings_are_not_flagged(tmp_path):
    # Stałe napisy z listy opcji biblioteki (np. kraje/normy) to nie numery seryjne.
    src = _write_report(tmp_path, [_entity("goodwe-safety_country-<SN>", entity_domain="select",
                                           options=["240VacHECO", "Energex30K"])])
    dst = tmp_path / "out.json"
    tool.main(str(src), str(dst))
    assert dst.exists()
