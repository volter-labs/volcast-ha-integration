"""Encje ceny zakupu i sprzedaży konta (blok `prices` z planu)."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorStateClass

from custom_components.volcast import control_entities as ce
from custom_components.volcast.control_entities import VolcastPriceSensor
from custom_components.volcast.core.account_prices import parse_prices
from tests.test_account_prices import block, hourly, quarter, utc


def make(prices, side="buy", now=utc(2026, 11, 20, 9, 30), monkeypatch=None):
    monkeypatch.setattr(ce.dt_util, "utcnow", lambda: now)
    entry = SimpleNamespace(entry_id="e1", options={})
    rt = SimpleNamespace(fetcher=SimpleNamespace(prices=parse_prices(prices) if prices else None))
    sensor = VolcastPriceSensor(entry, rt, side)
    sensor._take_snapshot()
    return sensor, rt


def test_identity_and_static_properties(monkeypatch):
    s, _ = make(block(), "sell", monkeypatch=monkeypatch)
    assert s._attr_unique_id == "e1_price_sell" and s._attr_translation_key == "price_sell"
    assert s._attr_state_class is SensorStateClass.MEASUREMENT and getattr(s, "_attr_device_class", None) is None
    assert s._attr_suggested_display_precision == 4
    assert {"today", "tomorrow"} <= s._unrecorded_attributes
    assert s._attr_device_info["identifiers"] == {("volcast", "e1_control")}


@pytest.mark.parametrize("cur", ["PLN", "RON", "AUD"])
def test_unit_from_currency(monkeypatch, cur):
    s, _ = make(block(currency=cur), monkeypatch=monkeypatch)
    assert s.native_unit_of_measurement == f"{cur}/kWh"


def test_state_hourly_and_quarter(monkeypatch):
    s, _ = make(block(), "buy", monkeypatch=monkeypatch)       # 10:30 lokalnie → slot 10:00
    assert s.native_value == pytest.approx(1.10) and s.available
    q, _ = make(block(interval_min=15, today=quarter()), "buy", now=utc(2026, 11, 20, 9, 47), monkeypatch=monkeypatch)
    assert q.native_value == pytest.approx(10.45)             # 10:47 lokalnie → slot 10:45


def test_sell_null_slot_is_unavailable_but_attrs_keep_null(monkeypatch):
    s, _ = make(block(today=hourly(sell=None)), "sell", monkeypatch=monkeypatch)
    assert s.native_value is None and not s.available
    assert all(x["price"] is None for x in s.extra_state_attributes["today"])


def test_unavailable_without_block_or_slot(monkeypatch):
    s, _ = make(None, monkeypatch=monkeypatch)
    assert not s.available and s.native_value is None and s.extra_state_attributes == {}
    late, _ = make(block(), now=utc(2026, 11, 21, 3, 0), monkeypatch=monkeypatch)
    assert not late.available


def test_attributes(monkeypatch):
    s, _ = make(block(date_tomorrow="2026-11-21", tomorrow=hourly("2026-11-21")), monkeypatch=monkeypatch)
    a = s.extra_state_attributes
    assert a["currency"] == "PLN" and a["interval_min"] == 60 and a["updated_at"] == "2026-11-20T08:00:00Z"
    assert len(a["today"]) == 24 and len(a["tomorrow"]) == 24 and set(a["today"][0]) == {"start", "price"}


def test_midnight_rollover_uses_tomorrow(monkeypatch):
    s, _ = make(block(date_tomorrow="2026-11-21", tomorrow=hourly("2026-11-21")), now=utc(2026, 11, 20, 23, 10),
                monkeypatch=monkeypatch)                      # 00:10 21.11
    assert s.native_value == pytest.approx(1.0) and s.extra_state_attributes["tomorrow"] == []


# ---- aktualizacja na granicy slotu ----

class Hass:
    pass


def _added(monkeypatch, prices, now):
    tracked = []

    def track(hass, action, when):
        unsub = lambda: tracked.append(("unsub", when))        # noqa: E731
        tracked.append(("track", when, action))
        return unsub

    monkeypatch.setattr(ce, "async_track_point_in_utc_time", track)
    written = []
    s, rt = make(prices, monkeypatch=monkeypatch, now=now)
    s.hass = Hass()
    s.async_write_ha_state = lambda: written.append(s.native_value)
    s.async_on_remove = lambda f: None
    handlers = []
    monkeypatch.setattr(ce, "async_dispatcher_connect", lambda hass, sig, fn: handlers.append(fn))
    s.handlers = handlers
    return s, rt, tracked, written


def test_boundary_timer_moves_to_next_slot(monkeypatch):
    import asyncio
    s, rt, tracked, written = _added(monkeypatch, block(), utc(2026, 11, 20, 9, 30))
    asyncio.run(s.async_added_to_hass())
    assert tracked[0][:2] == ("track", utc(2026, 11, 20, 10, 0))
    # zegar dochodzi: nowy slot, kolejny termin, poprzedni zdjęty
    monkeypatch.setattr(ce.dt_util, "utcnow", lambda: utc(2026, 11, 20, 10, 0))
    tracked[0][2](utc(2026, 11, 20, 10, 0))
    assert written[-1] == pytest.approx(1.11)
    assert tracked[-1][:2] == ("track", utc(2026, 11, 20, 11, 0))


def test_boundary_timer_unsubscribed_on_removal(monkeypatch):
    import asyncio
    s, rt, tracked, _ = _added(monkeypatch, block(), utc(2026, 11, 20, 9, 30))
    asyncio.run(s.async_added_to_hass())
    asyncio.run(s.async_will_remove_from_hass())
    assert ("unsub", utc(2026, 11, 20, 10, 0)) in tracked


def test_no_timer_without_data(monkeypatch):
    import asyncio
    s, rt, tracked, _ = _added(monkeypatch, None, utc(2026, 11, 20, 9, 30))
    asyncio.run(s.async_added_to_hass())
    assert tracked == []


def test_translation_keys_in_strings_and_every_language():
    import json
    from pathlib import Path
    root = Path(__file__).parent.parent / "custom_components" / "volcast"
    files = [root / "strings.json", *sorted((root / "translations").glob("*.json"))]
    assert len(files) == 14
    for f in files:
        sensor = json.loads(f.read_text("utf-8"))["entity"]["sensor"]
        for key in ("price_buy", "price_sell"):
            assert sensor[key]["name"].strip(), f"{f.name}: {key}"


# ---- @callback, ponowne uzbrojenie zegara, zmiana czasu, instalacja tylko do odczytu ----

def test_registered_handlers_are_marked_callbacks(monkeypatch):
    import asyncio
    s, rt, tracked, _ = _added(monkeypatch, block(), utc(2026, 11, 20, 9, 30))
    asyncio.run(s.async_added_to_hass())
    # w prawdziwym HA funkcja bez znacznika leci do wątku i zapis stanu się wywala
    assert getattr(s.handlers[0], "_hass_callback", False) is True
    assert getattr(tracked[0][2], "_hass_callback", False) is True


def test_poll_signal_rearms_single_timer(monkeypatch):
    import asyncio
    s, rt, tracked, written = _added(monkeypatch, block(), utc(2026, 11, 20, 9, 30))
    asyncio.run(s.async_added_to_hass())
    s.handlers[0]()                                  # sygnał po odpytaniu
    s.handlers[0]()
    tracks = [t for t in tracked if t[0] == "track"]
    unsubs = [t for t in tracked if t[0] == "unsub"]
    assert len(tracks) == 3 and len(unsubs) == 2     # każdy poprzedni zegar zdjęty, żywy jest jeden
    assert len(written) == 2


def test_unload_removes_timer(monkeypatch):
    import asyncio
    s, rt, tracked, _ = _added(monkeypatch, block(), utc(2026, 11, 20, 9, 30))
    asyncio.run(s.async_added_to_hass())
    asyncio.run(s.async_will_remove_from_hass())
    assert s._unsub_boundary is None and tracked[-1][0] == "unsub"


def test_value_and_availability_agree_at_end_of_data(monkeypatch):
    s, _ = make(block(), monkeypatch=monkeypatch, now=utc(2026, 11, 20, 22, 59))
    assert s.available and s.native_value is not None
    monkeypatch.setattr(ce.dt_util, "utcnow", lambda: utc(2026, 11, 20, 23, 0))
    assert s.available and s.native_value is not None       # migawka — jedna chwila na zapis
    s._take_snapshot()
    assert not s.available and s.native_value is None


def _dst_day(day, off_before, off_after, switch_hour, n):
    """Doba z przejściem czasu: n slotów godzinowych; od `switch_hour` (czasu UTC) zmienia się offset."""
    from datetime import timedelta
    t0 = datetime.fromisoformat(f"{day}T00:00:00{off_before}")
    out = []
    for i in range(n):
        t = t0 + timedelta(hours=i)
        loc = t.astimezone(timezone(timedelta(hours=int(off_after[:3]) if t >= switch_hour else int(off_before[:3]))))
        out.append({"start": loc.isoformat(), "buy": float(i), "sell": 0.1})
    return out


def test_dst_autumn_day_has_25_slots():
    sw = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)      # 03:00+02 -> 02:00+01
    slots = _dst_day("2026-10-25", "+02:00", "+01:00", sw, 25)
    p = parse_prices(block(date_today="2026-10-25", today=slots))
    assert len(p.slots) == 25                                  # dwie „02:00" (różne offsety) zostają obie
    today, _ = p.days(utc(2026, 10, 25, 5, 0), "buy")
    assert len(today) == 25
    assert p.slot_at(utc(2026, 10, 25, 1, 30)).buy == 3.0       # druga 02:xx (offset +01:00)


def test_dst_spring_day_has_23_slots():
    sw = datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)       # 02:00+01 -> 03:00+02
    slots = _dst_day("2026-03-29", "+01:00", "+02:00", sw, 23)
    p = parse_prices(block(date_today="2026-03-29", today=slots))
    assert len(p.slots) == 23
    assert p.slot_at(utc(2026, 3, 29, 1, 0)).buy == 2.0         # 03:00 lokalnie, zaraz po przeskoku
    assert p.next_boundary(utc(2026, 3, 29, 0, 30)) == utc(2026, 3, 29, 1, 0)


def test_read_only_install_creates_both_price_entities():
    import asyncio
    from custom_components.volcast.sensor import async_setup_entry
    entry = SimpleNamespace(entry_id="e1", options={})            # bez profilu falownika i sterowania
    control = SimpleNamespace(executor=SimpleNamespace(), fetcher=SimpleNamespace(prices=None), choice=None)
    hass = SimpleNamespace(data={"volcast": {"e1": {"control": control}}})
    added = []
    asyncio.run(async_setup_entry(hass, entry, added.extend))
    assert [e._attr_unique_id for e in added if isinstance(e, VolcastPriceSensor)] == ["e1_price_buy", "e1_price_sell"]
