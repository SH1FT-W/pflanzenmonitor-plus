"""Grenzwerte je Pflanze (Min/Max je Messgröße und der Lux-zu-PPFD-Faktor) – nur zu vorhandenen Sensoren."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    EntityCategory,
    UnitOfConductivity,
    UnitOfRatio,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import health
from .coordinator import PlusConfigEntry
from .entity import LiveEntity
from .live import PlantLive

PARALLEL_UPDATES = 0

# Einheit und Geräteklasse je Messgröße (Temperaturen in °C; HA zeigt sie in der Systemeinheit)
UNITS: dict[str, tuple[str | None, NumberDeviceClass | None]] = {
    "moisture": (PERCENTAGE, NumberDeviceClass.MOISTURE),
    "temperature": (UnitOfTemperature.CELSIUS, NumberDeviceClass.TEMPERATURE),
    "soil_temperature": (UnitOfTemperature.CELSIUS, NumberDeviceClass.TEMPERATURE),
    "humidity": (PERCENTAGE, NumberDeviceClass.HUMIDITY),
    "illuminance": (LIGHT_LUX, NumberDeviceClass.ILLUMINANCE),
    "conductivity": (UnitOfConductivity.MICROSIEMENS_PER_CM, NumberDeviceClass.CONDUCTIVITY),
    "co2": (UnitOfRatio.PARTS_PER_MILLION, NumberDeviceClass.CO2),
    "dli": ("mol/d⋅m²", None),
    "vpd": ("kPa", None),
    "lux_to_ppfd": (None, None),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: PlusConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    for plant in coordinator.plants.values():
        live = coordinator.live.plants[plant.key]
        async_add_entities(
            [LimitNumber(live, name) for name in health.limit_fields(plant.sensors)],
            config_subentry_id=plant.subentry_id,
        )


class LimitNumber(LiveEntity, NumberEntity):
    """Ein Grenzwert; geändert wird er im Store, die Pflanze prüft danach sofort neu."""

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, live: PlantLive, name: str) -> None:
        super().__init__(live, name, name.replace("_", "-"))
        self.field = name
        measurement = name if name == "lux_to_ppfd" else name.split("_", 1)[1]
        limits = health.RANGES[measurement]
        self._attr_native_min_value = limits.low
        self._attr_native_max_value = limits.high
        self._attr_native_step = limits.step
        self._attr_native_unit_of_measurement, self._attr_device_class = UNITS[measurement]

    @property
    def native_value(self) -> float:
        return self.live.limits.get(self.plant.key, self.field)

    async def async_set_native_value(self, value: float) -> None:
        self.live.limits.async_set(self.plant.key, {self.field: value})
