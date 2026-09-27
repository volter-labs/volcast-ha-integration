"""Złożenie sterowania dla wpisu sparowanego z kontem (część zależna od HA).

Zmiana opcji sterowania (sposób sterowania, profil, integracja falownika — od nich
zależy mapowanie encji) przeładowuje wpis. Nowy wykonawca nie może bezpiecznie
przywrócić migawki przez NOWE mapowanie, więc powrót do trybu bazowego robi STARY
wykonawca, zanim wpis się przeładuje: `async_restore_if_control_changed`. Woła go
przepływ opcji przed zapisem i słuchacz aktualizacji wpisu przed przeładowaniem.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

from ..const import OPT_CONTROL_MODE, OPT_INVERTER_DOMAIN, OPT_PROFILE_ID

_LOGGER = logging.getLogger(__name__)

# Opcje, od których zależą: czy sterujemy i przez które encje.
CONTROL_OPTION_KEYS = (OPT_CONTROL_MODE, OPT_PROFILE_ID, OPT_INVERTER_DOMAIN)


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


def control_options_changed(old: Mapping, new: Mapping) -> bool:
    return any(old.get(k) != new.get(k) for k in CONTROL_OPTION_KEYS)


async def async_restore_if_control_changed(runtime, old: Mapping, new: Mapping) -> bool:
    """Stary wykonawca przywraca tryb bazowy, gdy zmiana opcji zmienia sterowanie.

    True = powrót wykonany (wykonawca nie jest już właścicielem). Nigdy nie rzuca.
    """
    executor = getattr(runtime, "executor", None)
    if executor is None or not control_options_changed(old, new) or not getattr(executor, "owned", False):
        return False
    try:
        await executor.async_restore_now()
    except Exception as err:  # noqa: BLE001 — zapis opcji nie może się przez to wywrócić
        _LOGGER.warning("Volcast control: return to the baseline before reload failed (%s)",
                        type(err).__name__)
        return False
    return not getattr(executor, "owned", False)
