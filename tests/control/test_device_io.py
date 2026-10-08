"""Szew wejścia/wyjścia urządzenia w wykonawcy: tryb encji bez zmian zachowania."""
import asyncio
import math
from datetime import timedelta
from types import SimpleNamespace

from custom_components.volcast.control.device_io import EntityIO, Reading
from custom_components.volcast.control.executor import VolcastExecutor
from custom_components.volcast.control.ha_writer import EntityServiceWriter
from custom_components.volcast.control.store import ControlStore
from custom_components.volcast.core.control.readings import RawState, normalize_readings
from custom_components.volcast.core.control.target import EntityTarget

from .ha_fakes import GOODWE_ENTITIES as E, NOW, goodwe_hass
from .test_executor import GW, Clock, make, plan, ready


def _legacy_read(hass, profile, domain, mapped, now_utc):
    """Kopia dawnego `VolcastExecutor._read` — wyrocznia testu przenosin."""
    raw, units, attrs, soc_state, raw_mode = {}, {}, {}, None, None
    for key, eid in mapped.items():
        st = hass.states.get(eid)
        if st is None:
            continue
        unit = st.attributes.get("unit_of_measurement")
        raw[key], units[key] = RawState(st.state, unit), unit
        attrs[eid] = dict(st.attributes)
        if key == "mode" and isinstance(st.state, str):
            raw_mode = st.state
        if key == "soc":
            soc_state = st
    readings = normalize_readings(raw, profile, domain) if domain else {}
    age = math.inf
    ts = getattr(soc_state, "last_reported", None) or getattr(soc_state, "last_updated", None) if soc_state else None
    if ts is not None:
        age = (now_utc - ts).total_seconds()
        age = 0.0 if -5.0 < age < 0.0 else age
    return readings, raw_mode, units, attrs, age


def test_entity_io_read_matches_legacy_reading():
    for over in ({}, {"temp": "82.4"}, {"mode": "eco_mode_99"}):
        h = goodwe_hass(**over)
        io = EntityIO(h, GW.profile, "goodwe", E, EntityServiceWriter(h))
        now = NOW + timedelta(seconds=30)
        rd = io.read(now)
        assert isinstance(rd, Reading)
        assert (rd.readings, rd.raw_mode, rd.units, rd.attrs, rd.soc_age_s) == _legacy_read(
            h, GW.profile, "goodwe", E, now)
        assert isinstance(io.target(rd), EntityTarget) and io.target(rd).ents.readings == rd.for_cycle()


def test_executor_default_io_is_entity_io(monkeypatch):
    _, ex = make(monkeypatch=monkeypatch)
    assert isinstance(ex.io, EntityIO) and ex.io.kind == "entities" and ex._writer is ex.io.writer
    assert ex.io.owner() == {"profile": "goodwe-et", "domain": "goodwe", "mode_entity": E["mode"]}


class _DirectLikeIO(EntityIO):
    kind = "direct"


def test_gates_open_requires_matching_io_kind(monkeypatch):
    h = goodwe_hass()
    entry = SimpleNamespace(entry_id="e1", options={"control_mode": "entities"})
    monkeypatch.setattr("custom_components.volcast.control.executor.control_verified", lambda *_: True)
    writer = EntityServiceWriter(h)
    io = _DirectLikeIO(h, GW.profile, "goodwe", E, writer)
    ex = VolcastExecutor(h, entry, choice=GW, mapped=E, rated_power_w=8000.0, store=ControlStore(h, "e1"),
                         io=io, clock=Clock(), utcnow=lambda: NOW + timedelta(seconds=30))

    async def go():
        await ready(ex, raw=plan())
        await ex.async_tick()
    asyncio.run(go())
    assert h.states.get(E["mode"]).state != "sell_power" and not ex._gates_open()
    assert ex.last_decision.status != "write"


def test_direct_io_resend_needs_budget_left():
    # Ponowna wysyłka zapisu (UDP, po ciszy) tylko w budżecie NVM — sprawdzane PRZED wysłaniem.
    from custom_components.volcast.control.device_io import DirectIO
    from custom_components.volcast.core.guard_state import WriteBudget
    from custom_components.volcast.core.profile import load_builtin
    io = DirectIO(SimpleNamespace(client=None), load_builtin("goodwe-et"), trial=False, salt=bytes(16),
                  now_wall=lambda: 1000.0)
    assert io._may_resend("mode") is True                       # bez budżetu — jak dotąd
    io.bind_budget(WriteBudget(per_key=1, total=10))
    assert io._may_resend("mode") is True
    io._on_send("mode")
    assert io._may_resend("mode") is False and io._may_resend("power_w") is True
