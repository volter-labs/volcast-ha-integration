import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.volcast import onboarding as ob_mod
from custom_components.volcast.cloud.client import AccountState, PairingSession, PollResult
from custom_components.volcast.control.runtime import ControlRuntime
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.onboarding import STEP_KEYS, Onboarding, plan_outcome, progress_payload

from .core.test_price_actions import tibber_response

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
S = PairingSession("s1", "p", "https://volcast.app/connect?s=s1", "t")
WRITE_KEYS = ("mode", "power_w", "soc_min", "soc_max", "export_limit_w", "export_limit_enabled")
PRICE = "sensor.nordpool_kwh_pl"
REPORT = {"inverters": [{"domain": "goodwe", "host": "192.168.1.50",
                         "devices": [{"manufacturer": "GoodWe", "model": "GW8KN-ET"}]}],
          "price_entities": [{"entity_id": PRICE, "platform": "nordpool"}],
          "energy_sensors": [{"entity_id": "sensor.house_consumption", "days_of_statistics": 58}]}


def _price_attrs(hours=24):
    start = datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc)
    return {"currency": "PLN", "raw_today": [
        {"start": (start + timedelta(hours=i)).isoformat(), "end": (start + timedelta(hours=i + 1)).isoformat(),
         "value": 0.5} for i in range(hours)]}


class States:
    def __init__(self, attrs):
        self._attrs = attrs

    def get(self, eid):
        a = self._attrs.get(eid)
        return None if a is None else SimpleNamespace(state="0.5", attributes=a)


class Client:
    def __init__(self, polls, plan=None):
        self.progress, self.polls, self.plan, self.plan_calls = [], list(polls), plan, 0
        self.account, self.account_calls = None, 0

    async def async_account_state(self, s):
        self.account_calls += 1
        if isinstance(self.account, Exception):
            raise self.account
        return self.account

    async def async_progress(self, s, steps):
        self.progress.append(steps)
        return True

    async def async_poll(self, s):
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    async def async_request_plan(self, s):
        self.plan_calls += 1
        plan = self.plan
        if isinstance(plan, list):
            plan = plan.pop(0) if len(plan) > 1 else plan[0]
        return plan


def make(polls, *, plan=None, report=REPORT, mapped=WRITE_KEYS, clock_step=60, prices=None, options=None,
         imported=None, choice=None, entry=None, rated=8000.0, inverter_entities=(), gaps=None, rejected=False):
    t = [NOW]

    def utcnow():
        return t[0]

    async def sleep(_s):
        t[0] = t[0] + timedelta(seconds=clock_step)

    entry = entry or SimpleNamespace(entry_id="e1", data={"api_key": "vk_x", "pairing": {"session_id": "s1"}},
                                     options=dict({"load_energy_entity": "sensor.house_consumption"}
                                                  if options is None else options))
    hass = SimpleNamespace(
        data={}, loop=SimpleNamespace(call_soon=MagicMock()),
        config=SimpleNamespace(time_zone="Europe/Warsaw"),
        states=States({PRICE: _price_attrs()} if prices is None else prices),
        config_entries=SimpleNamespace(
            async_get_entry=lambda eid: entry,
            async_update_entry=MagicMock(side_effect=lambda e, **kw: [setattr(e, k, v) for k, v in kw.items()])))
    fetch = SimpleNamespace(async_refresh=MagicMock(side_effect=lambda: _ret("accepted")))
    rt = SimpleNamespace(choice=choice or ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET"),
                         mapped={k: f"x.{k}" for k in mapped}, rated_power_w=rated, fetcher=fetch,
                         inverter_entities=frozenset(inverter_entities))
    if gaps is not None:
        rt.entity_gaps = lambda: gaps
    rt.executor = SimpleNamespace(write_rejected=rejected, refresh_entity_gaps=MagicMock())
    rt.executor_calls = rt.executor.refresh_entity_gaps
    client = Client(polls, plan)
    ob = Onboarding(hass, "e1", client=client, session=S, live_until=NOW + timedelta(minutes=30),
                    runtime=lambda: rt, report=lambda: report,
                    import_history=lambda: _ret({"accepted": 1392} if imported is None else imported),
                    utcnow=utcnow, sleep=sleep, discovery_wait_s=0.0, choice_poll_s=5.0)
    ob.test_rt = rt
    return ob, client, entry


async def _ret(v):
    return v


def last(client):
    return {s["key"]: s for s in client.progress[-1]}


OK_PLAN = {"success": True, "skipped": None, "slots_count": 24}


def test_progress_payload_valid():
    p = progress_payload({"prices": ("choice", "x" * 300), "account": ("done", None)})
    assert [s["key"] for s in p] == ["account", "prices"] and len(p[1]["detail"]) == 200
    assert all(s["key"] in STEP_KEYS and s["state"] in {"pending", "active", "done", "choice", "error"} for s in p)


def test_progress_payload_keeps_invisible_chars_for_the_cloud_to_strip():
    # Chmura czyści znaki niewidoczne sama — integracja niczego nie odrzuca.
    p = progress_payload({"inverter": ("done", "Good​We")})
    assert p == [{"key": "inverter", "state": "done", "detail": "Good​We"}]


def test_happy_path_publishes_all_steps_and_first_plan():
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    st = last(client)
    assert list(st) == list(STEP_KEYS)
    assert st["account"]["state"] == "done"
    assert st["inverter"]["detail"] == "GoodWe GW8KN-ET"
    assert "192.168.1.50" not in str(st)  # local address never leaves HA in pairing progress
    assert st["installation"]["detail"] == "8 kW"
    assert st["capabilities"] == {"key": "capabilities", "state": "done",
                                  "detail": ", ".join(sorted(WRITE_KEYS))}
    assert st["control_mode"]["state"] == "choice" and st["prices"]["state"] == "choice"
    assert st["prices"]["detail"] == f"nordpool ({PRICE}) · PLN"
    assert st["consumption"]["state"] == "done" and st["consumption"]["detail"] == "58 days of history"
    assert st["first_plan"] == {"key": "first_plan", "state": "done", "detail": "24 slots"}
    assert ob.first_plan_outcome == "ok"


def test_remote_entities_choice_sets_option():
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "entities"})], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    assert entry.options["control_mode"] == "entities" and last(client)["control_mode"]["state"] == "done"
    assert last(client)["control_mode"]["detail"] == "entities"
    assert entry.options["load_energy_entity"] == "sensor.house_consumption"      # reszta opcji zostaje
    assert entry.options["profile_id"] == "goodwe-et" and entry.options["inverter_domain"] == "goodwe"


@pytest.mark.parametrize("mapped,choice", [
    (WRITE_KEYS[1:], None),                                             # brak encji trybu
    (WRITE_KEYS, ProfileChoice(load_builtin("goodwe-et"), None, "GW8KN-ET")),   # tylko odczyt
])
def test_remote_entities_choice_not_ready_is_error(mapped, choice):
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "entities"})], plan=OK_PLAN,
                             mapped=mapped, choice=choice)
    asyncio.run(ob.async_run())
    st = last(client)
    assert "control_mode" not in entry.options
    assert st["control_mode"] == {"key": "control_mode", "state": "error",
                                  "detail": "inverter control entities not found"}


def _entities_choice(**kw):
    return make([PollResult("consumed", choices={"control_mode": "entities"})], plan=OK_PLAN, **kw)


def test_disabled_control_entities_give_the_disabled_phrase():
    from custom_components.volcast.control.executor import ControlGaps
    ob, client, entry = _entities_choice(mapped=WRITE_KEYS[1:], gaps=ControlGaps(disabled=("select.gw_mode",)))
    asyncio.run(ob.async_run())
    assert "control_mode" not in entry.options
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "error",
                                            "detail": "inverter control entities disabled"}
    ob.test_rt.executor_calls.assert_called_with(force=True)      # zgłoszenie w Naprawach od razu


def test_missing_control_entities_keep_the_not_found_phrase():
    from custom_components.volcast.control.executor import ControlGaps
    ob, client, entry = _entities_choice(mapped=WRITE_KEYS[1:], gaps=ControlGaps(missing=("mode",)))
    asyncio.run(ob.async_run())
    assert last(client)["control_mode"]["detail"] == "inverter control entities not found"
    ob.test_rt.executor_calls.assert_called_with(force=True)


def test_rejected_control_writes_give_the_rejects_phrase_and_no_option():
    ob, client, entry = _entities_choice(rejected=True)
    asyncio.run(ob.async_run())
    assert "control_mode" not in entry.options
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "error",
                                            "detail": "inverter rejects control writes"}


def test_rejected_writes_with_entity_control_already_chosen_show_the_phrase():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, rejected=True,
                         options={"control_mode": "entities", "profile_id": "goodwe-et",
                                  "inverter_domain": "goodwe"})
    asyncio.run(ob.async_run())
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "error",
                                            "detail": "inverter rejects control writes"}


def test_capabilities_list_the_remote_switch_and_directional_set_points():
    keys = (*WRITE_KEYS, "enable", "charge_power_w", "discharge_power_w")
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, mapped=keys)
    asyncio.run(ob.async_run())
    assert last(client)["capabilities"]["detail"] == ", ".join(sorted(keys))


def test_capabilities_read_only_when_mode_entity_missing():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, mapped=WRITE_KEYS[1:])
    asyncio.run(ob.async_run())
    assert last(client)["capabilities"]["detail"] == "read only"


def test_capabilities_list_mapped_settings_when_one_setting_missing():
    # Nastawa bez encji jest nieobsługiwana — reszta sterowania zostaje dostępna.
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, mapped=WRITE_KEYS[:-1])
    asyncio.run(ob.async_run())
    assert "export_limit_enabled" not in last(client)["capabilities"]["detail"]
    assert "mode" in last(client)["capabilities"]["detail"]


# Zdalny wybór `direct` (wyszukiwanie i ustawienie celu): tests/test_onboarding_direct.py
# (`test_remote_direct_choice_starts_direct_search` i sąsiednie).


def test_price_choice_ha_uses_first_usable_candidate():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == PRICE
    assert last(client)["prices"] == {"key": "prices", "state": "done", "detail": PRICE}


BUY, SELL = "sensor.pstryk_current_buy_price", "sensor.pstryk_current_sell_price"


def _pair_report(*ids):
    return dict(REPORT, price_entities=[{"entity_id": i, "platform": "pstryk"} for i in ids])


def test_price_choice_ha_also_applies_paired_sell_entity():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             report=_pair_report(BUY, SELL), prices={BUY: _price_attrs(), SELL: _price_attrs()})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == BUY and entry.options["entity_price_sell"] == SELL


def test_sell_entity_first_in_registry_never_becomes_the_buy_price():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             report=_pair_report(SELL, BUY), prices={BUY: _price_attrs(), SELL: _price_attrs()})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == BUY and entry.options["entity_price_sell"] == SELL


def test_lone_sell_entity_is_not_offered_as_buy():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             report=_pair_report(SELL), prices={SELL: _price_attrs()})
    asyncio.run(ob.async_run())
    assert "entity_price_buy" not in entry.options and "entity_price_sell" not in entry.options
    assert last(client)["prices"]["detail"] == "none found"


def test_unusable_sell_entity_is_not_applied():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             report=_pair_report(BUY, SELL), prices={BUY: _price_attrs(), SELL: {"currency": "PLN"}})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == BUY and "entity_price_sell" not in entry.options


def test_sell_entity_that_lost_data_before_choice_is_not_applied():
    prices = {BUY: _price_attrs(), SELL: _price_attrs()}
    ob, client, entry = make([PollResult("pending"), PollResult("consumed", choices={"price_source": "ha"})],
                             plan=OK_PLAN, report=_pair_report(BUY, SELL), prices=prices)
    real = ob._client.async_poll

    async def poll(s):
        prices[SELL] = {"currency": "PLN"}
        return await real(s)
    ob._client.async_poll = poll
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == BUY and "entity_price_sell" not in entry.options


def test_choice_clears_actions_of_the_replaced_sides_only():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             report=_pair_report(BUY, SELL), prices={BUY: _price_attrs(), SELL: _price_attrs()},
                             options={"price_sell_action": {"action": "easyenergy.get_energy_return_prices"}})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_sell"] == SELL and "price_sell_action" not in entry.options


def _action_services(ob, respond):
    calls = []

    async def async_call(domain, service, data, blocking=False, return_response=False):
        calls.append((f"{domain}.{service}", dict(data)))
        return respond(data)

    ob._hass.services = SimpleNamespace(async_call=async_call)
    return calls


def _tibber_day(data):
    return tibber_response(date.fromisoformat(data["start"][:10]))


def test_configured_buy_action_counts_as_a_price_source():
    action = {"action": "tibber.get_prices", "currency": "SEK"}
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             prices={}, options={"price_buy_action": action})
    calls = _action_services(ob, _tibber_day)
    asyncio.run(ob.async_run())
    assert [c[0] for c in calls] == ["tibber.get_prices", "tibber.get_prices"]    # dziś + jutro, jak w runtime
    step = last(client)["prices"]
    assert step["state"] == "done" and step["detail"] == "tibber.get_prices"
    assert entry.options["price_buy_action"] == action and "entity_price_buy" not in entry.options


@pytest.mark.parametrize("respond", [
    lambda _d: {"prices": {"Home": []}},                    # poprawna akcja, ale bez cen (np. zły dom)
    lambda _d: (_ for _ in ()).throw(RuntimeError("down")),  # akcja zawodzi
])
def test_configured_buy_action_without_prices_is_not_done(respond):
    action = {"action": "tibber.get_prices", "currency": "SEK"}
    ob, client, entry = make([PollResult("pending")], plan=OK_PLAN, prices={}, options={"price_buy_action": action})
    _action_services(ob, respond)
    asyncio.run(ob.async_run())
    step = last(client)["prices"]
    # Błąd, nie wybór: bez oferty zastępczego źródła, które skasowałoby akcję właściciela.
    assert step == {"key": "prices", "state": "error", "detail": "none found"}
    assert entry.options["price_buy_action"] == action                  # konfiguracja zostaje, nic nie nadpisane


def test_configured_buy_action_failing_then_ha_choice_keeps_the_action():
    """Chwilowy błąd akcji przy usable encji HA: krok nie oferuje encji, a wybór „ha" niczego nie nadpisuje."""
    action = {"action": "tibber.get_prices", "currency": "SEK"}
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             options={"price_buy_action": action})
    _action_services(ob, lambda _d: (_ for _ in ()).throw(RuntimeError("not loaded yet")))
    asyncio.run(ob.async_run())
    assert all(s.get("state") != "choice" for p in client.progress for s in p if s["key"] == "prices")
    assert last(client)["prices"]["state"] == "error"
    assert entry.options["price_buy_action"] == action and "entity_price_buy" not in entry.options


def test_invalid_configured_buy_action_still_offers_the_ha_entity():
    """Nieparsowalna akcja to brak źródła — jak dotąd encja HA jest proponowana i może ją zastąpić."""
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             options={"price_buy_action": {"action": "nope.unknown"}})
    _action_services(ob, lambda _d: pytest.fail("invalid action must not be called"))
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == PRICE and "price_buy_action" not in entry.options


@pytest.mark.parametrize("today_ok, state", [(False, "error"), (True, "done")])
def test_hanging_price_action_is_bounded_in_onboarding(monkeypatch, today_ok, state):
    """Zawieszona akcja nie wstrzymuje onboardingu: łączny limit sondy (dziś + jutro), nie 2 × 30 s z runtime.
    Gdy wisi tylko jutro, dzisiejsza seria z sondy zostaje."""
    from custom_components.volcast.core import price_actions
    assert price_actions.CALL_TIMEOUT_S == 30.0                          # runtime bez zmian
    assert ob_mod.PRICE_PROBE_TOTAL_S < 10.0                             # krótki limit całej sondy
    monkeypatch.setattr(ob_mod, "PRICE_PROBE_TOTAL_S", 0.2)
    action = {"action": "tibber.get_prices", "currency": "SEK"}
    ob, client, entry = make([PollResult("pending")], plan=OK_PLAN, prices={}, options={"price_buy_action": action})
    calls = []

    async def hang(domain, service, data, blocking=False, return_response=False):
        calls.append(data)
        if today_ok and len(calls) == 1:
            await asyncio.sleep(0.02)                                    # dziś: szybko, z dużym zapasem do limitu
            return _tibber_day(data)
        await asyncio.Event().wait()

    ob._hass.services = SimpleNamespace(async_call=hang)

    async def run():
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.wait_for(ob._prices_step(REPORT), 5.0)
        return loop.time() - start

    elapsed = asyncio.run(run())
    assert elapsed < 0.2 + 0.3                                           # limit łączny + duży zapas na obciążone CI
    assert last(client)["prices"]["state"] == state
    assert entry.options["price_buy_action"] == action


@pytest.mark.parametrize("today_delay, tomorrow_hangs, state", [
    (0.4, False, "done"),      # dziś wolniej niż połowa budżetu, ale w całości → ceny użyteczne
    (None, True, "error"),     # dziś wisi → wynik w całkowitym limicie
    (0.0, True, "done"),       # dziś szybko, jutro wisi → dziś zostaje, całość w limicie
    (0.5, True, "done"),       # dziś zjada prawie budżet, jutro dostaje resztę i wygasa
])
def test_price_action_probe_budget_is_shared_between_today_and_tomorrow(
        monkeypatch, today_delay, tomorrow_hangs, state):
    """Budżet sondy jest wspólny: dziś może zużyć cały `PRICE_PROBE_TOTAL_S`, jutro dostaje tylko resztę."""
    monkeypatch.setattr(ob_mod, "PRICE_PROBE_TOTAL_S", 0.6)
    action = {"action": "tibber.get_prices", "currency": "SEK"}
    ob, client, entry = make([PollResult("pending")], plan=OK_PLAN, prices={}, options={"price_buy_action": action})
    calls = []

    async def slow(domain, service, data, blocking=False, return_response=False):
        calls.append(data)
        if len(calls) == 1 and today_delay is not None:
            await asyncio.sleep(today_delay)
            return _tibber_day(data)
        if len(calls) == 2 and not tomorrow_hangs:
            return _tibber_day(data)
        await asyncio.Event().wait()

    ob._hass.services = SimpleNamespace(async_call=slow)

    async def run():
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.wait_for(ob._prices_step(REPORT), 5.0)
        return loop.time() - start

    assert asyncio.run(run()) < 0.6 + 0.3                                # limit łączny + zapas na obciążone CI
    assert last(client)["prices"]["state"] == state


def test_action_only_integrations_point_to_the_options_menu():
    ob, client, entry = make([PollResult("pending")], plan=OK_PLAN, prices={}, report=dict(REPORT, price_entities=[]))
    ob._hass.config_entries.async_entries = lambda domain: (
        [SimpleNamespace(entry_id="t1", state=SimpleNamespace(value="loaded"))] if domain == "tibber" else [])
    asyncio.run(ob.async_run())
    step = last(client)["prices"]
    assert step["state"] == "choice" and "price_source" in step["detail"] and "tibber" in step["detail"]


def test_platform_currency_is_set_when_series_does_not_state_it():
    attrs = {k: v for k, v in _price_attrs().items() if k != "currency"}          # seria bez waluty (Octopus)
    eid = "event.octopus_energy_electricity_a_1_current_day_rates"
    report = dict(REPORT, price_entities=[{"entity_id": eid, "platform": "octopus_energy"}])
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN, report=report,
                             prices={eid: attrs})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == eid
    assert entry.options["price_currency"] == "GBP"


def test_unusable_price_entity_is_not_offered():
    # Encja ceny jest, ale jej atrybuty nie dają dziś pełnej serii — nie proponujemy jej.
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             prices={PRICE: {"unit_of_measurement": "PLN/kWh"}})
    asyncio.run(ob.async_run())
    assert "entity_price_buy" not in entry.options
    assert last(client)["prices"]["detail"] == "none found" and last(client)["prices"]["state"] != "done"


def test_second_usable_candidate_is_offered():
    report = dict(REPORT, price_entities=[{"entity_id": "sensor.bad", "platform": "x"},
                                          {"entity_id": PRICE, "platform": "nordpool"}])
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             report=report, prices={"sensor.bad": {"foo": 1}, PRICE: _price_attrs()})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == PRICE


def test_price_entity_no_longer_usable_at_choice_time_is_not_set():
    prices = {PRICE: _price_attrs()}
    ob, client, entry = make([PollResult("pending"), PollResult("consumed", choices={"price_source": "ha"})],
                             plan=OK_PLAN, prices=prices)
    real = ob._client.async_poll

    async def poll(s):
        prices[PRICE] = {"currency": "PLN"}                 # encja straciła dane przed wyborem
        return await real(s)
    ob._client.async_poll = poll
    asyncio.run(ob.async_run())
    assert "entity_price_buy" not in entry.options
    assert last(client)["prices"]["state"] == "error"


# ── pierwszy plan: jawny wynik ───────────────────────────────────────────────


@pytest.mark.parametrize("res,outcome", [
    ({"success": True, "skipped": None, "slots_count": 24}, "ok"),
    ({"success": True, "skipped": None, "slots_count": 0}, "failed:no_slots"),
    ({"success": True, "skipped": None, "slots_count": None}, "failed:no_slots"),
    ({"success": True, "skipped": None, "slots_count": True}, "failed:no_slots"),
    ({"success": True, "skipped": "no-real-prices", "slots_count": None}, "skipped"),
    ({"success": True, "skipped": "cooldown", "slots_count": None}, "cooldown"),
    ({"skipped": "cooldown"}, "cooldown"),
    ({"success": False, "skipped": None, "slots_count": None, "error": "tier_not_eligible"},
     "failed:tier_not_eligible"),
    ({"success": False, "error": "planner_failed"}, "failed:planner_failed"),
    ({"success": False, "error": "x" * 99}, "failed:planner_failed"),
    ({"slots_count": 3}, "failed:planner_failed"),
    (None, "failed:unreachable"),
])
def test_plan_outcome(res, outcome):
    assert plan_outcome(res) == outcome


def test_first_plan_cooldown_is_queued_and_retried():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=[{"skipped": "cooldown"}, OK_PLAN])
    asyncio.run(ob.async_run())
    states = [({s["key"]: s for s in p}.get("first_plan") or {}) for p in client.progress]
    assert {"key": "first_plan", "state": "active", "detail": "queued"} in states
    assert last(client)["first_plan"] == {"key": "first_plan", "state": "done", "detail": "24 slots"}
    assert client.plan_calls == 2


def test_first_plan_cooldown_forever_ends_queued_without_hanging():
    ob, client, _ = make([PollResult("consumed", choices={})], plan={"skipped": "cooldown"})
    asyncio.run(ob.async_run())
    assert last(client)["first_plan"] == {"key": "first_plan", "state": "done", "detail": "queued"}
    assert client.plan_calls <= 4


@pytest.mark.parametrize("res", [
    {"success": True, "skipped": None, "slots_count": 0},
    {"success": True, "skipped": None, "slots_count": None},
    {"success": False, "skipped": None, "slots_count": None, "error": "tier_not_eligible"},
    {"success": False, "skipped": None, "slots_count": None, "error": "planner_failed"},
])
def test_first_plan_refusal_is_error_never_zero_or_none_slots(res):
    ob, client, _ = make([PollResult("consumed", choices={})], plan=res)
    asyncio.run(ob.async_run())
    fp = last(client)["first_plan"]
    assert fp == {"key": "first_plan", "state": "error", "detail": "planner unavailable"}
    assert all("slots" not in (({s["key"]: s for s in p}.get("first_plan") or {}).get("detail") or "")
               for p in client.progress)


def test_first_plan_skipped_by_planner_is_done_queued():
    ob, client, _ = make([PollResult("consumed", choices={})],
                         plan={"success": True, "skipped": "market-not-ready", "slots_count": None})
    asyncio.run(ob.async_run())
    assert last(client)["first_plan"] == {"key": "first_plan", "state": "done", "detail": "queued"}
    assert ob.first_plan_outcome == "skipped"


def test_first_plan_unreachable_then_ok():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=[None, OK_PLAN])
    asyncio.run(ob.async_run())
    assert last(client)["first_plan"]["detail"] == "24 slots" and client.plan_calls == 2


def test_first_plan_ok_refreshes_schedule():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    ob._runtime().fetcher.async_refresh.assert_called_once()


# ── pozostałe kroki, okno, odporność ─────────────────────────────────────────


def test_inverter_not_found_is_error_step_but_continues():
    ob, client, _ = make([PollResult("consumed", choices={})], plan={"success": True, "slots_count": 3},
                         report={"inverters": [], "price_entities": [], "energy_sensors": []})
    asyncio.run(ob.async_run())
    st = last(client)
    assert st["inverter"]["state"] == "error" and st["first_plan"]["state"] == "done"
    assert st["prices"]["detail"] == "none found"


def _acct(**kw):
    base = dict(price_kind="pstryk", price_ready=True, price_reason=None, annual_consumption_kwh=None,
                control_enabled=None, readings_fresh=None, consumption_days_with_data=None)
    return AccountState(**{**base, **kw})


def _run_with(account, **kw):
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, **kw)
    client.account = account
    asyncio.run(ob.async_run())
    return ob, client


def test_prices_step_done_from_account_provider():
    ob, client = _run_with(_acct(), prices={})
    assert last(client)["prices"] == {"key": "prices", "state": "done", "detail": "from your account (pstryk)"}
    assert ob.account_state.price_kind == "pstryk" and ob.account_state_at is not None
    assert client.account_calls == 1


@pytest.mark.parametrize("account", [
    None, RuntimeError("boom"), _acct(price_ready=False, price_reason="market_not_ready"),
    _acct(price_kind="unconfigured", price_ready=False, price_reason="prices_not_configured")])
def test_prices_step_falls_back_to_entity_scan(account):
    ob, client = _run_with(account)
    assert last(client)["prices"]["state"] == "choice"          # kandydat z raportu HA
    ob2, client2 = _run_with(account, prices={})
    assert last(client2)["prices"] == {"key": "prices", "state": "choice", "detail": "none found"}


def test_consumption_step_done_from_annual_consumption():
    ob, client = _run_with(_acct(annual_consumption_kwh=7500), options={})
    assert last(client)["consumption"] == {"key": "consumption", "state": "done",
                                           "detail": "from your annual consumption"}


def test_consumption_annual_survives_failed_import():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    client.account = _acct(annual_consumption_kwh=7500)
    ob._import_history = lambda: _ret(None)
    asyncio.run(ob.async_run())
    assert last(client)["consumption"]["state"] == "done"


def test_consumption_without_annual_keeps_existing_path():
    ob, client = _run_with(_acct(annual_consumption_kwh=None), options={})
    assert last(client)["consumption"]["state"] == "choice"


def test_consumption_without_load_sensor_is_choice():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, options={})
    asyncio.run(ob.async_run())
    assert last(client)["consumption"] == {"key": "consumption", "state": "choice",
                                           "detail": "no house energy sensor selected"}


def test_consumption_import_failure_is_error():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    ob._import_history = lambda: _ret(None)
    asyncio.run(ob.async_run())
    assert last(client)["consumption"]["state"] == "error"


def test_stops_after_live_window():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, clock_step=600)
    asyncio.run(ob.async_run())                             # zakończy się, nie zawiśnie
    assert len(client.progress) < 30


def test_stops_when_session_gone():
    ob, client, _ = make([PollResult("expired")], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    assert ob._utcnow() < NOW + timedelta(minutes=5)


def test_never_raises_and_logs_class_only(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.volcast")
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)

    async def boom(*_a):
        raise RuntimeError("secret 10.0.0.1")
    ob._client.async_progress = boom
    asyncio.run(ob.async_run())
    assert "10.0.0.1" not in caplog.text and "RuntimeError" in caplog.text


def test_runtime_missing_keeps_remote_choice_pending_then_applies():
    # Wpis przeładowuje się (runtime chwilowo brak) — wybór czeka, nie przepada.
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "entities"})], plan=OK_PLAN)
    real = ob._runtime()
    calls = {"n": 0}

    def runtime():
        calls["n"] += 1
        return None if 2 <= calls["n"] <= 4 else real
    ob._runtime = runtime
    asyncio.run(ob.async_run())
    states = [({s["key"]: s for s in p}.get("control_mode") or {}).get("state") for p in client.progress]
    assert "error" not in states
    assert entry.options["control_mode"] == "entities" and last(client)["control_mode"]["state"] == "done"


def test_runtime_missing_for_whole_window_never_errors():
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "entities"})], plan=OK_PLAN)
    ob._runtime = lambda: None
    asyncio.run(ob.async_run())
    st = last(client)
    assert st["capabilities"]["detail"] == "read only" and st["control_mode"]["state"] == "choice"
    assert "control_mode" not in entry.options


def test_both_remote_choices_applied_in_one_entry_update():
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "entities", "price_source": "ha"})],
                             plan=OK_PLAN)
    asyncio.run(ob.async_run())
    assert ob._hass.config_entries.async_update_entry.call_count == 1
    assert entry.options["control_mode"] == "entities" and entry.options["entity_price_buy"] == PRICE


def test_applied_choice_is_recorded_with_session_and_time():
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "entities"})], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    applied = entry.data["pairing"]["applied_choices"]
    assert applied["session_id"] == "s1"
    assert applied["choices"]["control_mode"]["value"] == "entities"
    assert applied["choices"]["control_mode"]["at"] == NOW.isoformat()
    assert entry.data["api_key"] == "vk_x" and entry.data["pairing"]["session_id"] == "s1"


def test_remote_choice_not_reapplied_after_restart_local_change_wins():
    polls = [PollResult("consumed", choices={"control_mode": "entities", "price_source": "ha"})]
    ob, client, entry = make(polls, plan=OK_PLAN)
    asyncio.run(ob.async_run())
    # właściciel wyłącza sterowanie i ceny lokalnie, potem restart HA w oknie 30 min
    entry.options = {k: v for k, v in entry.options.items() if k not in ("control_mode", "entity_price_buy")}
    ob2, client2, _ = make(polls, plan=OK_PLAN, entry=entry)
    asyncio.run(ob2.async_run())
    assert "control_mode" not in entry.options and "entity_price_buy" not in entry.options
    st = last(client2)
    assert st["control_mode"]["state"] == "done" and st["prices"]["state"] == "done"
    assert ob2._hass.config_entries.async_update_entry.call_count == 0


def test_applied_record_of_another_session_is_ignored():
    entry = SimpleNamespace(entry_id="e1", options={"load_energy_entity": "sensor.house_consumption"},
                            data={"pairing": {"session_id": "s1", "applied_choices": {
                                "session_id": "old", "choices": {"control_mode": {"value": "entities", "at": "x"}}}}})
    ob, client, _ = make([PollResult("consumed", choices={"control_mode": "entities"})], plan=OK_PLAN, entry=entry)
    asyncio.run(ob.async_run())
    assert entry.options["control_mode"] == "entities"


# ── drobne: ponowienie postu, deduplikacja, dni historii, inne źródło cen ──


def test_lost_final_post_is_resent():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    real = client.async_progress
    state = {"fail": True}

    async def progress(s, steps):
        by_key = {x["key"]: x for x in steps}
        if state["fail"] and by_key.get("first_plan", {}).get("detail") == "24 slots":
            state["fail"] = False                              # ta jedna próba przepada
            return False
        return await real(s, steps)
    client.async_progress = progress
    asyncio.run(ob.async_run())
    assert last(client)["first_plan"] == {"key": "first_plan", "state": "done", "detail": "24 slots"}


def test_rejected_posts_do_not_stop_choices_or_first_plan():
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "entities"})], plan=OK_PLAN)

    async def rejected(s, steps):
        return False
    client.async_progress = rejected
    asyncio.run(ob.async_run())
    assert entry.options["control_mode"] == "entities" and client.plan_calls == 1


def test_posts_only_on_state_changes():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    snapshots = [tuple(sorted((x["key"], x["state"], x.get("detail")) for x in p)) for p in client.progress]
    assert len(snapshots) == len(set(snapshots))


def test_history_days_come_from_the_imported_sensor():
    report = dict(REPORT, energy_sensors=[{"entity_id": "sensor.other", "days_of_statistics": 400},
                                          {"entity_id": "sensor.house_consumption", "days_of_statistics": 58}])
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, report=report)
    asyncio.run(ob.async_run())
    assert last(client)["consumption"]["detail"] == "58 days of history"
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, report=report,
                         options={"load_energy_entity": "sensor.not_in_report"})
    asyncio.run(ob.async_run())
    assert last(client)["consumption"]["detail"] == "imported"


def test_other_price_source_ends_the_price_step():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "pstryk"})], plan=OK_PLAN,
                             prices={}, options={"load_energy_entity": "sensor.house_consumption",
                                                 "control_mode": "entities"})
    asyncio.run(ob.async_run())
    assert last(client)["prices"] == {"key": "prices", "state": "done"}
    assert "entity_price_buy" not in entry.options
    assert ob._utcnow() < NOW + timedelta(minutes=5)          # pętla nie odpytuje do końca okna


def test_installation_without_rated_power_is_done_without_detail():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, rated=None)
    asyncio.run(ob.async_run())
    assert last(client)["installation"] == {"key": "installation", "state": "done"}


def test_first_plan_refresh_uses_current_runtime():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    old = ob._runtime()
    new = SimpleNamespace(**{**vars(old), "fetcher": SimpleNamespace(
        async_refresh=MagicMock(side_effect=lambda: _ret("accepted")))})
    calls = {"n": 0}

    def runtime():
        calls["n"] += 1
        return old if calls["n"] == 1 else new
    ob._runtime = runtime
    asyncio.run(ob.async_run())
    new.fetcher.async_refresh.assert_called_once()
    old.fetcher.async_refresh.assert_not_called()


def test_control_runtime_fields():
    rt = ControlRuntime(executor=1, fetcher=2, telemetry=3, cloud=4, choice=None, mapped={}, rated_power_w=None)
    assert rt.unsubs == [] and ControlRuntime(1, 2, 3, 4, None, {}, None).unsubs is not rt.unsubs


def test_module_has_no_logger_exception_calls():
    import inspect
    assert "_LOGGER.exception" not in inspect.getsource(ob_mod)


# ── czujnik zużycia domu z wykrywania, wycofanie ponowień postępu ──────────


def _load_row(eid, **kw):
    return {"entity_id": eid, "unit": "kWh", "state_class": "total_increasing", **kw}


def test_single_clear_house_load_sensor_is_selected_and_imported():
    report = dict(REPORT, energy_sensors=[
        _load_row("sensor.goodwe_total_load", days_of_statistics=58),
        _load_row("sensor.pv_energy_total", days_of_statistics=90)])
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN, report=report, options={},
                             inverter_entities={"sensor.goodwe_total_load", "sensor.pv_energy_total"})
    asyncio.run(ob.async_run())
    assert entry.options["load_energy_entity"] == "sensor.goodwe_total_load"
    assert last(client)["consumption"] == {"key": "consumption", "state": "done", "detail": "58 days of history"}
    # zapamiętany jako zastosowany w tej sesji (restart go nie powtórzy)
    assert "load_energy" in entry.data["pairing"]["applied_choices"]["choices"]


def test_ambiguous_house_load_sensors_leave_consumption_as_choice():
    report = dict(REPORT, energy_sensors=[_load_row("sensor.house_consumption"), _load_row("sensor.home_load")])
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN, report=report, options={},
                             inverter_entities={"sensor.house_consumption", "sensor.home_load"})
    asyncio.run(ob.async_run())
    assert "load_energy_entity" not in entry.options
    assert last(client)["consumption"]["state"] == "choice"


@pytest.mark.parametrize("eid", ["sensor.heat_pump_consumption", "sensor.washing_machine_consumption",
                                 "sensor.shelly_plug_home_office_energy", "sensor.house_consumption"])
def test_house_load_outside_the_inverter_is_never_auto_picked(eid):
    report = dict(REPORT, energy_sensors=[_load_row(eid, platform="shelly")])
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN, report=report, options={},
                             inverter_entities={"select.goodwe_ems_mode"})
    asyncio.run(ob.async_run())
    assert "load_energy_entity" not in entry.options
    assert last(client)["consumption"]["state"] == "choice"


def test_single_appliance_meter_of_the_inverter_is_not_auto_picked():
    report = dict(REPORT, energy_sensors=[_load_row("sensor.goodwe_heat_pump_consumption")])
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN, report=report, options={},
                             inverter_entities={"sensor.goodwe_heat_pump_consumption"})
    asyncio.run(ob.async_run())
    assert "load_energy_entity" not in entry.options
    assert last(client)["consumption"]["state"] == "choice"


def test_auto_picked_load_sensor_cleared_by_owner_is_not_reapplied_after_restart():
    report = dict(REPORT, energy_sensors=[_load_row("sensor.goodwe_total_load", days_of_statistics=58)])
    own = {"sensor.goodwe_total_load"}
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN, report=report, options={},
                             inverter_entities=own)
    asyncio.run(ob.async_run())
    assert entry.options["load_energy_entity"] == "sensor.goodwe_total_load"
    entry.options = {k: v for k, v in entry.options.items() if k != "load_energy_entity"}   # właściciel czyści
    imports = []
    ob2, client2, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, report=report, entry=entry,
                           inverter_entities=own, imported={"accepted": 0})
    ob2._import_history = lambda: imports.append(1) or _ret({"accepted": 0})
    asyncio.run(ob2.async_run())                                        # nowy przebieg po restarcie HA
    assert "load_energy_entity" not in entry.options and imports == []
    assert last(client2)["consumption"]["state"] == "choice"


def test_no_runtime_means_no_auto_pick():
    report = dict(REPORT, energy_sensors=[_load_row("sensor.goodwe_total_load")])
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN, report=report, options={},
                             inverter_entities={"sensor.goodwe_total_load"})
    ob._runtime = lambda: None                                          # wpis właśnie się przeładowuje
    asyncio.run(ob.async_run())
    assert "load_energy_entity" not in entry.options
    assert last(client)["consumption"]["state"] == "choice"


def test_rejected_progress_posts_back_off():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN, clock_step=5, prices={})
    calls = {"n": 0}

    async def rejected(s, steps):
        calls["n"] += 1
        return False
    client.async_progress = rejected
    asyncio.run(ob.async_run())
    # 30 min okna co 5 s = 360 obiegów; z wycofaniem 5→60 s ponowień jest kilkadziesiąt
    assert calls["n"] < 60


def test_draft_integration_entry_not_offered_in_onboarding():
    # Wpis `ha` w stanie draft to tylko podpowiedź mapowania odczytów: bez wyboru „entities”.
    draft = ProfileChoice(load_builtin("huawei-sun2000"), "huawei_solar", "SUN2000-10KTL-M1")
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN, choice=draft)
    asyncio.run(ob.async_run())
    st = last(client)
    assert st["capabilities"]["detail"] == "read only"
    assert st["control_mode"]["state"] == "choice" and "entities" not in (st["control_mode"].get("detail") or "")
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "entities"})], plan=OK_PLAN,
                             choice=draft)
    asyncio.run(ob.async_run())
    assert "control_mode" not in entry.options and last(client)["control_mode"]["state"] == "error"


def _no_readings_rt(ob, *, sources=None, probe=()):
    rt = ob._runtime()
    src = {"soc": "none", "pv_power_w": "none", "grid_power_w": "none"} if sources is None else sources
    rt.telemetry = SimpleNamespace(readings_report=lambda: {"readings_source": src, "readings_dropped": []})
    rt.last_probe = list(probe)
    return rt


def test_onboarding_skips_plan_when_no_readings():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    _no_readings_rt(ob)
    asyncio.run(ob.async_run())
    assert client.plan_calls == 0
    assert ob.first_plan_outcome == "no_readings"
    assert ob._plan_attempts == 0
    assert last(client)["first_plan"]["state"] == "active"


def test_onboarding_requests_plan_when_readings_exist():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    _no_readings_rt(ob, sources={"soc": "entity", "pv_power_w": "none", "grid_power_w": "none"})
    asyncio.run(ob.async_run())
    assert client.plan_calls == 1 and ob.first_plan_outcome == "ok"


def test_inverter_step_read_only_when_direct_found_but_core_holds_the_port(monkeypatch):
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    _no_readings_rt(ob, probe=[object()])
    monkeypatch.setattr(ob_mod.ds, "found", lambda reports: list(reports))
    asyncio.run(ob.async_run())
    assert last(client)["inverter"] == {"key": "inverter", "state": "done", "detail": "read only, no readings"}


def test_inverter_step_keeps_label_without_a_direct_hit():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    _no_readings_rt(ob)
    asyncio.run(ob.async_run())
    assert last(client)["inverter"]["detail"] == "GoodWe GW8KN-ET"


NO_READINGS_PLAN = {"success": True, "skipped": "no_readings", "slots_count": None, "error": "no_readings"}


def _readings_arrive(rt):
    rt.telemetry = SimpleNamespace(
        readings_report=lambda: {"readings_source": {"soc": "entity"}, "readings_dropped": []})


def _reopen(ob):
    # Pętla główna kończy się dopiero z oknem na żywo; test wraca do chwili „po onboardingu, okno otwarte".
    ob._live_until = ob._utcnow() + timedelta(minutes=30)


def test_plan_outcome_no_readings():
    assert plan_outcome(NO_READINGS_PLAN) == "no_readings"
    assert plan_outcome({"success": False, "error": "no_readings"}) == "no_readings"
    assert plan_outcome({"success": True, "skipped": "no_readings"}) == "no_readings"
    assert plan_outcome({"skipped": "cooldown"}) == "cooldown"


def test_server_no_readings_is_active_no_readings_not_error():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=NO_READINGS_PLAN)
    asyncio.run(ob.async_run())
    assert ob.first_plan_outcome == "no_readings"
    assert last(client)["first_plan"] == {"key": "first_plan", "state": "active", "detail": "no readings"}


def test_plan_retried_after_first_telemetry_success():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    rt = _no_readings_rt(ob)
    asyncio.run(ob.async_run())
    assert client.plan_calls == 0 and ob.first_plan_outcome == "no_readings"
    _readings_arrive(rt)
    _reopen(ob)
    asyncio.run(ob.async_on_readings())
    assert client.plan_calls == 1 and ob.first_plan_outcome == "ok"
    assert last(client)["first_plan"] == {"key": "first_plan", "state": "done", "detail": "24 slots"}
    _reopen(ob)
    asyncio.run(ob.async_on_readings())
    assert client.plan_calls == 1                           # wynik ok: nic więcej


def test_retry_on_readings_respects_server_cooldown_and_attempts():
    ob, client, _ = make([PollResult("consumed", choices={})],
                         plan=[NO_READINGS_PLAN, {"skipped": "cooldown"}, OK_PLAN])
    asyncio.run(ob.async_run())                             # serwer: no_readings (1. próba)
    assert client.plan_calls == 1
    sleeps = []
    real_sleep = ob._sleep

    async def spy(s):
        sleeps.append(s)
        await real_sleep(s)
    ob._sleep = spy
    _reopen(ob)
    asyncio.run(ob.async_on_readings())
    assert client.plan_calls == 3 and ob.first_plan_outcome == "ok"
    assert sleeps and all(s <= 125.0 for s in sleeps)       # czekamy, nie młotkujemy


def test_retry_on_readings_stops_at_attempt_cap():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=NO_READINGS_PLAN)
    asyncio.run(ob.async_run())
    for _ in range(6):
        _reopen(ob)
    asyncio.run(ob.async_on_readings())
    assert client.plan_calls <= 3


def test_retry_on_readings_after_unreachable_planner():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    _readings_arrive(_no_readings_rt(ob))
    asyncio.run(ob.async_run())
    ob.first_plan_outcome, ob._plan_attempts = "failed:unreachable", 1
    before = client.plan_calls
    _reopen(ob)
    asyncio.run(ob.async_on_readings())
    assert client.plan_calls == before + 1 and ob.first_plan_outcome == "ok"


def test_no_retry_when_live_window_closed():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    _no_readings_rt(ob)
    asyncio.run(ob.async_run())
    ob._live_until = NOW
    asyncio.run(ob.async_on_readings())
    assert client.plan_calls == 0


def test_no_retry_when_entry_unloaded():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    _no_readings_rt(ob)
    asyncio.run(ob.async_run())
    ob._hass.config_entries.async_get_entry = lambda eid: None
    _reopen(ob)
    asyncio.run(ob.async_on_readings())
    assert client.plan_calls == 0


def test_no_retry_when_first_plan_already_ok():
    ob, client, _ = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    assert client.plan_calls == 1
    _reopen(ob)
    asyncio.run(ob.async_on_readings())
    assert client.plan_calls == 1


# ── 409 `not_live`: koniec raportowania, czyszczenie zapamiętanej sesji ──


def test_not_live_on_progress_stops_after_one_call_and_clears_pairing():
    from custom_components.volcast.cloud.client import SessionNotLive
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    calls = []

    async def progress(s, steps):
        calls.append(steps)
        raise SessionNotLive
    client.async_progress = progress
    asyncio.run(ob.async_run())
    assert len(calls) == 1 and client.plan_calls == 0
    assert ob.stopped and ob.reset_result == "cleared"
    assert "pairing" not in entry.data and entry.data["api_key"] == "vk_x"


def test_not_live_from_poll_stops_the_loop():
    ob, client, entry = make([PollResult("not_live")], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    assert ob.stopped and ob.reset_result == "cleared" and "pairing" not in entry.data


def test_not_live_after_window_does_not_clear_pairing():
    from custom_components.volcast.cloud.client import SessionNotLive
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    ob._live_until = NOW - timedelta(seconds=1)

    async def progress(s, steps):
        raise SessionNotLive
    client.async_progress = progress
    asyncio.run(ob.async_run())
    assert ob.stopped and ob.reset_result == "window_ended" and "pairing" in entry.data


def test_not_live_from_plan_request_stops():
    from custom_components.volcast.cloud.client import SessionNotLive
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN)

    async def plan(s):
        client.plan_calls += 1
        raise SessionNotLive
    client.async_request_plan = plan
    asyncio.run(ob.async_run())
    assert client.plan_calls == 1 and ob.stopped and "pairing" not in entry.data


def test_not_live_from_account_state_stops_without_another_progress_call():
    from custom_components.volcast.cloud.client import SessionNotLive
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    client.account = SessionNotLive()
    asyncio.run(ob.async_run())
    assert ob.stopped and client.account_calls == 1 and client.plan_calls == 0
    assert "pairing" not in entry.data
    progress_at_stop = len(client.progress)
    after = len(client.progress)
    asyncio.run(ob.async_on_readings())
    assert len(client.progress) == after == progress_at_stop and client.plan_calls == 0


def test_not_live_in_readings_task_stops():
    from custom_components.volcast.cloud.client import SessionNotLive
    ob, client, entry = make([PollResult("consumed", choices={})], plan=OK_PLAN)
    calls = []

    async def plan(s):
        calls.append(1)
        raise SessionNotLive
    client.async_request_plan = plan
    ob.first_plan_outcome = "failed:planner_failed"
    asyncio.run(ob.async_on_readings())
    assert calls == [1] and ob.stopped and "pairing" not in entry.data
    asyncio.run(ob.async_on_readings())
    assert calls == [1]


def test_not_live_keeps_wizard_mark_and_does_not_reload():
    from custom_components.volcast.const import PAIRING_ONLY_UPDATE_KEY
    ob, client, entry = make([PollResult("not_live")], plan=OK_PLAN)
    entry.data["pairing"] = {"session_id": "s1", "poll_token": "p", "live_until": "x", "url": "u",
                             "applied_choices": {"session_id": "s1", "choices": {"control_mode": {"value": "entities"}}}}
    asyncio.run(ob.async_run())
    assert entry.data["pairing"] == {"applied_choices": {"session_id": "s1",
                                                          "choices": {"control_mode": {"value": "entities"}}}}
    assert ob._entry_id in ob._hass.data[PAIRING_ONLY_UPDATE_KEY]


def test_not_live_marker_discarded_when_update_fires_no_listener():
    from custom_components.volcast.const import PAIRING_ONLY_UPDATE_KEY
    ob, client, entry = make([PollResult("not_live")], plan=OK_PLAN)
    ob._hass.config_entries.async_update_entry = MagicMock(return_value=False)
    asyncio.run(ob.async_run())
    assert ob._entry_id not in ob._hass.data[PAIRING_ONLY_UPDATE_KEY]
    ob._hass.loop.call_soon.assert_not_called()


def test_not_live_marker_has_a_deferred_discard():
    from custom_components.volcast.const import PAIRING_ONLY_UPDATE_KEY
    ob, client, entry = make([PollResult("not_live")], plan=OK_PLAN)
    ob._hass.config_entries.async_update_entry = MagicMock(return_value=True)
    asyncio.run(ob.async_run())
    discard, eid = ob._hass.loop.call_soon.call_args.args
    assert eid in ob._hass.data[PAIRING_ONLY_UPDATE_KEY]
    discard(eid)
    assert eid not in ob._hass.data[PAIRING_ONLY_UPDATE_KEY]


OCT = "octopus_energy"
OCT_BUY = "event.octopus_energy_electricity_aaa_111_current_day_rates"
OCT_SELL = "event.octopus_energy_electricity_bbb_222_export_current_day_rates"
OCT_GAS = "event.octopus_energy_gas_ccc_333_current_day_rates"


def _no_currency():
    return {k: v for k, v in _price_attrs().items() if k != "currency"}


def test_octopus_gas_first_in_registry_is_never_the_electricity_price():
    report = dict(REPORT, price_entities=[{"entity_id": e, "platform": OCT} for e in (OCT_GAS, OCT_BUY, OCT_SELL)])
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN, report=report,
                             prices={e: _no_currency() for e in (OCT_GAS, OCT_BUY, OCT_SELL)})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == OCT_BUY and entry.options["entity_price_sell"] == OCT_SELL
    assert entry.options["price_currency"] == "GBP"


def test_ambiguous_pair_applies_buy_only_nothing_guessed():
    report = dict(REPORT, price_entities=[{"entity_id": e, "platform": "pstryk"} for e in
                                          ("sensor.p_buy_a", "sensor.p_buy_b", "sensor.p_sell_c")])
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN, report=report,
                             prices={e: _price_attrs() for e in ("sensor.p_buy_a", "sensor.p_buy_b", "sensor.p_sell_c")})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == "sensor.p_buy_a" and "entity_price_sell" not in entry.options


def _amber_attrs():
    """Prognoza w kształcie Ambera: bez bieżącego przedziału — od następnej granicy 30 min po NOW."""
    start = NOW + timedelta(minutes=30)
    return {"unit_of_measurement": "$/kWh", "forecasts": [
        {"start_time": (start + timedelta(minutes=30 * i, seconds=1)).isoformat(),
         "end_time": (start + timedelta(minutes=30 * (i + 1))).isoformat(), "per_kwh": 0.3} for i in range(48)]}


def test_amber_entities_with_dollar_unit_are_a_pair_in_aud():
    attrs = _amber_attrs()
    buy, sell = "sensor.home_general_forecast", "sensor.home_feed_in_forecast"
    report = dict(REPORT, price_entities=[{"entity_id": e, "platform": "amberelectric"} for e in (sell, buy)])
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN, report=report,
                             prices={buy: attrs, sell: attrs})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == buy and entry.options["entity_price_sell"] == sell
    assert "price_currency" not in entry.options          # waluta wynika z reguły integracji, nie z opcji


def test_amber_choice_detail_shows_aud():
    attrs = _amber_attrs()
    eid = "sensor.home_general_forecast"
    ob, client, entry = make([PollResult("pending")], plan=OK_PLAN, prices={eid: attrs},
                             report=dict(REPORT, price_entities=[{"entity_id": eid, "platform": "amberelectric"}]))
    asyncio.run(ob.async_run())
    assert last(client)["prices"]["detail"].endswith("· AUD")


def test_platform_currency_is_shown_in_the_choice_detail():
    ob, client, entry = make([PollResult("pending")], plan=OK_PLAN,
                             report=dict(REPORT, price_entities=[{"entity_id": OCT_BUY, "platform": OCT}]),
                             prices={OCT_BUY: _no_currency()})
    asyncio.run(ob.async_run())
    assert last(client)["prices"]["state"] == "choice" and "GBP" in last(client)["prices"]["detail"]


def test_platform_currency_conflicting_with_option_is_not_applied():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             report=dict(REPORT, price_entities=[{"entity_id": OCT_BUY, "platform": OCT}]),
                             prices={OCT_BUY: _no_currency()},
                             options={"price_currency": "EUR"})
    asyncio.run(ob.async_run())
    assert "entity_price_buy" not in entry.options
    assert last(client)["prices"]["state"] != "done"


def test_action_integrations_must_be_loaded():
    ob, client, entry = make([PollResult("pending")], plan=OK_PLAN, prices={}, report=dict(REPORT, price_entities=[]))
    ob._hass.config_entries.async_entries = lambda domain: (
        [SimpleNamespace(entry_id="t1", state=SimpleNamespace(value="not_loaded"))] if domain == "tibber"
        else [SimpleNamespace(entry_id="n1", state=SimpleNamespace(value="loaded"))] if domain == "nordpool" else [])
    asyncio.run(ob.async_run())
    detail = last(client)["prices"]["detail"]
    assert "nordpool" in detail and "tibber" not in detail


def test_unparsable_buy_action_is_cleared_by_the_entity_choice():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN,
                             options={"price_buy_action": {"action": "not.supported"}})
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == PRICE and "price_buy_action" not in entry.options
