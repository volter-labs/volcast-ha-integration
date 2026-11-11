"""Krok opcji „Źródło cen z integracji": wykrywanie, walidacja prognozy i zapis akcji na prawdziwym rdzeniu HA."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.volcast import config_flow

from .conftest import make_entry

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "prices"


def _hour_start() -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _next_boundary(step_min: int) -> datetime:
    """Pierwsza granica siatki PO teraz — od niej zaczyna się prognoza Ambera (bez bieżącego przedziału)."""
    step = step_min * 60
    return datetime.fromtimestamp((int(datetime.now(timezone.utc).timestamp()) // step + 1) * step, timezone.utc)


def _hours(n: int, step_min: int = 60, key_start="start_time", value_key="per_kwh", value=0.3, start=None):
    start = start or _hour_start()
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


@pytest.fixture(autouse=True)
def _no_unload_of_fake_entries(hass: HomeAssistant):
    """Wpisy cudzych integracji są atrapami „załadowanymi"; przed zamknięciem HA wracają do stanu nieładowanego."""
    yield
    for entry in hass.config_entries.async_entries():
        if entry.domain != "volcast" and entry.state is ConfigEntryState.LOADED:
            entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


def _loaded(hass, domain, title, entry_id):
    entry = MockConfigEntry(domain=domain, title=title, entry_id=entry_id)
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


def _fixture(name: str, key: str, hours: int, step_min: int, start=None, **extra) -> dict:
    """Atrybuty zanonimizowanej encji z `tests/fixtures/prices`, z serią przesuniętą na „teraz"."""
    attrs = json.loads((FIXTURES / f"{name}.json").read_text())["attributes"]
    template = attrs[key][0]
    start = start or _hour_start()
    rows = []
    for i in range(hours * 60 // step_min):
        row = dict(template)
        s = start + timedelta(minutes=i * step_min)
        for k in ("start", "start_time"):
            if k in row:
                row[k] = s.isoformat()
        for k in ("end", "end_time"):
            if k in row:
                row[k] = (s + timedelta(minutes=step_min)).isoformat()
        rows.append(row)
    return {**attrs, key: rows, **extra}


def _entity(hass, domain, platform, object_id, attrs, state="0.3", disabled=False):
    er.async_get(hass).async_get_or_create(
        domain, platform, object_id, suggested_object_id=object_id,
        disabled_by=er.RegistryEntryDisabler.USER if disabled else None)
    hass.states.async_set(f"{domain}.{object_id}", state, attrs)
    return f"{domain}.{object_id}"


def _amber_set(hass):
    fc = _fixture("amber_forecasts", "forecasts", 36, 30, start=_next_boundary(30))
    plain = {"unit_of_measurement": "$/kWh", "per_kwh": 0.3}
    _entity(hass, "sensor", "amberelectric", "home_general_price", plain)
    buy = _entity(hass, "sensor", "amberelectric", "home_general_forecast", fc)
    _entity(hass, "sensor", "amberelectric", "home_feed_in_price", plain)
    sell = _entity(hass, "sensor", "amberelectric", "home_feed_in_forecast", fc)
    _entity(hass, "sensor", "amberelectric", "home_controlled_load_forecast", fc)
    _entity(hass, "sensor", "amberelectric", "home_renewables", {"unit_of_measurement": "%"}, "40")
    return buy, sell


def _octopus_set(hass):
    rates = _fixture("octopus_rates", "rates", 20, 30)
    tomorrow = _fixture("octopus_rates", "rates", 20, 30)
    for row in tomorrow["rates"]:
        row["start"] = (datetime.fromisoformat(row["start"]) + timedelta(days=1)).isoformat()
        row["end"] = (datetime.fromisoformat(row["end"]) + timedelta(days=1)).isoformat()
    buy = _entity(hass, "event", "octopus_energy", "octopus_energy_electricity_aaa_111_current_day_rates", rates)
    _entity(hass, "event", "octopus_energy", "octopus_energy_electricity_aaa_111_next_day_rates", tomorrow)
    _entity(hass, "sensor", "octopus_energy", "octopus_energy_electricity_aaa_111_current_rate",
            {"unit_of_measurement": "GBP/kWh"})
    sell = _entity(hass, "event", "octopus_energy", "octopus_energy_electricity_bbb_222_export_current_day_rates", rates)
    return buy, sell


def _pstryk_set(hass):
    attrs = _fixture("pstryk_all_prices", "All prices", 24, 60)
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", attrs)
    sell = _entity(hass, "sensor", "pstryk", "pstryk_current_sell_price", attrs)
    for name in ("pstryk_monthly_avg_buy_price", "pstryk_monthly_avg_sell_price", "pstryk_yearly_avg_buy_price"):
        _entity(hass, "sensor", "pstryk", name, {"unit_of_measurement": "PLN/kWh"}, "0.5")
    return buy, sell


def _source_id(form, prefix: str) -> str:
    values = [o["value"] for o in form["data_schema"].schema["source"].config["options"]]
    (found,) = [v for v in values if v.startswith(prefix)]
    return found


async def _choose(hass, entry, source):
    form = await _open(hass, entry)
    if source.startswith("entities|"):
        source = _source_id(form, source)
    return form, await hass.config_entries.options.async_configure(form["flow_id"], {"source": source})


async def _open(hass, entry, step="price_source"):
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert "price_source" in menu["menu_options"]
    return await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": step})


async def test_no_known_source_aborts(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
    res = await _open(hass, entry)
    assert res["type"] is FlowResultType.ABORT and res["reason"] == "no_price_sources"


async def test_amber_pair_is_validated_and_saved_as_actions(hass: HomeAssistant, network_down):
    _loaded(hass, "amberelectric", "Amber home", "amb1")
    calls = []
    _register(hass, "amberelectric", "get_forecasts", lambda call: {"forecasts": _hours(36, 30, start=_next_boundary(30))}, calls)
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
    _loaded(hass, "amberelectric", "Amber home", "amb1")
    _register(hass, "amberelectric", "get_forecasts",
              lambda call: {"forecasts": _hours(3, 30, start=_next_boundary(30))}, [])
    entry = make_entry(hass)
    form = await _open(hass, entry)
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|amberelectric|amb1"})
    assert res["type"] is FlowResultType.FORM and res["step_id"] == "price_source"
    assert res["errors"] == {"base": "price_horizon_short"} and res["description_placeholders"]["hours"] in ("2", "3")
    assert "price_buy_action" not in entry.options


async def test_failing_action_is_not_saved(hass: HomeAssistant, network_down):
    _loaded(hass, "amberelectric", "Amber home", "amb1")

    def boom(call):
        raise RuntimeError("down")
    _register(hass, "amberelectric", "get_forecasts", boom, [])
    entry = make_entry(hass)
    form = await _open(hass, entry)
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|amberelectric|amb1"})
    assert res["errors"] == {"base": "price_source_unusable"} and "price_buy_action" not in entry.options


async def test_entsoe_asks_for_unit_and_currency(hass: HomeAssistant, network_down):
    _loaded(hass, "entsoe", "ENTSO-e", "en1")
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
    _loaded(hass, "entsoe", "ENTSO-e", "en1")
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
    _loaded(hass, "nordpool", "Nord Pool", "np1")
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


async def test_real_entity_sets_yield_one_pair_each(hass: HomeAssistant, network_down):
    amber_buy, amber_sell = _amber_set(hass)
    octo_buy, octo_sell = _octopus_set(hass)
    pstryk_buy, pstryk_sell = _pstryk_set(hass)
    _entity(hass, "event", "octopus_energy", "octopus_energy_gas_ccc_333_current_day_rates",
            _fixture("octopus_rates", "rates", 20, 30))
    form = await _open(hass, make_entry(hass))
    values = [o["value"] for o in form["data_schema"].schema["source"].config["options"]]
    # Bez cen bieżących, średnich, `next_day_rates` (jutro), kanału controlled_load i gazu Octopusa.
    assert sorted(values) == sorted([f"entities|{amber_buy}|{amber_sell}", f"entities|{octo_buy}|{octo_sell}",
                                     f"entities|{pstryk_buy}|{pstryk_sell}"])


async def test_amber_pair_confirms_in_aud_without_writing_the_currency_option(hass: HomeAssistant, network_down):
    buy, sell = _amber_set(hass)
    entry = make_entry(hass)
    _form, confirm = await _choose(hass, entry, f"entities|{buy}")
    assert confirm["step_id"] == "price_source_confirm" and confirm["description_placeholders"]["currency"] == "AUD"
    await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert entry.options["entity_price_buy"] == buy and entry.options["entity_price_sell"] == sell
    assert "price_currency" not in entry.options


async def test_amber_pair_conflicts_with_a_different_currency_option(hass: HomeAssistant, network_down):
    buy, _sell = _amber_set(hass)
    _form, res = await _choose(hass, make_entry(hass, options={"price_currency": "EUR"}), f"entities|{buy}")
    assert res["errors"] == {"source": "currency_conflict"}
    assert res["description_placeholders"]["source_currency"] == "AUD"


async def test_platform_currency_conflicts_with_option_when_series_states_none(hass: HomeAssistant, network_down):
    buy, _sell = _octopus_set(hass)
    entry = make_entry(hass, options={"price_currency": "EUR"})
    _form, res = await _choose(hass, entry, f"entities|{buy}")
    assert res["errors"] == {"source": "currency_conflict"}
    assert res["description_placeholders"]["source_currency"] == "GBP"
    assert "entity_price_buy" not in entry.options


async def test_confirm_shows_buy_horizon_not_sell_horizon(hass: HomeAssistant, network_down):
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", _fixture("pstryk_all_prices", "All prices", 30, 60))
    _entity(hass, "sensor", "pstryk", "pstryk_current_sell_price", _fixture("pstryk_all_prices", "All prices", 10, 60))
    _form, confirm = await _choose(hass, make_entry(hass), f"entities|{buy}")
    assert int(confirm["description_placeholders"]["hours"]) >= 29


async def test_octopus_gas_is_not_offered_and_gas_cannot_be_picked_in_prices_form(hass: HomeAssistant, network_down):
    gas = _entity(hass, "event", "octopus_energy", "octopus_energy_gas_ccc_333_current_day_rates",
                  _fixture("octopus_rates", "rates", 20, 30), "2026-10-31T10:00:00")
    buy, sell = _octopus_set(hass)
    entry = make_entry(hass)
    form = await _open(hass, entry)
    values = [o["value"] for o in form["data_schema"].schema["source"].config["options"]]
    assert values == [f"entities|{buy}|{sell}"]
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    prices = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    field = next(sel for key, sel in prices["data_schema"].schema.items() if str(key) == "entity_price_buy")
    assert gas not in field.config["include_entities"] and buy in field.config["include_entities"]
    with pytest.raises(InvalidData):                         # selektor nie przyjmie encji spoza listy
        await hass.config_entries.options.async_configure(prices["flow_id"], {"entity_price_buy": gas})


async def test_ambiguous_pair_label_says_so(hass: HomeAssistant, network_down):
    attrs = _fixture("pstryk_all_prices", "All prices", 24, 60)
    for name in ("x_buy", "y_sell", "z_export"):
        _entity(hass, "sensor", "pstryk", name, attrs)
    form = await _open(hass, make_entry(hass))
    labels = [o["label"] for o in form["data_schema"].schema["source"].config["options"]]
    assert all("ambiguous" in label for label in labels)


async def test_prices_form_currency_change_alone_is_checked_against_saved_entity(hass: HomeAssistant, network_down):
    attrs = _fixture("pstryk_all_prices", "All prices", 24, 60, unit_of_measurement="SEK/kWh")
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", attrs)
    entry = make_entry(hass, options={"entity_price_buy": buy})
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": buy, "price_currency": "EUR"})
    assert res["errors"] == {"price_currency": "currency_conflict"}


async def test_prices_form_lists_amber_entities(hass: HomeAssistant, network_down):
    buy, _sell = _amber_set(hass)
    entry = make_entry(hass)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    field = next(sel for key, sel in form["data_schema"].schema.items() if str(key) == "entity_price_buy")
    assert buy in field.config["include_entities"]


async def _sent_prices(hass, entity_id, platform, attrs, options=None):
    from .test_price_actions import _refresh_and_flush, _sender
    er.async_get(hass).async_get_or_create("sensor", platform, entity_id.split(".")[1], suggested_object_id=entity_id.split(".")[1])
    hass.states.async_set(entity_id, "0.3", attrs)
    sender, cloud, _entry = await _sender(hass, {"entity_price_buy": entity_id, **(options or {})})
    await _refresh_and_flush(sender)
    return cloud.sent[0].get("prices")


def _now_forecasts(extra=None):
    from .test_price_actions import NOW
    start = NOW.replace(minute=0, second=0, microsecond=0)
    rows = [{"start_time": (start + timedelta(minutes=30 * i)).isoformat(),
             "end_time": (start + timedelta(minutes=30 * (i + 1))).isoformat(), "per_kwh": 0.3} for i in range(60)]
    return {"unit_of_measurement": "$/kWh", "forecasts": rows, **(extra or {})}


async def test_amber_entity_prices_are_sent_in_aud(hass: HomeAssistant):
    prices = await _sent_prices(hass, "sensor.home_general_forecast", "amberelectric", _now_forecasts())
    assert prices["currency"] == "AUD"


async def test_amber_explicit_currency_attribute_wins_over_the_integration_rule(hass: HomeAssistant):
    prices = await _sent_prices(hass, "sensor.home_general_forecast", "amberelectric", _now_forecasts({"currency": "NZD"}))
    assert prices["currency"] == "NZD"


async def test_dollar_unit_on_other_platforms_stays_rejected(hass: HomeAssistant):
    assert await _sent_prices(hass, "sensor.some_forecast", "some_other_platform", _now_forecasts()) is None
async def test_pair_confirms_with_hours_then_saves_and_sets_platform_currency(hass: HomeAssistant, network_down):
    buy, sell = _octopus_set(hass)
    entry = make_entry(hass, options={"price_buy_action": {"action": "x.y"}, "price_sell_action": {"action": "x.z"}})
    _form, confirm = await _choose(hass, entry, f"entities|{buy}")
    assert confirm["step_id"] == "price_source_confirm" and int(confirm["description_placeholders"]["hours"]) >= 9
    assert "entity_price_buy" not in entry.options                      # dopiero po potwierdzeniu
    done = await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["entity_price_buy"] == buy and entry.options["entity_price_sell"] == sell
    assert "price_buy_action" not in entry.options and "price_sell_action" not in entry.options
    assert entry.options["price_currency"] == "GBP"                     # seria nie podaje waluty, a opcji nie było


async def test_pair_without_sell_side_keeps_separately_set_sell(hass: HomeAssistant, network_down):
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", _fixture("pstryk_all_prices", "All prices", 24, 60))
    entry = make_entry(hass, options={"entity_price_sell": "sensor.my_sell"})
    _form, confirm = await _choose(hass, entry, f"entities|{buy}")
    await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert entry.options["entity_price_buy"] == buy and entry.options["entity_price_sell"] == "sensor.my_sell"


async def test_ambiguous_sells_are_offered_not_guessed(hass: HomeAssistant, network_down):
    attrs = _fixture("pstryk_all_prices", "All prices", 24, 60)
    _entity(hass, "sensor", "pstryk", "x_buy", attrs)
    _entity(hass, "sensor", "pstryk", "y_sell", attrs)
    _entity(hass, "sensor", "pstryk", "z_export", attrs)
    form = await _open(hass, make_entry(hass))
    values = [o["value"] for o in form["data_schema"].schema["source"].config["options"]]
    assert len(values) == 2 and all(v.startswith("entities|") for v in values)


async def test_short_forecast_pair_is_refused(hass: HomeAssistant, network_down):
    attrs = _fixture("pstryk_all_prices", "All prices", 4, 60)
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", attrs)
    entry = make_entry(hass)
    _form, res = await _choose(hass, entry, f"entities|{buy}")
    assert res["errors"] == {"source": "price_horizon_short"} and int(res["description_placeholders"]["hours"]) in (3, 4)
    assert "entity_price_buy" not in entry.options


async def test_pair_rechecked_at_choice_sell_that_lost_forecast_is_refused(hass: HomeAssistant, network_down):
    buy, sell = _pstryk_set(hass)
    entry = make_entry(hass)
    form = await _open(hass, entry)
    source = _source_id(form, f"entities|{buy}")
    hass.states.async_set(sell, "0.2", {"unit_of_measurement": "PLN/kWh"})       # sprzedaż straciła prognozę
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"source": source})
    assert res["errors"] == {"source": "prices_current_only"} and "entity_price_buy" not in entry.options


async def test_current_price_only_buy_that_lost_forecast_is_refused(hass: HomeAssistant, network_down):
    buy, _sell = _pstryk_set(hass)
    entry = make_entry(hass)
    form = await _open(hass, entry)
    source = _source_id(form, f"entities|{buy}")
    hass.states.async_set(buy, "0.61", {"unit_of_measurement": "PLN/kWh"})
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"source": source})
    assert res["errors"] == {"source": "prices_current_only"}


async def test_currency_from_unit_conflicts_with_option(hass: HomeAssistant, network_down):
    attrs = _fixture("pstryk_all_prices", "All prices", 24, 60, unit_of_measurement="SEK/kWh")
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", attrs)
    entry = make_entry(hass, options={"price_currency": "EUR"})
    _form, res = await _choose(hass, entry, f"entities|{buy}")
    assert res["errors"] == {"source": "currency_conflict"}
    assert res["description_placeholders"]["source_currency"] == "SEK"
    assert res["description_placeholders"]["option_currency"] == "EUR"


async def test_prices_form_currency_from_unit_conflicts(hass: HomeAssistant, network_down):
    attrs = _fixture("pstryk_all_prices", "All prices", 24, 60, unit_of_measurement="SEK/kWh")
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", attrs)
    entry = make_entry(hass)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": buy, "price_currency": "EUR"})
    assert res["errors"] == {"price_currency": "currency_conflict"}


async def test_current_price_only_entity_is_not_listed(hass: HomeAssistant, network_down):
    _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", {"unit_of_measurement": "PLN/kWh"}, "0.61")
    res = await _open(hass, make_entry(hass))
    assert res["type"] is FlowResultType.ABORT and res["reason"] == "no_price_sources"


async def test_disabled_entities_and_unloaded_entries_are_not_offered(hass: HomeAssistant, network_down):
    attrs = _fixture("pstryk_all_prices", "All prices", 24, 60)
    _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", attrs, disabled=True)
    MockConfigEntry(domain="tibber", title="Tibber", entry_id="t1").add_to_hass(hass)      # nie załadowany
    res = await _open(hass, make_entry(hass))
    assert res["type"] is FlowResultType.ABORT


async def test_prices_form_clears_only_the_replaced_side_action(hass: HomeAssistant, network_down):
    attrs = _fixture("pstryk_all_prices", "All prices", 24, 60)
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", attrs)
    entry = make_entry(hass, options={"price_buy_action": {"action": "tibber.get_prices"},
                                      "price_sell_action": {"action": "easyenergy.get_energy_return_prices"}})
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": buy})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert "price_buy_action" not in entry.options and "price_sell_action" in entry.options


async def test_prices_form_refuses_current_only_and_short(hass: HomeAssistant, network_down):
    hass.states.async_set("sensor.now_only", "0.61", {"unit_of_measurement": "PLN/kWh"})
    short = _entity(hass, "sensor", "pstryk", "short_buy", _fixture("pstryk_all_prices", "All prices", 3, 60))
    entry = make_entry(hass, options={"price_buy_action": {"action": "tibber.get_prices"}})
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    bad = await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": "sensor.now_only"})
    assert bad["errors"] == {"entity_price_buy": "prices_current_only"}
    bad = await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": short})
    assert bad["errors"] == {"entity_price_buy": "prices_not_usable"}
    assert "price_buy_action" in entry.options


async def test_hand_picked_excluded_entity_is_refused_only_for_its_integration(hass: HomeAssistant, network_down):
    """Wykluczenie z rejestru (`excluded_tokens`) należy do integracji, nie do samego tekstu id: encji
    controlled_load Ambera nie ma na liście, więc wpisanej ręcznie nie da się zapisać; ta sama nazwa innej
    integracji przechodzi."""
    amber = _entity(hass, "sensor", "amberelectric", "home_controlled_load_forecast",
                    _fixture("amber_forecasts", "forecasts", 36, 30, start=_next_boundary(30)))
    other = _entity(hass, "sensor", "pstryk", "pstryk_controlled_load_price",
                    _fixture("pstryk_all_prices", "All prices", 36, 60))
    entry = make_entry(hass)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    field = next(sel for key, sel in form["data_schema"].schema.items() if str(key) == "entity_price_buy")
    assert amber not in field.config["include_entities"] and other in field.config["include_entities"]
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": amber})
    assert "entity_price_buy" not in entry.options
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": other})
    assert done["type"] is FlowResultType.CREATE_ENTRY and entry.options["entity_price_buy"] == other


async def test_missing_currency_is_asked_for(hass: HomeAssistant, network_down):
    _loaded(hass, "tibber", "Tibber", "t1")
    _register(hass, "tibber", "get_prices", lambda call: {"prices": {"Dom": [
        {"start_time": s["start_time"], "price": 1.2} for s in _hours(30)]}}, [])
    entry = make_entry(hass)
    _form, details = await _choose(hass, entry, "action|tibber|t1")
    assert details["step_id"] == "price_source_details"
    res = await hass.config_entries.options.async_configure(details["flow_id"], {})
    assert res["errors"] == {"currency": "currency_missing"}


async def test_tomorrow_failure_does_not_fail_the_probe(hass: HomeAssistant, network_down):
    _loaded(hass, "entsoe", "ENTSO-e", "en1")
    today = datetime.now(timezone.utc).date()

    def build(call):
        if datetime.fromisoformat(call.data["start"]).date() > today:
            raise RuntimeError("tomorrow not published")
        return {"prices": _hours(30, 60, "timestamp", "price", 80.0)}

    _register(hass, "entsoe", "get_energy_prices", build, [])
    entry = make_entry(hass)
    _form, details = await _choose(hass, entry, "action|entsoe|en1")
    res = await hass.config_entries.options.async_configure(details["flow_id"], {"unit": "MWh", "currency": "EUR"})
    assert res["step_id"] == "price_source_confirm"


async def test_slow_action_times_out(hass: HomeAssistant, network_down, monkeypatch):
    _loaded(hass, "amberelectric", "Amber home", "amb1")
    monkeypatch.setattr(config_flow, "PROBE_TIMEOUT_S", 0.05)

    async def slow(call: ServiceCall):
        await asyncio.sleep(2)
        return {"forecasts": _hours(36, 30)}

    hass.services.async_register("amberelectric", "get_forecasts", slow, schema=vol.Schema({}, extra=vol.ALLOW_EXTRA),
                                 supports_response=SupportsResponse.ONLY)
    entry = make_entry(hass)
    form = await _open(hass, entry)
    res = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|amberelectric|amb1"})
    assert res["errors"] == {"base": "price_source_unusable"}


async def test_action_source_without_sell_keeps_existing_sell_entity_and_errors_stay_off_currency_field(
        hass: HomeAssistant, network_down):
    _loaded(hass, "entsoe", "ENTSO-e", "en1")
    _register(hass, "entsoe", "get_energy_prices", lambda call: {"prices": _hours(30, 60, "timestamp", "price", 80.0)}, [])
    entry = make_entry(hass, options={"entity_price_sell": "sensor.my_sell"})
    _form, details = await _choose(hass, entry, "action|entsoe|en1")
    confirm = await hass.config_entries.options.async_configure(details["flow_id"], {"unit": "MWh", "currency": "EUR"})
    await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert entry.options["entity_price_sell"] == "sensor.my_sell"


async def test_unusable_action_error_is_not_under_currency_field(hass: HomeAssistant, network_down):
    _loaded(hass, "entsoe", "ENTSO-e", "en1")

    def boom(call):
        raise RuntimeError("down")
    _register(hass, "entsoe", "get_energy_prices", boom, [])
    _form, details = await _choose(hass, make_entry(hass), "action|entsoe|en1")
    res = await hass.config_entries.options.async_configure(details["flow_id"], {"unit": "MWh", "currency": "EUR"})
    assert res["errors"] == {"base": "price_source_unusable"}


def test_core_price_domains_exist_in_home_assistant():
    from homeassistant import components
    from tests.discovery.test_classify import VERIFIED_CORE
    root = Path(components.__file__).parent
    assert all((root / domain).is_dir() for domain in VERIFIED_CORE)


@pytest.mark.parametrize("domain,expected", [
    ("tibber", [("home", "Optional", None), ("currency", "Optional", None)]),
    ("nordpool", [("area", "Required", None), ("currency", "Required", None)]),
    ("entsoe", [("unit", "Required", None), ("currency", "Required", None)]),
    ("energyzero", [("incl_vat", "Required", True)]),
    ("easyenergy", [("incl_vat", "Required", True)]),
])
async def test_details_form_fields_per_integration(hass: HomeAssistant, network_down, domain, expected):
    _loaded(hass, domain, domain, "x1")
    _form, details = await _choose(hass, make_entry(hass), f"action|{domain}|x1")
    assert details["step_id"] == "price_source_details"
    got = [(str(k), type(k).__name__, k.default() if k.default is not vol.UNDEFINED else None)
           for k in details["data_schema"].schema]
    assert got == expected
    unit = [v for k, v in details["data_schema"].schema.items() if str(k) == "unit"]
    if unit:
        assert unit[0].config["options"] == ["kWh", "MWh"]


async def test_amber_action_source_has_no_details_form(hass: HomeAssistant, network_down):
    _loaded(hass, "amberelectric", "Amber home", "amb1")
    _register(hass, "amberelectric", "get_forecasts", lambda call: {"forecasts": []}, [])
    _form, res = await _choose(hass, make_entry(hass), "action|amberelectric|amb1")
    assert res["step_id"] == "price_source" and res["errors"] == {"base": "price_source_unusable"}


def _frank_attrs(hours: int) -> dict:
    """Atrybuty z fixture'u (`prices` z `from` / `till` / `price`), seria od bieżącej godziny."""
    attrs = json.loads((FIXTURES / "frank_energie_prices.json").read_text())["attributes"]
    start = _hour_start()
    return {**attrs, "prices": [{"from": (start + timedelta(hours=i)).isoformat(),
                                 "till": (start + timedelta(hours=i + 1)).isoformat(), "price": 0.2}
                                for i in range(hours)]}


async def test_frank_energie_entity_is_not_suggested_but_can_be_picked_by_hand(hass: HomeAssistant, network_down):
    buy = _entity(hass, "sensor", "frank_energie", "frank_energie_prices_current_electricity_price_all_in",
                  _frank_attrs(24))
    # Ten sam kształt i jednostkę ma np. sam VAT — integracja z samym raportem wykrywania nie jest podpowiadana.
    vat = _entity(hass, "sensor", "frank_energie", "frank_energie_prices_current_electricity_vat_price",
                  _frank_attrs(24))
    _entity(hass, "sensor", "epex_spot", "epex_spot_price", _frank_attrs(24))
    entry = make_entry(hass)
    res = await _open(hass, entry)
    assert res["type"] is FlowResultType.ABORT and res["reason"] == "no_price_sources"
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    field = next(sel for key, sel in form["data_schema"].schema.items() if str(key) == "entity_price_buy")
    assert {buy, vat} <= set(field.config["include_entities"])
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": buy})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["entity_price_buy"] == buy and entry.options.get("price_buy_action") is None


# ── informacja: sprzedaż nie ma jeszcze jutra, kupno już ma ───────────────


def _local_midnight(days: int = 0) -> datetime:
    """Lokalna północ (strefa HA) za `days` dni, w UTC."""
    from homeassistant.util import dt as dt_util
    today = dt_util.start_of_local_day()
    return dt_util.start_of_local_day(today.date() + timedelta(days=days)).astimezone(timezone.utc)


def _span_h(start: datetime, end: datetime) -> int:
    return int((end - start) / timedelta(hours=1))


def _until(end: datetime, step_min: int = 30) -> list[dict]:
    """Prognoza Ambera od najbliższej granicy siatki do `end`."""
    start = _next_boundary(step_min)
    return [
        {"start_time": (start + timedelta(minutes=i * step_min)).isoformat(),
         "end_time": (start + timedelta(minutes=(i + 1) * step_min)).isoformat(), "per_kwh": 0.3}
        for i in range(int((end - start) / timedelta(minutes=step_min)))]


async def test_pair_with_sell_lacking_tomorrow_notes_it_on_the_confirm_step(
        hass: HomeAssistant, network_down):
    midnight, day_after, noon_tomorrow = _local_midnight(), _local_midnight(2), _local_midnight(1) + timedelta(hours=12)
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price",
                  _fixture("pstryk_all_prices", "All prices", _span_h(midnight, day_after), 60, start=midnight))
    sell = _entity(hass, "sensor", "pstryk", "pstryk_current_sell_price",
                   _fixture("pstryk_all_prices", "All prices", _span_h(midnight, noon_tomorrow), 60, start=midnight))
    entry = make_entry(hass)
    _form, confirm = await _choose(hass, entry, f"entities|{buy}")
    # Informacja w samym potwierdzeniu: jedno zatwierdzenie zapisuje, drugiego okna (do zamknięcia) nie ma.
    assert confirm["type"] is FlowResultType.FORM and confirm["step_id"] == "price_source_confirm_sell"
    assert confirm["description_placeholders"]["source"] == sell
    assert {"hours", "currency"} <= set(confirm["description_placeholders"])
    assert "entity_price_buy" not in entry.options                      # dopiero po zatwierdzeniu
    done = await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["entity_price_buy"] == buy and entry.options["entity_price_sell"] == sell


async def test_pair_with_both_sides_covering_tomorrow_saves_after_confirm(hass: HomeAssistant, network_down):
    midnight, day_after = _local_midnight(), _local_midnight(2)
    attrs = _fixture("pstryk_all_prices", "All prices", _span_h(midnight, day_after), 60, start=midnight)
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price", attrs)
    _entity(hass, "sensor", "pstryk", "pstryk_current_sell_price", attrs)
    entry = make_entry(hass)
    _form, confirm = await _choose(hass, entry, f"entities|{buy}")
    done = await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY and entry.options["entity_price_buy"] == buy


async def test_action_source_with_sell_lacking_tomorrow_names_the_sell_action(hass: HomeAssistant, network_down):
    _loaded(hass, "amberelectric", "Amber home", "amb1")
    day_after, noon_tomorrow = _local_midnight(2), _local_midnight(1) + timedelta(hours=12)
    _register(hass, "amberelectric", "get_forecasts", lambda call: {"forecasts": _until(
        day_after if call.data["channel_type"] == "general" else noon_tomorrow)}, [])
    entry = make_entry(hass)
    form = await _open(hass, entry)
    confirm = await hass.config_entries.options.async_configure(form["flow_id"], {"source": "action|amberelectric|amb1"})
    assert confirm["step_id"] == "price_source_confirm_sell"
    assert confirm["description_placeholders"]["source"] == "amberelectric.get_forecasts"
    done = await hass.config_entries.options.async_configure(confirm["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["price_sell_action"]["data"]["channel_type"] == "feed_in"


async def test_prices_form_with_sell_lacking_tomorrow_saves_before_the_note(hass: HomeAssistant, network_down):
    """Formularz bez potwierdzenia: zapis od razu, informacja po nim (zamknięcie okna niczego nie cofa)."""
    midnight, day_after, noon_tomorrow = _local_midnight(), _local_midnight(2), _local_midnight(1) + timedelta(hours=12)
    buy = _entity(hass, "sensor", "pstryk", "pstryk_current_buy_price",
                  _fixture("pstryk_all_prices", "All prices", _span_h(midnight, day_after), 60, start=midnight))
    sell = _entity(hass, "sensor", "pstryk", "pstryk_current_sell_price",
                   _fixture("pstryk_all_prices", "All prices", _span_h(midnight, noon_tomorrow), 60, start=midnight))
    entry = make_entry(hass)
    updates = []

    async def on_update(_hass, changed):
        updates.append(changed.entry_id)

    entry.async_on_unload(entry.add_update_listener(on_update))
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    res = await hass.config_entries.options.async_configure(
        form["flow_id"], {"entity_price_buy": buy, "entity_price_sell": sell})
    assert res["type"] is FlowResultType.ABORT and res["reason"] == "price_sell_tomorrow"
    assert res["description_placeholders"] == {"source": sell}
    assert entry.options["entity_price_buy"] == buy and entry.options["entity_price_sell"] == sell
    await hass.async_block_till_done()
    assert updates == [entry.entry_id]                   # zapis przed informacją przeładowuje wpis jak create_entry
