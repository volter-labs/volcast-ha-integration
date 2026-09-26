"""Słownik parametrów semantycznych — wspólny język silników, guardów i adapterów.

`None` = „nie zapisuj" (zostaw, co jest w falowniku), a nie „zero" — to rozróżnienie
jest kontraktem (w Boxie pola `has_*`): `power_w=0` przy standby znaczy „stój",
brak `power_w` przy `auto` znaczy „nie dotykaj rejestru" (oszczędność NVM).
"""
from __future__ import annotations

from dataclasses import dataclass

WRITE_PARAMS: tuple[str, ...] = (
    "soc_min", "soc_max", "power_w", "export_limit_w", "export_limit_enabled", "mode",
)
TOU_FIELDS: tuple[str, ...] = ("start", "power_w", "soc", "grid_charge")


@dataclass(frozen=True)
class TouProgram:
    """Jeden program okna czasowego: od `start_min` (minuta doby) do startu następnego."""

    start_min: int
    power_w: float
    soc: float
    grid_charge: bool


@dataclass(frozen=True)
class Params:
    mode: str | None = None               # nazwa trybu z `profile.modes`
    power_w: float | None = None          # nastawa mocy trybu, strona baterii (W)
    soc_min: float | None = None          # dolny próg SoC (%)
    soc_max: float | None = None          # górny próg SoC (%)
    export_limit_w: float | None = None
    export_limit_enabled: bool | None = None
    tou: tuple[TouProgram, ...] | None = None

    def flatten(self) -> dict[str, float | str]:
        """Płaska mapa klucz → wartość; na niej pracują throttling i uzgadnianie."""
        out: dict[str, float | str] = {}
        for key in WRITE_PARAMS:
            value = getattr(self, key)
            if value is None:
                continue
            if isinstance(value, bool):
                out[key] = 1.0 if value else 0.0
            elif isinstance(value, str):
                out[key] = value
            else:
                out[key] = float(value)
        for i, prog in enumerate(self.tou or (), start=1):
            out[f"tou.{i}.start"] = float(prog.start_min)
            out[f"tou.{i}.power_w"] = float(prog.power_w)
            out[f"tou.{i}.soc"] = float(prog.soc)
            out[f"tou.{i}.grid_charge"] = 1.0 if prog.grid_charge else 0.0
        return out
