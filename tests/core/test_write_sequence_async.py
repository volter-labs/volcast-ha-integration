import asyncio

import pytest

from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import encode_writes
from custom_components.volcast.core.write_sequence import (DENIED, ERROR, OK, UNSUPPORTED,
                                                           async_run_writes, run_writes)
from tests.core.golden import load_golden, params_from_golden

GW = load_builtin("goodwe-et")
VECTORS = load_golden("applier")["vectors"]
KIND = {"unsupported": UNSUPPORTED, "error": ERROR}


@pytest.mark.parametrize("vec", VECTORS, ids=[str(i) for i in range(len(VECTORS))])
def test_async_twin_matches_golden_applier(vec):
    def outcome(w):
        if vec["fail_kind"] == "denied":
            return DENIED
        return KIND[vec["fail_kind"]] if vec["fail_reg"] == w.addr else OK

    async def awrite(w):
        return outcome(w)

    ws = encode_writes(params_from_golden(vec["params"], GW), GW)
    sync_rep = run_writes(ws, outcome)
    async_rep = asyncio.run(async_run_writes(ws, awrite))
    assert async_rep == sync_rep


def test_async_twin_reports_exception():
    vec = VECTORS[0]
    ws = encode_writes(params_from_golden(vec["params"], GW), GW)
    seen = []

    async def flaky(w):
        if w.key == "power_w":
            raise RuntimeError("boom")
        return OK

    rep = asyncio.run(async_run_writes(ws, flaky, on_exception=lambda k, e: seen.append((k, type(e).__name__))))
    assert rep.failed == ["power_w"] and rep.mode_held is True
    assert rep.errors == {"power_w": "RuntimeError"}
    assert seen == [("power_w", "RuntimeError")]


def test_sync_on_exception_keeps_errors_and_is_called():
    vec = VECTORS[0]
    ws = encode_writes(params_from_golden(vec["params"], GW), GW)
    seen = []

    def flaky(w):
        if w.key == "soc_min":
            raise ValueError("bad")
        return OK

    rep = run_writes(ws, flaky, on_exception=lambda k, e: seen.append((k, type(e).__name__)))
    assert rep.errors == {"soc_min": "ValueError"} and rep.mode_held is True
    assert seen == [("soc_min", "ValueError")]


def test_failing_on_exception_never_aborts_the_sequence():
    # Wadliwy callback logujący nie może przerwać pętli w połowie ani zgubić raportu.
    vec = VECTORS[0]
    ws = encode_writes(params_from_golden(vec["params"], GW), GW)

    def boom(_k, _e):
        raise KeyError("logger broke")

    async def aflaky(w):
        if w.key == "mode":
            raise RuntimeError("x")
        return OK

    def flaky(w):
        if w.key == "mode":
            raise RuntimeError("x")
        return OK

    arep = asyncio.run(async_run_writes(ws, aflaky, on_exception=boom))
    srep = run_writes(ws, flaky, on_exception=boom)
    assert arep == srep
    assert arep.failed == ["mode"] and arep.errors == {"mode": "RuntimeError"}
    assert arep.written == ["soc_min", "soc_max", "power_w", "export_limit_w", "export_limit_enabled"]


def test_cancellation_is_not_swallowed():
    # Anulowanie (np. zdjęcie integracji) to nie błąd zapisu — musi przejść dalej.
    vec = VECTORS[0]
    ws = encode_writes(params_from_golden(vec["params"], GW), GW)

    async def cancelled(w):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(async_run_writes(ws, cancelled))
