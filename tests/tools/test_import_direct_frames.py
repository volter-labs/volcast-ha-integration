"""Import ramek z diagnostyki do złotych wektorów: format `goodwe_et/frames.json`, odmowa przy serialu."""
import json

import pytest

from tools.golden import import_direct_frames as imp


def _diag(frames):
    return {"control": {"direct": {"profile": "deye-sg", "transport": "solarman_v5", "frames": frames}}}


FRAME = {"offset": 0, "count": 2, "request": "0103000000020c0b", "response": "010304000500001b8b"}


def test_import_writes_golden_format(tmp_path):
    src = tmp_path / "diag.json"
    src.write_text(json.dumps(_diag([FRAME, {**FRAME, "offset": 586, "response": None}])))
    out = tmp_path / "frames.json"
    imp.main([str(src), "--profile", "deye-sg", "--out", str(out)])
    doc = json.loads(out.read_text())
    assert doc["block_0_2"] == {"offset": 0, "count": 2, "request": FRAME["request"], "response": FRAME["response"],
                                "valid": True}
    assert doc["block_586_2"]["valid"] is False and doc["block_586_2"]["response"] == ""


@pytest.mark.parametrize("bad", [
    {"response": "0103" + "10" + b"12345ETU678W9012".hex()},          # serial GoodWe w bajtach
    {"response": "0103" + "0a" + b"2345678901".hex()},                # 10-cyfrowy numer loggera w bajtach
    {"note": "logger 1234567890"},                                     # w tekście
    {"note": "11111111-2222-3333-4444-555555555555"},                  # UUID
    {"request": "a5" + "00" * 6 + "d2029649" + "00" * 20},            # V5 z niezerowym numerem loggera
])
def test_import_refuses_serial_like_content(tmp_path, bad):
    src = tmp_path / "diag.json"
    src.write_text(json.dumps(_diag([{**FRAME, **bad}])))
    out = tmp_path / "frames.json"
    with pytest.raises(SystemExit):
        imp.main([str(src), "--profile", "deye-sg", "--out", str(out)])
    assert not out.exists()


def test_import_refuses_without_frames(tmp_path):
    src = tmp_path / "diag.json"
    src.write_text(json.dumps({"control": {"direct": {"profile": "deye-sg"}}}))
    with pytest.raises(SystemExit):
        imp.main([str(src), "--profile", "deye-sg", "--out", str(tmp_path / "x.json")])
