"""Fabryka transportów: walidacja konfiguracji i adresu PRZED otwarciem czegokolwiek."""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from .base import RegisterTransport, Sleep, TransportConfig, check_target, validate_config
from .goodwe_udp import GoodweUdpTransport
from .modbus_tcp import ModbusTcpTransport
from .rtu_tcp import RtuTcpTransport
from .solarman_v5 import SolarmanV5Transport

_KINDS = {
    "goodwe_udp": GoodweUdpTransport,
    "modbus_tcp": ModbusTcpTransport,
    "modbus_rtu": RtuTcpTransport,
    "solarman_v5": SolarmanV5Transport,
}


def make_transport(cfg: TransportConfig, *, clock: Callable[[], float] = time.monotonic,
                   allow_loopback: bool = False, sleep: Sleep = asyncio.sleep) -> RegisterTransport:
    """Transport z konfiguracji; ValueError przy złej konfiguracji albo adresie spoza sieci lokalnej."""
    validate_config(cfg)
    host = check_target(cfg.host, allow_loopback=allow_loopback)
    return _KINDS[cfg.kind](cfg, host, clock=clock, sleep=sleep)
