"""Nazwy API HA używane przez integrację (testy główne stoją na atrapach)."""
import inspect

from homeassistant import config_entries
from homeassistant.core import Context, State
from homeassistant.helpers import issue_registry as ir


def test_config_flow_external_step_api():
    assert hasattr(config_entries.ConfigFlow, "async_external_step")
    assert hasattr(config_entries.ConfigFlow, "async_external_step_done")
    assert "unique_id" in inspect.signature(config_entries.ConfigEntries.async_update_entry).parameters
    # Kreator woła oba nazwanymi argumentami (bez przestarzałego `step_id` przy kroku zewnętrznym).
    assert "url" in inspect.signature(config_entries.ConfigFlow.async_external_step).parameters
    assert "next_step_id" in inspect.signature(config_entries.ConfigFlow.async_external_step_done).parameters
    # Jedno przeładowanie po aktualizacji wpisu bez słuchacza.
    assert hasattr(config_entries.ConfigEntries, "async_schedule_reload")
    # Pole instancji: kreator sprawdza, czy uruchomiony wpis ma słuchacza aktualizacji.
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    assert MockConfigEntry(domain="volcast").update_listeners == []


def test_helpers_used():
    from homeassistant.helpers import instance_id, issue_registry
    from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
    assert callable(instance_id.async_get) and callable(issue_registry.async_create_issue)
    assert callable(async_track_state_change_event) and callable(async_track_time_interval)
    assert hasattr(config_entries.ConfigEntry, "async_create_background_task")


def test_recorder_statistics_api():
    from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
    assert "statistic_ids" in inspect.signature(get_metadata).parameters
    assert "types" in inspect.signature(statistics_during_period).parameters
    # Import historii woła `statistics_during_period` POZYCYJNIE — kolejność parametrów to kontrakt.
    params = list(inspect.signature(statistics_during_period).parameters)
    assert params[:7] == ["hass", "start_time", "end_time", "statistic_ids", "period", "units", "types"]
    # `get_metadata` przyjmuje identyfikatory tylko nazwanym argumentem.
    p = inspect.signature(get_metadata).parameters["statistic_ids"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY


def test_frontend_api():
    from homeassistant.components import panel_custom
    from homeassistant.components.frontend import add_extra_js_url, async_remove_panel
    from homeassistant.components.http import StaticPathConfig
    assert callable(panel_custom.async_register_panel) and callable(add_extra_js_url)
    assert callable(async_remove_panel) and StaticPathConfig
    reg = inspect.signature(panel_custom.async_register_panel).parameters
    for name in ("frontend_url_path", "webcomponent_name", "sidebar_title", "sidebar_icon", "module_url",
                 "config", "require_admin"):
        assert name in reg


def test_context_and_state_fields():
    ctx = Context(user_id="u", parent_id="p")
    assert ctx.id and ctx.user_id == "u" and ctx.parent_id == "p"
    assert "last_reported" in dir(State("sensor.x", "1"))


def test_issue_severity_used_by_integration_is_real():
    """Zgłoszenia naprawy muszą dostać prawdziwą wagę z rejestru zgłoszeń HA."""
    import custom_components.volcast as integ
    from custom_components.volcast.control import executor

    assert integ.IssueSeverity is ir.IssueSeverity
    assert executor._WARNING is ir.IssueSeverity.WARNING


def test_options_flow_base_still_available():
    # Klasa bazowa przepływu opcji jest wycofywana w rdzeniu; dla integracji własnych działa.
    assert hasattr(config_entries, "OptionsFlowWithConfigEntry")
