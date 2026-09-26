"""Wspólne czytanie złotych wektorów implementacji referencyjnej."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from custom_components.volcast.core.params import Params
from custom_components.volcast.core.slot import Action, Slot

G = Path(__file__).resolve().parents[1] / "golden" / "goodwe_et"
T0 = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


def load_golden(name: str) -> dict:
    return json.loads((G / f"{name}.json").read_text())


def _mode_name(value: int, profile) -> str:
    m = profile.mode_by_value(value)
    return m.name if m else f"unknown:{value}"


def params_from_golden(raw: dict, profile) -> Params:
    return Params(mode=_mode_name(raw["mode"], profile), power_w=raw["power_w"],
                  soc_min=raw["soc_min"], soc_max=raw["soc_max"],
                  export_limit_w=raw["export_limit_w"],
                  export_limit_enabled=raw["export_limit_enabled"])


def slot_from_golden(raw: dict) -> Slot:
    return Slot(start=T0, end=T0.replace(hour=11), action=Action(raw["mode"]),
                charge_source=raw["charge_source"], discharge_purpose=raw["discharge_purpose"],
                power_w=raw["power_w"], soc_target=raw["soc_target"],
                export_allowed=raw["export_allowed"], export_limit_w=raw["export_limit_w"])


def assert_params_equal(actual: Params, expected: Params) -> None:
    for f in ("mode", "export_limit_enabled"):
        assert getattr(actual, f) == getattr(expected, f), f
    for f in ("power_w", "soc_min", "soc_max", "export_limit_w"):
        a, e = getattr(actual, f), getattr(expected, f)
        assert (a is None) == (e is None), f"{f}: {a} vs {e}"
        if e is not None:
            assert a == pytest.approx(e, abs=1e-3), f
    assert actual.tou == expected.tou, "tou"
