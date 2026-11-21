"""Opcja odwrócenia znaku baterii (`battery_power_negate`) nie zmienia, kiedy i co wtyczka pisze do falownika.

Odwrócenie dotyczy wyłącznie czujnika baterii wskazanego ręcznie w „Installation details” i działa tylko w
telemetrii wysyłanej do chmury. Pętla sterowania czyta baterię z encji profilu (albo z rejestrów), więc ten sam
scenariusz sterowania — złożony przez pełny `async_setup_control`, z prawdziwym wykonawcą i zapisami przez
usługi encji — musi dać identyczną sekwencję zapisów z opcją i bez niej. Ręczny czujnik raz jest TĄ SAMĄ encją
co bateria profilu (najgorszy przypadek: gdyby znak przeciekł do wykonawcy, zobaczyłby odwróconą baterię), raz
osobną encją.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.volcast.cloud.client import TelemetryResult
from custom_components.volcast.const import OPT_BATTERY_NEGATE, OPT_TELEMETRY_MAP
from custom_components.volcast.control import executor as ex_mod
from custom_components.volcast.control import runtime as rt_mod
from custom_components.volcast.core.slot import parse_schedule
from homeassistant.util import dt as dt_util

from .ha_fakes import GOODWE_ENTITIES as E, goodwe_hass
from .test_executor import E_LIVE, Clock, _idle_house
from .test_runtime import BACKEND, FakeCloud, _setup_hass

PROFILE_BATTERY = "sensor.goodwe_battery_power"
SEPARATE_BATTERY = "sensor.manual_battery_power"
ENTITIES = {"control_mode": "entities", "profile_id": "goodwe-et", "inverter_domain": "goodwe",
            "update_interval": 30}

SELL = {"mode": "discharge", "discharge_purpose": "sell", "power_w": 2000, "price_pln_kwh": 0.8}
CHARGE = {"mode": "charge", "power_w": 3000, "charge_source": "grid", "price_pln_kwh": 0.2}
CHARGE_PV_FIRST = {**CHARGE, "power_w": 1000}

# Cykl: (moc baterii w W z czujnika, PV w W, pobór w W). Ładowanie, rozładowanie, spoczynek.
MIXED_STEPS = [(1500, 0, 600), (-2500, 4000, 500), (0, 0, 0), (800, 200, 1200)]
# PV przed siecią (`core/control/pv_first.py`) całkuje ładowanie baterii (`max(0, −battery_w)`) do celu slotu:
# najpierw nadwyżka PV z ładowaniem 4 kW (faza `pv`, cel osiągnięty), potem nadwyżka znika. Z poprawnym znakiem
# slot zostaje w fazie `pv`; gdyby wykonawca widział odwróconą baterię, cel nie byłby osiągnięty i po
# `PV_FIRST_EXIT_S` wróciłby zapis ładowania z sieci — ten scenariusz wykrywa więc przeciek znaku do sterowania.
PV_FIRST_STEPS = [(-4000, 3000, 500)] * 8 + [(-4000, 0, 500)] * 8

# Scenariusze: lista (id planu, slot, minut przed teraz, minut po teraz, cykle, krok zegara w s).
# Krok 1200 s przepuszcza ograniczniki zmian kierunku (I-8); krok 60 s jest ciągłością dla PV przed siecią.
SCENARIOS = {
    "sell_then_charge": [("sell", SELL, 5, 55, MIXED_STEPS, 1200.0),
                         ("charge", CHARGE, 5, 55, MIXED_STEPS, 1200.0)],
    "pv_first": [("pv_first", CHARGE_PV_FIRST, 1, 19, PV_FIRST_STEPS, 60.0)],
}


class RecordingCloud(FakeCloud):
    sent: list[dict] = []

    async def async_post_telemetry(self, reading, persist=True):
        RecordingCloud.sent.append(reading)
        return TelemetryResult(200, None)


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan(sid: str, slot: dict, before_min: int, after_min: int) -> dict:
    # Wykonawca z `async_setup_control` liczy slot na prawdziwym zegarze UTC — slot obejmuje „teraz”.
    now = dt_util.utcnow().replace(microsecond=0)
    body = {"from": _iso(now - timedelta(minutes=before_min)), "to": _iso(now + timedelta(minutes=after_min)),
            **slot}
    return {"schedule_id": sid, "slots": [body], "fallback": {"mode": "self_consume", "soc_reserve": 10},
            "control_enabled": True}


async def _setup(monkeypatch, options: dict):
    """Pełny setup sterowania (tryb encji, goodwe-et) na atrapach; zwraca (hass, hass falownika, wpis, runtime,
    zegar wykonawcy) z włączoną zgodą i wyłącznikiem lokalnym."""
    from tests.setup_harness import FakeEntry, drain
    RecordingCloud.sent = []
    monkeypatch.setattr(rt_mod, "VolcastCloud", RecordingCloud)
    monkeypatch.setattr(rt_mod, "async_get_clientsession", lambda hass: None)
    monkeypatch.setattr(rt_mod, "map_entities",
                        lambda hass, c: {**E_LIVE, "battery_power_w": PROFILE_BATTERY}
                        if c and c.integration_domain else {})
    # Bramka weryfikacji otwarta na sztywno: test dotyczy zapisów planu. Weryfikacja (`verification._charge_w`)
    # czyta baterię z IO wykonawcy, nie z telemetrii — tu świadomie nieobjęta.
    monkeypatch.setattr(ex_mod, "control_verified", lambda *_: True)
    gh = _idle_house(goodwe_hass())
    hass = _setup_hass()
    hass.states, hass.services = gh.states, gh.services
    entry = FakeEntry(data={"api_key": "vk_" + "a" * 64, "backend": BACKEND}, options=options, entry_id="e1")
    rt = await rt_mod.async_setup_control(hass, entry, report=lambda: None)
    await drain(hass)
    ex = rt.executor
    # Zegar monotoniczny wykonawcy przesuwany o krok scenariusza na cykl (dostęp do pola prywatnego:
    # runtime nie przyjmuje zegara, a bez tego ograniczniki czasu trzymałyby każdy zapis poza pierwszym).
    clock = Clock()
    ex._clock = clock
    await ex.async_set_consent(True)
    await ex.async_set_local_switch(True)
    return hass, gh, entry, rt, clock


async def _cycle(gh, rt, clock, *, bat, pv, load, step_s, battery_eids) -> float | None:
    """Jeden cykl: stany czujników, tik wykonawcy, wysyłka telemetrii; zwraca wysłaną moc baterii."""
    for eid in battery_eids:
        gh.states.set(eid, str(bat), {"unit_of_measurement": "W"})
    gh.states.set(E_LIVE["pv_power_w"], str(pv), {"unit_of_measurement": "W"})
    gh.states.set(E_LIVE["load_power_w"], str(load), {"unit_of_measurement": "W"})
    clock.t += step_s
    await rt.executor.async_tick()
    before = len(RecordingCloud.sent)
    await rt.telemetry.async_flush()
    assert len(RecordingCloud.sent) == before + 1, "cykl bez wysłanej telemetrii"
    return RecordingCloud.sent[-1].get("battery_power_w")


async def _run(monkeypatch, *, manual_battery: str, negate: bool, scenario: str):
    """Jeden pełny przebieg: setup sterowania z opcjami wpisu, potem kolejne plany i cykle scenariusza.
    Zwraca (zapisy usług, decyzje wykonawcy, moce baterii wysłane w telemetrii)."""
    options = {**ENTITIES, OPT_TELEMETRY_MAP: {"battery_power_w": manual_battery}}
    if negate:
        options[OPT_BATTERY_NEGATE] = True
    hass, gh, _entry, rt, clock = await _setup(monkeypatch, options)
    try:
        ex = rt.executor
        gh.services.calls.clear()                      # tylko zapisy z cykli scenariusza
        decisions, battery_sent = [], []
        for sid, slot, before, after, steps, step_s in SCENARIOS[scenario]:
            raw = _plan(sid, slot, before, after)
            await ex.async_on_plan(raw, parse_schedule(raw))
            for bat, pv, load in steps:
                battery_sent.append(await _cycle(gh, rt, clock, bat=bat, pv=pv, load=load, step_s=step_s,
                                                 battery_eids={PROFILE_BATTERY, manual_battery}))
                d = ex.last_decision
                decisions.append((d.status, d.reason))
        writes = [(dom, svc, data) for dom, svc, data, _ctx in gh.services.calls]
        return writes, decisions, battery_sent
    finally:
        await rt_mod.async_unload_control(hass, rt)


def test_option_key_is_stable():
    # Klucz zapisany w opcjach wpisu (formularz „Installation details”) — zmiana zgubiłaby ustawienie.
    assert OPT_BATTERY_NEGATE == "battery_power_negate"


def _options(writes) -> list:
    return [data.get("option") for _dom, svc, data in writes if svc == "select_option"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", list(SCENARIOS))
@pytest.mark.parametrize("manual_battery", [PROFILE_BATTERY, SEPARATE_BATTERY], ids=["same_entity", "separate"])
async def test_battery_negate_never_changes_inverter_writes(monkeypatch, manual_battery, scenario):
    writes_off, decisions_off, sent_off = await _run(monkeypatch, manual_battery=manual_battery, negate=False,
                                                     scenario=scenario)
    writes_on, decisions_on, sent_on = await _run(monkeypatch, manual_battery=manual_battery, negate=True,
                                                  scenario=scenario)

    # Scenariusz naprawdę pisze do falownika przez usługi encji profilu — pusta lista niczego nie dowodzi.
    assert writes_off, "scenariusz bez zapisów niczego nie dowodzi"
    written = {data["entity_id"] for _dom, _svc, data in writes_off}
    assert E["mode"] in written and E["power_w"] in written
    assert any(status == "write" for status, _ in decisions_off)
    if scenario == "sell_then_charge":
        assert _options(writes_off) == ["sell_power", "charge_battery"]
        assert len(writes_off) >= 5                    # także zmiany mocy przy zmiennym PV i poborze
    else:
        # Z poprawnym znakiem cel ładowania osiągnięty w fazie `pv` trzyma ją do końca slotu: ostatni zapis to
        # tryb ładowania z PV, bez powrotu do ładowania z sieci (ten powrót byłby skutkiem odwróconej baterii).
        assert _options(writes_off) == ["charge_battery", "auto"]

    # Ta sama sekwencja zapisów i decyzji z opcją i bez niej.
    assert writes_on == writes_off
    assert decisions_on == decisions_off

    # Opcja działa (przekazana przez runtime z opcji wpisu) — ale tylko w telemetrii.
    steps = [bat for *_, cycles, _s in SCENARIOS[scenario] for bat, _pv, _load in cycles]
    assert sent_off == [float(b) for b in steps]
    assert sent_on == [-float(b) + 0.0 for b in steps]


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_on", [True, False], ids=["on", "off"])
async def test_toggling_battery_negate_applies_live_without_reload_or_writes(monkeypatch, turn_on):
    """Zmiana samej opcji znaku baterii przy działającym sterowaniu: bez przeładowania wpisu (rozładunek pisałby
    tryb neutralny, a nowy wykonawca stosowałby plan od nowa) i bez żadnego zapisu do falownika; kolejna
    telemetria ma już nowy znak."""
    import custom_components.volcast as integ
    options = {**ENTITIES, OPT_TELEMETRY_MAP: {"battery_power_w": SEPARATE_BATTERY}}
    if not turn_on:
        options[OPT_BATTERY_NEGATE] = True
    hass, gh, entry, rt, clock = await _setup(monkeypatch, options)
    reloads: list[str] = []

    async def reload(entry_id):
        reloads.append(entry_id)
    hass.config_entries.async_reload = reload
    try:
        raw = _plan("sell", SELL, 5, 55)
        await rt.executor.async_on_plan(raw, parse_schedule(raw))
        cycle = {"bat": 1500, "pv": 0, "load": 600, "step_s": 1200.0, "battery_eids": {SEPARATE_BATTERY}}
        before = await _cycle(gh, rt, clock, **cycle)
        assert gh.services.calls, "sterowanie musi już pisać, inaczej brak zapisów niczego nie dowodzi"
        assert before == (1500.0 if turn_on else -1500.0)
        gh.services.calls.clear()

        new = dict(entry.options)
        if turn_on:
            new[OPT_BATTERY_NEGATE] = True
        else:
            new.pop(OPT_BATTERY_NEGATE)
        entry.options = new
        await integ._async_update_listener(hass, entry)

        assert reloads == [] and gh.services.calls == []          # ani przeładowania, ani trybu neutralnego
        assert rt.options_at_setup == new                         # następna zmiana liczona od tego stanu
        after = await _cycle(gh, rt, clock, **cycle)
        assert after == (-1500.0 if turn_on else 1500.0)
        assert gh.services.calls == []                            # ten sam stan → kolejny cykl też bez zapisu
    finally:
        await rt_mod.async_unload_control(hass, rt)
