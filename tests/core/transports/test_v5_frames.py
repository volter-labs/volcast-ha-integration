"""Ramki Solarman V5 — układ zgodny z publicznym opisem protokołu (pysolarmanv5)."""
import pytest

from custom_components.volcast.core.transports import v5_frames as v5

RTU_READ = bytes.fromhex("0103009400068424")          # unit 1, FC3, 148, 6
REQ = bytes.fromhex("a5170010452a00d202964902000000000000000000000000000001030094000684249115")
RESP = bytes.fromhex("a51f0010152a01d2029649020100000000000000000000000001030c000001f403e8"
                     "051406a40898ab3d6015")
SERIAL = 1234567890


def _frame(control: int, seq_bytes: bytes, serial: int, payload: bytes) -> bytes:
    body = (len(payload).to_bytes(2, "little") + control.to_bytes(2, "little") + seq_bytes
            + serial.to_bytes(4, "little") + payload)
    return b"\xa5" + body + bytes([sum(body) & 0xFF]) + b"\x15"


def _response(rtu: bytes, *, status: int = 1, frame_type: int = 2, seq: int = 0x2A,
              serial: int = SERIAL) -> bytes:
    return _frame(0x1510, bytes([seq, 0x01]), serial, bytes([frame_type, status]) + bytes(12) + rtu)


def test_encode_request_layout():
    assert v5.encode_request(SERIAL, 0x2A, RTU_READ) == REQ


def test_frame_length_from_prefix():
    assert v5.frame_length(REQ[:3]) == len(REQ)
    assert v5.frame_length(REQ[:2]) is None


def test_decode_response_returns_rtu():
    rtu = v5.decode_response(RESP, SERIAL, 0x2A)
    assert rtu[:3] == bytes.fromhex("01030c")


@pytest.mark.parametrize("mutate,kind", [
    (lambda b: b[:-1] + b"\x00", "end"),
    (lambda b: b[:-2] + bytes([(b[-2] + 1) & 0xFF]) + b[-1:], "checksum"),
    (lambda b: b"\x00" + b[1:], "start"),
])
def test_corrupt_frames_rejected(mutate, kind):
    with pytest.raises(v5.V5Error) as e:
        v5.decode_response(mutate(RESP), SERIAL, 0x2A)
    assert e.value.kind == kind


def test_other_logger_serial_rejected():
    with pytest.raises(v5.V5Error) as e:
        v5.decode_response(RESP, SERIAL + 1, 0x2A)
    assert e.value.kind == "serial"


def test_stale_sequence_rejected():
    with pytest.raises(v5.V5Error) as e:
        v5.decode_response(RESP, SERIAL, 0x2B)
    assert e.value.kind == "sequence"


def test_heartbeat_control_code_is_not_a_response():
    hb = bytearray(RESP); hb[3:5] = (0x4710).to_bytes(2, "little")
    body = bytes(hb[1:-2]); hb[-2] = sum(body) & 0xFF
    assert v5.control_code(bytes(hb)) == 0x4710
    with pytest.raises(v5.V5Error) as e:
        v5.decode_response(bytes(hb), SERIAL, 0x2A)
    assert e.value.kind == "control"


# ── ustalenia ze źródła ───────────────────────────────────────────────────


@pytest.mark.parametrize("rtu", [b"", bytes.fromhex("01"), bytes.fromhex("02"), bytes.fromhex("0183")])
def test_asleep_error_response_kind(rtu):
    # Logger przy uśpionym falowniku: ładunek odpowiedzi bez (pełnej) ramki RTU.
    with pytest.raises(v5.V5Error) as e:
        v5.decode_response(_response(rtu), SERIAL, 0x2A)
    assert e.value.kind == "asleep"


def test_exception_rtu_frame_of_five_bytes_is_returned():
    rtu = bytes.fromhex("0183020d84")                  # wyjątek 2 — pełna ramka RTU, 5 B
    assert v5.decode_response(_response(rtu), SERIAL, 0x2A) == rtu


def test_trailing_bytes_after_rtu_tolerated():
    rtu = RESP[25:-2]
    # Podwójne CRC (dwa bajty 0x0000 za poprawną ramką RTU) — zdejmowane jak w źródle.
    assert v5.decode_response(_response(rtu + b"\x00\x00"), SERIAL, 0x2A) == rtu
    # Inne nadmiarowe bajty zostają — rozbiór RTU i tak czyta tylko zadeklarowaną długość.
    assert v5.decode_response(_response(rtu + b"\x11\x22"), SERIAL, 0x2A) == rtu + b"\x11\x22"


def test_trailing_zeroes_without_a_valid_inner_crc_are_kept():
    # 0x0000 na końcu, ale bez niego CRC się nie zgadza → to nie podwójne CRC.
    rtu = bytes.fromhex("0106000100000000")
    assert v5.decode_response(_response(rtu), SERIAL, 0x2A) == rtu


@pytest.mark.parametrize("status", [0, 1, 2, 0x80, 0xFF])
def test_status_byte_rule_matches_source(status):
    # Źródło nie opisuje żadnej wartości statusu jako błędu — żadna nie jest odrzucana.
    rtu = RESP[25:-2]
    assert v5.decode_response(_response(rtu, status=status), SERIAL, 0x2A) == rtu


@pytest.mark.parametrize("frame_type", [0, 1, 3])
def test_non_inverter_frame_type_rejected(frame_type):
    with pytest.raises(v5.V5Error) as e:
        v5.decode_response(_response(RESP[25:-2], frame_type=frame_type), SERIAL, 0x2A)
    assert e.value.kind == "frame_type"


def test_sequence_matches_first_byte_only():
    # Drugi bajt sekwencji podbija logger — liczy się tylko pierwszy (echo naszego).
    assert v5.decode_response(RESP, SERIAL, 0x012A) == RESP[25:-2]
    frame = _frame(0x1510, bytes([0x2A, 0x77]), SERIAL, bytes([2, 1]) + bytes(12) + RESP[25:-2])
    assert v5.decode_response(frame, SERIAL, 0x2A) == RESP[25:-2]


def test_protocol_frame_is_control_even_with_other_sequence():
    hb = _frame(0x4710, bytes([0x99, 0x00]), SERIAL, b"\x00")
    with pytest.raises(v5.V5Error) as e:
        v5.decode_response(hb, SERIAL, 0x2A)
    assert e.value.kind == "control"


def test_short_and_truncated_frames():
    for frame in (b"", RESP[:12], RESP[:-1]):
        with pytest.raises(v5.V5Error) as e:
            v5.decode_response(frame, SERIAL, 0x2A)
        assert e.value.kind == "short"


def test_control_code():
    assert v5.control_code(RESP) == v5.CTRL_RESPONSE
    assert v5.control_code(REQ) == v5.CTRL_REQUEST
    assert v5.control_code(RESP[:4]) is None
    assert v5.control_code(b"\x00" + RESP[1:]) is None


def _source_ack(frame: bytes, serial: int, now: int) -> bytes:
    # Odtworzenie odpowiedzi czasu ze źródła: nagłówek (długość 10, kod − 0x30, sekwencja z
    # pierwszym bajtem +1, nasz numer loggera) + 0x0100 (<H) + czas uniksowy (<I) + 0 (<I).
    seq = bytes([(frame[5] + 1) & 0xFF, frame[6]])
    payload = (0x0100).to_bytes(2, "little") + now.to_bytes(4, "little") + bytes(4)
    return _frame(0x10 | ((frame[4] - 0x30) << 8), seq, serial, payload)


@pytest.mark.parametrize("code", [0x41, 0x42, 0x43, 0x47, 0x48])
def test_protocol_ack_matches_source_or_none(code):
    frame = _frame(0x10 | (code << 8), bytes([0x05, 0x33]), SERIAL, bytes.fromhex("0100"))
    ack = v5.protocol_ack(frame, SERIAL, now=1_700_000_000)
    assert ack == _source_ack(frame, SERIAL, 1_700_000_000)
    assert v5.control_code(ack) == 0x10 | ((code - 0x30) << 8)
    assert v5.frame_length(ack[:3]) == len(ack) == 23


def test_protocol_ack_wraps_sequence_byte():
    frame = _frame(0x4710, bytes([0xFF, 0x00]), SERIAL, b"")
    assert v5.protocol_ack(frame, SERIAL, now=0)[5:7] == bytes([0x00, 0x00])


@pytest.mark.parametrize("frame", [
    RESP, REQ,                                                      # odpowiedź i żądanie Modbus — bez potwierdzenia
    _frame(0x4610, b"\x01\x00", SERIAL, b""),                      # kod spoza protokołu
    _frame(0x4720, b"\x01\x00", SERIAL, b""),                      # zły sufiks kodu
    _frame(0x4710, b"\x01\x00", SERIAL + 1, b""),                  # inny logger
    _frame(0x4710, b"\x01\x00", SERIAL, b"")[:-2] + b"\x00\x15",   # zła suma kontrolna
    b"\xa5\x00",                                                    # za krótka
])
def test_protocol_ack_none_for_other_frames(frame):
    assert v5.protocol_ack(frame, SERIAL, now=1) is None


@pytest.mark.parametrize("serial,seq,rtu", [
    (-1, 1, RTU_READ), (2 ** 32, 1, RTU_READ), (SERIAL, -1, RTU_READ), (SERIAL, 65536, RTU_READ),
    (True, 1, RTU_READ), (SERIAL, True, RTU_READ), ("1", 1, RTU_READ), (SERIAL, 1, b""),
    (SERIAL, 1, "0103"), (SERIAL, 1, bytes(1012)),
])
def test_encode_request_rejects_bad_input(serial, seq, rtu):
    with pytest.raises(ValueError):
        v5.encode_request(serial, seq, rtu)


def test_encode_request_largest_frame_fits_limit():
    assert len(v5.encode_request(SERIAL, 1, bytes(1024 - 28))) == 1024


def test_frame_length_limits():
    with pytest.raises(v5.V5Error) as e:
        v5.frame_length(b"\x00\x17\x00")
    assert e.value.kind == "start"
    with pytest.raises(v5.V5Error) as e:
        v5.frame_length(b"\xa5" + (1024 - 12).to_bytes(2, "little"))      # 13 + 1012 > 1024
    assert e.value.kind == "length"
    assert v5.frame_length(b"\xa5" + (1024 - 13).to_bytes(2, "little")) == 1024


@pytest.mark.parametrize("serial,seq", [(-1, 1), (2 ** 32, 1), (SERIAL, 65536), (SERIAL, None)])
def test_decode_response_rejects_bad_arguments(serial, seq):
    with pytest.raises(ValueError) as e:
        v5.decode_response(RESP, serial, seq)
    assert not isinstance(e.value, v5.V5Error)


@pytest.mark.parametrize("payload", [b"", b"\x02", b"\x02\x01" + bytes(12)])
def test_empty_or_cut_response_payload_is_asleep(payload):
    frame = _frame(0x1510, bytes([0x2A, 0x01]), SERIAL, payload)
    with pytest.raises(v5.V5Error) as e:
        v5.decode_response(frame, SERIAL, 0x2A)
    assert e.value.kind == "asleep"
