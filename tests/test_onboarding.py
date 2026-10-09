import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.volcast import onboarding as ob_mod
from custom_components.volcast.cloud.client import PairingSession, PollResult
from custom_components.volcast.control.runtime import ControlRuntime
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.onboarding import STEP_KEYS, Onboarding, plan_outcome, progress_payload

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
         imported=None, choice=None, entry=None, rated=8000.0, inverter_entities=()):
    t = [NOW]

    def utcnow():
        return t[0]

    async def sleep(_s):
        t[0] = t[0] + timedelta(seconds=clock_step)

    entry = entry or SimpleNamespace(entry_id="e1", data={"api_key": "vk_x", "pairing": {"session_id": "s1"}},
                                     options=dict({"load_energy_entity": "sensor.house_consumption"}
                                                  if options is None else options))
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Warsaw"),
        states=States({PRICE: _price_attrs()} if prices is None else prices),
        config_entries=SimpleNamespace(
            async_get_entry=lambda eid: entry,
            async_update_entry=MagicMock(side_effect=lambda e, **kw: [setattr(e, k, v) for k, v in kw.items()])))
    fetch = SimpleNamespace(async_refresh=MagicMock(side_effect=lambda: _ret("accepted")))
    rt = SimpleNamespace(choice=choice or ProfileChoice(load_builtin("goodwe-et"), "goodwe", "GW8KN-ET"),
                         mapped={k: f"x.{k}" for k in mapped}, rated_power_w=rated, fetcher=fetch,
                         inverter_entities=frozenset(inverter_entities))
    client = Client(polls, plan)
    ob = Onboarding(hass, "e1", client=client, session=S, live_until=NOW + timedelta(minutes=30),
                    runtime=lambda: rt, report=lambda: report,
                    import_history=lambda: _ret({"accepted": 1392} if imported is None else imported),
                    utcnow=utcnow, sleep=sleep, discovery_wait_s=0.0, choice_poll_s=5.0)
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
    assert st["prices"]["detail"] == f"nordpool ({PRICE})"
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


def test_remote_direct_choice_is_error_step():
    ob, client, entry = make([PollResult("consumed", choices={"control_mode": "direct"})], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    assert "control_mode" not in entry.options and last(client)["control_mode"]["state"] == "error"


def test_price_choice_ha_uses_first_usable_candidate():
    ob, client, entry = make([PollResult("consumed", choices={"price_source": "ha"})], plan=OK_PLAN)
    asyncio.run(ob.async_run())
    assert entry.options["entity_price_buy"] == PRICE
    assert last(client)["prices"] == {"key": "prices", "state": "done", "detail": PRICE}


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
