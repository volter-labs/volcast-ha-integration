import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from custom_components.volcast.cloud.client import TelemetryResult
from custom_components.volcast.control.telemetry import TelemetrySender, control_block
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin

from .ha_fakes import GOODWE_ENTITIES as E, goodwe_hass

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "control_block.json").read_text())
GW = ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET")
NOW = datetime(2026, 10, 10, 8, 0, 0, tzinfo=timezone.utc)


def _payload():
    return {k: v for k, v in FIXTURE.items() if k not in ("seq", "choice_ack")}


def test_block_matches_fixture_shape():
    meta: dict = {}
    block = control_block(_payload(), FIXTURE["choice_ack"], meta, 1791619200)
    assert set(block) == set(FIXTURE)
    assert block == FIXTURE
    assert isinstance(block["seq"], int) and not isinstance(block["seq"], bool)


def test_block_without_ack_has_no_choice_ack():
    block = control_block({"conflicts": []}, None, {}, 100)
    assert block == {"seq": 100, "conflicts": []}


def test_seq_stays_when_nothing_changed_and_grows_on_change():
    meta: dict = {}
    assert control_block(_payload(), None, meta, 1000)["seq"] == 1000
    assert control_block(_payload(), None, meta, 1005)["seq"] == 1000        # bez zmiany: ten sam seq
    changed = _payload()
    changed["conflicts"] = []
    assert control_block(changed, None, meta, 1010)["seq"] == 1010
    other = _payload()
    other["conflicts"] = [{"kind": "box", "label": "box", "evidence": "x"}]
    assert control_block(other, None, meta, 1010)["seq"] == 1011             # ta sama sekunda: +1
    assert control_block(other, None, meta, 900)["seq"] == 1011              # bez zmiany, zegar wstecz
    assert control_block(_payload(), None, meta, 500)["seq"] == 1012         # zmiana przy cofniętym zegarze


def test_ack_change_bumps_seq():
    meta: dict = {}
    a = control_block(_payload(), None, meta, 10)["seq"]
    b = control_block(_payload(), FIXTURE["choice_ack"], meta, 20)["seq"]
    assert (a, b) == (10, 20)


def test_seq_survives_restart_via_meta():
    meta = {"seq": 5000, "fp": "x"}
    assert control_block(_payload(), None, meta, 10)["seq"] == 5001


class Cloud:
    def __init__(self):
        self.sent = []

    async def async_post_telemetry(self, reading):
        self.sent.append(reading)
        return TelemetryResult(200, None)


class Rt:
    def __init__(self):
        self.persisted = 0
        self.block = {"seq": 7, "conflicts": []}

    def control_block(self):
        return self.block

    async def async_persist_control_meta(self):
        self.persisted += 1


def _sender(rt):
    entry = SimpleNamespace(entry_id="e1", options={})
    s = TelemetrySender(goodwe_hass(), entry, Cloud(), SimpleNamespace(local_switch=False), choice=GW,
                        profile_map=E, manual_map={}, grid_negate=False, limits=None, utcnow=lambda: NOW)
    s.control_runtime = rt
    return s


def test_sender_puts_control_in_driver_and_persists():
    rt = Rt()
    s = _sender(rt)
    assert asyncio.run(s.async_flush()) is True
    assert s._cloud.sent[0]["driver"]["control"] == {"seq": 7, "conflicts": []}
    assert rt.persisted == 1


def test_sender_without_runtime_has_no_control():
    s = _sender(None)
    asyncio.run(s.async_flush())
    assert "control" not in s._cloud.sent[0]["driver"]


def test_sender_survives_control_block_failure():
    class Bad(Rt):
        def control_block(self):
            raise RuntimeError("x")

    s = _sender(Bad())
    assert asyncio.run(s.async_flush()) is True
    assert "control" not in s._cloud.sent[0]["driver"]


def test_sender_without_profile_sends_driver_with_only_control():
    rt = Rt()
    entry = SimpleNamespace(entry_id="e1", options={})
    s = TelemetrySender(goodwe_hass(), entry, Cloud(), SimpleNamespace(local_switch=False), choice=None,
                        profile_map={}, manual_map={}, grid_negate=False, limits=None, utcnow=lambda: NOW)
    s.control_runtime = rt
    assert s._driver() == {"control": {"seq": 7, "conflicts": []}}
