"""Care calendar "Pflanzenpflege": fertilizing and repotting dates of all plants as all-day events."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import care
from .careplan import CarePlans
from .coordinator import PlusConfigEntry
from .plants import Plant, calendar_id

PARALLEL_UPDATES = 0
# The state shows the next event within this time
NEXT_WITHIN = timedelta(days=400)


async def async_setup_entry(
    hass: HomeAssistant, entry: PlusConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([CareCalendar(entry.runtime_data.care, entry.entry_id)])


class CareCalendar(CalendarEntity):
    """Read-only calendar; dates move on with the buttons or the action `care_done`."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "care"

    def __init__(self, plans: CarePlans, entry_id: str) -> None:
        self.plans = plans
        self._attr_unique_id = calendar_id(entry_id)

    def _event(self, plant: Plant, kind: care.Kind, day: date) -> CalendarEvent:
        language = self.hass.config.language
        return CalendarEvent(
            start=day,
            end=day + timedelta(days=1),
            summary=care.summary(kind, plant.name, language),
            description=care.detail(kind, self.plans.plan(plant), language),
            uid=f"{plant.key}-{kind}-{day.isoformat()}",
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Today's or the next event (overdue dates stay on today)."""
        today = dt_util.now().date()
        found = self.plans.upcoming(today, today + NEXT_WITHIN)
        return self._event(*found[0]) if found else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        start = dt_util.as_local(start_date).date()
        end = dt_util.as_local(end_date).date() + timedelta(days=1)
        events = [self._event(*item) for item in self.plans.upcoming(start, end)]
        # All-day events overlapping [start_date, end_date)
        return [e for e in events if e.start_datetime_local < end_date and e.end_datetime_local > start_date]

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.plans.async_listen(self.async_write_ha_state))
