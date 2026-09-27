from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.volcast.core.engines.time_window import compress
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage, decode, encode_writes
from custom_components.volcast.core.slot import parse_schedule

DEYE = load_builtin("deye-sg")


def test_draft_and_shape():
    assert (DEYE.status, DEYE.control_model, DEYE.tou_programs, DEYE.time_step_min) == (
        "draft", "time_window", 6, 5)
    assert DEYE.intent("sell") is None and DEYE.intent("discharge_forced") is None
    assert DEYE.raw["capabilities"]["sell_from_battery"] is False


def test_identify_never_reads_serial():
    # Rejestry 3-7 to numer seryjny (10 znaków ASCII), nie model — identyfikacja nie może
    # po niego sięgać, nawet pośrednio przez zakres jakiegoś innego rejestru.
    ident = DEYE.raw["identify"]
    assert "model_register" not in ident
    assert "model_regex" not in ident
    for name, spec in ident["registers"].items():
        addr = spec["addr"]
        words = 2 if spec["type"] in ("u32", "i32", "f32") else 1
        assert addr + words <= 3 or addr > 7, f"{name} nachodzi na numer seryjny (3-7)"


def test_lo_hi_word_order():
    img = RegisterImage.from_blocks({16: [0x86A0, 0x0001]})
    assert decode({"addr": 16, "type": "u32", "word_order": "lo_hi"}, img) == 100000


def test_day_plan_encodes_into_program_registers():
    day0 = datetime(2026, 9, 1, 22, tzinfo=timezone.utc)
    slots = [{"from": (day0 + timedelta(hours=h)).isoformat(), "to": (day0 + timedelta(hours=h + 1)).isoformat(),
              "mode": "charge" if h in (2, 3, 4) else "self_consume",
              "charge_source": "grid" if h in (2, 3, 4) else None,
              "power_w": 3000 if h in (2, 3, 4) else None, "soc_target": 90 if h in (2, 3, 4) else None,
              "price_pln_kwh": 0.3 if h in (2, 3, 4) else 0.6} for h in range(24)]
    r = compress(parse_schedule({"schedule_id": "t", "slots": slots}), day0, DEYE,
                 soc_reserve=15.0, rated_power_w=8000.0, tz=ZoneInfo("Europe/Warsaw"))
    cur = RegisterImage.from_blocks({172: [0] * 6})
    ws = {w.key: w for w in encode_writes(r.params(), DEYE, current=cur)}
    charge = [i for i, p in enumerate(r.programs, start=1) if p.grid_charge]
    assert len(charge) == 1
    i = charge[0]
    assert (ws[f"tou.{i}.start"].value, ws[f"tou.{i}.soc"].value, ws[f"tou.{i}.power_w"].value) == (200, 90, 3000)
    assert ws[f"tou.{i}.grid_charge"].addr == 172 + i - 1
    assert {w.addr for w in ws.values()} <= set(range(148, 154)) | set(range(154, 160)) | \
        set(range(166, 172)) | set(range(172, 178))


def test_deye_modbus_is_draft_with_fc16_and_tou_enable_146():
    m = DEYE.modbus
    assert (m.status, m.write_function, m.max_read_registers) == ("draft", 16, 100)
    assert m.transport_options["solarman_v5"] == {"port": 8899, "timeout_ms": 3000, "gap_ms": 200}
    assert m.transport_options["modbus_tcp"] == {"port": 502, "timeout_ms": 2000, "gap_ms": 100}
    assert m.transport_options["modbus_rtu"] == {"port": 8899, "timeout_ms": 2000, "gap_ms": 100}
    assert m.identify_reads == ((0, 1), (20, 2))
    assert m.probe_keys == ("tou",)
    assert DEYE.raw["write"]["tou_enable"] == {"addr": 146, "enable_bit": 0, "day_mask": 254}
    assert DEYE.raw["read"]["tou_enabled"] == {"addr": 146, "type": "u16"}
    assert DEYE.raw["write"]["tou_program"]["grid_charge"] == {"addr": 172, "bit": 0}
    assert (DEYE.nvm_budget.window_s, DEYE.nvm_budget.per_key, DEYE.nvm_budget.total) == (86400.0, 48, 400)
    refs = " ".join(s["ref"] for s in DEYE.raw["sources"])
    assert "three_phase_common.py" in refs and "deye_p3.yaml" in refs and "marklabs" in refs.lower()
