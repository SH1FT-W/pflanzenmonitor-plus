"""Buttons per plant: fertilized, repotted (care calendar on); with AI also accept, reject, undo, ask AI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import care
from .advisor import Advisor
from .careplan import CarePlans
from .coordinator import PlusConfigEntry, PlusCoordinator
from .entity import PlantEntity, device_info
from .plants import Plant

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class PlusButtonDescription(ButtonEntityDescription):
    """Was der Knopf beim Advisor auslöst."""

    press: Callable[[Advisor, str], Awaitable[None]]
    # Nur drückbar in diesen Zuständen des Vorschlags (None = immer)
    when: tuple[str, ...] | None = None


BUTTONS: tuple[PlusButtonDescription, ...] = (
    PlusButtonDescription(key="accept", when=("offen",), press=lambda a, k: a.async_apply(k)),
    PlusButtonDescription(key="reject", when=("offen",), press=lambda a, k: a.async_reject(k)),
    PlusButtonDescription(key="undo", when=("uebernommen",), press=lambda a, k: a.async_undo(k)),
    PlusButtonDescription(key="ask", entity_category=EntityCategory.CONFIG, press=lambda a, k: a.async_ask(k)),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PlusConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    for plant in coordinator.plants.values():
        buttons: list[ButtonEntity] = []
        if coordinator.care:
            buttons += [CareButton(coordinator.care, plant, kind) for kind in care.KINDS]
        if coordinator.advisor and plant.moisture:
            buttons += [PlusButton(coordinator, plant, description) for description in BUTTONS]
        async_add_entities(buttons, config_subentry_id=plant.subentry_id)


class CareButton(ButtonEntity):
    """Fertilized / repotted today: the next date in the care calendar moves on."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, plans: CarePlans, plant: Plant, kind: care.Kind) -> None:
        self.plans = plans
        self.plant = plant
        self.kind: care.Kind = kind
        self._attr_translation_key = kind
        self._attr_unique_id = f"{plant.key}_{kind}"
        self._attr_device_info = device_info(plant)

    async def async_press(self) -> None:
        await self.plans.async_done(self.plant.key, self.kind)


class PlusButton(PlantEntity, ButtonEntity):
    """Ein Knopf je Pflanze und Aktion; Fehler (nichts offen, KI nicht erreichbar) kommen übersetzt beim Nutzer an."""

    entity_description: PlusButtonDescription

    def __init__(self, coordinator: PlusCoordinator, plant: Plant, description: PlusButtonDescription) -> None:
        super().__init__(coordinator, plant, description.key)
        self.entity_description = description
        assert coordinator.advisor is not None
        self.advisor = coordinator.advisor

    @property
    def available(self) -> bool:
        """Übernehmen/Ablehnen nur bei offenem Vorschlag, Zurücknehmen nur direkt nach einer Übernahme."""
        when = self.entity_description.when
        return super().available and (when is None or self.advisor.record(self.plant.key)["state"] in when)

    async def async_press(self) -> None:
        await self.entity_description.press(self.advisor, self.plant.key)
