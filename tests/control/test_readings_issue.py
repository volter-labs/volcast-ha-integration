"""Zgłoszenia w Naprawach: brak odczytów (tryb encji) / niedostępne łącze (tryb bezpośredni) / zablokowany tryb wymuszony."""
from types import SimpleNamespace

import pytest

from custom_components.volcast.const import CONTROL_MODE_DIRECT, OPT_CONTROL_MODE
from custom_components.volcast.control import runtime as rt_mod

HOST = "192.0.2.77"          # zakres dokumentacyjny
NEUTRAL = "general"
FORCED = "charge_battery"


class _Report:
    def __init__(self, ok: bool):
        src = "direct" if ok else "none"
        self.sources = {"soc": src, "pv_power_w": src, "grid_power_w": src}

    def readings_report(self):
        return {"readings_source": dict(self.sources), "readings_dropped": []}


class _Executor:
    def __init__(self, last_mode=None, *, consent=True, switch=True, write_end=None):
        self.last_written_mode = last_mode
        self.neutral_mode = NEUTRAL
        self.consent = consent
        self.local_switch = switch
        self.last_write_end = write_end


class _Relocator:
    def __init__(self, dead):
        self.dead = dead

    def link_dead(self):
        return self.dead


def _rt(*, direct=True, dead=False, last_mode=None, **kw):
    conn = SimpleNamespace(trial=False, target={"host": HOST}) if direct else None
    return SimpleNamespace(direct=conn, relocator=_Relocator(dead) if direct else None,
                           executor=_Executor(last_mode, **kw))


def _entry(direct=True, eid="e1"):
    return SimpleNamespace(entry_id=eid, options={OPT_CONTROL_MODE: CONTROL_MODE_DIRECT} if direct else {})


@pytest.fixture
def issues(monkeypatch):
    created, deleted = {}, []
    monkeypatch.setattr(rt_mod.ir, "async_create_issue",
                        lambda hass, dom, iid, **kw: created.__setitem__(iid, kw))
    monkeypatch.setattr(rt_mod.ir, "async_delete_issue", lambda hass, dom, iid: deleted.append(iid))
    return created, deleted


def _hass():
    return SimpleNamespace(data={})


def _sync(hass, entry, ok, rt):
    rt_mod.sync_readings_issue(hass, entry, _Report(ok), rt=rt)


# ── no_readings / direct_unreachable ─────────────────────────────────────

def test_entity_mode_keeps_no_readings(issues):
    created, _ = issues
    _sync(_hass(), _entry(False), False, _rt(direct=False))
    assert list(created) == ["no_readings_e1"] and created["no_readings_e1"]["translation_key"] == "no_readings"


def test_direct_mode_creates_direct_unreachable_instead(issues):
    created, _ = issues
    _sync(_hass(), _entry(), False, _rt())
    assert list(created) == ["direct_unreachable_e1"]
    kw = created["direct_unreachable_e1"]
    assert kw["translation_key"] == "direct_unreachable" and kw["is_fixable"] is False
    assert HOST not in repr(kw) and not kw.get("translation_placeholders")


def test_direct_mode_deletes_both_when_readings_return(issues):
    created, deleted = issues
    _sync(_hass(), _entry(), True, _rt())
    assert created == {} and "direct_unreachable_e1" in deleted and "no_readings_e1" in deleted


def test_trial_connection_is_not_direct_mode(issues):
    created, _ = issues
    rt = _rt()
    rt.direct.trial = True
    _sync(_hass(), _entry(), False, rt)
    assert list(created) == ["no_readings_e1"]


def test_without_rt_behaves_as_before(issues):
    created, _ = issues
    rt_mod.sync_readings_issue(object(), SimpleNamespace(entry_id="e1"), _Report(False))
    assert list(created) == ["no_readings_e1"]


# ── forced_mode_stuck: macierz ───────────────────────────────────────────

@pytest.mark.parametrize("direct,dead,last,expect", [
    (False, True, FORCED, False),       # tryb encji: nigdy
    (False, False, FORCED, False),
    (True, False, FORCED, False),       # łącze żywe
    (True, False, NEUTRAL, False),
    (True, False, None, False),
    (True, True, NEUTRAL, False),       # martwe, ale neutralny
    (True, True, None, False),          # martwe, nic nie zapisywaliśmy
    (True, True, FORCED, True),         # martwe i wymuszony
])
def test_forced_mode_stuck_matrix(issues, direct, dead, last, expect):
    created, _ = issues
    _sync(_hass(), _entry(direct), False, _rt(direct=direct, dead=dead, last_mode=last))
    assert ("forced_mode_stuck_e1" in created) is expect
    if expect:
        kw = created["forced_mode_stuck_e1"]
        assert kw["translation_key"] == "forced_mode_stuck" and kw["is_fixable"] is False
        assert str(getattr(kw["severity"], "value", kw["severity"])) == "error"
        assert HOST not in repr(kw) and not kw.get("translation_placeholders")


def test_forced_issue_exists_while_readings_present_but_link_dead(issues):
    created, _ = issues
    _sync(_hass(), _entry(), True, _rt(dead=True, last_mode=FORCED))   # odczyt jeszcze „świeży” w raporcie
    assert "forced_mode_stuck_e1" in created and "direct_unreachable_e1" not in created


# ── forced_mode_stuck: kasowanie ─────────────────────────────────────────

def test_stays_after_link_returns_until_a_write_is_made(issues):
    created, deleted = issues
    hass, entry = _hass(), _entry()
    _sync(hass, entry, False, _rt(dead=True, last_mode=FORCED, write_end=10.0))
    _sync(hass, entry, True, _rt(dead=False, last_mode=FORCED, write_end=10.0))   # łącze wróciło, bez zapisu
    assert "forced_mode_stuck_e1" not in deleted
    _sync(hass, entry, True, _rt(dead=False, last_mode=FORCED, write_end=20.0))   # zapis bieżącego kroku planu
    assert "forced_mode_stuck_e1" in deleted


def test_deleted_after_neutral_write(issues):
    _, deleted = issues
    hass, entry = _hass(), _entry()
    _sync(hass, entry, False, _rt(dead=True, last_mode=FORCED, write_end=10.0))
    _sync(hass, entry, True, _rt(dead=False, last_mode=NEUTRAL, write_end=15.0))
    assert "forced_mode_stuck_e1" in deleted


def test_latch_survives_reload_with_empty_memory(issues):
    """Przeładowanie po przepięciu: nowy wykonawca nie zna ostatniego trybu — zgłoszenie nie znika."""
    created, deleted = issues
    hass, entry = _hass(), _entry()
    _sync(hass, entry, False, _rt(dead=True, last_mode=FORCED, write_end=10.0))
    deleted.clear()
    _sync(hass, entry, True, _rt(dead=False, last_mode=None, write_end=None))
    assert "forced_mode_stuck_e1" not in deleted
    _sync(hass, entry, True, _rt(dead=False, last_mode=FORCED, write_end=30.0))
    assert "forced_mode_stuck_e1" in deleted


@pytest.mark.parametrize("kw", [{"consent": False}, {"switch": False}])
def test_deleted_when_owner_disables_control(issues, kw):
    created, deleted = issues
    hass, entry = _hass(), _entry()
    _sync(hass, entry, False, _rt(dead=True, last_mode=FORCED))
    deleted.clear()
    _sync(hass, entry, False, _rt(dead=True, last_mode=FORCED, **kw))
    assert "forced_mode_stuck_e1" in deleted


def test_two_entries_do_not_share_the_latch(issues):
    created, deleted = issues
    hass = _hass()
    _sync(hass, _entry(eid="a"), False, _rt(dead=True, last_mode=FORCED, write_end=1.0))
    _sync(hass, _entry(eid="b"), True, _rt(dead=False, last_mode=FORCED, write_end=2.0))
    assert "forced_mode_stuck_b" in deleted and "forced_mode_stuck_a" not in deleted


# ── relokacja i sprzątanie ───────────────────────────────────────────────

def test_clear_issues_removes_all_three_and_latch(issues):
    _, deleted = issues
    hass = _hass()
    _sync(hass, _entry(), False, _rt(dead=True, last_mode=FORCED))
    rt_mod.clear_link_issues(hass, "e1")
    for iid in ("no_readings_e1", "direct_unreachable_e1", "forced_mode_stuck_e1"):
        assert iid in deleted
    assert "e1" not in hass.data.get(rt_mod._STUCK_KEY, {})


def test_relocator_link_dead_shares_the_trigger():
    from custom_components.volcast.control import relocator as rl
    conn = SimpleNamespace(refused=lambda: None, age_s=lambda: rl.RELOCATE_AFTER_S + 1)
    rel = rl.Relocator.__new__(rl.Relocator)
    rel._conn, rel._clock, rel._started_at = conn, (lambda: 1000.0), 0.0
    assert rel.link_dead() is True
    conn.age_s = lambda: 5.0
    assert rel.link_dead() is False
    conn.refused = lambda: "clash"
    conn.age_s = lambda: float("inf")
    assert rel.link_dead() is False
