"""Propozycja przypisania czujników — warstwa HA: migawki, rekorder, ocena znaku. Tylko odczyt."""
import copy
import math
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.volcast import sensor_assignment as sa
from custom_components.volcast.core.discovery import sign_check as sc_mod

pytestmark = pytest.mark.asyncio

T0 = 1_790_000_000.0       # początek doby statystyk (znacznik czasu UTC)
STEP = 300.0               # statystyki 5-minutowe
N = 288                    # doba


# ── atrapy ──────────────────────────────────────────────────────────────────

def _ent(entity_id, *, platform="modbus", device_class="power", unit="W", disabled_by=None,
         device_id="inv", state_class="measurement", name=None):
    return SimpleNamespace(
        entity_id=entity_id, platform=platform, unique_id=f"u-{entity_id}", device_id=device_id,
        config_entry_id="m1", device_class=None, original_device_class=device_class,
        unit_of_measurement=unit, translation_key=None, original_name=name, disabled_by=disabled_by,
        capabilities={"state_class": state_class} if state_class else None)


def _st(value, unit="W", device_class="power", state_class="measurement", **extra):
    attrs = {"unit_of_measurement": unit, "device_class": device_class, "state_class": state_class}
    attrs.update(extra)
    return (str(value), {k: v for k, v in attrs.items() if v is not None})


class _States:
    """hass.states z `get` i `async_all(domena)` (jak w HA)."""

    def __init__(self, states):
        self._s = {eid: types.SimpleNamespace(entity_id=eid, state=v[0], attributes=dict(v[1]))
                   for eid, v in states.items()}

    def get(self, entity_id):
        return self._s.get(entity_id)

    def async_all(self, domain_filter=None):
        return [s for eid, s in self._s.items()
                if domain_filter is None or eid.split(".", 1)[0] == domain_filter]


def _hass(make_hass, *, entities=(), states=None, components=("recorder",)):
    hass = make_hass(devices=[SimpleNamespace(
        id="inv", manufacturer="Acme", model="X", name="Inverter", sw_version="1", hw_version=None,
        serial_number=None, identifiers={("modbus", "inv")}, config_entries={"m1"}, disabled_by=None)],
        entities=list(entities), entries=[], components=set(components))
    hass.states = _States(states or {})
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _entry(options=None):
    return SimpleNamespace(entry_id="v1", domain="volcast", data={}, options=dict(options or {}))


# typowa instalacja: PV w kW, sieć, bateria i zużycie w W
PV, GRID, BAT, LOAD = "sensor.pv_power", "sensor.grid_power", "sensor.battery_power", "sensor.load_power"


def _four_entities(load_platform="modbus"):
    return [_ent(PV, unit="kW"), _ent(GRID), _ent(BAT), _ent(LOAD, platform=load_platform)]


def _four_states():
    return {PV: _st(2.5, unit="kW"), GRID: _st(-300), BAT: _st(-1500), LOAD: _st(700)}


def _day():
    """Doba w konwencji Volcast (sieć + = pobór, bateria + = rozładowanie); zwraca serie w W."""
    pv, grid, bat, load = [], [], [], []
    for i in range(N):
        p = max(0.0, 5000.0 * math.sin(math.pi * (i - 72) / 144)) if 72 <= i <= 216 else 0.0
        l = 600.0 + 300.0 * ((i * 7) % 5)
        if p > 3000:
            b = -2000.0                       # ładowanie w południe
        elif p == 0 and i % 3 == 0:
            b = 500.0                         # rozładowanie nocą
        else:
            b = 0.0
        pv.append(p); bat.append(b); load.append(l); grid.append(l - p - b)
    return pv, grid, bat, load


def _rows(values, scale=1.0, skip=()):
    return [{"start": T0 + i * STEP, "mean": v / scale} for i, v in enumerate(values) if i not in skip]


def _stats_rows(*, grid_inverted=True, drop=()):
    """Statystyki surowych czujników: PV w kW, sieć z odwróconym znakiem (czujnik + = oddawanie)."""
    pv, grid, bat, load = _day()
    # zapytanie żąda `units={"power": "W"}`, więc HA oddaje moc w W także dla encji w kW
    rows = {PV: _rows(pv, skip={5}),                          # brak jednej próbki — wyrównanie po czasie
            GRID: _rows([-g for g in grid] if grid_inverted else grid),
            BAT: _rows(bat), LOAD: _rows(load)}
    for eid in drop:
        rows.pop(eid)
    return rows


# ── historia z rejestratora ─────────────────────────────────────────────────

async def test_history_mixed_units_gives_recommendation_from_raw_sensors(make_hass):
    hass = _hass(make_hass, entities=_four_entities(), states=_four_states())
    # mapa: PV i sieć zapisane; bateria i zużycie z kandydatów pewnych. Bieżące flagi celowo
    # „złe” — nie mogą trafić do próbek (ocena dotyczy surowych czujników)
    entry = _entry({"telemetry_map": {"pv_power_w": PV, "grid_power_w": GRID},
                    "grid_power_negate": None, "battery_power_negate": True})
    stats = MagicMock(return_value=_stats_rows())
    seen = []
    real = sc_mod.check_signs

    def spy(samples, **kw):
        samples = list(samples)
        seen.append(samples)
        return real(samples, **kw)

    with patch.object(sa, "statistics_during_period", stats), patch.object(sa, "check_signs", side_effect=spy):
        res = await sa.async_propose(hass, entry)

    # jedno wywołanie rejestratora dla wszystkich encji
    assert stats.call_count == 1
    args = stats.call_args.args
    assert args[0] is hass
    assert abs((sa.dt_util.utcnow() - args[1]).total_seconds() - 24 * 3600) < 1
    assert args[2] is None and args[3] == {PV, GRID, BAT, LOAD}
    assert args[4] == "5minute" and args[5] == {"power": "W"} and args[6] == {"mean"}
    # PV encji w kW: wartości z rejestratora już w W — bez mnożenia ×1000
    pv_max = max(s["pv_power_w"] for s in seen[0] if s["pv_power_w"] is not None)
    assert 4900 < pv_max <= 5000

    assert res.basis == sa.BASIS_HISTORY
    assert res.samples == N
    assert res.sign_check.basis == sc_mod.BASIS_BALANCE
    assert res.sign_check.samples == N - 1                   # próbka bez PV odpada
    assert res.sign_check.grid.negate is True
    assert res.sign_check.battery.negate is False
    assert res.sources == {"pv_power_w": PV, "grid_power_w": GRID, "battery_power_w": BAT,
                           "load_power_w": LOAD}
    assert res.source_origin == {"pv_power_w": "map", "grid_power_w": "map",
                                 "battery_power_w": "proposal", "load_power_w": "proposal"}
    assert res.telemetry_map == {"pv_power_w": PV, "grid_power_w": GRID}
    assert res.grid_negate is False and res.battery_negate is True
    assert res.flag_changes() == {"grid_power_negate": True, "battery_power_negate": False}
    assert res.notes == ()


async def test_as_dict_is_plain_data_for_service_and_form(make_hass):
    hass = _hass(make_hass, entities=_four_entities(), states=_four_states())
    with patch.object(sa, "statistics_during_period", MagicMock(return_value=_stats_rows())):
        res = await sa.async_propose(hass, _entry())
    d = res.as_dict()
    assert set(d) == {"proposal", "telemetry_map", "flags", "sign_check", "basis", "samples",
                      "sources", "source_origin", "excluded", "flag_changes", "notes"}
    assert d["flags"] == {"grid_power_negate": False, "battery_power_negate": False}
    assert d["proposal"]["chosen"]["battery_power_w"] == BAT
    assert d["sign_check"]["grid"]["negate"] is True
    assert d["basis"] == "history" and d["notes"] == []
    assert d["flag_changes"] == {"grid_power_negate": True}
    import json
    json.dumps(d)                                             # odpowiedź usługi = czyste dane


async def test_recorder_not_loaded_falls_back_to_current_states(make_hass):
    hass = _hass(make_hass, entities=_four_entities(), states=_four_states(), components=())
    stats = MagicMock(return_value=_stats_rows())
    with patch.object(sa, "statistics_during_period", stats):
        res = await sa.async_propose(hass, _entry())
    stats.assert_not_called()
    assert res.basis == sa.BASIS_CURRENT and res.samples == 1
    assert sa.NOTE_RECORDER_UNAVAILABLE in res.notes
    # jedna próbka z bieżących stanów (kW przeliczone) — uczciwie bez zalecenia
    assert res.sign_check.samples == 1
    assert res.sign_check.grid.negate is None
    assert res.sign_check.grid.reason == sc_mod.TOO_FEW_SAMPLES


async def test_current_sample_converts_units_to_watts(make_hass):
    hass = _hass(make_hass, entities=_four_entities(), states=_four_states(), components=())
    seen = []
    real = sc_mod.check_signs

    def spy(samples, **kw):
        samples = list(samples)
        seen.append((samples, kw))
        return real(samples, **kw)

    with patch.object(sa, "check_signs", side_effect=spy):
        await sa.async_propose(hass, _entry())
    (samples, kw), = seen
    assert kw == {}                                           # bez podpowiedzi o baterii
    assert samples == [{"pv_power_w": 2500.0, "grid_power_w": -300.0, "battery_power_w": -1500.0,
                        "load_power_w": 700.0}]


async def test_entity_without_statistics_falls_back_to_current(make_hass):
    hass = _hass(make_hass, entities=_four_entities(), states=_four_states())
    with patch.object(sa, "statistics_during_period", MagicMock(return_value=_stats_rows(drop=(BAT,)))):
        res = await sa.async_propose(hass, _entry())
    assert res.basis == sa.BASIS_CURRENT and res.samples == 1
    assert sa.NOTE_NO_STATISTICS in res.notes


async def test_recorder_exception_does_not_escape(make_hass, caplog):
    hass = _hass(make_hass, entities=_four_entities(), states=_four_states())
    boom = MagicMock(side_effect=OSError("db locked at /config/home-assistant_v2.db"))
    with caplog.at_level("DEBUG", logger=sa.__name__), patch.object(sa, "statistics_during_period", boom):
        res = await sa.async_propose(hass, _entry())
    assert res.basis == sa.BASIS_CURRENT and res.samples == 1
    assert sa.NOTE_RECORDER_ERROR in res.notes
    assert "OSError" in caplog.text
    assert "/config" not in caplog.text                       # bez treści wyjątku (ścieżki, dane)


async def test_unexpected_failure_returns_empty_result(make_hass):
    hass = _hass(make_hass, entities=_four_entities(), states=_four_states())
    with patch.object(sa, "propose", side_effect=RuntimeError("bug")):
        res = await sa.async_propose(hass, _entry({"telemetry_map": {"pv_power_w": PV}}))
    assert res.basis == sa.BASIS_NONE and res.samples == 0
    assert sa.NOTE_FAILED in res.notes
    assert res.telemetry_map == {"pv_power_w": PV}
    assert res.proposal.chosen() == {}


async def test_nothing_to_judge_skips_recorder(make_hass):
    # tylko PV — bez sieci i baterii nie ma znaku do oceny
    hass = _hass(make_hass, entities=[_ent(PV, unit="kW")], states={PV: _st(1.0, unit="kW")})
    stats = MagicMock(return_value={})
    with patch.object(sa, "statistics_during_period", stats):
        res = await sa.async_propose(hass, _entry())
    stats.assert_not_called()
    assert res.basis == sa.BASIS_NONE and res.samples == 0
    assert res.sign_check.grid.negate is None and res.sign_check.battery.negate is None


# ── encje tylko ze stanów ───────────────────────────────────────────────────

async def test_state_only_sensor_reaches_proposal(make_hass):
    # czujnik szablonowy YAML bez unique_id: jest w maszynie stanów, nie ma go w rejestrze
    hass = _hass(make_hass, entities=[], states={
        "sensor.solar_production_power": _st(1200, friendly_name="Solar production power"),
        "sensor.outdoor_temperature": _st(5, unit="°C", device_class="temperature")})
    with patch.object(sa, "statistics_during_period", MagicMock(return_value={})):
        res = await sa.async_propose(hass, _entry())
    pv = res.proposal.field("pv_power_w")
    assert [c.entity_id for c in pv.candidates] == ["sensor.solar_production_power"]
    assert pv.chosen == "sensor.solar_production_power"
    assert res.sources == {"pv_power_w": "sensor.solar_production_power"}


async def test_registry_sensor_not_duplicated_from_states(make_hass):
    hass = _hass(make_hass, entities=[_ent(PV)], states={PV: _st(100)})
    with patch.object(sa, "statistics_during_period", MagicMock(return_value={})):
        res = await sa.async_propose(hass, _entry())
    assert [c.entity_id for c in res.proposal.field("pv_power_w").candidates] == [PV]


# ── zużycie wyliczane z pozostałych ─────────────────────────────────────────

@pytest.mark.parametrize("platform", ["template", "min_max", "group"])
async def test_derived_load_sensor_is_not_used_in_balance(make_hass, platform):
    hass = _hass(make_hass, entities=_four_entities(load_platform=platform), states=_four_states())
    stats = MagicMock(return_value=_stats_rows())
    seen = []
    real = sc_mod.check_signs

    def spy(samples, **kw):
        samples = list(samples)
        seen.append(samples)
        return real(samples, **kw)

    with patch.object(sa, "statistics_during_period", stats), patch.object(sa, "check_signs", side_effect=spy):
        res = await sa.async_propose(hass, _entry({"telemetry_map": {"load_power_w": LOAD}}))
    assert res.excluded == {"load_power_w": sa.EXCLUDED_LOAD_DERIVED}
    assert "load_power_w" not in res.sources
    assert stats.call_args.args[3] == {PV, GRID, BAT}
    assert all("load_power_w" not in s for s in seen[0])
    assert res.sign_check.grid.negate is None and res.sign_check.battery.negate is None


async def test_state_only_load_sensor_is_not_used_in_balance(make_hass):
    states = _four_states()
    hass = _hass(make_hass, entities=_four_entities()[:3], states=states)   # zużycie spoza rejestru
    with patch.object(sa, "statistics_during_period", MagicMock(return_value=_stats_rows())):
        res = await sa.async_propose(hass, _entry({"telemetry_map": {"load_power_w": LOAD}}))
    assert res.excluded == {"load_power_w": sa.EXCLUDED_LOAD_STATE_ONLY}
    assert res.sign_check.grid.negate is None and res.sign_check.battery.negate is None


async def test_measured_load_sensor_is_used(make_hass):
    hass = _hass(make_hass, entities=_four_entities(load_platform="utility_meter"), states=_four_states())
    with patch.object(sa, "statistics_during_period", MagicMock(return_value=_stats_rows())):
        res = await sa.async_propose(hass, _entry())
    assert res.excluded == {}
    assert res.sources["load_power_w"] == LOAD


# ── rejestr niedostępny ─────────────────────────────────────────────────────

async def test_registry_unavailable_load_is_not_trusted(make_hass):
    hass = _hass(make_hass, entities=[], states=_four_states())
    with patch.object(sa.er, "async_get", side_effect=RuntimeError("registry")), \
         patch.object(sa, "statistics_during_period", MagicMock(return_value=_stats_rows())):
        res = await sa.async_propose(hass, _entry({"telemetry_map": {"load_power_w": LOAD}}))
    assert res.excluded == {"load_power_w": sa.EXCLUDED_LOAD_UNVERIFIED}
    assert sa.NOTE_REGISTRY_UNAVAILABLE in res.notes
    assert res.sign_check.grid.negate is None and res.sign_check.battery.negate is None


# ── tylko odczyt ────────────────────────────────────────────────────────────

async def test_entry_options_unchanged_and_nothing_written(make_hass):
    hass = _hass(make_hass, entities=_four_entities(), states=_four_states())
    hass.services = MagicMock()
    options = {"telemetry_map": {"pv_power_w": PV, "grid_power_w": GRID}, "grid_power_negate": True,
               "battery_power_negate": None, "other": [1, 2]}
    entry = _entry(options)
    before = copy.deepcopy(entry.options)
    with patch.object(sa, "statistics_during_period", MagicMock(return_value=_stats_rows())):
        res = await sa.async_propose(hass, entry)
    assert entry.options == before
    hass.config_entries.async_update_entry.assert_not_called()
    assert not hass.services.method_calls
    # wynik nie dzieli obiektów z opcjami wpisu
    res.telemetry_map["pv_power_w"] = "sensor.x"
    assert entry.options == before
