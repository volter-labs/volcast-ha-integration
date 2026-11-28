"""Zgłoszenie naprawy prognozy powstaje zawsze, gdy brak czujników produkcji.

Waga zgłoszenia pochodzi z rejestru zgłoszeń (`issue_registry.IssueSeverity`) —
komponent `repairs` jej nie re-eksportuje, więc import stamtąd po cichu wyłączał zgłoszenie.
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from .setup_harness import run_setup


@pytest.fixture(autouse=True)
def _no_network_probe(monkeypatch):
    # Wykrywanie w teście nie wysyła pakietów w sieć (jak w pozostałych testach setupu).
    from unittest.mock import AsyncMock

    from custom_components.volcast import discovery_runner
    monkeypatch.setattr(discovery_runner, "probe_udp_48899", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_forecast_entry_without_production_sensors_raises_repair_issue(monkeypatch):
    ir = sys.modules["homeassistant.helpers.issue_registry"]
    create = MagicMock()
    monkeypatch.setattr(ir, "async_create_issue", create)
    hass, entry, ok = await run_setup(monkeypatch.setattr)
    assert ok
    issues = [c for c in create.call_args_list if c.args[2] == "production_tracking_available"]
    assert len(issues) == 1
    kw = issues[0].kwargs
    assert kw["translation_key"] == "production_tracking_available" and kw["is_fixable"] is False
    # Atrapa rejestru nie ma enuma wag — wtedy wartość tekstowa, jak w prawdziwym HA (StrEnum).
    assert kw["severity"] == "warning"


@pytest.mark.asyncio
async def test_severity_comes_from_issue_registry_when_available(monkeypatch):
    import custom_components.volcast as integ

    ir = sys.modules["homeassistant.helpers.issue_registry"]
    create = MagicMock()
    monkeypatch.setattr(ir, "async_create_issue", create)
    marker = object()
    monkeypatch.setattr(integ, "_ISSUE_WARNING", marker)
    hass, entry, ok = await run_setup(monkeypatch.setattr)
    assert ok
    kw = next(c.kwargs for c in create.call_args_list if c.args[2] == "production_tracking_available")
    assert kw["severity"] is marker


def test_integration_does_not_import_severity_from_repairs():
    from pathlib import Path

    src = (Path(__file__).parents[1] / "custom_components" / "volcast" / "__init__.py").read_text(encoding="utf-8")
    assert "components.repairs import" not in src


# --- Mapowanie czujników PV nieużywane przy sterowaniu falownikiem ---------------------------

_MAPPED = {"pv_energy_entity": "sensor.pv_e", "pv_power_entity": "sensor.pv_p"}


def _unused_calls(mock):
    return [c for c in mock.call_args_list if c.args[2].startswith("production_mapping_unused_")]


def _patch_issues(monkeypatch):
    ir = sys.modules["homeassistant.helpers.issue_registry"]
    create, delete = MagicMock(), MagicMock()
    monkeypatch.setattr(ir, "async_create_issue", create)
    monkeypatch.setattr(ir, "async_delete_issue", delete)
    return create, delete


def _paired(monkeypatch, *, carries: bool):
    import custom_components.volcast as integ
    from .test_setup_paired import fake_runtime

    async def setup_control(hass, entry, *, report):
        rt = fake_runtime()
        if carries:
            # Nadajnik telemetrii z mocą PV, siecią i zużyciem — jedyne źródło odpowiedzi „niesie produkcję".
            from custom_components.volcast.control.telemetry import TelemetrySender
            rt.telemetry = TelemetrySender(None, SimpleNamespace(entry_id="e1", options={}), None, None,
                                           choice=None, profile_map={}, grid_negate=False, limits=None,
                                           manual_map={"pv_power_w": "sensor.pv_power", "grid_power_w": "sensor.g",
                                                       "load_power_w": "sensor.l"})
        return rt
    monkeypatch.setattr(integ, "async_setup_control", setup_control)


@pytest.mark.asyncio
async def test_paired_with_mapping_raises_mapping_unused_issue(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=True)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, options=_MAPPED)
    assert ok
    calls = _unused_calls(create)
    assert len(calls) == 1
    assert calls[0].args[2] == f"production_mapping_unused_{entry.entry_id}"
    kw = calls[0].kwargs
    assert kw["translation_key"] == "production_mapping_unused" and kw["is_fixable"] is False
    assert kw["severity"] == "warning"
    assert kw["translation_placeholders"] == {"entities": "sensor.pv_e, sensor.pv_p"}


@pytest.mark.asyncio
async def test_paired_without_mapping_has_no_mapping_unused_issue(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=True)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and not _unused_calls(create)
    assert any(c.args[2] == f"production_mapping_unused_{entry.entry_id}" for c in delete.call_args_list)


@pytest.mark.asyncio
async def test_forecast_only_with_mapping_has_no_mapping_unused_issue(monkeypatch):
    create, delete = _patch_issues(monkeypatch)
    hass, entry, ok = await run_setup(monkeypatch.setattr, options=_MAPPED)
    assert ok and not _unused_calls(create)


@pytest.mark.asyncio
async def test_paired_without_production_telemetry_keeps_tracker_and_no_issue(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=False)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, options=_MAPPED)
    assert ok and not _unused_calls(create)


@pytest.mark.asyncio
async def test_unload_deletes_mapping_unused_issue(monkeypatch):
    import custom_components.volcast as integ
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired(monkeypatch, carries=True)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, options=_MAPPED)
    monkeypatch.setattr(integ, "async_unload_control", AsyncMock())
    delete.reset_mock()
    assert await integ.async_unload_entry(hass, entry) is True
    assert any(c.args[2] == f"production_mapping_unused_{entry.entry_id}" for c in delete.call_args_list)


# --- Przypisanie czujników: zgłoszenie „brak mapy" przy niepoznanym falowniku ------------------

def _assignment_calls(mock):
    return [c for c in mock.call_args_list if c.args[2].startswith("sensor_assignment_missing_")]


def _paired_with(monkeypatch, *, choice=None, control=True):
    import custom_components.volcast as integ
    from .test_setup_paired import fake_runtime

    async def setup_control(hass, entry, *, report):
        if not control:
            return None
        rt = fake_runtime()
        rt.choice = choice
        return rt
    monkeypatch.setattr(integ, "async_setup_control", setup_control)


@pytest.mark.asyncio
async def test_paired_without_profile_and_pv_power_raises_assignment_issue(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_with(monkeypatch)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok
    calls = _assignment_calls(create)
    assert len(calls) == 1
    assert calls[0].args[2] == f"sensor_assignment_missing_{entry.entry_id}"
    kw = calls[0].kwargs
    assert kw["translation_key"] == "sensor_assignment_missing" and kw["is_fixable"] is True
    assert kw["severity"] == "warning" and kw["data"] == {"entry_id": entry.entry_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"telemetry_map": {"pv_power_w": "sensor.pv"}},                      # moc PV przypisana (także bez baterii)
    {"direct_target": {"host": "192.0.2.5", "port": 502}},               # połączenie bezpośrednie
])
async def test_assignment_issue_absent_when_pv_power_or_direct_connection_is_set(monkeypatch, options):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_with(monkeypatch)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, options=options)
    assert ok and not _assignment_calls(create)
    assert any(c.args[2] == f"sensor_assignment_missing_{entry.entry_id}" for c in delete.call_args_list)


@pytest.mark.asyncio
async def test_assignment_issue_absent_for_recognised_inverter_integration(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_with(monkeypatch, choice=SimpleNamespace(integration_domain="goodwe"))
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and not _assignment_calls(create)
    assert any(c.args[2] == f"sensor_assignment_missing_{entry.entry_id}" for c in delete.call_args_list)


@pytest.mark.asyncio
async def test_assignment_issue_absent_for_api_key_entry_and_discovery_only(monkeypatch):
    create, delete = _patch_issues(monkeypatch)
    hass, entry, ok = await run_setup(monkeypatch.setattr)
    assert ok and not _assignment_calls(create)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data={"mode": "discovery_only"})
    assert ok and not _assignment_calls(create)


@pytest.mark.asyncio
async def test_assignment_issue_not_raised_when_control_did_not_come_up(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_with(monkeypatch, control=False)      # nie wiadomo, czy falownik rozpoznano
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and not _assignment_calls(create)


@pytest.mark.asyncio
async def test_assignment_issue_goes_away_on_unload_and_removal(monkeypatch):
    import custom_components.volcast as integ
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_with(monkeypatch)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    issue = f"sensor_assignment_missing_{entry.entry_id}"
    monkeypatch.setattr(integ, "async_unload_control", AsyncMock())
    delete.reset_mock()
    assert await integ.async_unload_entry(hass, entry) is True
    assert any(c.args[2] == issue for c in delete.call_args_list)
    delete.reset_mock()
    monkeypatch.setattr(integ, "async_remove_control", AsyncMock())
    await integ.async_remove_entry(hass, entry)
    assert any(c.args[2] == issue for c in delete.call_args_list)


@pytest.mark.asyncio
async def test_assignment_issue_clears_once_the_map_has_pv_power(monkeypatch):
    # Zapis mapy przeładowuje wpis — ponowny setup z mapą kasuje zgłoszenie.
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_with(monkeypatch)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert len(_assignment_calls(create)) == 1
    delete.reset_mock()
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED,
                                      options={"telemetry_map": {"pv_power_w": "sensor.pv"}})
    assert ok and any(c.args[2].startswith("sensor_assignment_missing_") for c in delete.call_args_list)


# --- Rozpoznany falownik bez kanału sieci albo zużycia domu -----------------------------------

def _choice(profile_id: str, domain: str):
    from custom_components.volcast.core.control.select import ProfileChoice
    from custom_components.volcast.core.profile import load_builtin
    return ProfileChoice(load_builtin(profile_id), domain, "X")


_GW = _choice("goodwe-et", "goodwe")
_HUAWEI = _choice("huawei-sun2000", "huawei_solar")


def _channels_calls(mock):
    return [c for c in mock.call_args_list if c.args[2].startswith("telemetry_channels_missing_")]


def _paired_reading(monkeypatch, *, profile_map, choice=_GW, manual=None, direct=None):
    """Sterowanie z rozpoznanym falownikiem i prawdziwym nadajnikiem telemetrii czytającym `profile_map`."""
    import custom_components.volcast as integ
    from custom_components.volcast.control.telemetry import TelemetrySender
    from .test_setup_paired import fake_runtime

    async def setup_control(hass, entry, *, report):
        rt = fake_runtime()
        rt.choice = choice
        rt.direct = direct
        rt.telemetry = TelemetrySender(None, SimpleNamespace(entry_id="e1", options={}), None, None,
                                       choice=choice, profile_map=profile_map, grid_negate=False, limits=None,
                                       manual_map=manual or {})
        return rt
    monkeypatch.setattr(integ, "async_setup_control", setup_control)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_map", [
    {"pv_power_w": "sensor.pv"},                                         # ani sieci, ani zużycia (Huawei bez miernika)
    {"pv_power_w": "sensor.pv", "grid_power_w": "sensor.grid"},          # bez zużycia domu
    {"pv_power_w": "sensor.pv", "load_power_w": "sensor.load"},          # bez sieci
])
async def test_recognised_inverter_without_grid_or_load_raises_fixable_issue(monkeypatch, profile_map):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_reading(monkeypatch, profile_map=profile_map)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok
    calls = _channels_calls(create)
    assert len(calls) == 1
    assert calls[0].args[2] == f"telemetry_channels_missing_{entry.entry_id}"
    kw = calls[0].kwargs
    assert kw["translation_key"] == "telemetry_channels_missing" and kw["is_fixable"] is True
    assert kw["severity"] == "warning" and kw["data"] == {"entry_id": entry.entry_id}
    assert not _assignment_calls(create)                       # profil rozpoznany: bez naprawy „wskaż czujniki”


@pytest.mark.asyncio
async def test_huawei_without_house_load_entity_raises_no_issue_when_the_meter_is_read(monkeypatch):
    # Integracja Huawei nie ma encji zużycia domu (profil: `null`) — nie zgłaszamy czegoś, czego nie da się wskazać.
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_reading(monkeypatch, choice=_HUAWEI,
                    profile_map={"pv_power_w": "sensor.hw_in", "grid_power_w": "sensor.hw_meter"})
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, options=_MAPPED)
    assert ok and not _channels_calls(create)
    # produkcja i tak zostaje na trackerze (serwer bez zużycia nie sprawdzi bilansu)
    assert hass.data["volcast"][entry.entry_id]["tracker"] is not None


@pytest.mark.asyncio
async def test_huawei_without_visible_meter_raises_the_issue_for_grid_only(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_reading(monkeypatch, choice=_HUAWEI, profile_map={"pv_power_w": "sensor.hw_in"})
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and len(_channels_calls(create)) == 1
    rt = hass.data["volcast"][entry.entry_id]["control"]
    assert rt.telemetry.missing_balance_channels == ("grid_power_w",)      # naprawa pokaże tylko sieć


@pytest.mark.asyncio
async def test_channels_issue_absent_when_grid_and_load_are_read(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_reading(monkeypatch, profile_map={"pv_power_w": "sensor.pv", "grid_power_w": "sensor.grid"},
                    manual={"load_power_w": "sensor.load"})    # brakujący kanał z mapy ręcznej
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and not _channels_calls(create)
    assert any(c.args[2] == f"telemetry_channels_missing_{entry.entry_id}" for c in delete.call_args_list)


@pytest.mark.asyncio
async def test_channels_issue_absent_without_recognised_inverter_or_reading_path(monkeypatch):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    # bez rozpoznanego falownika: od tego jest naprawa mapy czujników (wszystkie pola, także sieć i zużycie)
    _paired_reading(monkeypatch, choice=None, profile_map={}, manual={"pv_power_w": "sensor.pv"})
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and not _channels_calls(create)
    # rozpoznany falownik, ale telemetria nic nie czyta — to zgłoszenie „brak odczytów”, nie tej naprawy
    create.reset_mock()
    _paired_reading(monkeypatch, profile_map={})
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and not _channels_calls(create)
    # sterowanie nie wstało / wpis bez konta / tylko rozpoznanie
    create.reset_mock()
    _paired_with(monkeypatch, control=False)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and not _channels_calls(create)
    hass, entry, ok = await run_setup(monkeypatch.setattr)
    assert ok and not _channels_calls(create)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data={"mode": "discovery_only"})
    assert ok and not _channels_calls(create)


@pytest.mark.asyncio
async def test_channels_issue_survives_unload_and_goes_away_on_removal(monkeypatch):
    # Przeładowanie (rozładunek + setup) nie kasuje zgłoszenia — inaczej ginie „Ignoruj” użytkownika bez miernika.
    import custom_components.volcast as integ
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_reading(monkeypatch, profile_map={"pv_power_w": "sensor.pv"})
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    issue = f"telemetry_channels_missing_{entry.entry_id}"
    monkeypatch.setattr(integ, "async_unload_control", AsyncMock())
    delete.reset_mock()
    assert await integ.async_unload_entry(hass, entry) is True
    assert not any(c.args[2] == issue for c in delete.call_args_list)
    delete.reset_mock()
    monkeypatch.setattr(integ, "async_remove_control", AsyncMock())
    await integ.async_remove_entry(hass, entry)
    assert any(c.args[2] == issue for c in delete.call_args_list)


@pytest.mark.asyncio
async def test_channels_issue_absent_while_a_feed_profile_waits_for_reload(monkeypatch):
    # Profil z feedu przyszedł po starcie (`announced_choice`): do przeładowania wybór to None, a mapa ręczna ma
    # samą moc PV — kanały sieci i zużycia może dać dopiero profil, więc bez zgłoszenia obok „profil gotowy”.
    import custom_components.volcast as integ
    from custom_components.volcast.control.telemetry import TelemetrySender
    from .test_setup_paired import PAIRED, fake_runtime
    create, delete = _patch_issues(monkeypatch)

    async def setup_control(hass, entry, *, report):
        rt = fake_runtime()
        rt.announced_choice = ("goodwe-et", "goodwe")
        rt.telemetry = TelemetrySender(None, SimpleNamespace(entry_id="e1", options={}), None, None, choice=None,
                                       profile_map={}, grid_negate=False, limits=None,
                                       manual_map={"pv_power_w": "sensor.pv"})
        return rt
    monkeypatch.setattr(integ, "async_setup_control", setup_control)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok and not _channels_calls(create)


@pytest.mark.asyncio
async def test_channels_issue_survives_a_failing_issue_registry(monkeypatch):
    # Błąd rejestru zgłoszeń nie może wywrócić setupu (sterowanie już stoi).
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)

    def boom(hass, domain, issue_id, **kw):
        if issue_id.startswith("telemetry_channels_missing_"):
            raise RuntimeError("registry")
    create.side_effect = boom
    _paired_reading(monkeypatch, profile_map={"pv_power_w": "sensor.pv"})
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED)
    assert ok


# --- Produkcja: tracker wtyczki zostaje, gdy serwer nie przyjmie produkcji z telemetrii ---------

@pytest.mark.asyncio
@pytest.mark.parametrize("profile_map, tracker_on", [
    ({"pv_power_w": "sensor.pv"}, True),                                        # bez sieci i zużycia
    ({"pv_power_w": "sensor.pv", "grid_power_w": "sensor.grid"}, True),         # bez zużycia
    ({"pv_power_w": "sensor.pv", "grid_power_w": "sensor.grid", "load_power_w": "sensor.load"}, False),
])
async def test_tracker_stays_on_while_telemetry_lacks_a_balance_channel(monkeypatch, profile_map, tracker_on):
    from .test_setup_paired import PAIRED
    create, delete = _patch_issues(monkeypatch)
    _paired_reading(monkeypatch, profile_map=profile_map)
    hass, entry, ok = await run_setup(monkeypatch.setattr, data=PAIRED, options=_MAPPED)
    assert ok
    assert (hass.data["volcast"][entry.entry_id]["tracker"] is not None) is tracker_on
    # „zmapowane czujniki nieużywane” tylko wtedy, gdy produkcję naprawdę niesie telemetria
    assert bool(_unused_calls(create)) is not tracker_on
