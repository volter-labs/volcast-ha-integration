import pytest

from custom_components.volcast.core.transports import modbus_frames as mf


def test_write_single_matches_reference_frame():
    assert mf.write_single_request(0xF7, 47511, 10).hex() == "f706b997000a89eb"


def test_aa55_write_echo_accepted():
    mf.parse_aa55_write(bytes.fromhex("aa55f706b997000a89eb"), 0xF7, 47511, 10)


@pytest.mark.parametrize("addr,value", [(47512, 10), (47511, 1)])
def test_aa55_write_echo_of_other_register_or_value_rejected(addr, value):
    with pytest.raises(mf.FrameError) as e:
        mf.parse_aa55_write(bytes.fromhex("aa55f706b997000a89eb"), 0xF7, addr, value)
    assert e.value.kind == "echo"


def test_write_exception_2_carries_code():
    with pytest.raises(mf.FrameError) as e:
        mf.parse_aa55_write(bytes.fromhex("aa55f786022393"), 0xF7, 47760, 90)
    assert (e.value.kind, e.value.code) == ("exception", 2)


def test_write_multiple_request_and_echo():
    assert mf.write_multiple_request(1, 148, [130]).hex() == "0110009400010200823ae5"
    mf.parse_rtu_write(bytes.fromhex("0110009400014025"), 1, mf.FC_WRITE_MULTIPLE, 148, 1)


def test_rtu_read_bare_and_aa55_agree():
    body = bytes.fromhex("f7030800003e80000b228e645a")
    assert mf.parse_rtu_read(body, 0xF7, 4) == mf.parse_aa55_read(b"\xaa\x55" + body, 0xF7, 4)


def test_mbap_roundtrip():
    frame = mf.mbap(7, 247, mf.pdu_read(47511, 1))
    assert frame.hex() == "000700000006f703b9970001"
    assert mf.parse_mbap(frame) == (7, 247, mf.pdu_read(47511, 1))


@pytest.mark.parametrize("prefix,expected", [
    (bytes.fromhex("f786"), None), (bytes.fromhex("f78602"), 5),
    (bytes.fromhex("f7030c"), 17), (bytes.fromhex("011000"), 8), (bytes.fromhex("f70600"), 8)])
def test_rtu_frame_length(prefix, expected):
    assert mf.rtu_frame_length(prefix) == expected


def test_write_multiple_bounds():
    with pytest.raises(ValueError):
        mf.write_multiple_request(1, 148, [])
    with pytest.raises(ValueError):
        mf.write_multiple_request(1, 65535, [1, 2])     # wychodzi poza przestrzeń adresów


# ── parytet z testami zapisu firmware'u referencyjnego ────────────────────


def _aa55_echo(unit, addr, value):
    return b"\xaa\x55" + mf.write_single_request(unit, addr, value)


def test_aa55_write_bad_crc_is_crc_error():
    frame = bytearray(_aa55_echo(0xF7, 47511, 1))
    frame[9] ^= 0xFF
    with pytest.raises(mf.FrameError) as e:
        mf.parse_aa55_write(bytes(frame), 0xF7, 47511, 1)
    assert e.value.kind == "crc"


def test_crc_checked_before_echo():
    # Zepsute CRC na echu innej wartości = błąd CRC, nie „echo".
    frame = bytearray(_aa55_echo(0xF7, 47511, 8))
    frame[9] ^= 0xFF
    with pytest.raises(mf.FrameError) as e:
        mf.parse_aa55_write(bytes(frame), 0xF7, 47511, 1)
    assert e.value.kind == "crc"


def test_aa55_write_foreign_unit_is_header_error():
    with pytest.raises(mf.FrameError) as e:
        mf.parse_aa55_write(_aa55_echo(0x11, 47511, 1), 0xF7, 47511, 1)
    assert e.value.kind == "header"


@pytest.mark.parametrize("frame,kind", [
    (bytes.fromhex("aa55f706"), "short"), (bytes.fromhex("aa"), "short"),
    (bytes.fromhex("bb55f706b997000a89eb"), "header")])
def test_aa55_write_short_and_header(frame, kind):
    with pytest.raises(mf.FrameError) as e:
        mf.parse_aa55_write(frame, 0xF7, 47511, 10)
    assert e.value.kind == kind


def test_aa55_write_trailing_bytes_tolerated():
    mf.parse_aa55_write(_aa55_echo(0xF7, 47511, 10) + b"\x00\x00", 0xF7, 47511, 10)


def test_write_response_with_other_function_is_function_error():
    frame = mf.rtu(0xF7, bytes.fromhex("03020001"))
    with pytest.raises(mf.FrameError) as e:
        mf.parse_rtu_write(frame, 0xF7, mf.FC_WRITE_SINGLE, 47511, 1)
    assert e.value.kind == "function"


def test_write_multiple_echo_of_other_count_or_register_rejected():
    echo = mf.rtu(1, bytes.fromhex("1000940002"))            # 148, liczba 2
    with pytest.raises(mf.FrameError) as e:
        mf.parse_rtu_write(echo, 1, mf.FC_WRITE_MULTIPLE, 148, 1)
    assert e.value.kind == "echo"
    with pytest.raises(mf.FrameError) as e:
        mf.parse_rtu_write(echo, 1, mf.FC_WRITE_MULTIPLE, 149, 2)
    assert e.value.kind == "echo"


def test_write_exception_on_fc16():
    with pytest.raises(mf.FrameError) as e:
        mf.parse_rtu_write(mf.rtu(1, bytes([0x90, 0x04])), 1, mf.FC_WRITE_MULTIPLE, 148, 1)
    assert (e.value.kind, e.value.code) == ("exception", 4)


@pytest.mark.parametrize("unit,addr,value", [(256, 1, 1), (-1, 1, 1), (1, 65536, 1), (1, -1, 1),
                                             (1, 1, 65536), (1, 1, -1)])
def test_write_single_rejects_out_of_range(unit, addr, value):
    with pytest.raises(ValueError):
        mf.write_single_request(unit, addr, value)


def test_write_multiple_limits():
    assert len(mf.pdu_write_multiple(0, [0] * 123)) == 6 + 246
    with pytest.raises(ValueError):
        mf.pdu_write_multiple(0, [0] * 124)
    with pytest.raises(ValueError):
        mf.pdu_write_multiple(0, [70000])
    assert mf.pdu_write_multiple(65534, [1, 2])[:5] == bytes.fromhex("10fffe0002")


def test_pdu_parsers():
    assert mf.parse_pdu_read(bytes.fromhex("0304000a0014"), 2) == [10, 20]
    mf.parse_pdu_write(bytes.fromhex("06b997000a"), mf.FC_WRITE_SINGLE, 47511, 10)
    with pytest.raises(mf.FrameError) as e:
        mf.parse_pdu_read(bytes.fromhex("8302"), 1)
    assert (e.value.kind, e.value.code) == ("exception", 2)
    with pytest.raises(mf.FrameError) as e:
        mf.parse_pdu_read(bytes.fromhex("0304000a"), 2)
    assert e.value.kind == "short"
    with pytest.raises(mf.FrameError) as e:
        mf.parse_pdu_read(bytes.fromhex("0302000a"), 2)
    assert e.value.kind == "length"
    with pytest.raises(mf.FrameError) as e:
        mf.parse_pdu_write(bytes.fromhex("06b997000b"), mf.FC_WRITE_SINGLE, 47511, 10)
    assert e.value.kind == "echo"


@pytest.mark.parametrize("frame,kind", [
    (bytes.fromhex("0007000000"), "short"),
    (bytes.fromhex("000700010006f703b9970001"), "protocol"),        # identyfikator protokołu ≠ 0
    (bytes.fromhex("000700000007f703b9970001"), "length"),          # długość niezgodna z ramką
    (bytes.fromhex("000700000001f7"), "length"),                    # sam unit, bez PDU
])
def test_parse_mbap_rejects(frame, kind):
    with pytest.raises(mf.FrameError) as e:
        mf.parse_mbap(frame)
    assert e.value.kind == kind


def test_mbap_bounds():
    with pytest.raises(ValueError):
        mf.mbap(65536, 1, b"\x03")
    with pytest.raises(ValueError):
        mf.mbap(1, 1, b"")


def test_rtu_frame_length_unknown_function_raises():
    with pytest.raises(mf.FrameError) as e:
        mf.rtu_frame_length(bytes.fromhex("f70400"))
    assert e.value.kind == "function"
    assert mf.rtu_frame_length(bytes.fromhex("f790")) is None
    assert mf.rtu_frame_length(bytes.fromhex("f79004")) == 5
