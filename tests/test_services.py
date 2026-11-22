"""Tests for the volcast.sync_production service."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import FakeHass


class _FakeServiceCall:
    """Minimal ServiceCall double — only .data is used by the handler."""

    def __init__(self, data: dict | None = None):
        self.data = data or {}


def _register(hass: FakeHass):
    """Zarejestruj serwis i zwróć handler z fake rejestru."""
    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    _async_register_services(hass)
    return hass.services.registered[(DOMAIN, SERVICE_SYNC_PRODUCTION)]


def _hass_with_reconciler(reconciler) -> FakeHass:
    hass = FakeHass()
    hass.data["volcast"] = {"entry1": {"reconciler": reconciler}}
    return hass


def test_register_services_is_idempotent():
    """Podwójna rejestracja (2 config entries) nie nadpisuje handlera."""
    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    hass = FakeHass()
    _async_register_services(hass)
    first = hass.services.registered[(DOMAIN, SERVICE_SYNC_PRODUCTION)]
    _async_register_services(hass)
    assert hass.services.registered[(DOMAIN, SERVICE_SYNC_PRODUCTION)] is first


@pytest.mark.asyncio
async def test_sync_without_date_calls_reconcile_recent():
    reconciler = MagicMock()
    reconciler.reconcile_recent = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall())

    reconciler.reconcile_recent.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_with_date_calls_reconcile_day():
    reconciler = MagicMock()
    reconciler.reconcile_day = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall({"date": "2026-07-23"}))

    reconciler.reconcile_day.assert_awaited_once_with(date(2026, 7, 23))


@pytest.mark.asyncio
async def test_sync_with_date_object_calls_reconcile_day():
    """Selector date w HA może dostarczyć datetime.date, nie str."""
    reconciler = MagicMock()
    reconciler.reconcile_day = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall({"date": date(2026, 7, 23)}))

    reconciler.reconcile_day.assert_awaited_once_with(date(2026, 7, 23))


@pytest.mark.asyncio
async def test_sync_invalid_date_raises_validation_error():
    from homeassistant.exceptions import ServiceValidationError

    reconciler = MagicMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    with pytest.raises(ServiceValidationError):
        await handler(_FakeServiceCall({"date": "not-a-date"}))


@pytest.mark.asyncio
async def test_sync_no_reconcilers_raises_validation_error():
    """Entry bez production trackingu (brak energy_entity) → czytelny błąd."""
    from homeassistant.exceptions import ServiceValidationError

    hass = FakeHass()
    hass.data["volcast"] = {"entry1": {"reconciler": None}}
    handler = _register(hass)

    with pytest.raises(ServiceValidationError):
        await handler(_FakeServiceCall())


@pytest.mark.asyncio
async def test_sync_iterates_all_entries():
    """Serwis bez daty działa na wszystkich config entries z reconcilerem."""
    rec1, rec2 = MagicMock(), MagicMock()
    rec1.reconcile_recent = AsyncMock()
    rec2.reconcile_recent = AsyncMock()
    hass = FakeHass()
    hass.data["volcast"] = {
        "entry1": {"reconciler": rec1},
        "entry2": {"reconciler": rec2},
    }
    handler = _register(hass)

    await handler(_FakeServiceCall())

    rec1.reconcile_recent.assert_awaited_once()
    rec2.reconcile_recent.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_with_datetime_normalizes_to_date():
    """A full datetime (datetime subclasses date) is normalized to its date,
    not passed through — otherwise reconcile_day would TypeError silently."""
    from datetime import datetime

    reconciler = MagicMock()
    reconciler.reconcile_day = AsyncMock()
    hass = _hass_with_reconciler(reconciler)
    handler = _register(hass)

    await handler(_FakeServiceCall({"date": datetime(2026, 7, 23, 15, 30)}))

    reconciler.reconcile_day.assert_awaited_once_with(date(2026, 7, 23))


# ---------------------------------------------------------------------------
# Lifecycle: service removed only on the LAST config-entry unload
# ---------------------------------------------------------------------------


def _make_config_entry(entry_id: str):
    """Minimalny ConfigEntry double — async_unload_entry używa tylko .entry_id."""
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


@pytest.mark.asyncio
async def test_service_survives_first_of_two_unloads_removed_on_last():
    """Serwis domenowy przeżywa unload jednego z dwóch entry i jest usuwany
    dopiero przy ostatnim (hass.data[DOMAIN] pusty)."""
    from custom_components.volcast import async_unload_entry
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    hass = FakeHass()
    # Stub config_entries.async_unload_platforms → True (platformy odłączone OK).
    hass.config_entries = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    # Dwa entry z reconcilerem; tracker stubowany (async_stop musi być awaitable).
    tracker1, tracker2 = MagicMock(), MagicMock()
    tracker1.async_stop = AsyncMock()
    tracker2.async_stop = AsyncMock()
    hass.data[DOMAIN] = {
        "entry1": {"reconciler": MagicMock(), "tracker": tracker1},
        "entry2": {"reconciler": MagicMock(), "tracker": tracker2},
    }
    # Serwis zarejestrowany (jak po async_setup_entry).
    _register(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)

    # Pierwszy unload — serwis MUSI przetrwać (drugi entry wciąż aktywny).
    assert await async_unload_entry(hass, _make_config_entry("entry1")) is True
    tracker1.async_stop.assert_awaited_once()
    assert hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)
    assert "entry1" not in hass.data[DOMAIN]

    # Drugi (ostatni) unload — teraz serwis znika.
    assert await async_unload_entry(hass, _make_config_entry("entry2")) is True
    tracker2.async_stop.assert_awaited_once()
    assert not hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)
    assert hass.data[DOMAIN] == {}


@pytest.mark.asyncio
async def test_unload_platforms_failure_keeps_service_and_entry():
    """Gdy async_unload_platforms zwróci False, entry nie jest zdejmowane
    i serwis pozostaje (unload nieudany → brak side-effectów)."""
    from custom_components.volcast import async_unload_entry
    from custom_components.volcast.const import DOMAIN, SERVICE_SYNC_PRODUCTION

    hass = FakeHass()
    hass.config_entries = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

    tracker = MagicMock()
    tracker.async_stop = AsyncMock()
    hass.data[DOMAIN] = {"entry1": {"reconciler": MagicMock(), "tracker": tracker}}
    _register(hass)

    assert await async_unload_entry(hass, _make_config_entry("entry1")) is False
    # Nic nie zdjęte: entry zostaje, tracker nie zatrzymany, serwis żyje.
    assert "entry1" in hass.data[DOMAIN]
    tracker.async_stop.assert_not_awaited()
    assert hass.services.has_service(DOMAIN, SERVICE_SYNC_PRODUCTION)


# ── volcast.propose_sensor_assignment (tylko odczyt, z odpowiedzią) ───────────

def _assignment_world(monkeypatch, entries, *, controls=None):
    """hass z wpisami i składaniem sterowania; propozycja podmieniona na stałą (kandydat `pv_power_w`)."""
    from types import SimpleNamespace

    import tests.test_options_assignment as oa
    from custom_components.volcast import config_flow as cf
    from custom_components.volcast.const import DOMAIN

    async def propose(hass, entry):
        return oa._result(chosen={"pv_power_w": "sensor.pv_now"}, grid=oa.ON)
    monkeypatch.setattr(cf, "async_propose", propose)
    hass = FakeHass()
    hass.config_entries = SimpleNamespace(async_entries=lambda domain=None: list(entries))
    hass.data[DOMAIN] = {
        e.entry_id: {"control": (controls or {}).get(e.entry_id, SimpleNamespace(choice=None, direct=None))}
        for e in entries
    }
    return hass


def _entry(entry_id="e1", *, backend=True, mode=None, options=None):
    from types import SimpleNamespace

    data = {"api_key": "vk_x"}
    if backend:
        data["backend"] = {"x": 1}
    if mode:
        data["mode"] = mode
    return SimpleNamespace(entry_id=entry_id, domain="volcast", data=data, options=dict(options or {}))


def _propose_handler(hass):
    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN

    _async_register_services(hass)
    return hass.services.registered[(DOMAIN, "propose_sensor_assignment")]


def test_propose_service_registers_with_response_and_idempotently():
    from homeassistant.core import SupportsResponse

    from custom_components.volcast import _async_register_services
    from custom_components.volcast.const import DOMAIN

    hass = FakeHass()
    _async_register_services(hass)
    first = hass.services.registered[(DOMAIN, "propose_sensor_assignment")]
    assert hass.services.responses[(DOMAIN, "propose_sensor_assignment")] == SupportsResponse.ONLY
    schema = hass.services.schemas[(DOMAIN, "propose_sensor_assignment")]
    assert schema({}) == {} and schema({"config_entry_id": "e1"}) == {"config_entry_id": "e1"}
    _async_register_services(hass)
    assert hass.services.registered[(DOMAIN, "propose_sensor_assignment")] is first


@pytest.mark.asyncio
async def test_propose_returns_the_proposal_shape_and_changes_nothing(monkeypatch):
    import json

    entry = _entry(options={"telemetry_map": {"soc": "sensor.s"}})
    before = {"telemetry_map": {"soc": "sensor.s"}}
    hass = _assignment_world(monkeypatch, [entry])
    out = await _propose_handler(hass)(_FakeServiceCall())
    assert set(out) == {"applicable", "proposal", "telemetry_map", "flags", "sign_check", "basis", "samples",
                        "sources", "source_origin", "excluded", "flag_changes", "notes"}
    assert out["applicable"] is True
    assert out["proposal"]["chosen"] == {"pv_power_w": "sensor.pv_now"}
    json.dumps(out)                                    # czysty JSON
    assert entry.options == before
    assert hass.created_tasks == []


@pytest.mark.asyncio
async def test_propose_picks_the_only_paired_entry_and_the_given_one(monkeypatch):
    from custom_components.volcast import config_flow as cf

    seen = []
    a, b = _entry("a"), _entry("b")
    hass = _assignment_world(monkeypatch, [a, _entry("disc", mode="discovery_only"), _entry("unp", backend=False)])
    orig = cf.async_propose

    async def spy(h, e):
        seen.append(e.entry_id)
        return await orig(h, e)
    monkeypatch.setattr(cf, "async_propose", spy)
    await _propose_handler(hass)(_FakeServiceCall())
    hass2 = _assignment_world(monkeypatch, [a, b])
    monkeypatch.setattr(cf, "async_propose", spy)
    await _propose_handler(hass2)(_FakeServiceCall({"config_entry_id": "b"}))
    assert seen == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entries, data, message", [
    ([], {}, "No paired Volcast entry"),
    ([_entry(backend=False)], {}, "No paired Volcast entry"),
    ([_entry(mode="discovery_only")], {}, "No paired Volcast entry"),
    ([_entry("a"), _entry("b")], {}, "Several Volcast entries are paired"),
    ([_entry("a")], {"config_entry_id": "zzz"}, "Unknown Volcast entry"),
    ([_entry("a"), _entry("u", backend=False)], {"config_entry_id": "u"}, "is not paired"),
    ([_entry("a"), _entry("d", mode="discovery_only")], {"config_entry_id": "d"}, "is not paired"),
])
async def test_propose_validation_errors(monkeypatch, entries, data, message):
    from homeassistant.exceptions import ServiceValidationError

    hass = _assignment_world(monkeypatch, entries)
    with pytest.raises(ServiceValidationError, match=message):
        await _propose_handler(hass)(_FakeServiceCall(data))


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["profile", "direct", "none"])
async def test_propose_not_applicable_with_recognised_inverter_returns_empty_proposal(monkeypatch, control):
    from types import SimpleNamespace

    ctl = {"profile": SimpleNamespace(choice=SimpleNamespace(integration_domain="goodwe"), direct=None),
           "direct": SimpleNamespace(choice=None, direct=object()),
           "none": None}[control]
    entry = _entry(options={"telemetry_map": {"soc": "sensor.s"}})
    hass = _assignment_world(monkeypatch, [entry], controls={"e1": ctl})
    out = await _propose_handler(hass)(_FakeServiceCall())
    assert out["applicable"] is False
    assert out["proposal"] == {"fields": {}, "chosen": {}}
    assert "flags" in out and "sign_check" in out

