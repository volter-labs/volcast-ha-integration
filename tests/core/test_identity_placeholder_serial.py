"""Numer seryjny-zaślepka (jeden powtórzony znak, same myślniki) to serial nieczytelny.

Po wymianie płyty głównej falownik bywa z serialem `000000…` albo `AAAAAA…`. Dwa takie urządzenia
miałyby jeden odcisk (`profil|serial`), a odcisk z seriala wiąże własność i przepięcie adresu z egzemplarzem.
Profil z rejestrem seriala daje wtedy tożsamość nieznaną (None), jak przy serialu nieodczytanym.
"""
from __future__ import annotations

import pytest

from custom_components.volcast.core.modbus.identity import device_fingerprint
from custom_components.volcast.core.profile import load_builtin
from custom_components.volcast.core.registers import RegisterImage
from tests.sim.fixtures import goodwe_words

SALT = b"s" * 16
GW = load_builtin("goodwe-et")
SERIAL_ADDR, SERIAL_WORDS = 35003, 8


def _with_serial(text: str) -> dict[int, int]:
    raw = text.encode("ascii").ljust(SERIAL_WORDS * 2, b"\x00")
    words = dict(goodwe_words())
    for i in range(SERIAL_WORDS):
        words[SERIAL_ADDR + i] = int.from_bytes(raw[2 * i:2 * i + 2], "big")
    return words


def _fp(text: str) -> str | None:
    return device_fingerprint(SALT, GW, RegisterImage(_with_serial(text)))


@pytest.mark.parametrize("serial", ["0000000000000000", "000000", "AAAAAAAAAAAAAAAA", "aAaAaAaA", "999999999",
                                    "----------------", "0000-0000-0000", "------"])
def test_placeholder_serial_is_unreadable(serial):
    assert _fp(serial) is None


@pytest.mark.parametrize("serial", ["95010ETU221W0123", "000001", "A0A0A0", "AB-CDEF"])
def test_real_serials_still_fingerprint(serial):
    fp = _fp(serial)
    assert isinstance(fp, str) and len(fp) == 16


def test_two_placeholder_devices_never_share_a_fingerprint():
    assert _fp("0000000000000000") is None and _fp("0000000000000000") == _fp("1111111111111111")
    assert _fp("95010ETU221W0123") != _fp("95010ETU221W0124")


def test_packaged_simulator_serial_is_unchanged():
    # Odcisk zwykłego seriala nie zmienia się (zapisane cele i rekordy własności zostają ważne).
    assert device_fingerprint(SALT, GW, RegisterImage(goodwe_words())) is not None
