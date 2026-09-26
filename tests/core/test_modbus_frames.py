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
