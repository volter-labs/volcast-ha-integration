"""Złożenie sterowania dla wpisu sparowanego z kontem (część zależna od HA)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ControlRuntime:
    executor: object
    fetcher: object
    telemetry: object
    cloud: object
    choice: object | None
    mapped: dict[str, str]
    rated_power_w: float | None
    unsubs: list = field(default_factory=list)
