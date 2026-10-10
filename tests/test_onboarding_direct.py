"""Onboarding z połączeniem bezpośrednim: sonda tylko bez integracji falownika, token wyboru, wybór zdalny."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.volcast.cloud import control_choice
from custom_components.volcast.cloud.client import PollResult
from custom_components.volcast.control import direct_search as ds_mod
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.discovery.identify import Candidate
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from custom_components.volcast.core.transports.factory import make_transport
from custom_components.volcast.onboarding import Onboarding
from tests.cloud.test_control_choice import Exec
from tests.control.test_direct_connection import SALT
from tests.test_onboarding import NOW, OK_PLAN, PRICE, REPORT, S, WRITE_KEYS, Client, States, _price_attrs, last
from tests.test_options_direct import FP, GW, GW_V, HOST, _thaw, report

NO_INVERTER = {**REPORT, "inverters": []}


def make(polls, *, report_=NO_INVERTER, reports=(), profiles=(GW_V,), mapped=(), options=None, search=None,
         entries=(), choice_profile=GW, client=None):
    t = [NOW]

    def utcnow():
        return t[0]

    async def sleep(_s):
        t[0] = t[0] + timedelta(seconds=60)

    entry = SimpleNamespace(entry_id="e1", domain="volcast", disabled_by=None,
                            data={"api_key": "vk_x", "pairing": {"session_id": "s1"}},
                            options=dict({"load_energy_entity": "sensor.house_consumption"} if options is None
                                         else options))
    all_entries = [entry, *entries]
    hass = SimpleNamespace(
        data={}, config=SimpleNamespace(time_zone="Europe/Warsaw"), states=States({PRICE: _price_attrs()}),
        config_entries=SimpleNamespace(
            async_get_entry=lambda eid: entry,
            async_entries=lambda domain=None: [e for e in all_entries if domain is None or e.domain == domain],
            async_update_entry=MagicMock(side_effect=lambda e, **kw: [setattr(e, k, v) for k, v in kw.items()])))
    rt = SimpleNamespace(choice=ProfileChoice(choice_profile, "goodwe" if mapped else None, None),
                         mapped={k: f"x.{k}" for k in mapped}, rated_power_w=None,
                         fetcher=SimpleNamespace(async_refresh=MagicMock(side_effect=lambda: _ret("accepted"))),
                         inverter_entities=frozenset(), last_probe=[], executor=Exec(), entry=entry,
                         wall_clock=utcnow, async_apply_controller_choice=lambda c: _ret("ignored"),
                         notify_control_state=lambda: None)
    calls = []

    async def default_search():
        calls.append(1)
        rt.last_probe = list(reports)
        return list(reports)
    client = client or Client(polls, OK_PLAN)
    ob = Onboarding(hass, "e1", client=client, session=S, live_until=NOW + timedelta(minutes=30),
                    runtime=lambda: rt, report=lambda: report_, import_history=lambda: _ret({"accepted": 1}),
                    utcnow=utcnow, sleep=sleep, discovery_wait_s=0.0, choice_poll_s=5.0,
                    search=search or default_search, profiles=list(profiles))
    return ob, client, entry, calls


async def _ret(v):
    return v


def test_no_probe_when_inverter_integration_present():
    ob, client, entry, calls = make([PollResult("consumed", choices={})], report_=REPORT, reports=[report()],
                                    mapped=WRITE_KEYS)
    asyncio.run(ob.async_run())
    assert calls == []
    assert last(client)["control_mode"]["detail"] == "options: entities"


@pytest.mark.asyncio
async def test_no_probe_frames_when_inverter_integration_present(goodwe_udp_sim):
    """Z integracją falownika w HA symulator nie dostaje ani jednej ramki (także z prawdziwą sondą)."""
    hosts = []

    async def real_search():
        hosts.append(1)
        return await ds_mod.async_search(SimpleNamespace(data={}), SimpleNamespace(entry_id="e1"), [GW_V],
                                         manual=Candidate("127.0.0.1", "manual"))
    ob, client, entry, _ = make([PollResult("consumed", choices={})], report_=REPORT, search=real_search)
    await ob.async_run()
    assert hosts == [] and goodwe_udp_sim.requests == 0


def test_onboarding_publishes_options_token():
    ob, client, entry, calls = make([PollResult("consumed", choices={})], reports=[report()], mapped=())
    asyncio.run(ob.async_run())
    st = last(client)
    assert calls == [1]
    assert st["inverter"] == {"key": "inverter", "state": "done", "detail": "GoodWe GW10K-ET (direct)"}
    assert st["installation"]["detail"] == "10 kW"
    assert st["capabilities"]["detail"] == "export_limit_w, mode, power_w, soc_min"     # bez nieczytelnych
    assert (st["control_mode"]["state"], st["control_mode"]["detail"]) == ("choice", "options: direct")
    ob, client, _, _ = make([PollResult("consumed", choices={})], reports=[report()], mapped=WRITE_KEYS)
    asyncio.run(ob.async_run())
    assert last(client)["control_mode"]["detail"] == "options: entities,direct"
    # profil roboczy (sam modbus.status = draft) z rozpoznanym falownikiem: „Bezpośrednio” oferowane — drabina
    # weryfikacji rusza od próby bez zapisu
    ob, client, _, _ = make([PollResult("consumed", choices={})], reports=[report()], profiles=(GW,),
                            mapped=WRITE_KEYS)
    asyncio.run(ob.async_run())
    assert last(client)["control_mode"]["detail"] == "options: entities,direct"
    # nieudana próba ścieżki rejestrów: „Bezpośrednio” nie jest oferowane
    ob, client, _, _ = make([PollResult("consumed", choices={})], reports=[report(available=False)],
                            mapped=WRITE_KEYS)
    asyncio.run(ob.async_run())
    assert last(client)["control_mode"]["detail"] == "options: entities"
    ob, client, _, _ = make([PollResult("consumed", choices={})], reports=[], profiles=(GW,))
    asyncio.run(ob.async_run())
    st = last(client)
    assert st["control_mode"]["state"] == "choice" and "detail" not in st["control_mode"]
    assert st["inverter"]["state"] == "error"


def test_direct_progress_never_contains_host():
    serial = 1234567890
    rep = report(transport="solarman_v5", logger_serial=serial)
    ob, client, entry, _ = make([PollResult("consumed", choices={"control_mode": "direct"})], reports=[rep])
    asyncio.run(ob.async_run())
    text = str(client.progress)
    assert HOST not in text and str(serial) not in text and FP not in text


def test_remote_direct_applied_when_available():
    ob, client, entry, _ = make([PollResult("consumed", choices={"control_mode": "direct"})], reports=[report()])
    asyncio.run(ob.async_run())
    assert entry.options["control_mode"] == "direct"
    assert entry.options["direct_target"]["device_fp"] == FP and entry.options["direct_target"]["host"] == HOST
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "done", "detail": "direct"}
    assert entry.data["pairing"]["applied_choices"]["choices"]["control_mode"]["value"] == "direct"


@pytest.mark.parametrize("case,text,state", [
    ("unverified", "direct control is not available for this inverter yet", "error"),
    ("conflict", "another integration is using this inverter", "choice"),      # odmowa bez zamknięcia kroku
    ("not_found", "inverter not found on the network", "error")])
def test_remote_direct_rejected_with_reason_text(case, text, state):
    kw = {}
    if case == "unverified":
        kw = {"reports": [report(available=False)]}
    elif case == "conflict":
        goodwe = SimpleNamespace(domain="goodwe", entry_id="g", data={"host": HOST}, options={}, disabled_by=None)
        kw = {"reports": [report()], "entries": [goodwe]}
    ob, client, entry, _ = make([PollResult("consumed", choices={"control_mode": "direct"})], **kw)
    asyncio.run(ob.async_run())
    assert "control_mode" not in entry.options and "direct_target" not in entry.options
    st = last(client)["control_mode"]
    assert (st["state"], st["detail"]) == (state, text)


def test_capabilities_list_hides_unreadable_keys():
    rep = report()
    rep = replace(rep, echo_only=("soc_max", "export_limit_w"))
    assert ds_mod.display_capabilities(rep.capabilities, rep.unreadable) == ["mode", "power_w", "soc_min"]


def test_target_keeps_rated_power_from_probe_identity():
    t = ds_mod.target_from_report(report(rated=8000.0))
    assert t["rated_power_w"] == 8000.0
    assert "rated_power_w" not in ds_mod.target_from_report(report(rated=None))


@pytest.mark.asyncio
async def test_search_times_out(monkeypatch):
    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(ds_mod, "discover", slow)
    monkeypatch.setattr(ds_mod, "async_installation_salt", lambda hass: _ret(SALT))
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_entries=lambda domain=None: []))
    out = await ds_mod.async_search(hass, SimpleNamespace(entry_id="e1"), [GW_V],
                                    manual=Candidate("192.168.1.2", "manual"), timeout_s=0.05)
    assert out == []


@pytest.mark.asyncio
async def test_search_over_simulator_builds_target(goodwe_udp_sim, monkeypatch):
    monkeypatch.setattr(ds_mod, "async_installation_salt", lambda hass: _ret(SALT))
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_entries=lambda domain=None: []))

    def factory(cfg):
        return make_transport(replace(cfg, port=goodwe_udp_sim.port, timeout_s=0.1, gap_s=0.0), allow_loopback=True)
    reports = await ds_mod.async_search(hass, SimpleNamespace(entry_id="e1"), [GW_V],
                                        manual=Candidate("127.0.0.1", "manual", transports=("goodwe_udp",)),
                                        transport_factory=factory, allow_loopback=True)
    hits = ds_mod.found(reports)
    assert hits and ds_mod.offer_reason(hits[0], [GW_V]) is None
    t = ds_mod.target_from_report(hits[0])
    assert t["device_fp"] and t["unreadable"] == ["soc_max"] and t["host"] == "127.0.0.1"


def test_control_mode_detail_carries_the_recommendation_when_computed():
    from custom_components.volcast.core.control.recommend import Recommendation

    async def search_with_recommendation():
        rt = ob._runtime()
        rt.last_probe = [report()]
        rt.recommendation = Recommendation("direct", "no_integration_identify_ok", 1,
                                           device={"manufacturer": "GoodWe", "model": "GW10K-ET"})
        return [report()]
    ob, client, _, _ = make([PollResult("consumed", choices={})], search=search_with_recommendation)
    asyncio.run(ob.async_run())
    detail = last(client)["control_mode"]["detail"]
    assert detail == "recommended: direct (GoodWe GW10K-ET)" and len(detail) <= 200


# ── oferta kroku `control_mode`: obie ścieżki z opisem i rekomendacją ──
def _offer(client):
    st = last(client)["control_mode"]
    assert st["state"] == "choice" and [o["id"] for o in st["options"]] == ["entities", "direct"]
    return ({o["id"]: (o["available"], o["description_code"]) for o in st["options"]},
            st.get("recommended"), st.get("reason"))


def _unverified(profile):
    raw = _thaw(profile.raw)
    raw["status"] = "draft"
    return profile_from_dict(raw)


def test_control_mode_choice_offers_both_paths_with_recommendation():
    # Bez integracji falownika: sonda rozpoznała falownik, profil i ścieżka rejestrów zweryfikowane.
    ob, client, _, _ = make([PollResult("consumed", choices={})], reports=[report()])
    asyncio.run(ob.async_run())
    assert _offer(client) == ({"entities": (False, "entities_no_write_entities"), "direct": (True, "direct_ok")},
                              "direct", "no_integration_identify_ok")
    # profil roboczy: „Bezpośrednio” oferowane z opisem próby 24 h; powód z listy zamkniętej, bez nowego kodu
    ob, client, _, _ = make([PollResult("consumed", choices={})], reports=[report()], profiles=(GW,))
    asyncio.run(ob.async_run())
    assert _offer(client) == ({"entities": (False, "entities_no_write_entities"),
                               "direct": (True, "direct_profile_draft_trial")}, "direct", "no_integration_identify_ok")
    # nieudana próba ścieżki rejestrów: nie da się — zalecenia brak (nie na niedostępną kartę), powód zostaje
    ob, client, _, _ = make([PollResult("consumed", choices={})], reports=[report(available=False)])
    asyncio.run(ob.async_run())
    assert _offer(client) == ({"entities": (False, "entities_no_write_entities"),
                               "direct": (False, "direct_unavailable")}, None, "no_integration_identify_ok")
    # nic w sieci: `unsupported` → bez `recommended`, `reason` zostaje
    ob, client, _, _ = make([PollResult("consumed", choices={})], reports=[])
    asyncio.run(ob.async_run())
    assert _offer(client) == ({"entities": (False, "entities_no_write_entities"),
                               "direct": (False, "direct_unavailable")}, None, "no_write_path")


def test_control_mode_offer_with_an_inverter_integration():
    # Integracja trzyma falownik (adres lokalny): sondy nie ma, „Bezpośrednio” po wyłączeniu integracji.
    ob, client, _, calls = make([PollResult("consumed", choices={})], report_=REPORT, reports=[report()],
                                mapped=WRITE_KEYS)
    asyncio.run(ob.async_run())
    assert calls == []
    assert _offer(client) == ({"entities": (True, "entities_ok"),
                               "direct": (True, "direct_requires_disable_integration")},
                              "entities", "integration_write_entities")
    # profil niezweryfikowany: ścieżka encji tylko dla `verified` — karta niedostępna, bez zalecenia na nią
    ob, client, _, _ = make([PollResult("consumed", choices={})], report_=REPORT, mapped=WRITE_KEYS,
                            choice_profile=_unverified(GW))
    asyncio.run(ob.async_run())
    assert _offer(client) == ({"entities": (False, "entities_profile_unverified"),
                               "direct": (True, "direct_requires_disable_integration")},
                              None, "integration_write_entities")
    # integracja bez adresu lokalnego (chmura) nie trzyma portu, a sondy nie było — bezpośrednio nieznane
    cloud = {**REPORT, "inverters": [{**REPORT["inverters"][0], "host": None}]}
    ob, client, _, _ = make([PollResult("consumed", choices={})], report_=cloud, mapped=WRITE_KEYS)
    asyncio.run(ob.async_run())
    assert _offer(client)[0]["direct"] == (False, "direct_unavailable")


def test_direct_option_requires_disabling_only_a_known_foreign_integration():
    rep = report()
    assert ds_mod.direct_option(rep, [GW_V], ("goodwe",)) == {
        "id": "direct", "available": True, "description_code": "direct_requires_disable_integration"}
    # nieustalona kolizja, inny wpis Volcast, nieudana próba: wyłączenie integracji nie pomoże
    for clash, r in ((("unknown",), rep), (("volcast",), rep), (("goodwe",), report(available=False))):
        assert ds_mod.direct_option(r, [GW_V], clash)["description_code"] == "direct_unavailable"
    assert ds_mod.direct_option(None, [GW_V], held_by=GW_V)["available"] is True
    assert ds_mod.direct_option(None, [GW_V])["description_code"] == "direct_unavailable"
    assert set(ds_mod.DESCRIPTION_CODES) == {
        "entities_ok", "entities_no_write_entities", "entities_profile_unverified", "direct_ok",
        "direct_requires_disable_integration", "direct_profile_draft_trial", "direct_unavailable"}


# ── zdalny wybór `direct` z kreatora ──
def _goodwe_entry(disabled_by=None):
    return SimpleNamespace(domain="goodwe", entry_id="g", data={"host": HOST}, options={}, disabled_by=disabled_by)


def test_remote_direct_choice_starts_direct_search():
    # Integracja była w raporcie (bez sondy na starcie); właściciel ją wyłączył i wybrał „Bezpośrednio”.
    ob, client, entry, calls = make([PollResult("consumed", choices={"control_mode": "direct"})], report_=REPORT,
                                    reports=[report()], entries=[_goodwe_entry("user")])
    asyncio.run(ob.async_run())
    assert calls == [1]
    assert entry.options["control_mode"] == "direct" and entry.options["direct_target"]["device_fp"] == FP
    assert "direct_trial" not in entry.options
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "done", "detail": "direct"}


CONFLICT_TEXT = "another integration is using this inverter"


def test_remote_direct_choice_with_the_integration_still_enabled_keeps_the_step_open():
    # Odmowa z powodu kolizji nie kończy kroku: ta sama oferta, szczegół z tekstem odmowy; powtarzane
    # odpytania z tym samym wyborem nie ponawiają sondy, dopóki stan wpisów się nie zmieni.
    ob, client, entry, calls = make([PollResult("consumed", choices={"control_mode": "direct"})], report_=REPORT,
                                    reports=[report(identity=False, errors=("conflict",))],
                                    entries=[_goodwe_entry()], mapped=WRITE_KEYS)
    asyncio.run(ob.async_run())
    assert calls == [1]
    assert not {"control_mode", "direct_target", "direct_trial"} & set(entry.options)
    st = last(client)["control_mode"]
    assert (st["state"], st["detail"]) == ("choice", CONFLICT_TEXT)
    assert {o["id"]: o["description_code"] for o in st["options"]}["direct"] == "direct_requires_disable_integration"
    assert "control_mode" not in entry.data["pairing"].get("applied_choices", {}).get("choices", {})
    assert "path_done" not in ob._runtime().executor.control_meta                # znacznik nietknięty


def test_refused_direct_then_entities_chosen_applies_entities():
    polls = [PollResult("consumed", choices={"control_mode": "direct"})] * 3 + [
        PollResult("consumed", choices={"control_mode": "entities"})]
    ob, client, entry, calls = make(polls, report_=REPORT, reports=[report(identity=False, errors=("conflict",))],
                                    entries=[_goodwe_entry()], mapped=WRITE_KEYS)
    asyncio.run(ob.async_run())
    assert calls == [1] and entry.options["control_mode"] == "entities" and "direct_target" not in entry.options
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "done", "detail": "entities"}
    assert any(s["key"] == "control_mode" and s["state"] == "choice" and s.get("detail") == CONFLICT_TEXT
               for p in client.progress for s in p)


def test_refused_direct_then_integration_disabled_applies_direct_once():
    goodwe = _goodwe_entry()
    searches = []
    polls_seen = []

    async def search():
        searches.append(1)
        return [report()] if goodwe.disabled_by else [report(identity=False, errors=("conflict",))]

    class Owner(Client):
        async def async_poll(self, s):
            polls_seen.append(1)
            if len(polls_seen) == 3:
                goodwe.disabled_by = "user"        # właściciel wyłącza integrację i zostaje przy „Bezpośrednio”
            return await super().async_poll(s)
    client = Owner([PollResult("consumed", choices={"control_mode": "direct"})], OK_PLAN)
    ob, _, entry, _ = make([], report_=REPORT, entries=[goodwe], search=search, client=client)
    asyncio.run(ob.async_run())
    assert searches == [1, 1]                      # odmowa, potem jedno wyszukiwanie po zmianie
    assert entry.options["control_mode"] == "direct" and entry.options["direct_target"]["device_fp"] == FP
    assert len(_direct_option_writes(ob)) == 1
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "done", "detail": "direct"}
    assert entry.data["pairing"]["applied_choices"]["choices"]["control_mode"]["value"] == "direct"


# ── wybór z kreatora a tryb „tylko plan” (prawdziwy runtime sterowania) ──
def _real_runtime(ob, entry, *, plan_only=False, mapped=(), choice=None):
    from tests.cloud.test_control_choice import real_rt
    rt = real_rt()
    rt.entry, rt.hass = entry, ob._hass
    rt.executor.plan_only = plan_only
    rt.choice, rt.mapped = choice, {k: f"x.{k}" for k in mapped}
    ob._runtime = lambda: rt
    return rt


def test_wizard_direct_choice_leaves_plan_only_mode():
    ob, client, entry, searches = make([PollResult("consumed", choices={"control_mode": "direct"})],
                                       reports=[report()])
    rt = _real_runtime(ob, entry, plan_only=True)
    asyncio.run(ob.async_run())
    assert entry.options["control_mode"] == "direct"
    assert rt.executor.plan_only is False and rt.verification.calls == ["restart"]    # drabina od startu
    # ta sama decyzja z planu: znacznik nadal pomija drugie zastosowanie (bez drugiego wyszukiwania)
    plan_calls, rt.async_apply_path_choice = _path_spy(entry)
    rt.wall_clock = lambda: NOW + timedelta(minutes=10)
    asyncio.run(control_choice.apply({"path": "direct", "path_at": AT, "at": AT}, rt))
    assert plan_calls == [] and searches == [1] and len(_direct_option_writes(ob)) == 1
    assert rt.executor.control_meta["ack"]["path"] == "direct" and rt.executor.plan_only is False


def test_wizard_entities_choice_leaves_plan_only_mode():
    ob, client, entry, _ = make([PollResult("consumed", choices={"control_mode": "entities"})], report_=REPORT,
                                mapped=WRITE_KEYS)
    rt = _real_runtime(ob, entry, plan_only=True, mapped=WRITE_KEYS, choice=ProfileChoice(GW_V, "goodwe", None))
    asyncio.run(ob.async_run())
    assert entry.options["control_mode"] == "entities" and rt.executor.plan_only is False
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "done", "detail": "entities"}


def test_wizard_plan_only_choice_turns_plan_only_on():
    ob, client, entry, _ = make([PollResult("consumed", choices={"control_mode": "plan_only"})], reports=[report()])
    rt = _real_runtime(ob, entry)
    asyncio.run(ob.async_run())
    assert rt.executor.plan_only is True and "park" in rt.verification.calls
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "done", "detail": "plan_only"}
    assert entry.data["pairing"]["applied_choices"]["choices"]["control_mode"]["value"] == "plan_only"


# ── reguła jednego zastosowania (kreator i plan niosą tę samą decyzję) ──
AT = "2026-09-27T10:02:00Z"
AT_LATER = "2026-09-27T10:20:00Z"


def _path_spy(entry):
    calls = []

    async def apply(path):                    # jak `async_apply_path_choice`: zmienia opcje wpisu
        calls.append(path)
        entry.options = {**entry.options, "control_mode": path}
        return "applied"
    return calls, apply


def _direct_option_writes(ob):
    return [c for c in ob._hass.config_entries.async_update_entry.call_args_list
            if "direct_target" in (c.kwargs.get("options") or {})]


def test_wizard_applied_choice_and_same_control_path_apply_once():
    ob, client, entry, searches = make([PollResult("consumed", choices={"control_mode": "direct"})],
                                       reports=[report()])
    rt = ob._runtime()
    plan_calls, rt.async_apply_path_choice = _path_spy(entry)
    asyncio.run(ob.async_run())
    assert entry.options["control_mode"] == "direct" and searches == [1]
    assert rt.executor.control_meta["path_done"] == "direct" and rt.executor.saved >= 1
    rt.wall_clock = lambda: NOW + timedelta(minutes=10)
    asyncio.run(control_choice.apply({"path": "direct", "path_at": AT, "at": AT}, rt))
    meta = rt.executor.control_meta
    assert plan_calls == [] and searches == [1] and len(_direct_option_writes(ob)) == 1   # jeden start drabiny
    assert meta["ack"] == {"path": "direct", "at": AT} and meta["path_at"] == AT and meta["path_done"] == "direct"


def test_control_path_then_same_wizard_choice_apply_once():
    holder = {}

    class PlanFirst(Client):
        async def async_poll(self, s):
            if "done" not in holder:              # plan z decyzją dociera przed odpytaniem kreatora
                holder["done"] = True
                await control_choice.apply({"path": "direct", "path_at": AT, "at": AT}, holder["rt"])
            return await super().async_poll(s)
    client = PlanFirst([PollResult("consumed", choices={"control_mode": "direct"})], OK_PLAN)
    ob, _, entry, searches = make([], reports=[report()], client=client)
    rt = holder["rt"] = ob._runtime()
    plan_calls, rt.async_apply_path_choice = _path_spy(entry)
    asyncio.run(ob.async_run())
    assert plan_calls == ["direct"] and searches == [1] and _direct_option_writes(ob) == []
    assert last(client)["control_mode"] == {"key": "control_mode", "state": "done", "detail": "direct"}
    record = entry.data["pairing"]["applied_choices"]["choices"]["control_mode"]
    assert record["value"] == "direct" and "mark" not in record          # kreator niczego nie zastosował


def test_wizard_choice_then_different_control_path_applies():
    ob, client, entry, _ = make([PollResult("consumed", choices={"control_mode": "direct"})], reports=[report()])
    rt = ob._runtime()
    plan_calls, rt.async_apply_path_choice = _path_spy(entry)
    asyncio.run(ob.async_run())
    rt.wall_clock = lambda: NOW + timedelta(minutes=10)
    asyncio.run(control_choice.apply({"path": "entities", "path_at": AT, "at": AT}, rt))
    assert plan_calls == ["entities"] and rt.executor.control_meta["ack"]["path"] == "entities"
    # znacznik zużyty: późniejsze `direct` z planu to zwykły (prawdziwy) ponowny wybór
    asyncio.run(control_choice.apply({"path": "direct", "path_at": AT_LATER, "at": AT_LATER}, rt))
    assert plan_calls == ["entities", "direct"]


def test_wizard_mark_expires_with_the_live_window():
    ob, client, entry, _ = make([PollResult("consumed", choices={"control_mode": "direct"})], reports=[report()])
    rt = ob._runtime()
    plan_calls, rt.async_apply_path_choice = _path_spy(entry)
    asyncio.run(ob.async_run())
    rt.wall_clock = lambda: NOW + timedelta(minutes=45)
    asyncio.run(control_choice.apply({"path": "direct", "path_at": AT, "at": AT}, rt))
    assert plan_calls == ["direct"]
