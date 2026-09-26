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

    rep = run_writes(encode_writes(params_from_golden(vec["params"], GW), GW), fake)
    assert calls == vec["writes"]
    assert rep.written == vec["written"]
    assert rep.unsupported == vec["unsupported"]
    assert rep.mode_held == vec["mode_held"]


def test_mode_is_always_last_call():
    vec = VECTORS[0]
    seen = []
    run_writes(encode_writes(params_from_golden(vec["params"], GW), GW), lambda w: seen.append(w.key) or OK)
    assert seen[-1] == "mode"
