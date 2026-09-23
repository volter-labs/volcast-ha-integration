"""Wykrywanie instalacji — czysty pakiet (bez importów Home Assistant)."""
from .classify import classify
from .models import (Classification, ConfigEntrySnap, DeviceSnap, EntitySnap,
                     InverterFinding, StateSnap)

__all__ = ["classify", "Classification", "ConfigEntrySnap", "DeviceSnap",
           "EntitySnap", "InverterFinding", "StateSnap"]
