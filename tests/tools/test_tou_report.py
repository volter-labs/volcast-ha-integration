import json

from tools.tou_report import load_builtin, run, summarize, with_programs


def _entry(i, prices):
    slots = [{"from": f"2026-09-0{i}T{h:02d}:00:00Z", "to": f"2026-09-0{i}T{h + 1:02d}:00:00Z" if h < 23
              else f"2026-09-0{i + 1}T00:00:00Z",
              "mode": "charge" if p < 0.3 else "self_consume",
              "charge_source": "grid" if p < 0.3 else None,
              "power_w": 2000 if p < 0.3 else None, "price_pln_kwh": p} for h, p in enumerate(prices)]
    return {"account": f"a{i}", "schedule_date": f"2026-09-0{i}", "max_charge_rate_w": 5000,
            "schedule": {"schedule_id": "x", "generated_at": f"2026-09-0{i}T00:00:00Z", "slots": slots,
                         "fallback": {"mode": "self_consume", "soc_reserve": 15}}}


def test_summarize():
    s = summarize([0.0, 1.0, 2.0, 3.0])
    assert (s["n"], s["median"], s["max"], s["zero_share"]) == (4, 1.5, 3.0, 0.25)


def test_with_programs_changes_n_everywhere():
    p = with_programs(load_builtin("deye-sg"), 4)
    assert p.tou_programs == 4 and p.raw["write"]["tou_program"]["count"] == 4


def test_fewer_programs_never_lose_less():
    prices = [0.2, 0.6, 0.25, 0.6, 0.1, 0.6, 0.28, 0.6] + [0.6] * 16
    out = run([_entry(1, prices), _entry(2, list(reversed(prices)))], [4, 6, 8], load_builtin("deye-sg"))
    assert out[4]["lost"]["median"] >= out[6]["lost"]["median"] >= out[8]["lost"]["median"]
    assert out[6]["plans"] == 2
    json.dumps(out)   # wynik musi dać się zserializować do raportu
