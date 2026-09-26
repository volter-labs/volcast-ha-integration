import pytest

from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import encode_writes
from custom_components.volcast.core.write_sequence import (
    DENIED, ERROR, OK, UNSUPPORTED, run_writes,
)
from tests.core.golden import load_golden, params_from_golden

GW = load_builtin("goodwe-et")
VECTORS = load_golden("applier")["vectors"]
KIND = {"unsupported": UNSUPPORTED, "error": ERROR}


@pytest.mark.parametrize("vec", VECTORS, ids=[str(i) for i in range(len(VECTORS))])
def test_matches_reference_applier(vec):
    calls = []

    def fake(w):
        calls.append([w.addr, w.value])
        if vec["fail_kind"] == "denied":
            return DENIED
        if vec["fail_reg"] == w.addr:
            return KIND[vec["fail_kind"]]
        return OK

    ws = encode_writes(params_from_golden(vec["params"], GW), GW)
    rep = run_writes(ws, fake)
    assert calls == vec["writes"]
    assert rep.written == vec["written"]
    assert rep.unsupported == vec["unsupported"]
    assert rep.mode_held == vec["mode_held"]
    all_keys = [w.key for w in ws]
    expected_failed = [k for k in all_keys
                       if k not in rep.written and k not in rep.unsupported
                       and not (k == "mode" and vec["mode_held"])]
    assert rep.failed == expected_failed


def test_mode_is_always_last_call():
    vec = VECTORS[0]
    seen = []
    run_writes(encode_writes(params_from_golden(vec["params"], GW), GW), lambda w: seen.append(w.key) or OK)
    assert seen[-1] == "mode"


def test_writer_exception_is_treated_as_error_and_holds_mode():
    # Rzucający callback (np. wyjątek transportu) nie może zjeść raportu ani
    # przerwać pętli — traktujemy go jak ERROR dla danego klucza.
    vec = VECTORS[0]
    ws = encode_writes(params_from_golden(vec["params"], GW), GW)

    def flaky(w):
        if w.key == "power_w":
            raise RuntimeError("boom")
        return OK

    rep = run_writes(ws, flaky)
    assert rep.written == ["soc_min", "soc_max", "export_limit_w", "export_limit_enabled"]
    assert rep.failed == ["power_w"]
    assert rep.mode_held is True


def test_writer_exception_on_mode_is_reported_failed():
    vec = VECTORS[0]
    ws = encode_writes(params_from_golden(vec["params"], GW), GW)

    def flaky(w):
        if w.key == "mode":
            raise RuntimeError("boom")
        return OK

    rep = run_writes(ws, flaky)
    assert rep.written == ["soc_min", "soc_max", "power_w", "export_limit_w", "export_limit_enabled"]
    assert rep.failed == ["mode"]
    assert rep.mode_held is False


def test_writer_exception_keeps_class_name_per_failed_key():
    # Połknięty wyjątek musi zostawić ślad w raporcie — bez tego „failed" nie mówi,
    # czy padł transport, czy odmówił falownik.
    vec = VECTORS[0]
    ws = encode_writes(params_from_golden(vec["params"], GW), GW)

    class TransportTimeout(Exception):
        pass

    def flaky(w):
        if w.key == "power_w":
            raise TransportTimeout("10.0.0.5 did not answer")
        if w.key == "soc_max":
            return DENIED
        return OK

    rep = run_writes(ws, flaky)
    assert rep.failed == ["soc_max", "power_w"]
    assert rep.errors == {"power_w": "TransportTimeout"}   # klasa, bez treści (może mieć adres)


def test_no_errors_recorded_without_exceptions():
    vec = VECTORS[0]
    rep = run_writes(encode_writes(params_from_golden(vec["params"], GW), GW), lambda w: ERROR)
    assert rep.failed and rep.errors == {}
