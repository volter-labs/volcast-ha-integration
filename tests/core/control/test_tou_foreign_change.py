"""Obca zmiana programu TOU: pierwsza = przejęcie klucza (model okien czasowych), powrót warunkowy."""
from __future__ import annotations

import pytest

from custom_components.volcast.core.control.takeover import first_change_takes_over
from custom_components.volcast.core.control.tou_cycle import EN_KEY
from custom_components.volcast.core.control.tou_writes import restore_skips


@pytest.mark.parametrize("key", ["tou.1.start", "tou.2.start", "tou.6.grid_charge", "tou.3.soc", EN_KEY])
def test_time_window_tou_key_is_taken_over_at_the_first_change(key):
    assert first_change_takes_over("time_window", key)


@pytest.mark.parametrize("model,key", [
    ("mode_setpoint", "tou.2.start"),            # GoodWe i reszta: próg dwóch rozjazdów zostaje
    ("mode_setpoint", "mode"),
    ("time_window", "mode"),                     # klucz spoza programów TOU
    ("time_window", "export_limit_w"),
    ("time_window", "tou_word"),                 # słowo surowe to nie klucz widoku urządzenia
    (None, "tou.2.start"),
])
def test_other_keys_and_models_keep_the_drift_limit(model, key):
    assert not first_change_takes_over(model, key)


TARGET = {"tou.1.start": 0.0, "tou.2.start": 300.0, "tou.2.soc": 20.0, EN_KEY: 0.0}


def test_value_still_ours_is_restored():
    device = {"tou.1.start": 0.0, "tou.2.start": 120.0, "tou.2.soc": 90.0, EN_KEY: 1.0}
    ours = {"tou.2.start": 120.0, "tou.2.soc": 90.0, EN_KEY: 1.0}
    assert restore_skips(TARGET, device, ours) == {}


def test_value_changed_after_our_write_is_skipped_and_reported():
    device = {"tou.1.start": 0.0, "tou.2.start": 125.0, "tou.2.soc": 90.0, EN_KEY: 1.0}
    ours = {"tou.2.start": 120.0, "tou.2.soc": 90.0, EN_KEY: 1.0}
    assert restore_skips(TARGET, device, ours) == {"tou.2.start": (300.0, 125.0)}


def test_key_without_our_value_is_restored():
    """Bez wartości odniesienia (nie wiemy, co było nasze) — powrót jak dotąd."""
    device = {"tou.2.start": 125.0}
    assert restore_skips(TARGET, device, {}) == {}


def test_uncertain_key_is_restored():
    """Nasz zapis o nieznanym wyniku mógł dojść — nie przypisujemy wartości właścicielowi."""
    device = {"tou.2.start": 125.0}
    assert restore_skips(TARGET, device, {"tou.2.start": 120.0}, uncertain={"tou.2.start"}) == {}


def test_key_already_at_target_or_unread_is_not_reported():
    device = {"tou.1.start": 0.0}                # tou.2.* bez odczytu
    ours = {"tou.1.start": 60.0, "tou.2.start": 120.0}
    assert restore_skips(TARGET, device, ours) == {}


def test_enable_changed_by_the_owner_is_skipped():
    device = {EN_KEY: 1.0}
    assert restore_skips({EN_KEY: 0.0}, device, {EN_KEY: 0.0}) == {EN_KEY: (0.0, 1.0)}
