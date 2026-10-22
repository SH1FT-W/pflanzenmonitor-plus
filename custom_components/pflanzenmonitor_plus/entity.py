"""Basis aller Entitäten: je Pflanze ein Gerät von Plus."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PlantData, PlusCoordinator
from .live import PlantLive
from .plants import Plant


def device_info(plant: Plant) -> DeviceInfo:
    """Gerät der Pflanze; die Art steht als Modell darunter."""
    return DeviceInfo(
        identifiers={(DOMAIN, plant.key)},
        name=plant.name,
        model=plant.species,
        manufacturer="Pflanzenmonitor Plus",
        suggested_area=plant.area,
    )


class PlantEntity(CoordinatorEntity[PlusCoordinator]):
    """Gelerntes je Pflanze und Merkmal (unique_id „<Pflanze>_<key>“)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PlusCoordinator, plant: Plant, key: str) -> None:
        super().__init__(coordinator)
        self.plant = plant
        self._attr_translation_key = key
        self._attr_unique_id = f"{plant.key}_{key}"
        self._attr_device_info = device_info(plant)

    @property
    def item(self) -> PlantData | None:
        data = self.coordinator.data
        return data.get(self.plant.key) if data else None

    @property
    def available(self) -> bool:
        return super().available and self.item is not None


class LiveEntity(Entity):
    """Messwert, Zustand oder Grenzwert je Pflanze (unique_id „<Pflanze>-<key>“ wie im Plant Monitor)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, live: PlantLive, key: str, unique: str | None = None) -> None:
        self.live = live
        self.plant = live.plant
        self._attr_translation_key = key
        self._attr_unique_id = f"{live.plant.key}-{unique or key}"
        self._attr_device_info = device_info(live.plant)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.live.async_listen(self.async_write_ha_state))
