"""Walidacja adresu celu transportu: tylko literały z sieci lokalnych (bez nazw i adresów publicznych)."""
import pytest

from custom_components.volcast.core.transports.base import TransportConfig, check_target
from custom_components.volcast.core.transports.factory import make_transport


@pytest.mark.parametrize("host, expected", [
    ("192.168.1.50", "192.168.1.50"),
    ("10.0.0.2", "10.0.0.2"),
    ("172.16.4.9", "172.16.4.9"),
    ("169.254.10.20", "169.254.10.20"),
    ("fe80::1%eth0", "fe80::1%eth0"),
    ("fd00::5", "fd00::5"),
    ("::ffff:192.168.1.50", "192.168.1.50"),
])
def test_check_target_accepts_private_and_link_local(host, expected):
    assert check_target(host) == expected


@pytest.mark.parametrize("host", [
    "inverter.local", "localhost", "8.8.8.8", "2001:4860::1", "", "192.168.1", "192.168.1.50:502",
])
def test_check_target_rejects_hostname_and_public(host):
    with pytest.raises(ValueError):
        check_target(host)


@pytest.mark.parametrize("host", [
    "0.0.0.0", "255.255.255.255", "224.0.0.1", "239.1.2.3", "240.0.0.1", "192.0.2.10",
    "198.51.100.7", "203.0.113.200", "2001:db8::1", "::", "ff02::1", "fe80::1", "::ffff:8.8.8.8",
    "::ffff:0.0.0.0",
])
def test_check_target_rejects_special(host):
    with pytest.raises(ValueError):
        check_target(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_only_when_allowed(host):
    with pytest.raises(ValueError):
        check_target(host)
    assert check_target(host, allow_loopback=True) in ("127.0.0.1", "::1")


def test_non_string_host_rejected():
    with pytest.raises(ValueError):
        check_target(None)          # type: ignore[arg-type]


def test_rejection_message_never_contains_host():
    with pytest.raises(ValueError) as e:
        check_target("8.8.4.4")
    assert "8.8.4.4" not in str(e.value)


def test_make_transport_refuses_public_target_without_socket():
    # Nic nie jest otwierane: odmowa pada przy budowie transportu (straż sieci złapałaby gniazdo).
    with pytest.raises(ValueError):
        make_transport(TransportConfig(kind="modbus_tcp", host="203.0.113.5", port=502, unit=1))


@pytest.mark.parametrize("kw", [
    {"kind": "serial"},
    {"port": 0},
    {"port": 70000},
    {"unit": 256},
    {"timeout_s": 0},
    {"read_tries": 0},
    {"gap_s": -1},
])
def test_make_transport_validates_config(kw):
    base = {"kind": "modbus_tcp", "host": "127.0.0.1", "port": 502, "unit": 1}
    base.update(kw)
    with pytest.raises(ValueError):
        make_transport(TransportConfig(**base), allow_loopback=True)


def test_solarman_requires_logger_serial():
    with pytest.raises(ValueError):
        make_transport(TransportConfig(kind="solarman_v5", host="127.0.0.1", port=8899, unit=1),
                       allow_loopback=True)
