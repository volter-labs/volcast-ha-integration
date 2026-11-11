"""Adres urządzenia per żądanie: rejestr profilu z własnym `unit_id` idzie do innego urządzenia niż
adres połączenia — ramka żądania i dopasowanie odpowiedzi używają tego samego adresu."""
import pytest

from custom_components.volcast.core.transports.base import RequestTimeout, TransportConfig, read_req, write_req
from custom_components.volcast.core.transports.factory import make_transport
from custom_components.volcast.core.transports.modbus_frames import FC_WRITE_MULTIPLE, FC_WRITE_SINGLE
from tests.sim.device import Faults, RegisterBank
from tests.sim.servers import goodwe_udp_server, modbus_tcp_server, rtu_tcp_server, solarman_v5_server

_SERIAL = 1234567890
_DEVICE_UNIT = 247          # urządzenie odpowiada tylko na ten adres
_LINK_UNIT = 3              # adres połączenia (inny)


async def _server(kind: str, bank, faults):
    if kind == "modbus_tcp":
        return await modbus_tcp_server(bank, faults, unit=_DEVICE_UNIT)
    if kind == "modbus_rtu":
        return await rtu_tcp_server(bank, faults, unit=_DEVICE_UNIT)
    if kind == "solarman_v5":
        return await solarman_v5_server(bank, faults, logger_serial=_SERIAL, unit=_DEVICE_UNIT)
    return await goodwe_udp_server(bank, faults, unit=_DEVICE_UNIT)


def _transport(sim, kind: str):
    cfg = TransportConfig(kind=kind, host=sim.host, port=sim.port, unit=_LINK_UNIT, timeout_s=0.15, gap_s=0.0,
                          read_tries=1, logger_serial=_SERIAL if kind == "solarman_v5" else None)
    return make_transport(cfg, allow_loopback=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["modbus_tcp", "modbus_rtu", "solarman_v5", "goodwe_udp"])
async def test_read_and_write_on_own_unit(kind):
    bank = RegisterBank({40032: 0, 40033: 0, 40031: 2})
    sim = await _server(kind, bank, Faults())
    t = _transport(sim, kind)
    try:
        assert await t.read(40031, 1, unit=_DEVICE_UNIT) == [2]
        await t.write(40032, [0, 2500], function=FC_WRITE_MULTIPLE, unit=_DEVICE_UNIT)
        assert await t.read(40032, 2, unit=_DEVICE_UNIT) == [0, 2500]
        await t.write(40031, [3], function=FC_WRITE_SINGLE, unit=_DEVICE_UNIT)
        assert await t.read(40031, 1, unit=_DEVICE_UNIT) == [3]
        # bez własnego adresu żądanie idzie na adres połączenia — urządzenie milczy
        with pytest.raises(RequestTimeout):
            await t.read(40031, 1)
    finally:
        await t.close()
        await sim.close()


def test_request_unit_defaults_to_link_and_is_range_checked():
    assert read_req(1, 1).unit is None and write_req(FC_WRITE_SINGLE, 1, [1]).unit is None
    assert read_req(1, 1, 3, 247).unit == 247
    assert write_req(FC_WRITE_MULTIPLE, 1, [1, 2], 0).unit == 0
    for bad in (-1, 256, True, 1.0):
        with pytest.raises(ValueError):
            read_req(1, 1, 3, bad)
        with pytest.raises(ValueError):
            write_req(FC_WRITE_SINGLE, 1, [1], bad)
