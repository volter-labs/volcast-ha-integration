"""Nowy wybór profilu po aktualizacji feedu: ta sama naprawa „profil gotowy” co dla nowych profili feedu.

Wybór profilu zapada przy składaniu sterowania; profil „tylko encje” dla integracji, której nikt wcześniej nie
rozpoznał, przychodzi feedem PÓŹNIEJ (świeża instalacja przed pierwszym pobraniem feedu albo nowy profil).
"""
import asyncio
from types import SimpleNamespace

import pytest

from custom_components.volcast import coordinator as coord
from custom_components.volcast.const import ISSUE_BRAND_PROFILE_READY
from custom_components.volcast.control import runtime
from custom_components.volcast.core.control.select import ProfileChoice
from custom_components.volcast.core.profile import load_builtin, profile_from_dict
from tests.core.profile_fixtures import eo_profile

SA = "solar_assistant"
SA_ID = "solarassistant-generic"
SA_ISSUE = f"{ISSUE_BRAND_PROFILE_READY}_{SA_ID}"


def _sa_profile():
    raw = eo_profile()
    raw["id"], raw["label"] = SA_ID, "Solar Assistant"
    raw["ha"]["integrations"][0].update(domain=SA, model_regex=["(?i)\\bsolar ?assistant\\b"])
    return profile_from_dict(raw)


def _dev(i, manufacturer, model, entry):
    return SimpleNamespace(id=f"d{i}", manufacturer=manufacturer, model=model, name=None, sw_version=None,
                           config_entries={entry}, disabled_by=None, serial_number=None, identifiers=set())


class _Feed:
    def __init__(self):
        self.listeners = []

    def add_update_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)


class _Hass:
    def __init__(self, domains):
        self.config_entries = SimpleNamespace(
            async_get_entry=lambda ce_id: SimpleNamespace(domain=domains[ce_id]))
        self.tasks = []

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def async_create_background_task(self, coro, name):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


@pytest.fixture
def issues(monkeypatch):
    made: list[str] = []
    monkeypatch.setattr(coord.ir, "async_create_issue",
                        lambda hass, domain, issue_id, **kw: made.append((issue_id, kw["translation_placeholders"])))
    return made


def _setup(monkeypatch, devices, domains, *, choice, profiles_now, options=None):
    reg = SimpleNamespace(devices={d.id: d for d in devices})
    monkeypatch.setattr(runtime.dr, "async_get", lambda hass: reg)
    monkeypatch.setattr(runtime.ds, "load_profiles", lambda: list(profiles_now))
    hass, feed = _Hass(domains), _Feed()
    entry = SimpleNamespace(entry_id="v1", options=options or {})
    rt = runtime.ControlRuntime(None, None, None, None, choice, {}, None)
    runtime.watch_profile_choice(hass, entry, rt, feed)
    return hass, feed, rt


async def _feed_updated(hass, feed):
    for cb in list(feed.listeners):
        cb()
    await asyncio.gather(*hass.tasks)
    hass.tasks.clear()


@pytest.mark.asyncio
async def test_fresh_install_feed_after_setup_raises_profile_ready_once(monkeypatch, issues):
    # Złożenie przed pierwszym pobraniem feedu: brak wyboru. Feed przynosi profil — naprawa, raz.
    hass, feed, rt = _setup(monkeypatch, [_dev(1, "SolarAssistant", None, "sa")], {"sa": SA},
                            choice=None, profiles_now=[load_builtin("goodwe-et"), _sa_profile()])
    await _feed_updated(hass, feed)
    assert issues == [(SA_ISSUE, {"profile": SA_ID, "label": "Solar Assistant"})]
    await _feed_updated(hass, feed)            # ten sam zmieniony wynik — bez ponownej naprawy
    assert len(issues) == 1


@pytest.mark.asyncio
async def test_existing_install_read_only_choice_gets_entities_only_profile(monkeypatch, issues):
    # Działający wybór tylko do odczytu tego samego profilu → wybór z integracją to też zmiana wyniku.
    sa = _sa_profile()
    hass, feed, rt = _setup(monkeypatch, [_dev(1, "SolarAssistant", None, "sa")], {"sa": SA},
                            choice=ProfileChoice(sa, None, None), profiles_now=[sa])
    await _feed_updated(hass, feed)
    assert [i for i, _ in issues] == [SA_ISSUE]


@pytest.mark.asyncio
async def test_no_notice_when_the_choice_is_unchanged(monkeypatch, issues):
    sa = _sa_profile()
    hass, feed, rt = _setup(monkeypatch, [_dev(1, "SolarAssistant", None, "sa")], {"sa": SA},
                            choice=ProfileChoice(sa, SA, None), profiles_now=[sa])
    await _feed_updated(hass, feed)
    # dom z falownikiem z pakietu: profil „tylko encje” z feedu go nie przesłania — wybór bez zmian
    gw = load_builtin("goodwe-et")
    hass2, feed2, _ = _setup(monkeypatch, [_dev(1, "SolarAssistant", None, "sa"), _dev(2, "GoodWe", "GW10K-ET", "g")],
                             {"sa": SA, "g": "goodwe"}, choice=ProfileChoice(gw, "goodwe", "GW10K-ET"),
                             profiles_now=[gw, sa])
    await _feed_updated(hass2, feed2)
    # profil wycofany (nowy wynik: brak wyboru) — to nie jest „profil gotowy”
    hass3, feed3, _ = _setup(monkeypatch, [_dev(1, "SolarAssistant", None, "sa")], {"sa": SA},
                             choice=ProfileChoice(sa, SA, None), profiles_now=[])
    await _feed_updated(hass3, feed3)
    assert issues == []


@pytest.mark.asyncio
async def test_direct_mode_is_not_watched(monkeypatch, issues):
    hass, feed, rt = _setup(monkeypatch, [_dev(1, "SolarAssistant", None, "sa")], {"sa": SA}, choice=None,
                            profiles_now=[_sa_profile()],
                            options={"control_mode": "direct",
                                     "direct_target": {"host": "10.0.0.10", "profile_id": "goodwe-et",
                                                       "device_fp": "x"}})
    assert feed.listeners == []


@pytest.mark.asyncio
async def test_recheck_failure_is_logged_not_raised(monkeypatch, issues, caplog):
    hass, feed, rt = _setup(monkeypatch, [_dev(1, "SolarAssistant", None, "sa")], {"sa": SA},
                            choice=None, profiles_now=[_sa_profile()])

    def boom():
        raise OSError("disk")
    monkeypatch.setattr(runtime.ds, "load_profiles", boom)
    await _feed_updated(hass, feed)
    assert issues == [] and "profile choice check" in caplog.text
