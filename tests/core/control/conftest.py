"""Fixtures ścieżki rejestrów w cyklu sterowania (obraz z nagrania GoodWe)."""
from datetime import timedelta

import pytest

from custom_components.volcast.core.modbus.reading import build_reading
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage
from custom_components.volcast.core.slot import parse_schedule
from tests.core.golden import T0
from tests.sim.fixtures import goodwe_words

SOC_REG, MODE_REG, POWER_REG = 37007, 47511, 47512


def goodwe_reading(profile, **over):
    words = goodwe_words()
    words.update({int(a): v for a, v in over.items()})
    return build_reading(profile, RegisterImage(words), at_mono=1000.0, at_utc=T0)


@pytest.fixture
def goodwe_profile():
    return load_builtin("goodwe-et")


def one_slot(**kw):
    iso = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    slot = {"from": iso(T0 - timedelta(minutes=30)), "to": iso(T0 + timedelta(hours=1)),
            "price_pln_kwh": 0.8, **kw}
    return parse_schedule({"schedule_id": "s-reg", "slots": [slot],
                           "fallback": {"mode": "self_consume", "soc_reserve": 20},
                           "control_enabled": True})


@pytest.fixture
def sell_schedule():
    return one_slot(mode="discharge", discharge_purpose="sell", power_w=2500)


@pytest.fixture
def charge_schedule():
    """Ładowanie z sieci 3 kW do 90 % — plan z górnym progiem SoC (47760)."""
    return one_slot(mode="charge", charge_source="grid", power_w=3000, soc_target=90)


@pytest.fixture
def reading_auto(goodwe_profile):
    return goodwe_reading(goodwe_profile, **{str(MODE_REG): 1, str(SOC_REG): 80})


@pytest.fixture
def reading_mode_99(goodwe_profile):
    return goodwe_reading(goodwe_profile, **{str(MODE_REG): 99, str(SOC_REG): 80})
