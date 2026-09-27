"""Kreator i opcje na prawdziwym menedżerze przepływów HA: krok zewnętrzny parowania,
formularz adresu (tryb zaawansowany), zamknięcie kreatora, czyszczenie pól opcji
i `include_entities` w selektorze ceny."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations
import homeassistant.util.dt as dt_util

from custom_components.volcast import config_flow as cf
from custom_components.volcast.cloud.client import Backend, PairingSession, PollResult
from custom_components.volcast.const import DOMAIN

from .conftest import API_KEY, BACKEND, control_of, make_entry, setup_entry

CONNECT_URL = "https://app.example.test/connect?code=ABCD"


class FakePairing:
    """Klient parowania: sesja od razu, anulowanie zapamiętane."""
    instances: list["FakePairing"] = []

    def __init__(self, session, url):
        self.url = url
        self.begun = None
        self.cancelled = []
        FakePairing.instances.append(self)

    async def async_begin(self, **kw):
        self.begun = kw
        return PairingSession("sess1", "poll1", CONNECT_URL, "2026-09-27T12:10:00Z")

    async def async_cancel(self, session):
        self.cancelled.append(session.session_id)


@pytest.fixture
def pairing(monkeypatch):
    FakePairing.instances.clear()
    release = asyncio.Event()
    outcome: dict = {"result": PollResult("confirmed", api_key=API_KEY, user_id="u1",
                                          backend=Backend.from_dict(BACKEND))}

    class FakePoller:
        def __init__(self, client, session, **kw):
            pass

        async def async_wait(self):
            await release.wait()
            return outcome["result"]
    monkeypatch.setattr(cf, "PairingClient", FakePairing)
    monkeypatch.setattr(cf, "PairingPoller", FakePoller)
    return release, outcome


async def _to_pair(hass, **context):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user", **context})
    assert result["type"] is FlowResultType.MENU and "pair" in result["menu_options"]
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair"})


async def test_pairing_external_step_to_entry(hass: HomeAssistant, pairing, network_down):
    release, _ = pairing
    result = await _to_pair(hass)
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    # Krok zapisany jako `pair` — frontend pokazuje `config.step.pair.description`.
    assert result["step_id"] == "pair" and result["url"] == CONNECT_URL
    tr = await async_get_translations(hass, "en", "config", {DOMAIN})
    assert "Confirm this connection" in tr[f"component.{DOMAIN}.config.step.pair.description"]
    begun = FakePairing.instances[0].begun
    assert begun["instance_id"] and begun["ha_version"]

    # Chmura potwierdza: task oczekiwania przestawia kreator (external_step_done).
    release.set()
    await hass.async_block_till_done()
    progress = hass.config_entries.flow.async_get(result["flow_id"])
    assert progress["step_id"] == "pair_finish"
    # Frontend po zdarzeniu kontynuuje kreator bez danych.
    done = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert done["type"] is FlowResultType.CREATE_ENTRY
    entry = done["result"]
    assert entry.data["backend"] == BACKEND and entry.data["pairing"]["session_id"] == "sess1"
    assert entry.unique_id.startswith("account_") and API_KEY not in entry.unique_id
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    # Poświadczenia wydane — zamknięcie kreatora nie anuluje sesji.
    assert FakePairing.instances[0].cancelled == []


async def test_pairing_rejected_outcome_aborts(hass: HomeAssistant, pairing):
    release, outcome = pairing
    outcome["result"] = PollResult("expired")
    result = await _to_pair(hass)
    release.set()
    await hass.async_block_till_done()
    done = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert done["type"] is FlowResultType.ABORT and done["reason"] == "pairing_expired"


async def test_closing_the_wizard_cancels_the_session(hass: HomeAssistant, pairing):
    result = await _to_pair(hass)
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert FakePairing.instances[0].cancelled == ["sess1"]


async def test_advanced_mode_shows_address_form_then_external_step(hass: HomeAssistant, pairing):
    result = await _to_pair(hass, show_advanced_options=True)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pair"
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], {"pairing_url": "http://x.test/p"})
    assert bad["type"] is FlowResultType.FORM and bad["errors"] == {"base": "invalid_url"}
    ok = await hass.config_entries.flow.async_configure(result["flow_id"],
                                                        {"pairing_url": "https://other.example.test/p"})
    assert ok["type"] is FlowResultType.EXTERNAL_STEP and ok["step_id"] == "pair"
    assert FakePairing.instances[0].url == "https://other.example.test/p"
    tr = await async_get_translations(hass, "en", "config", {DOMAIN})
    assert f"component.{DOMAIN}.config.step.pair.data.pairing_url" in tr
    hass.config_entries.flow.async_abort(ok["flow_id"])
    await hass.async_block_till_done()


# ── opcje ─────────────────────────────────────────────────────────────────────


def _fields(result) -> dict:
    """Pola schematu formularza: nazwa → (klucz voluptuous, walidator/selektor)."""
    return {str(k): (k, v) for k, v in result["data_schema"].schema.items()}


async def test_forecast_options_optional_entity_can_be_cleared(hass: HomeAssistant, network_down):
    hass.states.async_set("sensor.pv", "1", {"device_class": "energy", "unit_of_measurement": "kWh"})
    entry = make_entry(hass, options={"pv_energy_entity": "sensor.pv", "control_mode": "entities"})
    await setup_entry(hass, entry)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["type"] is FlowResultType.MENU
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "forecast"})
    key, _ = _fields(form)["pv_energy_entity"]
    # Podpowiedź, nie wartość domyślna — wyczyszczone pole nie wraca samo.
    assert key.default is vol.UNDEFINED and key.description == {"suggested_value": "sensor.pv"}
    # Frontend pomija wyczyszczone pole wyboru encji.
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"update_interval": 60,
                                                                               "peak_threshold": 80})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert "pv_energy_entity" not in entry.options and entry.options["control_mode"] == "entities"


async def test_entity_selector_rejects_empty_string(hass: HomeAssistant, network_down):
    """Pusty napis to NIE wyczyszczenie w prawdziwym selektorze — tylko pominięty klucz."""
    entry = make_entry(hass, options={"pv_energy_entity": "sensor.pv"})
    await setup_entry(hass, entry)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "forecast"})
    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(form["flow_id"], {"pv_energy_entity": ""})


def _price_attrs():
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    return {"currency": "PLN", "raw_today": [
        {"start": (start + timedelta(hours=i)).isoformat(), "end": (start + timedelta(hours=i + 1)).isoformat(),
         "value": 0.5} for i in range(24)]}


async def test_price_selector_include_entities_and_saved_unusable_entity(hass: HomeAssistant, network_down):
    hass.states.async_set("sensor.good_prices", "0.5", _price_attrs())
    hass.states.async_set("sensor.stale_prices", "0.5", {"currency": "PLN"})
    hass.states.async_set("sensor.other", "1", {})
    entry = make_entry(hass, options={"entity_price_buy": "sensor.stale_prices", "price_currency": "PLN"})
    await setup_entry(hass, entry)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "prices"})
    _, buy = _fields(form)["entity_price_buy"]
    assert buy.config["include_entities"] == ["sensor.good_prices", "sensor.stale_prices"]
    # Encja spoza listy — odrzucona przez prawdziwy selektor.
    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(form["flow_id"], {"entity_price_buy": "sensor.other"})
    # Zapisana (chwilowo bez serii) nie blokuje zapisu reszty.
    form = await hass.config_entries.options.async_configure(
        (await hass.config_entries.options.async_init(entry.entry_id))["flow_id"], {"next_step_id": "prices"})
    done = await hass.config_entries.options.async_configure(
        form["flow_id"], {"entity_price_buy": "sensor.stale_prices", "price_currency": "EUR"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["price_currency"] == "EUR"
    # Wyczyszczenie ceny kupna (klucz pominięty) usuwa ją z opcji.
    form = await hass.config_entries.options.async_configure(
        (await hass.config_entries.options.async_init(entry.entry_id))["flow_id"], {"next_step_id": "prices"})
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"price_currency": "EUR"})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert "entity_price_buy" not in entry.options


async def test_options_change_reloads_entry_through_update_listener(hass: HomeAssistant, network_down):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    before = control_of(hass, entry)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(menu["flow_id"], {"next_step_id": "forecast"})
    await hass.config_entries.options.async_configure(form["flow_id"], {"update_interval": 90, "peak_threshold": 80})
    await hass.async_block_till_done()
    after = control_of(hass, entry)
    assert entry.state is ConfigEntryState.LOADED and after is not None and after is not before
    assert before.executor._stopped


async def _pair_through(hass, release) -> dict:
    result = await _to_pair(hass)
    release.set()
    await hass.async_block_till_done()
    done = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    return done


async def test_pairing_upgrades_discovery_only_entry_in_place(hass: HomeAssistant, pairing, network_down,
                                                              monkeypatch):
    import custom_components.volcast as integ

    release, _ = pairing
    disc = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    created = await hass.config_entries.flow.async_configure(disc["flow_id"], {"next_step_id": "discovery_only"})
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = created["result"]
    assert entry.state is ConfigEntryState.LOADED
    setups = []
    real = integ.async_setup_entry

    async def counting(h, e):
        setups.append(e.entry_id)
        return await real(h, e)
    monkeypatch.setattr(integ, "async_setup_entry", counting)
    done = await _pair_through(hass, release)
    assert done["type"] is FlowResultType.ABORT and done["reason"] == "paired_existing"
    assert entry.state is ConfigEntryState.LOADED and "mode" not in entry.data
    assert entry.data["backend"] == BACKEND and control_of(hass, entry) is not None
    assert setups == [entry.entry_id]              # dokładnie jedno przeładowanie


async def test_pairing_updates_running_account_entry_with_one_reload(hass: HomeAssistant, pairing, network_down,
                                                                     monkeypatch):
    import custom_components.volcast as integ

    release, _ = pairing
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    assert entry.update_listeners                  # uruchomiony wpis konta ma słuchacza
    setups = []
    real = integ.async_setup_entry

    async def counting(h, e):
        setups.append(e.entry_id)
        return await real(h, e)
    monkeypatch.setattr(integ, "async_setup_entry", counting)
    done = await _pair_through(hass, release)
    assert done["type"] is FlowResultType.ABORT and done["reason"] == "paired_existing"
    assert entry.state is ConfigEntryState.LOADED and setups == [entry.entry_id]
    assert entry.data["pairing"]["session_id"] == "sess1"
