"""Macierz identyfikacji: każdy profil rozpoznaje własny obraz golden i odrzuca wszystkie cudze."""
import pytest

from custom_components.volcast.core.profile import load_builtin
from tests.core import profile_golden as pg
from tests.core.modbus.helpers import GOLDEN, golden_image, goodwe_image

# Katalog golden -> id profilu (goodwe_et ma własny helper obrazu).
IMAGES = sorted(d.name for d in GOLDEN.iterdir() if (d / "registers.json").exists() or d.name == "goodwe_et")
PROFILE_IDS = {name: name.replace("_", "-") for name in IMAGES}
PROFILE_IDS["goodwe_et"] = "goodwe-et"


def _image(name):
    return goodwe_image() if name == "goodwe_et" else golden_image(name)


def _profile(pid):
    return load_builtin(pid)


@pytest.mark.parametrize("image_name", IMAGES)
@pytest.mark.parametrize("profile_name", IMAGES)
def test_identify_matrix(profile_name, image_name):
    profile = _profile(PROFILE_IDS[profile_name])
    image = _image(image_name)
    if profile_name == image_name:
        pg.assert_identify_matches(profile, image)
    else:
        pg.assert_identify_rejects(profile, image)
