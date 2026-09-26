"""Znane integracje HA falowników i cen. Rozszerzane danymi (etap profili)."""
from __future__ import annotations

# domena integracji HA -> marka (tylko podpowiedź w raporcie)
INVERTER_DOMAINS: dict[str, str] = {
    "goodwe": "GoodWe",          # core i mletenay (ta sama domena)
    "solarman": "Deye/Solarman", # davidrapan/ha-solarman, StephanJoubert
    "sunsynk": "Deye/Sunsynk",
    "deye": "Deye",
    "solax_modbus": "SolaX/Sofar",
    "huawei_solar": "Huawei",
    "foxess_modbus": "FoxESS",
    "solis_modbus": "Solis",
    "growatt_server": "Growatt",
    "sigen": "Sigenergy",
    "victron": "Victron",
    "solaredge_modbus_multi": "SolarEdge",
    "fronius": "Fronius",
    "sma": "SMA",
}
# producent w rejestrze urządzeń (małe litery, podciąg) — łapie integracje MQTT/modbus YAML
INVERTER_MANUFACTURERS: tuple[str, ...] = (
    "goodwe", "deye", "sunsynk", "solarman", "solis", "ginlong", "huawei", "sungrow",
    "solax", "sofar", "foxess", "growatt", "victron", "fronius", "solaredge", "sma",
    "sigenergy",
)
PRICE_PLATFORMS: frozenset[str] = frozenset({
    "nordpool", "entsoe", "tibber", "octopus_energy", "pstryk", "pstryk_aio", "energyzero",
    "easyenergy", "amber_electric", "epex_spot", "frank_energie",
})
# klucze wpisu konfiguracji, które mogą nieść adres dongla (wszystko inne ignorujemy)
HOST_KEYS: tuple[str, ...] = ("host", "ip_address", "inverter_host", "address")
# słowa w nazwie/unique_id sugerujące zużycie domu (priorytet w kandydatach energii)
LOAD_HINTS: tuple[str, ...] = ("load", "consumption", "house", "home", "zuzycie", "pobor")
MAX_ENERGY_CANDIDATES = 30
