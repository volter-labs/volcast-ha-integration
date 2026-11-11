"""Zgłoszenia naprawy sterowania encjami: encje sterujące wyłączone albo nieobecne i zapis odrzucony
przez urządzenie. Zgłoszenie znika po naprawie; profil bez tych konstrukcji niczego nie zgłasza."""
import asyncio
from datetime import timedelta

from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import issue_registry as ir

from custom_components.volcast.control.executor import ControlGaps

from . import test_executor as base
from .test_executor_handback import ENT, make, take_over


def _issues(issue_id):
    return [c for c in ir.async_create_issue.call_args_list if c.args[2] == issue_id]


def _deleted(issue_id):
    return [c for c in ir.async_delete_issue.call_args_list if c.args[2] == issue_id]


def _reset():
    ir.async_create_issue.reset_mock()
    ir.async_delete_issue.reset_mock()


def _tick(ex, seconds=60):
    ex._clock.t += seconds
    ex._utcnow.t += timedelta(seconds=seconds)
    asyncio.run(ex.async_tick())


# ── encje wyłączone / nieobecne ──────────────────────────────────────────


def test_disabled_control_entity_raises_issue_listing_it_and_clears_after_enabling(monkeypatch):
    _reset()
    h, ex = make(monkeypatch=monkeypatch)
    gaps = {"now": ControlGaps(disabled=("switch.x_remote",))}
    ex.entity_gaps_source = lambda: gaps["now"]
    take_over(h, ex)
    (call,) = _issues("control_entities_disabled_e1")
    assert call.kwargs["translation_key"] == "control_entities_disabled"
    assert call.kwargs["translation_placeholders"] == {"entities": "switch.x_remote"}
    assert not _issues("control_entities_missing_e1")
    _tick(ex)
    assert len(_issues("control_entities_disabled_e1")) == 1          # bez powtórzeń
    gaps["now"] = ControlGaps()
    _tick(ex)
    assert _deleted("control_entities_disabled_e1")


def test_missing_control_entity_raises_issue_with_the_setting_and_clears_when_present(monkeypatch):
    _reset()
    h, ex = make(monkeypatch=monkeypatch)
    gaps = {"now": ControlGaps(missing=("enable", "mode"))}
    ex.entity_gaps_source = lambda: gaps["now"]
    take_over(h, ex)
    (call,) = _issues("control_entities_missing_e1")
    assert call.kwargs["translation_key"] == "control_entities_missing"
    assert call.kwargs["translation_placeholders"] == {"entities": "enable, mode"}
    assert not _issues("control_entities_disabled_e1")
    gaps["now"] = ControlGaps()
    _tick(ex)
    assert _deleted("control_entities_missing_e1")


def test_no_gap_issue_when_the_owner_did_not_choose_entity_control(monkeypatch):
    _reset()
    h, ex = make(monkeypatch=monkeypatch)
    ex._entry.options = {}
    ex.entity_gaps_source = lambda: ControlGaps(disabled=("switch.x_remote",), missing=("enable",))
    take_over(h, ex)
    assert not _issues("control_entities_disabled_e1") and not _issues("control_entities_missing_e1")


def test_onboarding_request_raises_the_issue_before_control_is_chosen(monkeypatch):
    _reset()
    h, ex = make(monkeypatch=monkeypatch)
    ex._entry.options = {}
    ex.entity_gaps_source = lambda: ControlGaps(disabled=("switch.x_remote",))
    ex.refresh_entity_gaps(force=True)
    assert len(_issues("control_entities_disabled_e1")) == 1


def test_entities_that_appear_later_request_one_reload(monkeypatch):
    _reset()
    h, ex = make(monkeypatch=monkeypatch)
    reloads = []
    ex.request_reload = lambda: reloads.append(1)
    gaps = {"now": ControlGaps(disabled=("switch.x_remote",))}
    ex.entity_gaps_source = lambda: gaps["now"]
    take_over(h, ex)
    gaps["now"] = ControlGaps(recovered=("enable",))       # włączona, jeszcze niezmapowana
    _tick(ex)
    _tick(ex)
    assert reloads == [1] and _deleted("control_entities_disabled_e1")


def test_recovered_entities_without_a_prior_gap_never_reload(monkeypatch):
    # Encja pasuje, ale mapowanie ją odrzuca (np. jednostka): bez pętli przeładowań po każdym starcie.
    h, ex = make(monkeypatch=monkeypatch)
    reloads = []
    ex.request_reload = lambda: reloads.append(1)
    ex.entity_gaps_source = lambda: ControlGaps(recovered=("enable",))
    take_over(h, ex)
    _tick(ex)
    assert reloads == []


# ── zapis odrzucony przez urządzenie ─────────────────────────────────────


def test_unsupported_switch_raises_write_rejected_issue(monkeypatch):
    _reset()
    h, ex = make(monkeypatch=monkeypatch)
    h.services.fail[ENT["enable"]] = ServiceNotFound("switch", "turn_on")
    take_over(h, ex)
    (call, *_) = _issues("control_write_rejected_e1")
    assert call.kwargs["translation_key"] == "control_write_rejected"
    assert call.kwargs["translation_placeholders"] == {"settings": "enable"}


def test_refused_set_point_at_takeover_raises_issue_and_clears_after_a_clean_write(monkeypatch):
    _reset()
    h, ex = make(monkeypatch=monkeypatch)
    h.services.fail[ENT["discharge_power_w"]] = RuntimeError("x")
    take_over(h, ex)
    (call, *_) = _issues("control_write_rejected_e1")
    assert call.kwargs["translation_placeholders"] == {"settings": "discharge_power_w"}
    h.services.fail.clear()
    for _i in range(6):
        _tick(ex, 20)
    assert h.states.get(ENT["discharge_power_w"]).state == "2.5"
    assert _deleted("control_write_rejected_e1")


def test_clean_takeover_raises_nothing(monkeypatch):
    _reset()
    h, ex = make(monkeypatch=monkeypatch)
    take_over(h, ex)
    assert ex.last_decision.status == "write"
    assert not _issues("control_write_rejected_e1")


def test_write_rejected_issue_is_removed_when_control_stops(monkeypatch):
    _reset()
    h, ex = make(monkeypatch=monkeypatch)
    h.services.fail[ENT["enable"]] = ServiceNotFound("switch", "turn_on")
    take_over(h, ex)
    ir.async_delete_issue.reset_mock()
    asyncio.run(ex.async_stop())
    assert _deleted("control_write_rejected_e1")


def test_profile_without_gaps_source_is_unchanged(monkeypatch):
    # Zwykły profil (bez źródła luk): żadnych nowych zgłoszeń.
    _reset()
    h, ex = base.make(monkeypatch=monkeypatch)
    asyncio.run(base.ready(ex))
    asyncio.run(ex.async_tick())
    assert not _issues("control_entities_disabled_e1") and not _issues("control_entities_missing_e1")
    assert not _issues("control_write_rejected_e1")
