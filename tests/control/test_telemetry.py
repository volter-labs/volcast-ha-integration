import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from homeassistant.core import Context
from homeassistant.helpers import event as ha_event

from custom_components.volcast.control import telemetry as tm
from custom_components.volcast.control.telemetry import TelemetrySender, build_reading, driver_block
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin

from .ha_fakes import GOODWE_ENTITIES as E, FakeState, goodwe_hass

NOW = datetime(2026, 9, 23, 10, 0, 30, tzinfo=timezone.utc)
GW = ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET")
ALL = ("mode", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")
LOGGER = "custom_components.volcast.control"


def test_build_reading_maps_fields_and_mode():
    r = build_reading(now_utc=NOW, profile_readings={"soc": 55.0, "grid_power_w": -1200.0, "mode": "auto",
                                                     "power_w": 0.0},
                      manual={}, driver=None, extra={"x": 1}, prices=None)
    assert r == {"timestamp": NOW.isoformat(), "battery_soc": 55.0, "grid_power_w": -1200.0,
                 "ems_mode": "auto", "extra": {"volcast": {"x": 1}}}


def test_manual_overrides_profile_and_empty_is_none():
    r = build_reading(now_utc=NOW, profile_readings={"load_power_w": 100.0},
                      manual={"load_power_w": 250.0, "pv_energy_total_kwh": None}, driver=None,
                      extra={}, prices=None)
    assert r["load_power_w"] == 250.0 and "pv_energy_total_kwh" not in r
    assert build_reading(now_utc=NOW, profile_readings={}, manual={}, driver=None, extra={},
                         prices=None) is None


def test_manual_none_does_not_fall_back_to_profile():
    # Encja wskazana ręcznie, ale nieczytelna — nie podstawiamy odczytu z profilu.
    r = build_reading(now_utc=NOW, profile_readings={"load_power_w": 100.0, "soc": 50.0},
                      manual={"load_power_w": None}, driver=None, extra={}, prices=None)
    assert "load_power_w" not in r and r["battery_soc"] == 50.0


def test_build_reading_skips_non_finite_and_bool():
    r = build_reading(now_utc=NOW, profile_readings={"soc": float("nan"), "pv_power_w": float("inf"),
                                                     "load_power_w": True, "grid_power_w": 5.0},
                      manual={}, driver=None, extra={}, prices=None)
    assert set(r) == {"timestamp", "grid_power_w", "extra"}


def test_driver_block_carries_limits():
    b = driver_block(choice=GW, control_mode=None, mapped_keys=ALL, local_switch=False,
                     limits={"rated_power_w": 8000, "source": "entities"})
    assert b == {"id": "goodwe-et", "model": "mode_setpoint", "local_switch_enabled": False,
                 "limits": {"rated_power_w": 8000, "source": "entities"}}


def test_driver_block_capabilities_only_in_entity_mode():
    b = driver_block(choice=GW, control_mode="entities", mapped_keys=ALL, local_switch=True, limits=None)
    assert all(b["capabilities"].values()) and b["capabilities"]["set_soc_ceiling"] is True
    partial = driver_block(choice=GW, control_mode="entities", mapped_keys=[k for k in ALL if k != "soc_max"],
                           local_switch=True, limits=None)
    # Nastawa bez encji wyłącza tylko swoją możliwość.
    assert {k for k, v in partial["capabilities"].items() if not v} == {"set_soc_ceiling"}
    ro = ProfileChoice(load_builtin("deye-sg"), None, None)
    assert "capabilities" not in driver_block(choice=ro, control_mode="entities", mapped_keys=(),
                                              local_switch=False, limits=None)
    assert driver_block(choice=None, control_mode=None, mapped_keys=(), local_switch=False, limits=None) is None


class Cloud:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def async_post_telemetry(self, reading):
        self.sent.append(reading)
        if isinstance(self.ok, BaseException):
            raise self.ok
        return self.ok


class Exec:
    local_switch = False

    def exec_summary(self):
        return {"decision": {"dropped_unsupported": ["export_limit_enabled"]}}


def _nordpool(h, value=0.5, eid="sensor.nordpool"):
    h.states.set(eid, str(value), {"currency": "PLN", "raw_today": [
        {"start": f"2026-09-23T{i:02d}:00:00+02:00", "end": f"2026-09-23T{i + 1:02d}:00:00+02:00", "value": value}
        for i in range(0, 23)] + [{"start": "2026-09-23T23:00:00+02:00", "end": "2026-09-24T00:00:00+02:00",
                                   "value": value}]})


def sender(h, cloud, options=None, *, clock=None, executor=None, manual=None, negate=False, limits=None):
    _nordpool(h)
    entry = SimpleNamespace(entry_id="e1", options=options or {"entity_price_buy": "sensor.nordpool"})
    return TelemetrySender(h, entry, cloud, executor or Exec(), choice=GW, profile_map=E,
                           manual_map=manual or {}, grid_negate=negate, limits=limits,
                           utcnow=clock or (lambda: NOW))


def test_extra_reports_dropped_keys_and_prices_once():
    h, cloud = goodwe_hass(), Cloud()
    s = sender(h, cloud)
    assert asyncio.run(s.async_flush()) is True
    assert cloud.sent[0]["extra"]["volcast"]["decision"]["dropped_unsupported"] == ["export_limit_enabled"]
    assert cloud.sent[0]["prices"]["currency"] == "PLN" and len(cloud.sent[0]["prices"]["intervals"]) == 24
    asyncio.run(s.async_flush())
    assert "prices" not in cloud.sent[1]                     # bez zmiany i < 6 h


def test_prices_not_marked_sent_on_failed_post():
    h, cloud = goodwe_hass(), Cloud(ok=False)
    s = sender(h, cloud)
    asyncio.run(s.async_flush())
    cloud.ok = True
    asyncio.run(s.async_flush())
    assert "prices" in cloud.sent[1]


def test_flush_never_raises_on_broken_state():
    h, cloud = goodwe_hass(), Cloud()
    h.states.set(E["soc"], object())                       # zepsuty stan
    assert asyncio.run(sender(h, cloud).async_flush()) in (True, False)


# ── ceny: zmiana, odświeżenie, brak ─────────────────────────────────────────


def test_prices_resent_after_six_hours_and_on_change():
    t = {"now": NOW}
    h, cloud = goodwe_hass(), Cloud()
    s = sender(h, cloud, clock=lambda: t["now"])
    asyncio.run(s.async_flush())
    t["now"] = NOW + timedelta(hours=5, minutes=59)
    asyncio.run(s.async_flush())
    t["now"] = NOW + timedelta(hours=6)
    asyncio.run(s.async_flush())
    _nordpool(h, value=0.7)
    asyncio.run(s.async_flush())
    assert ["prices" in r for r in cloud.sent] == [True, False, True, True]
    assert cloud.sent[3]["prices"]["intervals"][0]["buy"] == 0.7


def test_prices_market_from_home_country():
    h, cloud = goodwe_hass(), Cloud()
    h.config.country = "de"
    asyncio.run(sender(h, cloud).async_flush())
    assert cloud.sent[0]["prices"]["market"] == "DE"


def test_no_prices_without_usable_entity_but_reading_still_sent():
    for options in ({}, {"entity_price_buy": "sensor.missing"}):
        h, cloud = goodwe_hass(), Cloud()
        assert asyncio.run(sender(h, cloud, options=options or {"x": 1}).async_flush()) is True
        assert "prices" not in cloud.sent[0] and cloud.sent[0]["battery_soc"] == 60.0
    h, cloud = goodwe_hass(), Cloud()
    s = sender(h, cloud)
    h.states.set("sensor.nordpool", "unavailable", {})
    asyncio.run(s.async_flush())
    assert "prices" not in cloud.sent[0]


def test_prices_error_never_blocks_telemetry(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    def boom(*_a, **_k):
        raise RuntimeError("sensor.nordpool_kwh_pl_pln_secret")
    monkeypatch.setattr(tm, "intervals_from_attributes", boom)
    h, cloud = goodwe_hass(), Cloud()
    assert asyncio.run(sender(h, cloud).async_flush()) is True
    assert "prices" not in cloud.sent[0]
    assert "secret" not in caplog.text and "RuntimeError" in caplog.text


def test_sell_price_entity_used():
    h, cloud = goodwe_hass(), Cloud()
    _nordpool(h, value=0.2, eid="sensor.sell")
    s = sender(h, cloud, options={"entity_price_buy": "sensor.nordpool", "entity_price_sell": "sensor.sell"})
    asyncio.run(s.async_flush())
    iv = cloud.sent[0]["prices"]["intervals"]
    assert iv[0]["buy"] == 0.5 and iv[0]["sell"] == 0.2


# ── odczyty, znak sieci, prywatność, cykl życia ────────────────────────────


def test_manual_map_with_grid_negate():
    h, cloud = goodwe_hass(), Cloud()
    h.states.set("sensor.grid", "1.5", {"unit_of_measurement": "kW"})
    s = sender(h, cloud, manual={"grid_power_w": "sensor.grid", "load_power_w": "sensor.nope"}, negate=True)
    asyncio.run(s.async_flush())
    assert cloud.sent[0]["grid_power_w"] == -1500.0 and "load_power_w" not in cloud.sent[0]


def test_limits_and_driver_in_reading():
    h, cloud = goodwe_hass(), Cloud()
    s = sender(h, cloud, options={"control_mode": "entities"},
               limits={"rated_power_w": 8000, "source": "user"})
    asyncio.run(s.async_flush())
    d = cloud.sent[0]["driver"]
    assert d["id"] == "goodwe-et" and d["limits"] == {"rated_power_w": 8000, "source": "user"}
    assert all(d["capabilities"].values())


def test_unsupported_setting_reported_without_its_capability():
    # Encja zmapowana, ale nastawa nieobsługiwana (niedostępna) — chmura nie dostaje jej możliwości.
    class Unsupported(Exec):
        unsupported_settings = ("soc_max",)

    h, cloud = goodwe_hass(), Cloud()
    s = sender(h, cloud, options={"control_mode": "entities"}, executor=Unsupported())
    asyncio.run(s.async_flush())
    caps = cloud.sent[0]["driver"]["capabilities"]
    assert {k for k, v in caps.items() if not v} == {"set_soc_ceiling"}


def test_sent_capabilities_reported_back_to_executor_only_after_accepted_post():
    class Recording(Exec):
        unsupported_settings = ("soc_min",)

        def __init__(self):
            self.sent = []

        def note_capabilities_sent(self, keys):
            self.sent.append(tuple(keys))

    h = goodwe_hass()
    rec = Recording()
    asyncio.run(sender(h, Cloud(ok=False), options={"control_mode": "entities"}, executor=rec).async_flush())
    assert rec.sent == []
    asyncio.run(sender(h, Cloud(), options={"control_mode": "entities"}, executor=rec).async_flush())
    assert rec.sent == [("soc_min",)]
    rec2 = Recording()
    asyncio.run(sender(h, Cloud(), options={}, executor=rec2).async_flush())    # bez możliwości w odczycie
    assert rec2.sent == []


def test_telemetry_carries_foreign_change_count_never_entity_ids(monkeypatch):
    from .test_executor import make, ready
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
        ev = SimpleNamespace(data={"entity_id": E["power_w"],
                                   "new_state": FakeState(E["power_w"], "500", {"unit_of_measurement": "W"},
                                                          context=Context(user_id="u1"))})
        await ex.async_on_state_event(ev)
    asyncio.run(go())
    cloud = Cloud()
    s = sender(h, cloud, options={"control_mode": "entities"}, executor=ex)
    asyncio.run(s.async_flush())
    text = json.dumps(cloud.sent[0])
    assert cloud.sent[0]["extra"]["volcast"]["foreign_changes"] == 1
    assert not any(eid in text for eid in E.values())


def test_post_exception_returns_false_and_logs_class_only(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    h, cloud = goodwe_hass(), Cloud(ok=OSError("http://10.0.0.1 refused"))
    assert asyncio.run(sender(h, cloud).async_flush()) is False
    assert "10.0.0.1" not in caplog.text


def test_executor_summary_failure_still_sends_reading():
    class BadExec(Exec):
        def exec_summary(self):
            raise RuntimeError("x")
    h, cloud = goodwe_hass(), Cloud()
    assert asyncio.run(sender(h, cloud, executor=BadExec()).async_flush()) is True
    assert cloud.sent[0]["extra"] == {"volcast": {}}


def test_start_is_idempotent_and_stop_unsubscribes():
    ha_event.async_track_time_interval.reset_mock()
    h, cloud = goodwe_hass(), Cloud()
    s = sender(h, cloud)
    asyncio.run(s.async_start())
    asyncio.run(s.async_start())
    assert ha_event.async_track_time_interval.call_count == 1
    unsub = ha_event.async_track_time_interval.return_value
    unsub.reset_mock()
    asyncio.run(s.async_stop())
    asyncio.run(s.async_stop())
    assert unsub.call_count == 1


def test_flush_is_single_flight():
    h = goodwe_hass()

    class SlowCloud(Cloud):
        async def async_post_telemetry(self, reading):
            await asyncio.sleep(0.01)
            return await super().async_post_telemetry(reading)
    cloud = SlowCloud()
    s = sender(h, cloud)

    async def go():
        return await asyncio.gather(s.async_flush(), s.async_flush())
    results = asyncio.run(go())
    assert len(cloud.sent) == 1 and sorted(results) == [False, True]


# ── ceny i blok driver bez odczytów, izolacja ręcznych encji, rynek ──────────


def test_build_reading_without_values_but_with_prices():
    prices = {"market": "PL", "currency": "PLN", "intervals": [{"startAt": "2026-09-23T10:00:00Z"}]}
    drv = {"id": "goodwe-et", "model": "mode_setpoint", "local_switch_enabled": False}
    r = build_reading(now_utc=NOW, profile_readings={}, manual={}, driver=drv, extra={}, prices=prices)
    assert r == {"timestamp": NOW.isoformat(), "extra": {"volcast": {}}, "driver": drv, "prices": prices}
    assert build_reading(now_utc=NOW, profile_readings={}, manual={}, driver=drv, extra={}, prices=None) is None


def test_prices_sent_even_without_monitoring_values():
    h, cloud = goodwe_hass(), Cloud()
    for eid in E.values():
        h.states.set(eid, "unavailable")
    assert asyncio.run(sender(h, cloud).async_flush()) is True
    (r,) = cloud.sent
    assert "prices" in r and r["driver"]["id"] == "goodwe-et"
    assert not set(r) & {*tm.TELEMETRY_FIELDS.values(), "ems_mode"}


def test_manual_entity_with_non_string_unit_drops_only_that_field():
    h, cloud = goodwe_hass(), Cloud()
    h.states.set("sensor.load", "250", {"unit_of_measurement": ["W"]})
    s = sender(h, cloud, manual={"load_power_w": "sensor.load"})
    assert asyncio.run(s.async_flush()) is True
    assert "load_power_w" not in cloud.sent[0] and cloud.sent[0]["battery_soc"] == 60.0


def test_manual_reading_exception_drops_only_that_field(monkeypatch):
    def boom(key, raw, negate=False):
        raise TypeError("x")
    monkeypatch.setattr(tm, "manual_reading", boom)
    h, cloud = goodwe_hass(), Cloud()
    h.states.set("sensor.load", "250", {"unit_of_measurement": "W"})
    assert asyncio.run(sender(h, cloud, manual={"load_power_w": "sensor.load"}).async_flush()) is True
    assert "load_power_w" not in cloud.sent[0] and cloud.sent[0]["battery_soc"] == 60.0


@pytest.mark.parametrize("country", [None, "", "POL", "p1", 5])
def test_market_falls_back_to_pl(country):
    h, cloud = goodwe_hass(), Cloud()
    h.config.country = country
    asyncio.run(sender(h, cloud).async_flush())
    assert cloud.sent[0]["prices"]["market"] == "PL"


def test_country_change_resends_prices():
    h, cloud = goodwe_hass(), Cloud()
    s = sender(h, cloud)
    asyncio.run(s.async_flush())
    h.config.country = "DE"
    asyncio.run(s.async_flush())
    assert cloud.sent[1]["prices"]["market"] == "DE"


def test_truthy_non_bool_post_result_is_not_success():
    h, cloud = goodwe_hass(), Cloud(ok="yes")
    s = sender(h, cloud)
    assert asyncio.run(s.async_flush()) is False
    cloud.ok = True
    asyncio.run(s.async_flush())
    assert "prices" in cloud.sent[1]                         # pierwsza wysyłka nie liczyła się jako udana


def test_time_window_profile_has_no_capabilities_even_in_entity_mode():
    deye = ProfileChoice(load_builtin("deye-sg"), "solarman", None)
    b = driver_block(choice=deye, control_mode="entities", mapped_keys=ALL, local_switch=True, limits=None)
    assert b is not None and "capabilities" not in b
