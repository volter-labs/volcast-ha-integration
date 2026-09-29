import asyncio
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

import tests.test_config_flow_menu  # noqa: F401 — atrapy flow

_ce = sys.modules["homeassistant.config_entries"]
for _name, _fn in {
    "async_show_menu": lambda self, *, step_id, menu_options, **_: {"type": "menu", "step_id": step_id,
                                                                    "menu_options": menu_options},
    "async_abort": lambda self, *, reason, **_: {"type": "abort", "reason": reason},
}.items():
    if not hasattr(_ce.OptionsFlowWithConfigEntry, _name):
        setattr(_ce.OptionsFlowWithConfigEntry, _name, _fn)

_sel = sys.modules["homeassistant.helpers.selector"]


class _SelectorConfig(dict):
    def __init__(self, **kw):
        super().__init__(**kw)


class _Selector:
    def __init__(self, config=None):
        self.config = config or {}

    def __call__(self, value):
        return value


for _name, _val in {"EntitySelector": _Selector, "EntitySelectorConfig": _SelectorConfig}.items():
    if not hasattr(_sel, _name):
        setattr(_sel, _name, _val)

from custom_components.volcast.config_flow import VolcastOptionsFlow  # noqa: E402
from custom_components.volcast.const import DOMAIN  # noqa: E402
from custom_components.volcast.control import runtime as rt_mod  # noqa: E402
from custom_components.volcast.core.control.caps import entity_mode_ready  # noqa: E402
from custom_components.volcast.core.control.select import ProfileChoice  # noqa: E402
from custom_components.volcast.core.profile import load_builtin  # noqa: E402

BASE = "https://s.example.test"
BACKEND = {"base_url": BASE, **{k: f"{BASE}/functions/v1/{k}" for k in (
    "forecast", "submit_production", "telemetry", "schedule", "history_import", "pairing")}}
WRITE_KEYS = ("mode", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")
NOW = __import__("homeassistant.util.dt", fromlist=["utcnow"]).utcnow()   # zegar atrapy HA


def _price_attrs():
    start = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    return {"currency": "PLN", "raw_today": [
        {"start": (start + timedelta(hours=i)).isoformat(), "end": (start + timedelta(hours=i + 1)).isoformat(),
         "value": 0.5} for i in range(24)]}


class _States:
    def __init__(self, attrs):
        self._attrs = attrs

    def get(self, eid):
        a = self._attrs.get(eid)
        return None if a is None else SimpleNamespace(entity_id=eid, state="0.5", attributes=a)

    def async_all(self, domain=None):
        return [self.get(e) for e in self._attrs if domain is None or e.startswith(domain + ".")]


def flow(*, paired=True, options=None, runtime=None, prices=None, data=None):
    entry = SimpleNamespace(entry_id="e1", data=data or {"api_key": "vk_x", **({"backend": BACKEND} if paired else {})},
                            options=options or {})
    f = VolcastOptionsFlow(entry)
    f.hass = SimpleNamespace(data={DOMAIN: {"e1": {"control": runtime}}},
                             states=_States({"sensor.nordpool": _price_attrs()} if prices is None else prices),
                             config=SimpleNamespace(time_zone="Europe/Warsaw"))
    return f


def rt(domain="goodwe", mapped=WRITE_KEYS, executor=None):
    return SimpleNamespace(choice=ProfileChoice(load_builtin("goodwe-et"), domain, "GW8KN-ET"),
                           mapped={k: f"x.{k}" for k in mapped}, executor=executor)


def test_unpaired_entry_keeps_single_form_step_init():
    r = asyncio.run(flow(paired=False).async_step_init())
    assert (r["type"], r["step_id"]) == ("form", "init")


def test_discovery_only_entry_has_no_options():
    r = asyncio.run(flow(data={"mode": "discovery_only"}).async_step_init())
    assert r == {"type": "create_entry", "data": {}}


def test_paired_entry_shows_menu_without_default():
    r = asyncio.run(flow().async_step_init())
    assert r == {"type": "menu", "step_id": "init", "menu_options": ["forecast", "control", "details", "prices"]}
    c = asyncio.run(flow().async_step_control())
    # trzy pozycje, żadnej domyślnej („Bezpośrednio” sprawdza dostępność dopiero po wyborze)
    assert c == {"type": "menu", "step_id": "control",
                 "menu_options": ["control_entities", "control_direct", "control_off"]}


def test_forecast_step_merges_options_keeps_control():
    f = flow(options={"control_mode": "entities", "update_interval": 60})
    r = asyncio.run(f.async_step_forecast({"update_interval": 30, "peak_threshold": 80}))
    assert r["data"] == {"control_mode": "entities", "update_interval": 30, "peak_threshold": 80}


def test_forecast_step_drops_omitted_forecast_key_keeps_control():
    f = flow(options={"control_mode": "entities", "pv_energy_entity": "sensor.old", "update_interval": 60})
    r = asyncio.run(f.async_step_forecast({"update_interval": 30, "pv_power_entity": ""}))
    assert r["data"] == {"control_mode": "entities", "update_interval": 30}


def test_unpaired_init_also_merges():
    f = flow(paired=False, options={"something_else": 1})
    r = asyncio.run(f.async_step_init({"update_interval": 30}))
    assert r["data"] == {"something_else": 1, "update_interval": 30}


def test_control_entities_sets_mode_and_profile():
    r = asyncio.run(flow(runtime=rt()).async_step_control_entities())
    assert r["data"] == {"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe"}


def test_control_entities_unavailable_aborts():
    # Bez encji trybu nie ma sterowania; brak innej nastawy tylko ją wyłącza (niżej).
    for runtime in (None, rt(domain=None), rt(mapped=("power_w",)), rt(mapped=WRITE_KEYS[1:])):
        assert asyncio.run(flow(runtime=runtime).async_step_control_entities()) == {
            "type": "abort", "reason": "entity_mode_unavailable"}


def test_readiness_rule_is_shared_with_onboarding():
    from custom_components.volcast import onboarding
    gw = ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET")
    assert entity_mode_ready(gw, dict.fromkeys(WRITE_KEYS)) is True
    assert entity_mode_ready(gw, dict.fromkeys(WRITE_KEYS[1:])) is False          # bez trybu
    assert entity_mode_ready(gw, dict.fromkeys(WRITE_KEYS[:-1])) is True          # bez jednej nastawy
    assert entity_mode_ready(ProfileChoice(gw.profile, None, None), dict.fromkeys(WRITE_KEYS)) is False
    assert entity_mode_ready(None, {}) is False
    assert onboarding.entity_mode_ready is entity_mode_ready


def test_control_off_removes_mode():
    r = asyncio.run(flow(options={"control_mode": "entities", "x": 1}).async_step_control_off())
    assert r["data"] == {"x": 1}


def test_details_saves_telemetry_map_without_empty():
    r = asyncio.run(flow().async_step_details({"soc": "sensor.soc", "pv_power_w": "", "grid_power_negate": True,
                                               "rated_power_w": 8000, "load_energy_entity": "sensor.house"}))
    assert r["data"] == {"telemetry_map": {"soc": "sensor.soc"}, "grid_power_negate": True,
                         "rated_power_w": 8000, "load_energy_entity": "sensor.house"}


def test_details_form_renders():
    r = asyncio.run(flow(options={"telemetry_map": {"soc": "sensor.soc"}}).async_step_details())
    assert (r["type"], r["step_id"]) == ("form", "details")


@pytest.mark.parametrize("watts,ok", [(999, False), (1000, True), (30000, True), (30001, False)])
def test_details_rated_power_range(watts, ok):
    import voluptuous as vol
    schema = flow()._details_schema()
    if ok:
        assert schema({"rated_power_w": watts})["rated_power_w"] == watts
    else:
        with pytest.raises(vol.Invalid):
            schema({"rated_power_w": watts})


def test_prices_saved_and_cleared():
    f = flow(options={"entity_price_sell": "sensor.old"})
    r = asyncio.run(f.async_step_prices({"entity_price_buy": "sensor.nordpool", "entity_price_sell": ""}))
    assert r["data"] == {"entity_price_buy": "sensor.nordpool"}


def test_prices_unusable_buy_entity_is_rejected():
    f = flow(prices={"sensor.nordpool": _price_attrs(), "sensor.bad": {"unit_of_measurement": "PLN/kWh"}})
    r = asyncio.run(f.async_step_prices({"entity_price_buy": "sensor.bad"}))
    assert r == {"type": "form", "step_id": "prices", "errors": {"entity_price_buy": "prices_not_usable"}}


def test_prices_buy_can_be_cleared():
    f = flow(options={"entity_price_buy": "sensor.nordpool", "price_currency": "PLN"})
    r = asyncio.run(f.async_step_prices({"entity_price_buy": "", "price_currency": ""}))
    assert r["data"] == {}


def test_prices_form_offers_only_usable_entities():
    f = flow(prices={"sensor.nordpool": _price_attrs(), "sensor.bad": {"x": 1}, "sensor.temp": {}})
    assert f._usable_price_entities() == ["sensor.nordpool"]
    r = asyncio.run(f.async_step_prices())
    assert (r["type"], r["step_id"]) == ("form", "prices")


# ── powrót do trybu bazowego przed przeładowaniem ──────────────────────────


def _owned_executor(monkeypatch):
    from tests.control.test_executor import make, ready
    h, ex = make(monkeypatch=monkeypatch)

    async def go():
        await ready(ex)
        await ex.async_tick()
    asyncio.run(go())
    assert ex.owned and h.states.get("select.goodwe_ems_mode").state == "sell_power"
    return h, ex


def test_control_off_restores_old_executor_before_reload(monkeypatch):
    h, ex = _owned_executor(monkeypatch)
    f = flow(options={"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe"},
             runtime=rt(executor=ex))
    r = asyncio.run(f.async_step_control_off())
    assert r["type"] == "create_entry" and "control_mode" not in r["data"]
    assert h.states.get("select.goodwe_ems_mode").state == "auto" and not ex.owned


def test_mapping_change_restores_old_executor_before_reload(monkeypatch):
    h, ex = _owned_executor(monkeypatch)
    f = flow(options={"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe_old"},
             runtime=rt(executor=ex))
    r = asyncio.run(f.async_step_control_entities())
    assert r["data"]["inverter_domain"] == "goodwe"
    assert h.states.get("select.goodwe_ems_mode").state == "auto" and not ex.owned


def test_unrelated_option_change_keeps_control(monkeypatch):
    h, ex = _owned_executor(monkeypatch)
    f = flow(options={"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe"},
             runtime=rt(executor=ex))
    asyncio.run(f.async_step_forecast({"update_interval": 30}))
    asyncio.run(f.async_step_control_entities())                  # ten sam wybór — bez zmiany
    assert h.states.get("select.goodwe_ems_mode").state == "sell_power" and ex.owned


@pytest.mark.parametrize("old,new,changed", [
    ({}, {}, False),
    ({"control_mode": "entities"}, {}, True),
    ({"control_mode": "entities", "profile_id": "a"}, {"control_mode": "entities", "profile_id": "b"}, True),
    ({"inverter_domain": "a"}, {"inverter_domain": "b"}, True),
    ({"control_mode": "entities", "update_interval": 60}, {"control_mode": "entities", "update_interval": 30}, False),
    ({"telemetry_map": {"soc": "a"}}, {"telemetry_map": {"soc": "b"}}, False),
])
def test_control_options_changed(old, new, changed):
    assert rt_mod.control_options_changed(old, new) is changed


def test_restore_helper_never_raises_and_skips_when_not_owned():
    class Boom:
        owned = True

        async def async_restore_now(self):
            raise RuntimeError("x")

    class NotOwned:
        owned = False
        called = False

        async def async_restore_now(self):
            NotOwned.called = True

    old, new = {"control_mode": "entities"}, {}
    assert asyncio.run(rt_mod.async_restore_if_control_changed(SimpleNamespace(executor=Boom()), old, new)) is False
    assert asyncio.run(rt_mod.async_restore_if_control_changed(SimpleNamespace(executor=NotOwned()), old, new)) is False
    assert NotOwned.called is False
    assert asyncio.run(rt_mod.async_restore_if_control_changed(None, old, new)) is False


def test_strings_have_new_steps_and_errors():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "custom_components" / "volcast"
    for name in ("strings.json", "translations/en.json"):
        opts = json.loads((root / name).read_text(encoding="utf-8"))["options"]
        assert {"init", "forecast", "control", "details", "prices"} <= set(opts["step"])
        assert set(opts["step"]["init"]["menu_options"]) == {"forecast", "control", "details", "prices"}
        assert set(opts["step"]["control"]["menu_options"]) == {"control_entities", "control_direct", "control_off"}
        assert "entity_mode_unavailable" in opts["abort"] and "prices_not_usable" in opts["error"]
        assert "currency_invalid" in opts["error"]
        assert "verified" in opts["step"]["control"]["description"]


# ── pola opcjonalne: wyczyszczone pole zostaje puste ───────────────────────


def _field(schema, name):
    for key, value in schema.schema.items():
        if str(key) == name:
            return key, value
    raise KeyError(name)


@pytest.mark.parametrize("schema_name,field", [
    ("_details_schema", "soc"), ("_details_schema", "load_energy_entity"),
    ("_prices_schema", "entity_price_buy"), ("_prices_schema", "entity_price_sell"),
    ("_forecast_schema", "pv_energy_entity"),
])
def test_entity_fields_use_suggested_value_not_default(schema_name, field):
    import voluptuous as vol
    f = flow(options={"telemetry_map": {"soc": "sensor.soc"}, "load_energy_entity": "sensor.house",
                      "entity_price_buy": "sensor.nordpool", "entity_price_sell": "sensor.sell",
                      "pv_energy_entity": "sensor.pv"})
    key, _ = _field(getattr(f, schema_name)(), field)
    assert key.default is vol.UNDEFINED
    assert key.description["suggested_value"]


def test_details_fields_submitted_without_key_are_cleared():
    f = flow(options={"telemetry_map": {"soc": "sensor.soc"}, "load_energy_entity": "sensor.house",
                      "control_mode": "entities"})
    r = asyncio.run(f.async_step_details({"grid_power_negate": False}))
    assert r["data"] == {"control_mode": "entities"}


def test_prices_fields_submitted_without_key_are_cleared():
    f = flow(options={"entity_price_buy": "sensor.nordpool", "entity_price_sell": "sensor.sell",
                      "price_currency": "PLN"})
    r = asyncio.run(f.async_step_prices({}))
    assert r["data"] == {}


def test_saved_but_now_unusable_buy_entity_does_not_block_saving():
    f = flow(options={"entity_price_buy": "sensor.bad"},
             prices={"sensor.nordpool": _price_attrs(), "sensor.bad": {"unit_of_measurement": "PLN/kWh"}})
    r = asyncio.run(f.async_step_prices({"entity_price_buy": "sensor.bad", "price_currency": "eur"}))
    assert r["data"] == {"entity_price_buy": "sensor.bad", "price_currency": "EUR"}
    key, sel = _field(f._prices_schema(), "entity_price_buy")
    assert sel.config["include_entities"] == ["sensor.bad", "sensor.nordpool"]


def test_include_entities_only_usable_and_absent_when_none_usable():
    f = flow(prices={"sensor.nordpool": _price_attrs(), "sensor.bad": {"x": 1}})
    _, sel = _field(f._prices_schema(), "entity_price_buy")
    assert sel.config["include_entities"] == ["sensor.nordpool"]
    f = flow(prices={"sensor.bad": {"x": 1}})
    _, sel = _field(f._prices_schema(), "entity_price_buy")
    assert "include_entities" not in sel.config


def test_newly_picked_unusable_sell_entity_and_bad_currency_are_errors():
    f = flow(prices={"sensor.nordpool": _price_attrs(), "sensor.bad": {"x": 1}})
    r = asyncio.run(f.async_step_prices({"entity_price_buy": "sensor.nordpool", "entity_price_sell": "sensor.bad",
                                         "price_currency": "zł"}))
    assert r["errors"] == {"entity_price_sell": "prices_not_usable", "price_currency": "currency_invalid"}


def test_battery_capacity_range_from_limits():
    import voluptuous as vol
    schema = flow()._details_schema()
    assert schema({"battery_capacity_kwh": 0.5})["battery_capacity_kwh"] == 0.5
    with pytest.raises(vol.Invalid):
        schema({"battery_capacity_kwh": 200.1})


def test_failed_restore_still_saves_and_keeps_ownership(monkeypatch):
    h, ex = _owned_executor(monkeypatch)

    async def bad_write(_w):
        raise RuntimeError("down")
    ex._writer.async_write = bad_write
    f = flow(options={"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe"},
             runtime=rt(executor=ex))
    r = asyncio.run(f.async_step_control_off())
    assert r["type"] == "create_entry" and "control_mode" not in r["data"]
    assert ex.owned is True                                       # następny wykonawca ponowi powrót


def test_same_choice_after_remote_choice_is_not_a_control_change(monkeypatch):
    # Wybór zdalny zapisuje te same trzy klucze co opcje — ponowny wybór nie oddaje falownika.
    from custom_components.volcast.core.control.caps import entity_mode_options
    h, ex = _owned_executor(monkeypatch)
    runtime = rt(executor=ex)
    f = flow(options=entity_mode_options(runtime.choice), runtime=runtime)
    asyncio.run(f.async_step_control_entities())
    assert ex.owned and h.states.get("select.goodwe_ems_mode").state == "sell_power"
