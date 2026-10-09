"""Szablon testu profilu marki: te same asercje dla każdego profilu z obrazem golden."""
import pytest

from custom_components.volcast.core.profile import load_builtin
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import golden_image, goodwe_image

CASES = {"goodwe-et": goodwe_image, "deye-sg": lambda: golden_image("deye-sg")}
OTHER = {"goodwe-et": "deye-sg", "deye-sg": "goodwe-et"}


@pytest.fixture(params=sorted(CASES))
def case(request):
    pid = request.param
    return load_builtin(pid), CASES[pid](), CASES[OTHER[pid]]()


def test_identify_matches_own_image_and_rejects_foreign(case):
    profile, image, foreign = case
    pg.assert_identify_matches(profile, image)
    pg.assert_identify_rejects(profile, foreign)


def test_every_read_key_decodes(case):
    profile, image, _ = case
    values = pg.decode_reads(profile, image)
    assert set(values) == set(profile.raw["read"])
    assert values["soc"] is not None


def test_static_consistency(case):
    profile, _, _ = case
    pg.assert_intents_consistent(profile)
    pg.assert_ha_regexes_compile(profile)
    pg.assert_capabilities_match_writes(profile)


def test_golden_image_reads_input_space():
    from custom_components.volcast.core.registers import FC_INPUT
    from tests.core.modbus import helpers
    img = helpers._image_from_doc({"registers": {"5": 7}, "input_registers": {"5": 9}})
    assert img.words(5, 1) == [7] and img.words(5, 1, FC_INPUT) == [9]
