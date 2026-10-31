"""Krok opcji „Źródło cen z integracji": wykrywanie, walidacja prognozy i zapis akcji na prawdziwym rdzeniu HA."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import make_entry


def _hours(n: int, step_min: int = 60, key_start="start_time", value_key="per_kwh", value=0.3):
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    out = []
    for i in range(n * 60 // step_min):
        s = start + timedelta(minutes=i * step_min)
        out.append({key_start: s.isoformat(), "end_time": (s + timedelta(minutes=step_min)).isoformat(), value_key: value})
    return out


def _register(hass, domain, service, build, calls):
    async def handle(call: ServiceCall):
        calls.append(dict(call.data))
        return build(call)

    hass.services.async_register(domain, service, handle, schema=vol.Schema({}, extra=vol.ALLOW_EXTRA),
                                 supports_response=SupportsResponse.ONLY)


async def _open(hass, entry, step="price_source"):
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert "price_source" in menu["menu_options"]
    return await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": step})


async def test_no_known_source_aborts(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
    res = await _open(hass, entry)
    assert res["type"] is FlowResultType.ABORT and res["reason"] == "no_price_sources"


async def test_amber_pair_is_validated_and_saved_as_actions(hass: HomeAssistant, network_down):
    MockConfigEntry(domain="amberelectric", title="Amber home", entry_id="amb1").add_to_hass(hass)
    calls = []
    _register(hass, "amberelectric", "get_forecasts", lambda call: {"forecasts": _hours(36, 30)}, calls)
    entry = make_entry(hass, options={"entity_price_buy": "sensor.old", "entity_price_sell": "sensor.old_sell"})
    form = await _open(hass, entry)
    assert form["type"] is FlowResultType.FORM and form["step_id"] == "price_source"
    confirm = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|amberelectric|amb1"})
    assert confirm["step_id"] == "price_source_confirm"
    assert int(confirm["description_placeholders"]["hours"]) >= 35
    assert [c["channel_type"] for c in calls] == ["general", "feed_in"]
    done = await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    opts = entry.options
    assert opts["price_buy_action"]["data"] == {"config_entry_id": "amb1", "channel_type": "general"}
    assert opts["price_sell_action"]["data"] == {"config_entry_id": "amb1", "channel_type": "feed_in"}
    assert "entity_price_buy" not in opts and "entity_price_sell" not in opts


async def test_short_forecast_is_not_saved(hass: HomeAssistant, network_down):
    MockConfigEntry(domain="amberelectric", title="Amber home", entry_id="amb1").add_to_hass(hass)
    _register(hass, "amberelectric", "get_forecasts", lambda call: {"forecasts": _hours(3)}, [])
    entry = make_entry(hass)
    form = await _open(hass, entry)
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|amberelectric|amb1"})
    assert res["type"] is FlowResultType.FORM and res["step_id"] == "price_source"
    assert res["errors"] == {"base": "price_horizon_short"} and res["description_placeholders"]["hours"] in ("2", "3")
    assert "price_buy_action" not in entry.options


async def test_failing_action_is_not_saved(hass: HomeAssistant, network_down):
    MockConfigEntry(domain="amberelectric", title="Amber home", entry_id="amb1").add_to_hass(hass)

    def boom(call):
        raise RuntimeError("down")
    _register(hass, "amberelectric", "get_forecasts", boom, [])
    entry = make_entry(hass)
    form = await _open(hass, entry)
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|amberelectric|amb1"})
    assert res["errors"] == {"base": "price_source_unusable"} and "price_buy_action" not in entry.options


async def test_entsoe_asks_for_unit_and_currency(hass: HomeAssistant, network_down):
    MockConfigEntry(domain="entsoe", title="ENTSO-e", entry_id="en1").add_to_hass(hass)
    calls = []
    _register(hass, "entsoe", "get_energy_prices",
              lambda call: {"prices": _hours(30, 60, "timestamp", "price", 80.0)}, calls)
    entry = make_entry(hass)
    form = await _open(hass, entry)
    details = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|entsoe|en1"})
    assert details["step_id"] == "price_source_details"
    assert {str(k) for k in details["data_schema"].schema} == {"unit", "currency"}
    confirm = await hass.config_entries.options.async_configure(details["flow_id"], {"unit": "MWh", "currency": "eur"})
    assert confirm["step_id"] == "price_source_confirm"
    done = await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    saved = entry.options["price_buy_action"]
    assert saved["unit"] == "MWh" and saved["currency"] == "EUR" and saved["action"] == "entsoe.get_energy_prices"


async def test_currency_conflict_is_shown_not_resolved(hass: HomeAssistant, network_down):
    MockConfigEntry(domain="entsoe", title="ENTSO-e", entry_id="en1").add_to_hass(hass)
    calls = []
    _register(hass, "entsoe", "get_energy_prices", lambda call: {"prices": _hours(30, 60, "timestamp", "price")}, calls)
    entry = make_entry(hass, options={"price_currency": "PLN"})
    form = await _open(hass, entry)
    details = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|entsoe|en1"})
    res = await hass.config_entries.options.async_configure(details["flow_id"], {"unit": "kWh", "currency": "EUR"})
    assert res["step_id"] == "price_source_details" and res["errors"] == {"currency": "currency_conflict"}
    assert res["description_placeholders"]["source_currency"] == "EUR"
    assert res["description_placeholders"]["option_currency"] == "PLN"
    assert calls == [] and "price_buy_action" not in entry.options


async def test_nordpool_needs_exactly_one_zone(hass: HomeAssistant, network_down):
    MockConfigEntry(domain="nordpool", title="Nord Pool", entry_id="np1").add_to_hass(hass)
    _register(hass, "nordpool", "get_prices_for_date",
              lambda call: {"SE3": [{"start": s["start_time"], "end": s["end_time"], "price": 500.0}
                                    for s in _hours(30)]}, [])
    entry = make_entry(hass)
    form = await _open(hass, entry)
    details = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|nordpool|np1"})
    bad = await hass.config_entries.options.async_configure(details["flow_id"], {"area": "SE3,SE4", "currency": "SEK"})
    assert bad["errors"] == {"base": "price_source_invalid"}
    ok = await hass.config_entries.options.async_configure(bad["flow_id"], {"area": "se3", "currency": "SEK"})
    assert ok["step_id"] == "price_source_confirm"


async def test_detected_entity_pair_validated_and_saved(hass: HomeAssistant, network_down):
    registry = er.async_get(hass)
    for eid in ("octopus_energy_x_current_rate", "octopus_energy_x_export_current_rate"):
        registry.async_get_or_create("sensor", "octopus_energy", eid, suggested_object_id=eid)
    series = _hours(24, 60, "start", "value", 0.2)
    for s in series:
        s["end"] = s.pop("end_time")
    attrs = {"currency": "GBP", "unit_of_measurement": "GBP/kWh", "rates": series}
    hass.states.async_set("sensor.octopus_energy_x_current_rate", "0.2", attrs)
    hass.states.async_set("sensor.octopus_energy_x_export_current_rate", "0.1", attrs)
    entry = make_entry(hass, options={"price_buy_action": {"action": "x.y"}})
    form = await _open(hass, entry)
    done = await hass.config_entries.options.async_configure(
        form["flow_id"], {"source": "entities|sensor.octopus_energy_x_current_rate"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["entity_price_buy"] == "sensor.octopus_energy_x_current_rate"
    assert entry.options["entity_price_sell"] == "sensor.octopus_energy_x_export_current_rate"
    assert "price_buy_action" not in entry.options


async def test_current_price_only_entity_is_refused(hass: HomeAssistant, network_down):
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "pstryk", "buy", suggested_object_id="pstryk_buy_now")
    hass.states.async_set("sensor.pstryk_buy_now", "0.61", {"unit_of_measurement": "PLN/kWh"})
    entry = make_entry(hass)
    form = await _open(hass, entry)
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "entities|sensor.pstryk_buy_now"})
    assert res["errors"] == {"source": "prices_current_only"} and "entity_price_buy" not in entry.options


async def test_prices_form_refuses_current_only_and_replaces_action(hass: HomeAssistant, network_down):
    hass.states.async_set("sensor.now_only", "0.61", {"unit_of_measurement": "PLN/kWh"})
    entry = make_entry(hass, options={"price_buy_action": {"action": "tibber.get_prices"}})
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    bad = await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": "sensor.now_only"})
    assert bad["errors"] == {"entity_price_buy": "prices_current_only"}
    assert "price_buy_action" in entry.options
