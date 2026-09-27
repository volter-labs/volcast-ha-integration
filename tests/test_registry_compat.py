"""Rejestr urządzeń bez wycofywanego widoku-mapowania (bieżące HA ostrzega, potem odmówi).

Starsze HA: `registry.devices` to mapowanie id → urządzenie. Nowsze: widok, którego
iteracja daje urządzenia, a `.values()`/`[id]` jest wycofywane.
"""
from types import SimpleNamespace

from custom_components.volcast.control import runtime
from custom_components.volcast.registry_compat import all_devices


class DeviceView:
    """Jak widok `registry.devices` w bieżącym HA: iteracja daje wpisy, mapowanie zabronione."""

    def __init__(self, devices):
        self._devices = list(devices)

    def __iter__(self):
        return iter(self._devices)

    def __len__(self):
        return len(self._devices)

    def __getattr__(self, name):
        raise AssertionError(f"deprecated mapping access: {name}")

    def __getitem__(self, key):
        raise AssertionError("deprecated mapping access: []")


def _dev(i, **kw):
    base = dict(id=f"d{i}", manufacturer="GoodWe", model="GW10K-ET", name="Inverter", sw_version=None,
                config_entries={"ce1"}, disabled_by=None,
                serial_number=None, identifiers=set())
    return SimpleNamespace(**{**base, **kw})


def test_all_devices_from_mapping_view_and_missing(monkeypatch, caplog):
    from custom_components.volcast import registry_compat

    monkeypatch.setattr(registry_compat, "_warned_missing", False)
    a, b = _dev(1), _dev(2)
    assert all_devices(SimpleNamespace(devices={"d1": a, "d2": b})) == [a, b]
    assert all_devices(SimpleNamespace(devices=DeviceView([a, b]))) == [a, b]
    assert caplog.records == []
    # Brak listy urządzeń: pusta lista, ale jedno ostrzeżenie (nie przy każdym wywołaniu).
    assert all_devices(SimpleNamespace()) == []
    assert all_devices(SimpleNamespace()) == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "device registry" in warnings[0].getMessage()


def test_inverter_hints_iterate_the_new_view(monkeypatch):
    reg = SimpleNamespace(devices=DeviceView([_dev(1), _dev(2, disabled_by="user")]))
    monkeypatch.setattr(runtime.dr, "async_get", lambda hass: reg)
    hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_get_entry=lambda ce_id: SimpleNamespace(domain="goodwe")))
    hints = runtime.inverter_hints(hass)
    assert [(h.domain, h.model) for h in hints] == [("goodwe", "GW10K-ET")]


def test_diagnostics_serials_iterate_the_new_view(monkeypatch):
    from custom_components.volcast import diagnostics

    reg = SimpleNamespace(devices=DeviceView([_dev(1, serial_number="SERIAL123456",
                                                   identifiers={("goodwe", "IDENT0000001")})]))
    monkeypatch.setattr(diagnostics.dr, "async_get", lambda hass: reg)
    assert diagnostics._serials(SimpleNamespace()) == {"SERIAL123456", "IDENT0000001"}


def test_discovery_snapshot_iterates_the_new_view(monkeypatch):
    from custom_components.volcast import discovery_runner

    reg = SimpleNamespace(devices=DeviceView([_dev(1), _dev(2, disabled_by="user")]))
    monkeypatch.setattr(discovery_runner.dr, "async_get", lambda hass: reg)
    runner = discovery_runner.DiscoveryRunner(SimpleNamespace(data={}), "e1", "1.0")
    snaps, skipped = runner._snap_devices()
    assert [s.id for s in snaps] == ["d1"] and skipped == frozenset({"d2"})
