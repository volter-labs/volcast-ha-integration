import json

from tools.tou_report import DEFAULT_RATED_W, load_builtin, run, summarize, to_markdown, verdict, with_programs


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


def _sell_evening_entry():
    # Deye nie ma wymuszonej sprzedaży → sloty `sell` degradują się do samokonsumpcji.
    # Strata z degradacji nie zależy od N, więc nie może obciążać kryterium liczby programów.
    slots = []
    for h in range(24):
        sell = 17 <= h < 23
        slots.append({"from": f"2026-09-05T{h:02d}:00:00Z",
                      "to": f"2026-09-05T{h + 1:02d}:00:00Z" if h < 23 else "2026-09-06T00:00:00Z",
                      "mode": "discharge" if sell else "self_consume",
                      "discharge_purpose": "sell" if sell else None,
                      "power_w": 3000 if sell else None, "price_pln_kwh": 1.5 if sell else 0.5})
    return {"account": "s", "schedule_date": "2026-09-05", "max_charge_rate_w": 5000,
            "schedule": {"schedule_id": "s", "generated_at": "2026-09-05T00:00:00Z", "slots": slots,
                         "fallback": {"mode": "self_consume", "soc_reserve": 15}}}


def test_criterion_uses_merge_loss_not_degrade_loss():
    out = run([_sell_evening_entry()], [4, 6], load_builtin("deye-sg"))
    for n in (4, 6):
        assert out[n]["merge_loss"]["median"] == 0
        assert out[n]["lost"]["median"] > 0
        assert out[n]["degrade"]["median"] > 0
        assert out[n]["verdict"] == "PASS"


def test_verdict_thresholds():
    assert verdict({"n": 3, "median": 0.05, "p90": 0.4}) == "PASS"
    assert verdict({"n": 3, "median": 0.10, "p90": 0.4}) == "FAIL"
    assert verdict({"n": 3, "median": 0.05, "p90": 0.50}) == "FAIL"
    assert verdict({"n": 0}) == "BRAK DANYCH"


def test_merge_loss_monotonic_in_n():
    prices = [0.2, 0.6, 0.25, 0.6, 0.1, 0.6, 0.28, 0.6] + [0.6] * 16
    out = run([_entry(1, prices), _entry(2, list(reversed(prices)))], [4, 6, 8], load_builtin("deye-sg"))
    assert out[4]["merge_loss"]["median"] >= out[6]["merge_loss"]["median"] >= out[8]["merge_loss"]["median"]
    assert out[4]["merge_loss"]["median"] > 0


def test_skips_counted_by_reason_without_abort():
    prices = [0.6] * 24
    good1, good2 = _entry(1, prices), _entry(2, prices)
    bad_power = _entry(3, prices)
    bad_power["schedule"]["slots"][0]["power_w"] = "dużo"          # kontrakt → InvalidSchedule
    off_step = _entry(4, prices)
    off_step["schedule"]["slots"][0]["from"] = "2026-09-04T00:07:00Z"  # silnik → ValueError (poza siatką)
    empty = _entry(5, prices)
    empty["schedule"]["slots"] = []
    missing = {"account": "m", "schedule_date": "2026-09-06", "max_charge_rate_w": 5000}
    out = run([good1, bad_power, off_step, empty, missing, good2], [6], load_builtin("deye-sg"))
    assert out[6]["plans"] == 2
    assert out[6]["skipped"] == {"missing": 1, "invalid": 1, "empty": 1, "engine": 1}


def test_default_rated_power_is_counted_and_shown():
    prices = [0.6] * 24
    a, b = _entry(1, prices), _entry(2, prices)
    a["max_charge_rate_w"] = None
    out = run([a, b], [6], load_builtin("deye-sg"))
    assert out[6]["assumed_rated_w"] == {"count": 1, "value": DEFAULT_RATED_W}
    assert DEFAULT_RATED_W == 5000
    md = to_markdown(out)
    assert "5000" in md and "PASS" in md
