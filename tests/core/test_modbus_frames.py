import json
from pathlib import Path

import pytest

from custom_components.volcast.core.transports.modbus_frames import (
    FrameError, crc16, parse_aa55_read, read_request,
)

FRAMES = json.loads((Path(__file__).resolve().parents[1] / "golden" / "goodwe_et" / "frames.json").read_text())


@pytest.mark.parametrize("name", sorted(FRAMES))
def test_request_bytes_match_recording(name):
    f = FRAMES[name]
    assert read_request(0xF7, f["offset"], f["count"]).hex() == f["request"]


@pytest.mark.parametrize("name", [n for n in sorted(FRAMES) if FRAMES[n]["valid"]])
def test_valid_responses_parse(name):
    f = FRAMES[name]
    assert len(parse_aa55_read(bytes.fromhex(f["response"]), 0xF7, f["count"])) == f["count"]


def test_foreign_block_is_length_error():
    # Falownik odsyła obcy blok rejestrów (inny niż żądany) — to nie jest poprawny odczyt.
    f = FRAMES["soc_upper_47760"]
    with pytest.raises(FrameError) as ei:
        parse_aa55_read(bytes.fromhex(f["response"]), 0xF7, f["count"])
    assert ei.value.kind == "length"


def test_crc_error():
    raw = bytearray(bytes.fromhex(FRAMES["ems_block_47509_4"]["response"]))
    raw[6] ^= 0xFF
    with pytest.raises(FrameError) as ei:
        parse_aa55_read(bytes(raw), 0xF7, 4)
    assert ei.value.kind == "crc"


def test_modbus_exception_2_is_reported_with_code():
    body = bytes([0xF7, 0x83, 0x02])
    c = crc16(body)
    frame = b"\xaa\x55" + body + bytes([c & 0xFF, c >> 8])
    with pytest.raises(FrameError) as ei:
        parse_aa55_read(frame, 0xF7, 1)
    assert (ei.value.kind, ei.value.code) == ("exception", 2)


@pytest.mark.parametrize("frame,kind", [(b"\xaa\x55\xf7", "short"), (b"\x00\x00\xf7\x03\x02\x00\x05\x00\x00", "header")])
def test_short_and_header(frame, kind):
    with pytest.raises(FrameError) as ei:
        parse_aa55_read(frame, 0xF7, 1)
    assert ei.value.kind == kind


def test_trailing_padding_after_valid_frame_is_accepted():
    # Ramka może mieć nadmiarowe bajty za deklarowaną długością danych.
    f = FRAMES["ems_block_47509_4"]
    padded = bytes.fromhex(f["response"]) + b"\x00\x00\x00"
    assert len(parse_aa55_read(padded, 0xF7, f["count"])) == f["count"]


def test_truncated_valid_frame_is_short_not_crc():
    f = FRAMES["ems_block_47509_4"]
    raw = bytes.fromhex(f["response"])
    with pytest.raises(FrameError) as ei:
        parse_aa55_read(raw[:-1], 0xF7, f["count"])
    assert ei.value.kind == "short"


def test_exception_with_other_function_code_bit():
    # Dowolny kod funkcji z ustawionym bitem błędu (0x80) to wyjątek, nie nieznana funkcja.
    body = bytes([0xF7, 0x86, 0x03])
    c = crc16(body)
    frame = b"\xaa\x55" + body + bytes([c & 0xFF, c >> 8])
    with pytest.raises(FrameError) as ei:
        parse_aa55_read(frame, 0xF7, 1)
    assert (ei.value.kind, ei.value.code) == ("exception", 3)


def test_unexpected_function_code_without_error_bit():
    body = bytes([0xF7, 0x04, 0x02, 0x00, 0x05])
    c = crc16(body)
    frame = b"\xaa\x55" + body + bytes([c & 0xFF, c >> 8])
    with pytest.raises(FrameError) as ei:
        parse_aa55_read(frame, 0xF7, 1)
    assert ei.value.kind == "function"


@pytest.mark.parametrize("unit,addr,count", [(-1, 0, 1), (256, 0, 1), (0xF7, -1, 1),
                                              (0xF7, 0x10000, 1), (0xF7, 0, 0), (0xF7, 0, 126)])
def test_read_request_rejects_out_of_range(unit, addr, count):
    with pytest.raises(ValueError):
        read_request(unit, addr, count)


# ── odczyt rejestrów input (FC 4) ──

from custom_components.volcast.core.transports import modbus_frames as mf  # noqa: E402
from tests.sim.device import with_crc  # noqa: E402  (CRC liczone niezależnie, tablicowo)


def test_read_request_default_function_is_3():
    assert mf.pdu_read(13022, 2) == bytes.fromhex("0332de0002")
    assert mf.read_request(1, 13022, 2) == with_crc(bytes.fromhex("010332de0002"))


def test_read_request_input_registers_function_4():
    assert mf.FC_READ_INPUT == 0x04 and mf.READ_FUNCTIONS == (0x03, 0x04)
    assert mf.pdu_read(13022, 2, fc=4) == bytes.fromhex("0432de0002")
    assert mf.read_request(1, 13022, 2, fc=4) == with_crc(bytes.fromhex("010432de0002"))


@pytest.mark.parametrize("fc", [0, 2, 5, 6, 16, True, 4.0])
def test_read_request_rejects_other_functions(fc):
    with pytest.raises(ValueError):
        mf.pdu_read(0, 1, fc=fc)


def test_parse_pdu_read_input_registers():
    assert mf.parse_pdu_read(bytes.fromhex("040400371234"), 2, fc=4) == [0x37, 0x1234]


@pytest.mark.parametrize("pdu,fc", [("040400371234", 3), ("030400371234", 4)])
def test_parse_pdu_read_function_must_echo_request(pdu, fc):
    with pytest.raises(FrameError) as ei:
        mf.parse_pdu_read(bytes.fromhex(pdu), 2, fc=fc)
    assert ei.value.kind == "function"


def test_parse_pdu_read_default_is_holding():
    with pytest.raises(FrameError) as ei:
        mf.parse_pdu_read(bytes.fromhex("040400371234"), 2)
    assert ei.value.kind == "function"


def test_parse_rtu_read_input_registers():
    frame = with_crc(bytes.fromhex("01040400371234"))
    assert mf.parse_rtu_read(frame, 1, 2, fc=4) == [0x37, 0x1234]
    assert mf.rtu_frame_length(frame[:3]) == len(frame) == 9


@pytest.mark.parametrize("body,fc", [("01040400371234", 3), ("01030400371234", 4)])
def test_parse_rtu_read_function_must_echo_request(body, fc):
    with pytest.raises(FrameError) as ei:
        mf.parse_rtu_read(with_crc(bytes.fromhex(body)), 1, 2, fc=fc)
    assert ei.value.kind == "function"


def test_parse_rtu_read_input_length_mismatch():
    with pytest.raises(FrameError) as ei:
        mf.parse_rtu_read(with_crc(bytes.fromhex("01040400371234")), 1, 3, fc=4)
    assert ei.value.kind == "length"
