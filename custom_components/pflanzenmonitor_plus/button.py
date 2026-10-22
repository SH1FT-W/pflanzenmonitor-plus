"""Knöpfe je Pflanze (nur mit KI): Vorschlag übernehmen, ablehnen, zurücknehmen, KI jetzt fragen."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .advisor import Advisor
from .coordinator import PlusConfigEntry, PlusCoordinator
from .entity import PlantEntity
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
    if not coordinator.advisor:
        return
    for plant in coordinator.plants.values():
        if plant.moisture:
            async_add_entities(
                [PlusButton(coordinator, plant, description) for description in BUTTONS],
                config_subentry_id=plant.subentry_id,
            )


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
