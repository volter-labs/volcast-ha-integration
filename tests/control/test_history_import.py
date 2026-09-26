import asyncio
import sys
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.volcast.control import history_import as hi

NOW = datetime(2026, 9, 27, 10, 30, tzinfo=timezone.utc)
T8 = datetime(2026, 9, 27, 8, tzinfo=timezone.utc).timestamp()


class Exec:
    def __init__(self, done=None):
        self.history_imported_at = done
        self.marked = []

    async def async_mark_history_imported(self, when):
        self.marked.append(when)
        self.history_imported_at = when


class Cloud:
    def __init__(self, *results, delay=0.0):
        self.results, self.calls, self.delay = list(results), [], delay

    @property
    def hours(self):
        return self.calls[-1] if self.calls else None

    async def async_import_history(self, hours):
        self.calls.append(hours)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def patch_recorder(monkeypatch, rows, unit="kWh"):
    monkeypatch.setattr(hi, "_async_statistics", lambda hass, ids, start, end: _ret(rows))
    monkeypatch.setattr(hi, "_async_units", lambda hass, ids: _ret({i: unit for i in ids}))


async def _ret(v):
    return v


def test_imports_once_and_marks(monkeypatch):
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]})
    ex, cloud = Exec(), Cloud({"accepted": 1, "inserted": 1})
    out = asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house", now_utc=NOW))
    assert out == {"accepted": 1, "inserted": 1} and ex.marked and cloud.hours[0]["load_kwh"] == 0.4
    assert ex.marked == [NOW.isoformat()]


def test_already_imported_or_no_entity_does_nothing(monkeypatch):
    patch_recorder(monkeypatch, {})
    assert asyncio.run(hi.async_import_history_once(object(), Cloud({}), Exec(done="x"),
                                                    load_entity="sensor.house", now_utc=NOW)) is None
    assert asyncio.run(hi.async_import_history_once(object(), Cloud({}), Exec(),
                                                    load_entity=None, now_utc=NOW)) is None


def test_cloud_failure_is_not_marked(monkeypatch):
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]})
    ex = Exec()
    assert asyncio.run(hi.async_import_history_once(object(), Cloud(None), ex, load_entity="sensor.house",
                                                    now_utc=NOW)) is None
    assert ex.marked == []


def test_no_hours_is_not_marked_and_not_sent(monkeypatch):
    patch_recorder(monkeypatch, {"sensor.house": []})
    ex, cloud = Exec(), Cloud({"accepted": 0})
    assert asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                    now_utc=NOW)) is None
    assert cloud.calls == [] and ex.marked == []


def test_recorder_error_is_swallowed_and_not_marked(monkeypatch):
    async def boom(*_a):
        raise KeyError("recorder")
    monkeypatch.setattr(hi, "_async_statistics", boom)
    ex, cloud = Exec(), Cloud({"accepted": 1})
    assert asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                    now_utc=NOW)) is None
    assert cloud.calls == [] and ex.marked == []


def test_pv_series_sent_with_load(monkeypatch):
    # Rekorder jest proszony o `units={"energy": "kWh"}`, więc wiersze przychodzą już
    # w kWh niezależnie od jednostki zapisanej w metadanych — metadana "Wh" tu tylko
    # POTWIERDZA klasę energii, nie jest współczynnikiem przeliczenia.
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}],
                                 "sensor.pv": [{"start": T8, "change": 1.5}]}, unit="Wh")
    cloud = Cloud({"accepted": 1, "inserted": 1})
    asyncio.run(hi.async_import_history_once(object(), cloud, Exec(), load_entity="sensor.house",
                                             pv_entity="sensor.pv", now_utc=NOW))
    assert cloud.hours == [{"start": "2026-09-27T08:00:00Z", "load_kwh": 0.4, "pv_kwh": 1.5}]


def test_non_energy_unit_is_skipped_not_treated_as_kwh(monkeypatch):
    """Jednostka w metadanych, która nie jest klasy energii (np. moc "W"), nie ma
    współczynnika przeliczenia do zgadywania — seria jest pomijana CAŁKOWICIE."""
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]}, unit="W")
    ex, cloud = Exec(), Cloud({"accepted": 1})
    out = asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                    now_utc=NOW))
    assert out is None and cloud.calls == [] and ex.marked == []


def test_large_history_sent_in_batches_and_totals_summed(monkeypatch):
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [{"start": (base + timedelta(hours=i)).timestamp(), "change": 0.3} for i in range(250)]
    patch_recorder(monkeypatch, {"sensor.house": rows})
    monkeypatch.setattr(hi, "batches", lambda hours: [hours[:100], hours[100:200], hours[200:]])
    cloud = Cloud({"accepted": 100, "inserted": 90, "skipped_existing": 10, "rejected": []},
                  {"accepted": 100, "inserted": 100, "skipped_existing": 0,
                   "rejected": [{"index": 3, "reason": "load_out_of_range"}]},
                  {"accepted": 50, "inserted": 50, "skipped_existing": 0, "rejected": []})
    ex = Exec()
    out = asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house", now_utc=NOW))
    assert [len(c) for c in cloud.calls] == [100, 100, 50]
    assert out == {"accepted": 250, "inserted": 240, "skipped_existing": 10,
                   "rejected": [{"index": 103, "reason": "load_out_of_range"}]}
    assert ex.marked == [NOW.isoformat()]


def test_failed_batch_stops_and_is_not_marked(monkeypatch):
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [{"start": (base + timedelta(hours=i)).timestamp(), "change": 0.3} for i in range(30)]
    patch_recorder(monkeypatch, {"sensor.house": rows})
    monkeypatch.setattr(hi, "batches", lambda hours: [hours[:10], hours[10:20], hours[20:]])
    cloud = Cloud({"accepted": 10}, None, {"accepted": 10})
    ex = Exec()
    assert asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                    now_utc=NOW)) is None
    assert len(cloud.calls) == 2 and ex.marked == []                   # trzecia partia nie poszła


def test_all_rejected_is_not_marked(monkeypatch):
    """Chmura odpowiada 0 przyjętych i 0 pominiętych — to znak zepsutego czujnika/jednostki,
    nie „wszystko już było". Wynik wraca do wołającego, ale znacznika NIE zapisujemy,
    żeby naprawiona encja mogła spróbować ponownie."""
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]})
    ex = Exec()
    cloud = Cloud({"accepted": 0, "inserted": 0, "skipped_existing": 0,
                   "rejected": [{"index": 0, "reason": "load_out_of_range"}]})
    out = asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                    now_utc=NOW))
    assert out == {"accepted": 0, "inserted": 0, "skipped_existing": 0,
                   "rejected": [{"index": 0, "reason": "load_out_of_range"}]}
    assert ex.marked == []


def test_partial_accept_logs_rejected_count(monkeypatch, caplog):
    """5 zaakceptowanych z 10 wysłanych (reszta odrzucona, np. zły czujnik dla części
    okresu) nadal się oznacza jako zrobione — ale w logu MUSI być widać, że coś odpadło,
    inaczej „done" wygląda identycznie jak pełny sukces."""
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [{"start": (base + timedelta(hours=i)).timestamp(), "change": 0.3} for i in range(10)]
    patch_recorder(monkeypatch, {"sensor.house": rows})
    ex = Exec()
    cloud = Cloud({"accepted": 5, "inserted": 5, "skipped_existing": 0,
                   "rejected": [{"index": 5, "reason": "load_out_of_range"}]})
    with caplog.at_level("INFO", logger=hi._LOGGER.name):
        out = asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                        now_utc=NOW))
    assert out["accepted"] == 5
    assert ex.marked == [NOW.isoformat()]                                  # 5 > 0 -> oznaczone mimo odrzuceń
    assert any("partial" in r.message and "5" in r.message for r in caplog.records)


def test_all_already_existing_is_still_marked(monkeypatch):
    """0 nowo przyjętych, ale same pominięte jako już istniejące — to legalny stan
    (import po raz drugi), nie porażka; znacznik ma się zapisać."""
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]})
    ex = Exec()
    cloud = Cloud({"accepted": 0, "inserted": 0, "skipped_existing": 1, "rejected": []})
    asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house", now_utc=NOW))
    assert ex.marked == [NOW.isoformat()]


def test_marker_error_is_swallowed_and_result_still_returned(monkeypatch):
    """Zapis znacznika może się wywrócić (magazyn), ale import się UDAŁ — błąd znacznika
    nie ma prawa ukryć udanego wyniku ani wywrócić wołającego."""
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]})

    class BrokenExec(Exec):
        async def async_mark_history_imported(self, when):
            raise RuntimeError("store broken")

    ex, cloud = BrokenExec(), Cloud({"accepted": 1, "inserted": 1})
    out = asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                    now_utc=NOW))
    assert out == {"accepted": 1, "inserted": 1}
    assert ex.marked == []


def test_waiter_does_not_rerun_after_leader_failure(monkeypatch):
    """Gdy przebieg w toku PADA, czekający dostaje TEN SAM wynik (None) i nie odpala
    własnego, drugiego przebiegu — tylko jedno wywołanie chmury."""
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]})
    ex = Exec()
    cloud = Cloud(None, delay=0.01)

    async def go():
        return await asyncio.gather(
            hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house", now_utc=NOW),
            hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house", now_utc=NOW))

    first, second = asyncio.run(go())
    assert first is None and second is None
    assert len(cloud.calls) == 1
    assert ex.marked == []


def test_last_result_is_reset_not_stale_after_noop_run(monkeypatch):
    """Po udanym przebiegu `_LAST_RESULT` niesie sukces. Kolejny przebieg, który trafia
    na ścieżkę no-op (już zaimportowano), MUSI go nadpisać na None — inaczej czekający za
    TYM przebiegiem dostałby sukces sprzed chwili, niezwiązany z tym wywołaniem."""
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]})
    ex, cloud = Exec(), Cloud({"accepted": 1, "inserted": 1})

    first = asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                      now_utc=NOW))
    assert first == {"accepted": 1, "inserted": 1}
    assert hi._LAST_RESULT[ex] == {"accepted": 1, "inserted": 1}

    second = asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                       now_utc=NOW))
    assert second is None                                                  # już zaimportowano — no-op
    assert hi._LAST_RESULT[ex] is None
    assert len(cloud.calls) == 1                                           # drugi przebieg nic nie wysłał


def test_last_result_is_reset_after_exception_in_import(monkeypatch):
    """Wyjątek w `_async_import` (poza znanymi, łykanymi ścieżkami) leci do wołającego,
    ale `_LAST_RESULT` nie może zostać ze starym sukcesem — `finally` musi go nadpisać."""
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]})
    ex, cloud = Exec(), Cloud({"accepted": 1, "inserted": 1})
    asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house", now_utc=NOW))
    assert hi._LAST_RESULT[ex] == {"accepted": 1, "inserted": 1}

    ex.history_imported_at = None                                         # wymuś ponowny (nie-no-op) przebieg

    async def boom(*_a):
        raise RuntimeError("cloud on fire")
    monkeypatch.setattr(hi, "_async_import", boom)

    with pytest.raises(RuntimeError):
        asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house", now_utc=NOW))
    assert hi._LAST_RESULT[ex] is None


def test_concurrent_imports_send_once(monkeypatch):
    patch_recorder(monkeypatch, {"sensor.house": [{"start": T8, "change": 0.4}]})
    ex, cloud = Exec(), Cloud({"accepted": 1, "inserted": 1}, delay=0.01)

    async def go():
        return await asyncio.gather(
            hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house", now_utc=NOW),
            hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house", now_utc=NOW))

    first, second = asyncio.run(go())
    assert len(cloud.calls) == 1 and ex.marked == [NOW.isoformat()]
    # Czekający dostaje wynik przebiegu, na który czekał — nie „porażkę".
    assert first == second == {"accepted": 1, "inserted": 1}
    # Kolejne wołanie po fakcie: nic nie wysyła.
    assert asyncio.run(hi.async_import_history_once(object(), cloud, ex, load_entity="sensor.house",
                                                    now_utc=NOW)) is None
    assert len(cloud.calls) == 1


def test_recorder_reads_run_in_executor_with_hour_change(monkeypatch):
    stats_mod = sys.modules["homeassistant.components.recorder.statistics"]
    rec_mod = sys.modules["homeassistant.components.recorder"]
    jobs, calls = [], []

    class Instance:
        async def async_add_executor_job(self, fn, *args):
            jobs.append(fn)
            return fn(*args)

    def fake_stats(hass, start, end, ids, period, units, types):
        calls.append((start, end, ids, period, units, types))
        return {"sensor.house": []}

    def fake_meta(hass, *, statistic_ids):
        return {"sensor.house": (1, {"unit_of_measurement": "Wh"}), "sensor.pv": (2, {})}

    monkeypatch.setattr(rec_mod, "get_instance", lambda hass: Instance())
    monkeypatch.setattr(stats_mod, "statistics_during_period", fake_stats)
    monkeypatch.setattr(stats_mod, "get_metadata", fake_meta, raising=False)

    start = NOW - timedelta(days=60)
    rows = asyncio.run(hi._async_statistics(object(), {"sensor.house"}, start, NOW))
    units = asyncio.run(hi._async_units(object(), {"sensor.house", "sensor.pv"}))
    assert rows == {"sensor.house": []}
    # `units={"energy": "kWh"}` — jednoznaczna jednostka, nie zgadywanie z aktualnej
    # jednostki stanu encji.
    assert calls == [(start, NOW, {"sensor.house"}, "hour", {"energy": "kWh"}, {"change"})]
    assert units == {"sensor.house": "Wh", "sensor.pv": None}
    assert len(jobs) == 2                                                  # oba odczyty poza pętlą zdarzeń
