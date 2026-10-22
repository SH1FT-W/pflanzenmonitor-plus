"""Nur für Pflanzen draußen (mit Wetter-Entität): Regen erwartet, Frostgefahr."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PlusConfigEntry, PlusCoordinator
from .entity import PlantEntity
from .plants import Plant

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: PlusConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if not coordinator.weather:
        return
    for plant in coordinator.plants.values():
        if plant.outdoor and plant.moisture:
            async_add_entities(
                [RainExpected(coordinator, plant), FrostRisk(coordinator, plant)], config_subentry_id=plant.subentry_id
            )


class RainExpected(PlantEntity, BinarySensorEntity):
    """Mindestens 1 mm Regen in den nächsten 12 Std. – Gießen kann warten."""

    def __init__(self, coordinator: PlusCoordinator, plant: Plant) -> None:
        super().__init__(coordinator, plant, "rain_expected")

    @property
    def is_on(self) -> bool | None:
        item = self.item
        return None if item is None else item.rain is not None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        rain = self.item.rain if self.item else None
        return {"amount": rain.amount if rain else 0.0, "until": rain.until.isoformat() if rain else None}


class FrostRisk(PlantEntity, BinarySensorEntity):
    """Vorhersage der nächsten 48 Std. unter der Mindesttemperatur der Pflanze."""

    _attr_device_class = BinarySensorDeviceClass.COLD

    def __init__(self, coordinator: PlusCoordinator, plant: Plant) -> None:
        super().__init__(coordinator, plant, "frost")

    @property
    def is_on(self) -> bool | None:
        frost = self.item.frost if self.item else None
        return None if frost is None else frost.active

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        frost = self.item.frost if self.item else None
        if frost is None:
            return None
        return {"lowest": frost.lowest, "at": frost.at.isoformat(), "limit": frost.limit}
