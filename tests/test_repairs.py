"""Naprawa „Wznów sterowanie teraz" w zgłoszeniu pauzy przejęcia."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import FakeHass


def _hass_with_control(executor) -> FakeHass:
    hass = FakeHass()
    hass.data["volcast"] = {"e1": {"control": SimpleNamespace(executor=executor)}}
    return hass


@pytest.mark.asyncio
async def test_foreign_control_fix_flow_confirms_then_resumes():
    from custom_components.volcast import repairs

    ex = SimpleNamespace(async_resume_control=AsyncMock(return_value="resumed"))
    hass = _hass_with_control(ex)
    flow = await repairs.async_create_fix_flow(hass, "foreign_control_e1", {"entry_id": "e1"})
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] == "form" and form["step_id"] == "confirm"
    ex.async_resume_control.assert_not_awaited()                 # samo otwarcie niczego nie wznawia
    done = await flow.async_step_confirm({})
    assert done["type"] == "create_entry"
    ex.async_resume_control.assert_awaited_once()


@pytest.mark.asyncio
async def test_fix_flow_for_unloaded_entry_just_closes():
    from custom_components.volcast import repairs

    hass = FakeHass()
    hass.data["volcast"] = {}
    flow = await repairs.async_create_fix_flow(hass, "foreign_control_e1", {"entry_id": "e1"})
    flow.hass = hass
    await flow.async_step_init()
    assert (await flow.async_step_confirm({}))["type"] == "create_entry"


@pytest.mark.asyncio
async def test_resume_service_resumes_every_controlled_entry():
    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_RESUME_CONTROL

    ex = SimpleNamespace(async_resume_control=AsyncMock(return_value="resumed"))
    hass = _hass_with_control(ex)
    hass.data["volcast"]["e2"] = {"reconciler": None}            # wpis bez sterowania
    _async_register_services(hass)
    await hass.services.registered[(DOMAIN, SERVICE_RESUME_CONTROL)](SimpleNamespace(data={}))
    ex.async_resume_control.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_service_without_control_raises_validation_error():
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_RESUME_CONTROL

    hass = FakeHass()
    hass.data["volcast"] = {"e1": {"reconciler": None}}
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.registered[(DOMAIN, SERVICE_RESUME_CONTROL)](SimpleNamespace(data={}))


@pytest.mark.asyncio
async def test_fix_flow_aborts_while_inverter_mode_is_outside_the_profile():
    # Tryb spoza profilu nadal wstrzymuje zapisy — „wznowiono" byłoby nieprawdą; zgłoszenie zostaje.
    from custom_components.volcast import repairs

    ex = SimpleNamespace(async_resume_control=AsyncMock(return_value="foreign_mode"))
    hass = _hass_with_control(ex)
    flow = await repairs.async_create_fix_flow(hass, "foreign_control_e1", {"entry_id": "e1"})
    flow.hass = hass
    await flow.async_step_init()
    assert await flow.async_step_confirm({}) == {"type": "abort", "reason": "foreign_mode"}
