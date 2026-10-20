"""Odporność prognozy na prawdziwym rdzeniu HA: 503 po udanym starcie, start bez danych,
czujnik wieku prognozy na zegarze HA (freezer) i sprzątanie jego timera przy rozładunku."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.volcast.const import DOMAIN

from .conftest import FORECAST_ONLY_DATA, forecast_payload, make_entry, setup_entry, volcast_timers

T0 = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
HOUR = timedelta(minutes=60)          # domyślny interwał odpytywania koordynatora
POLL = timedelta(seconds=31)          # odpytanie encji bez koordynatora (binary_sensor co 30 s)


def _forecast_only_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Volcast", data=dict(FORECAST_ONLY_DATA),
                            entry_id="fc01", unique_id="account_fc")
    entry.add_to_hass(hass)
    return entry


def _eid(hass, domain: str, entry, key: str) -> str:
    eid = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}")
    assert eid, key
    return eid


async def _tick(hass, freezer, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    # odświeżenie z harmonogramu koordynatora biegnie jako zadanie w tle wpisu
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_failed_refreshes_keep_last_values_and_count_failures(hass: HomeAssistant, forecast_api, freezer):
    """503 po udanym starcie: wartości i dostępność zostają, zdrowie gaśnie, licznik porażek rośnie
    o jeden na odświeżenie i wraca do zera po pierwszym sukcesie."""
    freezer.move_to(T0)
    forecast_api.payload = forecast_payload(hass, energy_today=12.5, generated_at="2026-06-15T11:50:00Z")
    entry = _forecast_only_entry(hass)
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    coord = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    today, tomorrow = _eid(hass, "sensor", entry, "energy_today"), _eid(hass, "sensor", entry, "energy_tomorrow")
    status = _eid(hass, "sensor", entry, "api_status")
    healthy = _eid(hass, "binary_sensor", entry, "integration_healthy")
    before = {e: hass.states.get(e).state for e in (today, tomorrow, status)}
    assert before[today] == "12.5" and before[status] == "Active"
    assert hass.states.get(healthy).state == STATE_ON
    assert coord.consecutive_failures == 0 and coord.last_update_success_time == T0

    forecast_api.status = 503
    for failures in (1, 2):
        requests = forecast_api.requests
        await _tick(hass, freezer, HOUR)
        await _tick(hass, freezer, POLL)
        assert forecast_api.requests - requests == 4          # 1 + 3 ponowienia (bez realnych przerw)
        assert coord.consecutive_failures == failures and coord.last_update_success is False
        for eid, value in before.items():
            st = hass.states.get(eid)
            assert st.state == value, (eid, st.state)          # ostatnia wartość, dalej dostępna
        assert hass.states.get(healthy).state == STATE_OFF
        assert hass.states.get(healthy).attributes["forecast_ok"] is False
    assert coord.last_update_success_time == T0                # czas ostatniego sukcesu bez zmian

    forecast_api.status = 200
    forecast_api.payload = forecast_payload(hass, energy_today=13.0, generated_at="2026-06-15T13:55:00Z")
    await _tick(hass, freezer, HOUR)
    await _tick(hass, freezer, POLL)
    assert coord.consecutive_failures == 0 and coord.last_update_success is True
    assert hass.states.get(today).state == "13.0"
    assert hass.states.get(healthy).state == STATE_ON
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_forecast_only_entry_never_loaded_retries_setup_without_entities(hass: HomeAssistant,
                                                                               forecast_api):
    """Wpis tylko-prognoza, pierwszy odczyt 503: HA ponowi setup później, bez encji i bez awarii."""
    forecast_api.status = 503
    entry = _forecast_only_entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert forecast_api.requests == 4
    assert not hass.data.get(DOMAIN, {}).get(entry.entry_id)
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []
    assert volcast_timers(hass) == []


async def test_paired_entry_never_loaded_has_unavailable_forecast_entities(hass: HomeAssistant, forecast_api):
    """Wpis sparowany, pierwszy odczyt 503: wpis wstaje, `coordinator.data` puste, encje prognozy
    niedostępne (czujnik wieku też), zdrowie zgaszone, licznik porażek = 1."""
    forecast_api.status = 503
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    coord = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    assert coord.data is None and coord.consecutive_failures == 1
    for key in ("energy_today", "energy_tomorrow", "forecast_age"):
        assert hass.states.get(_eid(hass, "sensor", entry, key)).state == STATE_UNAVAILABLE, key
    assert hass.states.get(_eid(hass, "binary_sensor", entry, "integration_healthy")).state == STATE_OFF
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_forecast_age_grows_each_minute_and_timer_goes_with_unload(hass: HomeAssistant, forecast_api,
                                                                          freezer):
    freezer.move_to(T0)
    forecast_api.payload = forecast_payload(hass, generated_at="2026-06-15T11:50:00Z", cache_age_minutes=7)
    entry = _forecast_only_entry(hass)
    await setup_entry(hass, entry)
    age = _eid(hass, "sensor", entry, "forecast_age")
    st = hass.states.get(age)
    assert st.state == "10"
    assert st.attributes["generated_at"] == "2026-06-15T11:50:00Z"
    assert st.attributes["server_reported_cache_age_minutes"] == 7
    assert st.attributes["minutes_since_last_success"] == 0
    assert st.attributes["consecutive_failures"] == 0
    for minute in (1, 2, 3):
        await _tick(hass, freezer, timedelta(minutes=1))
        st = hass.states.get(age)
        assert st.state == str(10 + minute)                    # bez odpytania koordynatora
        assert st.attributes["minutes_since_last_success"] == minute
    assert forecast_api.requests == 1

    timers = volcast_timers(hass)
    assert [t for t in timers if t.endswith("VolcastForecastAgeSensor._async_handle_refresh")], timers
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert volcast_timers(hass) == []
    assert hass.states.get(age).state == STATE_UNAVAILABLE
    await _tick(hass, freezer, timedelta(minutes=5))
    assert hass.states.get(age).state == STATE_UNAVAILABLE
